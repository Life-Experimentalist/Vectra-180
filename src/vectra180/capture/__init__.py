"""Camera acquisition: backend selection, device probing, frame reading."""

from vectra180.capture.backends import (
    DeviceInfo,
    backend_name,
    backend_names,
    enumerate_devices,
    preferred_backends,
    probe_backends,
    resolve_backend,
)
from vectra180.capture.source import CameraSource, Frame

__all__ = [
    "CameraSource",
    "DeviceInfo",
    "Frame",
    "backend_name",
    "backend_names",
    "enumerate_devices",
    "preferred_backends",
    "probe_backends",
    "resolve_backend",
]
