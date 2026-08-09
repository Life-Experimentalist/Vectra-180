"""Clip inventory, retention and the name validation the HTTP API leans on."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vectra180.config import RecordingConfig
from vectra180.recorder import storage


@pytest.fixture
def recording(tmp_path: Path) -> RecordingConfig:
    config = RecordingConfig(directory=tmp_path, min_free_bytes=0)
    storage.ensure_directories(config)
    return config


def add_clip(
    config: RecordingConfig,
    name: str,
    *,
    category: str = "normal",
    size: int = 1024,
    duration: float | None = None,
) -> Path:
    path = config.directory / category / name
    path.write_bytes(b"\0" * size)
    if duration is not None:
        path.with_suffix(".json").write_text(json.dumps({"duration_seconds": duration}), encoding="utf-8")
    return path


# -- names -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "VEC_20260809_142530.mp4",
        "VEC_20260809_142530.mkv",
        "VEC_20260809_142530.avi",
        "VEC_20260809_142530_1.mp4",
        "VEC_20260809_142530_locked.mp4",
    ],
)
def test_recognised_clip_names(name: str) -> None:
    assert storage.CLIP_NAME_PATTERN.match(name)


@pytest.mark.parametrize(
    "name",
    [
        "holiday.mp4",
        "VEC_2026089_142530.mp4",
        "VEC_20260809_1425.mp4",
        "VEC_20260809_142530.txt",
        "vec_20260809_142530.mp4",
        "VEC_20260809_142530.mp4.bak",
        "../VEC_20260809_142530.mp4",
        "VEC_20260809_142530.mp4\n",
    ],
)
def test_unrecognised_names_are_left_alone(name: str) -> None:
    """The pruner must never delete a file a user put in the folder."""
    assert storage.CLIP_NAME_PATTERN.match(name) is None


def test_clip_time_comes_from_the_name() -> None:
    """mtime is unreliable on a Pi with no battery-backed clock."""
    assert storage.parse_clip_time("VEC_20260809_142530.mp4") == datetime(2026, 8, 9, 14, 25, 30, tzinfo=UTC)


@pytest.mark.parametrize("name", ["nonsense.mp4", "VEC_20261301_142530.mp4", "VEC_20260809_256530.mp4"])
def test_unparseable_clip_times_are_none(name: str) -> None:
    assert storage.parse_clip_time(name) is None


@pytest.mark.parametrize("name", ["VEC_20260809_142530.mp4", "clip-1.mp4", "a_b.mkv"])
def test_safe_names_are_accepted(name: str) -> None:
    assert storage.safe_clip_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "..\\windows\\system32",
        "normal/VEC_20260809_142530.mp4",
        "/abs/path.mp4",
        "clip..mp4",
        "clip.mp4/",
        "",
        ".mp4",
        "clip",
        "clip.toolongext",
        "clip name.mp4",
        "clip%2e%2e.mp4",
        "clip\x00.mp4",
        "clip\n.mp4",
        # `$` would accept this one: in Python it also matches before a
        # trailing newline. Only `\Z` actually anchors to the end.
        "clip.mp4\n",
    ],
)
def test_unsafe_names_are_rejected(name: str) -> None:
    """Everything the HTTP layer might be handed by a hostile client."""
    with pytest.raises(ValueError, match="invalid clip name"):
        storage.safe_clip_name(name)


# -- listing -----------------------------------------------------------------


def test_clips_are_listed_newest_first(recording: RecordingConfig) -> None:
    for name in ("VEC_20260809_100000.mp4", "VEC_20260809_120000.mp4", "VEC_20260809_110000.mp4"):
        add_clip(recording, name)

    names = [clip.name for clip in storage.list_clips(recording)]

    assert names == ["VEC_20260809_120000.mp4", "VEC_20260809_110000.mp4", "VEC_20260809_100000.mp4"]


def test_listing_ignores_foreign_files(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4")
    (recording.normal_dir / "notes.txt").write_text("mine", encoding="utf-8")
    (recording.normal_dir / "subdir").mkdir()

    assert len(storage.list_clips(recording)) == 1


def test_listing_can_be_filtered_by_category(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4")
    add_clip(recording, "VEC_20260809_110000.mp4", category="events")

    assert len(storage.list_clips(recording, category="normal")) == 1
    assert len(storage.list_clips(recording, category="events")) == 1
    assert len(storage.list_clips(recording)) == 2


def test_listing_a_missing_tree_is_empty(tmp_path: Path) -> None:
    assert storage.list_clips(RecordingConfig(directory=tmp_path / "nothing")) == []


def test_duration_comes_from_the_sidecar(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4", duration=59.5)

    assert storage.list_clips(recording)[0].duration_seconds == 59.5


@pytest.mark.parametrize("body", ["not json", "{}", '{"duration_seconds": "sixty"}', "[]"])
def test_a_broken_sidecar_does_not_hide_the_clip(recording: RecordingConfig, body: str) -> None:
    path = add_clip(recording, "VEC_20260809_100000.mp4")
    path.with_suffix(".json").write_text(body, encoding="utf-8")

    clips = storage.list_clips(recording)

    assert len(clips) == 1
    assert clips[0].duration_seconds == 0.0


def test_event_clips_report_as_protected(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4", category="events")

    clip = storage.list_clips(recording)[0]

    assert clip.protected is True
    assert clip.as_dict()["protected"] is True


def test_clip_as_dict_is_json_safe(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4", duration=60.0)

    data = storage.list_clips(recording)[0].as_dict()

    assert json.loads(json.dumps(data))["started_at"] == "2026-08-09T10:00:00+00:00"


# -- stats -------------------------------------------------------------------


def test_stats_split_the_two_budgets(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4", size=100)
    add_clip(recording, "VEC_20260809_110000.mp4", size=200)
    add_clip(recording, "VEC_20260809_120000.mp4", category="events", size=400)

    stats = storage.storage_stats(recording)

    assert (stats.normal_clips, stats.normal_bytes) == (2, 300)
    assert (stats.event_clips, stats.event_bytes) == (1, 400)
    assert stats.total_bytes > 0
    assert json.loads(json.dumps(stats.as_dict()))["free_bytes"] == stats.free_bytes


def test_stats_create_the_tree_on_demand(tmp_path: Path) -> None:
    config = RecordingConfig(directory=tmp_path / "fresh")

    storage.storage_stats(config)

    assert config.normal_dir.is_dir()
    assert config.event_dir.is_dir()


# -- pruning -----------------------------------------------------------------


def test_prune_removes_oldest_first_until_within_budget(recording: RecordingConfig) -> None:
    recording.max_bytes = 250
    for hour in range(10, 15):
        add_clip(recording, f"VEC_20260809_{hour}0000.mp4", size=100)

    removed = storage.prune(recording)

    remaining = [clip.name for clip in storage.list_clips(recording, category="normal")]
    assert len(removed) == 3
    assert remaining == ["VEC_20260809_140000.mp4", "VEC_20260809_130000.mp4"]


def test_prune_takes_the_sidecar_with_the_clip(recording: RecordingConfig) -> None:
    recording.max_bytes = 100
    old = add_clip(recording, "VEC_20260809_100000.mp4", size=100, duration=60.0)
    add_clip(recording, "VEC_20260809_110000.mp4", size=100, duration=60.0)

    storage.prune(recording)

    assert not old.exists()
    assert not old.with_suffix(".json").exists()


def test_prune_never_touches_event_clips(recording: RecordingConfig) -> None:
    """The entire point of locking a clip is that the loop cannot reclaim it."""
    recording.max_bytes = 1
    recording.max_event_bytes = 10**9
    add_clip(recording, "VEC_20260809_100000.mp4", size=500)
    add_clip(recording, "VEC_20260809_110000.mp4", size=500)
    add_clip(recording, "VEC_20260809_120000.mp4", category="events", size=5000)

    storage.prune(recording)

    assert len(storage.list_clips(recording, category="events")) == 1


def test_prune_respects_the_keep_list(recording: RecordingConfig) -> None:
    """The segment being written must survive its own retention pass."""
    recording.max_bytes = 1
    oldest = add_clip(recording, "VEC_20260809_100000.mp4", size=500)
    add_clip(recording, "VEC_20260809_110000.mp4", size=500)
    add_clip(recording, "VEC_20260809_120000.mp4", size=500)

    storage.prune(recording, keep=[oldest])

    assert oldest.exists()


def test_prune_always_leaves_one_clip(recording: RecordingConfig) -> None:
    """Deleting the last clip frees nothing useful and could race the writer."""
    recording.max_bytes = 1
    add_clip(recording, "VEC_20260809_100000.mp4", size=500)

    assert storage.prune(recording) == []
    assert len(storage.list_clips(recording, category="normal")) == 1


def test_prune_is_a_no_op_inside_budget(recording: RecordingConfig) -> None:
    recording.max_bytes = 10**9
    for hour in (10, 11):
        add_clip(recording, f"VEC_20260809_{hour}0000.mp4", size=100)

    assert storage.prune(recording) == []


def test_low_free_space_triggers_pruning(recording: RecordingConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A card can be inside the byte budget and still be nearly full."""
    recording.max_bytes = 10**9
    recording.min_free_bytes = 5000
    for hour in (10, 11, 12):
        add_clip(recording, f"VEC_20260809_{hour}0000.mp4", size=1000)

    usage = shutil.disk_usage(recording.directory)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage.__class__(usage.total, usage.used, 3000))

    removed = storage.prune(recording)

    assert len(removed) == 2  # frees 2000, reaching the 5000 floor


