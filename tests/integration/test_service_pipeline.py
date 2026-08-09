"""The phone's-eye view: a live engine answered over a real socket.

``tests/test_service.py`` points the handler at a stub engine so it can assert
on status codes and headers in isolation. Here the server is wired to the
genuine article -- a capture thread, a recorder writing files, a g-sensor -- and
every assertion is about the two of them agreeing: a clip the recorder wrote is
a clip the API lists, a lock pressed over HTTP is a file in ``events/``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.client import HTTPResponse
from typing import Any

import cv2
import numpy as np
import pytest

from tests.integration.conftest import Client, ReplaySource, free_port, level_run, wait_until
from vectra180 import engine as engine_module
from vectra180.config import EngineConfig
from vectra180.engine import Engine
from vectra180.recorder import storage
from vectra180.service.app import serve

pytestmark = pytest.mark.integration

#: The pair every test here works with: the engine, and a socket onto it.
Live = tuple[Engine, Client]

#: Frame geometry once the engine has cropped the metadata strip off the left.
PREVIEW_SIZE = (312, 64)

#: Long enough that no test outlives its script even though the source loops.
SCRIPT_SECONDS = 30


@contextmanager
def running(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Live]:
    """Bring up an engine and a server on it, and take both down afterwards.

    A context manager rather than only a fixture because a couple of tests need
    to change the server's configuration -- a token, most of all -- before it
    binds, and a fixture has already bound by the time a test can touch it.
    """
    source = ReplaySource(level_run(SCRIPT_SECONDS), loop=True)
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: source)
    config.server.enabled = True
    config.server.host = "127.0.0.1"
    config.server.port = free_port()

    engine = Engine(config)
    server = serve(engine, config, block=False)
    client = Client(config.server.port)
    try:
        engine.start()
        # Returns once frame zero has landed; the source holds the rest back
        # until it is armed, so nothing is recorded before there is a recorder.
        engine.begin_recording()
        source.armed.set()
        yield engine, client
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        engine.stop(timeout=15.0)


@pytest.fixture
def live(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Live]:
    """A recording engine reachable over HTTP for the length of one test."""
    with running(config, monkeypatch) as pair:
        yield pair


def first_closed_clip(engine: Engine) -> storage.ClipInfo:
    """Wait for a segment to roll, then hand back the one that closed.

    The listing includes the segment currently being written, whose size grows
    under the test's feet. The oldest entry is the settled one.
    """
    assert wait_until(lambda: engine.recorder.stats.segments_written >= 1), "no segment ever closed"
    clips = storage.list_clips(engine.config.recording)
    assert clips, "the recorder closed a segment the listing cannot see"
    return clips[-1]


def decode(payload: bytes) -> Any:
    """Decode a JPEG body the way the browser receiving it would."""
    return cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)


def read_parts(response: HTTPResponse, count: int, *, limit: int = 4_000_000) -> bytes:
    """Pull an MJPEG stream until ``count`` boundaries have gone past.

    ``read1`` rather than ``read``: the response has no length and never ends,
    so any read that insists on filling its buffer would block past the last
    frame the test cares about.
    """
    body = b""
    while body.count(b"--vectraframe") < count and len(body) < limit:
        chunk = response.read1(4096)
        if not chunk:
            break
        body += chunk
    return body


def test_the_status_endpoint_reports_a_live_pipeline(live: Live) -> None:
    """One request should be enough to tell whether the dashcam is working.

    It is the only feedback a headless box on a windscreen gives, so it has to
    cover the whole chain: camera open, frames arriving, telemetry decoding,
    encoder writing, card not full.
    """
    engine, client = live
    assert wait_until(lambda: engine.recorder.stats.written_frames > 10)

    status = client.json("/api/status")
    assert status["running"] is True
    assert status["error"] == ""
    assert status["frames"] > 0
    assert status["fps"] == pytest.approx(30.0, abs=1.0)
    assert status["camera"]["open"] is True
    assert status["camera"]["backend"] == "REPLAY"
    assert status["telemetry"]["present"] is True
    assert status["telemetry"]["decoded_frames"] > 0
    assert status["telemetry"]["sample"]["accel_z"] == pytest.approx(9.80665, abs=0.01)
    assert status["recorder"]["current_clip"].endswith(".mp4")
    assert status["recorder"]["last_error"] == ""
    assert status["recorder"]["written_frames"] > 0
    assert status["storage"]["total_bytes"] > 0
    assert status["incidents"]["count"] == 0


def test_every_asset_the_page_asks_for_is_served(live: Live) -> None:
    """A UI that ships with a broken reference is a blank screen in a car park.

    The page is parsed rather than trusted, so adding a stylesheet without
    shipping it fails here instead of on someone's phone.
    """
    _, client = live
    page = client.get("/").read().decode("utf-8")
    references = sorted(set(re.findall(r'(?:href|src)="(/static/[^"]+)"', page)))

    assert references, "the bundled page references no assets at all"
    for reference in references:
        response = client.get(reference)
        assert response.status == HTTPStatus.OK, f"{reference} is missing"
        assert response.read(), f"{reference} is empty"


def test_a_recorded_clip_downloads_whole_and_in_ranges(live: Live) -> None:
    """Saving a clip has to give back the file, byte for byte.

    The ranged read matters as much as the whole one: seeking inside an MP4 is
    how a browser's ``<video>`` element plays a segment at all, and a server
    that answers a range with the wrong slice produces a player that scrubs to
    the wrong place with no error anywhere.
    """
    engine, client = live
    clip = first_closed_clip(engine)

    response = client.get(f"/api/clips/{clip.name}")
    assert response.status == HTTPStatus.OK
    assert response.getheader("Accept-Ranges") == "bytes"
    body = response.read()
    assert body == clip.path.read_bytes()

    listed = [entry for entry in client.json("/api/clips")["clips"] if entry["name"] == clip.name]
    assert listed and listed[0]["protected"] is False
    assert listed[0]["duration_seconds"] > 0

    partial = client.get(f"/api/clips/{clip.name}", Range="bytes=16-47")
    assert partial.status == HTTPStatus.PARTIAL_CONTENT
    assert partial.getheader("Content-Range") == f"bytes 16-47/{len(body)}"
    assert partial.read() == body[16:48]


def test_protecting_a_clip_over_http_moves_it_out_of_the_loop(live: Live) -> None:
    """The save button has to survive the drive home.

    Protection is a move into ``events/``, not a flag, so the check is on the
    filesystem: the loop recorder decides what to prune by which directory a
    file is in and never reads the API's answer.
    """
    engine, client = live
    config = engine.config.recording
    clip = first_closed_clip(engine)

    result = json.loads(client.post(f"/api/clips/{clip.name}/protect").read())
    assert result["clip"]["protected"] is True
    assert result["clip"]["category"] == "events"

    assert (config.event_dir / clip.name).is_file()
    assert not (config.normal_dir / clip.name).exists()
    # The sidecar has to travel with it or the clip loses its telemetry.
    assert (config.event_dir / clip.name).with_suffix(".json").is_file()
    assert not (config.normal_dir / clip.name).with_suffix(".json").exists()


def test_deleting_a_clip_over_http_takes_its_sidecar_with_it(live: Live) -> None:
    """Freeing space by hand should free all of it, and stop being listed."""
    engine, client = live
    clip = first_closed_clip(engine)

    assert client.delete(f"/api/clips/{clip.name}").status == HTTPStatus.OK
    assert not clip.path.exists()
    assert not clip.sidecar.exists()
    assert clip.name not in {entry["name"] for entry in client.json("/api/clips")["clips"]}


def test_the_lock_button_protects_the_segment_that_is_open(live: Live) -> None:
    """A near miss the g-sensor shrugged off is what this button is for.

    Pressed early in the run there is no previous segment to keep, so the one
    clip that comes out is the one that was open -- and it is only written when
    the engine stops, which is why the assertion waits for that.
    """
    engine, client = live
    assert wait_until(lambda: engine.recorder.stats.written_frames > 10)

    payload = json.loads(client.post("/api/lock").read())
    assert payload["locked"] is True
    assert payload["incident"]["source"] == "manual"

    # Flushes the open segment so there is a finished file to look at.
    engine.stop(timeout=15.0)
    events = storage.list_clips(engine.config.recording, category="events")
    assert len(events) == 1
    sidecar = json.loads(events[0].sidecar.read_text(encoding="utf-8"))
    assert sidecar["locked"] is True
    assert sidecar["lock_reasons"] == ["manual"]


def test_the_snapshot_endpoint_returns_the_frame_with_and_without_the_overlay(live: Live) -> None:
    """A still is what a phone on a slow link asks for instead of the stream.

    Both forms are checked because they are different products: the overlaid
    one is for a human aiming the camera, the clean one is for anything that
    means to process the pixels.
    """
    engine, client = live
    assert wait_until(lambda: engine.snapshot() is not None)

    response = client.get("/snapshot.jpg")
    assert response.status == HTTPStatus.OK
    assert response.getheader("Content-Type") == "image/jpeg"
    assert response.getheader("Cache-Control") == "no-store"
    overlaid = response.read()
    image = decode(overlaid)
    assert image is not None
    assert image.shape == (PREVIEW_SIZE[1], PREVIEW_SIZE[0], 3)

    plain = client.get("/snapshot.jpg?overlay=0").read()
    assert decode(plain) is not None
    # The scene the replay source paints never changes, so anything that
    # differs between these two is the HUD -- and it must be one or the other.
    assert plain != overlaid


def test_the_depth_endpoint_computes_from_the_live_frame(live: Live) -> None:
    """Depth is the expensive half of the engine, and it runs only on request.

    Nothing here judges the disparity itself -- a synthetic frame has no real
    parallax to find. What it proves is that the split, dewarp, match and
    colourise chain runs end to end on a live frame and returns an image.
    """
    engine, client = live
    assert wait_until(lambda: engine.snapshot() is not None)

    response = client.get("/depth.jpg")
    assert response.status == HTTPStatus.OK
    assert response.getheader("Content-Type") == "image/jpeg"
    depth = decode(response.read())
    assert depth is not None
    assert depth.ndim == 3
    assert depth.shape[2] == 3


def test_the_preview_stream_delivers_frames_until_the_viewer_leaves(live: Live) -> None:
    """The live view is a multipart response that only ends when a tab closes.

    Reading a few parts and hanging up exercises the disconnect path as well as
    the streaming one: the handler treats a dropped peer as routine, and a
    server that logged it as an error would fill the Pi's journal with every
    glance at the camera.
    """
    engine, client = live
    assert wait_until(lambda: engine.snapshot() is not None)

    response = client.get("/stream.mjpg")
    assert response.status == HTTPStatus.OK
    assert response.getheader("Content-Type") == "multipart/x-mixed-replace; boundary=vectraframe"

    body = read_parts(response, 3)
    assert body.count(b"--vectraframe") >= 3
    assert b"Content-Type: image/jpeg" in body

    client.close()
    # The engine has to be entirely unbothered by a viewer walking away.
    frames = engine.recorder.stats.written_frames
    assert wait_until(lambda: engine.recorder.stats.written_frames > frames)
    assert engine.running


def test_recording_stops_and_restarts_over_http(live: Live) -> None:
    """Stopping is what makes the card safe to pull, so it must really flush.

    The clip has to exist the moment the response comes back, not eventually:
    an operator who reads "stopped" and unplugs the Pi a second later should
    still find every frame that was captured before they asked.
    """
    engine, client = live
    config = engine.config.recording
    assert wait_until(lambda: engine.recorder.stats.written_frames > 10)

    assert json.loads(client.post("/api/recording/stop").read())["recording"] is False
    before = {clip.name for clip in storage.list_clips(config)}
    assert before, "stopping the recorder left nothing on the card"
    assert client.json("/api/status")["recorder"]["current_clip"] == ""

    assert json.loads(client.post("/api/recording/start").read())["recording"] is True
    assert wait_until(lambda: {clip.name for clip in storage.list_clips(config)} > before)


def test_a_token_gates_the_api_without_hiding_the_health_check(
    config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dashcam on a car's hotspot is on a network it does not control.

    ``/healthz`` stays open on purpose: systemd and any uptime probe have to be
    able to see the service is alive without being handed the key to the
    footage.
    """
    config.server.token = "a-shared-secret"
    with running(config, monkeypatch) as (engine, client):
        assert wait_until(lambda: engine.snapshot() is not None)

        assert client.get("/api/status").status == HTTPStatus.UNAUTHORIZED
        assert json.loads(client.get("/healthz").read())["status"] == "ok"

        authorised = client.get("/api/status", Authorization="Bearer a-shared-secret")
        assert authorised.status == HTTPStatus.OK
        assert json.loads(authorised.read())["running"] is True

        # An <img> cannot carry a header, so the preview has to take the token
        # in the query string or the UI cannot show a picture at all.
        assert decode(client.get("/snapshot.jpg?token=a-shared-secret").read()) is not None
