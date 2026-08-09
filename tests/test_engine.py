"""The capture pipeline.

The engine's loop is where telemetry, incident detection, recording and preview
meet. Most of these tests drive :meth:`Engine._process` directly with frames
whose capture times are *chosen* rather than measured -- that is the only way to
assert on the frame-rate average and on the dt guards around the orientation
filter without racing the wall clock.

Lifecycle and failure paths do use the real capture thread, with
:class:`~tests.conftest.FakeCameraSource` standing in for the camera and
:class:`FakeWriter` for the encoder, so what is under test stays the engine
rather than libx264.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
import pytest

from tests.conftest import (
    FIRST_TIMESTAMP_US,
    FRAME_HEIGHT,
    FRAME_INTERVAL_US,
    FRAME_WIDTH,
    METADATA_WIDTH,
    FakeCameraSource,
    encode_payload,
    make_frame,
    make_strip,
)
from vectra180 import engine as engine_module
from vectra180.capture import Frame
from vectra180.config import EngineConfig, RecordingConfig
from vectra180.engine import Engine
from vectra180.errors import CaptureError
from vectra180.recorder import segmenter as segmenter_module

#: Capture time of the first hand-built frame. Deliberately not near zero, so a
#: stray absolute comparison shows up rather than passing by accident.
START = 1_000.0

#: Wall-clock instant of that frame. Only its differences matter.
WALL = 1_786_000_000.0

#: Nominal frame interval, matching the fake camera's 30fps.
STEP = 1.0 / 30.0

#: Vertical acceleration, in g, that clears the default 0.6g incident
#: threshold. Kept under 2.0 because the wire format holds raw LSB as int16.
IMPACT_G = 1.9


def frame_at(
    index: int,
    *,
    monotonic: float | None = None,
    accel_g: tuple[float, float, float] = (0.0, 0.0, 1.0),
    telemetry: bool = True,
    image: np.ndarray | None = None,
) -> Frame:
    """A frame captured at an exactly known instant.

    The sensor clock advances with ``index`` because the decoder only trusts a
    sample once a second frame continues its timeline -- so a test that wants
    telemetry has to hand over a run of frames, not a single one.
    """
    base = make_frame() if image is None else image.copy()
    if telemetry:
        payload = encode_payload(
            timestamp_us=FIRST_TIMESTAMP_US + index * FRAME_INTERVAL_US,
            accel_g=accel_g,
        )
        base[:, :METADATA_WIDTH] = make_strip(payload, height=base.shape[0])
    return Frame(
        image=base,
        index=index,
        monotonic=START + index * STEP if monotonic is None else monotonic,
        wall_time=WALL + index * STEP,
    )


def feed(engine: Engine, count: int, **kwargs: Any) -> None:
    """Push a run of consecutive frames through the pipeline."""
    for index in range(count):
        engine._process(frame_at(index, **kwargs))


def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Block until a background thread has caught up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("the engine did not reach the expected state in time")


def raise_oserror(_config: RecordingConfig) -> NoReturn:
    """Stand-in for ``storage_stats`` on a volume that has gone away."""
    raise OSError("recording volume went away")


class UnopenableSource(FakeCameraSource):
    """A camera that is not there."""

    def open(self) -> None:
        raise CaptureError("no camera at index 0")


class DyingSource(FakeCameraSource):
    """A camera that is unplugged mid-drive."""

    def frames(self, *, reconnect: bool = True) -> Iterator[Frame]:
        self.open()
        for _ in range(2):
            frame = self.read()
            assert frame is not None
            yield frame
        raise CaptureError("camera vanished")


class ExhaustedSource(FakeCameraSource):
    """A camera whose frame generator ends rather than raising.

    Nothing in the field does this, but the capture loop still has to leave the
    recorder flushed when it happens, so it is worth pinning.
    """

    def frames(self, *, reconnect: bool = True) -> Iterator[Frame]:
        self.open()
        for _ in range(2):
            frame = self.read()
            assert frame is not None
            yield frame


class FakeWriter:
    """Stands in for the encoder, keeping whatever it was asked to write."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.frames: list[np.ndarray] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, frame: np.ndarray) -> None:
        self.frames.append(frame.copy())

    def close(self) -> None:
        return None


