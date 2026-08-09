"""Vectra-180 -- dual-fisheye dashcam and stereoscopic depth engine.

The package is arranged around the pipeline:

``capture``
    Opening the UVC device and reading frames, with reconnection.
``telemetry``
    Decoding the embedded IMU block and estimating attitude.
``imaging``
    Dewarping, stereo depth, stitching, stabilisation, the HUD.
``recorder``
    Encoding, segmentation, retention and incident locking.
``engine``
    The capture thread that ties those together.
``service``
    The headless HTTP interface and its phone-facing web UI.
``ui``
    An optional DearPyGui desktop console.

Only names re-exported here are the public API; everything else may move
between releases. Importing this package pulls in OpenCV and NumPy but nothing
GUI-related -- the desktop UI loads DearPyGui lazily, so a headless Pi never
pays for it.
"""

__version__ = "1.0.0"

from vectra180.config import EngineConfig
from vectra180.engine import Engine, EngineSnapshot
from vectra180.errors import (
    CaptureError,
    ConfigError,
    DeviceNotFoundError,
    RecorderError,
    ServiceError,
    VectraError,
)

__all__ = [
    "CaptureError",
    "ConfigError",
    "DeviceNotFoundError",
    "Engine",
    "EngineConfig",
    "EngineSnapshot",
    "RecorderError",
    "ServiceError",
    "VectraError",
    "__version__",
]
