"""Heads-up display overlay.

Draws telemetry, attitude and recording state onto a frame. Everything scales
from a 1280px-wide reference so the panel stays legible from a 640px preview up
to a 2560px panorama.

The burned-in variant is what a dashcam needs: the overlay must survive being
copied off the SD card, so it is drawn into the pixels rather than composited
by a viewer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from vectra180.telemetry import Orientation, TelemetrySample

__all__ = ["HUDRenderer", "HUDStatus"]


@dataclass(frozen=True)
class HUDStatus:
    """Recorder state shown in the HUD's status line."""

    recording: bool = False
    clip_name: str = ""
    #: Seconds into the current segment.
    segment_elapsed: float = 0.0
    #: Free space on the recording volume, in bytes.
    free_bytes: int = 0
    #: Set while an incident lock is active.
    locked: bool = False
    dropped_frames: int = 0


def _bgr(hex_color: str) -> tuple[int, int, int]:
    """Convert ``#RRGGBB`` to the BGR tuple OpenCV expects.

    Doing the swap here rather than by hand removes the whole class of bug
    where a colour constant is transcribed in RGB order and renders wrong.
    """
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return (blue, green, red)


class HUDRenderer:
    """Static drawing routines for the telemetry overlay."""

    CYAN = _bgr("#00F2FE")
    DARK_BG = _bgr("#0B0F19")
    SLATE = _bgr("#94A3B8")
    AMBER = _bgr("#FBBF24")
    RED = _bgr("#EF4444")
    WHITE = (255, 255, 255)

    FONT = cv2.FONT_HERSHEY_SIMPLEX

    @staticmethod
    def draw_telemetry_overlay(
        frame: np.ndarray,
        sample: TelemetrySample | None,
        orientation: Orientation,
        fps: float = 0.0,
        status: HUDStatus | None = None,
    ) -> np.ndarray:
        """Draw the full overlay in place and return the same frame."""
        scale = max(0.6, frame.shape[1] / 1280.0)

        box_w = int(400 * scale)
        box_h = int(280 * scale)
        pad = int(8 * scale)

        # Composite the panel through addWeighted rather than drawing a solid
        # box, so the scene stays readable underneath it.
        overlay = frame.copy()
        cv2.rectangle(overlay, (pad, pad), (pad + box_w, pad + box_h), HUDRenderer.DARK_BG, -1)
        cv2.addWeighted(overlay, 0.75, frame, 0.25, 0, frame)

        title_scale = 0.7 * scale
        sub_scale = 0.35 * scale
        text_scale = 0.38 * scale
        thick_title = max(1, int(2 * scale))
        thick_text = max(1, int(1 * scale))
        line_height = int(16 * scale)

        y = pad + int(24 * scale)
        x = pad + int(8 * scale)
        right = pad + box_w - int(10 * scale)

        cv2.putText(frame, "VECTRA-180", (x, y), HUDRenderer.FONT, title_scale, HUDRenderer.CYAN, thick_title)
        y += int(18 * scale)
        cv2.putText(frame, "ENGINEERING TELEMETRY", (x, y), HUDRenderer.FONT, sub_scale, HUDRenderer.SLATE, thick_text)

        y += int(8 * scale)
        cv2.line(frame, (x, y), (right, y), HUDRenderer.CYAN, thick_text)

        y += int(17 * scale)
        timestamp = f"TS: {sample.timestamp_us:020d}" if sample else "TS: no telemetry"
        cv2.putText(frame, timestamp, (x, y), HUDRenderer.FONT, sub_scale, HUDRenderer.WHITE, thick_text)

        gyro = sample.gyro if sample else (0.0, 0.0, 0.0)
        accel = sample.accel if sample else (0.0, 0.0, 0.0)
        gyro_lines = [f"GYRO {axis}: {value:+8.3f} r/s" for axis, value in zip("XYZ", gyro, strict=True)]
        accel_lines = [f"ACC {axis}: {value:+8.3f} m/s2" for axis, value in zip("XYZ", accel, strict=True)]

        y += int(20 * scale)
        column_two = x + int(184 * scale)
        for gyro_line, accel_line in zip(gyro_lines, accel_lines, strict=True):
            cv2.putText(frame, gyro_line, (x, y), HUDRenderer.FONT, text_scale, HUDRenderer.WHITE, thick_text)
            cv2.putText(frame, accel_line, (column_two, y), HUDRenderer.FONT, text_scale, HUDRenderer.WHITE, thick_text)
            y += line_height

        y = HUDRenderer._divider(frame, x, right, y, scale, thick_text)
        for label, value in zip(("ROLL", "PITCH", "YAW"), orientation.as_tuple(), strict=True):
            cv2.putText(
                frame,
                f"{label + ':':<7}{value:+7.2f} deg",
                (x, y),
                HUDRenderer.FONT,
                text_scale,
                HUDRenderer.CYAN,
                thick_text,
            )
            y += line_height

        y = HUDRenderer._divider(frame, x, right, y, scale, thick_text)
        cv2.putText(frame, f"FPS: {fps:.0f}", (x, y), HUDRenderer.FONT, 0.5 * scale, HUDRenderer.CYAN, thick_text)

        if status is not None:
            HUDRenderer._draw_status(frame, status, x, column_two, y, scale, text_scale, thick_text)

        HUDRenderer.draw_artificial_horizon(frame, orientation.roll, orientation.pitch, scale)
        return frame

    @staticmethod
    def _divider(frame: np.ndarray, x: int, right: int, y: int, scale: float, thickness: int) -> int:
        """Draw a separator and return the baseline for the next text row."""
        y -= int(12 * scale)
        cv2.line(frame, (x, y + int(4 * scale)), (right, y + int(4 * scale)), HUDRenderer.SLATE, thickness)
        return y + int(22 * scale)

    @staticmethod
    def _draw_status(
        frame: np.ndarray,
        status: HUDStatus,
        x: int,
        column_two: int,
        y: int,
        scale: float,
        text_scale: float,
        thickness: int,
    ) -> None:
        if status.recording:
            # A filled circle is the universally understood record indicator,
            # and it reads at a glance in a rear-view mirror.
            cv2.circle(frame, (column_two + int(6 * scale), y - int(4 * scale)), int(5 * scale), HUDRenderer.RED, -1)
            label = f"REC {status.segment_elapsed:5.1f}s"
            colour = HUDRenderer.WHITE
        else:
            label = "STANDBY"
            colour = HUDRenderer.SLATE
        cv2.putText(frame, label, (column_two + int(18 * scale), y), HUDRenderer.FONT, text_scale, colour, thickness)

        y += int(16 * scale)
        if status.locked:
            cv2.putText(frame, "INCIDENT LOCKED", (x, y), HUDRenderer.FONT, text_scale, HUDRenderer.AMBER, thickness)
        elif status.free_bytes:
            free_gb = status.free_bytes / 1024**3
            cv2.putText(
                frame, f"FREE: {free_gb:.1f} GB", (x, y), HUDRenderer.FONT, text_scale, HUDRenderer.SLATE, thickness
            )

        if status.dropped_frames:
            y += int(16 * scale)
            cv2.putText(
                frame,
                f"DROPPED: {status.dropped_frames}",
                (x, y),
                HUDRenderer.FONT,
                text_scale,
                HUDRenderer.AMBER,
                thickness,
            )

    @staticmethod
    def draw_artificial_horizon(frame: np.ndarray, roll: float, pitch: float, scale: float = 1.0) -> np.ndarray:
        """Draw an aircraft-style attitude indicator at frame centre.

        The horizon bar rotates with roll and slides with pitch, while the
        centre reticle stays fixed to represent the vehicle.
        """
        height, width = frame.shape[:2]
        cx, cy = width // 2, height // 2
        thickness = max(1, int(2 * scale))

        # Three screen pixels per degree of pitch is the usual gauge gain: it
        # keeps normal road pitch inside the reticle without feeling inert.
        pitch_offset = pitch * 3.0 * scale
        radians = math.radians(roll)
        cos_r, sin_r = math.cos(radians), math.sin(radians)

        half_width = 100.0 * scale
        dx, dy = int(half_width * cos_r), int(half_width * sin_r)
        px = cx + int(pitch_offset * sin_r)
        py = cy - int(pitch_offset * cos_r)

        cv2.line(frame, (px - dx, py - dy), (px + dx, py + dy), HUDRenderer.CYAN, thickness)

        size, gap = int(15 * scale), int(5 * scale)
        cv2.line(frame, (cx - size, cy), (cx - gap, cy), HUDRenderer.CYAN, thickness)
        cv2.line(frame, (cx + gap, cy), (cx + size, cy), HUDRenderer.CYAN, thickness)
        cv2.line(frame, (cx, cy), (cx, cy + gap), HUDRenderer.CYAN, thickness)
        return frame

    @staticmethod
    def draw_crosshair(frame: np.ndarray, size: int = 20) -> np.ndarray:
        """Draw a centre reticle, used by the calibration view."""
        height, width = frame.shape[:2]
        cx, cy = width // 2, height // 2
        cv2.line(frame, (cx - size, cy), (cx + size, cy), HUDRenderer.CYAN, 1)
        cv2.line(frame, (cx, cy - size), (cx, cy + size), HUDRenderer.CYAN, 1)
        cv2.circle(frame, (cx, cy), 4, HUDRenderer.CYAN, 1)
        return frame

    @staticmethod
    def draw_timestamp_bar(frame: np.ndarray, text: str) -> np.ndarray:
        """Burn a timestamp strip across the bottom of a recorded frame.

        Recorded footage needs a visible time even after the container's
        metadata is stripped by a copy or a re-encode.
        """
        height, width = frame.shape[:2]
        scale = max(0.5, width / 1920.0)
        (text_w, text_h), baseline = cv2.getTextSize(text, HUDRenderer.FONT, 0.6 * scale, max(1, int(scale)))
        margin = int(10 * scale)
        top = height - text_h - baseline - 2 * margin

        overlay = frame.copy()
        cv2.rectangle(overlay, (0, top), (text_w + 2 * margin, height), HUDRenderer.DARK_BG, -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
        cv2.putText(
            frame,
            text,
            (margin, height - margin - baseline),
            HUDRenderer.FONT,
            0.6 * scale,
            HUDRenderer.WHITE,
            max(1, int(scale)),
        )
        return frame