@pytest.fixture
def writers(monkeypatch: pytest.MonkeyPatch) -> list[FakeWriter]:
    """Replace the encoder factory; collect every writer the recorder opens."""
    created: list[FakeWriter] = []

    def factory(path: Path, _size: tuple[int, int], _fps: float, _config: RecordingConfig) -> FakeWriter:
        writer = FakeWriter(path)
        created.append(writer)
        return writer

    monkeypatch.setattr(segmenter_module, "create_writer", factory)
    return created


@pytest.fixture
def source() -> FakeCameraSource:
    return FakeCameraSource()


@pytest.fixture
def engine(config: EngineConfig, source: FakeCameraSource, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: source)
    instance = Engine(config)
    yield instance
    instance.stop(timeout=5.0)


@pytest.fixture
def recording(engine: Engine, writers: list[FakeWriter]) -> Engine:
    """An engine that has seen a frame and has the recorder running.

    ``writers`` is requested for its patch: without it the recorder would open
    a real encoder.
    """
    del writers
    feed(engine, 1)
    engine.begin_recording()
    return engine


# -- lifecycle ---------------------------------------------------------------


def test_a_fresh_engine_has_nothing_to_show(engine: Engine) -> None:
    assert engine.snapshot() is None
    assert not engine.running
    assert not engine.recorder.running


def test_starting_opens_the_camera_and_captures(engine: Engine, source: FakeCameraSource) -> None:
    engine.start()
    assert engine.running
    assert source.is_open
    wait_until(lambda: engine.snapshot() is not None)


def test_starting_twice_keeps_one_capture_thread(engine: Engine) -> None:
    engine.start()
    thread = engine._thread
    engine.start()
    assert engine._thread is thread


def test_a_camera_that_will_not_open_fails_loudly(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: UnopenableSource())
    instance = Engine(config)
    with pytest.raises(CaptureError, match="no camera"):
        instance.start()
    assert not instance.running


def test_stopping_releases_the_camera_and_the_recorder(engine: Engine, source: FakeCameraSource) -> None:
    engine.start()
    wait_until(lambda: engine.snapshot() is not None)
    engine.stop(timeout=5.0)
    assert not engine.running
    assert source.closed
    assert not engine.recorder.running


def test_stopping_an_engine_that_never_started_is_harmless(engine: Engine) -> None:
    engine.stop(timeout=1.0)
    assert not engine.running


def test_the_context_manager_runs_the_pipeline(engine: Engine, source: FakeCameraSource) -> None:
    with engine as running:
        assert running is engine
        wait_until(lambda: engine.snapshot() is not None)
    assert not engine.running
    assert source.closed


def test_a_camera_that_disappears_is_reported(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: DyingSource())
    instance = Engine(config)
    instance.start()
    wait_until(lambda: not instance.running)
    assert "camera vanished" in instance.status()["error"]
    instance.stop(timeout=5.0)


