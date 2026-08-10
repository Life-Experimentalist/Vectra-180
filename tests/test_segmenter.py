"""Segmented loop recording.

The recorder runs a writer thread behind a bounded queue, so these tests drive
it through its real public surface and wait for the thread to catch up rather
than reaching into it. Encoding is replaced by :class:`FakeWriter`: what is
under test is segmentation, dropping, locking and sidecars -- not libx264.

Frame timestamps are supplied explicitly, which makes segment rollover exact
instead of a race against the wall clock.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vectra180.config import RecordingConfig
from vectra180.errors import RecorderError
from vectra180.recorder import segmenter as segmenter_module
from vectra180.recorder.segmenter import RecorderStats, SegmentRecorder
from vectra180.telemetry import TelemetrySample, level_sample

SIZE = (32, 16)
SEGMENT_SECONDS = 5

#: A wall-clock instant with no sub-second part, so clip names are predictable.
WALL = 1_786_000_000.0


@pytest.fixture
def frame() -> np.ndarray:
    return np.zeros((SIZE[1], SIZE[0], 3), dtype=np.uint8)


class FakeWriter:
    """Stands in for an encoder. Writes a byte per frame so the file is real."""

    #: Set to raise from ``write`` on the given frame index, to simulate an
    #: encoder dying mid-segment.
    fail_on_frame: int | None = None

    def __init__(self, path: Path) -> None:
        self._path = path
        self.frames = 0
        self.closed = False
        self.blocked = threading.Event()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, frame: np.ndarray) -> None:
        if self.fail_on_frame is not None and self.frames == self.fail_on_frame:
            raise RecorderError("encoder died")
        self.frames += 1
        with self._path.open("ab") as handle:
            handle.write(b"\0")

    def close(self) -> None:
        self.closed = True


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
def config(tmp_path: Path) -> RecordingConfig:
    return RecordingConfig(directory=tmp_path, segment_seconds=SEGMENT_SECONDS, min_free_bytes=0)


@pytest.fixture
def recorder(config: RecordingConfig, writers: list[FakeWriter]) -> Iterator[SegmentRecorder]:
    instance = SegmentRecorder(config)
    yield instance
    instance.stop(timeout=5.0)


def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Block until the writer thread has caught up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("recorder did not reach the expected state in time")


def wait_for_frames(recorder: SegmentRecorder, count: int) -> None:
    wait_until(lambda: recorder.stats.written_frames >= count)


def feed(recorder: SegmentRecorder, frame: np.ndarray, offsets: list[float], *, wall: float = WALL) -> None:
    """Submit frames at explicit times, waiting for each to be encoded.

    Waiting keeps the queue from absorbing frames out of order, so rollover
    happens exactly where the offsets say it does.
    """
    for offset in offsets:
        target = recorder.stats.written_frames + 1
        assert recorder.submit(frame, monotonic=offset, wall_time=wall + offset, sample=level_sample())
        wait_for_frames(recorder, target)


# -- lifecycle ---------------------------------------------------------------


def test_a_fresh_recorder_is_not_running(config: RecordingConfig) -> None:
    assert SegmentRecorder(config).running is False


def test_start_creates_the_recording_tree(recorder: SegmentRecorder, config: RecordingConfig) -> None:
    recorder.start(SIZE, 30.0)

    assert config.normal_dir.is_dir()
    assert config.event_dir.is_dir()
    assert recorder.running is True


def test_starting_twice_is_refused(recorder: SegmentRecorder) -> None:
    """Two writer threads on one queue would interleave frames across clips."""
    recorder.start(SIZE, 30.0)

    with pytest.raises(RecorderError, match="already running"):
        recorder.start(SIZE, 30.0)


def test_stopping_an_idle_recorder_is_harmless(recorder: SegmentRecorder) -> None:
    recorder.stop()

    assert recorder.running is False


def test_stop_finalises_the_open_segment(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 0.1])

    recorder.stop(timeout=5.0)

    assert writers[0].closed is True
    assert recorder.running is False
    assert recorder.stats.segments_written == 1


def test_it_works_as_a_context_manager(config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray) -> None:
    with SegmentRecorder(config) as recorder:
        recorder.start(SIZE, 30.0)
        feed(recorder, frame, [0.0])

    assert recorder.running is False
    assert writers[0].closed is True


# -- the producer side -------------------------------------------------------


def test_frames_submitted_before_start_are_refused(recorder: SegmentRecorder, frame: np.ndarray) -> None:
    """No thread is draining the queue, so accepting them would leak memory."""
    assert recorder.submit(frame) is False
    assert recorder.stats.dropped_frames == 0


def test_submit_defaults_to_now(recorder: SegmentRecorder, frame: np.ndarray) -> None:
    recorder.start(SIZE, 30.0)

    assert recorder.submit(frame) is True
    wait_until(lambda: recorder.stats.written_frames == 1)


def test_a_full_queue_drops_frames_instead_of_growing(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dashcam that runs out of memory records nothing at all.

    The alternative -- an unbounded queue -- turns a transient encoder stall
    into an OOM kill, which loses the whole session rather than a few frames.
    """
    stalled = threading.Event()
    entered = threading.Event()

    def stall(self: FakeWriter, _frame: np.ndarray) -> None:
        entered.set()
        stalled.wait(timeout=5.0)
        self.frames += 1

    monkeypatch.setattr(FakeWriter, "write", stall)
    recorder.start(SIZE, 30.0)
    queue_size = recorder._queue.maxsize

    recorder.submit(frame)
    assert entered.wait(timeout=5.0)
    accepted = sum(recorder.submit(frame) for _ in range(queue_size + 20))

    stalled.set()

    assert accepted == queue_size
    assert recorder.stats.dropped_frames == 20


