"""Shared fixtures.

The whole suite runs without a camera. Anything that would touch hardware is
served by :class:`FakeCameraSource`, which produces the same
:class:`~vectra180.capture.Frame` objects the real source does -- including a
genuine telemetry strip built by :func:`encode_payload`, so the decoder under
test is fed the real wire format rather than a mock of it.
"""

from __future__ import annotations

import itertools
import os
import struct
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from vectra180.capture import Frame
from vectra180.config import EngineConfig
from vectra180.telemetry.decoder import ACCEL_SCALE_LSB_PER_G, GYRO_SCALE_LSB_PER_DPS, PAYLOAD_BYTES

#: A small stand-in for the 2560x720 sensor frame. Same layout, ~1/40th the
#: pixels, so the suite stays fast.
FRAME_WIDTH = 320
FRAME_HEIGHT = 64
METADATA_WIDTH = 8

#: Sensor clock step between frames, matching 30fps. The decoder only accepts a
#: sample once a second frame continues its timeline, so anything feeding it
#: has to advance this the way real hardware does.
FRAME_INTERVAL_US = 33_333

#: Arbitrary non-zero start, as if the sensor had been up for a few seconds.
FIRST_TIMESTAMP_US = 1_234_567


def encode_payload(
    *,
    timestamp_us: int = FIRST_TIMESTAMP_US,
    accel_g: tuple[float, float, float] = (0.0, 0.0, 1.0),
    gyro_dps: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> bytes:
    """Build a 20-byte IMU payload in the camera's wire format.

    Mixed endianness on purpose: little-endian timestamp, big-endian sensor
    words. See :mod:`vectra180.telemetry.decoder`.
    """
    raw_accel = [round(value * ACCEL_SCALE_LSB_PER_G) for value in accel_g]
    raw_gyro = [round(value * GYRO_SCALE_LSB_PER_DPS) for value in gyro_dps]
    return struct.pack("<Q", timestamp_us) + struct.pack(">hhhhhh", *raw_accel, *raw_gyro)


def make_strip(payload: bytes, *, height: int = FRAME_HEIGHT, width: int = METADATA_WIDTH) -> np.ndarray:
    """Render a payload into a metadata strip, one byte per row."""
    strip = np.zeros((height, width, 3), dtype=np.uint8)
    strip[: len(payload), 0, 0] = np.frombuffer(payload, dtype=np.uint8)
    return strip


def make_strips(
    count: int,
    *,
    accel_g: tuple[float, float, float] = (0.0, 0.0, 1.0),
    gyro_dps: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> list[np.ndarray]:
    """A run of strips whose timestamps advance like a real sensor's.

    The decoder needs two consecutive frames before it trusts a timeline, so
    tests that want a decoded sample feed a sequence rather than one strip.
    """
    return [
        make_strip(
            encode_payload(
                timestamp_us=FIRST_TIMESTAMP_US + index * FRAME_INTERVAL_US,
                accel_g=accel_g,
                gyro_dps=gyro_dps,
            )
        )
        for index in range(count)
    ]


def make_frame(payload: bytes | None = None, *, value: int = 90) -> np.ndarray:
    """A full raw frame: metadata strip, then a side-by-side stereo image.

    The two halves are given different brightnesses and a shifted bright
    rectangle so that stereo matching has something real to match.
    """
    frame = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), value, dtype=np.uint8)
    image_width = FRAME_WIDTH - METADATA_WIDTH
    half = image_width // 2

    # Textured background: without it SGBM finds no correspondences at all.
    noise = np.tile(np.arange(half, dtype=np.uint8) % 37, (FRAME_HEIGHT, 1))
    left = METADATA_WIDTH
    frame[:, left : left + half, 0] = noise
    frame[:, left + half : left + 2 * half, 0] = np.roll(noise, -4, axis=1)

    # A block that sits 4px further left in the right view -- a disparity of 4.
    frame[20:44, left + 60 : left + 100] = 240
    frame[20:44, left + half + 56 : left + half + 96] = 240

    if payload is not None:
        frame[:, :METADATA_WIDTH] = make_strip(payload)
    return frame


