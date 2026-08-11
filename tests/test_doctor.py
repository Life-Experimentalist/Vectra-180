"""Pre-flight diagnostics.

``vectra180 doctor`` exists to tell someone with a new camera whether it will
record. That makes a wrong answer worse than no answer, so these tests drive the
individual ``_check_*`` functions rather than only the assembled report -- every
OK, WARN and FAIL branch is exercised, including the ones a developer with
working hardware would never see.

Two things are substituted throughout. The hardware seams
(``enumerate_devices``, ``CameraSource``, ``ffmpeg_path``, ``create_writer``,
``storage_stats``, ``Engine``) are patched in the :mod:`vectra180.doctor`
namespace, and :mod:`time` is replaced with :class:`Clock`, whose readings
advance by a fixed step. Every rate measurement in doctor takes exactly two
readings, so the measured rates come out exact instead of depending on how busy
the machine running the suite happens to be.
"""

from __future__ import annotations

import itertools
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import numpy as np
import pytest

from tests.conftest import (
    FIRST_TIMESTAMP_US,
    FRAME_INTERVAL_US,
    FRAME_WIDTH,
    FakeCameraSource,
    encode_payload,
    make_frame,
)
from vectra180 import doctor as doctor_module
from vectra180 import engine as engine_module
from vectra180.capture import DeviceInfo
from vectra180.config import EngineConfig
from vectra180.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    Report,
    _check_camera,
    _check_devices,
    _check_encoder,
    _check_environment,
    _check_ffmpeg,
    _check_pipeline,
    _check_server,
    _check_storage,
    _check_telemetry,
    _sidecar_totals,
    run_diagnostics,
)
from vectra180.errors import CaptureError, RecorderError
from vectra180.recorder import segmenter as segmenter_module
from vectra180.recorder.storage import StorageStats

#: Frames the capture probe reads. Mirrors ``doctor._CAPTURE_SAMPLES``; the rate
#: arithmetic below is written out in full rather than derived from it, so a
#: change to either constant shows up as a failure rather than passing quietly.
SAMPLES = 30


class Clock:
    """Stands in for :mod:`time`, one fixed step per reading.

    ``_check_camera`` and ``_check_encoder`` each call :meth:`monotonic` twice,
    so ``step`` *is* the elapsed time they measure.
    """

    def __init__(self, step: float) -> None:
        self._step = step
        self._now = 0.0

    def monotonic(self) -> float:
        now = self._now
        self._now += self._step
        return now

    def sleep(self, seconds: float) -> None:
        """Pass the benchmark window without spending it.

        ``_check_pipeline`` records for five real seconds. A suite cannot afford
        that per test, so the wait is skipped and only the clock moves.
        """
        self._now += seconds


class BlindSource(FakeCameraSource):
    """A camera that opens and then hands back nothing."""

    def read(self) -> None:
        return None


class DeadSource(FakeCameraSource):
    """A camera that is not attached."""

    def open(self) -> None:
        raise CaptureError("could not open camera 0")


class BenchWriter:
    """Stands in for the encoder during the throughput benchmark."""

    def __init__(self, path: Path, size: tuple[int, int], *, fail_at: int | None = None) -> None:
        self.path = path
        self.size = size
        self.written = 0
        self.closed = 0
        self.frames: list[np.ndarray] = []
        self._fail_at = fail_at

    def write(self, frame: np.ndarray) -> None:
        if self._fail_at is not None and self.written >= self._fail_at:
            raise RecorderError("ffmpeg stopped accepting frames")
        self.frames.append(frame)
        self.written += 1

    def close(self) -> None:
        self.closed += 1


class PipelineRecorder:
    def __init__(self, dropped: int) -> None:
        self.stats = SimpleNamespace(dropped_frames=dropped)


class PipelineEngine:
    """Stands in for the engine the end-to-end benchmark builds for itself.

    Only the four calls the check makes are implemented, plus the record of
    which config it was handed -- the check is supposed to redirect the clips
    somewhere disposable rather than into the user's own recordings.

    Stopping writes the sidecar, because that is when the real engine finalises
    the open clip, and the sidecar is where the benchmark reads its answer.
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        written: int,
        covers: float,
        dropped: int,
        error: Exception | None,
    ) -> None:
        self.config = config
        self.recorder = PipelineRecorder(dropped)
        self.stopped = 0
        self.written = written
        self.covers = covers
        self._error = error

    def start(self) -> None:
        if self._error is not None:
            raise self._error

    def begin_recording(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped += 1
        if self._error is not None:
            return
        directory = self.config.recording.normal_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "VEC_20260101_000000.json").write_text(
            json.dumps({"frames": self.written, "covers_seconds": self.covers}), encoding="utf-8"
        )


def device(index: int = 0, name: str = "USB 3.0 Camera") -> DeviceInfo:
    return DeviceInfo(index=index, path="", name=name, width=2560, height=720, fps=30.0, backend="msmf")


def stats(free_bytes: int = 40 * 1024**3) -> StorageStats:
    return StorageStats(
        total_bytes=64 * 1024**3,
        free_bytes=free_bytes,
        normal_bytes=8 * 1024**3,
        event_bytes=1024**3,
        normal_clips=12,
        event_clips=2,
    )


def telemetry_frames(count: int = 2, *, accel_g: tuple[float, float, float] = (0.0, 0.0, 1.0)) -> list[np.ndarray]:
    """A run of frames whose sensor clock advances, as real hardware's does."""
    return [
        make_frame(encode_payload(timestamp_us=FIRST_TIMESTAMP_US + index * FRAME_INTERVAL_US, accel_g=accel_g))
        for index in range(count)
    ]