def test_events_have_their_own_budget(recording: RecordingConfig) -> None:
    recording.max_bytes = 10**9
    recording.max_event_bytes = 250
    for hour in range(10, 15):
        add_clip(recording, f"VEC_20260809_{hour}0000.mp4", category="events", size=100)

    storage.prune(recording)

    assert len(storage.list_clips(recording, category="events")) == 2


def test_event_pruning_will_delete_the_last_one(recording: RecordingConfig) -> None:
    """Unlike the loop, an event budget of zero means keep nothing."""
    recording.max_bytes = 10**9
    recording.max_event_bytes = 0
    add_clip(recording, "VEC_20260809_100000.mp4", category="events", size=100)

    storage.prune(recording)

    assert storage.list_clips(recording, category="events") == []


def test_event_pruning_respects_the_keep_list(recording: RecordingConfig) -> None:
    """An incident locked seconds ago is the clip being written -- never delete it."""
    recording.max_bytes = 10**9
    recording.max_event_bytes = 0
    current = add_clip(recording, "VEC_20260809_120000.mp4", category="events", size=100)
    add_clip(recording, "VEC_20260809_100000.mp4", category="events", size=100)

    removed = storage.prune(recording, keep=[current])

    assert [path.name for path in removed] == ["VEC_20260809_100000.mp4"]
    assert current.exists()