def test_the_queue_depth_follows_the_frame_rate(config: RecordingConfig) -> None:
    """Two seconds of footage may sit unencoded, whatever the camera runs at."""
    recorder = SegmentRecorder(config, queue_seconds=2.0)
    recorder.start(SIZE, 30.0)
    try:
        assert recorder._queue.maxsize == 60
    finally:
        recorder.stop(timeout=5.0)


def test_the_queue_is_never_degenerate(config: RecordingConfig) -> None:
    """A one-slot queue would drop every other frame even when keeping up."""
    recorder = SegmentRecorder(config, queue_seconds=0.0)
    recorder.start(SIZE, 1.0)
    try:
        assert recorder._queue.maxsize == 2
    finally:
        recorder.stop(timeout=5.0)


# -- segmentation ------------------------------------------------------------


def test_frames_within_the_window_share_one_clip(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)

    feed(recorder, frame, [0.0, 1.0, 2.0, 4.9])

    assert len(writers) == 1
    assert writers[0].frames == 4


def test_crossing_the_window_opens_a_new_clip(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """Fixed-length segments bound what a power cut can destroy."""
    recorder.start(SIZE, 30.0)

    feed(recorder, frame, [0.0, 1.0, float(SEGMENT_SECONDS), float(SEGMENT_SECONDS) + 1.0])

    assert len(writers) == 2
    assert [writer.frames for writer in writers] == [2, 2]
    assert writers[0].closed is True
    assert writers[1].closed is False


def test_clip_names_carry_the_start_time(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """Names are the source of truth for ordering, so they must be right."""
    from datetime import UTC, datetime

    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0])

    expected = datetime.fromtimestamp(WALL, tz=UTC).strftime("VEC_%Y%m%d_%H%M%S.mp4")
    assert writers[0].path.name == expected


