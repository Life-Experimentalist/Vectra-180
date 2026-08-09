"""Pre-flight hardware diagnostics.

``vectra180 doctor`` answers the question a new install actually raises: *will
this record, on this machine, with this camera?* Every check therefore exercises
the real path rather than reporting a setting back. The camera is opened, frames
are read, the encoder is timed against the resolution the camera really produced,
and the recording volume is written to.

The encoder benchmark matters most on a Compute Module 5. It has no hardware
H.264 block, so libx264 runs on the Cortex-A76 cores and 2560x720 at 30fps is
genuinely close to the limit. Finding that out here beats finding it out from a
clip with two thirds of its frames missing.
"""

from __future__ import annotations

import logging
import platform
import shutil
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from vectra180 import __version__
from vectra180.capture import CameraSource, enumerate_devices
from vectra180.config import EngineConfig
from vectra180.errors import CaptureError, RecorderError
from vectra180.imaging import strip_metadata
from vectra180.recorder import create_writer, ffmpeg_path, storage_stats
from vectra180.telemetry import TelemetryDecoder

__all__ = ["Check", "Report", "run_diagnostics"]

log = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_ICONS = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}

#: Frames read when measuring capture throughput. Enough to average out one
#: slow read without making the command feel hung.
_CAPTURE_SAMPLES = 30

#: Frames pushed through the encoder in the throughput benchmark.
_ENCODE_SAMPLES = 30

#: Frames kept back from the capture probe for the later checks. Two, because
#: the telemetry decoder accepts a sample only once a second frame continues its
#: timeline -- handed a single strip it always reports nothing.
_KEPT_FRAMES = 2

#: Measured rate below this fraction of the configured rate is reported as a
#: problem rather than noise.
_RATE_TOLERANCE = 0.8


@dataclass(frozen=True)
class Check:
    """One diagnostic result."""

    name: str
    status: str
    detail: str
    #: What to do about it. Empty when the check passed.
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "remedy": self.remedy}

    def __str__(self) -> str:
        line = f"{_ICONS[self.status]} {self.name}: {self.detail}"
        return f"{line}\n         -> {self.remedy}" if self.remedy else line


@dataclass
class Report:
    """The full set of checks and the verdict they add up to."""

    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, remedy: str = "") -> Check:
        check = Check(name=name, status=status, detail=detail, remedy=remedy)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> int:
        return sum(1 for check in self.checks if check.status == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for check in self.checks if check.status == WARN)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failed": self.failed,
            "warned": self.warned,
            "checks": [check.as_dict() for check in self.checks],
        }

    def render(self) -> str:
        lines = [str(check) for check in self.checks]
        if self.failed:
            lines.append(f"\n{self.failed} check(s) failed, {self.warned} warning(s). Recording will not be reliable.")
        elif self.warned:
            lines.append(f"\nAll critical checks passed with {self.warned} warning(s).")
        else:
            lines.append("\nAll checks passed.")
        return "\n".join(lines)


def _check_environment(report: Report) -> None:
    report.add(
        "environment",
        OK,
        f"vectra180 {__version__} on {platform.system()} {platform.machine()}, "
        f"python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}, "
        f"opencv {cv2.__version__}, numpy {np.__version__}",
    )


def _check_devices(report: Report) -> None:
    devices = enumerate_devices()
    if not devices:
        report.add(
            "devices",
            FAIL,
            "no capture device responded to a probe",
            "check the USB connection; on Linux confirm the user is in the 'video' group and that /dev/video* exists",
        )
        return
    report.add("devices", OK, "; ".join(device.label for device in devices))


