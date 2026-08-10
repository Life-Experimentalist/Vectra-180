"""Configuration loading, layering and validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vectra180.config import (
    CameraConfig,
    DepthConfig,
    EngineConfig,
    IncidentConfig,
    RecordingConfig,
    ServerConfig,
    TelemetryConfig,
    default_config_path,
    default_recording_dir,
)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# -- defaults ----------------------------------------------------------------


def test_defaults_are_a_working_setup() -> None:
    """A user who installs and runs with no config must get a valid engine."""
    EngineConfig().validate()


def test_the_server_defaults_to_loopback() -> None:
    """Recorded footage is sensitive; exposure has to be a deliberate act."""
    assert EngineConfig().server.host == "127.0.0.1"
    assert EngineConfig().server.is_public is False


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_non_loopback_hosts_are_flagged_public(host: str) -> None:
    assert ServerConfig(host=host).is_public is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_not_public(host: str) -> None:
    assert ServerConfig(host=host).is_public is False


# -- TOML merge --------------------------------------------------------------


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
        [camera]
        width = 1920
        fps = 25

        [recording]
        segment_seconds = 30
        """,
    )
    config = EngineConfig.load(path, use_env=False)

    assert config.camera.width == 1920
    assert config.camera.fps == 25
    assert config.recording.segment_seconds == 30
    # Untouched keys keep their defaults rather than being reset.
    assert config.camera.height == 720


def test_a_path_is_expanded(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[recording]\ndirectory = "~/clips"\n')
    config = EngineConfig.load(path, use_env=False)

    assert config.recording.directory == Path.home() / "clips"


def test_unknown_section_is_rejected(tmp_path: Path) -> None:
    """A typo must fail loudly, not be silently ignored on a headless Pi."""
    path = write_config(tmp_path, "[recrding]\nenabled = false\n")

    with pytest.raises(ValueError, match=r"unknown config section \[recrding\]"):
        EngineConfig.load(path, use_env=False)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[recording]\nsegment_secondz = 30\n")

    with pytest.raises(ValueError, match=r"unknown key recording\.segment_secondz"):
        EngineConfig.load(path, use_env=False)


def test_a_section_must_be_a_table(tmp_path: Path) -> None:
    path = write_config(tmp_path, 'camera = "front"\n')

    with pytest.raises(ValueError, match="must be a table"):
        EngineConfig.load(path, use_env=False)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[camera]\nwidth = "1920"\n', "must be an integer"),
        ("[camera]\nwidth = true\n", "must be an integer"),
        ("[recording]\nenabled = 1\n", "must be true or false"),
        ("[recording]\ndirectory = 7\n", "must be a quoted path"),
        ("[telemetry]\nsmoothing_alpha = true\n", "must be a number"),
        ("[recording]\nencoder = 3\n", "must be a string"),
    ],
)
def test_wrong_types_are_reported_against_the_key(tmp_path: Path, body: str, message: str) -> None:
    """Better here than as a TypeError from deep inside the engine."""
    path = write_config(tmp_path, body)

    with pytest.raises(ValueError, match=message):
        EngineConfig.load(path, use_env=False)


def test_an_integer_is_accepted_for_a_float_key(tmp_path: Path) -> None:
    """TOML has no way to write 20 as a float, so this must not be pedantic."""
    path = write_config(tmp_path, "[telemetry]\nyaw_leak_seconds = 20\n")

    config = EngineConfig.load(path, use_env=False)

    assert config.telemetry.yaw_leak_seconds == 20.0
    assert isinstance(config.telemetry.yaw_leak_seconds, float)


def test_a_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    """Silence would leave a user debugging why their settings did nothing."""
    with pytest.raises(FileNotFoundError):
        EngineConfig.load(tmp_path / "absent.toml", use_env=False)


def test_a_missing_default_config_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VECTRA_CONFIG", str(tmp_path / "absent.toml"))

    assert EngineConfig.load(use_env=False).camera.width == 2560


# -- environment -------------------------------------------------------------


def test_env_wins_over_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = write_config(tmp_path, "[camera]\nwidth = 1920\n")
    monkeypatch.setenv("VECTRA_CAPTURE_WIDTH", "1280")

    assert EngineConfig.load(path).camera.width == 1280


def test_env_is_skipped_when_asked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = write_config(tmp_path, "[camera]\nwidth = 1920\n")
    monkeypatch.setenv("VECTRA_CAPTURE_WIDTH", "1280")

    assert EngineConfig.load(path, use_env=False).camera.width == 1920


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_env_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("VECTRA_RECORDING_ENABLED", raw)
    config = RecordingConfig(enabled=False)
    config.apply_env()

    assert config.enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
def test_falsey_env_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("VECTRA_RECORDING_ENABLED", raw)
    config = RecordingConfig(enabled=True)
    config.apply_env()

    assert config.enabled is False


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("VECTRA_RECORDING_ENABLED", "maybe", "must be a boolean"),
        ("VECTRA_CAPTURE_WIDTH", "wide", "must be an integer"),
        ("VECTRA_GYRO_SMOOTHING", "smooth", "must be a number"),
    ],
)
def test_malformed_env_values_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str, raw: str, message: str) -> None:
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=message):
        EngineConfig.load(use_env=True)


