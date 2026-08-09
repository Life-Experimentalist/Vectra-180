"""Loop recording: encoders, segmentation, retention, incident locking."""

from vectra180.recorder.incident import Incident, IncidentDetector
from vectra180.recorder.segmenter import RecorderStats, SegmentRecorder
from vectra180.recorder.storage import (
    ClipInfo,
    StorageStats,
    delete_clip,
    ensure_directories,
    list_clips,
    protect_clip,
    prune,
    resolve_clip,
    safe_clip_name,
    storage_stats,
)
from vectra180.recorder.writer import FFmpegWriter, FrameWriter, OpenCVWriter, create_writer, ffmpeg_path

__all__ = [
    "ClipInfo",
    "FFmpegWriter",
    "FrameWriter",
    "Incident",
    "IncidentDetector",
    "OpenCVWriter",
    "RecorderStats",
    "SegmentRecorder",
    "StorageStats",
    "create_writer",
    "delete_clip",
    "ensure_directories",
    "ffmpeg_path",
    "list_clips",
    "protect_clip",
    "prune",
    "resolve_clip",
    "safe_clip_name",
    "storage_stats",
]
