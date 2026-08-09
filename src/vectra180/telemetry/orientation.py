"""Attitude estimation from the embedded IMU.

Integrating the gyroscope alone drifts without bound. A complementary filter
fixes roll and pitch by leaning on gravity: when the accelerometer reads close
to 1 g the vehicle is not accelerating hard, so the acceleration vector points
down and gives an absolute reference.

Yaw gets no such correction -- the module has no magnetometer, and gravity says
nothing about heading. It is reported as a relative heading that bleeds back to
zero over :attr:`TelemetryConfig.yaw_leak_seconds`.

Every filter constant is expressed as a time constant in seconds and converted
per-sample using the real elapsed time, so a dropped frame or a change of frame
rate does not change the filter's behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from vectra180.config import TelemetryConfig
from vectra180.telemetry.decoder import STANDARD_GRAVITY, TelemetrySample

__all__ = ["Orientation", "OrientationFilter", "level_sample"]

#: Reference sample interval for the configured smoothing weights. The
#: per-sample weight is rescaled from this to the actual dt.
_NOMINAL_DT = 1.0 / 30.0


def _rescale_alpha(alpha: float, dt: float) -> float:
    """Convert a per-frame retention weight to one for an arbitrary ``dt``.

    ``alpha`` is what fraction of the old value survives one nominal frame.
    Surviving ``dt`` seconds means surviving ``dt / _NOMINAL_DT`` of them, so
    the weight is raised to that power. Without this, halving the frame rate
    would silently double every filter's response time.
    """
    if alpha <= 0.0:
        return 0.0
    if alpha >= 1.0:
        return 1.0
    return float(alpha ** (dt / _NOMINAL_DT))


@dataclass(frozen=True)
class Orientation:
    """Attitude in degrees."""

    roll: float
    pitch: float
    yaw: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.roll, self.pitch, self.yaw)

    def as_dict(self) -> dict[str, float]:
        return {"roll": self.roll, "pitch": self.pitch, "yaw": self.yaw}


class OrientationFilter:
    """Complementary filter over the decoded IMU stream."""

    def __init__(self, config: TelemetryConfig | None = None) -> None:
        self._config = config or TelemetryConfig()
        self._roll = 0.0
        self._pitch = 0.0
        self._yaw = 0.0
        self._smooth_gyro = (0.0, 0.0, 0.0)
        #: True once a trusted gravity reading has snapped the attitude to
        #: absolute; before that, roll and pitch are only relative.
        self.gravity_locked = False

    @staticmethod
    def gravity_angles(sample: TelemetrySample) -> tuple[float, float]:
        """Return (roll, pitch) in radians implied by the gravity vector.

        The camera's Z axis points along the optical axis and Y points down,
        which is the standard aerospace arrangement these formulae assume.
        """
        ax, ay, az = sample.accel
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.hypot(ay, az))
        return roll, pitch

    def is_gravity_trusted(self, sample: TelemetrySample) -> bool:
        """True when total acceleration is close enough to 1 g to trust.

        Braking, cornering and potholes all add acceleration that is not
        gravity; correcting toward those would tilt the horizon the wrong way.
        """
        return abs(sample.accel_magnitude_g - 1.0) <= self._config.gravity_tolerance_g

    def update(self, sample: TelemetrySample | None, dt: float) -> Orientation:
        """Advance the filter by ``dt`` seconds and return the new attitude.

        A ``None`` sample or a non-positive ``dt`` leaves the state untouched,
        so a frame that failed to decode simply holds the last attitude rather
        than integrating a zero into it.
        """
        if sample is None or dt <= 0.0:
            return self.orientation

        smoothing = _rescale_alpha(self._config.smoothing_alpha, dt)
        self._smooth_gyro = tuple(  # type: ignore[assignment]
            smoothing * old + (1.0 - smoothing) * new for old, new in zip(self._smooth_gyro, sample.gyro, strict=True)
        )
        gyro_x, gyro_y, gyro_z = self._smooth_gyro

        roll = self._roll + gyro_x * dt
        pitch = self._pitch + gyro_y * dt

        if self.is_gravity_trusted(sample):
            accel_roll, accel_pitch = self.gravity_angles(sample)
            if self.gravity_locked:
                weight = _rescale_alpha(self._config.complementary_alpha, dt)
                roll = weight * roll + (1.0 - weight) * accel_roll
                pitch = weight * pitch + (1.0 - weight) * accel_pitch
            else:
                # First trusted reading: adopt it outright instead of easing
                # toward it, so the horizon is level from the first second
                # rather than after the filter's time constant.
                roll, pitch = accel_roll, accel_pitch
                self.gravity_locked = True

        self._roll = roll
        self._pitch = pitch

        yaw = self._yaw + gyro_z * dt
        tau = self._config.yaw_leak_seconds
        self._yaw = yaw * math.exp(-dt / tau) if tau > 0 else yaw

        return self.orientation

    @property
    def orientation(self) -> Orientation:
        return Orientation(
            roll=math.degrees(self._roll),
            pitch=math.degrees(self._pitch),
            yaw=math.degrees(self._yaw),
        )

    def reset(self) -> None:
        """Re-level the horizon and clear the heading."""
        self._roll = 0.0
        self._pitch = 0.0
        self._yaw = 0.0
        self._smooth_gyro = (0.0, 0.0, 0.0)
        self.gravity_locked = False


def level_sample() -> TelemetrySample:
    """A synthetic sample of a stationary, level device.

    The reference input for the filter: gravity on Z alone, no rotation. Roll,
    pitch and yaw should all settle at zero.
    """
    return TelemetrySample(
        timestamp_us=0,
        accel_x=0.0,
        accel_y=0.0,
        accel_z=STANDARD_GRAVITY,
        gyro_x=0.0,
        gyro_y=0.0,
        gyro_z=0.0,
    )
