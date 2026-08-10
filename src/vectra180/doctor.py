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

import copy
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
from vectra180.engine import Engine
from vectra180.errors import CaptureError, RecorderError
from vectra180.imaging import crop_to_even, downscale, strip_metadata
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

#: Frames kept back from the capture probe for the later checks.
#:
#: Two would satisfy the telemetry decoder, which accepts a sample only once a
#: second frame continues its timeline. The encoder benchmark needs more: video
#: codecs spend their time on what changed since the previous frame, so timing
#: one frame written repeatedly measures an empty residual and reports a rate
#: the camera will never see. Six distinct frames land within a few percent of
#: thirty, and cost six frames of memory rather than thirty.
_KEPT_FRAMES = 6

#: Measured rate below this fraction of the configured rate is reported as a
#: problem rather than noise.
_RATE_TOLERANCE = 0.8

#: Seconds the end-to-end check records for. Long enough to get past the first
#: keyframe and settle, short enough that `doctor` still feels like a check.
_PIPELINE_SECONDS = 5.0


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

    # The mode is printed alongside each entry because on Windows there are no
    # device names to go by, and a dual-fisheye module's frame is unmistakable
    # next to a webcam's: it is the wide one.
    listing = "; ".join(
        f"{device.label} {device.width}x{device.height}" + ("" if device.readable else " -- no frames")
        for device in devices
    )
    if not any(device.readable for device in devices):
        report.add(
            "devices",
            FAIL,
            listing,
            "every device opened but none streamed -- another program is almost certainly holding the "
            "camera. Close it (including any other 'vectra180 run') and try again",
        )
    elif any(not device.readable for device in devices):
        report.add(
            "devices",
            WARN,
            listing,
            "the entries marked 'no frames' opened but streamed nothing, which usually means another "
            "program is holding them",
        )
    else:
        report.add("devices", OK, listing)


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
            "run 'vectra180 devices' to see what is attached, then set camera.index, camera.backend or "
            "camera.device in the config -- the same index names different hardware on different backends",
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

        requested = (config.camera.width, config.camera.height)
        native = requested == (0, 0)

        # A camera can open, answer at the right size and hit the right rate
        # while recording nothing at all: a sensor that never exposes streams
        # flat black, and a hung one repeats its last good frame forever.
        # Neither shows up in the numbers above, so a dashcam left like this
        # fills its disk with footage of nothing and nobody finds out until
        # they need the clip. A real sensor always carries some noise, so a
        # frame with a single value in it is broken rather than merely dark.
        frames = list(recent)
        blank = all(int(image.max()) == int(image.min()) for image in frames)
        frozen = len(frames) > 1 and all(np.array_equal(frames[0], image) for image in frames[1:])

        if blank:
            report.add(
                "camera",
                WARN,
                f"{detail}; every frame is one flat colour",
                "the lens cap is on, the sensor is not exposing, or another program is muting the "
                "stream -- look at the picture with 'vectra180 view' before trusting a recording",
            )
        elif frozen:
            report.add(
                "camera",
                WARN,
                f"{detail}; every frame is identical",
                "the camera is repeating one image rather than streaming. Reconnect it, and give it a "
                "port of its own if it is sharing a hub",
            )
        elif not native and (width, height) != requested:
            report.add(
                "camera",
                WARN,
                f"{detail}; driver gave {width}x{height}, not the requested "
                f"{config.camera.width}x{config.camera.height}",
                # Advising the width be lowered to match is only right once
                # this is known to be the intended camera. A built-in webcam
                # answering in place of the fisheye looks exactly like a mode
                # substitution, and taking the advice would pin the mistake in.
                f"check 'vectra180 devices' that {source.backend}[{config.camera.index}] is the camera you "
                f"mean -- a built-in webcam looks like this. If it is right, set camera.width = {width} "
                f"and camera.height = {height} to match it, or set both to 0 to accept its native mode",
            )
        elif measured < config.camera.fps * _RATE_TOLERANCE:
            # A raw format cannot carry a full-size stereo frame at 30 fps, so
            # the pixel format is the first thing to look at. Which way to move
            # it is not obvious: some modules ignore the request and stay in
            # the slow mode, and a few reach their full rate only when nothing
            # is asked for at all. The driver's own answer is quoted rather
            # than guessed at, because that gap is the whole diagnosis.
            settled = (
                f"the driver settled on {source.pixel_format}"
                if source.pixel_format
                else "the driver will not say which format it settled on"
            )
            report.add(
                "camera",
                WARN,
                detail,
                f"the USB link or the pixel format is the bottleneck: camera.fourcc asked for "
                f"{config.camera.fourcc or 'nothing'} and {settled}. MJPG is what most modules need to "
                f'carry a full-size stereo frame, but some reach their full rate only with fourcc = "" '
                f"-- try both, and use a USB 3 port",
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
            # Turning telemetry off is the obvious move and the wrong one on a
            # module that writes a block this decoder cannot read: cropping is
            # gated on the same switch, so disabling it leaves the block burned
            # into every recorded frame. Look at the picture before choosing.
            "not every dual-fisheye module embeds this IMU block, and some write a different "
            "one into the same corner. Look at the leading columns with 'vectra180 view': if "
            "they carry a machine-written block, set telemetry.metadata_width to its real width "
            "so it is cropped out of the recording. Set telemetry.enabled = false only if they "
            "are ordinary picture, since that also stops the cropping",
        )
        return

    report.add(
        "telemetry",
        OK,
        f"IMU present: {sample.accel_magnitude_g:.2f} g total, "
        f"gyro {sample.gyro_x:+.2f}/{sample.gyro_y:+.2f}/{sample.gyro_z:+.2f} rad/s",
    )


