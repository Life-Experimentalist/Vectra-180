"""Frame geometry: splitting, dewarping, levelling and stitching."""

from __future__ import annotations

import math

import numpy as np
import pytest

from vectra180.imaging import FisheyeDewarper, HorizonStabilizer, PanoramaStitcher
from vectra180.imaging.layout import crop_to_even, split_stereo, strip_metadata

from .conftest import FRAME_HEIGHT, FRAME_WIDTH, METADATA_WIDTH

# -- layout ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ((64, 320), (64, 320)),
        ((65, 320), (64, 320)),
        ((64, 321), (64, 320)),
        ((65, 321), (64, 320)),
    ],
)
def test_crop_to_even_trims_at_most_one_line(shape: tuple[int, int], expected: tuple[int, int]) -> None:
    """libx264 in yuv420p refuses odd dimensions outright."""
    assert crop_to_even(np.zeros(shape, dtype=np.uint8)).shape == expected


def test_crop_to_even_preserves_channels() -> None:
    assert crop_to_even(np.zeros((65, 321, 3), dtype=np.uint8)).shape == (64, 320, 3)


def test_strip_metadata_splits_at_the_boundary(raw_frame: np.ndarray) -> None:
    image, strip = strip_metadata(raw_frame, METADATA_WIDTH)

    assert strip is not None
    assert strip.shape[1] == METADATA_WIDTH
    assert image.shape[1] == FRAME_WIDTH - METADATA_WIDTH
    # The two parts must reconstruct the original exactly -- no pixel is
    # duplicated across the boundary or dropped at it.
    assert np.array_equal(np.hstack([strip, image]), raw_frame)


def test_strip_metadata_is_a_no_op_at_zero(raw_frame: np.ndarray) -> None:
    """Modules without an IMU embed nothing, so nothing may be cropped."""
    image, strip = strip_metadata(raw_frame, 0)

    assert strip is None
    assert image is raw_frame


def test_strip_metadata_rejects_consuming_the_frame(raw_frame: np.ndarray) -> None:
    with pytest.raises(ValueError, match="not narrower"):
        strip_metadata(raw_frame, FRAME_WIDTH)


def test_split_stereo_halves_an_even_width() -> None:
    image = np.zeros((8, 100, 3), dtype=np.uint8)
    left, right = split_stereo(image)

    assert left.shape[1] == right.shape[1] == 50


def test_split_stereo_drops_the_centre_column_when_odd() -> None:
    """Downstream matching requires the halves to be the same width."""
    image = np.arange(101, dtype=np.uint8).reshape(1, 101)
    left, right = split_stereo(image)

    assert left.shape[1] == right.shape[1] == 50
    assert left[0, -1] == 49
    assert right[0, 0] == 51  # column 50 is gone


def test_split_stereo_rejects_a_degenerate_frame() -> None:
    with pytest.raises(ValueError, match="too narrow"):
        split_stereo(np.zeros((4, 1), dtype=np.uint8))


# -- dewarper ----------------------------------------------------------------


