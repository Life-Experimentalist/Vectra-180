"""The always-on capture pipeline.

One thread owns the camera. Its loop is deliberately short -- read, decode the
telemetry strip, hand the frame to the recorder, publish it for preview -- so
that nothing optional can delay the next :meth:`~CameraSource.read`.

Everything expensive is pulled *out* of this loop:

* Encoding happens on the recorder's own thread behind a bounded queue.
* Preview JPEGs are encoded by the HTTP handler, only while a client is
  watching -- and so is the panoramic dewarp, when one asks for it.
* Depth is computed on request, never per frame.

That ordering is what "recording-first" means here: a viewer, a depth request
or a slow SD card can cost you a preview frame or a disparity map, but it
cannot cost you recorded footage.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from vectra180.capture import CameraSource, Frame
from vectra180.config import EngineConfig
from vectra180.errors import CaptureError
from vectra180.imaging import (
    FisheyeDewarper,
    HorizonStabilizer,
    HUDRenderer,
    HUDStatus,
    PanoramaStitcher,
    StereoDepthEngine,
    crop_to_even,
    split_stereo,
    strip_metadata,
)
from vectra180.recorder import Incident, IncidentDetector, SegmentRecorder, storage_stats
from vectra180.telemetry import Orientation, OrientationFilter, TelemetryDecoder, TelemetrySample

__all__ = ["Engine", "EngineSnapshot"]

log = logging.getLogger(__name__)

#: Weight of the running frame-rate average. High enough that the reported
#: rate is steady, low enough that a real stall shows within a second.
_FPS_SMOOTHING = 0.9

#: A dt outside this range means the clock jumped or the loop stalled; the
#: orientation filter is told to skip rather than integrate a bogus interval.
_MIN_DT = 1e-4
_MAX_DT = 1.0


@dataclass(frozen=True)
class EngineSnapshot:
    """A consistent view of the pipeline at one instant."""

    image: np.ndarray
    sample: TelemetrySample | None
    orientation: Orientation
    fps: float
    frame_index: int
    wall_time: float


class Engine:
    """Owns the camera thread and everything hanging off it."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.source = CameraSource(config.camera)
        self.decoder = TelemetryDecoder()
        self.orientation_filter = OrientationFilter(config.telemetry)
        self.incidents = IncidentDetector(config.incident)
        self.recorder = SegmentRecorder(config.recording, lock_previous=config.incident.lock_previous_segment)
        self.depth = StereoDepthEngine(config.depth)
        self.dewarper = FisheyeDewarper(config.depth.focal_scale)
        self.stitcher = PanoramaStitcher()

        self._lock = threading.Lock()
        self._snapshot: EngineSnapshot | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at = 0.0
        self._fps = 0.0
        self._last_monotonic: float | None = None
        self._last_incident: Incident | None = None
        self._error: str = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open the camera and start the capture thread.

        Raises:
            CaptureError: if the camera cannot be opened. Failing here rather
                than in the background gives the CLI something to report.
        """
        if self._thread is not None:
            return
        self.source.open()
        self._started_at = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vectra-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Stop capturing, flush the recorder and release the camera."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.recorder.stop()
        self.source.close()

    def __enter__(self) -> Engine:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- capture loop ------------------------------------------------------

    def _run(self) -> None:
        try:
            for frame in self.source.frames():
                if self._stop.is_set():
                    break
                self._process(frame)
        except CaptureError as exc:
            self._error = str(exc)
            log.error("capture loop ended: %s", exc)
        finally:
            self.recorder.stop()

    def _process(self, frame: Frame) -> None:
        image, strip = strip_metadata(frame.image, self.config.telemetry.metadata_width)

        sample = None
        if self.config.telemetry.enabled and strip is not None:
            sample = self.decoder.decode_strip(strip)

        dt = 0.0 if self._last_monotonic is None else frame.monotonic - self._last_monotonic
        self._last_monotonic = frame.monotonic
        if _MIN_DT <= dt <= _MAX_DT:
            instant = 1.0 / dt
            self._fps = _FPS_SMOOTHING * self._fps + (1.0 - _FPS_SMOOTHING) * instant if self._fps else instant
            orientation = self.orientation_filter.update(sample, dt)
        else:
            orientation = self.orientation_filter.orientation

        incident = self.incidents.update(sample, frame.monotonic)
        if incident is not None:
            self._last_incident = incident
            self.recorder.lock_current(incident.source)

        if self.recorder.running:
            self.recorder.submit(
                self._prepare_for_recording(image, frame.wall_time),
                monotonic=frame.monotonic,
                wall_time=frame.wall_time,
                sample=sample,
            )

        with self._lock:
            self._snapshot = EngineSnapshot(
                image=image,
                sample=sample,
                orientation=orientation,
                fps=self._fps,
                frame_index=frame.index,
                wall_time=frame.wall_time,
            )

    def _prepare_for_recording(self, image: np.ndarray, wall_time: float) -> np.ndarray:
        """Produce the exact pixels that go into the file.

        The timestamp is drawn on a copy: the snapshot published for preview
        and depth must stay clean, or the burned text would end up inside a
        disparity computation.
        """
        recorded = crop_to_even(image)
        if not self.config.recording.burn_timestamp:
            return recorded
        stamped = recorded.copy()
        label = datetime.fromtimestamp(wall_time).strftime("%Y-%m-%d %H:%M:%S")
        return HUDRenderer.draw_timestamp_bar(stamped, label)

    def begin_recording(self) -> None:
        """Start the recorder once the real frame geometry is known.

        Deferred until a frame has arrived because the driver may hand back a
        different resolution than the one requested, and the encoder must be
        opened with the size it will actually receive.
        """
        if self.recorder.running or not self.config.recording.enabled:
            return
        snapshot = self.wait_for_frame(timeout=10.0)
        if snapshot is None:
            raise CaptureError("no frames arrived within 10s; cannot start recording")
        prepared = self._prepare_for_recording(snapshot.image, snapshot.wall_time)
        height, width = prepared.shape[:2]
        self.recorder.start((width, height), self.source.fps)

    # -- consumers ---------------------------------------------------------

    def snapshot(self) -> EngineSnapshot | None:
        """Latest processed frame, or ``None`` before the first arrives."""
        with self._lock:
            return self._snapshot

    def wait_for_frame(self, timeout: float = 5.0) -> EngineSnapshot | None:
        """Block until a frame is available or ``timeout`` elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot is not None:
                return snapshot
            time.sleep(0.02)
        return self.snapshot()

    def preview_frame(
        self, *, overlay: bool = True, width: int | None = None, panorama: bool = False
    ) -> np.ndarray | None:
        """Render a frame for display, with the HUD burned in.

        Args:
            overlay: Draw the telemetry HUD over the result.
            width: Target width. Defaults to ``server.preview_width``.
            panorama: Dewarp both eyes, join them into one wide view and level
                the horizon, instead of returning the raw side-by-side frame.

        Returns ``None`` before the first frame. This is called from HTTP
        handler threads, never from the capture loop.
        """
        snapshot = self.snapshot()
        if snapshot is None:
            return None

        target = width or self.config.server.preview_width
        if panorama:
            image = self.render_panorama(snapshot, target)
        else:
            image = self._fit_width(snapshot.image, target)
            if image is snapshot.image:
                # The overlay draws in place, and the snapshot is shared with
                # the depth path and every other viewer.
                image = image.copy()

        if overlay:
            HUDRenderer.draw_telemetry_overlay(
                image, snapshot.sample, snapshot.orientation, snapshot.fps, self.hud_status()
            )
        return image

    @staticmethod
    def _fit_width(image: np.ndarray, target: int) -> np.ndarray:
        """Shrink to ``target`` pixels wide. Never enlarges, never copies."""
        import cv2  # local import: only the preview path needs it here

        if not target or image.shape[1] <= target:
            return image
        scale = target / image.shape[1]
        return cv2.resize(image, (target, max(1, int(image.shape[0] * scale))), interpolation=cv2.INTER_AREA)

    def render_panorama(self, snapshot: EngineSnapshot, width: int = 0) -> np.ndarray:
        """Dewarp both eyes, join them, and level the horizon.

        This is a *viewing* transform. The recorder writes the raw side-by-side
        frame, because that is the closest thing to what the sensors saw and the
        only version an incident is worth arguing over.

        Each eye is shrunk *before* it is dewarped: the remap dominates the cost
        of this path, and doing it at viewing size instead of sensor size is the
        difference between a viewer being free and a viewer being felt.

        Args:
            snapshot: The frame to render, from :meth:`snapshot`.
            width: Target width for the finished panorama. Zero keeps the
                sensor's own resolution.
        """
        left, right = split_stereo(snapshot.image)
        eye_width = max(1, width // 2) if width else 0
        left = self.dewarper.dewarp(self._fit_width(left, eye_width))
        right = self.dewarper.dewarp(self._fit_width(right, eye_width))
        return HorizonStabilizer.stabilize(self.stitcher.stitch(left, right), snapshot.orientation.roll)

    def hud_status(self) -> HUDStatus:
        stats = self.recorder.stats
        return HUDStatus(
            recording=self.recorder.running,
            clip_name=stats.current_clip,
            segment_elapsed=stats.segment_elapsed,
            free_bytes=self._free_bytes(),
            locked=self._last_incident is not None,
            dropped_frames=stats.dropped_frames,
        )

    def _free_bytes(self) -> int:
        try:
            return storage_stats(self.config.recording).free_bytes
        except OSError:
            return 0

    def compute_depth(
        self,
        *,
        num_disparities: int | None = None,
        block_size: int | None = None,
        uniqueness_ratio: int | None = None,
    ) -> np.ndarray | None:
        """Run stereo matching on the current frame and return a colour map.

        Expensive by design and called only on request -- see the module
        docstring. The keyword arguments override the configured matcher
        parameters for this one call, which is how the desktop UI's sliders
        take effect without mutating shared state. Returns ``None`` before the
        first frame.
        """
        snapshot = self.snapshot()
        if snapshot is None:
            return None
        left, right = split_stereo(snapshot.image)
        left = self.dewarper.dewarp(self.depth.downscale(left))
        right = self.dewarper.dewarp(self.depth.downscale(right))
        return self.depth.compute(
            left,
            right,
            num_disparities=num_disparities,
            block_size=block_size,
            uniqueness_ratio=uniqueness_ratio,
        ).colorized

    def lock_incident(self) -> Incident:
        """Manually protect the current segment."""
        incident = self.incidents.trigger_manual(time.monotonic())
        self._last_incident = incident
        self.recorder.lock_current(incident.source)
        return incident

    def status(self) -> dict[str, Any]:
        """Everything the status endpoint reports."""
        snapshot = self.snapshot()
        try:
            storage = storage_stats(self.config.recording).as_dict()
        except OSError as exc:
            storage = {"error": str(exc)}

        return {
            "running": self.running,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1) if self._started_at else 0.0,
            "error": self._error,
            "camera": self.source.describe(),
            "fps": round(snapshot.fps, 2) if snapshot else 0.0,
            "frames": snapshot.frame_index + 1 if snapshot else 0,
            "telemetry": {
                "enabled": self.config.telemetry.enabled,
                "present": self.decoder.has_telemetry,
                "decoded_frames": self.decoder.decoded_frames,
                "failed_frames": self.decoder.failed_frames,
                "sample": snapshot.sample.as_dict() if snapshot and snapshot.sample else None,
                "orientation": snapshot.orientation.as_dict() if snapshot else None,
                "gravity_locked": self.orientation_filter.gravity_locked,
            },
            "recorder": self.recorder.stats.as_dict(),
            "incidents": {
                "count": self.incidents.trigger_count,
                "peak_g": round(self.incidents.peak_magnitude_g, 3),
                "last": self._last_incident.as_dict() if self._last_incident else None,
            },
            "storage": storage,
        }
