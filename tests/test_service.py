"""The HTTP interface: routing, auth, and the hostile-input boundary.

These tests drive a real socket rather than calling handler methods, because
most of what matters here is protocol behaviour -- keep-alive framing, Range
replies, HEAD semantics -- which only exists on the wire.

The engine is stubbed. Everything the service asks of it is a handful of
accessors, and a real one would need a camera thread whose timing would make
these tests flaky for no gain.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http import HTTPStatus
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from vectra180 import __version__
from vectra180.config import EngineConfig
from vectra180.engine import Engine, EngineSnapshot
from vectra180.errors import ServiceError
from vectra180.recorder.incident import Incident, IncidentDetector
from vectra180.recorder.storage import ensure_directories
from vectra180.service import app as service_module
from vectra180.service.app import STATIC_DIR, VectraHTTPServer, serve
from vectra180.telemetry.orientation import Orientation

WALL = 1_786_000_000.0


# -- stubs -------------------------------------------------------------------


class StubRecorder:
    """The three recorder members the service reaches for."""

    def __init__(self) -> None:
        self.running = False
        self.stops = 0
        self.locked: list[str] = []

    def stop(self) -> None:
        self.running = False
        self.stops += 1

    def lock_current(self, reason: str) -> None:
        self.locked.append(reason)


class StubEngine:
    """The slice of :class:`~vectra180.engine.Engine` the HTTP layer uses.

    ``image`` starts as ``None``, which is what the service sees before the
    first frame arrives -- the state every preview endpoint has to survive.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.recorder = StubRecorder()
        self.detector = IncidentDetector(config.incident)
        self.image: np.ndarray | None = None
        self.depth: np.ndarray | None = None
        self.frame_index = 0
        self.begins = 0
        self.depth_calls = 0
        self.overlay_calls: list[bool] = []
        self.panorama_calls: list[bool] = []

    def snapshot(self) -> EngineSnapshot | None:
        if self.image is None:
            return None
        return EngineSnapshot(
            image=self.image,
            sample=None,
            orientation=Orientation(0.0, 0.0, 0.0),
            fps=30.0,
            frame_index=self.frame_index,
            wall_time=WALL,
        )

    def preview_frame(
        self, *, overlay: bool = True, width: int | None = None, panorama: bool = False
    ) -> np.ndarray | None:
        self.overlay_calls.append(overlay)
        self.panorama_calls.append(panorama)
        return self.image

    def compute_depth(self, **_kwargs: Any) -> np.ndarray | None:
        self.depth_calls += 1
        return self.depth

    def lock_incident(self) -> Incident:
        incident = self.detector.trigger_manual(time.monotonic())
        self.recorder.lock_current(incident.source)
        return incident

    def begin_recording(self) -> None:
        self.begins += 1
        self.recorder.running = True

    def status(self) -> dict[str, Any]:
        return {"running": True, "frames": self.frame_index}


# -- client ------------------------------------------------------------------


