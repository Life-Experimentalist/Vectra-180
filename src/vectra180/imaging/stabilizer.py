"""Digital horizon levelling.

The frame is counter-rotated by the IMU's roll angle so the horizon stays flat
while the vehicle leans. This corrects roll only: pitch and yaw would need the
image to be reprojected, not rotated, and the extra cost buys little on a
road-facing camera.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

__all__ = ["HorizonStabilizer"]


class HorizonStabilizer:
    """Rotation-based roll compensation."""

    #: Rotations below this many degrees are skipped -- the resampling would
    #: cost a full warp and blur the image for a sub-pixel shift.
    DEADBAND_DEGREES = 0.01

    @staticmethod
    def cover_scale(width: int, height: int, roll_degrees: float) -> float:
        """Zoom needed so a rotated frame still covers the output rectangle.

        Rotating an image leaves triangular gaps at the corners. This returns
        the smallest scale that pushes those gaps outside the frame, which is
        the ratio between the rotated bounding box and the original.
        """
        radians = abs(math.radians(roll_degrees))
        cos_r, sin_r = abs(math.cos(radians)), abs(math.sin(radians))
        if width <= 0 or height <= 0:
            return 1.0
        return max(
            (width * cos_r + height * sin_r) / width,
            (width * sin_r + height * cos_r) / height,
        )

    @staticmethod
    def stabilize(frame: np.ndarray, roll_degrees: float, scale: float | None = None) -> np.ndarray:
        """Counter-rotate ``frame`` to level the horizon.

        Args:
            frame: Image to level.
            roll_degrees: Roll from :class:`~vectra180.telemetry.OrientationFilter`.
            scale: Zoom factor. ``None`` computes the smallest zoom that hides
                the corner gaps; ``1.0`` keeps the full field of view and
                accepts them.

        Returns:
            A new levelled image, or ``frame`` itself when the roll is inside
            the deadband.
        """
        if abs(roll_degrees) < HorizonStabilizer.DEADBAND_DEGREES:
            return frame

        height, width = frame.shape[:2]
        centre = (width / 2.0, height / 2.0)
        zoom = HorizonStabilizer.cover_scale(width, height, roll_degrees) if scale is None else scale

        # Negated: the IMU reports how far the camera rolled, and the image
        # must turn the other way to cancel it.
        matrix = cv2.getRotationMatrix2D(centre, -roll_degrees, zoom)
        return cv2.warpAffine(
            frame,
            matrix,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
