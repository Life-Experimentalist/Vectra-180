"""The burned-in telemetry overlay.

A HUD test cannot assert what the pixels look like, so these assert the
properties that actually break: that drawing stays inside the frame at every
scale, that it does not crash on absent telemetry, and that state the driver
relies on -- recording, incident lock, dropped frames -- changes the output.
"""

from __future__ import annotations

import numpy as np
import pytest

from vectra180.imaging.hud import HUDRenderer, HUDStatus
from vectra180.telemetry import Orientation, TelemetrySample, level_sample


@pytest.fixture
def canvas() -> np.ndarray:
    return np.zeros((360, 640, 3), dtype=np.uint8)


def _drawn(frame: np.ndarray) -> int:
    return int(np.count_nonzero(frame))


# -- colour ------------------------------------------------------------------


def test_hex_colours_are_converted_to_bgr() -> None:
    """A constant transcribed in RGB order renders the wrong colour silently."""
    from vectra180.imaging.hud import _bgr

    assert _bgr("#00F2FE") == (0xFE, 0xF2, 0x00)
    assert _bgr("EF4444") == (0x44, 0x44, 0xEF)


# -- overlay -----------------------------------------------------------------


def test_overlay_draws_into_the_frame_it_was_given(canvas: np.ndarray) -> None:
    """It returns the same array: the recorder relies on the in-place edit."""
    out = HUDRenderer.draw_telemetry_overlay(canvas, level_sample(), Orientation(0, 0, 0), fps=30.0)

    assert out is canvas
    assert _drawn(canvas) > 0


def test_overlay_survives_absent_telemetry(canvas: np.ndarray) -> None:
    """A camera with no IMU is a supported configuration, not an error."""
    HUDRenderer.draw_telemetry_overlay(canvas, None, Orientation(0, 0, 0), fps=0.0)

    assert _drawn(canvas) > 0