class Client:
    """A thin wrapper over one keep-alive connection to the test server.

    Deliberately not :mod:`urllib`: these tests need to send a HEAD, a bogus
    ``Range``, or two requests down one socket, and a high-level client hides
    exactly those things.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.connection = HTTPConnection("127.0.0.1", port, timeout=10.0)
        self._pending: HTTPResponse | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HTTPResponse:
        # A keep-alive socket carries one response at a time, and the tests
        # care about headers far more often than bodies -- so finish reading
        # whatever the last call left behind rather than making every test
        # remember to.
        if self._pending is not None and not self._pending.isclosed():
            self._pending.read()
        self.connection.request(method, path, body=body, headers=headers or {})
        self._pending = self.connection.getresponse()
        return self._pending

    def raw(self, method: str, path: str, headers: dict[str, str]) -> HTTPResponse:
        """Send headers with no body, whatever ``Content-Length`` claims.

        Used to exercise the body limit without racing the server: a client
        that actually sends the oversized payload gets its connection reset
        mid-write, and never sees the reply it earned.
        """
        self.connection.putrequest(method, path)
        for key, value in headers.items():
            self.connection.putheader(key, value)
        self.connection.endheaders()
        self._pending = self.connection.getresponse()
        return self._pending

    def get(self, path: str, **headers: str) -> HTTPResponse:
        return self.request("GET", path, headers=headers)

    def post(self, path: str, **headers: str) -> HTTPResponse:
        return self.request("POST", path, headers=headers)

    def delete(self, path: str, **headers: str) -> HTTPResponse:
        return self.request("DELETE", path, headers=headers)

    def json(self, path: str, **headers: str) -> Any:
        return json.loads(self.get(path, **headers).read())

    def close(self) -> None:
        # Closing the connection is not enough to close the socket. A response
        # with no length ends at the close, so ``getresponse`` hands the socket
        # to it and closes the connection there and then -- but the response's
        # file object still holds a reference to the descriptor, and CPython
        # defers the real close until that is released. Without this the server
        # keeps writing into a buffer nobody reads, never sees a broken pipe,
        # and only stops once the buffer is full: two seconds on Windows,
        # longer than the test's patience on a Linux runner.
        if self._pending is not None:
            self._pending.close()
            self._pending = None
        self.connection.close()


def advancing(engine: StubEngine) -> Callable[[], EngineSnapshot | None]:
    """Give the stub a live camera: a new frame index on every look."""

    def snapshot() -> EngineSnapshot | None:
        engine.frame_index += 1
        return StubEngine.snapshot(engine)

    return snapshot


def read_parts(response: HTTPResponse, count: int, *, timeout: float = 10.0) -> bytes:
    """Read from an endless response until ``count`` MJPEG parts have arrived.

    The stream has no end, so a plain ``read()`` would never return; this asks
    for small fixed amounts until enough boundaries have gone past.
    """
    body = b""
    deadline = time.monotonic() + timeout
    while body.count(b"--vectraframe") < count:
        if time.monotonic() > deadline:
            raise AssertionError(f"only {body.count(b'--vectraframe')} of {count} parts arrived")
        body += response.read(64)
    return body


def wait_until_quiet(counter: Callable[[], int], *, timeout: float = 10.0) -> None:
    """Wait for a counter to stop moving, which is how a loop's exit is seen."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        before = counter()
        time.sleep(0.2)
        if counter() == before:
            return
    raise AssertionError("the stream kept pulling frames after the client left")