def _check_camera(report: Report, config: EngineConfig) -> list[np.ndarray]:
    """Open the configured camera and measure it.

    Returns:
        The last :data:`_KEPT_FRAMES` images read, oldest first, for the
        telemetry and encoder checks to work on. Empty if nothing was captured.
    """
    source = CameraSource(config.camera)
    try:
        source.open()
    except CaptureError as exc:
        report.add(
            "camera",
            FAIL,
            str(exc),
            "run 'vectra180 devices' to see what is attached, then set camera.index or camera.device in the config",
        )
        return []

    try:
        recent: deque[np.ndarray] = deque(maxlen=_KEPT_FRAMES)
        started = time.monotonic()
        read = 0
        for _ in range(_CAPTURE_SAMPLES):
            frame = source.read()
            if frame is not None:
                recent.append(frame.image)
            read += 1
        elapsed = time.monotonic() - started

        if not recent:
            report.add(
                "camera",
                FAIL,
                "the device opened but returned no frames",
                "another process may hold the camera; close it and retry",
            )
            return []

        height, width = recent[-1].shape[:2]
        measured = read / elapsed if elapsed > 0 else 0.0
        detail = f"{width}x{height} via {source.backend}, {measured:.1f} fps measured ({config.camera.fps} requested)"

        if (width, height) != (config.camera.width, config.camera.height):
            report.add(
                "camera",
                WARN,
                f"{detail}; driver gave {width}x{height}, not the requested "
                f"{config.camera.width}x{config.camera.height}",
                "the requested mode is unsupported at this pixel format -- confirm the device "
                "lists it (v4l2-ctl --list-formats-ext on Linux) or set camera.width/height to a real mode",
            )
        elif measured < config.camera.fps * _RATE_TOLERANCE:
            report.add(
                "camera",
                WARN,
                detail,
                "the USB link or the pixel format is the bottleneck; MJPG is required for "
                "2560x720 and a USB 3 port helps",
            )
        else:
            report.add("camera", OK, detail)
        return list(recent)
    finally:
        source.close()


def _check_telemetry(report: Report, config: EngineConfig, images: list[np.ndarray]) -> None:
    if not config.telemetry.enabled:
        report.add("telemetry", OK, "disabled in the config")
        return
    if not images:
        report.add("telemetry", WARN, "skipped -- no frame to inspect")
        return

    # Both widths are checked here rather than left to strip_metadata, which
    # raises: one bad setting must not cancel the rest of the report.
    width = config.telemetry.metadata_width
    frame_width = images[-1].shape[1]
    if width <= 0:
        report.add("telemetry", OK, "no metadata strip configured (telemetry.metadata_width = 0)")
        return
    if width >= frame_width:
        report.add(
            "telemetry",
            WARN,
            f"telemetry.metadata_width is {width}px but the frame is only {frame_width}px wide",
            "set telemetry.metadata_width to the strip's real width, or 0 if this camera has no strip",
        )
        return

    # The whole run is decoded, not just the newest strip: a sample counts only
    # once a second frame continues its timeline, so one strip alone never does.
    decoder = TelemetryDecoder()
    sample = None
    for image in images:
        _, strip = strip_metadata(image, width)
        sample = decoder.decode_strip(strip)

    if sample is None:
        report.add(
            "telemetry",
            WARN,
            "no IMU block decoded from the metadata strip",
            "not every dual-fisheye module embeds telemetry. Set telemetry.enabled = false "
            "to stop looking, or telemetry.metadata_width if the strip is a different width",
        )
        return

    report.add(
        "telemetry",
        OK,
        f"IMU present: {sample.accel_magnitude_g:.2f} g total, "
        f"gyro {sample.gyro_x:+.2f}/{sample.gyro_y:+.2f}/{sample.gyro_z:+.2f} rad/s",
    )


def _check_ffmpeg(report: Report) -> None:
    path = ffmpeg_path()
    if path is None:
        report.add(
            "ffmpeg",
            WARN,
            "not on PATH -- recording will fall back to the OpenCV writer",
            "install it (apt install ffmpeg) for bitrate control and reliable container finalisation",
        )
        return
    report.add("ffmpeg", OK, path)


def _check_storage(report: Report, config: EngineConfig) -> None:
    directory = config.recording.directory
    try:
        stats = storage_stats(config.recording)
    except OSError as exc:
        report.add(
            "storage",
            FAIL,
            f"cannot use {directory}: {exc}",
            "create the directory and give the service user write access",
        )
        return

    probe = directory / ".vectra-write-test"
    try:
        probe.write_bytes(b"vectra")
        probe.unlink()
    except OSError as exc:
        report.add(
            "storage",
            FAIL,
            f"{directory} is not writable: {exc}",
            "chown the recording directory to the user the service runs as",
        )
        return

    free_gb = stats.free_bytes / 1024**3
    detail = (
        f"{directory}: {free_gb:.1f} GB free, {stats.normal_clips} loop clip(s), {stats.event_clips} locked clip(s)"
    )
    if stats.free_bytes < config.recording.min_free_bytes:
        report.add(
            "storage",
            WARN,
            detail,
            f"below the {config.recording.min_free_bytes / 1024**3:.1f} GB reserve; "
            "the first pruning pass will reclaim space",
        )
    else:
        report.add("storage", OK, detail)


