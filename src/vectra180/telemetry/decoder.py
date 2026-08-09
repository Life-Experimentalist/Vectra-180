"""Decoding of the IMU block embedded in each frame's leftmost pixels.

The camera module's SoC writes a 20-byte ICM-42688 register dump into the
luminance of the first pixel column, one byte per row, before the image data
starts. Cropping the metadata strip off the left of the frame therefore also
removes the telemetry.

Wire format, byte offsets into that column:

===========  ======  ===============  =========================================
Offset       Size    Encoding         Field
===========  ======  ===============  =========================================
``0``        8       ``<Q``           Sensor timestamp, microseconds
``8``        2       ``>h``           Accelerometer X, raw LSB
``10``       2       ``>h``           Accelerometer Y, raw LSB
``12``       2       ``>h``           Accelerometer Z, raw LSB
``14``       2       ``>h``           Gyroscope X, raw LSB
``16``       2       ``>h``           Gyroscope Y, raw LSB
``18``       2       ``>h``           Gyroscope Z, raw LSB
===========  ======  ===============  =========================================

The mixed endianness is not a mistake: the SoC emits its own timestamp
little-endian, while the six sensor words are copied verbatim out of the
ICM-42688's big-endian register file.

Scaling assumes the sensor's default full-scale ranges -- +/-2 g and
+/-2000 deg/s -- which is what the stock firmware configures.

Not every dual-fisheye module writes this block, and on one that does not, the
first pixel column is ordinary image data. Since any 16-bit value is a
physically valid reading at those ranges, the sensor words cannot reveal that;
the timestamp can. It is checked against an absolute ceiling, and then across
frames for monotonic advance -- so a camera without telemetry reports none
rather than a stream of plausible-looking noise that would drive the horizon
and trip the incident detector.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["ACCEL_SCALE_LSB_PER_G", "GYRO_SCALE_LSB_PER_DPS", "PAYLOAD_BYTES", "TelemetryDecoder", "TelemetrySample"]

#: Total bytes of the embedded payload.
PAYLOAD_BYTES = 20

#: ICM-42688 sensitivity at the default +/-2 g full-scale range.
ACCEL_SCALE_LSB_PER_G = 16384.0

#: ICM-42688 sensitivity at the default +/-2000 deg/s full-scale range.
GYRO_SCALE_LSB_PER_DPS = 16.4

#: Standard gravity, for converting g to m/s^2.
STANDARD_GRAVITY = 9.80665

_HEADER = struct.Struct("<Q")
_IMU = struct.Struct(">hhhhhh")

#: Upper bound on the sensor's uptime counter, about 8.9 years in microseconds.
#: Eight arbitrary bytes clear this bound 99.998% of the time, which is what
#: makes it the effective filter against a strip that carries no telemetry.
_MAX_PLAUSIBLE_TIMESTAMP_US = 1 << 48

#: Largest forward step accepted between consecutive frames. Comfortably past
#: any real frame interval, including a stalled camera.
_MAX_TIMESTAMP_GAP_US = 5_000_000


@dataclass(frozen=True)
class TelemetrySample:
    """One decoded IMU reading."""

    #: Sensor-local timestamp in microseconds. It has no fixed epoch -- treat
    #: it as monotonic within a session, not as a wall clock.
    timestamp_us: int
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float

    @property
    def accel(self) -> tuple[float, float, float]:
        """Acceleration in m/s^2."""
        return (self.accel_x, self.accel_y, self.accel_z)

    @property
    def gyro(self) -> tuple[float, float, float]:
        """Angular velocity in rad/s."""
        return (self.gyro_x, self.gyro_y, self.gyro_z)

    @property
    def accel_magnitude_g(self) -> float:
        """Total acceleration in g. Reads ~1.0 when the vehicle is at rest."""
        magnitude = math.sqrt(self.accel_x**2 + self.accel_y**2 + self.accel_z**2)
        return magnitude / STANDARD_GRAVITY

    def as_dict(self) -> dict[str, Any]:
        """Flat JSON-serialisable view, used by sidecars and the HTTP API."""
        return {
            "timestamp_us": self.timestamp_us,
            "accel_x": self.accel_x,
            "accel_y": self.accel_y,
            "accel_z": self.accel_z,
            "gyro_x": self.gyro_x,
            "gyro_y": self.gyro_y,
            "gyro_z": self.gyro_z,
        }

    @classmethod
    def from_bytes(cls, payload: bytes) -> TelemetrySample:
        """Decode a raw 20-byte payload.

        The sensor words need no range check: the payload holds them as int16
        and the full-scale range is fixed, so every one of the 65536 possible
        values decodes to a physically valid reading. Only the timestamp can
        tell telemetry from image data.

        Raises:
            ValueError: if the payload is short, or its timestamp is past any
                real sensor uptime, which means the strip is not telemetry.
        """
        if len(payload) < PAYLOAD_BYTES:
            raise ValueError(f"telemetry payload is {len(payload)} bytes, need {PAYLOAD_BYTES}")

        (timestamp,) = _HEADER.unpack_from(payload, 0)
        ax, ay, az, gx, gy, gz = _IMU.unpack_from(payload, 8)

        if timestamp > _MAX_PLAUSIBLE_TIMESTAMP_US:
            raise ValueError("timestamp is far past any real sensor uptime; strip is not telemetry")

        accel_g = (ax / ACCEL_SCALE_LSB_PER_G, ay / ACCEL_SCALE_LSB_PER_G, az / ACCEL_SCALE_LSB_PER_G)
        gyro_dps = (gx / GYRO_SCALE_LSB_PER_DPS, gy / GYRO_SCALE_LSB_PER_DPS, gz / GYRO_SCALE_LSB_PER_DPS)

        return cls(
            timestamp_us=timestamp,
            accel_x=accel_g[0] * STANDARD_GRAVITY,
            accel_y=accel_g[1] * STANDARD_GRAVITY,
            accel_z=accel_g[2] * STANDARD_GRAVITY,
            gyro_x=math.radians(gyro_dps[0]),
            gyro_y=math.radians(gyro_dps[1]),
            gyro_z=math.radians(gyro_dps[2]),
        )


class TelemetryDecoder:
    """Pulls :class:`TelemetrySample` values out of frame metadata strips.

    The last good sample is retained so a single corrupt frame does not blank
    the HUD; :attr:`decoded_frames` and :attr:`failed_frames` let ``doctor``
    report whether a given camera emits the strip at all.
    """

    def __init__(self) -> None:
        self.last_sample: TelemetrySample | None = None
        self.decoded_frames = 0
        self.failed_frames = 0
        self._pending: TelemetrySample | None = None

    @staticmethod
    def payload_from_strip(metadata_strip: np.ndarray) -> bytes:
        """Read the payload column out of a metadata strip.

        Args:
            metadata_strip: ``(H, W)`` or ``(H, W, C)`` slice taken from the
                left edge of a raw frame.
        """
        if metadata_strip.ndim == 3:
            column = metadata_strip[:, 0, 0]
        elif metadata_strip.ndim == 2:
            column = metadata_strip[:, 0]
        else:
            raise ValueError(f"metadata strip must be 2- or 3-dimensional, got {metadata_strip.ndim}")
        return bytes(np.asarray(column[:PAYLOAD_BYTES], dtype=np.uint8))

    @staticmethod
    def _follows(candidate: TelemetrySample, reference: TelemetrySample) -> bool:
        """True if ``candidate``'s clock plausibly follows ``reference``'s."""
        delta = candidate.timestamp_us - reference.timestamp_us
        return 0 < delta <= _MAX_TIMESTAMP_GAP_US

    def decode_strip(self, metadata_strip: np.ndarray | None) -> TelemetrySample | None:
        """Decode a strip, returning the last good sample if this one is bad.

        A sample is accepted only once a second frame corroborates its
        timeline, which is what makes a camera without a telemetry block report
        nothing instead of noise. Eight arbitrary bytes clear the timestamp
        ceiling about once every 65536 frames, and a single stray sample is
        enough to lock a segment through the incident detector -- but two in a
        row that are also one frame interval apart essentially never happen.
        The cost is one frame of startup latency.
        """
        if metadata_strip is None or metadata_strip.size == 0:
            return self.last_sample

        try:
            sample = TelemetrySample.from_bytes(self.payload_from_strip(metadata_strip))
        except (ValueError, struct.error, IndexError):
            # A frame that decodes to nothing breaks any candidate timeline.
            self._pending = None
            self.failed_frames += 1
            return self.last_sample

        # Continuing the established timeline is the common case. Continuing
        # the *candidate* one is how the decoder recovers when that timeline
        # breaks -- a restarted sensor, a reconnected camera, a long stall.
        for reference in (self.last_sample, self._pending):
            if reference is not None and self._follows(sample, reference):
                self._pending = None
                self.decoded_frames += 1
                self.last_sample = sample
                return sample

        self._pending = sample
        self.failed_frames += 1
        return self.last_sample

    def decode_frame(self, frame: np.ndarray, metadata_width: int) -> TelemetrySample | None:
        """Convenience wrapper that slices the strip off a full frame."""
        if metadata_width <= 0:
            return None
        return self.decode_strip(frame[:, :metadata_width])

    @property
    def has_telemetry(self) -> bool:
        """True once at least one frame decoded cleanly."""
        return self.decoded_frames > 0

    def reset(self) -> None:
        self.last_sample = None
        self.decoded_frames = 0
        self.failed_frames = 0
        self._pending = None
