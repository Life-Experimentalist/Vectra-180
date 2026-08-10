"""Splitting a raw dual-fisheye frame into its parts.

A raw frame from the module is laid out as::

    | metadata | left fisheye | right fisheye |
    |<- 30px ->|<-  (W-30)/2  ->|<-  (W-30)/2  ->|

The metadata strip carries the IMU payload (see
:mod:`vectra180.telemetry.decoder`) and must be removed before the image is
shown or encoded, or it appears as a column of noise down the left edge.
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["crop_to_even", "downscale", "split_stereo", "strip_metadata"]


def downscale(image: np.ndarray, factor: float) -> np.ndarray:
    """Shrink an image by ``factor``, or hand it back untouched at ``1.0``.

    Encoding cost scales with pixel count, and a module that only offers one
    mode leaves no other way to buy headroom: this camera answers every
    resolution request with 4000x1200, which is 2.6 times the pixels the CM5
    was measured against. Half-scale is a quarter of the work.

    ``INTER_AREA`` averages the pixels it discards rather than sampling one of
    them, which is what keeps number plates and lane markings readable instead
    of aliased into noise.
    """
    if factor >= 1.0:
        return image
    height, width = image.shape[:2]
    size = (max(2, round(width * factor)), max(2, round(height * factor)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def crop_to_even(image: np.ndarray) -> np.ndarray:
    """Trim at most one row and column so both dimensions are even.

    H.264 in ``yuv420p`` subsamples chroma by two, so libx264 refuses odd
    dimensions outright. Cropping a single line is invisible; letting the
    encoder fail mid-drive is not.
    """
    height, width = image.shape[:2]
    return image[: height - (height % 2), : width - (width % 2)]


def strip_metadata(frame: np.ndarray, metadata_width: int) -> tuple[np.ndarray, np.ndarray | None]:
    """Separate the metadata strip from the image area.

    Args:
        frame: Raw frame straight from the capture device.
        metadata_width: Column count to remove. ``0`` disables stripping,
            which is correct for modules that embed no telemetry.

    Returns:
        ``(image, strip)``. ``strip`` is ``None`` when nothing was removed.

    Raises:
        ValueError: if the strip would consume the whole frame.
    """
    if metadata_width <= 0:
        return frame, None
    if metadata_width >= frame.shape[1]:
        raise ValueError(f"metadata_width {metadata_width} is not narrower than the frame ({frame.shape[1]}px)")
    return frame[:, metadata_width:], frame[:, :metadata_width]


def split_stereo(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Halve a side-by-side image into left and right views.

    An odd width drops its centre column so both halves come out equal, which
    every downstream stereo operation requires.
    """
    width = image.shape[1]
    if width < 2:
        raise ValueError("frame is too narrow to split into a stereo pair")
    half = width // 2
    return image[:, :half], image[:, width - half :]
