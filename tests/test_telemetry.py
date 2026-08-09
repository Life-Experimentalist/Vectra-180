"""Telemetry decoding and attitude estimation."""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from vectra180.config import TelemetryConfig
from vectra180.telemetry import Orientation, OrientationFilter, TelemetryDecoder, TelemetrySample, level_sample
from vectra180.telemetry.decoder import PAYLOAD_BYTES, STANDARD_GRAVITY

from .conftest import (
    FIRST_TIMESTAMP_US,
    FRAME_HEIGHT,
    FRAME_INTERVAL_US,
    METADATA_WIDTH,
    encode_payload,
    make_frame,
    make_strip,
    make_strips,
)

# -- wire format -------------------------------------------------------------


def test_payload_round_trips_through_the_decoder() -> None:
    payload = encode_payload(timestamp_us=42, accel_g=(0.25, -0.5, 1.0), gyro_dps=(10.0, -20.0, 30.0))
    sample = TelemetrySample.from_bytes(payload)

    assert sample.timestamp_us == 42
    assert sample.accel_x == pytest.approx(0.25 * STANDARD_GRAVITY, rel=1e-3)
    assert sample.accel_y == pytest.approx(-0.5 * STANDARD_GRAVITY, rel=1e-3)
    assert sample.accel_z == pytest.approx(1.0 * STANDARD_GRAVITY, rel=1e-3)
    assert math.degrees(sample.gyro_x) == pytest.approx(10.0, rel=1e-2)
    assert math.degrees(sample.gyro_z) == pytest.approx(30.0, rel=1e-2)


def test_sensor_words_are_big_endian() -> None:
    """The mixed endianness is the part most easily got wrong.

    A little-endian reading of the same bytes gives a completely different
    number, so this pins the byte order rather than merely the field offsets.
    """
    payload = struct.pack("<Q", 0) + struct.pack(">hhhhhh", 0, 0, 16384, 0, 0, 0)
    assert TelemetrySample.from_bytes(payload).accel_z == pytest.approx(STANDARD_GRAVITY, rel=1e-4)

    swapped = struct.pack("<Q", 0) + struct.pack("<hhhhhh", 0, 0, 16384, 0, 0, 0)
    assert TelemetrySample.from_bytes(swapped).accel_z != pytest.approx(STANDARD_GRAVITY, rel=1e-4)


def test_short_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="need 20"):
        TelemetrySample.from_bytes(b"\x00" * (PAYLOAD_BYTES - 1))


def test_implausible_timestamp_is_rejected() -> None:
    """Image data landing in the strip must not be mistaken for telemetry.

    The sensor words cannot give it away -- at +/-2 g every one of the 65536
    possible 16-bit values is a physically valid reading -- so the timestamp
    carries the whole discriminator.
    """
    payload = struct.pack("<Q", 0xFFFF_FFFF_FFFF_FFFF) + struct.pack(">hhhhhh", 0, 0, 16384, 0, 0, 0)
    with pytest.raises(ValueError, match="sensor uptime"):
        TelemetrySample.from_bytes(payload)


@pytest.mark.parametrize("word", [-32768, -1, 0, 1, 32767])
def test_every_sensor_word_is_a_valid_reading(word: int) -> None:
    """Why ``from_bytes`` range-checks the clock and not the sensors.

    At +/-2 g and +/-2000 dps the extremes of int16 are 2 g and 1998 dps, so no
    encodable value is out of range. A range check on these fields would be
    unreachable code masquerading as validation.
    """
    payload = struct.pack("<Q", 1000) + struct.pack(">hhhhhh", *([word] * 6))
    sample = TelemetrySample.from_bytes(payload)

    assert abs(sample.accel_x) <= 2.0 * STANDARD_GRAVITY
    assert abs(math.degrees(sample.gyro_x)) <= 2000.0


def test_accel_magnitude_is_in_g() -> None:
    sample = TelemetrySample.from_bytes(encode_payload(accel_g=(0.0, 0.0, 1.0)))
    assert sample.accel_magnitude_g == pytest.approx(1.0, rel=1e-3)


def test_as_dict_is_json_safe() -> None:
    data = TelemetrySample.from_bytes(encode_payload()).as_dict()
    assert set(data) == {
        "timestamp_us",
        "accel_x",
        "accel_y",
        "accel_z",
        "gyro_x",
        "gyro_y",
        "gyro_z",
    }
    assert all(isinstance(value, (int, float)) for value in data.values())


# -- strip extraction --------------------------------------------------------


def test_decoder_reads_a_three_channel_strip() -> None:
    decoder = TelemetryDecoder()
    for candidate in make_strips(2):
        sample = decoder.decode_strip(candidate)

    assert sample is not None
    assert sample.accel_magnitude_g == pytest.approx(1.0, rel=1e-3)


