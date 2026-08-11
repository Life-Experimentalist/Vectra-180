"""The command line.

Every subcommand is driven through :func:`~vectra180.cli.main` with a real
argument list, because the parser wiring -- which flags exist, which are shared,
which handler they reach -- is as much a part of the interface as the code
behind it. What sits under each command is faked at its own module, since
``cli`` imports lazily inside each function so that ``vectra180 run`` never
pulls in a GUI toolkit on a headless Pi.

The exit codes matter more than the prose: systemd reads them, and so does
anyone scripting ``doctor`` into a provisioning run.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import cv2
import numpy as np
import pytest

from tests.conftest import METADATA_WIDTH, encode_payload, make_frame
from vectra180 import __version__
from vectra180 import cli as cli_module
from vectra180.capture import DeviceInfo
from vectra180.cli import _to_toml, build_parser, main
from vectra180.config import EngineConfig
from vectra180.doctor import FAIL, OK, Report
from vectra180.errors import CaptureError


class FakeStats:
    """What ``cmd_run`` reports on the way out."""

    segments_written = 3
    written_frames = 90
    dropped_frames = 1


class FakeRecorder:
    stats = FakeStats()


class FakeEngine:
    """Stands in for the capture pipeline."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.recorder = FakeRecorder()
        self.started = False
        self.stopped = False
        self.recording = False

    def start(self) -> None:
        self.started = True

    def begin_recording(self) -> None:
        self.recording = True

    def stop(self) -> None:
        self.stopped = True


class FakeServer:
    def __init__(self) -> None:
        self.shut_down = False
        self.closed = False

    def shutdown(self) -> None:
        self.shut_down = True

    def server_close(self) -> None:
        self.closed = True


def device(index: int = 0, name: str = "USB 3.0 Camera") -> DeviceInfo:
    return DeviceInfo(index=index, path="", name=name, width=2560, height=720, fps=30.0, backend="v4l2")


def wait_until(predicate: Any, *, timeout: float = 5.0) -> None:
    """Block until a background thread has caught up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("the command did not reach the expected state in time")


def write_image(path: Path, image: np.ndarray) -> str:
    """Save a frame losslessly. PNG, so the telemetry column survives."""
    assert cv2.imwrite(str(path), image)
    return str(path)


@pytest.fixture
def config_file(tmp_path: Path, config: EngineConfig) -> str:
    """A config file on disk holding the isolated test config.

    The HTTP service is switched back on, because ``run`` starting it is part of
    what these tests check. Nothing binds a socket: every test that gets that
    far replaces ``serve`` with :class:`FakeServer`.
    """
    config.server.enabled = True
    path = tmp_path / "vectra.toml"
    path.write_text(_to_toml(config, redact=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def secret_config_file(tmp_path: Path, config: EngineConfig) -> str:
    """A config file that carries an auth token."""
    config.server.enabled = True
    config.server.token = "hunter2"
    path = tmp_path / "with-token.toml"
    path.write_text(_to_toml(config, redact=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch) -> list[FakeEngine]:
    """Replace the engine; collect every instance ``run`` builds."""
    created: list[FakeEngine] = []

    def factory(config: EngineConfig) -> FakeEngine:
        engine = FakeEngine(config)
        created.append(engine)
        return engine

    monkeypatch.setattr("vectra180.engine.Engine", factory)
    return created


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> list[FakeServer]:
    """Replace the HTTP service; collect every server ``run`` starts."""
    created: list[FakeServer] = []

    def serve(_engine: Any, _config: EngineConfig, *, block: bool = True) -> FakeServer:
        assert not block, "the run command must not block inside serve()"
        server = FakeServer()
        created.append(server)
        return server

    monkeypatch.setattr("vectra180.service.serve", serve)
    return created


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> dict[int, Any]:
    """Capture the signal handlers instead of installing them.

    Left alone, ``run`` would replace pytest's own SIGINT handler for the rest
    of the session.
    """
    installed: dict[int, Any] = {}

    def record(number: int, handler: Any) -> None:
        installed[number] = handler

    monkeypatch.setattr(signal, "signal", record)
    return installed


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------


def test_the_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert "vectra180" in capsys.readouterr().out


def test_a_bare_invocation_is_rejected() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2


def test_an_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["photograph"])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    ("command", "handler"),
    [
        ("run", "cmd_run"),
        ("view", "cmd_view"),
        ("devices", "cmd_devices"),
        ("doctor", "cmd_doctor"),
        ("config", "cmd_config"),
    ],
)
def test_each_subcommand_reaches_its_handler(command: str, handler: str) -> None:
    args = build_parser().parse_args([command])

    assert args.func is getattr(cli_module, handler)


def test_decode_requires_an_image() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["decode"])

    assert exit_info.value.code == 2


def test_the_shared_flags_are_accepted_by_every_subcommand() -> None:
    """``--config`` and friends live on a parent parser, which is easy to lose."""
    for command in ("run", "view", "devices", "doctor", "config"):
        args = build_parser().parse_args([command, "--camera", "2", "--quiet"])
        assert args.camera == 2
        assert args.quiet


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


@pytest.fixture
def log_level(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Capture the level ``basicConfig`` is asked for.

    Asserting on the root logger instead would be unreliable: ``basicConfig``
    does nothing once a handler exists, and pytest installs one.
    """
    levels: list[int] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: levels.append(kwargs["level"]))
    return levels


