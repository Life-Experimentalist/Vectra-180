"""Configuration model for the Vectra-180 engine.

Settings resolve in three layers, later layers winning:

1. Dataclass defaults (below).
2. A TOML file -- ``/etc/vectra180/config.toml`` on Linux, or whatever
   ``--config`` / ``VECTRA_CONFIG`` points at.
3. ``VECTRA_*`` environment variables.

Command-line flags are applied by :mod:`vectra180.cli` on top of the result.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

__all__ = [
    "CameraConfig",
    "DepthConfig",
    "EngineConfig",
    "IncidentConfig",
    "RecordingConfig",
    "ServerConfig",
    "TelemetryConfig",
    "default_config_path",
    "default_recording_dir",
]

# Frame layout of the ICM42688 telemetry block embedded in the metadata strip.
# See vectra180.telemetry.decoder for the authoritative description.
TELEMETRY_PAYLOAD_BYTES: int = 20

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, current: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return current
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean (got {raw!r})")


def _env_int(name: str, current: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from exc


def _env_float(name: str, current: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number (got {raw!r})") from exc


def _env_str(name: str, current: str) -> str:
    return os.environ.get(name, current)


def _coerce(source: Path, key: str, current: Any, value: Any) -> Any:
    """Fit a TOML value to the type of the default it is replacing.

    The dataclass defaults are the type declaration here: ``from __future__
    import annotations`` turns ``field.type`` into a string, so the runtime
    default is the only reliable description of what a key should hold. A
    mismatch is reported against the file and key rather than surfacing later
    as a ``TypeError`` from deep inside the engine.
    """
    # bool before int: bool is a subclass of int, and `enabled = 1` should be
    # rejected rather than silently accepted as True.
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{source}: {key} must be true or false")
        return value
    if isinstance(current, Path):
        if not isinstance(value, str):
            raise ValueError(f"{source}: {key} must be a quoted path")
        return Path(value).expanduser()
    if isinstance(current, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{source}: {key} must be an integer")
        return value
    if isinstance(current, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{source}: {key} must be a number")
        return float(value)
    if isinstance(current, str) and not isinstance(value, str):
        raise ValueError(f"{source}: {key} must be a string")
    return value


def default_config_path() -> Path:
    """Return the platform's conventional config location."""
    override = os.environ.get("VECTRA_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "Vectra180" / "config.toml"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Vectra180" / "config.toml"
    # Linux / Raspberry Pi OS: prefer the system path an installed service uses.
    system = Path("/etc/vectra180/config.toml")
    if system.exists():
        return system
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vectra180" / "config.toml"


def default_recording_dir() -> Path:
    """Return the default root for recorded footage."""
    override = os.environ.get("VECTRA_RECORDING_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return Path.home() / "Videos" / "Vectra180"
    system = Path("/var/lib/vectra180/recordings")
    if system.parent.exists():
        return system
    return Path.home() / "vectra180-recordings"


@dataclass
class CameraConfig:
    """How the dual-fisheye UVC device is opened."""

    index: int = 0
    #: Explicit device path (``/dev/video2``). Takes precedence over ``index``.
    device: str = ""
    width: int = 2560
    height: int = 720
    fps: int = 30
    #: ``auto`` picks V4L2 on Linux, DirectShow on Windows, AVFoundation on macOS.
    backend: str = "auto"
    #: Requested pixel format. MJPG is required on most UVC devices to reach
    #: 2560x720 at 30fps -- YUYV cannot sustain that over USB 2.0 bandwidth.
    fourcc: str = "MJPG"
    #: Seconds to wait between reconnect attempts after the stream drops.
    reconnect_delay: float = 2.0
    #: Consecutive failed reads before the source is considered disconnected.
    read_failure_limit: int = 30

    def apply_env(self) -> None:
        self.index = _env_int("VECTRA_CAMERA_INDEX", self.index)
        self.device = _env_str("VECTRA_CAMERA_DEVICE", self.device)
        self.width = _env_int("VECTRA_CAPTURE_WIDTH", self.width)
        self.height = _env_int("VECTRA_CAPTURE_HEIGHT", self.height)
        self.fps = _env_int("VECTRA_CAPTURE_FPS", self.fps)
        self.backend = _env_str("VECTRA_CAPTURE_BACKEND", self.backend)
        self.fourcc = _env_str("VECTRA_CAPTURE_FOURCC", self.fourcc)

    def validate(self) -> None:
        if self.index < 0:
            raise ValueError("camera.index must be >= 0")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("camera.width and camera.height must be positive")
        if self.fps <= 0:
            raise ValueError("camera.fps must be positive")
        if len(self.fourcc) != 4:
            raise ValueError("camera.fourcc must be exactly four characters (e.g. MJPG)")
        if self.reconnect_delay < 0:
            raise ValueError("camera.reconnect_delay must be >= 0")
        if self.read_failure_limit < 1:
            raise ValueError("camera.read_failure_limit must be >= 1")


@dataclass
class TelemetryConfig:
    """Extraction of the IMU block embedded in the frame's leftmost columns.

    Not every dual-fisheye module emits this strip. Run ``vectra180 doctor``
    against your hardware to confirm before relying on it.
    """

    enabled: bool = True
    #: Width in pixels of the metadata strip cropped off the left of each frame.
    metadata_width: int = 30
    #: Low-pass factor for angular velocity. Higher = smoother, more lag.
    smoothing_alpha: float = 0.92
    #: Complementary-filter weight. 1.0 = gyro only (drifts), 0.0 = accel only
    #: (noisy under vehicle acceleration). 0.98 is the usual dashcam compromise.
    complementary_alpha: float = 0.98
    #: Roll and pitch are corrected against gravity, but yaw has no absolute
    #: reference without a magnetometer, so it is bled back toward zero with
    #: this time constant in seconds. 0 disables the leak and lets yaw drift.
    yaw_leak_seconds: float = 20.0
    #: Accelerometer magnitude may deviate from 1g by at most this much (in g)
    #: for a sample to be trusted as a gravity reference. Braking, cornering
    #: and potholes all push it outside this band.
    gravity_tolerance_g: float = 0.25

    def apply_env(self) -> None:
        self.enabled = _env_bool("VECTRA_TELEMETRY_ENABLED", self.enabled)
        self.metadata_width = _env_int("VECTRA_METADATA_WIDTH", self.metadata_width)
        self.smoothing_alpha = _env_float("VECTRA_GYRO_SMOOTHING", self.smoothing_alpha)
        self.complementary_alpha = _env_float("VECTRA_COMPLEMENTARY_ALPHA", self.complementary_alpha)
        self.yaw_leak_seconds = _env_float("VECTRA_YAW_LEAK_SECONDS", self.yaw_leak_seconds)
        self.gravity_tolerance_g = _env_float("VECTRA_GRAVITY_TOLERANCE_G", self.gravity_tolerance_g)

    def validate(self) -> None:
        if self.metadata_width < 0:
            raise ValueError("telemetry.metadata_width must be >= 0")
        if not 0.0 <= self.smoothing_alpha < 1.0:
            raise ValueError("telemetry.smoothing_alpha must be in [0.0, 1.0)")
        if not 0.0 <= self.complementary_alpha <= 1.0:
            raise ValueError("telemetry.complementary_alpha must be in [0.0, 1.0]")
        if self.yaw_leak_seconds < 0:
            raise ValueError("telemetry.yaw_leak_seconds must be >= 0")
        if self.gravity_tolerance_g <= 0:
            raise ValueError("telemetry.gravity_tolerance_g must be positive")


@dataclass
class RecordingConfig:
    """Loop recording -- the dashcam's primary duty.

    Footage is written as fixed-length segments so that pruning the oldest
    material never truncates a file that is currently being written.
    """

    enabled: bool = True
    directory: Path = field(default_factory=default_recording_dir)
    #: Length of each clip. Shorter segments lose less on power cut but cost
    #: more container overhead.
    segment_seconds: int = 60
    #: Total budget for unprotected footage. Oldest segments are pruned first.
    max_bytes: int = 32 * 1024**3
    #: Pruning also triggers if the filesystem drops below this much free space.
    min_free_bytes: int = 2 * 1024**3
    #: Separate budget for incident clips, which normal pruning never touches.
    max_event_bytes: int = 8 * 1024**3
    container: str = "mp4"
    #: ``auto`` uses ffmpeg when present and falls back to OpenCV's writer.
    encoder: str = "auto"
    #: x264 preset. The CM5 has no hardware H.264 encoder, so this is CPU-bound;
    #: ``ultrafast`` or ``superfast`` are the only realistic choices at 2560x720.
    preset: str = "ultrafast"
    bitrate_kbps: int = 8000
    #: Write a JSON sidecar of IMU samples alongside each segment.
    write_telemetry_sidecar: bool = True
    #: Burn the wall-clock time into the recorded pixels. Container metadata
    #: does not survive a re-encode or a screenshot; the pixels do, which is
    #: what makes footage useful after an incident.
    burn_timestamp: bool = True

    def apply_env(self) -> None:
        self.enabled = _env_bool("VECTRA_RECORDING_ENABLED", self.enabled)
        self.burn_timestamp = _env_bool("VECTRA_BURN_TIMESTAMP", self.burn_timestamp)
        raw_dir = os.environ.get("VECTRA_RECORDING_DIR")
        if raw_dir:
            self.directory = Path(raw_dir).expanduser()
        self.segment_seconds = _env_int("VECTRA_SEGMENT_SECONDS", self.segment_seconds)
        self.max_bytes = _env_int("VECTRA_MAX_BYTES", self.max_bytes)
        self.min_free_bytes = _env_int("VECTRA_MIN_FREE_BYTES", self.min_free_bytes)
        self.max_event_bytes = _env_int("VECTRA_MAX_EVENT_BYTES", self.max_event_bytes)
        self.encoder = _env_str("VECTRA_ENCODER", self.encoder)
        self.preset = _env_str("VECTRA_ENCODER_PRESET", self.preset)
        self.bitrate_kbps = _env_int("VECTRA_BITRATE_KBPS", self.bitrate_kbps)

    def validate(self) -> None:
        if self.segment_seconds < 5:
            raise ValueError("recording.segment_seconds must be >= 5")
        if self.max_bytes <= 0:
            raise ValueError("recording.max_bytes must be positive")
        if self.min_free_bytes < 0:
            raise ValueError("recording.min_free_bytes must be >= 0")
        if self.max_event_bytes < 0:
            raise ValueError("recording.max_event_bytes must be >= 0")
        if self.bitrate_kbps <= 0:
            raise ValueError("recording.bitrate_kbps must be positive")
        if self.encoder not in {"auto", "ffmpeg", "opencv"}:
            raise ValueError("recording.encoder must be one of: auto, ffmpeg, opencv")

    @property
    def normal_dir(self) -> Path:
        return self.directory / "normal"

    @property
    def event_dir(self) -> Path:
        return self.directory / "events"


@dataclass
class IncidentConfig:
    """G-sensor incident detection.

    A collision or hard brake shows up as the accelerometer magnitude departing
    from 1g. When that happens the current segment -- and the one before it, so
    the run-up is preserved -- are moved out of the pruning pool.
    """

    enabled: bool = True
    #: Deviation from 1g, in g, that counts as an impact.
    threshold_g: float = 0.6
    #: Ignore further triggers for this long so one event locks one incident.
    cooldown_seconds: float = 10.0
    #: Also protect the segment recorded immediately before the trigger.
    lock_previous_segment: bool = True

    def apply_env(self) -> None:
        self.enabled = _env_bool("VECTRA_INCIDENT_ENABLED", self.enabled)
        self.threshold_g = _env_float("VECTRA_INCIDENT_THRESHOLD_G", self.threshold_g)
        self.cooldown_seconds = _env_float("VECTRA_INCIDENT_COOLDOWN", self.cooldown_seconds)

    def validate(self) -> None:
        if self.threshold_g <= 0:
            raise ValueError("incident.threshold_g must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("incident.cooldown_seconds must be >= 0")


@dataclass
class DepthConfig:
    """Stereo depth parameters.

    Depth is computed on demand rather than in the recording path: SGBM on a
    CM5 runs at low single-digit fps on a full 2560x720 frame, and a dashcam
    must never trade a recorded frame for a disparity map.
    """

    #: Frames are downscaled to this width before matching. The dominant cost
    #: knob -- SGBM scales roughly with pixel count times disparity range.
    working_width: int = 640
    num_disparities: int = 80
    block_size: int = 7
    uniqueness_ratio: int = 10
    #: Fisheye focal length as a fraction of frame width, used to build K.
    focal_scale: float = 0.5

    def apply_env(self) -> None:
        self.working_width = _env_int("VECTRA_DEPTH_WIDTH", self.working_width)
        self.num_disparities = _env_int("VECTRA_DEPTH_DISPARITIES", self.num_disparities)
        self.block_size = _env_int("VECTRA_DEPTH_BLOCK_SIZE", self.block_size)
        self.uniqueness_ratio = _env_int("VECTRA_DEPTH_UNIQUENESS", self.uniqueness_ratio)
        self.focal_scale = _env_float("VECTRA_FOCAL_SCALE", self.focal_scale)

    def validate(self) -> None:
        if self.working_width < 64:
            raise ValueError("depth.working_width must be >= 64")
        if self.num_disparities < 16:
            raise ValueError("depth.num_disparities must be >= 16")
        if self.block_size < 3:
            raise ValueError("depth.block_size must be >= 3")
        if not 0.0 < self.focal_scale <= 2.0:
            raise ValueError("depth.focal_scale must be in (0.0, 2.0]")


@dataclass
class ServerConfig:
    """The headless HTTP interface.

    Recorded footage is sensitive. The default binds to loopback only; widening
    ``host`` publishes every clip to everyone on the network, so that is an
    explicit opt-in and ``token`` should be set alongside it.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    #: Shared secret required as ``Authorization: Bearer <token>`` or ``?token=``.
    #: Empty disables auth, which is only safe on loopback.
    token: str = ""
    #: JPEG quality for the MJPEG preview stream (1-100).
    preview_quality: int = 70
    #: Preview frames per second. Kept low so preview never starves recording.
    preview_fps: int = 10
    #: Longest edge of the preview image, in pixels.
    preview_width: int = 960

    def apply_env(self) -> None:
        self.enabled = _env_bool("VECTRA_SERVER_ENABLED", self.enabled)
        self.host = _env_str("VECTRA_SERVER_HOST", self.host)
        self.port = _env_int("VECTRA_SERVER_PORT", self.port)
        self.token = _env_str("VECTRA_SERVER_TOKEN", self.token)
        self.preview_quality = _env_int("VECTRA_PREVIEW_QUALITY", self.preview_quality)
        self.preview_fps = _env_int("VECTRA_PREVIEW_FPS", self.preview_fps)
        self.preview_width = _env_int("VECTRA_PREVIEW_WIDTH", self.preview_width)

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("server.port must be in 1..65535")
        if not 1 <= self.preview_quality <= 100:
            raise ValueError("server.preview_quality must be in 1..100")
        if self.preview_fps < 1:
            raise ValueError("server.preview_fps must be >= 1")
        if self.preview_width < 160:
            raise ValueError("server.preview_width must be >= 160")

    @property
    def is_public(self) -> bool:
        """True when the bind address is reachable from outside this machine."""
        return self.host not in {"127.0.0.1", "localhost", "::1"}


@dataclass
class EngineConfig:
    """Root configuration object."""

    camera: CameraConfig = field(default_factory=CameraConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    incident: IncidentConfig = field(default_factory=IncidentConfig)
    depth: DepthConfig = field(default_factory=DepthConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    @classmethod
    def load(cls, path: Path | str | None = None, *, use_env: bool = True) -> EngineConfig:
        """Build a config from defaults, then a TOML file, then the environment.

        A missing file is not an error -- the defaults are a working setup.
        """
        config = cls()
        resolved = Path(path).expanduser() if path is not None else default_config_path()
        if resolved.is_file():
            config.merge_toml(resolved)
        elif path is not None:
            raise FileNotFoundError(f"config file not found: {resolved}")
        if use_env:
            config.apply_env()
        config.validate()
        return config

    def merge_toml(self, path: Path) -> None:
        """Overlay a TOML file onto this config. Unknown keys are rejected."""
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        sections = {f.name: getattr(self, f.name) for f in fields(self)}
        for section_name, values in data.items():
            section = sections.get(section_name)
            if section is None:
                raise ValueError(f"{path}: unknown config section [{section_name}]")
            if not isinstance(values, dict):
                raise ValueError(f"{path}: [{section_name}] must be a table")
            known = {f.name for f in fields(section)}
            for key, value in values.items():
                if key not in known:
                    raise ValueError(f"{path}: unknown key {section_name}.{key}")
                setattr(section, key, _coerce(path, f"{section_name}.{key}", getattr(section, key), value))

    def apply_env(self) -> None:
        for f in fields(self):
            getattr(self, f.name).apply_env()

    def validate(self) -> None:
        for f in fields(self):
            getattr(self, f.name).validate()

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """JSON-serialisable view.

        Args:
            redact: replace the auth token with ``"***"``. On by default,
                because the main consumer is the status endpoint, which any
                authenticated client can read. Only the local CLI turns it off,
                and only when explicitly asked to.
        """
        data = asdict(self)
        data["recording"]["directory"] = str(self.recording.directory)
        if redact and self.server.token:
            data["server"]["token"] = "***"
        return data
