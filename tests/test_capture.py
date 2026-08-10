"""Backend selection and the camera lifecycle.

No camera is attached, so ``cv2.VideoCapture`` is replaced with a driver that
can be told to refuse to open, to open but stream nothing, or to drop out
mid-session -- which is what a USB device in a car actually does over a
pothole. The code under test is the real thing; only the driver is fake.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from vectra180.capture import CameraSource, Frame
from vectra180.capture import backends as backends_module
from vectra180.capture.backends import (
    DeviceInfo,
    backend_name,
    enumerate_devices,
    preferred_backends,
    resolve_backend,
)
from vectra180.config import CameraConfig
from vectra180.errors import CaptureError

IMAGE = np.zeros((8, 16, 3), dtype=np.uint8)


# -- fake driver -------------------------------------------------------------


@dataclass
class Device:
    """How one fake capture target behaves."""

    #: Backends that will open it. ``None`` means every backend.
    backends: set[int] | None = None
    #: Reads that fail before the device starts streaming. ``None`` never streams.
    failures: int | None = 0
    fps: float = 30.0
    width: float = 2560.0
    height: float = 720.0

    def opens_on(self, backend: int) -> bool:
        return self.backends is None or backend in self.backends


class FakeCapture:
    """Stands in for one ``cv2.VideoCapture`` object."""

    def __init__(self, driver: FakeDriver, target: int | str, backend: int) -> None:
        self.driver = driver
        self.target = target
        self.backend = backend
        self.device = driver.devices.get(target)
        self.settings: list[tuple[int, float]] = []
        self.released = False
        self.reads = 0

    def isOpened(self) -> bool:  # noqa: N802 - the name OpenCV exposes
        return self.device is not None and self.device.opens_on(self.backend) and not self.released

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.reads += 1
        device = self.device
        if device is None or device.failures is None:
            return False, None
        if device.failures > 0:
            device.failures -= 1
            return False, None
        return True, self.driver.image.copy()

    def set(self, prop: int, value: float) -> bool:
        self.settings.append((prop, value))
        return True

    def get(self, prop: int) -> float:
        if self.device is None:
            return 0.0
        return {
            cv2.CAP_PROP_FPS: self.device.fps,
            cv2.CAP_PROP_FRAME_WIDTH: self.device.width,
            cv2.CAP_PROP_FRAME_HEIGHT: self.device.height,
        }.get(prop, 0.0)

    def release(self) -> None:
        self.released = True


@dataclass
class FakeDriver:
    """The ``cv2.VideoCapture`` constructor, with a known set of devices."""

    devices: dict[Any, Device] = field(default_factory=dict)
    image: np.ndarray = field(default_factory=lambda: IMAGE.copy())
    captures: list[FakeCapture] = field(default_factory=list)

    def __call__(self, target: int | str, backend: int) -> FakeCapture:
        capture = FakeCapture(self, target, backend)
        self.captures.append(capture)
        return capture


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    """A single camera on index 0, opening on any backend."""
    fake = FakeDriver(devices={0: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    return fake


@pytest.fixture
def camera() -> CameraConfig:
    return CameraConfig(width=16, height=8, fps=30, reconnect_delay=0.0, read_failure_limit=3)


# -- backend selection -------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("linux", ["v4l2", "any"]),
        ("linux2", ["v4l2", "any"]),
        ("win32", ["msmf", "dshow", "any"]),
        ("darwin", ["avfoundation", "any"]),
        ("freebsd14", ["any"]),
    ],
)
def test_the_backend_order_follows_the_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: list[str]
) -> None:
    """Hard-coding DirectShow is what made the original engine Windows-only."""
    monkeypatch.setattr(sys, "platform", platform)

    assert [backend_name(value) for value in preferred_backends()] == expected


@pytest.mark.parametrize("name", ["auto", "", "  AUTO  "])
def test_auto_expands_to_the_platform_order(name: str) -> None:
    assert resolve_backend(name) == preferred_backends()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("v4l2", cv2.CAP_V4L2),
        ("V4L2", cv2.CAP_V4L2),
        (" dshow ", cv2.CAP_DSHOW),
        ("gstreamer", cv2.CAP_GSTREAMER),
        ("msmf", cv2.CAP_MSMF),
        ("avfoundation", cv2.CAP_AVFOUNDATION),
        ("any", cv2.CAP_ANY),
    ],
)
def test_an_explicit_backend_pins_exactly_one(name: str, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forcing a driver is the escape hatch when auto-detection picks badly.

    Which backends a given OpenCV build carries varies by wheel and platform,
    so availability is stubbed: this is about parsing the name.
    """
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: True)

    assert resolve_backend(name) == [expected]