class FakeCameraSource:
    """Drop-in replacement for :class:`~vectra180.capture.CameraSource`.

    Implements only what :class:`~vectra180.engine.Engine` uses. ``frames``
    keeps producing indefinitely, as a real camera does.

    Each frame gets a freshly stamped telemetry strip whose clock advances,
    because a decoder fed one frozen timestamp correctly concludes the strip is
    not telemetry. ``telemetry=False`` leaves the strip as image data, which is
    what a module without an IMU looks like.
    """

    def __init__(
        self,
        images: list[np.ndarray] | None = None,
        *,
        fps: float = 30.0,
        telemetry: bool = True,
    ) -> None:
        self._images = images or [make_frame()]
        self.fps = fps
        self.telemetry = telemetry
        self.opened = False
        self.closed = False
        self.read_count = 0

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    @property
    def is_open(self) -> bool:
        return self.opened and not self.closed

    @property
    def backend(self) -> str:
        return "FAKE"

    def describe(self) -> dict[str, object]:
        image = self._images[0]
        return {
            "open": self.is_open,
            "backend": self.backend,
            "width": image.shape[1],
            "height": image.shape[0],
            "fps": self.fps,
            "device": "fake",
            "fourcc": "MJPG",
        }

    def read(self) -> Frame | None:
        image = self._images[self.read_count % len(self._images)].copy()
        if self.telemetry:
            payload = encode_payload(timestamp_us=FIRST_TIMESTAMP_US + self.read_count * FRAME_INTERVAL_US)
            image[:, :METADATA_WIDTH] = make_strip(payload)
        frame = Frame(image=image, index=self.read_count, monotonic=time.monotonic(), wall_time=time.time())
        self.read_count += 1
        return frame

    def frames(self, *, reconnect: bool = True) -> Iterator[Frame]:
        self.open()
        for _ in itertools.count():
            frame = self.read()
            assert frame is not None
            yield frame
            # Roughly real-time, so the recorder's queue behaves as it would in
            # the field instead of being flooded by a spin loop.
            time.sleep(1.0 / self.fps)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Detach every test from the developer's own installation.

    ``EngineConfig.load()`` with no argument reads the platform config path and
    the ``VECTRA_*`` variables. Left alone, a developer with either of those
    set gets different results from CI -- and worse, a test could write into a
    real recording directory.
    """
    for name in list(os.environ):
        if name.startswith("VECTRA_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VECTRA_CONFIG", str(tmp_path_factory.mktemp("cfg") / "absent.toml"))


@pytest.fixture
def payload() -> bytes:
    """A level, stationary IMU reading."""
    return encode_payload()


@pytest.fixture
def strip(payload: bytes) -> np.ndarray:
    return make_strip(payload)


@pytest.fixture
def raw_frame(payload: bytes) -> np.ndarray:
    """A raw frame with a valid telemetry strip."""
    return make_frame(payload)


@pytest.fixture
def stereo_pair(raw_frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    image = raw_frame[:, METADATA_WIDTH:]
    half = image.shape[1] // 2
    return image[:, :half], image[:, half:]


@pytest.fixture
def config(tmp_path: Path) -> EngineConfig:
    """A config that touches nothing outside ``tmp_path``."""
    cfg = EngineConfig()
    cfg.recording.directory = tmp_path / "clips"
    cfg.recording.segment_seconds = 5
    cfg.recording.encoder = "opencv"
    cfg.recording.burn_timestamp = False
    cfg.telemetry.metadata_width = METADATA_WIDTH
    cfg.camera.width = FRAME_WIDTH
    cfg.camera.height = FRAME_HEIGHT
    cfg.depth.working_width = 128
    cfg.server.enabled = False
    cfg.validate()
    return cfg


@pytest.fixture
def payload_bytes_length() -> int:
    return PAYLOAD_BYTES
