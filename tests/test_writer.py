"""Video encoders and backend selection.

The ffmpeg tests drive a stand-in subprocess rather than the real binary: what
matters here is the command that gets built and how a dying encoder is
reported, neither of which needs a working libx264 on the test host.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import numpy as np
import pytest

from vectra180.config import RecordingConfig
from vectra180.errors import RecorderError
from vectra180.recorder import writer as writer_module
from vectra180.recorder.writer import FFmpegWriter, OpenCVWriter, create_writer, ffmpeg_path

SIZE = (64, 48)


@pytest.fixture
def frame() -> np.ndarray:
    return np.zeros((SIZE[1], SIZE[0], 3), dtype=np.uint8)


class FakeProcess:
    """Enough of ``Popen`` for the writer to talk to."""

    def __init__(self, *, returncode: int = 0, stderr: bytes = b"", stdin_error: type[OSError] | None = None) -> None:
        #: The argv the writer built, filled in by the ``spawned`` fixture.
        self.command: list[str] = []
        self.stdin = FakePipe(stdin_error)
        self.stdout = None
        self.stderr = io.BytesIO(stderr)
        self.returncode: int | None = returncode
        self.waits = 0
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        return self.returncode or 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class FakePipe(io.BytesIO):
    def __init__(self, error: type[OSError] | None = None) -> None:
        super().__init__()
        self._error = error

    def write(self, data: object) -> int:  # type: ignore[override]
        if self._error is not None:
            raise self._error("pipe is gone")
        return super().write(data)  # type: ignore[arg-type]


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[FakeProcess]:
    """Capture every encoder the writer tries to launch.

    Also pretends ffmpeg is installed, so these run the same on a developer
    machine without it as on a Pi image with it.
    """
    processes: list[FakeProcess] = []

    def fake_popen(command: list[str], **_kwargs: object) -> FakeProcess:
        process = FakeProcess()
        process.command = command
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(writer_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    return processes


# -- ffmpeg command ----------------------------------------------------------


def test_ffmpeg_is_launched_as_an_argument_list(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """A shell string here would make any path with a space an injection point."""
    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    assert isinstance(spawned[0].command, list)
    assert all(isinstance(part, str) for part in spawned[0].command)


def test_ffmpeg_is_told_the_exact_raw_layout(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """Frames arrive already decoded, so ffmpeg must do no probing."""
    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")
    command = spawned[0].command

    assert command[command.index("-f") + 1] == "rawvideo"
    assert command[command.index("-pix_fmt") + 1] == "bgr24"
    assert command[command.index("-s") + 1] == "64x48"
    assert command[command.index("-i") + 1] == "pipe:0"


def test_encoder_settings_reach_the_command_line(spawned: list[FakeProcess], tmp_path: Path) -> None:
    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 25.0, preset="superfast", bitrate_kbps=4200, binary="ffmpeg")
    command = spawned[0].command

    assert command[command.index("-preset") + 1] == "superfast"
    assert command[command.index("-b:v") + 1] == "4200k"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[-1] == str(tmp_path / "clip.mp4")


def test_keyframes_land_every_two_seconds(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """Bounds what a corrupt write destroys, and makes the clip seekable."""
    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    assert spawned[0].command[spawned[0].command.index("-g") + 1] == "60"


def test_a_very_slow_frame_rate_still_gets_a_keyframe(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """``-g 0`` is rejected by libx264, so the interval floors at one."""
    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 0.25, binary="ffmpeg")

    assert spawned[0].command[spawned[0].command.index("-g") + 1] == "1"


def test_the_index_is_written_up_front(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """A segment truncated by a power cut must still play."""
    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    assert spawned[0].command[spawned[0].command.index("-movflags") + 1] == "+faststart"


# -- ffmpeg behaviour --------------------------------------------------------


def test_a_missing_binary_is_reported_not_guessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer_module, "ffmpeg_path", lambda: None)

    with pytest.raises(RecorderError, match="not on PATH"):
        FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0)


def test_a_binary_that_will_not_start_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "Popen", refuse)

    with pytest.raises(RecorderError, match="could not start ffmpeg"):
        FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")


def test_frames_are_piped_verbatim(spawned: list[FakeProcess], tmp_path: Path, frame: np.ndarray) -> None:
    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    encoder.write(frame)

    assert spawned[0].stdin.getvalue() == frame.tobytes()


def test_a_non_contiguous_frame_is_still_written(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """Cropping a frame leaves a view whose raw buffer is not what ffmpeg wants."""
    wide = np.zeros((SIZE[1], SIZE[0] * 2, 3), dtype=np.uint8)
    view = wide[:, : SIZE[0]]
    assert not view.flags["C_CONTIGUOUS"]

    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")
    encoder.write(view)

    assert len(spawned[0].stdin.getvalue()) == SIZE[0] * SIZE[1] * 3


def test_a_wrongly_sized_frame_is_refused(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """ffmpeg would silently reinterpret it, shearing every frame after."""
    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    with pytest.raises(RecorderError, match="encoder expects"):
        encoder.write(np.zeros((10, 10, 3), dtype=np.uint8))


def test_writing_after_close_is_refused(spawned: list[FakeProcess], tmp_path: Path, frame: np.ndarray) -> None:
    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")
    encoder.close()

    with pytest.raises(RecorderError, match="closed"):
        encoder.write(frame)


def test_a_dead_encoder_surfaces_its_own_diagnosis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg's stderr says why it quit; the broken pipe says nothing."""
    process = FakeProcess(returncode=1, stderr=b"height not divisible by 2\n", stdin_error=BrokenPipeError)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: process)

    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    with pytest.raises(RecorderError, match="height not divisible by 2"):
        encoder.write(np.zeros((SIZE[1], SIZE[0], 3), dtype=np.uint8))


