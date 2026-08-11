"""``vectra180 run`` as an operator actually invokes it.

``tests/test_cli.py`` swaps in a fake engine and a fake server to check the
argument wiring. Nothing is faked here except the camera: the command parses a
real TOML file, builds the real engine, writes real clips and binds a real
socket, and the assertions are made after it has returned -- which is the only
moment a service manager gets to look at anything.
"""

from __future__ import annotations

import json
import signal
import socket
from collections.abc import Iterator
from pathlib import Path

import cv2
import pytest

from tests.integration.conftest import ReplaySource, StatusProbe, free_port, level_run
from vectra180 import engine as engine_module
from vectra180.cli import _to_toml, main
from vectra180.config import EngineConfig
from vectra180.recorder import storage

pytestmark = pytest.mark.integration

#: Real seconds a run is given. The replay source is far faster than real time,
#: so this is minutes of footage, not one second of it.
RUN_SECONDS = 1.5


@pytest.fixture(autouse=True)
def restore_signal_handlers() -> Iterator[None]:
    """Put back the handlers ``cmd_run`` replaces.

    It installs its own SIGINT and SIGTERM handlers and, being a process-wide
    setting, never removes them -- which is correct for a service and would
    otherwise leave every later test in this session unable to be interrupted.
    """
    saved = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGINT", "SIGTERM") if hasattr(signal, name)}
    try:
        yield
    finally:
        for name, handler in saved.items():
            if handler is not None:
                signal.signal(getattr(signal, name), handler)


@pytest.fixture
def config_file(config: EngineConfig, tmp_path: Path) -> Path:
    """The fixture config, written out the way ``vectra180 config`` writes it.

    Round-tripping through the command's own emitter is deliberate: the file an
    operator captures from a working setup is the file this test feeds back in,
    so a key that cannot survive the trip fails here.
    """
    path = tmp_path / "vectra.toml"
    # The shared fixture leaves the server off so unit tests never bind; a
    # dashcam ships with it on, and the runs below need it.
    config.server.enabled = True
    path.write_text(_to_toml(config, redact=False), encoding="utf-8")
    return path


@pytest.fixture
def camera(monkeypatch: pytest.MonkeyPatch) -> ReplaySource:
    """A replayed camera, already armed -- the CLI offers no hook to arm it."""
    source = ReplaySource(level_run(30), loop=True)
    source.armed.set()
    monkeypatch.setattr(engine_module, "CameraSource", lambda _config: source)
    return source


def test_run_writes_playable_clips_into_the_directory_the_config_names(
    config: EngineConfig, config_file: Path, camera: ReplaySource
) -> None:
    """The headless case: no display, no arguments beyond a file, real output.

    Clips landing under ``tmp_path`` rather than the platform's default video
    directory is the proof that ``--config`` was read and applied, not merely
    accepted.
    """
    assert main(["run", "--no-serve", "--duration", str(RUN_SECONDS), "--config", str(config_file)]) == 0

    clips = storage.list_clips(config.recording)
    assert clips, "a full run produced no clips"
    for clip in clips:
        assert clip.path.parent == config.recording.normal_dir
        assert clip.size_bytes > 0

    # The oldest clip is closed and complete; the newest was finalised by the
    # shutdown path, which is the one a service manager depends on.
    for clip in (clips[0], clips[-1]):
        capture = cv2.VideoCapture(str(clip.path))
        assert capture.isOpened(), f"{clip.name} will not play back"
        try:
            ok, _ = capture.read()
            assert ok, f"{clip.name} decoded no frames"
        finally:
            capture.release()
        assert json.loads(clip.sidecar.read_text(encoding="utf-8"))["clip"] == clip.name


def test_run_serves_the_api_while_recording_and_frees_the_port_afterwards(
    config: EngineConfig, config_file: Path, camera: ReplaySource
) -> None:
    """One command has to be both the recorder and the phone's web interface.

    Releasing the socket on the way out matters as much as binding it: systemd
    restarts a failed unit within seconds, and a port still held by the corpse
    of the last run turns one crash into a service that never comes back.
    """
    port = free_port()
    # Waiting for a written frame rather than for any answer at all: the socket
    # is up before the first frame has reached the encoder, so the earliest
    # reply would show a recorder that has done nothing yet and prove only that
    # the two started, not that they ran together.
    probe = StatusProbe(port, until=lambda status: status["recorder"]["written_frames"] > 0)
    probe.start()

    exit_code = main(["run", "--duration", str(RUN_SECONDS), "--port", str(port), "--config", str(config_file)])

    assert exit_code == 0
    status = probe.result()
    assert status["running"] is True
    assert status["recorder"]["written_frames"] > 0
    assert status["camera"]["backend"] == "REPLAY"
    assert storage.list_clips(config.recording), "serving came at the cost of recording"

    with socket.socket() as probe_socket:
        probe_socket.settimeout(5.0)
        with pytest.raises(OSError):
            probe_socket.connect(("127.0.0.1", port))


def test_run_with_recording_off_serves_a_live_view_and_writes_nothing(
    config: EngineConfig, config_file: Path, camera: ReplaySource
) -> None:
    """Aiming the camera should not cost a single frame of the card.

    ``--no-record`` is what an installer uses while bolting the module to the
    windscreen, so the pipeline has to come up far enough to show a picture
    while leaving the recorder untouched.
    """
    port = free_port()
    # Same race as the recording case, one stage earlier: the server answers
    # before the capture loop has produced anything to show.
    probe = StatusProbe(port, until=lambda status: status["frames"] > 0)
    probe.start()

    exit_code = main(
        ["run", "--no-record", "--duration", str(RUN_SECONDS), "--port", str(port), "--config", str(config_file)]
    )

    assert exit_code == 0
    status = probe.result()
    assert status["running"] is True
    assert status["frames"] > 0
    assert status["recorder"]["written_frames"] == 0
    assert storage.list_clips(config.recording) == []