def test_every_section_reads_its_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Guards against a new section being added without wiring apply_env."""
    monkeypatch.setenv("VECTRA_CAMERA_DEVICE", "/dev/video7")
    monkeypatch.setenv("VECTRA_METADATA_WIDTH", "16")
    monkeypatch.setenv("VECTRA_RECORDING_DIR", str(tmp_path))
    monkeypatch.setenv("VECTRA_INCIDENT_THRESHOLD_G", "0.9")
    monkeypatch.setenv("VECTRA_DEPTH_WIDTH", "320")
    monkeypatch.setenv("VECTRA_SERVER_PORT", "9099")

    config = EngineConfig.load(use_env=True)

    assert config.camera.device == "/dev/video7"
    assert config.telemetry.metadata_width == 16
    assert config.recording.directory == tmp_path
    assert config.incident.threshold_g == 0.9
    assert config.depth.working_width == 320
    assert config.server.port == 9099


def test_config_path_honours_its_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VECTRA_CONFIG", str(tmp_path / "custom.toml"))
    assert default_config_path() == tmp_path / "custom.toml"


def test_recording_dir_honours_its_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VECTRA_RECORDING_DIR", str(tmp_path / "clips"))
    assert default_recording_dir() == tmp_path / "clips"


def test_default_paths_are_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VECTRA_CONFIG", raising=False)
    monkeypatch.delenv("VECTRA_RECORDING_DIR", raising=False)

    assert default_config_path().is_absolute()
    assert default_recording_dir().is_absolute()


# The per-platform branches below are the ones that decide where an installed Pi
# service reads its config from and writes its footage to. The suite may well be
# running on Windows, so ``sys.platform`` and the two system-path probes are
# substituted rather than left to the host to answer.

SYSTEM_CONFIG = Path("/etc/vectra180/config.toml")
SYSTEM_RECORDINGS = Path("/var/lib/vectra180/recordings")


@pytest.fixture
def no_path_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VECTRA_CONFIG", raising=False)
    monkeypatch.delenv("VECTRA_RECORDING_DIR", raising=False)


def pretend_present(monkeypatch: pytest.MonkeyPatch, *present: Path) -> None:
    """Answer ``Path.exists`` from a fixed list instead of from the filesystem."""
    monkeypatch.setattr(Path, "exists", lambda self: self in present)


@pytest.mark.usefixtures("no_path_overrides")
def test_windows_keeps_its_config_in_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert default_config_path() == tmp_path / "Vectra180" / "config.toml"


@pytest.mark.usefixtures("no_path_overrides")
def test_macos_keeps_its_config_in_application_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    expected = Path.home() / "Library" / "Application Support" / "Vectra180" / "config.toml"
    assert default_config_path() == expected


@pytest.mark.usefixtures("no_path_overrides")
def test_an_installed_service_config_wins_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provisioned Pi has /etc/vectra180/config.toml, and that is the one to use."""
    monkeypatch.setattr(sys, "platform", "linux")
    pretend_present(monkeypatch, SYSTEM_CONFIG)

    assert default_config_path() == SYSTEM_CONFIG


@pytest.mark.usefixtures("no_path_overrides")
def test_linux_without_a_system_config_falls_back_to_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pretend_present(monkeypatch)

    assert default_config_path() == tmp_path / "vectra180" / "config.toml"


@pytest.mark.usefixtures("no_path_overrides")
def test_linux_without_xdg_set_falls_back_to_dot_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    pretend_present(monkeypatch)

    assert default_config_path() == Path.home() / ".config" / "vectra180" / "config.toml"