def test_event_pruning_survives_an_undeletable_clip(
    recording: RecordingConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording.max_bytes = 10**9
    recording.max_event_bytes = 0
    for hour in (10, 11):
        add_clip(recording, f"VEC_20260809_{hour}0000.mp4", category="events", size=100)

    original = Path.unlink

    def refuse_the_oldest(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "VEC_20260809_100000.mp4":
            raise PermissionError("in use")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse_the_oldest)

    removed = storage.prune(recording)

    assert [path.name for path in removed] == ["VEC_20260809_110000.mp4"]


def test_a_clip_deleted_mid_scan_is_skipped(recording: RecordingConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retention pass runs while the recorder writes; the tree moves under it.

    The window is between the ``is_file`` check and the size lookup, so the
    stand-in lets the first stat of that clip through and fails the second --
    which is what deleting the file in between would do.
    """
    add_clip(recording, "VEC_20260809_100000.mp4")
    add_clip(recording, "VEC_20260809_110000.mp4")

    original = Path.stat
    seen: list[str] = []

    def vanish_after_the_first_look(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "VEC_20260809_100000.mp4":
            seen.append(self.name)
            if len(seen) > 1:
                raise FileNotFoundError(2, "No such file or directory", str(self))
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", vanish_after_the_first_look)

    assert [clip.name for clip in storage.list_clips(recording)] == ["VEC_20260809_110000.mp4"]


def test_prune_survives_an_undeletable_clip(recording: RecordingConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked file on the card must not abort the whole retention pass."""
    recording.max_bytes = 250
    for hour in (10, 11, 12):
        add_clip(recording, f"VEC_20260809_{hour}0000.mp4", size=100)

    original = Path.unlink

    def refuse_the_oldest(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "VEC_20260809_100000.mp4":
            raise PermissionError("in use")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse_the_oldest)

    removed = storage.prune(recording)

    assert [path.name for path in removed] == ["VEC_20260809_110000.mp4"]


# -- lookup and mutation -----------------------------------------------------


def test_resolve_finds_a_clip_by_name(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4")

    assert storage.resolve_clip(recording, "VEC_20260809_100000.mp4").category == "normal"


def test_resolve_rejects_an_unknown_clip(recording: RecordingConfig) -> None:
    with pytest.raises(FileNotFoundError):
        storage.resolve_clip(recording, "VEC_20260809_100000.mp4")


def test_resolve_rejects_a_traversal_before_touching_the_disk(recording: RecordingConfig) -> None:
    with pytest.raises(ValueError, match="invalid clip name"):
        storage.resolve_clip(recording, "../../../etc/passwd")


def test_resolve_will_not_return_a_file_outside_the_inventory(recording: RecordingConfig) -> None:
    """A name that passes validation but is not a clip still does not exist."""
    (recording.normal_dir / "secrets.mp4").write_bytes(b"x")

    with pytest.raises(FileNotFoundError):
        storage.resolve_clip(recording, "secrets.mp4")


def test_protect_moves_a_clip_into_events(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4", duration=60.0)

    clip = storage.protect_clip(recording, "VEC_20260809_100000.mp4")

    assert clip.category == "events"
    assert clip.protected is True
    assert (recording.event_dir / "VEC_20260809_100000.mp4").exists()
    assert not (recording.normal_dir / "VEC_20260809_100000.mp4").exists()


def test_protect_brings_the_sidecar_along(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4", duration=60.0)

    storage.protect_clip(recording, "VEC_20260809_100000.mp4")

    assert (recording.event_dir / "VEC_20260809_100000.json").exists()
    assert not (recording.normal_dir / "VEC_20260809_100000.json").exists()


def test_protect_without_a_sidecar_is_fine(recording: RecordingConfig) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4")

    assert storage.protect_clip(recording, "VEC_20260809_100000.mp4").category == "events"


def test_protecting_an_event_clip_is_idempotent(recording: RecordingConfig) -> None:
    """The recorder can lock the same clip twice in one incident."""
    add_clip(recording, "VEC_20260809_100000.mp4", category="events")

    clip = storage.protect_clip(recording, "VEC_20260809_100000.mp4")

    assert clip.category == "events"
    assert len(storage.list_clips(recording)) == 1


def test_delete_removes_clip_and_sidecar(recording: RecordingConfig) -> None:
    path = add_clip(recording, "VEC_20260809_100000.mp4", duration=60.0)

    storage.delete_clip(recording, "VEC_20260809_100000.mp4")

    assert not path.exists()
    assert not path.with_suffix(".json").exists()


def test_delete_rejects_an_unknown_clip(recording: RecordingConfig) -> None:
    with pytest.raises(FileNotFoundError):
        storage.delete_clip(recording, "VEC_20260809_100000.mp4")


def test_delete_reports_a_failure(recording: RecordingConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    add_clip(recording, "VEC_20260809_100000.mp4")
    monkeypatch.setattr(Path, "unlink", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("in use")))

    with pytest.raises(OSError, match="could not delete"):
        storage.delete_clip(recording, "VEC_20260809_100000.mp4")