@pytest.mark.parametrize("width", [320, 640, 1280, 2560])
def test_overlay_fits_every_supported_width(width: int) -> None:
    """Scaling is derived from width; a bad derivation crashes or clips."""
    frame = np.zeros((max(180, width // 4), width, 3), dtype=np.uint8)

    HUDRenderer.draw_telemetry_overlay(frame, level_sample(), Orientation(5, -3, 12), fps=30.0)

    assert _drawn(frame) > 0


def test_overlay_reflects_orientation(canvas: np.ndarray) -> None:
    level = np.zeros_like(canvas)
    HUDRenderer.draw_telemetry_overlay(level, level_sample(), Orientation(0, 0, 0))

    rolled = np.zeros_like(canvas)
    HUDRenderer.draw_telemetry_overlay(rolled, level_sample(), Orientation(30, 0, 0))

    assert not np.array_equal(level, rolled)


def test_extreme_attitude_does_not_escape_the_frame(canvas: np.ndarray) -> None:
    """Pitch drives an unclamped offset; only the frame bounds contain it."""
    for roll, pitch in ((180.0, 90.0), (-180.0, -90.0), (0.0, 720.0)):
        HUDRenderer.draw_telemetry_overlay(canvas.copy(), level_sample(), Orientation(roll, pitch, 0))


def test_extreme_sample_values_do_not_overflow_the_panel(canvas: np.ndarray) -> None:
    """The gyro and accel rows are fixed-width; saturation must still fit."""
    import struct

    from vectra180.telemetry.decoder import ACCEL_SCALE_LSB_PER_G

    payload = struct.pack("<Q", 2**47) + struct.pack(">hhhhhh", -32768, 32767, -32768, 32767, -32768, 32767)
    saturated = TelemetrySample.from_bytes(payload)
    assert abs(saturated.accel_y) == pytest.approx(32767 / ACCEL_SCALE_LSB_PER_G * 9.80665, rel=1e-3)

    HUDRenderer.draw_telemetry_overlay(canvas, saturated, Orientation(0, 0, 0))

    assert _drawn(canvas) > 0


# -- status line -------------------------------------------------------------


def test_recording_state_changes_the_status_line(canvas: np.ndarray) -> None:
    standby = np.zeros_like(canvas)
    HUDRenderer.draw_telemetry_overlay(standby, level_sample(), Orientation(0, 0, 0), status=HUDStatus())

    recording = np.zeros_like(canvas)
    HUDRenderer.draw_telemetry_overlay(
        recording,
        level_sample(),
        Orientation(0, 0, 0),
        status=HUDStatus(recording=True, segment_elapsed=12.5),
    )

    assert not np.array_equal(standby, recording)


def test_incident_lock_is_shown(canvas: np.ndarray) -> None:
    unlocked = np.zeros_like(canvas)
    status = HUDStatus(recording=True, free_bytes=10**10)
    HUDRenderer.draw_telemetry_overlay(unlocked, level_sample(), Orientation(0, 0, 0), status=status)

    locked = np.zeros_like(canvas)
    HUDRenderer.draw_telemetry_overlay(
        locked,
        level_sample(),
        Orientation(0, 0, 0),
        status=HUDStatus(recording=True, free_bytes=10**10, locked=True),
    )

    assert not np.array_equal(unlocked, locked)


def test_dropped_frames_are_surfaced(canvas: np.ndarray) -> None:
    """A silently dropping recorder is the failure users notice last."""
    clean = np.zeros_like(canvas)
    HUDRenderer.draw_telemetry_overlay(clean, level_sample(), Orientation(0, 0, 0), status=HUDStatus(recording=True))

    dropping = np.zeros_like(canvas)
    HUDRenderer.draw_telemetry_overlay(
        dropping, level_sample(), Orientation(0, 0, 0), status=HUDStatus(recording=True, dropped_frames=42)
    )

    assert not np.array_equal(clean, dropping)


def test_status_without_free_space_or_lock_still_renders(canvas: np.ndarray) -> None:
    HUDRenderer.draw_telemetry_overlay(
        canvas, level_sample(), Orientation(0, 0, 0), status=HUDStatus(recording=False, free_bytes=0)
    )

    assert _drawn(canvas) > 0


# -- standalone widgets ------------------------------------------------------


def test_artificial_horizon_draws_at_frame_centre(canvas: np.ndarray) -> None:
    HUDRenderer.draw_artificial_horizon(canvas, roll=0.0, pitch=0.0)

    centre_row = canvas[canvas.shape[0] // 2]
    assert np.count_nonzero(centre_row) > 0


def test_artificial_horizon_tilts_with_roll(canvas: np.ndarray) -> None:
    level = np.zeros_like(canvas)
    HUDRenderer.draw_artificial_horizon(level, roll=0.0, pitch=0.0)

    tilted = np.zeros_like(canvas)
    HUDRenderer.draw_artificial_horizon(tilted, roll=45.0, pitch=0.0)

    assert not np.array_equal(level, tilted)


def test_crosshair_marks_the_centre(canvas: np.ndarray) -> None:
    HUDRenderer.draw_crosshair(canvas)

    cy, cx = canvas.shape[0] // 2, canvas.shape[1] // 2
    assert np.count_nonzero(canvas[cy, cx - 20 : cx + 20]) > 0


def test_timestamp_bar_writes_along_the_bottom(canvas: np.ndarray) -> None:
    """Recorded footage needs a visible time after metadata is stripped."""
    HUDRenderer.draw_timestamp_bar(canvas, "2026-08-09 14:30:00")

    top_half = canvas[: canvas.shape[0] // 2]
    bottom_rows = canvas[-40:]
    assert _drawn(bottom_rows) > 0
    assert _drawn(top_half) == 0


def test_timestamp_bar_handles_a_long_string(canvas: np.ndarray) -> None:
    HUDRenderer.draw_timestamp_bar(canvas, "2026-08-09 14:30:00 UTC | SEG 0042 | LOCKED")

    assert canvas.shape == (360, 640, 3)


def test_timestamp_bar_scales_with_width() -> None:
    small = np.zeros((120, 320, 3), dtype=np.uint8)
    large = np.zeros((720, 1920, 3), dtype=np.uint8)

    HUDRenderer.draw_timestamp_bar(small, "12:00:00")
    HUDRenderer.draw_timestamp_bar(large, "12:00:00")

    assert _drawn(small) > 0
    assert _drawn(large) > _drawn(small)