def test_decoder_reads_a_single_channel_strip() -> None:
    """A grayscale capture has no channel axis; the payload is still column 0."""
    decoder = TelemetryDecoder()
    for candidate in make_strips(2):
        sample = decoder.decode_strip(candidate[:, :, 0])

    assert sample is not None


def test_decoder_rejects_a_one_dimensional_strip() -> None:
    with pytest.raises(ValueError, match="2- or 3-dimensional"):
        TelemetryDecoder.payload_from_strip(np.zeros(20, dtype=np.uint8))


def test_a_single_frame_is_not_yet_telemetry() -> None:
    """One frame cannot distinguish a sensor from a lucky run of pixels.

    Accepting it is how noise reaches the incident detector, so the first
    frame is held as a candidate and confirmed by the second.
    """
    decoder = TelemetryDecoder()
    first, second = make_strips(2)

    assert decoder.decode_strip(first) is None
    assert decoder.decode_strip(second) is not None
    assert decoder.decoded_frames == 1


def test_decoder_holds_the_last_good_sample() -> None:
    """One corrupt frame must not blank the HUD."""
    decoder = TelemetryDecoder()
    strips = make_strips(3)
    good = None
    for candidate in strips[:2]:
        good = decoder.decode_strip(candidate)

    garbage = np.full_like(strips[0], 200)
    assert decoder.decode_strip(garbage) is good

    # ...and the stream picks up again where it left off.
    assert decoder.decode_strip(strips[2]) is not None


def test_a_stalled_clock_is_not_telemetry() -> None:
    """A strip repeating one timestamp is a frozen buffer, not a sensor."""
    decoder = TelemetryDecoder()
    frozen = make_strips(1)[0]

    for _ in range(10):
        assert decoder.decode_strip(frozen) is None

    assert decoder.has_telemetry is False


def test_decoder_resyncs_after_a_sensor_reset() -> None:
    """A sensor that restarts its counter must not lock the decoder out.

    Two frames on the new timeline are enough to adopt it -- the same rule
    that admitted the original one, so no separate escape hatch is needed.
    """
    decoder = TelemetryDecoder()
    for candidate in make_strips(4):
        decoder.decode_strip(candidate)
    assert decoder.has_telemetry

    restarted = [make_strip(encode_payload(timestamp_us=1_000 + i * 33_333)) for i in range(2)]
    assert decoder.decode_strip(restarted[0]) is not None  # still the old sample
    sample = decoder.decode_strip(restarted[1])

    assert sample is not None
    assert sample.timestamp_us == 1_000 + 33_333


def test_a_camera_without_telemetry_reports_none() -> None:
    """The bug this guards: noise driving the horizon and the incident detector.

    Image data in the strip is unrelated frame to frame, so the timestamps it
    decodes to never form a timeline. None of it may reach a caller.
    """
    decoder = TelemetryDecoder()
    rng = np.random.default_rng(seed=7)

    for _ in range(2000):
        noise = rng.integers(0, 256, size=(FRAME_HEIGHT, METADATA_WIDTH, 3), dtype=np.uint8)
        assert decoder.decode_strip(noise) is None

    assert decoder.has_telemetry is False
    assert decoder.decoded_frames == 0


def test_decoder_reports_absence_before_any_frame() -> None:
    decoder = TelemetryDecoder()
    assert decoder.has_telemetry is False
    assert decoder.decode_strip(None) is None


def test_reset_clears_the_candidate_timeline() -> None:
    decoder = TelemetryDecoder()
    for candidate in make_strips(2):
        decoder.decode_strip(candidate)

    decoder.reset()

    assert decoder.last_sample is None
    assert decoder.has_telemetry is False
    # A single strip after a reset is a candidate again, not an instant sample.
    assert decoder.decode_strip(make_strips(1)[0]) is None


def test_decode_frame_slices_the_strip() -> None:
    decoder = TelemetryDecoder()
    for index in range(2):
        payload = encode_payload(timestamp_us=FIRST_TIMESTAMP_US + index * FRAME_INTERVAL_US)
        sample = decoder.decode_frame(make_frame(payload), METADATA_WIDTH)

    assert sample is not None
    # A zero-width strip means the caller cropped it off; there is nothing to read.
    assert decoder.decode_frame(make_frame(payload), 0) is None


# -- orientation -------------------------------------------------------------


def test_level_sample_settles_at_zero() -> None:
    filt = OrientationFilter()
    for _ in range(100):
        orientation = filt.update(level_sample(), 1 / 30)
    assert orientation.roll == pytest.approx(0.0, abs=1e-6)
    assert orientation.pitch == pytest.approx(0.0, abs=1e-6)
    assert orientation.yaw == pytest.approx(0.0, abs=1e-6)


