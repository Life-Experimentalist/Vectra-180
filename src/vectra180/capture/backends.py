"""Selection and probing of OpenCV capture backends.

The original engine hard-coded ``cv2.CAP_DSHOW``, which exists only on
Windows. Vectra-180 targets a Raspberry Pi Compute Module 5, so the backend is
resolved from the running platform instead.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import cv2

__all__ = [
    "DeviceInfo",
    "backend_name",
    "enumerate_devices",
    "preferred_backends",
    "resolve_backend",
]

#: Names accepted by ``camera.backend`` mapped to their OpenCV constant.
_BACKENDS: dict[str, int] = {
    "any": cv2.CAP_ANY,
    "v4l2": cv2.CAP_V4L2,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "avfoundation": cv2.CAP_AVFOUNDATION,
    "gstreamer": cv2.CAP_GSTREAMER,
}

_BACKEND_NAMES: dict[int, str] = {value: key for key, value in _BACKENDS.items()}

#: Highest-priority backend first, per platform.
#:
#: Linux: V4L2 is the only sane choice for a UVC device and the one the CM5
#: uses. Windows: MSMF handles MJPG at high resolutions more reliably than
#: DirectShow on modern builds, but DirectShow remains a good fallback for
#: older UVC drivers.
_PLATFORM_ORDER: dict[str, tuple[str, ...]] = {
    "linux": ("v4l2", "any"),
    "win32": ("msmf", "dshow", "any"),
    "darwin": ("avfoundation", "any"),
}


def _platform_key() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "other"


def preferred_backends() -> list[int]:
    """Return the backends to try, in order, for the current platform."""
    names = _PLATFORM_ORDER.get(_platform_key(), ("any",))
    return [_BACKENDS[name] for name in names]


def resolve_backend(name: str) -> list[int]:
    """Turn a ``camera.backend`` setting into an ordered list of candidates.

    ``auto`` expands to the platform order; anything else pins a single
    backend so a user can force a specific driver when auto-detection picks
    badly.
    """
    key = name.strip().lower()
    if key in {"auto", ""}:
        return preferred_backends()
    if key not in _BACKENDS:
        valid = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown capture backend {name!r} (expected auto, {valid})")
    return [_BACKENDS[key]]


def backend_name(value: int) -> str:
    """Return the human-readable name of an OpenCV backend constant."""
    return _BACKEND_NAMES.get(value, f"backend-{value}")


@dataclass(frozen=True)
class DeviceInfo:
    """A capture device that responded to a probe."""

    index: int
    #: ``/dev/videoN`` on Linux, empty elsewhere -- Windows and macOS address
    #: devices by index only.
    path: str
    name: str
    width: int
    height: int
    fps: float
    backend: str

    @property
    def label(self) -> str:
        location = self.path or f"index {self.index}"
        return f"[{self.index}] {self.name} ({location})"


def _linux_device_name(index: int) -> str:
    """Read a V4L2 device's product name from sysfs.

    Reading sysfs avoids shelling out, so there is no command string for a
    device name to escape from.
    """
    try:
        return Path(f"/sys/class/video4linux/video{index}/name").read_text(encoding="utf-8").strip()
    except OSError:
        return f"Video device {index}"


def enumerate_devices(max_index: int = 10) -> list[DeviceInfo]:
    """Probe capture indices and report the ones that yield a frame.

    Opening a device is the only portable way to know it works: an entry in
    ``/dev`` may be a metadata node with no streaming capability, and the
    ordering of Windows device names does not match OpenCV's indices.
    """
    on_linux = _platform_key() == "linux"
    found: list[DeviceInfo] = []

    for index in range(max_index):
        for backend in preferred_backends():
            capture = cv2.VideoCapture(index, backend)
            try:
                if not capture.isOpened():
                    continue
                ok, frame = capture.read()
                if not ok or frame is None:
                    continue
                height, width = frame.shape[:2]
                found.append(
                    DeviceInfo(
                        index=index,
                        path=f"/dev/video{index}" if on_linux else "",
                        name=_linux_device_name(index) if on_linux else f"Camera {index}",
                        width=width,
                        height=height,
                        fps=float(capture.get(cv2.CAP_PROP_FPS)),
                        backend=backend_name(backend),
                    )
                )
            finally:
                capture.release()
            break

    return found