def test_dewarp_preserves_shape_and_dtype(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    left, _ = stereo_pair
    out = FisheyeDewarper().dewarp(left)

    assert out.shape == left.shape
    assert out.dtype == left.dtype


def test_dewarp_maps_are_cached_per_resolution(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    """Building the maps costs tens of ms; applying them costs about one."""
    left, _ = stereo_pair
    dewarper = FisheyeDewarper()

    dewarper.dewarp(left)
    dewarper.dewarp(left)
    assert dewarper.cached_resolutions == 1

    dewarper.dewarp(left[: FRAME_HEIGHT // 2])
    assert dewarper.cached_resolutions == 2


def test_changing_focal_scale_requires_invalidation(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    left, _ = stereo_pair
    dewarper = FisheyeDewarper()
    dewarper.dewarp(left)

    dewarper.invalidate_cache()

    assert dewarper.cached_resolutions == 0


def test_focal_scale_is_part_of_the_cache_key(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    """Otherwise a changed focal length would silently reuse the old maps."""
    left, _ = stereo_pair
    dewarper = FisheyeDewarper()
    first = dewarper.dewarp(left)

    dewarper.focal_scale = 0.9
    second = dewarper.dewarp(left)

    assert dewarper.cached_resolutions == 2
    assert not np.array_equal(first, second)


# -- stabilizer --------------------------------------------------------------


def test_zero_roll_returns_the_frame_untouched(raw_frame: np.ndarray) -> None:
    """A sub-pixel rotation would cost a full warp and blur the image."""
    assert HorizonStabilizer.stabilize(raw_frame, 0.0) is raw_frame
    assert HorizonStabilizer.stabilize(raw_frame, 0.001) is raw_frame


def test_stabilize_rotates_and_keeps_the_shape(raw_frame: np.ndarray) -> None:
    out = HorizonStabilizer.stabilize(raw_frame, 10.0)

    assert out.shape == raw_frame.shape
    assert not np.array_equal(out, raw_frame)


def test_cover_scale_is_unity_when_level() -> None:
    assert HorizonStabilizer.cover_scale(320, 64, 0.0) == pytest.approx(1.0)


def test_cover_scale_grows_with_roll() -> None:
    """The zoom must hide the triangular gaps a rotation opens at the corners."""
    scales = [HorizonStabilizer.cover_scale(320, 64, angle) for angle in (0.0, 5.0, 15.0, 30.0)]
    assert scales == sorted(scales)
    assert scales[-1] > 1.0


def test_cover_scale_is_symmetric() -> None:
    assert HorizonStabilizer.cover_scale(320, 64, -12.0) == pytest.approx(HorizonStabilizer.cover_scale(320, 64, 12.0))


def test_cover_scale_handles_a_degenerate_frame() -> None:
    assert HorizonStabilizer.cover_scale(0, 0, 30.0) == 1.0


def test_covering_zoom_leaves_no_border(raw_frame: np.ndarray) -> None:
    """The whole point of cover_scale: no black wedges in a recorded clip."""
    frame = np.full_like(raw_frame, 255)
    out = HorizonStabilizer.stabilize(frame, 20.0)

    assert out.min() > 0


def test_explicit_scale_overrides_the_computed_zoom(raw_frame: np.ndarray) -> None:
    wide = HorizonStabilizer.stabilize(raw_frame, 20.0, scale=1.0)
    zoomed = HorizonStabilizer.stabilize(raw_frame, 20.0)

    assert not np.array_equal(wide, zoomed)


# -- stitcher ----------------------------------------------------------------


def _views(width: int = 40, height: int = 8) -> tuple[np.ndarray, np.ndarray]:
    left = np.full((height, width, 3), 40, dtype=np.uint8)
    right = np.full((height, width, 3), 200, dtype=np.uint8)
    return left, right


def test_stitch_width_matches_the_prediction() -> None:
    stitcher = PanoramaStitcher(seam_blend_width=10)
    left, right = _views()

    panorama = stitcher.stitch(left, right)

    assert panorama.shape[1] == stitcher.output_width(left.shape[1], right.shape[1]) == 70


def test_a_zero_width_seam_is_a_hard_cut() -> None:
    stitcher = PanoramaStitcher(seam_blend_width=0)
    left, right = _views()

    panorama = stitcher.stitch(left, right)

    assert panorama.shape[1] == 80
    assert panorama[0, 39, 0] == 40
    assert panorama[0, 40, 0] == 200


def test_seam_ramps_monotonically_between_the_views() -> None:
    """A non-monotonic ramp shows up as a visible band down the join."""
    stitcher = PanoramaStitcher(seam_blend_width=10)
    left, right = _views()

    panorama = stitcher.stitch(left, right)
    seam = panorama[0, 30:40, 0].astype(int)

    assert seam[0] < seam[-1]
    assert np.all(np.diff(seam) >= 0)
    assert seam.min() >= 40
    assert seam.max() <= 200


def test_seam_wider_than_a_view_is_clamped() -> None:
    stitcher = PanoramaStitcher(seam_blend_width=1000)
    left, right = _views(width=20)

    assert stitcher.stitch(left, right).shape[1] == 20


def test_stitch_rejects_mismatched_views() -> None:
    stitcher = PanoramaStitcher()
    left, right = _views()

    with pytest.raises(ValueError, match="heights differ"):
        stitcher.stitch(left, right[:4])
    with pytest.raises(ValueError, match="channel counts differ"):
        stitcher.stitch(left, right[:, :, 0])
    with pytest.raises(ValueError, match="dtypes differ"):
        stitcher.stitch(left, right.astype(np.float32))


def test_negative_seam_width_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        PanoramaStitcher(seam_blend_width=-1)


def test_depth_overlay_stays_anchored_to_the_left_view() -> None:
    """Stretching it across the panorama would imply depth where there is none."""
    panorama = np.full((8, 100, 3), 10, dtype=np.uint8)
    depth = np.full((8, 40, 3), 250, dtype=np.uint8)

    out = PanoramaStitcher.overlay_depth(panorama, depth, alpha=0.5)

    assert out[0, 20, 0] > 10  # inside the left view: blended
    assert out[0, 80, 0] == 10  # past it: untouched


def test_depth_overlay_is_resized_to_the_panorama_height() -> None:
    panorama = np.full((16, 60, 3), 10, dtype=np.uint8)
    depth = np.full((4, 30, 3), 250, dtype=np.uint8)

    assert PanoramaStitcher.overlay_depth(panorama, depth).shape == panorama.shape


def test_depth_overlay_alpha_is_bounded() -> None:
    panorama = np.zeros((4, 10, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match=r"0\.0\.\.1\.0"):
        PanoramaStitcher.overlay_depth(panorama, panorama, alpha=1.5)


def test_depth_overlay_does_not_mutate_its_input() -> None:
    panorama = np.full((8, 40, 3), 10, dtype=np.uint8)
    depth = np.full((8, 40, 3), 250, dtype=np.uint8)

    PanoramaStitcher.overlay_depth(panorama, depth)

    assert panorama.max() == 10


def test_dewarped_pair_still_stitches(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    """The two stages compose: this is the panorama path the UI serves."""
    dewarper = FisheyeDewarper()
    left, right = (dewarper.dewarp(view) for view in stereo_pair)

    panorama = PanoramaStitcher(seam_blend_width=8).stitch(left, right)

    assert panorama.shape[0] == FRAME_HEIGHT
    assert panorama.shape[1] == left.shape[1] + right.shape[1] - 8


def test_stabilized_panorama_survives_the_full_chain(stereo_pair: tuple[np.ndarray, np.ndarray]) -> None:
    dewarper = FisheyeDewarper()
    left, right = (dewarper.dewarp(view) for view in stereo_pair)
    panorama = PanoramaStitcher().stitch(left, right)

    levelled = crop_to_even(HorizonStabilizer.stabilize(panorama, math.degrees(0.1)))

    assert levelled.shape[0] % 2 == 0
    assert levelled.shape[1] % 2 == 0
