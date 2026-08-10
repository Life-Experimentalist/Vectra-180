"""Selection and probing of OpenCV capture backends.

The original engine hard-coded ``cv2.CAP_DSHOW``, which exists only on
Windows. Vectra-180 targets a Raspberry Pi Compute Module 5, so the backend is
resolved from the running platform instead.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2

__all__ = [
    "DeviceInfo",
    "backend_name",
    "backend_names",
    "enumerate_devices",
    "has_backend",
    "preferred_backends",
    "probe_backends",
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


def backend_names() -> list[str]:
    """Every name ``camera.backend`` accepts, whatever this build carries.

    Spelling and availability are different questions: ``v4l2`` in a config
    edited on a laptop is the right setting for the Pi it is bound for.
    """
    return sorted(_BACKENDS)


def has_backend(backend: int) -> bool:
    """Whether this OpenCV build can actually use ``backend``.

    A backend constant always exists; the code behind it only exists if the
    build was compiled with it. The PyPI wheels ship without GStreamer, so
    ``cv2.CAP_GSTREAMER`` is a name with nothing behind it there.
    """
    return bool(cv2.videoio_registry.hasBackend(backend))


def resolve_backend(name: str) -> list[int]:
    """Turn a ``camera.backend`` setting into an ordered list of candidates.

    ``auto`` expands to the platform order; anything else pins a single
    backend so a user can force a specific driver when auto-detection picks
    badly.

    Raises:
        ValueError: if the name is unknown, or names a backend this OpenCV
            build cannot use.
    """
    key = name.strip().lower()
    if key in {"auto", ""}:
        return preferred_backends()
    if key not in _BACKENDS:
        valid = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown capture backend {name!r} (expected auto, {valid})")

    backend = _BACKENDS[key]
    # Without this the failure surfaces as "device did not open" against a
    # perfectly good camera, which sends people looking at cabling. It bites
    # hardest on `gstreamer`, the backend a CSI camera needs and the one the
    # pip wheels leave out.
    if not has_backend(backend):
        raise ValueError(
            f"this OpenCV build has no {key} support "
            f"(cv2 {cv2.__version__} provides: {', '.join(_available_names()) or 'none'}); "
            'install a build that includes it, or set camera.backend = "auto"'
        )
    return [backend]


def _available_names() -> list[str]:
    """Names from ``_BACKENDS`` that this build can actually use."""
    return sorted(name for name, value in _BACKENDS.items() if name != "any" and has_backend(value))


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
    #: ``False`` when the device opened but handed back no frame. That is what
    #: a camera another program is already holding looks like, and it is worth
    #: saying out loud rather than leaving off the list as though it were
    #: unplugged.
    readable: bool = True

    @property
    def label(self) -> str:
        """Backend-qualified, because an index alone does not name a camera.

        The same integer addresses different hardware on different drivers, so
        ``[0]`` on its own is an invitation to configure the wrong one.
        """
        location = self.path or f"index {self.index}"
        return f"{self.backend}[{self.index}] {self.name} ({location})"


def _linux_device_name(index: int) -> str:
    """Read a V4L2 device's product name from sysfs.

    Reading sysfs avoids shelling out, so there is no command string for a
    device name to escape from.
    """
    try:
        return Path(f"/sys/class/video4linux/video{index}/name").read_text(encoding="utf-8").strip()
    except OSError:
        return f"Video device {index}"


def probe_backends() -> list[int]:
    """Distinct drivers worth probing on this platform.

    ``any`` is dropped: it is a resolver rather than a driver, and it would
    re-list whichever of the others answered first.
    """
    names = _PLATFORM_ORDER.get(_platform_key(), ("any",))
    usable = [_BACKENDS[name] for name in names if name != "any" and has_backend(_BACKENDS[name])]
    return usable or [cv2.CAP_ANY]


def enumerate_devices(max_index: int = 10, backends: Sequence[int] | None = None) -> list[DeviceInfo]:
    """Probe capture indices on every usable driver and report what answered.

    Opening a device is the only portable way to know it works: an entry in
    ``/dev`` may be a metadata node with no streaming capability, and the
    ordering of Windows device names does not match OpenCV's indices.

    Every driver is probed rather than stopping at the first that works,
    because **an index does not name the same camera across backends**. On one
    Windows laptop MSMF numbers a USB fisheye 0 and the built-in webcam 1,
    while DirectShow numbers the pair the other way round -- so ``index = 0``
    selects different hardware depending on which driver ends up answering.
    Listing both makes that visible rather than surprising.

    A device that opens but hands back no frame is listed with ``readable``
    false instead of being skipped. Dropping it silently is how "another
    program is using this camera" gets misread as "this camera is not there".
    """
    on_linux = _platform_key() == "linux"
    candidates = list(backends) if backends is not None else probe_backends()
    found: list[DeviceInfo] = []

    for backend in candidates:
        for index in range(max_index):
            capture = cv2.VideoCapture(index, backend)
            try:
                if not capture.isOpened():
                    continue
                ok, frame = capture.read()
                readable = bool(ok) and frame is not None
                if readable:
                    height, width = frame.shape[:2]
                else:
                    # No frame to measure, so fall back to what the driver
                    # claims. It is usually right about the mode even when it
                    # cannot hand the pixels over.
                    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                found.append(
                    DeviceInfo(
                        index=index,
                        path=f"/dev/video{index}" if on_linux else "",
                        name=_linux_device_name(index) if on_linux else f"Camera {index}",
                        width=width,
                        height=height,
                        fps=float(capture.get(cv2.CAP_PROP_FPS)),
                        backend=backend_name(backend),
                        readable=readable,
                    )
                )
            finally:
                capture.release()

    return found