def test_an_unknown_backend_names_the_valid_ones() -> None:
    with pytest.raises(ValueError, match="unknown capture backend 'directshow'") as error:
        resolve_backend("directshow")

    assert "v4l2" in str(error.value)


def test_a_backend_missing_from_the_build_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming a backend OpenCV was not compiled with is a config error.

    Left to itself this surfaces as "device did not open" against working
    hardware. It matters most for ``gstreamer``, which a CSI camera needs and
    which the PyPI wheels omit.
    """
    monkeypatch.setattr(backends_module, "has_backend", lambda backend: backend != cv2.CAP_GSTREAMER)

    with pytest.raises(ValueError, match="no gstreamer support") as error:
        resolve_backend("gstreamer")

    message = str(error.value)
    assert "camera.backend" in message
    # The message has to say what this build *does* offer, or the reader is
    # left guessing what to put there instead.
    assert "msmf" in message


def test_the_available_list_excludes_what_the_build_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends_module, "has_backend", lambda backend: backend == cv2.CAP_V4L2)

    with pytest.raises(ValueError) as error:
        resolve_backend("gstreamer")

    assert "provides: v4l2" in str(error.value)


def test_auto_is_never_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` is a list of candidates to try, not a demand for any one."""
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: False)

    assert resolve_backend("auto") == preferred_backends()


def test_backend_names_round_trip() -> None:
    assert backend_name(cv2.CAP_MSMF) == "msmf"
    assert backend_name(-987) == "backend--987"


def test_a_device_label_prefers_the_path() -> None:
    info = DeviceInfo(index=2, path="/dev/video2", name="Fisheye", width=2560, height=720, fps=30.0, backend="v4l2")

    assert info.label == "v4l2[2] Fisheye (/dev/video2)"


def test_a_device_label_falls_back_to_the_index() -> None:
    """Windows and macOS address devices by index; there is no path to show."""
    info = DeviceInfo(index=1, path="", name="Camera 1", width=1280, height=720, fps=30.0, backend="msmf")

    assert info.label == "msmf[1] Camera 1 (index 1)"


def test_a_device_label_names_the_backend_it_was_seen_on() -> None:
    """The same index is different hardware on a different driver."""
    on_msmf = DeviceInfo(index=0, path="", name="Camera 0", width=4000, height=1200, fps=30.0, backend="msmf")
    on_dshow = DeviceInfo(index=0, path="", name="Camera 0", width=640, height=480, fps=30.0, backend="dshow")

    assert on_msmf.label != on_dshow.label


# -- backend probing ---------------------------------------------------------


def test_probing_drops_the_any_pseudo_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """``any`` resolves to one of the others, so it would list them twice."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: True)

    assert backends_module.probe_backends() == [cv2.CAP_MSMF, cv2.CAP_DSHOW]


def test_probing_skips_backends_this_build_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends_module, "has_backend", lambda backend: backend == cv2.CAP_DSHOW)

    assert backends_module.probe_backends() == [cv2.CAP_DSHOW]


def test_probing_falls_back_to_any_when_nothing_is_compiled_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build with no named backend can still be asked to guess."""
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: False)

    assert backends_module.probe_backends() == [cv2.CAP_ANY]


# -- enumeration -------------------------------------------------------------