def named(report: Report, name: str) -> Check:
    """The single check called ``name``. Fails loudly if it is not there."""
    matches = [check for check in report.checks if check.name == name]
    assert len(matches) == 1, f"expected one {name!r} check, got {len(matches)}"
    return matches[0]


@pytest.fixture
def report() -> Report:
    return Report()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a clock whose step each test sets to the rate it wants."""

    def install(step: float) -> Clock:
        instance = Clock(step)
        monkeypatch.setattr(doctor_module, "time", instance)
        return instance

    return install


@pytest.fixture
def writers(monkeypatch: pytest.MonkeyPatch) -> list[BenchWriter]:
    """Replace the encoder factory; collect every writer the benchmark opens."""
    created: list[BenchWriter] = []

    def factory(path: Path, size: tuple[int, int], _fps: float, _config: Any) -> BenchWriter:
        writer = BenchWriter(path, size)
        created.append(writer)
        return writer

    monkeypatch.setattr(doctor_module, "create_writer", factory)
    return created


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the engine the pipeline benchmark builds; collect every one.

    The check drives a whole engine rather than a stage, so the substitution is
    at the ``Engine`` name doctor imported. Frame counts are handed back rather
    than produced, which is what makes the measured rate exact.
    """
    created: list[PipelineEngine] = []

    def install(
        *,
        written: int = 1000,
        covers: float = 5.0,
        dropped: int = 0,
        error: Exception | None = None,
    ) -> list[PipelineEngine]:
        def factory(config: EngineConfig) -> PipelineEngine:
            engine = PipelineEngine(config, written=written, covers=covers, dropped=dropped, error=error)
            created.append(engine)
            return engine

        monkeypatch.setattr(doctor_module, "Engine", factory)
        return created

    return install