def test_a_frame_stream_that_simply_ends_is_not_an_error(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generator that returns leaves through the same flush, but reports nothing.

    Only :class:`CaptureError` means the camera failed; running out of frames
    must not put a message in front of the operator.
    """
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: ExhaustedSource())
    instance = Engine(config)
    instance.start()
    wait_until(lambda: not instance.running)

    assert instance.status()["error"] == ""
    assert not instance.recorder.running
    instance.stop(timeout=5.0)


# -- frame processing --------------------------------------------------------


def test_the_metadata_strip_is_removed_from_the_published_frame(engine: Engine) -> None:
    feed(engine, 1)
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.image.shape[1] == FRAME_WIDTH - METADATA_WIDTH


def test_telemetry_appears_once_a_second_frame_confirms_the_clock(engine: Engine) -> None:
    engine._process(frame_at(0))
    first = engine.snapshot()
    assert first is not None
    assert first.sample is None

    engine._process(frame_at(1))
    second = engine.snapshot()
    assert second is not None
    assert second.sample is not None
    assert second.sample.timestamp_us == FIRST_TIMESTAMP_US + FRAME_INTERVAL_US


def test_disabling_telemetry_still_crops_the_strip(engine: Engine) -> None:
    engine.config.telemetry.enabled = False
    feed(engine, 3)
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.sample is None
    assert snapshot.image.shape[1] == FRAME_WIDTH - METADATA_WIDTH
    assert engine.decoder.decoded_frames == 0


def test_a_module_without_a_strip_reports_no_telemetry(engine: Engine) -> None:
    engine.config.telemetry.metadata_width = 0
    feed(engine, 3, telemetry=False)
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.sample is None
    assert snapshot.image.shape[1] == FRAME_WIDTH


def test_the_snapshot_carries_the_frames_identity(engine: Engine) -> None:
    feed(engine, 3)
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.frame_index == 2
    assert snapshot.wall_time == pytest.approx(WALL + 2 * STEP)


# -- frame rate --------------------------------------------------------------


def test_the_first_frame_reports_no_frame_rate(engine: Engine) -> None:
    feed(engine, 1)
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.fps == 0.0


def test_a_steady_stream_reports_its_true_frame_rate(engine: Engine) -> None:
    feed(engine, 5)
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.fps == pytest.approx(30.0)


def test_a_clock_jump_does_not_reach_the_frame_rate(engine: Engine) -> None:
    feed(engine, 5)
    engine._process(frame_at(5, monotonic=START + 60.0))
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.fps == pytest.approx(30.0)


def test_a_repeated_capture_time_does_not_reach_the_frame_rate(engine: Engine) -> None:
    feed(engine, 5)
    engine._process(frame_at(5, monotonic=START + 4 * STEP))
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.fps == pytest.approx(30.0)


# -- orientation -------------------------------------------------------------


def test_a_level_stream_settles_the_horizon(engine: Engine) -> None:
    feed(engine, 3)
    assert engine.orientation_filter.gravity_locked
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.orientation.roll == pytest.approx(0.0, abs=1e-6)
    assert snapshot.orientation.pitch == pytest.approx(0.0, abs=1e-6)


def test_the_orientation_filter_never_sees_an_implausible_interval(engine: Engine) -> None:
    # Two frames a minute apart: the sample decodes, but integrating a 60s
    # interval would swing the horizon by whatever the gyro happened to read.
    engine._process(frame_at(0))
    engine._process(frame_at(1, monotonic=START + 60.0))
    assert not engine.orientation_filter.gravity_locked
    snapshot = engine.snapshot()
    assert snapshot is not None
    assert snapshot.orientation.as_tuple() == (0.0, 0.0, 0.0)


# -- incidents ---------------------------------------------------------------


def test_an_impact_locks_the_current_segment(engine: Engine) -> None:
    feed(engine, 2, accel_g=(0.0, 0.0, IMPACT_G))
    assert engine.incidents.trigger_count == 1
    assert engine.recorder.stats.incidents_locked == 1
    assert engine.status()["incidents"]["last"] == {"magnitude_g": 0.9, "source": "gsensor"}


def test_a_ringing_impact_only_locks_once(engine: Engine) -> None:
    feed(engine, 6, accel_g=(0.0, 0.0, IMPACT_G))
    assert engine.incidents.trigger_count == 1
    assert engine.recorder.stats.incidents_locked == 1


def test_a_quiet_drive_locks_nothing(engine: Engine) -> None:
    feed(engine, 6)
    assert engine.incidents.trigger_count == 0
    assert engine.status()["incidents"]["last"] is None


def test_locking_by_hand_protects_the_segment(engine: Engine) -> None:
    incident = engine.lock_incident()
    assert incident.source == "manual"
    assert engine.recorder.stats.incidents_locked == 1
    assert engine.status()["incidents"]["last"]["source"] == "manual"


# -- recording ---------------------------------------------------------------


def test_frames_are_not_recorded_before_recording_starts(engine: Engine, writers: list[FakeWriter]) -> None:
    feed(engine, 3)
    assert engine.recorder.stats.written_frames == 0
    assert writers == []


def test_frames_reach_the_recorder_while_it_is_running(recording: Engine, writers: list[FakeWriter]) -> None:
    for index in range(1, 4):
        recording._process(frame_at(index))
    wait_until(lambda: recording.recorder.stats.written_frames == 3)
    assert len(writers) == 1


def test_recording_starts_with_the_frames_real_geometry(engine: Engine, writers: list[FakeWriter]) -> None:
    # A frame one pixel wider and taller than the encoder can accept: H.264 in
    # yuv420p refuses odd dimensions, so a line has to come off each.
    odd = np.full((FRAME_HEIGHT + 1, FRAME_WIDTH + 1, 3), 90, dtype=np.uint8)
    engine._process(frame_at(0, image=odd))
    engine.begin_recording()
    assert engine.recorder.running

    engine._process(frame_at(1, image=odd))
    wait_until(lambda: bool(writers and writers[0].frames))
    height, width = writers[0].frames[0].shape[:2]
    assert (height % 2, width % 2) == (0, 0)
    assert (height, width) == (FRAME_HEIGHT, FRAME_WIDTH - METADATA_WIDTH)


def test_beginning_recording_twice_is_a_no_op(recording: Engine) -> None:
    # The recorder raises if it is started while already running.
    recording.begin_recording()
    assert recording.recorder.running


def test_recording_can_be_switched_off(engine: Engine) -> None:
    engine.config.recording.enabled = False
    feed(engine, 1)
    engine.begin_recording()
    assert not engine.recorder.running


def test_recording_without_frames_is_reported(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    # The real wait is ten seconds long; what is under test is what happens
    # when it comes back empty, not how long it is prepared to wait.
    monkeypatch.setattr(engine, "wait_for_frame", lambda timeout=5.0: None)
    with pytest.raises(CaptureError, match="no frames"):
        engine.begin_recording()


def test_burning_the_timestamp_leaves_the_published_frame_clean(engine: Engine, writers: list[FakeWriter]) -> None:
    engine.config.recording.burn_timestamp = True
    engine._process(frame_at(0))
    engine.begin_recording()

    second = frame_at(1)
    pristine = second.image.copy()
    engine._process(second)
    wait_until(lambda: bool(writers and writers[0].frames))

    snapshot = engine.snapshot()
    assert snapshot is not None
    np.testing.assert_array_equal(snapshot.image, pristine[:, METADATA_WIDTH:])
    # The bar really was drawn -- without this, the assertion above is vacuous.
    assert not np.array_equal(writers[0].frames[0], snapshot.image)


# -- waiting -----------------------------------------------------------------


def test_waiting_for_a_frame_gives_up(engine: Engine) -> None:
    assert engine.wait_for_frame(timeout=0.05) is None


def test_waiting_returns_the_frame_already_in_hand(engine: Engine) -> None:
    feed(engine, 1)
    assert engine.wait_for_frame(timeout=0.05) is engine.snapshot()


# -- preview -----------------------------------------------------------------


def test_there_is_no_preview_before_the_first_frame(engine: Engine) -> None:
    assert engine.preview_frame() is None


def test_a_wide_frame_is_scaled_down_for_preview(engine: Engine) -> None:
    engine.config.server.preview_width = 160
    feed(engine, 1)
    preview = engine.preview_frame(overlay=False)
    assert preview is not None
    assert preview.shape[1] == 160


def test_a_narrow_frame_is_not_scaled_up(engine: Engine) -> None:
    # preview_width defaults to 960; the test frame is far narrower.
    feed(engine, 1)
    preview = engine.preview_frame(overlay=False)
    assert preview is not None
    assert preview.shape[1] == FRAME_WIDTH - METADATA_WIDTH


def test_an_explicit_width_overrides_the_configured_one(engine: Engine) -> None:
    feed(engine, 1)
    preview = engine.preview_frame(overlay=False, width=100)
    assert preview is not None
    assert preview.shape[1] == 100


def test_the_overlay_is_only_drawn_on_request(engine: Engine) -> None:
    feed(engine, 1)
    plain = engine.preview_frame(overlay=False)
    decorated = engine.preview_frame(overlay=True)
    snapshot = engine.snapshot()
    assert plain is not None
    assert decorated is not None
    assert snapshot is not None
    np.testing.assert_array_equal(plain, snapshot.image)
    assert not np.array_equal(decorated, plain)


def test_drawing_the_overlay_leaves_the_published_frame_clean(engine: Engine) -> None:
    feed(engine, 1)
    snapshot = engine.snapshot()
    assert snapshot is not None
    before = snapshot.image.copy()
    assert engine.preview_frame(overlay=True) is not None
    np.testing.assert_array_equal(snapshot.image, before)


# -- panorama ----------------------------------------------------------------


def test_the_panorama_is_wider_than_one_eye(engine: Engine) -> None:
    feed(engine, 1)
    snapshot = engine.snapshot()
    assert snapshot is not None
    panorama = engine.render_panorama(snapshot)
    eye_width = snapshot.image.shape[1] // 2
    assert panorama.shape[1] > eye_width
    assert panorama.shape[1] <= snapshot.image.shape[1]


def test_the_panorama_honours_the_requested_width(engine: Engine) -> None:
    feed(engine, 1)
    snapshot = engine.snapshot()
    assert snapshot is not None
    # Each eye is fitted to half the target and the seam eats the overlap, so
    # the result lands at or below the request -- never above it.
    assert engine.render_panorama(snapshot, 120).shape[1] <= 120


def test_the_panorama_is_a_different_picture_from_the_raw_frame(engine: Engine) -> None:
    feed(engine, 1)
    raw = engine.preview_frame(overlay=False)
    panorama = engine.preview_frame(overlay=False, panorama=True)
    assert raw is not None
    assert panorama is not None
    assert panorama.shape != raw.shape or not np.array_equal(panorama, raw)


def test_rendering_the_panorama_leaves_the_published_frame_clean(engine: Engine) -> None:
    feed(engine, 1)
    snapshot = engine.snapshot()
    assert snapshot is not None
    before = snapshot.image.copy()
    assert engine.preview_frame(overlay=True, panorama=True) is not None
    np.testing.assert_array_equal(snapshot.image, before)


def test_the_recorded_frame_is_never_the_panorama(engine: Engine) -> None:
    """The panorama is a viewing transform; evidence stays as the sensor saw it."""
    feed(engine, 1)
    snapshot = engine.snapshot()
    assert snapshot is not None
    recorded = engine._prepare_for_recording(snapshot.image, snapshot.wall_time)
    assert recorded.shape[1] == snapshot.image.shape[1] - snapshot.image.shape[1] % 2
    assert recorded.shape != engine.render_panorama(snapshot).shape


# -- depth -------------------------------------------------------------------


def test_there_is_no_depth_before_the_first_frame(engine: Engine) -> None:
    assert engine.compute_depth() is None


def test_depth_is_computed_at_the_working_width(engine: Engine) -> None:
    feed(engine, 1)
    depth = engine.compute_depth()
    assert depth is not None
    assert depth.shape[1] == engine.config.depth.working_width
    assert depth.shape[2] == 3


def test_matcher_overrides_are_normalised_before_use(engine: Engine) -> None:
    feed(engine, 1)
    # 20 disparities and a block size of 4 are both illegal for SGBM, which
    # raises rather than rounding. Getting a map back at all proves the engine
    # normalises whatever the UI's sliders hand it.
    assert engine.compute_depth(num_disparities=20, block_size=4, uniqueness_ratio=0) is not None


# -- HUD and status ----------------------------------------------------------


def test_the_hud_reflects_the_recorder(recording: Engine, writers: list[FakeWriter]) -> None:
    recording._process(frame_at(1))
    wait_until(lambda: bool(writers and writers[0].frames))
    status = recording.hud_status()
    assert status.recording
    assert status.clip_name.startswith("VEC_")
    assert status.free_bytes > 0
    assert not status.locked


def test_the_hud_reports_no_space_when_the_volume_cannot_be_read(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "storage_stats", raise_oserror)
    assert engine.hud_status().free_bytes == 0


def test_status_is_json_serialisable(engine: Engine) -> None:
    feed(engine, 3)
    assert json.loads(json.dumps(engine.status()))["frames"] == 3


def test_status_counts_frames_and_decoded_telemetry(engine: Engine) -> None:
    feed(engine, 3)
    status = engine.status()
    assert status["frames"] == 3
    assert status["fps"] == pytest.approx(30.0)
    assert status["error"] == ""
    assert status["camera"]["backend"] == "FAKE"
    telemetry = status["telemetry"]
    assert telemetry["present"] is True
    assert telemetry["decoded_frames"] == 2
    # The first frame has no predecessor to corroborate its clock.
    assert telemetry["failed_frames"] == 1
    assert telemetry["gravity_locked"] is True
    assert telemetry["sample"]["timestamp_us"] == FIRST_TIMESTAMP_US + 2 * FRAME_INTERVAL_US


def test_status_reports_the_running_pipeline(engine: Engine) -> None:
    engine.start()
    wait_until(lambda: engine.snapshot() is not None)
    status = engine.status()
    assert status["running"] is True
    assert status["uptime_seconds"] >= 0.0
    assert status["camera"]["open"] is True


def test_status_reports_a_storage_error_instead_of_raising(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "storage_stats", raise_oserror)
    assert engine.status()["storage"] == {"error": "recording volume went away"}
