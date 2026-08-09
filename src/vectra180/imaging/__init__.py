"""Image processing: dewarping, stereo depth, stitching, stabilisation, HUD."""

from vectra180.imaging.depth import DisparityResult, StereoDepthEngine, disparity_to_depth
from vectra180.imaging.dewarper import FisheyeDewarper
from vectra180.imaging.hud import HUDRenderer, HUDStatus
from vectra180.imaging.layout import crop_to_even, split_stereo, strip_metadata
from vectra180.imaging.stabilizer import HorizonStabilizer
from vectra180.imaging.stitcher import PanoramaStitcher

__all__ = [
    "DisparityResult",
    "FisheyeDewarper",
    "HUDRenderer",
    "HUDStatus",
    "HorizonStabilizer",
    "PanoramaStitcher",
    "StereoDepthEngine",
    "crop_to_even",
    "disparity_to_depth",
    "split_stereo",
    "strip_metadata",
]