def test_first_trusted_gravity_reading_snaps_the_horizon() -> None:
    """A 45-degree roll must be reported immediately, not eased into."""
    filt = OrientationFilter()
    tilted = TelemetrySample.from_bytes(
        encode_payload(accel_g=(0.0, math.sin(math.radians(45)), math.cos(math.radians(45))))
    )

    orientation = filt.update(tilted, 1 / 30)

    assert filt.gravity_locked is True
    assert orientation.roll == pytest.approx(45.0, abs=0.5)


def test_hard_acceleration_is_not_treated_as_gravity() -> None:
    filt = OrientationFilter()
    braking = TelemetrySample.from_bytes(encode_payload(accel_g=(0.9, 0.0, 1.0)))
    assert filt.is_gravity_trusted(braking) is False

    filt.update(braking, 1 / 30)
    assert filt.gravity_locked is False


def test_gyro_integration_without_trusted_gravity() -> None:
    """With gravity untrusted, attitude comes from the gyro alone."""
    filt = OrientationFilter(TelemetryConfig(smoothing_alpha=0.0))
    # 1.5 g on two axes is a 2.1 g magnitude -- well outside the gravity trust
    # band, and inside what a +/-2 g accelerometer can encode per axis.
    spinning = TelemetrySample.from_bytes(encode_payload(accel_g=(1.5, 0.0, 1.5), gyro_dps=(0.0, 0.0, 60.0)))

    for _ in range(30):
        orientation = filt.update(spinning, 1 / 30)

    # 60 dps for one second, minus the yaw leak, which only bleeds it down.
    assert 0.0 < orientation.yaw < 60.0


def test_yaw_leaks_back_to_zero() -> None:
    filt = OrientationFilter(TelemetryConfig(yaw_leak_seconds=0.5, smoothing_alpha=0.0))
    turning = TelemetrySample.from_bytes(encode_payload(accel_g=(0.0, 0.0, 1.0), gyro_dps=(0.0, 0.0, 90.0)))
    for _ in range(10):
        filt.update(turning, 1 / 30)
    peak = abs(filt.orientation.yaw)

    still = level_sample()
    for _ in range(60):
        filt.update(still, 1 / 30)

    assert abs(filt.orientation.yaw) < peak * 0.1


def test_filter_response_is_independent_of_frame_rate() -> None:
    """Halving the frame rate must not halve the filter's response.

    This is what the ``alpha ** (dt / nominal)`` rescaling buys, and it is
    invisible until someone runs the camera at 15fps.
    """
    tilted = TelemetrySample.from_bytes(encode_payload(accel_g=(0.0, 0.34, 0.94), gyro_dps=(5.0, 0.0, 0.0)))

    fast = OrientationFilter()
    for _ in range(60):
        fast.update(tilted, 1 / 60)

    slow = OrientationFilter()
    for _ in range(15):
        slow.update(tilted, 1 / 15)

    assert fast.orientation.roll == pytest.approx(slow.orientation.roll, abs=1.0)


def test_a_smoothing_weight_of_one_ignores_the_gyro_entirely() -> None:
    """``alpha`` is the fraction of the old value that survives one frame.

    At 1.0 nothing new ever lands. It is a useless setting, but the rescaling
    has to answer it directly instead of raising it to a power, so the result
    is a frozen heading rather than a NaN on the horizon.
    """
    filt = OrientationFilter(TelemetryConfig(smoothing_alpha=1.0))
    turning = TelemetrySample.from_bytes(encode_payload(accel_g=(0.0, 0.0, 1.0), gyro_dps=(0.0, 0.0, 90.0)))

    for _ in range(30):
        filt.update(turning, 1 / 30)

    assert filt.orientation.yaw == 0.0


def test_update_ignores_missing_samples_and_bad_intervals() -> None:
    filt = OrientationFilter()
    filt.update(level_sample(), 1 / 30)
    before = filt.orientation

    assert filt.update(None, 1 / 30) == before
    assert filt.update(level_sample(), 0.0) == before
    assert filt.update(level_sample(), -1.0) == before


def test_reset_relevels_the_horizon() -> None:
    filt = OrientationFilter()
    filt.update(TelemetrySample.from_bytes(encode_payload(accel_g=(0.0, 0.7, 0.7))), 1 / 30)
    assert filt.orientation.roll != pytest.approx(0.0, abs=1.0)

    filt.reset()

    assert filt.orientation == Orientation(0.0, 0.0, 0.0)
    assert filt.gravity_locked is False


def test_orientation_conversions() -> None:
    orientation = Orientation(roll=1.0, pitch=2.0, yaw=3.0)
    assert orientation.as_tuple() == (1.0, 2.0, 3.0)
    assert orientation.as_dict() == {"roll": 1.0, "pitch": 2.0, "yaw": 3.0}
