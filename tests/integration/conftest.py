"""Fixtures for the integration suite.

The unit tests isolate one module and stub its neighbours. These do the
opposite: the real capture loop feeds the real recorder, which drives a real
encoder, writes real files, runs real retention, and serves them back over a
real socket. The only thing replaced is the camera, because there is none.

Replaying rather than waiting
----------------------------

Every clock the pipeline reads comes off the frame, not off the wall: the
recorder rolls a segment on ``frame.monotonic`` and names the file from
``frame.wall_time``. :class:`ReplaySource` advances both synthetically, so a
test can cover a minute of footage in a fraction of a second without dropping
``recording.segment_seconds`` below the five the config validator allows.
"""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPResponse
from typing import Any

import pytest

from tests.conftest import (
    FIRST_TIMESTAMP_US,
    FRAME_INTERVAL_US,
    METADATA_WIDTH,
    encode_payload,
    make_frame,
    make_strip,
)
from vectra180.capture import Frame
from vectra180.config import EngineConfig

#: Wall clock the replayed run starts at, so clip names are predictable.
BASE_WALL = datetime(2026, 8, 9, 14, 25, 30, tzinfo=UTC).timestamp()

#: Frame rate of the replayed run, matching the config fixture's camera.
REPLAY_FPS = 30.0

#: Real seconds between frames. Not zero: the encoder runs on its own thread
#: behind a bounded queue, and a producer that never yields would fill it and
#: turn most of the run into dropped frames.
PACE = 0.001

#: A stationary, level module.
LEVEL = (0.0, 0.0, 1.0)

#: A lateral hit: 1.8 g total, so 0.8 g of deviation -- past any plausible
#: incident threshold, and still inside the sensor's +/-2 g full-scale range.
IMPACT = (1.5, 0.0, 1.0)

#: Deviation from rest that :data:`IMPACT` produces, in g.
IMPACT_DEVIATION_G = 0.803


def level_run(seconds: float) -> list[tuple[float, float, float]]:
    """A stationary script of the given length, in replayed seconds."""
    return [LEVEL] * round(seconds * REPLAY_FPS)


class ReplaySource:
    """A camera that replays a scripted run faster than real time.

    ``script`` holds one accelerometer reading per frame, which is how a test
    says "six seconds of driving, then an impact". Each frame carries a real
    telemetry strip built by :func:`~tests.conftest.encode_payload`, so the
    decoder under test sees the wire format rather than a mock of it.

    With ``loop`` unset the generator ends when the script does, which is how
    these tests know a run has finished: the capture loop leaves the recorder
    flushed and its own thread dead, so what is on disk afterwards is final.
    """

    def __init__(
        self,
        script: Sequence[tuple[float, float, float]],
        *,
        telemetry: bool = True,
        loop: bool = False,
    ) -> None:
        self.script = list(script)
        self.telemetry = telemetry
        self.loop = loop
        self.fps = REPLAY_FPS
        self.opened = False
        self.closed = False
        self.read_count = 0
        #: Held shut until the caller has started the recorder, so a short
        #: script cannot run out before there is anything to record into.
        self.armed = threading.Event()
        self._base = make_frame()
        self._origin = time.monotonic()

    # -- the CameraSource surface the engine uses --------------------------

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    @property
    def is_open(self) -> bool:
        return self.opened and not self.closed

    @property
    def backend(self) -> str:
        return "REPLAY"

    @property
    def pixel_format(self) -> str:
        return "MJPG"

    def describe(self) -> dict[str, object]:
        return {
            "open": self.is_open,
            "backend": self.backend,
            "width": self._base.shape[1],
            "height": self._base.shape[0],
            "fps": self.fps,
            "device": "replay",
            "fourcc": "MJPG",
            "pixel_format": self.pixel_format,
        }

    def frames(self, *, reconnect: bool = True) -> Iterator[Frame]:
        self.open()
        indices: Iterator[int] = itertools.count() if self.loop else iter(range(len(self.script)))
        for index in indices:
            if index == 1 and not self.armed.wait(timeout=15.0):
                raise AssertionError("the replay source was never armed")
            yield self._frame(index)
            self.read_count += 1
            time.sleep(PACE)

    # -- internals ---------------------------------------------------------

    def _frame(self, index: int) -> Frame:
        image = self._base.copy()
        if self.telemetry:
            payload = encode_payload(
                timestamp_us=FIRST_TIMESTAMP_US + index * FRAME_INTERVAL_US,
                accel_g=self.script[index % len(self.script)],
            )
            image[:, :METADATA_WIDTH] = make_strip(payload)
        offset = index / self.fps
        return Frame(image=image, index=index, monotonic=self._origin + offset, wall_time=BASE_WALL + offset)