@pytest.fixture
def storage(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Report a fixed volume, and make sure the probe has somewhere to write.

    The real ``storage_stats`` measures the developer's own disk, which decides
    on its own whether the free-space warning fires.
    """

    def install(free_bytes: int = 40 * 1024**3) -> None:
        config.recording.directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(doctor_module, "storage_stats", lambda _config: stats(free_bytes))

    return install


# --------------------------------------------------------------------------
# results and rendering
# --------------------------------------------------------------------------


def test_a_passing_check_renders_on_one_line() -> None:
    assert str(Check("camera", OK, "2560x720 at 30fps")) == "[ ok ] camera: 2560x720 at 30fps"


def test_a_check_with_a_remedy_renders_it_underneath() -> None:
    rendered = str(Check("storage", FAIL, "not writable", "chown the directory"))

    assert rendered.splitlines()[0] == "[FAIL] storage: not writable"
    assert rendered.splitlines()[1].strip() == "-> chown the directory"


def test_a_clean_report_says_everything_passed(report: Report) -> None:
    report.add("camera", OK, "fine")

    assert report.ok
    assert report.render().endswith("All checks passed.")


def test_a_warning_does_not_make_the_report_fail(report: Report) -> None:
    report.add("camera", OK, "fine")
    report.add("ffmpeg", WARN, "not on PATH")

    assert report.ok
    assert report.warned == 1
    assert "1 warning(s)" in report.render()


def test_a_failure_says_recording_is_not_reliable(report: Report) -> None:
    report.add("camera", FAIL, "no device")
    report.add("ffmpeg", WARN, "not on PATH")

    assert not report.ok
    assert report.failed == 1
    assert "Recording will not be reliable." in report.render()


def test_a_report_survives_json(report: Report) -> None:
    report.add("storage", WARN, "low", "free some space")

    restored = json.loads(json.dumps(report.as_dict()))

    assert restored["ok"] is True
    assert restored["warned"] == 1
    assert restored["checks"] == [{"name": "storage", "status": WARN, "detail": "low", "remedy": "free some space"}]


# --------------------------------------------------------------------------
# environment and devices
# --------------------------------------------------------------------------


def test_the_environment_check_names_the_libraries(report: Report) -> None:
    _check_environment(report)

    detail = named(report, "environment").detail
    assert "vectra180" in detail
    assert "opencv" in detail
    assert "numpy" in detail


def test_a_machine_with_no_capture_device_fails(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module, "enumerate_devices", list)

    _check_devices(report)

    check = named(report, "devices")
    assert check.status == FAIL
    assert "video" in check.remedy


def test_attached_devices_are_listed(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module, "enumerate_devices", lambda: [device(0), device(2, "Webcam")])

    _check_devices(report)

    check = named(report, "devices")
    assert check.status == OK
    assert "msmf[0] USB 3.0 Camera" in check.detail
    assert "msmf[2] Webcam" in check.detail


def test_the_listing_carries_each_devices_mode(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no device names to go by, so the frame size is the only way
    to tell a dual-fisheye module from a built-in webcam."""
    monkeypatch.setattr(doctor_module, "enumerate_devices", lambda: [device()])

    _check_devices(report)

    assert "2560x720" in named(report, "devices").detail


def test_a_device_held_by_another_program_is_called_out(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    """One camera streaming and one not is the normal shape of this: the
    fisheye is busy and the webcam beside it is idle and happy to answer."""
    monkeypatch.setattr(
        doctor_module,
        "enumerate_devices",
        lambda: [replace(device(), readable=False), device(1, "Webcam")],
    )

    _check_devices(report)

    check = named(report, "devices")
    assert check.status == WARN
    assert "no frames" in check.detail
    assert "another program" in check.remedy


def test_nothing_streaming_at_all_fails(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    """Devices that all open and none stream is not a wiring fault, and the
    remedy has to say so or the next hour goes on cables."""
    monkeypatch.setattr(doctor_module, "enumerate_devices", lambda: [replace(device(), readable=False)])

    _check_devices(report)

    check = named(report, "devices")
    assert check.status == FAIL
    assert "vectra180 run" in check.remedy


# --------------------------------------------------------------------------
# camera
# --------------------------------------------------------------------------


def test_a_camera_that_will_not_open_fails(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: DeadSource())

    assert _check_camera(report, config) == []

    check = named(report, "camera")
    assert check.status == FAIL
    assert "could not open camera 0" in check.detail
    assert "vectra180 devices" in check.remedy


def test_a_camera_that_returns_no_frames_fails(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    clock(1.0)
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: BlindSource())

    assert _check_camera(report, config) == []

    check = named(report, "camera")
    assert check.status == FAIL
    assert "another process may hold the camera" in check.remedy


def test_a_camera_at_the_configured_rate_passes(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    # Thirty frames in one second, which is exactly what the config asks for.
    clock(1.0)
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource())

    _check_camera(report, config)

    check = named(report, "camera")
    assert check.status == OK
    assert "30.0 fps measured (30 requested)" in check.detail


def test_a_camera_streaming_flat_black_warns(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    """Right size, right rate, no picture -- the failure the numbers cannot see."""
    clock(1.0)
    dark = np.zeros_like(make_frame())
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource([dark], telemetry=False))

    _check_camera(report, config)

    check = named(report, "camera")
    assert check.status == WARN
    assert "one flat colour" in check.detail
    assert "lens cap" in check.remedy


def test_a_camera_repeating_one_image_warns(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    """A hung camera keeps answering; it just stops seeing."""
    clock(1.0)
    monkeypatch.setattr(
        doctor_module, "CameraSource", lambda _config: FakeCameraSource([make_frame()], telemetry=False)
    )

    _check_camera(report, config)

    check = named(report, "camera")
    assert check.status == WARN
    assert "every frame is identical" in check.detail


def test_a_moving_picture_is_not_mistaken_for_a_hung_camera(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    """The check must not fire on a camera that is working."""
    clock(1.0)
    images = [make_frame(value=value) for value in (60, 90, 120)]
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource(images, telemetry=False))

    _check_camera(report, config)

    assert named(report, "camera").status == OK


def test_a_camera_below_the_configured_rate_warns(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    # Thirty frames in three seconds: 10fps against the 24fps tolerance floor.
    clock(3.0)
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource())

    _check_camera(report, config)

    check = named(report, "camera")
    assert check.status == WARN
    assert "10.0 fps measured" in check.detail
    assert "MJPG" in check.remedy


@pytest.mark.parametrize(
    ("settled", "expected"),
    [("YUY2", "settled on YUY2"), ("", "will not say which format")],
)
def test_the_rate_warning_names_the_format_the_driver_settled_on(
    report: Report,
    config: EngineConfig,
    monkeypatch: pytest.MonkeyPatch,
    clock: Any,
    settled: str,
    expected: str,
) -> None:
    """Asking for MJPG and being handed YUY2 is why the rate is low.

    The gap between request and answer is the whole diagnosis, so the remedy
    quotes the driver rather than repeating the request back -- and says so
    plainly when the backend will not name a format at all.
    """
    clock(3.0)

    class Substituting(FakeCameraSource):
        pixel_format = settled

    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: Substituting())

    _check_camera(report, config)

    remedy = named(report, "camera").remedy
    assert "asked for MJPG" in remedy
    assert expected in remedy


def test_a_probe_that_takes_no_time_does_not_divide_by_zero(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    clock(0.0)
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource())

    _check_camera(report, config)

    assert "0.0 fps measured" in named(report, "camera").detail


def test_a_driver_ignoring_the_requested_mode_warns(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    clock(1.0)
    config.camera.width = 2560
    config.camera.height = 720
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource())

    _check_camera(report, config)

    check = named(report, "camera")
    assert check.status == WARN
    assert "not the requested 2560x720" in check.detail
    # The remedy quotes the numbers the driver actually gave, so it can be
    # pasted into the config without a second trip to the terminal.
    assert "camera.width = 320" in check.remedy
    assert "camera.height = 64" in check.remedy
    assert "set both to 0" in check.remedy


def test_native_mode_never_reports_a_mismatch(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    """With the size left to the driver there is no requested mode to miss."""
    clock(1.0)
    config.camera.width = 0
    config.camera.height = 0
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource())

    _check_camera(report, config)

    check = named(report, "camera")
    assert check.status == OK
    assert "not the requested" not in check.detail


def test_the_camera_is_released_afterwards(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    clock(1.0)
    source = FakeCameraSource()
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: source)

    _check_camera(report, config)

    assert source.closed


def test_the_camera_check_keeps_a_run_of_frames(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    """More than one: a single strip can never confirm telemetry, and a single
    frame written repeatedly cannot measure an encoder."""
    clock(1.0)
    source = FakeCameraSource()
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: source)

    images = _check_camera(report, config)

    assert len(images) == doctor_module._KEPT_FRAMES
    assert source.read_count == SAMPLES
    # The last frames read, not the first: their strips carry later timestamps.
    assert not np.array_equal(images[0], images[1])


# --------------------------------------------------------------------------
# telemetry
# --------------------------------------------------------------------------


def test_telemetry_switched_off_is_not_a_problem(report: Report, config: EngineConfig) -> None:
    config.telemetry.enabled = False

    _check_telemetry(report, config, telemetry_frames())

    assert named(report, "telemetry").status == OK


def test_telemetry_without_a_frame_is_skipped(report: Report, config: EngineConfig) -> None:
    _check_telemetry(report, config, [])

    check = named(report, "telemetry")
    assert check.status == WARN
    assert "skipped" in check.detail


def test_a_camera_declared_to_have_no_strip_is_not_a_problem(report: Report, config: EngineConfig) -> None:
    """The old report warned here, and its remedy was the setting already made."""
    config.telemetry.metadata_width = 0

    _check_telemetry(report, config, telemetry_frames())

    check = named(report, "telemetry")
    assert check.status == OK
    assert check.remedy == ""


def test_a_strip_wider_than_the_frame_warns_rather_than_raising(report: Report, config: EngineConfig) -> None:
    config.telemetry.metadata_width = FRAME_WIDTH * 2

    _check_telemetry(report, config, telemetry_frames())

    check = named(report, "telemetry")
    assert check.status == WARN
    assert f"only {FRAME_WIDTH}px wide" in check.detail


def test_a_module_without_an_imu_warns(report: Report, config: EngineConfig) -> None:
    _check_telemetry(report, config, [make_frame(), make_frame()])

    check = named(report, "telemetry")
    assert check.status == WARN
    assert "telemetry.enabled = false" in check.remedy


def test_the_imu_remedy_says_that_switching_off_also_stops_the_cropping(report: Report, config: EngineConfig) -> None:
    """Some modules write a block this decoder cannot read.

    Switching telemetry off is the obvious response and the wrong one there:
    cropping is gated on the same switch, so it would leave the block burned
    into every recorded frame. The remedy has to say so.
    """
    _check_telemetry(report, config, [make_frame(), make_frame()])

    remedy = named(report, "telemetry").remedy
    assert "telemetry.metadata_width" in remedy
    assert "stops the cropping" in remedy


def test_one_frame_alone_cannot_confirm_telemetry(report: Report, config: EngineConfig) -> None:
    """Why the camera check hands back a run: the decoder needs corroboration."""
    _check_telemetry(report, config, telemetry_frames(1))

    assert named(report, "telemetry").status == WARN


def test_a_working_imu_is_reported(report: Report, config: EngineConfig) -> None:
    _check_telemetry(report, config, telemetry_frames())

    check = named(report, "telemetry")
    assert check.status == OK
    assert "IMU present: 1.00 g total" in check.detail


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------


def test_ffmpeg_on_the_path_passes(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

    _check_ffmpeg(report)

    check = named(report, "ffmpeg")
    assert check.status == OK
    assert check.detail == "/usr/bin/ffmpeg"


def test_ffmpeg_missing_warns_without_blocking_recording(report: Report, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: None)

    _check_ffmpeg(report)

    check = named(report, "ffmpeg")
    assert check.status == WARN
    assert "OpenCV writer" in check.detail


@pytest.mark.parametrize(
    ("plat", "expected"),
    [("win32", "winget"), ("darwin", "brew"), ("linux", "apt")],
)
def test_the_ffmpeg_hint_names_this_platform_s_installer(
    report: Report, monkeypatch: pytest.MonkeyPatch, plat: str, expected: str
) -> None:
    """Naming a package manager the reader does not have is worse than naming none."""
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: None)
    monkeypatch.setattr(doctor_module.sys, "platform", plat)

    _check_ffmpeg(report)

    assert expected in named(report, "ffmpeg").remedy


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def test_a_writable_volume_with_room_passes(report: Report, config: EngineConfig, storage: Any) -> None:
    storage()

    _check_storage(report, config)

    check = named(report, "storage")
    assert check.status == OK
    assert "40.0 GB free, 12 loop clip(s), 2 locked clip(s)" in check.detail


def test_a_volume_that_cannot_be_measured_fails(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_oserror(_config: Any) -> NoReturn:
        raise OSError("no such device")

    monkeypatch.setattr(doctor_module, "storage_stats", raise_oserror)

    _check_storage(report, config)

    check = named(report, "storage")
    assert check.status == FAIL
    assert "no such device" in check.detail


def test_a_read_only_volume_fails(
    report: Report, config: EngineConfig, storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage()

    def raise_oserror(self: Path, data: bytes) -> NoReturn:
        del self, data
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_bytes", raise_oserror)

    _check_storage(report, config)

    check = named(report, "storage")
    assert check.status == FAIL
    assert "not writable" in check.detail
    assert "chown" in check.remedy


def test_a_volume_below_the_reserve_warns(report: Report, config: EngineConfig, storage: Any) -> None:
    storage(free_bytes=config.recording.min_free_bytes // 2)

    _check_storage(report, config)

    check = named(report, "storage")
    assert check.status == WARN
    assert "reserve" in check.remedy


def test_the_write_probe_leaves_nothing_behind(report: Report, config: EngineConfig, storage: Any) -> None:
    storage()

    _check_storage(report, config)

    assert list(config.recording.directory.iterdir()) == []


# --------------------------------------------------------------------------
# encoder
# --------------------------------------------------------------------------


def test_the_encoder_check_is_skipped_without_a_frame(report: Report, config: EngineConfig) -> None:
    _check_encoder(report, config, [])

    check = named(report, "encoder")
    assert check.status == WARN
    assert "skipped" in check.detail


def test_an_encoder_that_will_not_start_fails(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_recorder_error(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise RecorderError("could not start ffmpeg")

    monkeypatch.setattr(doctor_module, "create_writer", raise_recorder_error)

    _check_encoder(report, config, [make_frame()])

    check = named(report, "encoder")
    assert check.status == FAIL
    assert "recording.encoder = 'opencv'" in check.remedy


def test_an_encoder_that_dies_mid_benchmark_fails(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    clock(1.0)
    dying = BenchWriter(Path("benchmark.mp4"), (320, 64), fail_at=5)
    monkeypatch.setattr(doctor_module, "create_writer", lambda *_args: dying)

    _check_encoder(report, config, [make_frame()])

    check = named(report, "encoder")
    assert check.status == FAIL
    assert "encoding failed" in check.detail
    # The writer is closed on the way out, so ffmpeg does not outlive the probe
    # still holding the benchmark file open.
    assert dying.closed == 1


def test_the_benchmark_workspace_is_removed(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any
) -> None:
    clock(1.0)

    _check_encoder(report, config, [make_frame()])

    assert not writers[0].path.parent.exists()


def test_an_encoder_with_headroom_passes(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any
) -> None:
    # Thirty frames in half a second: 60fps against the 30fps the camera needs.
    clock(0.5)

    _check_encoder(report, config, [make_frame()])

    check = named(report, "encoder")
    assert check.status == OK
    assert "60.0 fps (30 needed)" in check.detail
    assert writers[0].written == 30


def test_an_encoder_with_no_margin_warns(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any
) -> None:
    # Exactly 30fps: fast enough today, with nothing left for a warm cabin.
    clock(1.0)
    del writers

    _check_encoder(report, config, [make_frame()])

    check = named(report, "encoder")
    assert check.status == WARN
    assert "little headroom" in check.remedy


def test_an_encoder_that_cannot_keep_up_fails(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Thirty frames in two seconds: half the rate the camera produces.
    clock(2.0)
    del writers
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

    _check_encoder(report, config, [make_frame()])

    check = named(report, "encoder")
    assert check.status == FAIL
    assert "frames will be dropped" in check.remedy
    assert "Install ffmpeg" not in check.remedy


def test_a_slow_encoder_is_given_a_scale_to_try(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Lower something" is not advice anyone can act on.

    Encoder cost tracks pixel count, so the measured rate says how far the
    scale has to come down: half the rate means half the pixels, which is
    about 0.71 of the scale, less a tenth for headroom.
    """
    clock(2.0)
    del writers
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

    _check_encoder(report, config, [make_frame()])

    remedy = named(report, "encoder").remedy
    assert "recording.scale to about 0.64" in remedy
    # Already at the fastest preset, so suggesting it would be noise.
    assert "recording.preset" not in remedy


def test_the_suggested_scale_builds_on_the_one_already_set(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The benchmark ran at the configured scale, so the factor multiplies it."""
    clock(2.0)
    del writers
    config.recording.scale = 0.5
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

    _check_encoder(report, config, [make_frame()])

    assert "recording.scale to about 0.32" in named(report, "encoder").remedy


def test_a_slow_encoder_on_a_slow_preset_is_told_to_change_it(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock(2.0)
    del writers
    config.recording.preset = "medium"
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

    _check_encoder(report, config, [make_frame()])

    assert "recording.preset to 'ultrafast'" in named(report, "encoder").remedy


def test_a_slow_encoder_without_ffmpeg_is_told_to_install_it(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OpenCV writer has no bitrate control, so it is usually the reason."""
    clock(2.0)
    del writers
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: None)

    _check_encoder(report, config, [make_frame()])

    assert "Install ffmpeg" in named(report, "encoder").remedy


def test_the_benchmark_uses_the_size_the_recorder_would_write(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any
) -> None:
    """The strip goes first, then the odd column: yuv420p refuses odd sizes."""
    clock(0.5)
    config.telemetry.metadata_width = 8
    odd = np.zeros((65, 321, 3), dtype=np.uint8)

    _check_encoder(report, config, [odd])

    assert writers[0].size == (312, 64)


def test_the_newest_frame_sets_the_benchmark_size(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any
) -> None:
    clock(0.5)
    config.telemetry.enabled = False
    older = np.zeros((64, 320, 3), dtype=np.uint8)
    newer = np.zeros((48, 240, 3), dtype=np.uint8)

    _check_encoder(report, config, [older, newer])

    assert writers[0].size == (240, 48)


def test_the_benchmark_follows_the_recording_scale(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any
) -> None:
    """A quarter of the pixels is a different measurement, not a faster one."""
    clock(0.5)
    config.telemetry.enabled = False
    config.recording.scale = 0.5

    _check_encoder(report, config, [np.zeros((64, 320, 3), dtype=np.uint8)])

    assert writers[0].size == (160, 32)


def test_the_benchmark_cycles_distinct_frames(
    report: Report, config: EngineConfig, writers: list[BenchWriter], clock: Any
) -> None:
    """The same frame written thirty times costs an encoder almost nothing --
    every one after the first is an empty residual -- so the benchmark would
    report a rate the camera will never see."""
    clock(0.5)
    config.telemetry.enabled = False
    frames = [np.full((64, 320, 3), fill, dtype=np.uint8) for fill in (10, 120, 230)]

    _check_encoder(report, config, frames)

    written = writers[0].frames
    assert len(written) == 30
    assert all(not np.array_equal(a, b) for a, b in itertools.pairwise(written))


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------
#
# The rate comes out of the sidecar rather than off a stopwatch, so each test
# hands back a frame count and the span those frames cover; ``covers`` defaults
# to five seconds, which makes ``written`` divide by exactly five.


def test_the_pipeline_check_measures_capture_and_encode_together(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    """Two fast stages can still add up to a slow recorder; this is the total."""
    clock(0.0)
    engines(written=150)

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == OK
    assert check.detail == "30.0 fps captured, prepared and encoded together (30 requested)"


def test_a_damaged_sidecar_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    """A truncated sidecar is what a power cut mid-write leaves behind.

    The benchmark reads whatever sidecars it finds, and one unreadable file must
    not turn a diagnostic into a traceback -- least of all on the machine whose
    power the operator is already suspicious of.
    """
    (tmp_path / "good.json").write_text(json.dumps({"frames": 60, "covers_seconds": 2.0}), encoding="utf-8")
    (tmp_path / "truncated.json").write_text('{"frames": 60, "covers_se', encoding="utf-8")

    assert _sidecar_totals(tmp_path) == (60, 2.0)


def test_sidecar_totals_add_up_across_a_rollover(tmp_path: Path) -> None:
    """A benchmark long enough to roll a segment still measures one rate."""
    (tmp_path / "a.json").write_text(json.dumps({"frames": 60, "covers_seconds": 2.0}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"frames": 30, "covers_seconds": 1.0}), encoding="utf-8")

    assert _sidecar_totals(tmp_path) == (90, 3.0)


def test_the_rate_is_measured_against_the_frames_not_the_stopwatch(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    """The queue between capture and encode must not be able to skew the answer.

    Two seconds of frames sit in that queue, so a count divided by the length of
    the sleep is short while the queue fills and long once the shutdown drain
    flushes it into a window that has already closed -- on a five-second
    benchmark, wrong by a third either way. Here the clip covers twice the
    window: if the sleep were the denominator the check would read 30 fps and
    pass, and it must read 15 and warn instead.
    """
    clock(0.0)
    engines(written=150, covers=doctor_module._PIPELINE_SECONDS * 2)

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == WARN
    assert "15.0 fps" in check.detail


def test_a_pipeline_that_records_nothing_fails(report: Report, config: EngineConfig, engines: Any, clock: Any) -> None:
    """An engine that starts, runs and writes no clip is a failure, not 0 fps.

    Dividing by a span of zero would be the other way to answer this, and
    reporting '0.0 fps, set camera.fps to about 0' would be useless advice.
    """
    clock(0.0)
    engines(written=0, covers=0.0)

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == FAIL
    assert "no frames reached the card" in check.detail
    assert "failed silently" in check.remedy


def test_a_pipeline_slower_than_the_camera_warns(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    """Twenty of the thirty frames a second: recording, but not at the rate asked."""
    clock(0.0)
    engines(written=100)

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == WARN
    assert "20.0 fps" in check.detail
    assert "play faster than real time" in check.remedy
    # recording.fps, not camera.fps: the latter is only a request, and a driver
    # that reports the mode it opened in puts its own figure in the header
    # whatever was asked for -- so naming it here would be advice that does not
    # work on the very hardware that provokes the warning.
    assert "recording.fps to about 20" in check.remedy


def test_a_declared_rate_the_machine_sustains_is_not_a_failure(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    """Doing what the warning above says has to clear the warning.

    ``recording.fps`` is what the clip header carries, so a machine that
    sustains the rate it declares is recording real time -- there is nothing
    left for this check to report. Measured against ``camera.fps`` instead, the
    operator who took the advice would be told the same thing forever, and on a
    CM5 feeding a 4000x1200 module that configuration is the right one rather
    than a compromise to be nagged about.

    The camera's own request is still named, because the module is offering
    more than this machine is taking from it and that is worth knowing.
    """
    config.recording.fps = 20.0
    clock(0.0)
    engines(written=100)

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == OK
    assert check.detail == "20.0 fps captured, prepared and encoded together (20 requested), from a camera asked for 30"


def test_a_declared_rate_the_machine_still_misses_warns(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    """Declaring a rate is not the same as reaching it.

    Clips still play faster than the road went by, so the advice is to declare
    a lower one -- and the figure offered is the rate that was measured, not
    the one that was already tried and missed.
    """
    config.recording.fps = 20.0
    clock(0.0)
    engines(written=75)

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == WARN
    assert "15.0 fps" in check.detail
    assert "recording.fps to about 15" in check.remedy


def test_a_pipeline_at_half_the_rate_fails(report: Report, config: EngineConfig, engines: Any, clock: Any) -> None:
    clock(0.0)
    engines(written=45)

    _check_pipeline(report, config)

    assert named(report, "pipeline").status == FAIL


def test_dropped_frames_are_named_in_the_pipeline_result(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    """A recorder shedding frames under load is the number that explains the rate."""
    clock(0.0)
    engines(written=150, dropped=7)

    _check_pipeline(report, config)

    assert "7 frame(s) dropped" in named(report, "pipeline").detail


def test_the_pipeline_check_is_skipped_when_recording_is_off(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    clock(0.0)
    built = engines()
    config.recording.enabled = False

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == OK
    assert "skipped" in check.detail
    assert built == []


def test_a_pipeline_that_cannot_open_the_camera_fails(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    clock(0.0)
    built = engines(error=CaptureError("could not open camera 0"))

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status == FAIL
    assert "could not open camera 0" in check.detail
    # Still shut down: a half-started engine holds the camera open otherwise,
    # and every check after this one would then find the device busy.
    assert built[0].stopped == 1


def test_the_pipeline_benchmark_writes_nowhere_near_the_real_clips(
    report: Report, config: EngineConfig, engines: Any, clock: Any
) -> None:
    """Five seconds of footage is not something to leave in someone's recordings."""
    clock(0.0)
    built = engines(written=150)
    real_clips = config.recording.directory

    _check_pipeline(report, config)

    probe = built[0].config
    assert probe.recording.directory != real_clips
    # Torn down afterwards, so a benchmark run leaves no footage anywhere.
    assert not probe.recording.directory.exists()
    # And the preview server stays down, or a viewer would be measured as part
    # of the pipeline and make the machine look slower than it is.
    assert not probe.server.enabled
    # The redirection happened on a copy: the caller's config is unchanged.
    assert config.recording.directory == real_clips
    assert built[0].stopped == 1


def test_the_pipeline_benchmark_measures_a_real_engine(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other pipeline tests substitute the engine; this one drives it.

    Nothing here is timed to a threshold -- a suite running on a loaded CI box
    would fail that at random. What it proves is that the check can start the
    real capture-prepare-encode chain, get frames through it, and stop cleanly.
    """
    opened: list[BenchWriter] = []

    def factory(path: Path, size: tuple[int, int], _fps: float, _config: Any) -> BenchWriter:
        writer = BenchWriter(path, size)
        opened.append(writer)
        return writer

    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: FakeCameraSource())
    monkeypatch.setattr(segmenter_module, "create_writer", factory)
    monkeypatch.setattr(doctor_module, "_PIPELINE_SECONDS", 0.5)

    _check_pipeline(report, config)

    check = named(report, "pipeline")
    assert check.status in {OK, WARN, FAIL}
    assert "captured, prepared and encoded together" in check.detail
    assert opened and opened[0].written > 0


def test_the_pipeline_count_waits_for_the_queue_to_drain(
    report: Report, config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frames still queued when the window closes are neither written nor dropped.

    Counting them mid-flight would understate the rate by up to the depth of the
    queue -- two seconds of it -- which on a five-second benchmark is a third of
    the answer. Reported as a warning, that is doctor telling someone to lower
    their scale over frames that were about to be encoded anyway.

    Here the encoder is held back until the engine is stopped, so every frame it
    writes is one that only the drain produced. If the count were taken before
    the shutdown, the measured rate would be zero.
    """
    released = threading.Event()

    class SlowWriter(BenchWriter):
        def write(self, frame: np.ndarray) -> None:
            released.wait(timeout=5.0)
            super().write(frame)

    opened: list[SlowWriter] = []

    def factory(path: Path, size: tuple[int, int], _fps: float, _config: Any) -> SlowWriter:
        writer = SlowWriter(path, size)
        opened.append(writer)
        return writer

    def stop_then_release(self: Any, *args: Any, **kwargs: Any) -> None:
        released.set()
        real_stop(self, *args, **kwargs)

    real_stop = engine_module.Engine.stop
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: FakeCameraSource())
    monkeypatch.setattr(segmenter_module, "create_writer", factory)
    monkeypatch.setattr(engine_module.Engine, "stop", stop_then_release)
    monkeypatch.setattr(doctor_module, "_PIPELINE_SECONDS", 0.3)

    _check_pipeline(report, config)

    assert opened[0].written > 0
    assert not named(report, "pipeline").detail.startswith("0.0 fps")


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


def test_a_disabled_service_is_reported(report: Report, config: EngineConfig) -> None:
    config.server.enabled = False

    _check_server(report, config)

    check = named(report, "service")
    assert check.status == OK
    assert "disabled" in check.detail


def test_a_loopback_service_without_a_token_passes(report: Report, config: EngineConfig) -> None:
    config.server.enabled = True
    config.server.host = "127.0.0.1"

    _check_server(report, config)

    check = named(report, "service")
    assert check.status == OK
    assert "loopback only, no token" in check.detail


def test_a_public_service_without_a_token_fails(report: Report, config: EngineConfig) -> None:
    config.server.enabled = True
    # Binding the wildcard address with no token is the misconfiguration here.
    config.server.host = "0.0.0.0"
    config.server.token = ""

    _check_server(report, config)

    check = named(report, "service")
    assert check.status == FAIL
    assert "download and delete your footage" in check.remedy


def test_a_public_service_with_a_token_passes(report: Report, config: EngineConfig) -> None:
    config.server.enabled = True
    config.server.host = "0.0.0.0"
    config.server.token = "s3cret"

    _check_server(report, config)

    check = named(report, "service")
    assert check.status == OK
    assert "network, token required" in check.detail


# --------------------------------------------------------------------------
# the assembled report
# --------------------------------------------------------------------------


@pytest.fixture
def working_hardware(
    monkeypatch: pytest.MonkeyPatch, storage: Any, writers: list[BenchWriter], clock: Any, engines: Any
) -> list[BenchWriter]:
    """Everything attached and fast enough."""
    storage()
    clock(0.5)
    engines(written=1000)
    monkeypatch.setattr(doctor_module, "enumerate_devices", lambda: [device()])
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: FakeCameraSource())
    return writers


def test_diagnostics_run_every_check(config: EngineConfig, working_hardware: list[BenchWriter]) -> None:
    del working_hardware

    result = run_diagnostics(config)

    assert [check.name for check in result.checks] == [
        "environment",
        "ffmpeg",
        "storage",
        "service",
        "devices",
        "camera",
        "telemetry",
        "encoder",
        "pipeline",
    ]
    assert result.ok


def test_diagnostics_on_a_healthy_rig_report_the_imu(config: EngineConfig, working_hardware: list[BenchWriter]) -> None:
    del working_hardware

    result = run_diagnostics(config)

    assert named(result, "telemetry").status == OK


def test_diagnostics_can_skip_the_hardware(config: EngineConfig, storage: Any) -> None:
    storage()

    result = run_diagnostics(config, probe_camera=False)

    assert [check.name for check in result.checks] == ["environment", "ffmpeg", "storage", "service", "camera"]
    assert named(result, "camera").detail == "skipped (--no-camera)"


def test_a_misconfigured_strip_width_does_not_cancel_the_report(
    config: EngineConfig, working_hardware: list[BenchWriter]
) -> None:
    """One bad setting used to raise out of the telemetry check and lose the rest."""
    del working_hardware
    config.telemetry.metadata_width = FRAME_WIDTH

    result = run_diagnostics(config)

    assert named(result, "telemetry").status == WARN
    assert named(result, "encoder").status == OK
    assert named(result, "storage").status == OK


def test_a_report_from_a_bare_machine_fails_loudly(
    config: EngineConfig, storage: Any, monkeypatch: pytest.MonkeyPatch, clock: Any, engines: Any
) -> None:
    storage()
    clock(1.0)
    engines(error=CaptureError("could not open camera 0"))
    monkeypatch.setattr(doctor_module, "enumerate_devices", list)
    monkeypatch.setattr(doctor_module, "ffmpeg_path", lambda: None)
    monkeypatch.setattr(doctor_module, "CameraSource", lambda _config: DeadSource())

    result = run_diagnostics(config)

    assert not result.ok
    assert named(result, "devices").status == FAIL
    assert named(result, "camera").status == FAIL
    # Nothing to inspect, but the report still says so rather than crashing.
    assert named(result, "telemetry").status == WARN
    assert named(result, "encoder").status == WARN
    assert "Recording will not be reliable." in result.render()
