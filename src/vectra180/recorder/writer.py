"""Video encoders.

Two backends, both writing H.264 in an MP4 container:

``ffmpeg``
    Raw BGR frames are piped to an ``ffmpeg`` subprocess. This is preferred
    because libx264 accepts a bitrate target and a tuning preset, and because
    ffmpeg finalises the container correctly even when it is killed.

``opencv``
    ``cv2.VideoWriter``. No external binary, but the codec depends on whatever
    the local OpenCV build was linked against and there is no bitrate control.

The Compute Module 5 has **no hardware H.264 encoder** -- the Pi 4's block was
removed -- so both paths are CPU-bound on the Cortex-A76 cores. That is why
``recording.preset`` defaults to ``ultrafast``.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from vectra180.config import RecordingConfig
from vectra180.errors import RecorderError

__all__ = ["FFmpegWriter", "FrameWriter", "OpenCVWriter", "create_writer", "ffmpeg_path"]

log = logging.getLogger(__name__)


def ffmpeg_path() -> str | None:
    """Absolute path to ``ffmpeg``, or ``None`` if it is not installed."""
    return shutil.which("ffmpeg")


class FrameWriter(Protocol):
    """The minimum an encoder must support."""

    def write(self, frame: np.ndarray) -> None: ...

    def close(self) -> None: ...

    @property
    def path(self) -> Path: ...


class FFmpegWriter:
    """Pipes raw frames into an ``ffmpeg`` subprocess."""

    def __init__(
        self,
        path: Path,
        size: tuple[int, int],
        fps: float,
        *,
        preset: str = "ultrafast",
        bitrate_kbps: int = 8000,
        binary: str | None = None,
    ) -> None:
        executable = binary or ffmpeg_path()
        if executable is None:
            raise RecorderError("ffmpeg was requested but is not on PATH")

        width, height = size
        self._path = path
        # Frames arrive already decoded, so ffmpeg is told the exact raw
        # layout and does no probing.
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{width}x{height}",
            "-r",
            f"{fps:.4f}",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{bitrate_kbps}k",
            # Two seconds between keyframes bounds how much of a segment a
            # corrupt write can destroy, and lets players seek.
            "-g",
            str(max(1, int(fps * 2))),
            # Writes the index up front so a segment truncated by a power cut
            # is still playable rather than an unseekable stub.
            "-movflags",
            "+faststart",
            str(path),
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise RecorderError(f"could not start ffmpeg: {exc}") from exc
        self._frame_bytes = width * height * 3
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, frame: np.ndarray) -> None:
        if self._closed or self._process.stdin is None:
            raise RecorderError("writer is closed")
        data = np.ascontiguousarray(frame).tobytes()
        if len(data) != self._frame_bytes:
            raise RecorderError(f"frame is {len(data)} bytes, encoder expects {self._frame_bytes}")
        try:
            self._process.stdin.write(data)
        except (BrokenPipeError, OSError) as exc:
            # ffmpeg died mid-segment -- surface its stderr, which says why.
            raise RecorderError(f"ffmpeg stopped accepting frames: {self._stderr() or exc}") from exc

    def _stderr(self) -> str:
        if self._process.stderr is None:
            return ""
        try:
            return str(self._process.stderr.read().decode("utf-8", "replace").strip())
        except OSError:
            return ""

    def close(self) -> None:
        """Close the pipe and wait for ffmpeg to finalise the container."""
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            # Already-broken pipe means ffmpeg died first; the returncode check
            # below is what reports that.
            with contextlib.suppress(OSError):
                self._process.stdin.close()
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg did not exit within 15s; terminating")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._process.returncode not in (0, None):
            log.error("ffmpeg exited %s: %s", self._process.returncode, self._stderr())


class OpenCVWriter:
    """Writes through ``cv2.VideoWriter``.

    The fallback for hosts without ffmpeg. ``mp4v`` is used because it is
    present in every OpenCV wheel; ``avc1`` is better but ships only where
    OpenCV was built against a licensed H.264 encoder.
    """

    def __init__(self, path: Path, size: tuple[int, int], fps: float, *, fourcc: str = "mp4v") -> None:
        self._path = path
        self._writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*fourcc), fps, size)
        if not self._writer.isOpened():
            raise RecorderError(f"OpenCV could not open a writer for {path} with fourcc {fourcc!r}")
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write(self, frame: np.ndarray) -> None:
        if self._closed:
            raise RecorderError("writer is closed")
        self._writer.write(frame)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._writer.release()


def create_writer(path: Path, size: tuple[int, int], fps: float, config: RecordingConfig) -> FrameWriter:
    """Build the encoder named by ``recording.encoder``.

    ``auto`` prefers ffmpeg and silently falls back to OpenCV, so a Pi image
    that lost its ffmpeg package still records. An explicit ``ffmpeg`` or
    ``opencv`` never falls back -- if a user pinned a backend, a silent
    substitution would hide the real problem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fps = max(1.0, fps)

    if config.encoder == "opencv":
        return OpenCVWriter(path, size, fps)
    if config.encoder == "ffmpeg":
        return FFmpegWriter(path, size, fps, preset=config.preset, bitrate_kbps=config.bitrate_kbps)

    if ffmpeg_path() is not None:
        try:
            return FFmpegWriter(path, size, fps, preset=config.preset, bitrate_kbps=config.bitrate_kbps)
        except RecorderError as exc:
            log.warning("ffmpeg unavailable (%s); falling back to the OpenCV writer", exc)
    return OpenCVWriter(path, size, fps)
