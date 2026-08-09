"""Joining the two dewarped views into one wide image.

This is a fixed-geometry blend, not a feature-matching stitch. The two lenses
are rigidly mounted on one PCB, so their relative pose never changes and there
is nothing for a homography search to discover -- it would only cost frames and
wobble the seam when the scene lacks texture.
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["PanoramaStitcher"]


class PanoramaStitcher:
    """Alpha-blends two views across a fixed overlap column."""

    def __init__(self, seam_blend_width: int = 20) -> None:
        """
        Args:
            seam_blend_width: Width of the cross-fade, in pixels. It doubles
                as the overlap between the two views, so a wider seam yields a
                narrower panorama. Zero produces a hard cut.
        """
        if seam_blend_width < 0:
            raise ValueError("seam_blend_width must be >= 0")
        self.seam_blend_width = seam_blend_width

    def output_width(self, left_width: int, right_width: int) -> int:
        """Width of the panorama :meth:`stitch` will produce."""
        overlap = min(self.seam_blend_width, left_width, right_width)
        return left_width + right_width - overlap

    def stitch(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        """Blend ``left`` and ``right`` into a single wide image.

        Raises:
            ValueError: if the two views differ in height, channel count or
                dtype.
        """
        if left.shape[0] != right.shape[0]:
            raise ValueError(f"view heights differ: {left.shape[0]} vs {right.shape[0]}")
        if left.shape[2:] != right.shape[2:]:
            raise ValueError(f"view channel counts differ: {left.shape} vs {right.shape}")
        if left.dtype != right.dtype:
            raise ValueError(f"view dtypes differ: {left.dtype} vs {right.dtype}")

        height, left_width = left.shape[:2]
        right_width = right.shape[1]
        overlap = min(self.seam_blend_width, left_width, right_width)
        total = left_width + right_width - overlap

        panorama = np.zeros((height, total, *left.shape[2:]), dtype=left.dtype)
        panorama[:, :left_width] = left
        panorama[:, left_width:] = right[:, overlap:]

        if overlap > 0:
            # Ramp from 1.0 to 0.0 across the seam so the left view fades out
            # exactly as the right fades in. Broadcasting the ramp over the
            # channel axis keeps this a single vectorised pass.
            ramp = np.linspace(1.0, 0.0, overlap, dtype=np.float32)
            ramp = ramp.reshape(1, overlap, *([1] * (left.ndim - 2)))
            seam_left = left[:, left_width - overlap :].astype(np.float32)
            seam_right = right[:, :overlap].astype(np.float32)
            blended = seam_left * ramp + seam_right * (1.0 - ramp)
            panorama[:, left_width - overlap : left_width] = blended.astype(left.dtype)

        return panorama

    @staticmethod
    def overlay_depth(panorama: np.ndarray, depth_color: np.ndarray, alpha: float = 0.5) -> np.ndarray:
        """Composite a colourised depth map over the panorama.

        The disparity map is computed in the left camera's frame, so it is
        anchored to the left edge and resized to that view's height. It is
        never stretched across the full panorama, which would imply depth
        information exists where no second view overlaps.
        """
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be in 0.0..1.0")

        out = panorama.copy()
        height = out.shape[0]
        target_width = min(depth_color.shape[1], out.shape[1])

        overlay = depth_color[:, :target_width]
        if overlay.shape[0] != height:
            overlay = cv2.resize(overlay, (target_width, height), interpolation=cv2.INTER_NEAREST)

        region = out[:, :target_width]
        out[:, :target_width] = cv2.addWeighted(region, 1.0 - alpha, overlay, alpha, 0.0)
        return out