@pytest.mark.usefixtures("no_path_overrides")
@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_desktop_platforms_record_into_the_videos_folder(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    assert default_recording_dir() == Path.home() / "Videos" / "Vectra180"


@pytest.mark.usefixtures("no_path_overrides")
def test_a_provisioned_linux_host_records_into_var_lib(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer creates /var/lib/vectra180; its presence is what selects it."""
    monkeypatch.setattr(sys, "platform", "linux")
    pretend_present(monkeypatch, SYSTEM_RECORDINGS.parent)

    assert default_recording_dir() == SYSTEM_RECORDINGS


@pytest.mark.usefixtures("no_path_overrides")
def test_an_unprovisioned_linux_host_records_into_the_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running from a checkout must not need root to have somewhere to write."""
    monkeypatch.setattr(sys, "platform", "linux")
    pretend_present(monkeypatch)

    assert default_recording_dir() == Path.home() / "vectra180-recordings"


# -- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "kwargs", "message"),
    [
        (CameraConfig, {"index": -1}, "index must be >= 0"),
        (CameraConfig, {"width": 0}, "both be 0 .native mode. or both positive"),
        (CameraConfig, {"height": 0}, "both be 0 .native mode. or both positive"),
        (CameraConfig, {"height": -720}, "must be >= 0"),
        (CameraConfig, {"fps": 0}, "fps must be positive"),
        (CameraConfig, {"fourcc": "MJP"}, "exactly four characters"),
        (CameraConfig, {"reconnect_delay": -1.0}, "reconnect_delay must be >= 0"),
        (CameraConfig, {"read_failure_limit": 0}, "read_failure_limit must be >= 1"),
        (TelemetryConfig, {"metadata_width": -1}, "metadata_width must be >= 0"),
        (TelemetryConfig, {"smoothing_alpha": 1.0}, r"\[0.0, 1.0\)"),
        (TelemetryConfig, {"complementary_alpha": 1.5}, r"\[0.0, 1.0\]"),
        (TelemetryConfig, {"yaw_leak_seconds": -1.0}, "must be >= 0"),
        (TelemetryConfig, {"gravity_tolerance_g": 0.0}, "must be positive"),
        (RecordingConfig, {"segment_seconds": 4}, "must be >= 5"),
        (RecordingConfig, {"max_bytes": 0}, "max_bytes must be positive"),
        (RecordingConfig, {"min_free_bytes": -1}, "min_free_bytes must be >= 0"),
        (RecordingConfig, {"max_event_bytes": -1}, "max_event_bytes must be >= 0"),
        (RecordingConfig, {"bitrate_kbps": 0}, "bitrate_kbps must be positive"),
        (RecordingConfig, {"encoder": "vp9"}, "auto, ffmpeg, opencv"),
        (RecordingConfig, {"scale": 0.0}, r"scale must be between 0.1 and 1.0"),
        (RecordingConfig, {"scale": 1.5}, r"scale must be between 0.1 and 1.0"),
        (IncidentConfig, {"threshold_g": 0.0}, "threshold_g must be positive"),
        (IncidentConfig, {"cooldown_seconds": -1.0}, "cooldown_seconds must be >= 0"),
        (DepthConfig, {"working_width": 32}, "working_width must be >= 64"),
        (DepthConfig, {"num_disparities": 8}, "num_disparities must be >= 16"),
        (DepthConfig, {"block_size": 2}, "block_size must be >= 3"),
        (DepthConfig, {"focal_scale": 0.0}, r"\(0.0, 2.0\]"),
        (DepthConfig, {"focal_scale": 2.5}, r"\(0.0, 2.0\]"),
        (ServerConfig, {"port": 0}, r"1\.\.65535"),
        (ServerConfig, {"port": 70000}, r"1\.\.65535"),
        (ServerConfig, {"preview_quality": 0}, r"1\.\.100"),
        (ServerConfig, {"preview_quality": 101}, r"1\.\.100"),
        (ServerConfig, {"preview_fps": 0}, "preview_fps must be >= 1"),
        (ServerConfig, {"preview_width": 100}, "preview_width must be >= 160"),
    ],
)
def test_invalid_values_are_rejected(section: type, kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        section(**kwargs).validate()


def test_an_empty_fourcc_is_accepted(tmp_path: Path) -> None:
    """ "Leave the format alone" has to be expressible.

    It is the format counterpart of a zero width and height, and on some
    modules the only setting that reaches full frame rate: requesting a format
    at all is what pins them to a slower mode.
    """
    CameraConfig(fourcc="").validate()

    config = EngineConfig.load(write_config(tmp_path, '[camera]\nfourcc = ""\n'), use_env=False)

    assert config.camera.fourcc == ""


def test_validation_runs_on_load(tmp_path: Path) -> None:
    """A bad file must fail at load, not at the first frame."""
    path = write_config(tmp_path, "[recording]\nsegment_seconds = 1\n")

    with pytest.raises(ValueError, match="must be >= 5"):
        EngineConfig.load(path, use_env=False)


# -- derived views -----------------------------------------------------------


def test_category_directories_hang_off_the_root(tmp_path: Path) -> None:
    config = RecordingConfig(directory=tmp_path)

    assert config.normal_dir == tmp_path / "normal"
    assert config.event_dir == tmp_path / "events"


def test_to_dict_is_json_serialisable(tmp_path: Path) -> None:
    import json

    config = EngineConfig()
    config.recording.directory = tmp_path

    data = config.to_dict()

    assert json.loads(json.dumps(data))["recording"]["directory"] == str(tmp_path)


def test_to_dict_redacts_the_token_by_default() -> None:
    """Its main consumer is the status endpoint, which clients can read."""
    config = EngineConfig()
    config.server.token = "s3cret"

    assert config.to_dict()["server"]["token"] == "***"


def test_to_dict_can_emit_the_real_token() -> None:
    """The CLI's config dump would otherwise write '***' as the token."""
    config = EngineConfig()
    config.server.token = "s3cret"

    assert config.to_dict(redact=False)["server"]["token"] == "s3cret"


def test_redaction_leaves_an_empty_token_alone() -> None:
    """'***' would look like auth is on when it is not."""
    assert EngineConfig().to_dict()["server"]["token"] == ""


def test_to_dict_covers_every_section() -> None:
    from dataclasses import fields

    data = EngineConfig().to_dict()

    assert set(data) == {f.name for f in fields(EngineConfig)}