def test_a_restart_within_the_same_second_does_not_truncate(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """Reopening the same path would destroy the segment just written."""
    recorder.start(SIZE, 30.0)

    # Same wall second for both segments; only the monotonic clock advances.
    feed(recorder, frame, [0.0], wall=WALL)
    feed(recorder, frame, [float(SEGMENT_SECONDS)], wall=WALL - SEGMENT_SECONDS)

    assert writers[0].path != writers[1].path
    assert writers[1].path.name.endswith("_1.mp4")
    assert writers[0].path.exists()


def test_the_container_extension_is_configurable(
    config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    config.container = "mkv"
    recorder = SegmentRecorder(config)
    recorder.start(SIZE, 30.0)
    try:
        feed(recorder, frame, [0.0])
        assert writers[0].path.suffix == ".mkv"
    finally:
        recorder.stop(timeout=5.0)


# -- sidecars ----------------------------------------------------------------


def sidecar_of(writer: FakeWriter) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(writer.path.with_suffix(".json").read_text(encoding="utf-8"))
    return data


def test_a_closed_segment_gets_a_sidecar(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 0.1, 0.2])
    recorder.stop(timeout=5.0)

    data = sidecar_of(writers[0])

    assert data["clip"] == writers[0].path.name
    assert data["frames"] == 3
    assert data["width"] == SIZE[0]
    assert data["height"] == SIZE[1]
    assert data["locked"] is False


def test_the_sidecar_duration_comes_from_the_frame_count(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """Not from the clock: a stalled camera must not claim footage it lacks."""
    recorder.start(SIZE, 10.0)
    feed(recorder, frame, [0.0, 1.0, 2.0, 3.0, 4.0])
    recorder.stop(timeout=5.0)

    assert sidecar_of(writers[0])["duration_seconds"] == 0.5


def test_the_sidecar_separates_playback_length_from_road_time(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """Five frames spanning four seconds play for half a second at 10fps.

    A reviewer reading an incident clip has to be able to tell that the
    footage is a sample of the period rather than a continuous record of it.
    """
    recorder.start(SIZE, 10.0)
    feed(recorder, frame, [0.0, 1.0, 2.0, 3.0, 4.0])
    recorder.stop(timeout=5.0)

    data = sidecar_of(writers[0])

    assert data["duration_seconds"] == 0.5
    assert data["covers_seconds"] == 4.0


def test_a_segment_that_lost_nothing_is_marked_continuous(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 1 / 30, 2 / 30])
    recorder.stop(timeout=5.0)

    data = sidecar_of(writers[0])

    assert data["dropped_frames"] == 0
    assert data["continuous"] is True


def test_a_segment_that_dropped_frames_says_so(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """The count is per segment, not the lifetime total of the process."""
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 1 / 30, 2 / 30])
    recorder.stats.dropped_frames += 7
    recorder.stop(timeout=5.0)

    data = sidecar_of(writers[0])

    assert data["dropped_frames"] == 7
    assert data["continuous"] is False


def test_a_camera_that_under_delivers_is_not_continuous(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """Three frames spanning a second, written as 30fps, play for a tenth of it.

    Nothing was dropped -- every frame the camera produced was written -- and
    the clip still runs ten times too fast. A reviewer who trusts `continuous`
    over a bare frame count has to be told that.
    """
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 0.5, 1.0])
    recorder.stop(timeout=5.0)

    data = sidecar_of(writers[0])

    assert data["dropped_frames"] == 0
    assert data["continuous"] is False


def test_telemetry_is_recorded_against_the_segment_start(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 1.5, 3.0])
    recorder.stop(timeout=5.0)

    samples = sidecar_of(writers[0])["telemetry"]

    assert isinstance(samples, list)
    assert [entry["offset_seconds"] for entry in samples] == [0.0, 1.5, 3.0]


def test_frames_without_telemetry_leave_no_samples(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    recorder.submit(frame, monotonic=0.0, wall_time=WALL, sample=None)
    wait_until(lambda: recorder.stats.written_frames == 1)
    recorder.stop(timeout=5.0)

    assert sidecar_of(writers[0])["telemetry"] == []


def test_the_sidecar_can_be_turned_off(config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray) -> None:
    """Hours of IMU samples per clip is real space on a small card."""
    config.write_telemetry_sidecar = False
    recorder = SegmentRecorder(config)
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0])
    recorder.stop(timeout=5.0)

    assert not writers[0].path.with_suffix(".json").exists()


def test_a_sidecar_that_cannot_be_written_does_not_lose_the_clip(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "write_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only")))

    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0])
    recorder.stop(timeout=5.0)

    assert writers[0].path.exists()
    assert recorder.stats.segments_written == 1


# -- incident locking --------------------------------------------------------


def test_locking_moves_the_open_segment_once_it_closes(
    recorder: SegmentRecorder, config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 1.0])

    recorder.lock_current("gsensor")
    recorder.stop(timeout=5.0)

    assert (config.event_dir / writers[0].path.name).exists()
    assert not (config.normal_dir / writers[0].path.name).exists()
    assert recorder.stats.incidents_locked == 1


def test_a_locked_segment_records_why(
    recorder: SegmentRecorder, config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0])

    recorder.lock_current("manual")
    recorder.stop(timeout=5.0)

    data = json.loads((config.event_dir / writers[0].path.with_suffix(".json").name).read_text(encoding="utf-8"))
    assert data["locked"] is True
    assert data["lock_reasons"] == ["manual"]


