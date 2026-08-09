"""Camera to playable file: the path a dashcam exists to walk.

Nothing below the camera is stubbed. Frames go through the real capture loop,
the real segmenter, a real ``cv2.VideoWriter`` and real retention, and the
assertions are made against the files that come out -- decoded with OpenCV,
because a clip that will not play back is not a recording.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import pytest

from tests.integration.conftest import IMPACT, IMPACT_DEVIATION_G, ReplaySource, level_run, wait_until
from vectra180 import engine as engine_module
from vectra180.config import EngineConfig
from vectra180.engine import Engine
from vectra180.recorder import storage

pytestmark = pytest.mark.integration

#: Frame geometry once the engine has cropped the metadata strip off the left.
RECORDED_SIZE = (312, 64)


def replay(config: EngineConfig, source: ReplaySource, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Run a script to its end and hand back the stopped engine.

    Returns only once the capture thread has finished, and it finishes after
    its ``finally`` has flushed the recorder -- so the tree on disk is final
    and no test here has to sleep and hope.
    """
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: source)
    engine = Engine(config)
    engine.start()
    engine.begin_recording()
    source.armed.set()

    finished = wait_until(lambda: not engine.running, timeout=60.0)
    engine.stop(timeout=15.0)
    assert finished, "the capture thread outlived its script"
    return engine


def read_sidecar(clip: storage.ClipInfo) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(clip.sidecar.read_text(encoding="utf-8"))
    return payload


def started(clip: storage.ClipInfo) -> datetime:
    """When a clip began, which its own name always carries."""
    assert clip.started_at is not None, f"{clip.name} has no parseable timestamp"
    return clip.started_at


def decoded_frames(path: Path) -> int:
    """Play a clip back and count the frames that actually come out."""
    capture = cv2.VideoCapture(str(path))
    assert capture.isOpened(), f"OpenCV could not open {path.name}"
    try:
        count = 0
        while True:
            ok, image = capture.read()
            if not ok:
                return count
            assert image.shape == (RECORDED_SIZE[1], RECORDED_SIZE[0], 3)
            count += 1
    finally:
        capture.release()


def test_a_scripted_run_becomes_playable_clips_with_matching_sidecars(
    config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twelve seconds at a five-second segment length is three files.

    The sidecar is checked against the decoded video rather than on its own: it
    is what the listing, the UI timeline and the retention pass all read, so a
    sidecar that disagrees with its clip is worse than no sidecar at all.
    """
    engine = replay(config, ReplaySource(level_run(12)), monkeypatch)

    clips = sorted(storage.list_clips(config.recording), key=lambda clip: clip.name)
    assert [clip.category for clip in clips] == ["normal"] * 3
    assert clips[0].name == "VEC_20260809_142530.mp4"
    assert engine.recorder.stats.segments_written == 3

    for clip in clips:
        sidecar = read_sidecar(clip)
        played = decoded_frames(clip.path)
        assert storage.parse_clip_time(clip.name) is not None
        assert clip.size_bytes > 0
        assert sidecar["clip"] == clip.name
        assert (sidecar["width"], sidecar["height"]) == RECORDED_SIZE
        assert sidecar["locked"] is False
        assert played > 0
        # One frame of slack: which side of the trailer a muxer counts the last
        # frame on is its business, not the recorder's.
        assert abs(played - sidecar["frames"]) <= 1
        assert clip.duration_seconds == pytest.approx(sidecar["frames"] / 30.0, abs=0.01)


def test_every_segment_carries_the_telemetry_that_was_captured_with_it(
    config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar is the only place the IMU survives -- the pixels do not hold it.

    Offsets are relative to the segment rather than the session, so a player can
    seek to a sample without knowing which clip of the run it came from.
    """
    replay(config, ReplaySource(level_run(11)), monkeypatch)

    for clip in storage.list_clips(config.recording):
        samples = read_sidecar(clip)["telemetry"]
        assert samples, f"{clip.name} recorded no telemetry"
        offsets = [sample["offset_seconds"] for sample in samples]
        assert offsets == sorted(offsets)
        assert offsets[0] == 0.0
        assert offsets[-1] < config.recording.segment_seconds
        assert all(sample["accel_z"] == pytest.approx(9.80665, abs=0.01) for sample in samples)


def test_an_impact_protects_the_open_clip_and_the_one_before_it(
    config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run-up lives in the file that just closed, and it is the useful part.

    The impact lands near the end of the second segment, so protecting only
    what is open would keep the aftermath and lose the approach. Recording then
    carries on into a third, ordinary clip: an incident locks footage, it does
    not end the session.
    """
    script = [*level_run(9), IMPACT, *level_run(3)]
    engine = replay(config, ReplaySource(script), monkeypatch)

    events = sorted(storage.list_clips(config.recording, category="events"), key=lambda clip: clip.name)
    normal = storage.list_clips(config.recording, category="normal")

    assert len(events) == 2
    assert events[0].name == "VEC_20260809_142530.mp4"
    assert all(clip.protected for clip in events)
    assert len(normal) == 1
    assert started(normal[0]) > started(events[1])

    # Only the segment that was open when it happened records why it was kept.
    # The earlier one had already closed and is protected by association.
    assert read_sidecar(events[0])["locked"] is False
    assert read_sidecar(events[1])["locked"] is True
    assert read_sidecar(events[1])["lock_reasons"] == ["gsensor"]

    status = engine.status()
    assert status["incidents"]["count"] == 1
    assert status["incidents"]["peak_g"] == pytest.approx(IMPACT_DEVIATION_G, abs=0.01)
    assert engine.recorder.stats.incidents_locked == 1


def test_the_loop_reclaims_space_without_ever_touching_an_event_clip(
    config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full card must cost the oldest ordinary footage and nothing else.

    The event clip here is also the oldest file in the tree, so an age-only
    pruner takes it first -- which is exactly the failure that leaves a dashcam
    worthless after a crash.
    """
    config.recording.max_bytes = 1
    script = [*level_run(4), IMPACT, *level_run(15)]
    engine = replay(config, ReplaySource(script), monkeypatch)

    events = storage.list_clips(config.recording, category="events")
    normal = storage.list_clips(config.recording, category="normal")

    assert [clip.name for clip in events] == ["VEC_20260809_142530.mp4"]
    assert engine.recorder.stats.segments_written == 4
    # Retention stops at the last remaining clip rather than emptying the loop.
    assert len(normal) == 1
    assert started(normal[0]) > started(events[0])
    # A pruned clip takes its sidecar with it; an orphan would be listed as a
    # clip of unknown duration for as long as the card lives.
    assert sorted(path.name for path in config.recording.normal_dir.iterdir()) == [
        f"{normal[0].path.stem}.json",
        f"{normal[0].path.stem}.mp4",
    ]


def test_a_camera_with_no_telemetry_strip_still_records(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not every dual-fisheye module embeds an IMU block.

    On one that does not, the leftmost column is ordinary image data. That
    costs the horizon and the g-sensor, and it has to cost nothing else:
    footage is the duty, telemetry is the garnish.
    """
    engine = replay(config, ReplaySource(level_run(7), telemetry=False), monkeypatch)

    clips = storage.list_clips(config.recording)
    assert len(clips) == 2
    assert all(clip.size_bytes > 0 for clip in clips)
    assert all(decoded_frames(clip.path) > 0 for clip in clips)
    assert all(read_sidecar(clip)["telemetry"] == [] for clip in clips)

    status = engine.status()
    assert status["telemetry"]["present"] is False
    assert status["telemetry"]["sample"] is None
    assert status["incidents"]["count"] == 0
    assert status["recorder"]["written_frames"] > 0
