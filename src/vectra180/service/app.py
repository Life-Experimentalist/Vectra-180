"""The headless HTTP interface.

Built on the standard library's :class:`~http.server.ThreadingHTTPServer`. That
is a deliberate choice over a web framework: the Pi image stays at zero extra
runtime dependencies, the attack surface is a few hundred lines that can be
read in full, and there is no ASGI server competing with the encoder for CPU.

Security posture
----------------

Recorded footage is sensitive -- it shows where the vehicle has been and who
was in it. Therefore:

* The default bind address is ``127.0.0.1``. Reaching the UI from a phone is an
  explicit choice the operator makes, not the default.
* Binding anywhere else without setting ``server.token`` logs a warning at
  startup, every time.
* Tokens are compared with :func:`hmac.compare_digest`, so a wrong guess takes
  the same time as a right one.
* Clip names from the network are matched against the on-disk inventory rather
  than joined onto a path, so no name can address a file outside the recording
  directory.
* Requests that change state are refused when they carry a cross-origin
  ``Origin`` header. Without that, any page open in the driver's browser could
  submit a form that stops the recording -- the default loopback bind has no
  token to stand in the way.
* Nothing here executes a shell, and no request data reaches a subprocess.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import re
import socket
import socketserver
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

import cv2

from vectra180 import __version__
from vectra180.config import EngineConfig
from vectra180.engine import Engine
from vectra180.errors import ServiceError
from vectra180.recorder import delete_clip, list_clips, protect_clip, resolve_clip, storage_stats

__all__ = ["VectraHTTPServer", "serve"]

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Content types for the bundled UI assets.
#:
#: These are pinned rather than looked up with :func:`mimetypes.guess_type`,
#: which consults the Windows registry and reports ``text/plain`` for ``.js``
#: on some installs. Paired with the ``nosniff`` header that would leave the
#: panel a blank page, so the mapping is stated here instead of inherited
#: from whatever the host machine happens to believe.
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/vnd.microsoft.icon",
    ".webmanifest": "application/manifest+json",
}

#: Bytes per chunk when streaming a clip. Large enough to keep the socket
#: busy, small enough that a cancelled download frees memory promptly.
_CHUNK = 256 * 1024

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)\Z")

_MJPEG_BOUNDARY = "vectraframe"

#: No endpoint takes a request body, but one still has to be read off the
#: socket. Anything larger than this is refused rather than buffered.
_MAX_BODY = 64 * 1024

#: Methods that change something, and so need the cross-origin check.
_UNSAFE_METHODS = frozenset({"POST", "DELETE"})


class VectraHTTPServer(ThreadingHTTPServer):
    """Threading server carrying a reference to the running engine."""

    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    def __init__(self, address: tuple[str, int], engine: Engine, config: EngineConfig) -> None:
        self.engine = engine
        self.app_config = config
        # An IPv6 literal such as ``::1`` cannot be bound on an IPv4 socket,
        # and a host that contains a colon is one by construction.
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, VectraRequestHandler)

    def server_bind(self) -> None:
        """Bind without asking a resolver what the bound address is called.

        The base class fills ``server_name`` in from :func:`socket.getfqdn`,
        which is a reverse lookup issued after the socket is bound and before
        it is listening. A dashcam runs where there is nothing to answer it --
        no uplink, and often no clock to validate one with -- so the call sits
        there for the resolver's timeout, and the UI is unreachable for every
        second of it. The name it produces is only ever read back into CGI
        variables this server does not serve, so the address itself does.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        # Narrowed rather than converted: the address family is pinned to
        # AF_INET or AF_INET6 above, and both carry a textual host.
        self.server_name = cast("str", host)
        self.server_port = port

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Report a failed request without treating a hang-up as a fault.

        A phone that leaves Wi-Fi mid-stream, a tab closed during a clip
        download, and a keep-alive socket dropped between requests all arrive
        here, and the base class prints a full traceback to stderr for each
        one. For a dashcam whose UI is a phone that comes and goes, that is
        the ordinary case rather than the exceptional one, and it would bury
        the recorder's own messages. Those are logged at debug; anything else
        is a real fault and keeps its traceback.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError | TimeoutError):
            log.debug("client %s hung up: %s", client_address, exc)
            return
        log.exception("unhandled error while serving %s", client_address)