def test_locking_also_protects_the_preceding_segment(
    recorder: SegmentRecorder, config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """An impact early in a segment leaves the run-up in the file just closed."""
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 1.0, float(SEGMENT_SECONDS)])
    wait_until(lambda: recorder.stats.segments_written == 1)

    recorder.lock_current()
    recorder.stop(timeout=5.0)

    assert (config.event_dir / writers[0].path.name).exists()
    assert (config.event_dir / writers[1].path.name).exists()


def test_the_preceding_segment_can_be_left_alone(
    config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder = SegmentRecorder(config, lock_previous=False)
    recorder.start(SIZE, 30.0)
    try:
        feed(recorder, frame, [0.0, float(SEGMENT_SECONDS)])
        wait_until(lambda: recorder.stats.segments_written == 1)
        recorder.lock_current()
    finally:
        recorder.stop(timeout=5.0)

    assert (config.normal_dir / writers[0].path.name).exists()
    assert (config.event_dir / writers[1].path.name).exists()


def test_the_same_segment_is_not_protected_twice(
    recorder: SegmentRecorder, config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """A second jolt during the same segment must not fail on the moved file."""
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, float(SEGMENT_SECONDS)])
    wait_until(lambda: recorder.stats.segments_written == 1)

    recorder.lock_current()
    recorder.lock_current()
    recorder.stop(timeout=5.0)

    assert recorder.stats.incidents_locked == 2
    assert len(list(config.event_dir.glob("*.mp4"))) == 2


def test_locking_with_nothing_recorded_is_harmless(recorder: SegmentRecorder) -> None:
    """The web UI's lock button is reachable before the first frame lands."""
    recorder.start(SIZE, 30.0)

    recorder.lock_current("manual")

    assert recorder.stats.incidents_locked == 1


def test_a_locked_clip_survives_retention(
    recorder: SegmentRecorder, config: RecordingConfig, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """The budget is one byte; only the events directory saves the clip."""
    config.max_bytes = 1
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 1.0])

    recorder.lock_current()
    recorder.stop(timeout=5.0)

    assert (config.event_dir / writers[0].path.name).exists()


# -- failure handling --------------------------------------------------------


def test_a_dying_encoder_does_not_end_the_session(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad segment is a lost minute; a dead thread is a lost drive."""
    monkeypatch.setattr(FakeWriter, "fail_on_frame", 0)

    recorder.start(SIZE, 30.0)
    recorder.submit(frame, monotonic=0.0, wall_time=WALL)
    wait_until(lambda: bool(recorder.stats.last_error))

    monkeypatch.setattr(FakeWriter, "fail_on_frame", None)
    feed(recorder, frame, [1.0])

    assert recorder.stats.last_error == "encoder died"
    assert recorder.running is True
    assert recorder.stats.written_frames == 1


def test_a_discarded_segment_leaves_no_file(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty clip occupies a retention slot and plays as broken footage."""
    monkeypatch.setattr(FakeWriter, "fail_on_frame", 0)

    recorder.start(SIZE, 30.0)
    recorder.submit(frame, monotonic=0.0, wall_time=WALL)
    # The writer thread records the error before it discards the partial file,
    # so waiting on last_error would race the very unlink being asserted on.
    wait_until(lambda: bool(writers) and not writers[0].path.exists())

    assert recorder.stats.last_error == "encoder died"
    assert recorder.stats.segments_written == 0


def test_a_protect_that_fails_leaves_the_clip_where_it_is(
    recorder: SegmentRecorder,
    config: RecordingConfig,
    writers: list[FakeWriter],
    frame: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full events directory must not also cost the footage in hand."""
    monkeypatch.setattr(
        segmenter_module.storage, "protect_clip", lambda *_a, **_k: (_ for _ in ()).throw(OSError("card full"))
    )

    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0])
    recorder.lock_current()
    recorder.stop(timeout=5.0)

    assert (config.normal_dir / writers[0].path.name).exists()
    assert recorder.stats.segments_written == 1


def test_a_preceding_clip_that_cannot_be_protected_is_only_logged(
    recorder: SegmentRecorder,
    config: RecordingConfig,
    writers: list[FakeWriter],
    frame: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The current segment is the one being locked; the run-up is a bonus."""
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, float(SEGMENT_SECONDS)])
    wait_until(lambda: recorder.stats.segments_written == 1)

    monkeypatch.setattr(
        segmenter_module.storage, "protect_clip", lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only"))
    )
    with caplog.at_level("WARNING", logger="vectra180.recorder.segmenter"):
        recorder.lock_current()

    assert recorder.stats.incidents_locked == 1
    assert "could not protect preceding clip" in caplog.text
    assert (config.normal_dir / writers[0].path.name).exists()