@pytest.fixture
def service(config: EngineConfig) -> Iterator[tuple[StubEngine, Client]]:
    """A server on an ephemeral port, torn down with the test."""
    ensure_directories(config.recording)
    engine = StubEngine(config)
    server = VectraHTTPServer(("127.0.0.1", 0), cast("Engine", engine), config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(server.server_address[1])
    try:
        yield engine, client
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def add_clip(config: EngineConfig, name: str, *, category: str = "normal", body: bytes = b"data") -> Path:
    path = config.recording.directory / category / name
    path.write_bytes(body)
    return path


# -- liveness and auth -------------------------------------------------------


def test_healthz_reports_the_version(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    assert client.json("/healthz") == {"status": "ok", "version": __version__}


def test_healthz_answers_without_a_token(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    """A supervisor has to be able to watch the service without the operator's secret."""
    config.server.token = "s3cret"
    _, client = service

    assert client.get("/healthz").status == HTTPStatus.OK
    assert client.get("/api/status").status == HTTPStatus.UNAUTHORIZED


def test_no_token_configured_means_no_auth(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    assert client.get("/api/status").status == HTTPStatus.OK


def test_a_bearer_header_is_accepted(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    config.server.token = "s3cret"
    _, client = service

    assert client.get("/api/status", Authorization="Bearer s3cret").status == HTTPStatus.OK


def test_a_query_token_is_accepted(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    """``<img>`` and ``<video>`` cannot send headers, so the preview needs this."""
    config.server.token = "s3cret"
    _, client = service

    assert client.get("/api/status?token=s3cret").status == HTTPStatus.OK


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong",
        "Bearer ",
        "s3cret",
        "Basic czNjcmV0",
        "",
    ],
)
def test_a_bad_authorization_header_is_refused(
    config: EngineConfig, service: tuple[StubEngine, Client], header: str
) -> None:
    config.server.token = "s3cret"
    _, client = service

    assert client.get("/api/status", Authorization=header).status == HTTPStatus.UNAUTHORIZED


def test_unauthorized_advertises_the_scheme(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    config.server.token = "s3cret"
    _, client = service

    response = client.get("/api/status")

    assert response.getheader("WWW-Authenticate") == 'Bearer realm="vectra180"'


def test_a_non_ascii_token_guess_is_refused_not_fatal(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    """``hmac.compare_digest`` rejects non-ASCII text, so the comparison is on bytes.

    Anyone can put a café in a query string; it must cost them a 401, not the
    connection.
    """
    config.server.token = "s3cret"
    _, client = service

    assert client.get("/api/status?token=caf%C3%A9").status == HTTPStatus.UNAUTHORIZED
    assert client.get("/api/status?token=s3cret").status == HTTPStatus.OK


def test_a_non_ascii_configured_token_still_works(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    config.server.token = "café"
    _, client = service

    assert client.get("/api/status?token=caf%C3%A9").status == HTTPStatus.OK


# -- cross-origin ------------------------------------------------------------


def test_a_cross_origin_post_is_refused(service: tuple[StubEngine, Client]) -> None:
    """A form POST needs no preflight, so nothing else would stop it."""
    engine, client = service
    engine.recorder.running = True

    response = client.post("/api/recording/stop", Origin="https://evil.example")

    assert response.status == HTTPStatus.FORBIDDEN
    response.read()
    assert engine.recorder.running is True


def test_a_cross_origin_delete_is_refused(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    _, client = service
    clip = add_clip(config, "VEC_20260809_142530.mp4")

    response = client.delete("/api/clips/VEC_20260809_142530.mp4", Origin="https://evil.example")

    assert response.status == HTTPStatus.FORBIDDEN
    response.read()
    assert clip.exists()


def test_a_same_origin_post_is_allowed(service: tuple[StubEngine, Client]) -> None:
    engine, client = service
    origin = f"http://127.0.0.1:{client.port}"

    response = client.post("/api/lock", Origin=origin, Host=f"127.0.0.1:{client.port}")

    assert response.status == HTTPStatus.OK
    response.read()
    assert engine.recorder.locked == ["manual"]


def test_a_request_with_no_origin_is_allowed(service: tuple[StubEngine, Client]) -> None:
    """curl and the CLI send none; refusing them would break every scripted use."""
    engine, client = service

    assert client.post("/api/lock").status == HTTPStatus.OK
    assert engine.recorder.locked == ["manual"]


def test_a_cross_origin_get_is_allowed(service: tuple[StubEngine, Client]) -> None:
    """Reads change nothing, and the preview is loaded by tags that set Origin."""
    _, client = service

    assert client.get("/api/status", Origin="https://evil.example").status == HTTPStatus.OK


# -- request bodies ----------------------------------------------------------


def test_a_body_is_drained_so_the_next_request_still_parses(service: tuple[StubEngine, Client]) -> None:
    """An unread body gets parsed as the next request line on a keep-alive socket."""
    _, client = service

    first = client.request("POST", "/api/lock", body=b"x" * 512)
    assert first.status == HTTPStatus.OK
    first.read()

    second = client.get("/healthz")
    assert second.status == HTTPStatus.OK
    assert json.loads(second.read())["status"] == "ok"


def test_an_oversized_body_is_refused(service: tuple[StubEngine, Client]) -> None:
    engine, client = service

    response = client.raw("POST", "/api/lock", {"Content-Length": str(128 * 1024)})

    assert response.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert engine.recorder.locked == []


def test_a_refused_body_closes_the_connection(service: tuple[StubEngine, Client]) -> None:
    """The unread bytes are still queued, so the socket can no longer be framed."""
    _, client = service

    response = client.raw("POST", "/api/lock", {"Content-Length": str(128 * 1024)})

    assert response.getheader("Connection") == "close"


def test_a_chunked_body_is_refused(service: tuple[StubEngine, Client]) -> None:
    """The base handler cannot frame chunked input, so it cannot be drained."""
    _, client = service

    response = client.raw("POST", "/api/lock", {"Transfer-Encoding": "chunked"})

    assert response.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_a_malformed_content_length_is_refused(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.raw("POST", "/api/lock", {"Content-Length": "not-a-number"})

    assert response.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


# -- routing -----------------------------------------------------------------


def test_the_root_serves_the_ui(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.get("/")

    assert response.status == HTTPStatus.OK
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert b"<html" in response.read().lower()


def test_a_trailing_slash_is_ignored(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    assert client.get("/api/status/").status == HTTPStatus.OK


def test_an_unknown_route_is_a_404(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.get("/api/nope")

    assert response.status == HTTPStatus.NOT_FOUND
    assert "no route" in json.loads(response.read())["error"]


def test_an_unroutable_method_is_a_404(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.delete("/api/status")

    assert response.status == HTTPStatus.NOT_FOUND
    response.read()


def test_an_unknown_post_route_is_a_404(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.post("/api/nope")

    assert response.status == HTTPStatus.NOT_FOUND
    assert "no route for POST" in json.loads(response.read())["error"]


def test_an_unsupported_method_is_rejected(service: tuple[StubEngine, Client]) -> None:
    """No ``do_PUT`` exists, so the base class answers before any of this code runs."""
    _, client = service

    response = client.request("PUT", "/api/status")

    assert response.status == HTTPStatus.NOT_IMPLEMENTED


def test_a_client_that_hangs_up_mid_response_is_not_an_error(
    service: tuple[StubEngine, Client], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Half a response written to a closed socket is routine, not a fault.

    The break is raised from inside the handler rather than by closing a real
    socket at exactly the right instant, which cannot be timed reliably.
    """
    engine, client = service

    def hang_up() -> dict[str, Any]:
        raise BrokenPipeError("the browser went away")

    monkeypatch.setattr(engine, "status", hang_up)

    with caplog.at_level("ERROR"), pytest.raises(ConnectionError):
        client.get("/api/status", Connection="close")

    assert caplog.text == ""


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("an existing connection was forcibly closed"),
        BrokenPipeError("the browser went away"),
        TimeoutError("the socket sat idle"),
    ],
)
def test_a_hang_up_is_recorded_quietly(
    config: EngineConfig, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """A viewer that disappears is ordinary traffic for a dashcam, not a fault."""
    ensure_directories(config.recording)
    server = VectraHTTPServer(("127.0.0.1", 0), cast("Engine", StubEngine(config)), config)

    try:
        with caplog.at_level("DEBUG"):
            try:
                raise error
            except (ConnectionError, TimeoutError):
                server.handle_error(None, ("127.0.0.1", 51234))
    finally:
        server.server_close()

    assert "hung up" in caplog.text
    assert "Traceback" not in caplog.text


def test_a_genuine_fault_keeps_its_traceback(config: EngineConfig, caplog: pytest.LogCaptureFixture) -> None:
    """Quietening disconnects must not quieten everything else."""
    ensure_directories(config.recording)
    server = VectraHTTPServer(("127.0.0.1", 0), cast("Engine", StubEngine(config)), config)

    try:
        with caplog.at_level("ERROR"):
            try:
                raise ValueError("something genuinely broke")
            except ValueError:
                server.handle_error(None, ("127.0.0.1", 51234))
    finally:
        server.server_close()

    assert "unhandled error while serving" in caplog.text
    assert "ValueError: something genuinely broke" in caplog.text


def test_the_config_endpoint_is_json_serialisable(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    payload = client.json("/api/config")

    assert payload["server"]["port"] == 8080
    assert payload["recording"]["container"] == "mp4"


def test_the_config_endpoint_never_leaks_the_token(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    """It is readable with the token, but a shoulder-surfer should not get it back."""
    config.server.token = "s3cret"
    _, client = service

    body = client.get("/api/config?token=s3cret").read().decode()

    assert "s3cret" not in body


# -- security headers --------------------------------------------------------


@pytest.mark.parametrize("route", ["/", "/healthz", "/api/status", "/api/clips"])
def test_every_response_carries_the_sniffing_guard(service: tuple[StubEngine, Client], route: str) -> None:
    _, client = service

    response = client.get(route)
    response.read()

    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    assert "frame-ancestors 'none'" in (response.getheader("Content-Security-Policy") or "")


def test_a_clip_download_carries_the_sniffing_guard(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    """This response is written directly, so it needs its own header."""
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4")

    response = client.get("/api/clips/VEC_20260809_142530.mp4")
    response.read()

    assert response.getheader("X-Content-Type-Options") == "nosniff"


# -- static assets -----------------------------------------------------------


def test_a_bundled_asset_is_served(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.get("/static/app.css")

    assert response.status == HTTPStatus.OK
    assert response.read() == (STATIC_DIR / "app.css").read_bytes()


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/app.css", "text/css; charset=utf-8"),
        ("/static/app.js", "text/javascript; charset=utf-8"),
        ("/static/theme.js", "text/javascript; charset=utf-8"),
    ],
)
def test_an_asset_declares_a_type_the_browser_will_execute(
    service: tuple[StubEngine, Client], path: str, content_type: str
) -> None:
    """The panel sends ``nosniff``, so a wrong type here is a blank page.

    The types are pinned in the service rather than read from the host's
    MIME database, which reports ``text/plain`` for ``.js`` on some machines.
    """
    _, client = service

    response = client.get(path)

    assert response.status == HTTPStatus.OK
    assert response.getheader("Content-Type") == content_type
    assert response.getheader("X-Content-Type-Options") == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/static/../app.py",
        "/static/../../config.py",
        "/static/..%2f..%2fapp.py",
        "/static/%2e%2e/%2e%2e/app.py",
        "/static//etc/passwd",
        "/static/nope.css",
        "/static/",
    ],
)
def test_no_path_escapes_the_asset_directory(service: tuple[StubEngine, Client], path: str) -> None:
    _, client = service

    response = client.get(path)

    assert response.status == HTTPStatus.NOT_FOUND
    response.read()


def test_a_directory_is_not_an_asset(service: tuple[StubEngine, Client]) -> None:
    """``is_file()`` has to run before the containment check, not after."""
    _, client = service

    assert client.get("/static/.").status == HTTPStatus.NOT_FOUND


# -- previews ----------------------------------------------------------------


def test_a_snapshot_before_the_first_frame_is_unavailable(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.get("/snapshot.jpg")

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert "no frame" in json.loads(response.read())["error"]


def test_a_snapshot_is_a_jpeg(service: tuple[StubEngine, Client], raw_frame: np.ndarray) -> None:
    engine, client = service
    engine.image = raw_frame

    response = client.get("/snapshot.jpg")
    body = response.read()

    assert response.getheader("Content-Type") == "image/jpeg"
    assert response.getheader("Cache-Control") == "no-store"
    assert body.startswith(b"\xff\xd8")  # SOI


def test_the_overlay_can_be_switched_off(service: tuple[StubEngine, Client], raw_frame: np.ndarray) -> None:
    engine, client = service
    engine.image = raw_frame

    client.get("/snapshot.jpg?overlay=0").read()
    client.get("/snapshot.jpg?overlay=1").read()
    client.get("/snapshot.jpg").read()

    assert engine.overlay_calls == [False, True, True]


def test_the_panorama_view_is_opt_in(service: tuple[StubEngine, Client], raw_frame: np.ndarray) -> None:
    """Only ``view=pano`` costs a dewarp; anything else serves the raw frame."""
    engine, client = service
    engine.image = raw_frame

    client.get("/snapshot.jpg").read()
    client.get("/snapshot.jpg?view=raw").read()
    client.get("/snapshot.jpg?view=nonsense").read()
    client.get("/snapshot.jpg?view=pano").read()

    assert engine.panorama_calls == [False, False, False, True]


def test_depth_is_only_computed_on_request(service: tuple[StubEngine, Client], raw_frame: np.ndarray) -> None:
    """It costs far more than a preview frame, so nothing else may trigger it."""
    engine, client = service
    engine.image = raw_frame

    client.get("/snapshot.jpg").read()
    assert engine.depth_calls == 0

    engine.depth = raw_frame
    response = client.get("/depth.jpg")
    assert response.read().startswith(b"\xff\xd8")
    assert engine.depth_calls == 1


def test_depth_before_the_first_frame_is_unavailable(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    assert client.get("/depth.jpg").status == HTTPStatus.SERVICE_UNAVAILABLE


def test_the_stream_announces_its_boundary(service: tuple[StubEngine, Client]) -> None:
    """A HEAD describes the stream; only a GET may open the endless body."""
    _, client = service

    response = client.request("HEAD", "/stream.mjpg")

    assert response.status == HTTPStatus.OK
    assert response.getheader("Content-Type") == "multipart/x-mixed-replace; boundary=vectraframe"
    assert response.read() == b""


def test_the_stream_delivers_frames(service: tuple[StubEngine, Client], raw_frame: np.ndarray) -> None:
    engine, client = service
    engine.image = raw_frame
    engine.config.server.preview_fps = 30

    response = client.get("/stream.mjpg")
    chunk = response.read(64)
    response.close()

    assert chunk.startswith(b"--vectraframe\r\nContent-Type: image/jpeg\r\n")


def test_the_stream_keeps_sending_while_the_camera_runs(
    service: tuple[StubEngine, Client], raw_frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One part per captured frame, paced by ``preview_fps``.

    The stub's frame index has to advance for this: the loop deliberately skips
    a frame it has already sent, so a frozen index produces exactly one part.
    """
    engine, client = service
    engine.image = raw_frame
    engine.config.server.preview_fps = 60
    monkeypatch.setattr(engine, "snapshot", advancing(engine))

    response = client.get("/stream.mjpg")
    body = read_parts(response, 3)
    response.close()

    assert body.count(b"--vectraframe") >= 3


def test_the_stream_ends_when_the_viewer_goes_away(
    service: tuple[StubEngine, Client], raw_frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing signals the handler to stop; the failed write is the only notice.

    Left running, every closed tab would leak a thread encoding JPEGs nobody
    reads -- on a Pi that is the encoder's CPU.
    """
    engine, client = service
    engine.image = raw_frame
    engine.config.server.preview_fps = 60
    monkeypatch.setattr(engine, "snapshot", advancing(engine))

    response = client.get("/stream.mjpg")
    read_parts(response, 1)
    client.close()

    wait_until_quiet(lambda: len(engine.overlay_calls))


def test_a_frame_that_will_not_encode_is_a_server_error(
    service: tuple[StubEngine, Client], raw_frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct from "no frame yet": there is one, and it could not be sent."""
    engine, client = service
    engine.image = raw_frame
    monkeypatch.setattr(service_module.cv2, "imencode", lambda *_a, **_k: (False, np.empty(0, dtype=np.uint8)))

    response = client.get("/snapshot.jpg")

    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "JPEG encoding failed" in json.loads(response.read())["error"]


# -- clips -------------------------------------------------------------------


def test_the_clip_listing_is_newest_first(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4")
    add_clip(config, "VEC_20260809_150000.mp4")
    add_clip(config, "VEC_20260809_144500.mp4", category="events")

    names = [clip["name"] for clip in client.json("/api/clips")["clips"]]

    assert names == ["VEC_20260809_150000.mp4", "VEC_20260809_144500.mp4", "VEC_20260809_142530.mp4"]


def test_storage_stats_are_reported(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4", body=b"x" * 100)

    stats = client.json("/api/storage")

    assert stats["normal_bytes"] == 100
    assert stats["normal_clips"] == 1
    assert stats["event_clips"] == 0
    assert stats["total_bytes"] > 0


def test_a_clip_downloads_whole(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4", body=bytes(range(256)))

    response = client.get("/api/clips/VEC_20260809_142530.mp4")
    body = response.read()

    assert response.status == HTTPStatus.OK
    assert body == bytes(range(256))
    assert response.getheader("Accept-Ranges") == "bytes"
    assert response.getheader("Content-Disposition") == 'attachment; filename="VEC_20260809_142530.mp4"'


def test_an_event_clip_downloads_too(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    """Nothing in the URL says which folder it is in; the inventory decides."""
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4", category="events", body=b"locked")

    assert client.get("/api/clips/VEC_20260809_142530.mp4").read() == b"locked"


def test_a_head_on_a_clip_sends_no_body(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4", body=bytes(range(256)))

    response = client.request("HEAD", "/api/clips/VEC_20260809_142530.mp4")

    assert response.getheader("Content-Length") == "256"
    assert response.read() == b""


@pytest.mark.parametrize(
    ("header", "expected", "content_range"),
    [
        ("bytes=0-9", bytes(range(10)), "bytes 0-9/256"),
        ("bytes=10-19", bytes(range(10, 20)), "bytes 10-19/256"),
        ("bytes=250-", bytes(range(250, 256)), "bytes 250-255/256"),
        ("bytes=0-9999", bytes(range(256)), "bytes 0-255/256"),
        ("bytes=-8", bytes(range(248, 256)), "bytes 248-255/256"),
    ],
)
def test_ranges_are_honoured(
    config: EngineConfig,
    service: tuple[StubEngine, Client],
    header: str,
    expected: bytes,
    content_range: str,
) -> None:
    """Seeking inside an MP4 in a browser is entirely built on these."""
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4", body=bytes(range(256)))

    response = client.get("/api/clips/VEC_20260809_142530.mp4", Range=header)
    body = response.read()

    assert response.status == HTTPStatus.PARTIAL_CONTENT
    assert response.getheader("Content-Range") == content_range
    assert body == expected


@pytest.mark.parametrize("header", ["bytes=300-", "bytes=300-400", "bytes=200-100", "bytes=-0"])
def test_an_unsatisfiable_range_is_reported(
    config: EngineConfig, service: tuple[StubEngine, Client], header: str
) -> None:
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4", body=bytes(range(256)))

    response = client.get("/api/clips/VEC_20260809_142530.mp4", Range=header)
    response.read()

    assert response.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
    assert response.getheader("Content-Range") == "bytes */256"


@pytest.mark.parametrize("header", ["bytes=-", "items=0-9", "0-9", "bytes=abc-def", ""])
def test_a_meaningless_range_header_sends_the_whole_file(
    config: EngineConfig, service: tuple[StubEngine, Client], header: str
) -> None:
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4", body=bytes(range(256)))

    response = client.get("/api/clips/VEC_20260809_142530.mp4", Range=header)
    body = response.read()

    assert response.status == HTTPStatus.OK
    assert body == bytes(range(256))


def test_an_unknown_clip_is_a_404(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.get("/api/clips/VEC_20260809_142530.mp4")

    assert response.status == HTTPStatus.NOT_FOUND
    response.read()


@pytest.mark.parametrize(
    "name",
    [
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e%2fVEC_20260809_142530.mp4",
        "VEC_20260809_142530.mp4%00.txt",
        "held.mp4",
        "%20",
    ],
)
def test_no_clip_name_escapes_the_recording_directory(service: tuple[StubEngine, Client], name: str) -> None:
    _, client = service

    response = client.get(f"/api/clips/{name}")

    assert response.status == HTTPStatus.NOT_FOUND
    response.read()


def test_a_traversal_cannot_read_a_neighbouring_file(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    """The decisive check: a real file just outside the tree stays unreachable."""
    _, client = service
    secret = config.recording.directory.parent / "secret.mp4"
    secret.write_bytes(b"private")

    response = client.get("/api/clips/..%2fsecret.mp4")

    assert response.status == HTTPStatus.NOT_FOUND
    assert b"private" not in response.read()


# -- state changes -----------------------------------------------------------


def test_the_lock_button_protects_the_current_segment(service: tuple[StubEngine, Client]) -> None:
    engine, client = service

    payload = json.loads(client.post("/api/lock").read())

    assert payload["locked"] is True
    assert payload["incident"]["source"] == "manual"
    assert engine.recorder.locked == ["manual"]


def test_protecting_a_clip_moves_it(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    _, client = service
    add_clip(config, "VEC_20260809_142530.mp4")

    payload = json.loads(client.post("/api/clips/VEC_20260809_142530.mp4/protect").read())

    assert payload["clip"]["protected"] is True
    assert (config.recording.directory / "events" / "VEC_20260809_142530.mp4").exists()
    assert not (config.recording.directory / "normal" / "VEC_20260809_142530.mp4").exists()


def test_protecting_an_unknown_clip_is_a_404(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.post("/api/clips/VEC_20260809_142530.mp4/protect")

    assert response.status == HTTPStatus.NOT_FOUND
    response.read()


def test_recording_can_be_started_and_stopped(service: tuple[StubEngine, Client]) -> None:
    engine, client = service

    assert json.loads(client.post("/api/recording/start").read()) == {"recording": True}
    assert engine.begins == 1

    assert json.loads(client.post("/api/recording/stop").read()) == {"recording": False}
    assert engine.recorder.stops == 1


def test_a_clip_can_be_deleted(config: EngineConfig, service: tuple[StubEngine, Client]) -> None:
    _, client = service
    clip = add_clip(config, "VEC_20260809_142530.mp4")

    assert json.loads(client.delete("/api/clips/VEC_20260809_142530.mp4").read()) == {"deleted": True}
    assert not clip.exists()


def test_deleting_an_unknown_clip_is_a_404(service: tuple[StubEngine, Client]) -> None:
    _, client = service

    response = client.delete("/api/clips/VEC_20260809_142530.mp4")

    assert response.status == HTTPStatus.NOT_FOUND
    response.read()


# -- serve() -----------------------------------------------------------------


def test_serve_returns_a_running_server(config: EngineConfig) -> None:
    config.server.port = 0
    engine = StubEngine(config)

    server = serve(cast("Engine", engine), config, block=False)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10.0)
        connection.request("GET", "/healthz")
        assert connection.getresponse().status == HTTPStatus.OK
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_serve_blocks_until_the_server_is_shut_down(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """How the systemd unit runs it: the call lasts the life of the process.

    The server is captured on the way past because a blocking call cannot
    return the object whose port the test needs.
    """
    config.server.port = 0
    servers: list[VectraHTTPServer] = []

    def build(address: tuple[str, int], engine: Engine, cfg: EngineConfig) -> VectraHTTPServer:
        server = VectraHTTPServer(address, engine, cfg)
        servers.append(server)
        return server

    monkeypatch.setattr(service_module, "VectraHTTPServer", build)
    caller = threading.Thread(target=serve, args=(cast("Engine", StubEngine(config)), config), daemon=True)
    caller.start()

    deadline = time.monotonic() + 5.0
    while not servers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert servers, "serve() never opened a socket"

    connection = HTTPConnection("127.0.0.1", servers[0].server_address[1], timeout=10.0)
    connection.request("GET", "/healthz")
    assert connection.getresponse().status == HTTPStatus.OK
    connection.close()

    servers[0].shutdown()
    caller.join(timeout=5.0)

    assert not caller.is_alive()


def test_serve_reports_an_unusable_address(config: EngineConfig) -> None:
    config.server.host = "203.0.113.1"  # TEST-NET-3: not assignable here
    config.server.port = 0
    engine = StubEngine(config)

    with pytest.raises(ServiceError, match="could not bind"):
        serve(cast("Engine", engine), config, block=False)


def test_a_public_bind_without_a_token_warns(config: EngineConfig, caplog: pytest.LogCaptureFixture) -> None:
    """Publishing every recorded clip to the LAN must never be silent.

    The warning is emitted before the bind, so an unassignable address proves
    it without this test actually opening a socket to the network.
    """
    config.server.host = "203.0.113.1"
    config.server.port = 0
    config.server.token = ""
    engine = StubEngine(config)

    with caplog.at_level("WARNING"), pytest.raises(ServiceError):
        serve(cast("Engine", engine), config, block=False)

    assert "no token set" in caplog.text


def test_a_public_bind_with_a_token_is_quiet(config: EngineConfig, caplog: pytest.LogCaptureFixture) -> None:
    config.server.host = "203.0.113.1"
    config.server.port = 0
    config.server.token = "s3cret"
    engine = StubEngine(config)

    with caplog.at_level("WARNING"), pytest.raises(ServiceError):
        serve(cast("Engine", engine), config, block=False)

    assert "no token set" not in caplog.text


def test_binding_asks_no_resolver_what_the_address_is_called(
    config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A car has no DNS, and the base class blocks on a reverse lookup at bind.

    ``HTTPServer.server_bind`` resolves the bound address into a hostname
    between binding the socket and listening on it, so a resolver that never
    answers holds the UI down for its whole timeout -- on a machine that has
    no uplink by design. Nothing here reads the name back.
    """

    def _refuse(*args: object, **kwargs: object) -> str:
        raise AssertionError("bind asked a resolver to name the address")

    monkeypatch.setattr(socket, "getfqdn", _refuse)
    monkeypatch.setattr(socket, "gethostbyaddr", _refuse)
    config.server.host = "127.0.0.1"
    config.server.port = 0
    engine = StubEngine(config)

    server = serve(cast("Engine", engine), config, block=False)
    try:
        assert server.server_name == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()


def test_an_ipv6_host_binds_on_an_ipv6_socket(config: EngineConfig) -> None:
    """``AF_INET`` cannot carry ``::1``, and a host with a colon is IPv6 by construction."""
    config.server.host = "::1"
    config.server.port = 0
    engine = StubEngine(config)

    server = serve(cast("Engine", engine), config, block=False)
    try:
        connection = HTTPConnection("::1", server.server_address[1], timeout=10.0)
        connection.request("GET", "/healthz")
        assert connection.getresponse().status == HTTPStatus.OK
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