def test_the_default_verbosity_is_info(log_level: list[int], config_file: str) -> None:
    main(["config", "--config", config_file])

    assert log_level == [logging.INFO]


def test_verbose_turns_on_debug(log_level: list[int], config_file: str) -> None:
    main(["config", "--config", config_file, "-v"])

    assert log_level == [logging.DEBUG]


def test_quiet_beats_verbose(log_level: list[int], config_file: str) -> None:
    main(["config", "--config", config_file, "-v", "--quiet"])

    assert log_level == [logging.WARNING]


# --------------------------------------------------------------------------
# configuration loading and overrides
# --------------------------------------------------------------------------


def test_a_missing_config_file_is_reported(tmp_path: Path) -> None:
    assert main(["config", "--config", str(tmp_path / "nope.toml")]) == 1


def test_an_impossible_override_is_reported_rather_than_raised(config_file: str) -> None:
    """Config validation raises ValueError; the user gets a line, not a stack."""
    assert main(["config", "--config", config_file, "--camera", "-1"]) == 1


def test_the_camera_and_device_overrides_are_applied(config_file: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["config", "--config", config_file, "--camera", "3", "--device", "/dev/video3", "--json"])

    printed = json.loads(capsys.readouterr().out)
    assert printed["camera"]["index"] == 3
    assert printed["camera"]["device"] == "/dev/video3"


