"""G-sensor incident detection.

The threshold is the one number in the project a driver is likely to change, so
these pin down exactly what it means: deviation of total acceleration from 1 g,
independent of how the module is mounted.
"""

from __future__ import annotations

import math

import pytest

from vectra180.config import IncidentConfig
from vectra180.recorder.incident import Incident, IncidentDetector
from vectra180.telemetry import TelemetrySample

REST = 9.80665


def sample(accel: tuple[float, float, float] = (0.0, 0.0, REST)) -> TelemetrySample:
    """A telemetry sample carrying a given acceleration, in m/s^2."""
    return TelemetrySample(
        timestamp_us=0,
        accel_x=accel[0],
        accel_y=accel[1],
        accel_z=accel[2],
        gyro_x=0.0,
        gyro_y=0.0,
        gyro_z=0.0,
    )


@pytest.fixture
def detector() -> IncidentDetector:
    return IncidentDetector(IncidentConfig(threshold_g=0.5, cooldown_seconds=10.0))


# -- threshold ---------------------------------------------------------------


def test_a_car_at_rest_is_not_an_incident(detector: IncidentDetector) -> None:
    assert detector.update(sample(), now=0.0) is None


def test_freefall_is_an_incident(detector: IncidentDetector) -> None:
    """Zero g is a full 1 g away from rest -- a drop, or the module coming loose."""
    incident = detector.update(sample((0.0, 0.0, 0.0)), now=0.0)

    assert incident is not None
    assert incident.magnitude_g == pytest.approx(1.0)
    assert incident.source == "gsensor"


def test_a_hard_impact_is_an_incident(detector: IncidentDetector) -> None:
    incident = detector.update(sample((0.0, 0.0, 2.0 * REST)), now=0.0)

    assert incident is not None
    assert incident.magnitude_g == pytest.approx(1.0)


@pytest.mark.parametrize("deviation", [0.0, 0.2, 0.49])
def test_below_the_threshold_nothing_fires(detector: IncidentDetector, deviation: float) -> None:
    assert detector.update(sample((0.0, 0.0, (1.0 + deviation) * REST)), now=0.0) is None


@pytest.mark.parametrize("deviation", [0.51, 1.0, 4.0])
def test_above_the_threshold_it_fires(detector: IncidentDetector, deviation: float) -> None:
    assert detector.update(sample((0.0, 0.0, (1.0 + deviation) * REST)), now=0.0) is not None


def test_the_threshold_is_two_sided(detector: IncidentDetector) -> None:
    """Braking hard unloads the sensor as surely as a kerb loads it."""
    assert detector.update(sample((0.0, 0.0, 0.4 * REST)), now=0.0) is not None


def test_mounting_angle_does_not_matter() -> None:
    """The magnitude is what is tested, so the same event fires on any axis.

    This is what lets a driver stick the module to the windscreen at whatever
    angle it happens to sit at, with no per-vehicle calibration.
    """
    magnitude = 2.0 * REST
    diagonal = magnitude / math.sqrt(3.0)

    for accel in ((magnitude, 0, 0), (0, magnitude, 0), (0, 0, magnitude), (diagonal, diagonal, diagonal)):
        detector = IncidentDetector(IncidentConfig(threshold_g=0.5))
        incident = detector.update(sample(accel), now=0.0)  # type: ignore[arg-type]

        assert incident is not None
        assert incident.magnitude_g == pytest.approx(1.0, abs=1e-6)


# -- cooldown ----------------------------------------------------------------


def test_the_ring_down_after_an_impact_fires_once(detector: IncidentDetector) -> None:
    """An impact rings the accelerometer for hundreds of milliseconds.

    Without the cooldown that ring-down locks a whole run of segments and the
    loop has nothing left to reclaim.
    """
    hit = sample((0.0, 0.0, 3.0 * REST))

    fired = [detector.update(hit, now=index / 30.0) is not None for index in range(60)]

    assert fired.count(True) == 1
    assert detector.trigger_count == 1


def test_a_second_event_fires_once_the_cooldown_expires(detector: IncidentDetector) -> None:
    hit = sample((0.0, 0.0, 3.0 * REST))

    assert detector.update(hit, now=100.0) is not None
    assert detector.update(hit, now=109.9) is None
    assert detector.update(hit, now=110.0) is not None
    assert detector.trigger_count == 2


def test_a_zero_cooldown_fires_on_every_frame() -> None:
    detector = IncidentDetector(IncidentConfig(threshold_g=0.5, cooldown_seconds=0.0))
    hit = sample((0.0, 0.0, 3.0 * REST))

    for index in range(5):
        assert detector.update(hit, now=float(index)) is not None

    assert detector.trigger_count == 5


# -- manual and disabled -----------------------------------------------------


def test_the_lock_button_ignores_the_cooldown(detector: IncidentDetector) -> None:
    """A person pressing the button means it, whatever just happened."""
    detector.update(sample((0.0, 0.0, 3.0 * REST)), now=0.0)

    incident = detector.trigger_manual(now=0.5)

    assert incident.source == "manual"
    assert incident.magnitude_g == 0.0
    assert detector.trigger_count == 2


def test_a_manual_lock_starts_the_cooldown(detector: IncidentDetector) -> None:
    """Otherwise the impact that made the driver press the button fires too."""
    detector.trigger_manual(now=0.0)

    assert detector.update(sample((0.0, 0.0, 3.0 * REST)), now=1.0) is None


def test_disabling_detection_silences_the_g_sensor() -> None:
    detector = IncidentDetector(IncidentConfig(enabled=False, threshold_g=0.5))

    assert detector.update(sample((0.0, 0.0, 5.0 * REST)), now=0.0) is None
    assert detector.trigger_count == 0


def test_the_lock_button_still_works_when_detection_is_disabled() -> None:
    """Turning off the g-sensor must not take the manual lock with it."""
    detector = IncidentDetector(IncidentConfig(enabled=False))

    assert detector.trigger_manual(now=0.0).source == "manual"


def test_a_frame_without_telemetry_is_not_an_incident(detector: IncidentDetector) -> None:
    """A camera with no IMU records normally; it just never auto-locks."""
    assert detector.update(None, now=0.0) is None
    assert detector.trigger_count == 0


def test_the_detector_defaults_to_the_shipped_config() -> None:
    detector = IncidentDetector()

    assert detector.update(sample(), now=0.0) is None
    assert detector.update(sample((0.0, 0.0, 2.0 * REST)), now=0.0) is not None


# -- reported state ----------------------------------------------------------


def test_the_peak_tracks_the_worst_jolt_seen(detector: IncidentDetector) -> None:
    """Reported on the status page, including for sub-threshold events."""
    for deviation in (0.1, 0.3, 0.2):
        detector.update(sample((0.0, 0.0, (1.0 + deviation) * REST)), now=0.0)

    assert detector.peak_magnitude_g == pytest.approx(0.3, abs=1e-3)
    assert detector.trigger_count == 0


def test_the_peak_ignores_frames_without_telemetry(detector: IncidentDetector) -> None:
    detector.update(sample((0.0, 0.0, 2.0 * REST)), now=0.0)
    detector.update(None, now=1.0)

    assert detector.peak_magnitude_g == pytest.approx(1.0)


def test_reset_clears_everything(detector: IncidentDetector) -> None:
    detector.update(sample((0.0, 0.0, 3.0 * REST)), now=0.0)

    detector.reset()

    assert detector.trigger_count == 0
    assert detector.peak_magnitude_g == 0.0
    # The cooldown is cleared too, so the next impact fires immediately.
    assert detector.update(sample((0.0, 0.0, 3.0 * REST)), now=0.1) is not None


def test_incident_serialises_for_the_api() -> None:
    data = Incident(monotonic=12.5, magnitude_g=1.23456, source="gsensor").as_dict()

    assert data == {"magnitude_g": 1.235, "source": "gsensor"}