def test_enumeration_reports_a_working_device(driver: FakeDriver) -> None:
    found = enumerate_devices(max_index=3, backends=[cv2.CAP_V4L2])

    assert [device.index for device in found] == [0]
    assert found[0].width == 16
    assert found[0].height == 8
    assert found[0].fps == 30.0
    assert found[0].readable is True


def test_enumeration_skips_an_index_that_will_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDriver(devices={1: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)

    assert [device.index for device in enumerate_devices(max_index=3, backends=[cv2.CAP_V4L2])] == [1]


def test_a_device_that_streams_nothing_is_listed_as_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leaving it off the list turns "in use" into "not plugged in".

    A camera another program holds opens and then streams nothing, which is
    indistinguishable from a metadata-only ``/dev/video*`` node. Both are worth
    seeing; neither is worth silently pretending is absent.
    """
    fake = FakeDriver(devices={0: Device(failures=None), 1: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)

    found = enumerate_devices(max_index=3, backends=[cv2.CAP_V4L2])

    assert [(device.index, device.readable) for device in found] == [(0, False), (1, True)]


def test_an_unreadable_device_reports_the_mode_the_driver_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no frame to measure, and the driver usually still knows."""
    fake = FakeDriver(devices={0: Device(failures=None, width=4000.0, height=1200.0)})
    monkeypatch.setattr(cv2, "VideoCapture", fake)

    found = enumerate_devices(max_index=1, backends=[cv2.CAP_V4L2])

    assert (found[0].width, found[0].height) == (4000, 1200)


def test_a_camera_is_listed_once_per_backend_that_sees_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both rows are the point: index 0 is not one camera, it is two answers.

    On a laptop with a USB fisheye and a built-in webcam the two drivers number
    them oppositely, so collapsing to the first backend that answers is how you
    end up recording the wrong camera.
    """
    fake = FakeDriver(devices={0: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: True)

    found = enumerate_devices(max_index=1)

    assert [device.backend for device in found] == ["msmf", "dshow"]


def test_enumeration_tries_the_next_backend_when_the_first_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDriver(devices={0: Device(backends={cv2.CAP_DSHOW})})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: True)

    found = enumerate_devices(max_index=1)

    assert [device.backend for device in found] == ["dshow"]


def test_enumeration_releases_every_capture_it_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A held handle blocks the recorder from opening the same camera."""
    fake = FakeDriver(devices={0: Device(failures=None), 2: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)

    enumerate_devices(max_index=4)

    assert fake.captures
    assert all(capture.released for capture in fake.captures)


def test_enumeration_honours_the_index_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDriver(devices={5: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)

    assert enumerate_devices(max_index=3) == []


def test_enumeration_reports_linux_paths_and_sysfs_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    node = tmp_path / "name"
    node.write_text("Dual Fisheye Camera\n", encoding="utf-8")
    fake = FakeDriver(devices={0: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(backends_module, "Path", lambda _path: node)

    found = enumerate_devices(max_index=1, backends=[cv2.CAP_V4L2])

    assert found[0].path == "/dev/video0"
    assert found[0].name == "Dual Fisheye Camera"


def test_a_missing_sysfs_entry_still_yields_a_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reading sysfs is a convenience; it must never be the thing that fails."""
    monkeypatch.setattr(backends_module, "Path", lambda _path: tmp_path / "absent")

    assert backends_module._linux_device_name(3) == "Video device 3"


# -- opening -----------------------------------------------------------------


def test_a_fresh_source_is_closed(camera: CameraConfig) -> None:
    source = CameraSource(camera)

    assert source.is_open is False
    assert source.backend == "closed"


def test_opening_selects_a_backend(driver: FakeDriver, camera: CameraConfig) -> None:
    source = CameraSource(camera)
    source.open()

    assert source.is_open is True
    assert source.backend == backend_name(preferred_backends()[0])
    source.close()


def test_opening_twice_reuses_the_handle(driver: FakeDriver, camera: CameraConfig) -> None:
    source = CameraSource(camera)
    source.open()
    source.open()

    assert len(driver.captures) == 1
    source.close()


def test_the_format_is_requested_before_the_resolution(driver: FakeDriver, camera: CameraConfig) -> None:
    """A UVC device advertises different resolutions per pixel format.

    Asking for 2560x720 while still in YUYV silently lands on a smaller mode,
    so FOURCC has to be set first.
    """
    source = CameraSource(camera)
    source.open()

    props = [prop for prop, _ in driver.captures[0].settings]
    assert props.index(cv2.CAP_PROP_FOURCC) < props.index(cv2.CAP_PROP_FRAME_WIDTH)
    assert dict(driver.captures[0].settings)[cv2.CAP_PROP_BUFFERSIZE] == 1
    source.close()


def test_a_zero_size_leaves_the_drivers_own_mode_alone(driver: FakeDriver, camera: CameraConfig) -> None:
    """Native mode: ask for the pixel format and the rate, but not the size.

    Dual-fisheye modules ship in several native resolutions. Forcing one the
    device does not list lands on a downscaled mode, so zero means "whatever
    it opens in".
    """
    camera.width = 0
    camera.height = 0

    source = CameraSource(camera)
    source.open()

    props = [prop for prop, _ in driver.captures[0].settings]
    assert cv2.CAP_PROP_FRAME_WIDTH not in props
    assert cv2.CAP_PROP_FRAME_HEIGHT not in props
    # The format and the rate are still requested -- only the size is deferred.
    assert cv2.CAP_PROP_FOURCC in props
    assert cv2.CAP_PROP_FPS in props
    source.close()


def test_an_explicit_device_path_wins_over_the_index(driver: FakeDriver, camera: CameraConfig) -> None:
    camera.index = 0
    camera.device = "/dev/video4"
    driver.devices["/dev/video4"] = Device()

    with CameraSource(camera) as source:
        assert source.is_open is True

    assert driver.captures[0].target == "/dev/video4"


def test_a_camera_that_never_opens_is_reported(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    monkeypatch.setattr(cv2, "VideoCapture", FakeDriver())

    with pytest.raises(CaptureError, match="did not open"):
        CameraSource(camera).open()


def test_a_camera_that_streams_nothing_is_reported(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    monkeypatch.setattr(cv2, "VideoCapture", FakeDriver(devices={0: Device(failures=None)}))

    with pytest.raises(CaptureError, match="returned no frames"):
        CameraSource(camera).open()


def test_a_camera_held_elsewhere_says_so(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    """Opens, never streams: what another program holding the device looks like.

    Without the hint this reads as a hardware fault and sends people to the
    cabling, which is the one thing that is definitely fine.
    """
    monkeypatch.setattr(cv2, "VideoCapture", FakeDriver(devices={0: Device(failures=None)}))

    with pytest.raises(CaptureError, match="another program may be using this camera"):
        CameraSource(camera).open()


def test_a_failed_open_releases_its_handles(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    fake = FakeDriver(devices={0: Device(failures=None)})
    monkeypatch.setattr(cv2, "VideoCapture", fake)

    with pytest.raises(CaptureError):
        CameraSource(camera).open()

    assert all(capture.released for capture in fake.captures)


def test_every_candidate_backend_is_tried(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    fake = FakeDriver(devices={0: Device(backends={cv2.CAP_ANY})})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    monkeypatch.setattr(sys, "platform", "win32")

    with CameraSource(camera) as source:
        assert source.backend == "any"

    assert [capture.backend for capture in fake.captures] == [cv2.CAP_MSMF, cv2.CAP_DSHOW, cv2.CAP_ANY]


def test_falling_back_to_another_backend_is_warned_about(
    monkeypatch: pytest.MonkeyPatch, camera: CameraConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """A fallback can silently open a different camera, not the same one again.

    Drivers number devices in their own order, so when the preferred one is
    unavailable the index that was meant for a USB fisheye can land on a
    built-in webcam -- and everything downstream keeps working, on the wrong
    picture.
    """
    monkeypatch.setattr(cv2, "VideoCapture", FakeDriver(devices={0: Device(backends={cv2.CAP_DSHOW})}))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: True)

    with caplog.at_level(logging.WARNING, logger="vectra180.capture.source"), CameraSource(camera):
        pass

    assert "msmf could not be used" in caplog.text
    assert "different device on a different backend" in caplog.text


def test_an_explicit_device_is_not_warned_about(
    monkeypatch: pytest.MonkeyPatch, camera: CameraConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """A path names one device on every driver, so a fallback cannot stray."""
    monkeypatch.setattr(cv2, "VideoCapture", FakeDriver(devices={"/dev/video4": Device(backends={cv2.CAP_DSHOW})}))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: True)
    camera.device = "/dev/video4"
    camera.width = 0
    camera.height = 0

    with caplog.at_level(logging.WARNING, logger="vectra180.capture.source"), CameraSource(camera):
        pass

    assert caplog.text == ""


def test_a_pinned_backend_is_not_retried_elsewhere(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    fake = FakeDriver(devices={0: Device(backends={cv2.CAP_ANY})})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    # V4L2 is absent from the Windows wheel this suite also runs on, and the
    # point here is the retry policy, not what the local build carries.
    monkeypatch.setattr(backends_module, "has_backend", lambda _backend: True)
    camera.backend = "v4l2"

    with pytest.raises(CaptureError, match="v4l2"):
        CameraSource(camera).open()

    assert len(fake.captures) == 1


def test_an_invalid_backend_name_surfaces_at_open(camera: CameraConfig) -> None:
    camera.backend = "directshow"

    with pytest.raises(ValueError, match="unknown capture backend"):
        CameraSource(camera).open()


def test_closing_releases_the_handle(driver: FakeDriver, camera: CameraConfig) -> None:
    source = CameraSource(camera)
    source.open()
    source.close()

    assert driver.captures[0].released is True
    assert source.is_open is False


def test_closing_twice_is_harmless(driver: FakeDriver, camera: CameraConfig) -> None:
    source = CameraSource(camera)
    source.open()
    source.close()
    source.close()

    assert source.is_open is False


# -- reported geometry -------------------------------------------------------


def test_geometry_comes_from_the_driver_once_open(driver: FakeDriver, camera: CameraConfig) -> None:
    with CameraSource(camera) as source:
        assert (source.width, source.height, source.fps) == (2560, 720, 30.0)


def test_a_substituted_mode_is_warned_about(
    driver: FakeDriver, camera: CameraConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """UVC substitutes its nearest mode rather than refusing, so nothing else
    in the run would say the picture is the wrong size."""
    with caplog.at_level(logging.WARNING, logger="vectra180.capture.source"), CameraSource(camera):
        pass

    assert "gave 2560x720, not the requested 16x8" in caplog.text


def test_native_mode_is_not_warned_about(
    driver: FakeDriver, camera: CameraConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """Width and height of 0 mean 'whatever this device opens in'."""
    camera.width = 0
    camera.height = 0

    with caplog.at_level(logging.WARNING, logger="vectra180.capture.source"), CameraSource(camera):
        pass

    assert caplog.text == ""


def test_geometry_falls_back_to_the_config_when_closed(camera: CameraConfig) -> None:
    source = CameraSource(camera)

    assert (source.width, source.height, source.fps) == (16, 8, 30.0)


def test_a_driver_reporting_zero_fps_falls_back(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    """Several UVC drivers report 0; dividing by it would end the session."""
    monkeypatch.setattr(cv2, "VideoCapture", FakeDriver(devices={0: Device(fps=0.0)}))
    camera.fps = 24

    with CameraSource(camera) as source:
        assert source.fps == 24.0


def test_describe_is_json_safe(driver: FakeDriver, camera: CameraConfig) -> None:
    with CameraSource(camera) as source:
        described = source.describe()

    assert described["open"] is True
    assert described["backend"] == backend_name(preferred_backends()[0])
    assert described["device"] == 0
    assert described["fourcc"] == "MJPG"


# -- reading -----------------------------------------------------------------


def test_reading_before_opening_yields_nothing(camera: CameraConfig) -> None:
    assert CameraSource(camera).read() is None


def test_frames_are_numbered_and_timed(driver: FakeDriver, camera: CameraConfig) -> None:
    with CameraSource(camera) as source:
        first = source.read()
        second = source.read()

    assert first is not None
    assert second is not None
    assert (first.index, second.index) == (0, 1)
    assert second.monotonic >= first.monotonic
    assert first.size == (16, 8)


def test_a_good_read_clears_the_streak(driver: FakeDriver, camera: CameraConfig) -> None:
    source = CameraSource(camera)
    source.open()
    driver.devices[0].failures = 2

    assert source.read() is None
    assert source.read() is None
    assert source.failure_streak == 2
    assert source.read() is not None
    assert source.failure_streak == 0
    source.close()


# -- the frame iterator ------------------------------------------------------


def take(iterator: Iterator[Frame], count: int) -> list[Frame]:
    return [next(iterator) for _ in range(count)]


def test_the_iterator_opens_the_device(driver: FakeDriver, camera: CameraConfig) -> None:
    source = CameraSource(camera)

    frames = take(source.frames(), 3)

    assert [frame.index for frame in frames] == [0, 1, 2]
    source.close()


def test_a_short_glitch_does_not_interrupt_the_stream(driver: FakeDriver, camera: CameraConfig) -> None:
    """Below the failure limit the read is simply retried -- no reconnect."""
    source = CameraSource(camera)
    stream = source.frames()
    assert next(stream).index == 0

    driver.devices[0].failures = camera.read_failure_limit - 1

    assert next(stream).index == 1
    assert len(driver.captures) == 1
    source.close()


def test_a_sustained_failure_reopens_the_device(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    """A USB brownout re-enumerates the camera; the session must survive it."""
    device = Device()
    fake = FakeDriver(devices={0: device})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    source = CameraSource(camera)
    stream = source.frames()

    assert next(stream).index == 0
    device.failures = camera.read_failure_limit
    assert next(stream).index == 1

    assert len(fake.captures) == 2
    assert fake.captures[0].released is True
    source.close()


def test_a_sustained_failure_ends_a_non_reconnecting_stream(
    monkeypatch: pytest.MonkeyPatch, camera: CameraConfig
) -> None:
    device = Device()
    monkeypatch.setattr(cv2, "VideoCapture", FakeDriver(devices={0: device}))
    source = CameraSource(camera)
    stream = source.frames(reconnect=False)

    assert next(stream).index == 0
    device.failures = camera.read_failure_limit

    with pytest.raises(StopIteration):
        next(stream)
    assert source.is_open is False


class FlakyDriver(FakeDriver):
    """A camera that is missing for a while, then re-enumerates.

    ``absent_probes`` counts constructor calls, and one reconnect attempt
    probes every candidate backend -- so a whole missed attempt is
    ``len(preferred_backends())`` of them.
    """

    absent_probes: int = 0
    probes: int = 0

    def __call__(self, target: int | str, backend: int) -> FakeCapture:
        self.probes += 1
        if self.probes <= self.absent_probes:
            return FakeCapture(FakeDriver(), target, backend)
        return super().__call__(target, backend)


def test_a_failed_reconnect_is_retried(monkeypatch: pytest.MonkeyPatch, camera: CameraConfig) -> None:
    """The camera may still be enumerating; giving up would end the recording."""
    fake = FlakyDriver(devices={0: Device()})
    monkeypatch.setattr(cv2, "VideoCapture", fake)
    source = CameraSource(camera)
    stream = source.frames()
    assert next(stream).index == 0

    # The stream dies, and the device stays away for one whole reconnect.
    fake.devices[0].failures = camera.read_failure_limit
    fake.absent_probes = fake.probes + len(preferred_backends())

    assert next(stream).index == 1
    assert len(fake.captures) == 2
    source.close()
