"""Frame acquisition from a dual-fisheye UVC device.

A dashcam runs unattended in a vehicle, where USB devices brown out over
potholes and re-enumerate a second later. :class:`CameraSource` therefore
treats a dead stream as normal and reopens it rather than raising.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from vectra180.capture.backends import backend_name, resolve_backend
from vectra180.config import CameraConfig
from vectra180.errors import CaptureError

__all__ = ["CameraSource", "Frame"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Frame:
    """One captured image plus the clocks needed to time it.

    ``monotonic`` drives frame pacing and segment length because it cannot
    jump; ``wall_time`` names files and stamps sidecars, and on a Pi without an
    RTC it may leap once NTP settles.
    """

    image: np.ndarray
    index: int
    monotonic: float
    wall_time: float

    @property
    def size(self) -> tuple[int, int]:
        height, width = self.image.shape[:2]
        return width, height


class CameraSource:
    """An OpenCV capture wrapped in open/reconnect/close lifecycle handling."""

    def __init__(self, config: CameraConfig) -> None:
        self._config = config
        self._capture: cv2.VideoCapture | None = None
        self._backend: int = 0
        self._failures = 0
        self._index = 0

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Open the device, trying each candidate backend in turn.

        Raises:
            CaptureError: if no backend produced a working stream.
        """
        if self._capture is not None:
            return

        target: int | str = self._config.device or self._config.index
        attempts: list[str] = []
        candidates = resolve_backend(self._config.backend)

        for backend in candidates:
            capture = cv2.VideoCapture(target, backend)
            if not capture.isOpened():
                capture.release()
                attempts.append(f"{backend_name(backend)}: device did not open")
                continue

            self._configure(capture)
            ok, _ = capture.read()
            if not ok:
                capture.release()
                # This is what a camera another program already holds looks
                # like: the handle opens, the stream never starts. Saying so
                # saves an hour spent on cables and drivers.
                attempts.append(
                    f"{backend_name(backend)}: opened but returned no frames (another program may be using this camera)"
                )
                continue

            self._capture = capture
            self._backend = backend
            self._failures = 0
            log.info(
                "camera %s open via %s at %dx%d @ %.1ffps",
                target,
                backend_name(backend),
                self.width,
                self.height,
                self.fps,
            )
            # Falling through to a second driver is not a neutral retry when
            # the camera is addressed by index. Backends enumerate devices in
            # their own order -- a laptop with a USB fisheye and a built-in
            # webcam can have MSMF calling the fisheye 0 while DirectShow
            # calls the webcam 0 -- so a fallback can quietly start recording
            # a completely different camera.
            if backend != candidates[0] and not self._config.device:
                log.warning(
                    "%s could not be used, so camera %s was opened via %s instead -- "
                    "an index means a different device on a different backend. "
                    "Run 'vectra180 devices' and pin camera.backend if this is the wrong camera",
                    backend_name(candidates[0]),
                    target,
                    backend_name(backend),
                )
            # A UVC device offers a fixed list of modes and silently substitutes
            # the nearest one it has. Asking for a rate it cannot do at full
            # resolution is answered with a smaller picture, not an error, and
            # an INFO line reading 640x480 is easy to scroll past on a machine
            # that was meant to be recording the road at 2560x720.
            wanted = (self._config.width, self._config.height)
            if wanted != (0, 0) and wanted != (self.width, self.height):
                log.warning(
                    "camera gave %dx%d, not the requested %dx%d -- run 'vectra180 doctor' "
                    "to see the modes this device really offers",
                    self.width,
                    self.height,
                    wanted[0],
                    wanted[1],
                )
            return

        detail = "; ".join(attempts) or "no capture backends available"
        raise CaptureError(f"could not open camera {target!r} ({detail})")

    def _configure(self, capture: cv2.VideoCapture) -> None:
        """Request the configured format.

        The FOURCC must be set before the resolution: a UVC device advertises
        different resolution tables per pixel format, and asking for 2560x720
        while still in YUYV mode silently lands on a much smaller mode.

        A width and height of zero mean "whatever the driver opens in", so the
        resolution is left untouched rather than forced.
        """
        cfg = self._config
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*cfg.fourcc))
        if cfg.width and cfg.height:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        capture.set(cv2.CAP_PROP_FPS, cfg.fps)
        # A large driver-side buffer adds latency without helping a dashcam:
        # a stale frame is worth less than a dropped one. Not every backend
        # honours this, hence no check on the return value.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def __enter__(self) -> CameraSource:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- properties --------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._capture is not None

    def _prop(self, prop: int, fallback: float) -> float:
        if self._capture is None:
            return fallback
        value = float(self._capture.get(prop))
        return value if value > 0 else fallback

    @property
    def width(self) -> int:
        return int(self._prop(cv2.CAP_PROP_FRAME_WIDTH, self._config.width))

    @property
    def height(self) -> int:
        return int(self._prop(cv2.CAP_PROP_FRAME_HEIGHT, self._config.height))

    @property
    def fps(self) -> float:
        """Frame rate as reported by the driver.

        Several UVC drivers report 0 or a nonsensical value, so the configured
        rate is used as the fallback. Never divide by this without a guard.
        """
        return self._prop(cv2.CAP_PROP_FPS, float(self._config.fps))

    @property
    def backend(self) -> str:
        return backend_name(self._backend) if self._capture is not None else "closed"

    def describe(self) -> dict[str, Any]:
        return {
            "open": self.is_open,
            "backend": self.backend,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 2),
            "device": self._config.device or self._config.index,
            "fourcc": self._config.fourcc,
        }

    # -- reading -----------------------------------------------------------

    def read(self) -> Frame | None:
        """Return the next frame, or ``None`` if this read failed.

        A ``None`` is not fatal on its own; :meth:`frames` counts consecutive
        failures and reconnects once they pass the configured limit.
        """
        if self._capture is None:
            return None
        ok, image = self._capture.read()
        if not ok or image is None:
            self._failures += 1
            return None
        self._failures = 0
        frame = Frame(image=image, index=self._index, monotonic=time.monotonic(), wall_time=time.time())
        self._index += 1
        return frame

    @property
    def failure_streak(self) -> int:
        return self._failures

    def frames(self, *, reconnect: bool = True) -> Iterator[Frame]:
        """Yield frames forever, reopening the device when the stream dies.

        Args:
            reconnect: when ``False`` the iterator stops at the first
                sustained failure instead of retrying. Tests and one-shot
                tools want that; the dashcam service does not.
        """
        self.open()
        while True:
            frame = self.read()
            if frame is not None:
                yield frame
                continue

            if self._failures < self._config.read_failure_limit:
                continue

            log.warning("camera stream stalled after %d failed reads", self._failures)
            self.close()
            if not reconnect:
                return
            time.sleep(self._config.reconnect_delay)
            try:
                self.open()
            except CaptureError as exc:
                log.warning("reconnect failed: %s", exc)
                time.sleep(self._config.reconnect_delay)