class VectraRequestHandler(BaseHTTPRequestHandler):
    """Routes and responses for the dashcam UI and its JSON API."""

    server_version = f"Vectra180/{__version__}"
    # HTTP/1.1 so the MJPEG stream and ranged downloads work without the
    # client reconnecting for every response.
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    @property
    def _server(self) -> VectraHTTPServer:
        """``self.server`` narrowed from the base class's ``BaseServer``."""
        return cast("VectraHTTPServer", self.server)

    @property
    def engine(self) -> Engine:
        return self._server.engine

    @property
    def app_config(self) -> EngineConfig:
        return self._server.app_config

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature fixed by the base class
        log.debug("%s - %s", self.address_string(), format % args)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _authorized(self) -> bool:
        """Check the bearer token, if one is configured.

        Both the header and a query parameter are accepted: ``<video>`` and
        ``<img>`` elements cannot carry an Authorization header, so a
        header-only scheme would make the preview unusable.

        Comparison is on bytes rather than text, because
        :func:`hmac.compare_digest` refuses non-ASCII strings -- and a client
        is free to send one.
        """
        token = self.app_config.server.token
        if not token:
            return True
        expected = token.encode("utf-8")

        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode("utf-8"), expected):
            return True
        supplied = self._query().get("token", [""])[0]
        return hmac.compare_digest(supplied.encode("utf-8"), expected)

    def _same_origin(self) -> bool:
        """Whether a state-changing request came from the UI itself.

        A browser attaches ``Origin`` to every cross-site request it makes and
        a page cannot forge it. Non-browser clients -- curl, the CLI -- send
        none at all, which is why a missing header is accepted.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlparse(origin).netloc == self.headers.get("Host", "")

    def _drain_body(self) -> bool:
        """Consume any request body, reporting whether the socket is still sane.

        No endpoint takes a body, but leaving one unread desynchronises the
        next request on a keep-alive connection: its first line gets parsed out
        of the previous body. Anything oversized, malformed, or chunked -- which
        the base handler cannot frame -- has to close the connection instead.
        """
        if self.headers.get("Transfer-Encoding"):
            return False
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return False
        if not 0 <= length <= _MAX_BODY:
            return False
        if length:
            self.rfile.read(length)
        return True

    # -- response helpers --------------------------------------------------

    def _send(
        self,
        status: HTTPStatus,
        body: bytes = b"",
        content_type: str = "text/plain; charset=utf-8",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The UI is same-origin and entirely self-hosted, so everything
        # external is denied outright.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "style-src 'self'; script-src 'self'; frame-ancestors 'none'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message, "status": int(status)}, status)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        route = unquote(urlparse(self.path).path).rstrip("/") or "/"

        if not self._drain_body():
            # The body is still on the socket, so this connection can no longer
            # be framed. ``Connection: close`` both tells the client and makes
            # the base handler drop the socket after this response.
            self._send(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                b"unsupported request body",
                extra={"Connection": "close"},
            )
            return

        # Liveness must answer before auth so a supervisor can watch the
        # service without holding the operator's token.
        if route == "/healthz":
            self._json({"status": "ok", "version": __version__})
            return

        if method in _UNSAFE_METHODS and not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            return

        if not self._authorized():
            self._send(
                HTTPStatus.UNAUTHORIZED,
                b"unauthorized",
                extra={"WWW-Authenticate": 'Bearer realm="vectra180"'},
            )
            return

        try:
            handled = self._route(method, route)
        except ConnectionError:
            # The browser closed a preview or a download. Routine, not an error.
            # The whole family is caught because which one a dropped peer raises
            # is platform-specific: a broken pipe on Linux, an aborted
            # connection on Windows.
            return
        except (FileNotFoundError, ValueError) as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
            return
        except OSError as exc:
            log.exception("request failed: %s %s", method, route)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        if not handled:
            self._error(HTTPStatus.NOT_FOUND, f"no route for {method} {route}")

    def _route(self, method: str, route: str) -> bool:
        if method == "GET":
            return self._route_get(route)
        if method == "POST":
            return self._route_post(route)
        if method == "DELETE":
            return self._route_delete(route)
        return False

    def _route_get(self, route: str) -> bool:
        if route == "/":
            self._serve_static("index.html")
        elif route.startswith("/static/"):
            self._serve_static(route[len("/static/") :])
        elif route == "/api/status":
            self._json(self.engine.status())
        elif route == "/api/config":
            self._json(self.app_config.to_dict())
        elif route == "/api/clips":
            self._json({"clips": [clip.as_dict() for clip in list_clips(self.app_config.recording)]})
        elif route == "/api/storage":
            self._json(storage_stats(self.app_config.recording).as_dict())
        elif route.startswith("/api/clips/"):
            self._serve_clip(route[len("/api/clips/") :])
        elif route == "/snapshot.jpg":
            self._serve_snapshot()
        elif route == "/depth.jpg":
            self._serve_depth()
        elif route == "/stream.mjpg":
            self._serve_stream()
        else:
            return False
        return True

    def _route_post(self, route: str) -> bool:
        if route == "/api/lock":
            incident = self.engine.lock_incident()
            self._json({"locked": True, "incident": incident.as_dict()})
        elif route.startswith("/api/clips/") and route.endswith("/protect"):
            name = route[len("/api/clips/") : -len("/protect")]
            self._json({"clip": protect_clip(self.app_config.recording, name).as_dict()})
        elif route == "/api/recording/start":
            self.engine.begin_recording()
            self._json({"recording": self.engine.recorder.running})
        elif route == "/api/recording/stop":
            self.engine.recorder.stop()
            self._json({"recording": self.engine.recorder.running})
        else:
            return False
        return True

    def _route_delete(self, route: str) -> bool:
        if route.startswith("/api/clips/"):
            delete_clip(self.app_config.recording, route[len("/api/clips/") :])
            self._json({"deleted": True})
            return True
        return False

    # -- handlers ----------------------------------------------------------

    def _serve_static(self, relative: str) -> None:
        """Serve a bundled UI asset.

        The resolved path must stay inside ``static/``; ``..`` segments and
        absolute paths are rejected by comparing resolved parents.
        """
        candidate = (STATIC_DIR / relative).resolve()
        if not candidate.is_file() or STATIC_DIR.resolve() not in candidate.parents:
            self._error(HTTPStatus.NOT_FOUND, f"no such asset: {relative}")
            return
        content_type = _STATIC_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)

    def _encode_jpeg(self, image: Any) -> bytes:
        quality = self.app_config.server.preview_quality
        ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise OSError("JPEG encoding failed")
        return bytes(buffer)

    def _panorama_requested(self) -> bool:
        """Whether ``?view=pano`` asked for the joined, levelled view.

        Anything else means the raw side-by-side frame, which is what the
        recorder writes and therefore the honest default.
        """
        return self._query().get("view", [""])[0] == "pano"

    def _serve_snapshot(self) -> None:
        overlay = self._query().get("overlay", ["1"])[0] != "0"
        image = self.engine.preview_frame(overlay=overlay, panorama=self._panorama_requested())
        if image is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "no frame captured yet")
            return
        self._send(HTTPStatus.OK, self._encode_jpeg(image), "image/jpeg", {"Cache-Control": "no-store"})

    def _serve_depth(self) -> None:
        depth = self.engine.compute_depth()
        if depth is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "no frame captured yet")
            return
        self._send(HTTPStatus.OK, self._encode_jpeg(depth), "image/jpeg", {"Cache-Control": "no-store"})

    def _serve_stream(self) -> None:
        """Stream MJPEG until the client disconnects.

        Frames are pulled at ``server.preview_fps``, well under the capture
        rate, so a watching phone costs a fixed slice of CPU rather than
        doubling the pipeline's work.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Length is unknown and the stream never ends; closing on disconnect
        # is the only correct framing.
        self.send_header("Connection", "close")
        self.end_headers()

        # A HEAD asks what the response would look like, not for an endless one.
        if self.command == "HEAD":
            return

        interval = 1.0 / max(1, self.app_config.server.preview_fps)
        panorama = self._panorama_requested()
        last_index = -1
        try:
            while True:
                deadline = time.monotonic() + interval
                snapshot = self.engine.snapshot()
                if snapshot is not None and snapshot.frame_index != last_index:
                    last_index = snapshot.frame_index
                    image = self.engine.preview_frame(panorama=panorama)
                    if image is not None:
                        payload = self._encode_jpeg(image)
                        self.wfile.write(
                            f"--{_MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        )
                        self.wfile.write(payload)
                        self.wfile.write(b"\r\n")
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        except ConnectionError:
            # The viewer closing the tab is the only way this loop ever ends.
            pass

    def _serve_clip(self, name: str) -> None:
        """Send a recorded clip, honouring a single Range request.

        Range support is what lets a browser seek inside an MP4 without
        downloading the whole segment first.
        """
        clip = resolve_clip(self.app_config.recording, name)
        size = clip.path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{clip.name}"',
            # This response is written directly rather than through _send, so it
            # has to carry the sniffing guard itself.
            "X-Content-Type-Options": "nosniff",
        }

        # ``bytes=-`` names neither end, so there is no range to honour: the
        # spec says to ignore a header like that and send the whole file.
        match = _RANGE_RE.match(self.headers.get("Range", ""))
        if match and any(match.groups()):
            raw_start, raw_end = match.groups()
            if raw_start:
                start = int(raw_start)
                end = int(raw_end) if raw_end else size - 1
            elif raw_end:
                # "bytes=-500" means the final 500 bytes.
                start = max(0, size - int(raw_end))
            if start >= size or start > end:
                self._send(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    b"",
                    extra={"Content-Range": f"bytes */{size}"},
                )
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(clip.name)[0] or "video/mp4")
        self.send_header("Content-Length", str(length))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

        if self.command == "HEAD":
            return

        with clip.path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def serve(engine: Engine, config: EngineConfig, *, block: bool = True) -> VectraHTTPServer:
    """Start the HTTP service.

    Args:
        block: when ``True`` this call serves forever; when ``False`` it
            returns after starting a background thread, which is what the
            tests and the desktop UI want.

    Raises:
        ServiceError: if the address is already in use or not assignable.
    """
    settings = config.server
    if settings.is_public and not settings.token:
        log.warning(
            "server.host is %s with no token set: every recorded clip is readable "
            "by anyone on this network. Set server.token or bind to 127.0.0.1.",
            settings.host,
        )

    try:
        server = VectraHTTPServer((settings.host, settings.port), engine, config)
    except OSError as exc:
        raise ServiceError(f"could not bind {settings.host}:{settings.port}: {exc}") from exc

    log.info("serving on http://%s:%d/", settings.host, settings.port)
    if block:
        try:
            server.serve_forever()
        finally:
            server.server_close()
    else:
        threading.Thread(target=server.serve_forever, name="vectra-http", daemon=True).start()
    return server