# Naming the wrong package manager is worse than naming none: it sends the
# reader off to a command that does not exist on their machine.
_FFMPEG_INSTALL = {
    "win32": "winget install Gyan.FFmpeg, then open a new terminal so PATH is picked up",
    "darwin": "brew install ffmpeg",
}


def _check_ffmpeg(report: Report) -> None:
    path = ffmpeg_path()
    if path is None:
        how = _FFMPEG_INSTALL.get(sys.platform, "sudo apt install ffmpeg")
        report.add(
            "ffmpeg",
            WARN,
            "not on PATH -- recording will fall back to the OpenCV writer",
            f"install it ({how}) for bitrate control and reliable container finalisation",
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
    """Time the encoder on the pixels the recorder would actually write."""
    if not images:
        report.add("encoder", WARN, "skipped -- no frame to encode")
        return

    # The metadata strip is removed and any downscale applied before encoding.
    # A benchmark run at the camera's raw size measures work the recorder will
    # never do, and at scale = 0.5 it would be four times too much of it.
    strip = config.telemetry.metadata_width if config.telemetry.enabled else 0
    if not 0 < strip < images[-1].shape[1]:
        strip = 0
    # Cycled rather than repeated: consecutive frames must differ for the
    # encoder to do the inter-frame work it will do on the road.
    frames = [
        np.ascontiguousarray(crop_to_even(downscale(image[:, strip:], config.recording.scale))) for image in images
    ]
    height, width = frames[-1].shape[:2]
    size = (width, height)

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
        for index in range(_ENCODE_SAMPLES):
            writer.write(frames[index % len(frames)])
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
        # Ordered by what wins most: on a machine without ffmpeg the OpenCV
        # writer has no bitrate control and spends the difference on the disk,
        # so installing it is worth more than any setting below it.
        #
        # recording.scale then comes before camera.width/height because it
        # keeps the camera in its native mode. Asking the camera for a smaller
        # picture instead lands on a cropped sensor mode on many modules,
        # narrowing the very field of view a dashcam is there for -- and on a
        # module with only one mode it does nothing at all.
        #
        # Encoder cost tracks pixel count, which is the square of the scale, so
        # the suggestion is a starting point rather than a promise; the tenth
        # off is headroom for a warm cabin. The benchmark ran at the current
        # scale, so it multiplies rather than replaces it.
        suggested = max(0.1, round(config.recording.scale * (rate / config.camera.fps) ** 0.5 * 0.9, 2))
        remedy = "the encoder cannot keep up and frames will be dropped. "
        remedy += "Install ffmpeg, then set " if ffmpeg_path() is None else "Set "
        remedy += f"recording.scale to about {suggested} to encode fewer pixels from the same field of view"
        if config.recording.preset != "ultrafast":
            remedy += ", and recording.preset to 'ultrafast'"
        remedy += ", or lower camera.fps"
        report.add("encoder", FAIL, detail, remedy)
    elif rate < config.camera.fps / _RATE_TOLERANCE:
        report.add(
            "encoder",
            WARN,
            detail,
            "there is little headroom; a warm cabin or a background task could push it under",
        )
    else:
        report.add("encoder", OK, detail)


def _check_pipeline(report: Report, config: EngineConfig) -> None:
    """Measure capture, prepare and encode running at the same time.

    The camera and encoder checks each time their own stage with nothing else
    running, and two comfortable numbers do not add up to a comfortable
    pipeline: the stages share cores, memory bandwidth and one interpreter
    lock. A module measured at 32 fps feeding an encoder measured at 150 has
    been seen to record at 19. This is the figure that decides what reaches the
    card, so it is measured rather than inferred from the other two.
    """
    if not config.recording.enabled:
        report.add("pipeline", OK, "skipped -- recording is disabled")
        return

    probe = copy.deepcopy(config)
    workspace = Path(tempfile.mkdtemp(prefix="vectra-pipeline-"))
    probe.recording.directory = workspace
    # Nothing may watch the preview during the benchmark: a viewer would be
    # measured as part of the pipeline and make it look slower than it is.
    probe.server.enabled = False
    engine = Engine(probe)
    try:
        engine.start()
        engine.begin_recording()
        started = time.monotonic()
        before = engine.recorder.stats.written_frames
        time.sleep(_PIPELINE_SECONDS)
        elapsed = time.monotonic() - started
    except (CaptureError, RecorderError) as exc:
        report.add("pipeline", FAIL, f"end-to-end recording failed: {exc}")
        return
    finally:
        # Stopping drains the queue, which is why the counters are read after
        # it rather than before. Up to two seconds of frames sit in that queue
        # at any moment -- neither written nor dropped -- and on a five-second
        # window that is a third of them.
        engine.stop()
        shutil.rmtree(workspace, ignore_errors=True)

    written = engine.recorder.stats.written_frames - before
    dropped = engine.recorder.stats.dropped_frames
    rate = written / elapsed if elapsed > 0 else 0.0
    detail = f"{rate:.1f} fps captured, prepared and encoded together ({config.camera.fps} requested)"
    if dropped:
        detail += f", {dropped} frame(s) dropped"

    if rate >= config.camera.fps * _RATE_TOLERANCE:
        report.add("pipeline", OK, detail)
        return

    # Falling short is not just lost detail. The clip declares the requested
    # rate in its header, so footage arriving slower than that plays faster
    # than the road went by -- which is why the sidecar marks these clips
    # discontinuous. Matching camera.fps to what the machine sustains fixes the
    # playback speed; lowering the scale is what actually buys the rate back.
    remedy = (
        f"the whole pipeline is slower than the camera alone, so clips play faster than real time "
        f"and their sidecars are marked discontinuous. Lower recording.scale to encode fewer pixels, "
        f"or set camera.fps to about {max(1, int(rate))} so the header matches what is recorded"
    )
    report.add("pipeline", WARN if rate >= config.camera.fps * 0.5 else FAIL, detail, remedy)


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
    _check_pipeline(report, config)
    return report