def _check_encoder(report: Report, config: EngineConfig, images: list[np.ndarray]) -> None:
    """Time the encoder on frames the size the camera actually produces."""
    if not images:
        report.add("encoder", WARN, "skipped -- no frame to encode")
        return

    image = images[-1]
    height, width = image.shape[:2]
    # The recorder crops to even dimensions before encoding; match that here or
    # the benchmark measures a size that will never be written.
    size = (width - width % 2, height - height % 2)
    frame = np.ascontiguousarray(image[: size[1], : size[0]])

    workspace = Path(tempfile.mkdtemp(prefix="vectra-doctor-"))
    target = workspace / f"benchmark.{config.recording.container}"
    try:
        writer = create_writer(target, size, float(config.camera.fps), config.recording)
    except RecorderError as exc:
        report.add(
            "encoder",
            FAIL,
            str(exc),
            "install ffmpeg, or set recording.encoder = 'opencv' to use the built-in writer",
        )
        shutil.rmtree(workspace, ignore_errors=True)
        return

    try:
        started = time.monotonic()
        for _ in range(_ENCODE_SAMPLES):
            writer.write(frame)
        writer.close()
        elapsed = time.monotonic() - started
    except RecorderError as exc:
        report.add("encoder", FAIL, f"encoding failed: {exc}", "check the ffmpeg install and the container setting")
        return
    finally:
        # close() is idempotent, so this only bites on the path where write()
        # raised. Without it the ffmpeg process outlives the benchmark and still
        # holds benchmark.mp4 open, which is enough to defeat rmtree on Windows.
        writer.close()
        shutil.rmtree(workspace, ignore_errors=True)

    rate = _ENCODE_SAMPLES / elapsed if elapsed > 0 else 0.0
    detail = (
        f"{type(writer).__name__} at {size[0]}x{size[1]} preset '{config.recording.preset}': "
        f"{rate:.1f} fps ({config.camera.fps} needed)"
    )
    if rate < config.camera.fps:
        report.add(
            "encoder",
            FAIL,
            detail,
            "the encoder cannot keep up and frames will be dropped. Lower camera.fps or "
            "camera.width/height, or keep recording.preset at 'ultrafast'",
        )
    elif rate < config.camera.fps / _RATE_TOLERANCE:
        report.add(
            "encoder",
            WARN,
            detail,
            "there is little headroom; a warm cabin or a background task could push it under",
        )
    else:
        report.add("encoder", OK, detail)


def _check_server(report: Report, config: EngineConfig) -> None:
    if not config.server.enabled:
        report.add("service", OK, "HTTP service disabled")
        return
    endpoint = f"http://{config.server.host}:{config.server.port}"
    if config.server.is_public and not config.server.token:
        report.add(
            "service",
            FAIL,
            f"{endpoint} is reachable from the network with no token",
            "set server.token, or bind server.host to 127.0.0.1. Without it, anyone on the "
            "network can download and delete your footage",
        )
        return
    scope = "network" if config.server.is_public else "loopback only"
    auth = "token required" if config.server.token else "no token"
    report.add("service", OK, f"{endpoint} ({scope}, {auth})")


def run_diagnostics(config: EngineConfig, *, probe_camera: bool = True) -> Report:
    """Run every check and return the report.

    Args:
        probe_camera: when ``False`` the camera, telemetry and encoder checks
            are skipped. Useful for validating a config on a machine with no
            hardware attached.
    """
    report = Report()
    _check_environment(report)
    _check_ffmpeg(report)
    _check_storage(report, config)
    _check_server(report, config)

    if not probe_camera:
        report.add("camera", OK, "skipped (--no-camera)")
        return report

    _check_devices(report)
    images = _check_camera(report, config)
    _check_telemetry(report, config, images)
    _check_encoder(report, config, images)
    return report