def test_the_backend_override_is_applied(config_file: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Pinning the driver from the command line is how you settle which camera
    an index refers to without editing a file first."""
    main(["config", "--config", config_file, "--backend", "msmf", "--json"])

    assert json.loads(capsys.readouterr().out)["camera"]["backend"] == "msmf"


def test_an_unknown_backend_is_refused(config_file: str) -> None:
    assert main(["config", "--config", config_file, "--backend", "directshow"]) == 1


def test_the_recording_directory_override_is_applied(
    config_file: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "elsewhere"

    main(["config", "--config", config_file, "--recording-dir", str(target), "--json"])

    assert json.loads(capsys.readouterr().out)["recording"]["directory"] == str(target)


def test_the_server_overrides_reach_the_engine(
    config_file: str, tmp_path: Path, engines: list[FakeEngine], servers: list[FakeServer], handlers: dict[int, Any]
) -> None:
    del servers, handlers

    main(
        [
            "run",
            "--config",
            config_file,
            "--duration",
            "0.01",
            "--recording-dir",
            str(tmp_path / "clips"),
            "--host",
            "0.0.0.0",
            "--port",
            "9099",
            "--token",
            "s3cret",
        ]
    )

    server = engines[0].config.server
    assert (server.host, server.port, server.token) == ("0.0.0.0", 9099, "s3cret")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_the_printed_toml_loads_back_unchanged(
    config: EngineConfig, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_file: str
) -> None:
    """The point of the command: capture a working setup into a file."""
    main(["config", "--config", config_file, "--show-secrets"])

    captured = tmp_path / "captured.toml"
    captured.write_text(capsys.readouterr().out, encoding="utf-8")

    assert EngineConfig.load(captured, use_env=False).to_dict() == config.to_dict()


def test_a_token_is_redacted_as_a_comment(secret_config_file: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A redacted token must not become a valid-looking wrong password."""
    main(["config", "--config", secret_config_file])

    printed = capsys.readouterr().out
    assert "hunter2" not in printed
    assert "# token = <redacted" in printed
    # Still a TOML file, and one that no longer claims to have a token.
    assert "token" not in tomllib.loads(printed)["server"]


def test_show_secrets_prints_the_token(secret_config_file: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["config", "--config", secret_config_file, "--show-secrets"])

    assert tomllib.loads(capsys.readouterr().out)["server"]["token"] == "hunter2"


def test_config_json_is_machine_readable(config_file: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["config", "--config", config_file, "--json"])

    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {"camera", "telemetry", "recording", "incident", "depth", "server"}


def test_the_config_path_is_reported_on_stderr(config_file: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Kept off stdout so ``vectra180 config --path > my.toml`` still works."""
    assert main(["config", "--config", config_file, "--path"]) == 0

    captured = capsys.readouterr()
    assert config_file in captured.err
    assert config_file not in captured.out


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------


class Probe:
    """Stands in for device enumeration, remembering how it was called."""

    def __init__(self) -> None:
        self.found: list[DeviceInfo] = []
        self.max_index: int | None = None

    def __call__(self, *, max_index: int = 10) -> list[DeviceInfo]:
        self.max_index = max_index
        return self.found


@pytest.fixture
def attached(monkeypatch: pytest.MonkeyPatch) -> Probe:
    probe = Probe()
    monkeypatch.setattr("vectra180.capture.enumerate_devices", probe)
    return probe


def test_attached_cameras_are_listed(attached: Probe, capsys: pytest.CaptureFixture[str]) -> None:
    attached.found.append(device())

    assert main(["devices"]) == 0
    assert "v4l2[0] USB 3.0 Camera" in capsys.readouterr().out


def test_a_camera_seen_on_two_backends_gets_the_index_caveat(
    attached: Probe, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listing is the moment to say it, because that is where the number
    someone is about to copy into their config appears."""
    attached.found += [device(), replace(device(), backend="dshow")]

    assert main(["devices"]) == 0
    assert "Indices are per-backend" in capsys.readouterr().out


def test_one_backend_needs_no_caveat(attached: Probe, capsys: pytest.CaptureFixture[str]) -> None:
    attached.found.append(device())

    assert main(["devices"]) == 0
    assert "Indices are per-backend" not in capsys.readouterr().out


def test_an_unreadable_device_is_flagged_in_the_listing(attached: Probe, capsys: pytest.CaptureFixture[str]) -> None:
    attached.found.append(replace(device(), readable=False))

    assert main(["devices"]) == 0
    assert "another program may hold it" in capsys.readouterr().out


def test_nothing_attached_exits_nonzero_with_a_hint(attached: Probe, capsys: pytest.CaptureFixture[str]) -> None:
    del attached

    assert main(["devices"]) == 1
    assert "video" in capsys.readouterr().out


def test_devices_json_is_machine_readable(attached: Probe, capsys: pytest.CaptureFixture[str]) -> None:
    attached.found.append(device(index=2, name="Webcam"))

    assert main(["devices", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "Webcam"


def test_devices_json_with_nothing_attached_still_exits_nonzero(
    attached: Probe, capsys: pytest.CaptureFixture[str]
) -> None:
    del attached

    assert main(["devices", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == []


def test_the_probe_depth_is_passed_through(attached: Probe) -> None:
    main(["devices", "--max-index", "3"])

    assert attached.max_index == 3


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


class Diagnosis:
    """Stands in for the diagnostics, remembering how it was called.

    The report starts empty; each test adds the checks whose handling it means to
    exercise.
    """

    def __init__(self) -> None:
        self.report = Report()
        self.probe_camera: bool | None = None

    def __call__(self, _config: EngineConfig, *, probe_camera: bool = True) -> Report:
        self.probe_camera = probe_camera
        return self.report


@pytest.fixture
def diagnosis(monkeypatch: pytest.MonkeyPatch) -> Diagnosis:
    """Replace the diagnostics with a report the test decides on."""
    stub = Diagnosis()
    monkeypatch.setattr("vectra180.doctor.run_diagnostics", stub)
    return stub


def test_a_healthy_report_exits_zero(diagnosis: Diagnosis, capsys: pytest.CaptureFixture[str]) -> None:
    diagnosis.report.add("camera", OK, "2560x720 at 30fps")

    assert main(["doctor"]) == 0
    assert "[ ok ] camera" in capsys.readouterr().out


def test_a_failing_report_exits_nonzero(diagnosis: Diagnosis, capsys: pytest.CaptureFixture[str]) -> None:
    diagnosis.report.add("camera", FAIL, "no device", "plug one in")

    assert main(["doctor"]) == 1
    assert "plug one in" in capsys.readouterr().out


def test_doctor_json_is_machine_readable(diagnosis: Diagnosis, capsys: pytest.CaptureFixture[str]) -> None:
    diagnosis.report.add("storage", OK, "plenty of room")

    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"][0]["name"] == "storage"


def test_no_camera_skips_the_hardware_probe(diagnosis: Diagnosis) -> None:
    main(["doctor", "--no-camera"])

    assert diagnosis.probe_camera is False


# --------------------------------------------------------------------------
# decode
# --------------------------------------------------------------------------


def test_decoding_a_file_that_is_not_there_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["decode", str(tmp_path / "absent.png")]) == 1
    assert "no such file" in capsys.readouterr().err


def test_decoding_something_that_is_not_an_image_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "notes.png"
    path.write_text("this is not a picture", encoding="utf-8")

    assert main(["decode", str(path)]) == 1
    assert "could not decode an image" in capsys.readouterr().err


def test_a_strip_wider_than_the_image_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    image = write_image(tmp_path / "frame.png", make_frame(encode_payload()))

    assert main(["decode", image, "--metadata-width", "9999"]) == 1
    assert "too narrow" in capsys.readouterr().err


def test_a_zero_width_strip_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    image = write_image(tmp_path / "frame.png", make_frame(encode_payload()))

    assert main(["decode", image, "--metadata-width", "0"]) == 1
    assert "at least 1" in capsys.readouterr().err


def test_a_single_still_frame_decodes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A still has no next frame, so the corroborating decoder is the wrong tool."""
    image = write_image(tmp_path / "frame.png", make_frame(encode_payload(accel_g=(0.0, 0.0, 1.0))))

    assert main(["decode", image, "--metadata-width", str(METADATA_WIDTH)]) == 0

    printed = capsys.readouterr().out
    assert "magnitude : 1.000 g" in printed
    assert "payload:" in printed


def test_a_frame_without_telemetry_fails_and_says_why(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    image = write_image(tmp_path / "plain.png", make_frame())

    assert main(["decode", image, "--metadata-width", str(METADATA_WIDTH)]) == 1

    printed = capsys.readouterr().out
    assert "No valid IMU block" in printed
    assert "--metadata-width" in printed


def test_decode_json_carries_the_payload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = encode_payload()
    image = write_image(tmp_path / "frame.png", make_frame(payload))

    assert main(["decode", image, "--metadata-width", str(METADATA_WIDTH), "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["payload_hex"] == payload.hex()
    assert printed["error"] is None
    assert printed["sample"]["timestamp_us"] > 0


def test_decode_json_reports_the_reason_it_failed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    image = write_image(tmp_path / "plain.png", make_frame())

    assert main(["decode", image, "--metadata-width", str(METADATA_WIDTH), "--json"]) == 1

    printed = json.loads(capsys.readouterr().out)
    assert printed["sample"] is None
    assert "not telemetry" in printed["error"]


# --------------------------------------------------------------------------
# view
# --------------------------------------------------------------------------


def test_view_hands_the_config_to_the_desktop_app(config_file: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[EngineConfig] = []

    def launch(config: EngineConfig) -> int:
        seen.append(config)
        return 0

    monkeypatch.setattr("vectra180.ui.launch", launch)

    assert main(["view", "--config", config_file]) == 0
    assert seen[0].camera.index == 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


@pytest.fixture
def run_argv(config_file: str, tmp_path: Path) -> list[str]:
    return ["run", "--config", config_file, "--recording-dir", str(tmp_path / "clips"), "--duration", "0.01"]


def test_run_records_and_serves(
    run_argv: list[str], engines: list[FakeEngine], servers: list[FakeServer], handlers: dict[int, Any]
) -> None:
    del handlers

    assert main(run_argv) == 0

    assert engines[0].started
    assert engines[0].recording
    assert engines[0].stopped
    assert servers[0].shut_down
    assert servers[0].closed


def test_no_record_serves_without_writing_clips(
    run_argv: list[str], engines: list[FakeEngine], servers: list[FakeServer], handlers: dict[int, Any]
) -> None:
    del handlers

    assert main([*run_argv, "--no-record"]) == 0

    assert engines[0].started
    assert not engines[0].recording
    assert servers


def test_no_serve_records_without_the_web_interface(
    run_argv: list[str], engines: list[FakeEngine], servers: list[FakeServer], handlers: dict[int, Any]
) -> None:
    del handlers

    assert main([*run_argv, "--no-serve"]) == 0

    assert engines[0].recording
    assert servers == []


def test_both_shutdown_signals_are_handled(
    run_argv: list[str], engines: list[FakeEngine], servers: list[FakeServer], handlers: dict[int, Any]
) -> None:
    """systemd sends SIGTERM on stop; a terminal sends SIGINT."""
    del engines, servers

    main(run_argv)

    assert signal.SIGINT in handlers
    assert signal.SIGTERM in handlers


@pytest.mark.skipif(not hasattr(signal, "SIGBREAK"), reason="SIGBREAK is Windows-only")
def test_ctrl_break_is_handled_where_it_exists(
    run_argv: list[str], engines: list[FakeEngine], servers: list[FakeServer], handlers: dict[int, Any]
) -> None:
    """Left unhandled, Ctrl-Break kills the process mid-segment."""
    del engines, servers
    # Reached through getattr for the same reason the code under test does it:
    # the name does not exist off Windows, and a type checker run on Linux --
    # which is where CI runs it -- rejects the attribute outright.
    sigbreak = getattr(signal, "SIGBREAK", None)

    main(run_argv)

    assert sigbreak is not None
    assert sigbreak in handlers


def test_a_platform_without_sigterm_still_runs(
    run_argv: list[str],
    engines: list[FakeEngine],
    servers: list[FakeServer],
    handlers: dict[int, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every signal is looked up by name, so a missing one is skipped, not fatal."""
    del servers
    sigterm = int(signal.SIGTERM)
    monkeypatch.delattr(signal, "SIGTERM")

    assert main(run_argv) == 0

    assert engines[0].stopped
    assert signal.SIGINT in handlers
    assert sigterm not in handlers


def test_a_second_interrupt_does_not_cut_the_shutdown_short(
    config_file: str,
    tmp_path: Path,
    engines: list[FakeEngine],
    servers: list[FakeServer],
    handlers: dict[int, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pressing Ctrl-C again is the natural reflex, and it must stay harmless.

    Both presses land on the same handler, so the second one only says that the
    open segment is still being finalised -- it does not abandon it.
    """
    del servers
    result: list[int] = []
    argv = ["run", "--config", config_file, "--recording-dir", str(tmp_path / "clips")]
    thread = threading.Thread(target=lambda: result.append(main(argv)), daemon=True)
    thread.start()
    try:
        wait_until(lambda: bool(engines) and engines[0].started and signal.SIGINT in handlers)
        with caplog.at_level(logging.INFO, logger="vectra180"):
            handlers[signal.SIGINT](int(signal.SIGINT), None)
            handlers[signal.SIGINT](int(signal.SIGINT), None)
        thread.join(timeout=5.0)
    finally:
        assert not thread.is_alive(), "the run command ignored the shutdown signal"

    assert result == [0]
    assert engines[0].stopped
    assert "already shutting down" in caplog.text


def test_a_termination_signal_ends_an_open_ended_run(
    config_file: str,
    tmp_path: Path,
    engines: list[FakeEngine],
    servers: list[FakeServer],
    handlers: dict[int, Any],
) -> None:
    """Without --duration the command waits forever, which is the service case."""
    del servers
    result: list[int] = []
    argv = ["run", "--config", config_file, "--recording-dir", str(tmp_path / "clips")]
    thread = threading.Thread(target=lambda: result.append(main(argv)), daemon=True)
    thread.start()
    try:
        wait_until(lambda: bool(engines) and engines[0].started and signal.SIGTERM in handlers)
        handlers[signal.SIGTERM](int(signal.SIGTERM), None)
        thread.join(timeout=5.0)
    finally:
        assert not thread.is_alive(), "the run command ignored the shutdown signal"

    assert result == [0]
    assert engines[0].stopped


def test_the_engine_is_stopped_even_when_starting_it_fails(
    run_argv: list[str], monkeypatch: pytest.MonkeyPatch, handlers: dict[int, Any]
) -> None:
    """A half-open camera must not be left holding the device."""
    del handlers
    stopped: list[bool] = []

    class FailingEngine(FakeEngine):
        def start(self) -> NoReturn:
            raise CaptureError("no camera at index 0")

        def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr("vectra180.engine.Engine", FailingEngine)

    assert main(run_argv) == 1
    assert stopped == [True]


# --------------------------------------------------------------------------
# top-level error handling
# --------------------------------------------------------------------------


def test_an_engine_error_becomes_one_line_and_exit_one(
    run_argv: list[str], monkeypatch: pytest.MonkeyPatch, handlers: dict[int, Any], caplog: pytest.LogCaptureFixture
) -> None:
    del handlers

    def raise_capture_error(_config: EngineConfig) -> NoReturn:
        raise CaptureError("no camera at index 0")

    monkeypatch.setattr("vectra180.engine.Engine", raise_capture_error)

    with caplog.at_level(logging.ERROR, logger="vectra180"):
        assert main(run_argv) == 1

    assert "no camera at index 0" in caplog.text


def test_an_interrupt_exits_with_the_conventional_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_interrupt(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr("vectra180.doctor.run_diagnostics", raise_interrupt)

    assert main(["doctor"]) == 130


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def test_the_module_entry_point_reaches_the_same_main() -> None:
    """``python -m vectra180`` is documented, so its import must resolve."""
    import vectra180.__main__ as module

    assert module.main is main


def test_the_module_entry_point_runs() -> None:
    """A real subprocess, because an installed console script is a real process.

    ``-m`` re-runs the import machinery from scratch, which is what catches a
    package that only works from a source checkout.
    """
    result = subprocess.run(
        [sys.executable, "-m", "vectra180", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout
