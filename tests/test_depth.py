"""Stereo disparity and the depth conversion built on it."""

from __future__ import annotations

import numpy as np
import pytest

from vectra180.config import DepthConfig
from vectra180.imaging.depth import DISPARITY_SCALE, DisparityResult, StereoDepthEngine, disparity_to_depth

# -- parameter coercion ------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, 16), (1, 16), (15, 16), (16, 16), (31, 16), (64, 64), (70, 64), (-8, 16)],
)
def test_num_disparities_is_snapped_to_a_multiple_of_16(requested: int, expected: int) -> None:
    """OpenCV raises on anything else rather than rounding, so this must."""
    assert StereoDepthEngine.normalize_params(requested, 5)[0] == expected


@pytest.mark.parametrize(("requested", "expected"), [(0, 3), (3, 3), (4, 5), (5, 5), (8, 9), (-1, 3)])
def test_block_size_is_forced_odd_and_at_least_three(requested: int, expected: int) -> None:
    assert StereoDepthEngine.normalize_params(64, requested)[1] == expected


# -- matching ----------------------------------------------------------------


def test_compute_returns_all_three_views(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    left, right = stereo_pair
    result = StereoDepthEngine(DepthConfig()).compute(left, right)

    assert result.raw.shape == left.shape[:2]
    assert result.raw.dtype == np.int16
    assert result.normalized.shape == left.shape[:2]
    assert result.normalized.dtype == np.uint8
    assert result.colorized.shape == (*left.shape[:2], 3)


def test_compute_accepts_grayscale_input(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    import cv2

    left, right = stereo_pair
    grey = [cv2.cvtColor(view, cv2.COLOR_BGR2GRAY) for view in (left, right)]

    assert StereoDepthEngine().compute(*grey).raw.shape == left.shape[:2]


def test_compute_rejects_a_size_mismatch(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    left, right = stereo_pair
    with pytest.raises(ValueError, match="size mismatch"):
        StereoDepthEngine().compute(left, right[:, :-4])


def test_matcher_is_reused_until_parameters_change(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    """Rebuilding reallocates SGBM's aggregation buffers on every frame."""
    left, right = stereo_pair
    engine = StereoDepthEngine()

    engine.compute(left, right)
    first = engine._matcher
    engine.compute(left, right)
    assert engine._matcher is first

    engine.compute(left, right, num_disparities=32)
    assert engine._matcher is not first


def test_shifted_pair_yields_the_expected_disparity() -> None:
    """A known 8px shift must come back as a disparity of 8.

    This is the one test that would catch the two views being handed to the
    matcher the wrong way round, which produces a plausible-looking but
    entirely negative map.
    """
    rng = np.random.default_rng(seed=3)
    texture = rng.integers(0, 255, size=(96, 200), dtype=np.uint8)
    left = texture
    right = np.roll(texture, -8, axis=1)

    result = StereoDepthEngine().compute(left, right, num_disparities=32, block_size=5)

    valid = result.raw[result.raw > 0] / DISPARITY_SCALE
    assert np.median(valid) == pytest.approx(8.0, abs=1.0)


def test_coverage_counts_only_matched_pixels() -> None:
    raw = np.array([[16, -16], [32, -1]], dtype=np.int16)
    result = DisparityResult(raw=raw, normalized=raw.astype(np.uint8), colorized=raw.astype(np.uint8))

    assert result.coverage == 0.5


def test_a_textureless_pair_reports_low_coverage() -> None:
    """Flat sky has nothing to match; that is a coverage figure, not a depth."""
    blank = np.full((64, 128), 128, dtype=np.uint8)

    assert StereoDepthEngine().compute(blank, blank).coverage < 0.5


# -- depth conversion --------------------------------------------------------


def test_disparity_to_depth_follows_the_triangle() -> None:
    """Z = f*B/d, with d in whole pixels once the 1/16 scaling is undone."""
    raw = np.array([[8 * DISPARITY_SCALE]], dtype=np.int16)

    depth = disparity_to_depth(raw, focal_px=400.0, baseline=0.06)

    assert depth[0, 0] == pytest.approx(400.0 * 0.06 / 8.0, rel=1e-4)


def test_unmatched_pixels_become_infinite_not_zero() -> None:
    """Zero would read as 'touching the lens' -- the opposite of the truth."""
    raw = np.array([[-16, 0, 160]], dtype=np.int16)

    depth = disparity_to_depth(raw, focal_px=400.0, baseline=0.06)

    assert np.isinf(depth[0, 0])
    assert np.isinf(depth[0, 1])
    assert np.isfinite(depth[0, 2])


def test_depth_is_inversely_proportional_to_disparity() -> None:
    raw = np.array([[16, 32, 64]], dtype=np.int16)

    depth = disparity_to_depth(raw, focal_px=100.0, baseline=1.0)

    assert depth[0, 0] > depth[0, 1] > depth[0, 2]
    assert depth[0, 0] == pytest.approx(depth[0, 1] * 2, rel=1e-5)


def test_depth_output_is_float32() -> None:
    """Float64 would double the bandwidth of every depth frame on a CM5."""
    depth = disparity_to_depth(np.array([[160]], dtype=np.int16), 400.0, 0.06)
    assert depth.dtype == np.float32


# -- downscaling -------------------------------------------------------------


def test_downscale_hits_the_working_width_and_keeps_aspect() -> None:
    engine = StereoDepthEngine(DepthConfig(working_width=160))
    image = np.zeros((360, 640, 3), dtype=np.uint8)

    out = engine.downscale(image)

    assert out.shape[1] == 160
    assert out.shape[0] == 90


def test_downscale_never_upscales() -> None:
    """Upscaling adds matching cost without adding detail."""
    engine = StereoDepthEngine(DepthConfig(working_width=640))
    image = np.zeros((64, 320, 3), dtype=np.uint8)

    assert engine.downscale(image) is image


def test_downscale_keeps_at_least_one_row() -> None:
    """A very wide, short frame must not round its height away to zero."""
    engine = StereoDepthEngine(DepthConfig(working_width=16))
    image = np.zeros((4, 2000, 3), dtype=np.uint8)

    assert engine.downscale(image).shape[0] >= 1


def test_downscaled_pair_still_matches(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    """The path depth-on-demand actually takes: shrink, then match."""
    engine = StereoDepthEngine(DepthConfig(working_width=128))
    left, right = (engine.downscale(view) for view in stereo_pair)

    result = engine.compute(left, right)

    assert result.raw.shape == left.shape[:2]