def wait_until(condition: Callable[[], bool], *, timeout: float = 20.0) -> bool:
    """Poll until ``condition`` holds, reporting whether it ever did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


def free_port() -> int:
    """Ask the OS for a port nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Client:
    """One keep-alive connection to a test server.

    Deliberately not :mod:`urllib`: these tests send HEAD requests, bogus
    ``Range`` headers and endless streams, and a high-level client hides
    exactly those things.
    """

    def __init__(self, port: int, *, timeout: float = 15.0) -> None:
        self.connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
        self._pending: HTTPResponse | None = None

    def request(self, method: str, path: str, **headers: str) -> HTTPResponse:
        # A keep-alive socket carries one response at a time, so finish reading
        # whatever the last call left behind rather than making every test
        # remember to.
        if self._pending is not None and not self._pending.isclosed():
            self._pending.read()
        self.connection.request(method, path, headers=headers)
        self._pending = self.connection.getresponse()
        return self._pending

    def get(self, path: str, **headers: str) -> HTTPResponse:
        return self.request("GET", path, **headers)

    def post(self, path: str, **headers: str) -> HTTPResponse:
        return self.request("POST", path, **headers)

    def delete(self, path: str, **headers: str) -> HTTPResponse:
        return self.request("DELETE", path, **headers)

    def json(self, path: str, **headers: str) -> Any:
        return json.loads(self.get(path, **headers).read())

    def close(self) -> None:
        self.connection.close()


class StatusProbe(threading.Thread):
    """Fetch ``/api/status`` from outside a run, and remember why not.

    The command binds its socket somewhere inside a call the test thread cannot
    see into, so the only way to catch the server while it is up is to retry
    from another thread until something answers. Retrying blindly is what makes
    this worth a class: a probe that swallows every failure and reports an
    empty list cannot tell a port that was never bound from a response that
    never came, and the difference is the whole diagnosis. Every attempt and
    the last exception are kept so the assertion can say which it was.

    The request timeout is deliberately short. A handler starved of CPU by the
    encoder would otherwise sit inside one attempt for longer than the run
    lasts, which reads from outside exactly like a server that never started.
    """

    #: Seconds to keep trying. Only reached if nothing ever answers, because
    #: :meth:`result` stops the probe as soon as the run under test is over.
    TIMEOUT = 20.0

    #: Seconds one attempt may take. Short enough that a stalled request is
    #: reported as a stall rather than mistaken for silence.
    ATTEMPT_TIMEOUT = 2.0

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True, name=f"status-probe-{port}")
        self.port = port
        self.status: Any = None
        self.attempts = 0
        self.last_error: BaseException | None = None
        self._stop = threading.Event()

    def run(self) -> None:
        deadline = time.monotonic() + self.TIMEOUT
        while not self._stop.is_set() and time.monotonic() < deadline:
            self.attempts += 1
            client = Client(self.port, timeout=self.ATTEMPT_TIMEOUT)
            try:
                self.status = client.json("/api/status")
                return
            except Exception as exc:
                # Anything at all: a refused connection while the server is
                # still coming up, a timeout, or a body that is not JSON. Left
                # to propagate it would kill this thread in silence and the
                # test would fail with no more to say than "nothing answered".
                self.last_error = exc
                self._stop.wait(0.05)
            finally:
                client.close()

    def result(self) -> Any:
        """The status the run served, or an assertion that says what went wrong.

        Call once the run is over: the server is gone by then, so there is
        nothing left to wait for and the probe is wound down rather than left
        to burn through its remaining deadline.
        """
        self._stop.set()
        self.join(timeout=self.ATTEMPT_TIMEOUT + 5.0)
        if self.status is None:
            raise AssertionError(
                f"nothing answered on port {self.port} while the run was up: "
                f"{self.attempts} attempt(s), last error {self.last_error!r}"
                + (", probe still running" if self.is_alive() else "")
            )
        return self.status


@pytest.fixture
def config(config: EngineConfig) -> EngineConfig:
    """The shared config, pinned so retention cannot fire by accident.

    The defaults prune once the volume drops below 2 GB free, which would make
    every test here depend on how full the developer's disk happens to be.
    """
    config.recording.min_free_bytes = 0
    config.recording.max_bytes = 1024**3
    config.recording.max_event_bytes = 1024**3
    return config
