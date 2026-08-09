"""Stereo disparity via Semi-Global Block Matching.

Disparity ``d`` is how far a point shifts horizontally between the two views.
Where the two lenses are rectified and their separation ``B`` and focal length
``f`` are known, depth follows from similar triangles:

.. math:: Z = \\frac{f \\cdot B}{d}

The module's lens separation is fixed but not published to the host, and the
intrinsics used here are approximate (see :mod:`vectra180.imaging.dewarper`),
so :func:`disparity_to_depth` needs both supplied by the caller and returns
depth in whatever unit ``baseline`` was given in. Without a calibration the
disparity map is a *relative* depth cue -- nearer is brighter -- not a
distance measurement.

Cost on a Compute Module 5: SGBM scales with pixels times disparity range, and
the CM5's four Cortex-A76 cores manage only low single-digit frames per second
on a full 2560x720 pair. That is why the recording path never calls this and
:class:`StereoDepthEngine` downscales to ``depth.working_width`` first.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from vectra180.config import DepthConfig

__all__ = ["DisparityResult", "StereoDepthEngine", "disparity_to_depth"]

#: SGBM returns disparity as a 16-bit signed integer scaled by 16.
DISPARITY_SCALE = 16.0


@dataclass(frozen=True)
class DisparityResult:
    """The three views of one disparity computation."""

    #: Raw ``CV_16S`` output, in 1/16 pixel units. Use this for measurement.
    raw: np.ndarray
    #: ``CV_8U`` min-max normalised map, for display.
    normalized: np.ndarray
    #: ``normalized`` through COLORMAP_JET: red is near, blue is far.
    colorized: np.ndarray

    @property
    def coverage(self) -> float:
        """Fraction of pixels that got a valid match, in 0..1.

        SGBM writes negative values where matching failed -- in textureless
        sky, in the occluded band at the left edge, and past the disparity
        range. Low coverage means the parameters or the rectification are
        wrong, not that the scene is far away.
        """
        return float(np.count_nonzero(self.raw > 0) / self.raw.size)


def disparity_to_depth(raw_disparity: np.ndarray, focal_px: float, baseline: float) -> np.ndarray:
    """Convert raw SGBM disparity to depth via ``Z = f*B/d``.

    Args:
        raw_disparity: ``CV_16S`` output from :meth:`StereoDepthEngine.compute`.
        focal_px: Focal length in pixels, at the resolution the disparity was
            computed at.
        baseline: Lens separation. The result carries this unit.

    Returns:
        Float32 depth, with ``inf`` where no match was found.
    """
    disparity = raw_disparity.astype(np.float32) / DISPARITY_SCALE
    depth = np.full(disparity.shape, np.inf, dtype=np.float32)
    valid = disparity > 0
    depth[valid] = (focal_px * baseline) / disparity[valid]
    return depth


class StereoDepthEngine:
    """A reusable SGBM matcher.

    The matcher is rebuilt only when parameters change, because
    ``cv2.StereoSGBM.create`` allocates its aggregation buffers up front.
    """

    def __init__(self, config: DepthConfig | None = None) -> None:
        self._config = config or DepthConfig()
        self._matcher: cv2.StereoSGBM | None = None
        self._params: tuple[int, int, int] | None = None

    @staticmethod
    def normalize_params(num_disparities: int, block_size: int) -> tuple[int, int]:
        """Coerce parameters into the values SGBM requires.

        ``numDisparities`` must be a positive multiple of 16 and ``blockSize``
        must be odd; OpenCV raises rather than rounding, so it is done here.
        """
        num_disparities = max(16, (num_disparities // 16) * 16)
        block_size = max(3, block_size | 1)
        return num_disparities, block_size

    def _matcher_for(self, num_disparities: int, block_size: int, uniqueness_ratio: int) -> cv2.StereoSGBM:
        params = (num_disparities, block_size, uniqueness_ratio)
        if self._matcher is None or params != self._params:
            channels = 3
            self._matcher = cv2.StereoSGBM.create(
                minDisparity=0,
                numDisparities=num_disparities,
                blockSize=block_size,
                # P1 and P2 penalise disparity changes between neighbours: P1
                # for a step of one, P2 for anything larger. OpenCV's
                # documented starting point is 8*c*b^2 and 32*c*b^2.
                P1=8 * channels * block_size**2,
                P2=32 * channels * block_size**2,
                disp12MaxDiff=1,
                uniquenessRatio=uniqueness_ratio,
                speckleWindowSize=100,
                speckleRange=3,
                # 3-way aggregation costs about a third of the memory of the
                # full 8-path mode at a small quality loss -- the right trade
                # on a 4-core CM5.
                mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
            )
            self._params = params
        return self._matcher

    def compute(
        self,
        left: np.ndarray,
        right: np.ndarray,
        *,
        num_disparities: int | None = None,
        block_size: int | None = None,
        uniqueness_ratio: int | None = None,
    ) -> DisparityResult:
        """Match a rectified stereo pair.

        Colour input is converted to grayscale; the two images must be the
        same size.
        """
        if left.shape[:2] != right.shape[:2]:
            raise ValueError(f"stereo pair size mismatch: {left.shape[:2]} vs {right.shape[:2]}")

        left_gray = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY) if left.ndim == 3 else left
        right_gray = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY) if right.ndim == 3 else right

        disparities, block = self.normalize_params(
            self._config.num_disparities if num_disparities is None else num_disparities,
            self._config.block_size if block_size is None else block_size,
        )
        uniqueness = self._config.uniqueness_ratio if uniqueness_ratio is None else uniqueness_ratio

        raw = self._matcher_for(disparities, block, uniqueness).compute(left_gray, right_gray)
        # OpenCV allocates the destination when it is None; the bundled stub
        # does not model that overload.
        normalized = cv2.normalize(  # type: ignore[call-overload]
            raw, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U
        )
        colorized = cv2.applyColorMap(normalized, cv2.COLORMAP_JET)
        return DisparityResult(raw=raw, normalized=normalized, colorized=colorized)

    def downscale(self, image: np.ndarray) -> np.ndarray:
        """Shrink an image to ``depth.working_width``, preserving aspect.

        Images already at or below the working width are returned unchanged --
        upscaling would only add matching cost without adding detail.
        """
        height, width = image.shape[:2]
        target = self._config.working_width
        if width <= target:
            return image
        scale = target / width
        return cv2.resize(image, (target, max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
