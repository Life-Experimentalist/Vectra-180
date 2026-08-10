"""Segmented loop recording.

Encoding runs on its own thread behind a bounded queue. The capture loop hands
frames over and returns immediately, so a slow encode -- and on a CM5 encoding
is always the slowest step -- stalls the queue instead of the camera. When the
queue fills, frames are dropped and counted rather than buffered without limit;
a dashcam that runs out of memory records nothing at all.

Turning a captured frame into the pixels that go in the file -- the downscale,
the burned clock -- happens on this thread too, through the ``prepare`` callable
given to :meth:`SegmentRecorder.start`. It belongs here rather than in the
caller because a UVC driver does not buffer: it hands over the frame that is
ready when asked and the next one is not ready until the following interval, so
every millisecond the capture loop spends on anything else risks missing a whole
frame. Measured on a 4000x1200 module, doing this work in the capture loop cost
a third of the frame rate.

Footage is split into fixed-length segments. That bounds what a power cut can
destroy to the segment in flight, and gives retention something to delete that
is never the file being written.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from vectra180.config import RecordingConfig
from vectra180.errors import RecorderError
from vectra180.recorder import storage
from vectra180.recorder.writer import FrameWriter, create_writer
from vectra180.telemetry import TelemetrySample

__all__ = ["RecorderStats", "SegmentRecorder"]

log = logging.getLogger(__name__)

#: Sentinel pushed onto the queue to end the writer thread cleanly.
_STOP = object()

#: How far a clip's playing time may fall short of the stretch of road it
#: covers before it stops being a real-time record. A little slack is
#: unavoidable: the segment's clock starts before its first frame arrives.
_CONTINUITY_TOLERANCE = 1.05

#: Ceiling on the frames waiting to be encoded, in bytes. The queue is sized in
#: seconds of footage, but seconds are not a fixed amount of memory: a 4000x1200
#: frame is 14 MB, so two seconds of them is most of a gigabyte and a 4 GB CM5
#: would be killed by the kernel long before the encoder recovered. Dropping
#: frames under that pressure is the whole point of a bounded queue.
_MAX_QUEUE_BYTES = 256 * 1024 * 1024


@dataclass
class _QueuedFrame:
    image: np.ndarray
    monotonic: float
    wall_time: float
    sample: TelemetrySample | None


@dataclass
class RecorderStats:
    """Counters exposed by the status endpoint and the HUD."""

    written_frames: int = 0
    dropped_frames: int = 0
    segments_written: int = 0
    incidents_locked: int = 0
    current_clip: str = ""
    segment_elapsed: float = 0.0
    encoder: str = ""
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "written_frames": self.written_frames,
            "dropped_frames": self.dropped_frames,
            "segments_written": self.segments_written,
            "incidents_locked": self.incidents_locked,
            "current_clip": self.current_clip,
            "segment_elapsed": round(self.segment_elapsed, 2),
            "encoder": self.encoder,
            "last_error": self.last_error,
        }


@dataclass
class _Segment:
    """State for the segment currently being encoded."""

    writer: FrameWriter
    started_monotonic: float
    started_wall: float
    #: Monotonic stamp of the most recent frame accepted into this segment.
    #: With the start stamp it gives the span of road the clip covers, which
    #: is longer than the clip plays for whenever frames were dropped.
    last_monotonic: float = 0.0
    #: Value of the global dropped counter when this segment opened.
    dropped_at_start: int = 0
    frames: int = 0
    protect: bool = False
    lock_reasons: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)


class SegmentRecorder:
    """Writes a continuous stream of frames as fixed-length clips."""

    def __init__(self, config: RecordingConfig, *, queue_seconds: float = 2.0, lock_previous: bool = True) -> None:
        """
        Args:
            queue_seconds: How much footage may sit unencoded. This is the
                latency/loss trade: a deeper queue rides out longer encoder
                stalls but delays how quickly a dropped frame is noticed.
            lock_previous: Whether :meth:`lock_current` also protects the
                segment that closed just before the trigger.
        """
        self._config = config
        self._queue_seconds = queue_seconds
        self._lock_previous = lock_previous
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._prepare: Callable[[np.ndarray, float], np.ndarray] | None = None
        #: Bytes currently sitting in the queue, and its own lock: the frame
        #: count alone does not bound memory when frame sizes differ by 3x.
        self._queued_bytes = 0
        self._bytes_lock = threading.Lock()
        self._segment: _Segment | None = None
        self._previous_path: Path | None = None
        self._size: tuple[int, int] = (0, 0)
        self._fps: float = 30.0
        self._running = False
        self.stats = RecorderStats()

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        size: tuple[int, int],
        fps: float,
        *,
        prepare: Callable[[np.ndarray, float], np.ndarray] | None = None,
    ) -> None:
        """Begin recording frames of the given size and rate.

        Args:
            size: Geometry of the frames the writer will be opened with. When
                ``prepare`` is given this is the size it *returns*, not the
                size submitted.
            fps: Frame rate declared to the encoder.
            prepare: Applied to each frame on the recorder thread, as
                ``prepare(image, wall_time)``, to produce the pixels written to
                the file. Keeping it here rather than at the call site is what
                stops the per-frame pixel work from stealing capture time.

        Raises:
            RecorderError: if already running.
        """
        if self._running:
            raise RecorderError("recorder is already running")

        storage.ensure_directories(self._config)
        self._size = size
        self._fps = max(1.0, fps)
        self._prepare = prepare
        self._queue = queue.Queue(maxsize=max(2, int(self._fps * self._queue_seconds)))
        self._queued_bytes = 0
        self._running = True
        self._thread = threading.Thread(target=self._run, name="vectra-recorder", daemon=True)
        self._thread.start()
        log.info("recording %dx%d @ %.1ffps into %s", size[0], size[1], self._fps, self._config.directory)

    def stop(self, timeout: float = 30.0) -> None:
        """Flush the queue, finalise the open segment and join the thread."""
        if not self._running:
            return
        self._running = False
        self._queue.put(_STOP)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.error("recorder thread did not stop within %.0fs", timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._running

    def __enter__(self) -> SegmentRecorder:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- producer side -----------------------------------------------------

    def submit(
        self,
        image: np.ndarray,
        *,
        monotonic: float | None = None,
        wall_time: float | None = None,
        sample: TelemetrySample | None = None,
    ) -> bool:
        """Queue a frame for encoding.

        The frame is queued as captured; any downscale or overlay happens on
        the recorder thread through the ``prepare`` callable, so this returns
        without touching a pixel.

        Returns:
            ``True`` if queued, ``False`` if the queue was full and the frame
            was dropped. Never blocks.
        """
        if not self._running:
            return False
        item = _QueuedFrame(
            image=image,
            monotonic=time.monotonic() if monotonic is None else monotonic,
            wall_time=time.time() if wall_time is None else wall_time,
            sample=sample,
        )
        nbytes = image.nbytes
        with self._bytes_lock:
            # An empty queue always accepts, even for a frame bigger than the
            # whole budget: refusing it would drop every frame forever rather
            # than record a large one slowly.
            if self._queued_bytes and self._queued_bytes + nbytes > _MAX_QUEUE_BYTES:
                self.stats.dropped_frames += 1
                return False
            self._queued_bytes += nbytes
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._bytes_lock:
                self._queued_bytes -= nbytes
            self.stats.dropped_frames += 1
            return False
        return True

    def lock_current(self, reason: str = "gsensor") -> None:
        """Protect the segment being written, and optionally the one before.

        The previous segment matters as much as the current one: an impact at
        the start of a segment leaves the run-up -- the part that shows what
        happened -- in the file that just closed.
        """
        with self._lock:
            if self._segment is not None:
                self._segment.protect = True
                self._segment.lock_reasons.append(reason)
            previous = self._previous_path
        self.stats.incidents_locked += 1

        if self._lock_previous and previous is not None and previous.exists():
            try:
                storage.protect_clip(self._config, previous.name)
                with self._lock:
                    if self._previous_path == previous:
                        self._previous_path = None
            except (FileNotFoundError, OSError) as exc:
                log.warning("could not protect preceding clip %s: %s", previous.name, exc)

    # -- consumer side -----------------------------------------------------

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                with self._bytes_lock:
                    self._queued_bytes -= item.image.nbytes
                try:
                    self._write(item)
                except RecorderError as exc:
                    # One bad segment must not end the recording session: drop
                    # it, record why, and open a fresh one on the next frame.
                    self.stats.last_error = str(exc)
                    log.error("segment failed: %s", exc)
                    self._close_segment(discard=True)
        finally:
            self._close_segment()
            self._running = False

    def _write(self, item: _QueuedFrame) -> None:
        segment = self._segment
        if segment is not None and item.monotonic - segment.started_monotonic >= self._config.segment_seconds:
            self._close_segment()
            segment = None

        if segment is None:
            segment = self._open_segment(item)

        image = item.image if self._prepare is None else self._prepare(item.image, item.wall_time)
        segment.writer.write(image)
        segment.frames += 1
        segment.last_monotonic = item.monotonic
        self.stats.written_frames += 1
        self.stats.segment_elapsed = item.monotonic - segment.started_monotonic

        if item.sample is not None and self._config.write_telemetry_sidecar:
            record = item.sample.as_dict()
            record["offset_seconds"] = round(item.monotonic - segment.started_monotonic, 4)
            segment.samples.append(record)

    def _open_segment(self, item: _QueuedFrame) -> _Segment:
        stamp = datetime.fromtimestamp(item.wall_time, tz=UTC).strftime("%Y%m%d_%H%M%S")
        path = self._config.normal_dir / f"VEC_{stamp}.{self._config.container}"
        # A restart within the same second would otherwise reopen and truncate
        # the segment just written.
        suffix = 1
        while path.exists():
            path = self._config.normal_dir / f"VEC_{stamp}_{suffix}.{self._config.container}"
            suffix += 1

        writer = create_writer(path, self._size, self._fps, self._config)
        segment = _Segment(
            writer=writer,
            started_monotonic=item.monotonic,
            started_wall=item.wall_time,
            last_monotonic=item.monotonic,
            dropped_at_start=self.stats.dropped_frames,
        )
        with self._lock:
            self._segment = segment
        self.stats.current_clip = path.name
        self.stats.encoder = type(writer).__name__
        return segment

    def _close_segment(self, *, discard: bool = False) -> None:
        with self._lock:
            segment = self._segment
            self._segment = None
        if segment is None:
            return

        path = segment.writer.path
        segment.writer.close()
        self.stats.current_clip = ""
        self.stats.segment_elapsed = 0.0

        if discard or segment.frames == 0:
            # An empty file is worse than none: it occupies a retention slot
            # and plays as a broken clip.
            path.unlink(missing_ok=True)
            return

        duration = segment.frames / self._fps
        self._write_sidecar(path, segment, duration)
        self.stats.segments_written += 1

        if segment.protect:
            try:
                storage.protect_clip(self._config, path.name)
            except (FileNotFoundError, OSError) as exc:
                log.warning("could not protect %s: %s", path.name, exc)
            else:
                self._previous_path = None
                return

        with self._lock:
            self._previous_path = path

        try:
            storage.prune(self._config, keep=[path])
        except OSError as exc:
            log.warning("retention pass failed: %s", exc)

    def _write_sidecar(self, path: Path, segment: _Segment, duration: float) -> None:
        if not self._config.write_telemetry_sidecar:
            return
        # How long the clip plays for, and how long a stretch of road it
        # actually covers. They are the same number only when nothing was
        # dropped; when they diverge the footage is not continuous, and an
        # incident record has to say so rather than let a reviewer assume it.
        covers = max(duration, segment.last_monotonic - segment.started_monotonic)
        dropped = max(0, self.stats.dropped_frames - segment.dropped_at_start)
        # Dropping frames is not the only way to lose time. A camera that
        # advertises 30fps and delivers 17 costs no drops at all -- every frame
        # it produced was written -- and still yields a clip that plays half
        # again too fast. Both faults land on the same pair of numbers, so both
        # are answered here.
        real_time = covers <= duration * _CONTINUITY_TOLERANCE

        payload = {
            "clip": path.name,
            "started_at": datetime.fromtimestamp(segment.started_wall, tz=UTC).isoformat(),
            "duration_seconds": round(duration, 3),
            "covers_seconds": round(covers, 3),
            "dropped_frames": dropped,
            "continuous": dropped == 0 and real_time,
            "frames": segment.frames,
            "fps": round(self._fps, 3),
            "width": self._size[0],
            "height": self._size[1],
            "locked": segment.protect,
            "lock_reasons": segment.lock_reasons,
            "telemetry": segment.samples,
        }
        try:
            path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write sidecar for %s: %s", path.name, exc)