def test_close_waits_for_the_container_to_be_finalised(spawned: list[FakeProcess], tmp_path: Path) -> None:
    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    encoder.close()

    assert spawned[0].waits == 1
    assert spawned[0].stdin.closed


def test_close_is_idempotent(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """The recorder closes on error and again on shutdown."""
    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    encoder.close()
    encoder.close()

    assert spawned[0].waits == 1


def test_a_hung_encoder_is_terminated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stuck ffmpeg must not hold the recorder's shutdown open forever."""

    class HungProcess(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if not self.terminated:
                raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
            return 0

    process = HungProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: process)

    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg").close()

    assert process.terminated is True
    assert process.killed is False


def test_an_unkillable_encoder_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class WedgedProcess(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)

    process = WedgedProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: process)

    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg").close()

    assert process.killed is True


def test_a_nonzero_exit_is_logged_with_what_ffmpeg_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The clip is already unusable; the reason is all the operator gets."""
    process = FakeProcess(returncode=1, stderr=b"No space left on device\n")
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: process)
    encoder = FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg")

    with caplog.at_level("ERROR", logger="vectra180.recorder.writer"):
        encoder.close()

    assert "No space left on device" in caplog.text


def test_an_encoder_with_no_stderr_pipe_still_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(returncode=1)
    process.stderr = None  # type: ignore[assignment]
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: process)

    FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg").close()

    assert process.waits == 1


def test_an_unreadable_stderr_does_not_mask_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Reading a pipe whose process is gone raises; the exit code still matters."""

    class DeadPipe(io.BytesIO):
        def read(self, _size: int | None = -1) -> bytes:
            raise OSError("the pipe went away")

    process = FakeProcess(returncode=255)
    process.stderr = DeadPipe()
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: process)

    with caplog.at_level("ERROR", logger="vectra180.recorder.writer"):
        FFmpegWriter(tmp_path / "clip.mp4", SIZE, 30.0, binary="ffmpeg").close()

    assert "255" in caplog.text


