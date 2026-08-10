"""Pre-flight diagnostics.

``vectra180 doctor`` exists to tell someone with a new camera whether it will
record. That makes a wrong answer worse than no answer, so these tests drive the
individual ``_check_*`` functions rather than only the assembled report -- every
OK, WARN and FAIL branch is exercised, including the ones a developer with
working hardware would never see.

Two things are substituted throughout. The hardware seams
(``enumerate_devices``, ``CameraSource``, ``ffmpeg_path``, ``create_writer``,
``storage_stats``) are patched in the :mod:`vectra180.doctor` namespace, and
:mod:`time` is replaced with :class:`Clock`, whose readings advance by a fixed
step. Both rate measurements in doctor take exactly two readings, so the
measured capture and encode rates come out exact instead of depending on how
busy the machine running the suite happens to be.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path
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
    _check_server,
    _check_storage,
    _check_telemetry,
    run_diagnostics,
)
from vectra180.errors import CaptureError, RecorderError
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
    monkeypatch: pytest.MonkeyPatch, storage: Any, writers: list[BenchWriter], clock: Any
) -> list[BenchWriter]:
    """Everything attached and fast enough."""
    storage()
    clock(0.5)
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
    config: EngineConfig, storage: Any, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    storage()
    clock(1.0)
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