def test_a_segment_closing_mid_lock_does_not_clear_the_wrong_clip(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer thread keeps running while ``lock_current`` moves a file.

    If a segment closes in that window, ``_previous_path`` now names a clip
    that was never protected -- clearing it would lose the next lock's run-up.
    """
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, float(SEGMENT_SECONDS)])
    wait_until(lambda: recorder.stats.segments_written == 1)

    original = segmenter_module.storage.protect_clip
    replacement = Path("VEC_20260809_999999.mp4")

    def protect_then_roll_over(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        recorder._previous_path = replacement
        return result

    monkeypatch.setattr(segmenter_module.storage, "protect_clip", protect_then_roll_over)
    recorder.lock_current()

    assert recorder._previous_path == replacement


def test_a_writer_thread_that_will_not_stop_is_reported(
    recorder: SegmentRecorder, frame: np.ndarray, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Waiting forever on a wedged encoder would hang the whole shutdown."""
    stalled = threading.Event()
    entered = threading.Event()

    def stall(self: FakeWriter, _frame: np.ndarray) -> None:
        entered.set()
        stalled.wait(timeout=10.0)
        self.frames += 1

    monkeypatch.setattr(FakeWriter, "write", stall)
    recorder.start(SIZE, 30.0)
    recorder.submit(frame, monotonic=0.0, wall_time=WALL)
    assert entered.wait(timeout=5.0)

    try:
        with caplog.at_level("ERROR", logger="vectra180.recorder.segmenter"):
            recorder.stop(timeout=0.1)
        assert "did not stop within" in caplog.text
    finally:
        stalled.set()


def test_a_failing_retention_pass_does_not_lose_the_clip(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        segmenter_module.storage, "prune", lambda *_a, **_k: (_ for _ in ()).throw(OSError("card removed"))
    )

    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0])
    recorder.stop(timeout=5.0)

    assert writers[0].path.exists()
    assert recorder.stats.segments_written == 1


# -- reported state ----------------------------------------------------------


def test_stats_track_the_open_segment(recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0, 2.5])

    assert recorder.stats.current_clip == writers[0].path.name
    assert recorder.stats.segment_elapsed == pytest.approx(2.5)
    assert recorder.stats.encoder == "FakeWriter"


def test_stats_clear_when_the_segment_closes(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    recorder.start(SIZE, 30.0)
    feed(recorder, frame, [0.0])
    recorder.stop(timeout=5.0)

    assert recorder.stats.current_clip == ""
    assert recorder.stats.segment_elapsed == 0.0


def test_stats_are_json_safe() -> None:
    stats = RecorderStats(written_frames=10, segment_elapsed=1.23456, encoder="FFmpegWriter")

    data = json.loads(json.dumps(stats.as_dict()))

    assert data["segment_elapsed"] == 1.23
    assert data["encoder"] == "FFmpegWriter"


def test_a_sample_reaches_the_sidecar_intact(
    recorder: SegmentRecorder, writers: list[FakeWriter], frame: np.ndarray
) -> None:
    """The sidecar is the only record of what the IMU saw during a clip."""
    sample = TelemetrySample(timestamp_us=42, accel_x=0.1, accel_y=0.2, accel_z=9.8, gyro_x=1.0, gyro_y=2.0, gyro_z=3.0)
    recorder.start(SIZE, 30.0)
    recorder.submit(frame, monotonic=0.0, wall_time=WALL, sample=sample)
    wait_until(lambda: recorder.stats.written_frames == 1)
    recorder.stop(timeout=5.0)

    entry = sidecar_of(writers[0])["telemetry"][0]  # type: ignore[index]

    assert entry["timestamp_us"] == 42
    assert entry["offset_seconds"] == 0.0
