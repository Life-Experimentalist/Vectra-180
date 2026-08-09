"""Clip inventory and retention.

Layout under ``recording.directory``::

    normal/   VEC_20260809_142530.mp4   loop footage, pruned oldest-first
              VEC_20260809_142530.json  telemetry sidecar
    events/   VEC_20260809_142530.mp4   incident clips, never pruned by the loop

An SD card in a car fills up in hours, so pruning is not optional and must
never touch ``events/`` -- the whole point of locking a clip is that the loop
cannot reclaim it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vectra180.config import RecordingConfig

__all__ = [
    "CLIP_NAME_PATTERN",
    "ClipInfo",
    "StorageStats",
    "delete_clip",
    "ensure_directories",
    "list_clips",
    "parse_clip_time",
    "protect_clip",
    "prune",
    "resolve_clip",
    "safe_clip_name",
    "storage_stats",
]

log = logging.getLogger(__name__)

#: Segment file names. Anything else in the directory is ignored, so a user's
#: own files are never deleted by the pruner.
#:
#: Both patterns here end in ``\Z`` rather than ``$``, which in Python also
#: matches before a trailing newline -- and a name is not a name if a newline
#: can ride along on the end of it.
CLIP_NAME_PATTERN = re.compile(r"^VEC_(\d{8})_(\d{6})(?:_[A-Za-z0-9-]+)?\.(mp4|mkv|avi)\Z")

#: Names the HTTP API will accept. Deliberately stricter than the filesystem:
#: no separators, no dots beyond the extension, so no path can escape the
#: recording directory.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]{2,4}\Z")

_CATEGORY_DIRS = ("normal", "events")


def ensure_directories(config: RecordingConfig) -> None:
    """Create the recording tree if it does not exist."""
    for name in _CATEGORY_DIRS:
        (config.directory / name).mkdir(parents=True, exist_ok=True)


def parse_clip_time(name: str) -> datetime | None:
    """Recover the start time encoded in a clip's file name.

    File names are the source of truth for ordering because mtime is
    unreliable on a Pi: without a battery-backed clock every file written
    before NTP syncs carries a bogus timestamp.
    """
    match = CLIP_NAME_PATTERN.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class ClipInfo:
    """One recorded segment."""

    path: Path
    name: str
    #: ``normal`` or ``events``.
    category: str
    size_bytes: int
    started_at: datetime | None
    duration_seconds: float

    @property
    def protected(self) -> bool:
        return self.category == "events"

    @property
    def sidecar(self) -> Path:
        return self.path.with_suffix(".json")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "size_bytes": self.size_bytes,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "protected": self.protected,
        }


def _read_duration(sidecar: Path) -> float:
    """Read a segment's duration from its sidecar.

    Cheaper and more reliable than probing the container -- the sidecar is
    written by the recorder that produced the file. A card pulled mid-write
    leaves half-written sidecars behind, so every shape of garbage has to read
    as "duration unknown" rather than break the whole listing.
    """
    try:
        with sidecar.open("r", encoding="utf-8") as handle:
            return float(json.load(handle)["duration_seconds"])
    except (OSError, ValueError, TypeError, KeyError):
        return 0.0


def list_clips(config: RecordingConfig, *, category: str | None = None) -> list[ClipInfo]:
    """Return every recognised clip, newest first."""
    clips: list[ClipInfo] = []
    for folder in _CATEGORY_DIRS:
        if category is not None and folder != category:
            continue
        directory = config.directory / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file() or not CLIP_NAME_PATTERN.match(path.name):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            clips.append(
                ClipInfo(
                    path=path,
                    name=path.name,
                    category=folder,
                    size_bytes=size,
                    started_at=parse_clip_time(path.name),
                    duration_seconds=_read_duration(path.with_suffix(".json")),
                )
            )
    clips.sort(key=lambda clip: clip.name, reverse=True)
    return clips


@dataclass(frozen=True)
class StorageStats:
    """Disk accounting for the status endpoint."""

    total_bytes: int
    free_bytes: int
    normal_bytes: int
    event_bytes: int
    normal_clips: int
    event_clips: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "normal_bytes": self.normal_bytes,
            "event_bytes": self.event_bytes,
            "normal_clips": self.normal_clips,
            "event_clips": self.event_clips,
        }


def storage_stats(config: RecordingConfig) -> StorageStats:
    """Measure the recording volume and the space each category occupies."""
    ensure_directories(config)
    usage = shutil.disk_usage(config.directory)
    clips = list_clips(config)
    normal = [clip for clip in clips if clip.category == "normal"]
    events = [clip for clip in clips if clip.category == "events"]
    return StorageStats(
        total_bytes=usage.total,
        free_bytes=usage.free,
        normal_bytes=sum(clip.size_bytes for clip in normal),
        event_bytes=sum(clip.size_bytes for clip in events),
        normal_clips=len(normal),
        event_clips=len(events),
    )


def _remove(clip: ClipInfo) -> bool:
    """Delete a clip and its sidecar. Returns False if the video survived."""
    try:
        clip.path.unlink()
    except OSError as exc:
        log.warning("could not delete %s: %s", clip.path, exc)
        return False
    clip.sidecar.unlink(missing_ok=True)
    return True


def prune(config: RecordingConfig, *, keep: Iterable[Path] = ()) -> list[Path]:
    """Delete the oldest loop clips until the budget and free space are met.

    Args:
        keep: Paths never to delete, used to protect the segment currently
            being written.

    Returns:
        The paths removed.
    """
    ensure_directories(config)
    protected = {path.resolve() for path in keep}
    removed: list[Path] = []

    # Oldest first: names sort chronologically by construction.
    normal = sorted(list_clips(config, category="normal"), key=lambda clip: clip.name)
    total = sum(clip.size_bytes for clip in normal)
    free = shutil.disk_usage(config.directory).free

    for clip in normal:
        if total <= config.max_bytes and free >= config.min_free_bytes:
            break
        # Always leave one clip behind: deleting the last one would free
        # nothing useful and could race the writer on a nearly full card.
        if len(normal) - len(removed) <= 1:
            break
        if clip.path.resolve() in protected:
            continue
        if not _remove(clip):
            continue
        removed.append(clip.path)
        total -= clip.size_bytes
        free += clip.size_bytes

    events = sorted(list_clips(config, category="events"), key=lambda clip: clip.name)
    event_total = sum(clip.size_bytes for clip in events)
    for clip in events:
        if event_total <= config.max_event_bytes:
            break
        if clip.path.resolve() in protected:
            continue
        if not _remove(clip):
            continue
        removed.append(clip.path)
        event_total -= clip.size_bytes

    if removed:
        log.info("pruned %d clip(s), reclaiming space", len(removed))
    return removed


def safe_clip_name(name: str) -> str:
    """Validate a clip name supplied over HTTP.

    Raises:
        ValueError: if the name could address anything but a clip in the
            recording directory.
    """
    if not _SAFE_NAME.match(name):
        raise ValueError(f"invalid clip name: {name!r}")
    return name


def resolve_clip(config: RecordingConfig, name: str) -> ClipInfo:
    """Look up a clip by name.

    The name is validated, then matched against the actual inventory rather
    than joined onto a path -- a clip that is not in the listing does not
    exist, whatever the string looks like.
    """
    safe = safe_clip_name(name)
    for clip in list_clips(config):
        if clip.name == safe:
            return clip
    raise FileNotFoundError(f"no such clip: {name}")


def protect_clip(config: RecordingConfig, name: str) -> ClipInfo:
    """Move a loop clip into ``events/`` so pruning cannot reclaim it."""
    clip = resolve_clip(config, name)
    if clip.protected:
        return clip

    ensure_directories(config)
    destination = config.event_dir / clip.name
    clip.path.replace(destination)
    if clip.sidecar.exists():
        clip.sidecar.replace(destination.with_suffix(".json"))

    log.info("protected clip %s", clip.name)
    return ClipInfo(
        path=destination,
        name=clip.name,
        category="events",
        size_bytes=clip.size_bytes,
        started_at=clip.started_at,
        duration_seconds=clip.duration_seconds,
    )


def delete_clip(config: RecordingConfig, name: str) -> None:
    """Delete a clip and its sidecar."""
    clip = resolve_clip(config, name)
    if not _remove(clip):
        raise OSError(f"could not delete {clip.name}")
