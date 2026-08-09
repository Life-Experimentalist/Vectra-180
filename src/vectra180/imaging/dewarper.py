"""Fisheye-to-rectilinear projection.

The dual-fisheye module produces two hemispherical images. Stereo matching
needs straight lines to stay straight, so each half is remapped through
OpenCV's fisheye model before it reaches the matcher.

The intrinsics here are an *approximation* derived from the frame geometry, not
a calibration. They are good enough that vertical structures line up and SGBM
converges, which is what depth-on-demand needs. They are not good enough for
metric distance: absolute depth requires a real checkerboard calibration and a
measured baseline. ``vectra180 calibrate`` is not implemented, and no part of
this project claims metric accuracy.
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["DEFAULT_DISTORTION", "FisheyeDewarper"]

#: Fisheye distortion coefficients (k1..k4) for the stock lens.
#:
#: A negative k1 counteracts barrel distortion; k2 trims the result at the
#: edges. These were fitted by eye against the module's test footage.
DEFAULT_DISTORTION = (-0.25, 0.05, 0.0, 0.0)


class FisheyeDewarper:
    """Caches and applies an undistortion remap per (height, width, scale).

    Building the maps costs tens of milliseconds; applying them costs about a
    millisecond. Since the resolution is fixed for a whole session, the maps
    are built once and reused.
    """

    def __init__(self, focal_scale: float = 0.5, distortion: tuple[float, float, float, float] = DEFAULT_DISTORTION):
        self.focal_scale = focal_scale
        self.distortion = distortion
        self._map_cache: dict[tuple[int, int, float], tuple[np.ndarray, np.ndarray]] = {}

    def dewarp(self, image: np.ndarray) -> np.ndarray:
        """Flatten one fisheye image into a rectilinear projection."""
        height, width = image.shape[:2]
        key = (height, width, self.focal_scale)
        maps = self._map_cache.get(key)
        if maps is None:
            maps = self._compute_maps(height, width)
            self._map_cache[key] = maps
        map1, map2 = maps
        return cv2.remap(image, map1, map2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    def _compute_maps(self, height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
        """Build the remap tables for one resolution.

        The focal length is estimated as ``focal_scale * width``. For an
        equidistant fisheye covering 180 degrees across the full frame width,
        f = width / pi ~= 0.318 * width; the 0.5 default is deliberately longer
        because the stock lens does not fill the frame edge to edge.
        """
        focal = width * self.focal_scale

        camera_matrix = np.array(
            [
                [focal, 0.0, width / 2.0],
                [0.0, focal, height / 2.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        distortion = np.array(self.distortion, dtype=np.float64)

        # balance=0 keeps only pixels valid in the source, trading field of
        # view for the absence of black wedges at the corners.
        new_matrix = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            camera_matrix, distortion, (width, height), np.eye(3), balance=0.0
        )
        # CV_16SC2 fixed-point maps are roughly twice as fast as float maps in
        # cv2.remap and cost half the memory, which matters on a CM5.
        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            camera_matrix, distortion, np.eye(3), new_matrix, (width, height), cv2.CV_16SC2
        )
        return map1, map2

    def invalidate_cache(self) -> None:
        """Drop cached maps. Call after changing ``focal_scale``."""
        self._map_cache.clear()

    @property
    def cached_resolutions(self) -> int:
        return len(self._map_cache)