def test_each_writer_reports_the_file_it_owns(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """The recorder names the sidecar and the retention entry from this."""
    del spawned
    path = tmp_path / "clip.mp4"

    assert FFmpegWriter(path, SIZE, 30.0, binary="ffmpeg").path == path
    assert OpenCVWriter(path, SIZE, 30.0).path == path


def test_ffmpeg_path_reports_what_is_installed() -> None:
    """Either a real path or None -- never a bare name that might not resolve."""
    found = ffmpeg_path()

    assert found is None or Path(found).exists()


# -- opencv ------------------------------------------------------------------


def test_the_opencv_writer_produces_a_playable_file(tmp_path: Path, frame: np.ndarray) -> None:
    path = tmp_path / "clip.mp4"
    encoder = OpenCVWriter(path, SIZE, 30.0)

    for _ in range(10):
        encoder.write(frame)
    encoder.close()

    assert path.exists()
    assert path.stat().st_size > 0


def test_an_unwritable_destination_is_reported(tmp_path: Path) -> None:
    """OpenCV's writer fails silently; only ``isOpened`` reveals it."""
    with pytest.raises(RecorderError, match="could not open a writer"):
        OpenCVWriter(tmp_path / "no-such-directory" / "clip.mp4", SIZE, 30.0)


def test_the_opencv_writer_refuses_writes_after_close(tmp_path: Path, frame: np.ndarray) -> None:
    encoder = OpenCVWriter(tmp_path / "clip.mp4", SIZE, 30.0)
    encoder.close()

    with pytest.raises(RecorderError, match="closed"):
        encoder.write(frame)


def test_the_opencv_writer_closes_idempotently(tmp_path: Path) -> None:
    encoder = OpenCVWriter(tmp_path / "clip.mp4", SIZE, 30.0)

    encoder.close()
    encoder.close()


# -- backend selection -------------------------------------------------------


def test_an_explicit_opencv_backend_is_honoured(tmp_path: Path) -> None:
    config = RecordingConfig(directory=tmp_path, encoder="opencv")

    encoder = create_writer(tmp_path / "clip.mp4", SIZE, 30.0, config)
    encoder.close()

    assert isinstance(encoder, OpenCVWriter)


def test_an_explicit_ffmpeg_backend_never_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent substitution would hide the real problem from a pinned setup."""
    monkeypatch.setattr(writer_module, "ffmpeg_path", lambda: None)
    config = RecordingConfig(directory=tmp_path, encoder="ffmpeg")

    with pytest.raises(RecorderError):
        create_writer(tmp_path / "clip.mp4", SIZE, 30.0, config)


def test_auto_prefers_ffmpeg_when_it_is_installed(
    spawned: list[FakeProcess], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writer_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    config = RecordingConfig(directory=tmp_path, encoder="auto")

    assert isinstance(create_writer(tmp_path / "clip.mp4", SIZE, 30.0, config), FFmpegWriter)


def test_auto_records_anyway_without_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Pi image that lost its ffmpeg package still has to record."""
    monkeypatch.setattr(writer_module, "ffmpeg_path", lambda: None)
    config = RecordingConfig(directory=tmp_path, encoder="auto")

    encoder = create_writer(tmp_path / "clip.mp4", SIZE, 30.0, config)
    encoder.close()

    assert isinstance(encoder, OpenCVWriter)


def test_auto_falls_back_when_ffmpeg_will_not_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer_module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no such file or directory"))
    )
    config = RecordingConfig(directory=tmp_path, encoder="auto")

    encoder = create_writer(tmp_path / "clip.mp4", SIZE, 30.0, config)
    encoder.close()

    assert isinstance(encoder, OpenCVWriter)


def test_the_destination_directory_is_created(tmp_path: Path) -> None:
    config = RecordingConfig(directory=tmp_path, encoder="opencv")
    path = tmp_path / "normal" / "nested" / "clip.mp4"

    create_writer(path, SIZE, 30.0, config).close()

    assert path.parent.is_dir()


def test_a_zero_frame_rate_is_clamped(spawned: list[FakeProcess], tmp_path: Path) -> None:
    """A camera that reports 0fps must not produce an undecodable file."""
    config = RecordingConfig(directory=tmp_path, encoder="ffmpeg")

    create_writer(tmp_path / "clip.mp4", SIZE, 0.0, config)

    assert spawned[0].command[spawned[0].command.index("-r") + 1] == "1.0000"
