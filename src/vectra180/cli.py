"""Command-line entry point.

::

    vectra180 run       record and serve -- the systemd entry point
    vectra180 view      desktop control panel (needs the 'desktop' extra)
    vectra180 devices   list attached cameras
    vectra180 doctor    check that this machine can actually record
    vectra180 decode    read the IMU block out of a captured frame
    vectra180 config    print the effective configuration

Nothing here imports a GUI toolkit at module level, so ``vectra180 run`` starts
on a headless Pi with no display libraries installed.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Any

from vectra180 import __version__
from vectra180.config import EngineConfig, default_config_path
from vectra180.errors import VectraError

__all__ = ["build_parser", "main"]

log = logging.getLogger("vectra180")

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _configure_logging(verbosity: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbosity else logging.INFO)
    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt="%H:%M:%S")


def _load_config(args: argparse.Namespace) -> EngineConfig:
    """Build the config, then apply the command-line overrides on top."""
    config = EngineConfig.load(args.config)

    if args.camera is not None:
        config.camera.index = args.camera
    if args.device:
        config.camera.device = args.device
    if args.recording_dir:
        config.recording.directory = Path(args.recording_dir).expanduser()

    for attribute, value in (("host", getattr(args, "host", None)), ("token", getattr(args, "token", None))):
        if value:
            setattr(config.server, attribute, value)
    if getattr(args, "port", None):
        config.server.port = args.port

    config.validate()
    return config


# -- toml output -------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))


def _to_toml(config: EngineConfig, *, redact: bool) -> str:
    """Emit the config as TOML.

    Hand-rolled because the standard library reads TOML but does not write it,
    and the shape here is fixed: flat sections of scalars. The output is
    accepted by ``--config`` unchanged, which is the point -- it is how you
    capture a working setup into a file.

    A redacted token is commented out rather than written as ``"***"``, so the
    file stays valid instead of quietly becoming the wrong password.
    """
    lines = [f"# Vectra-180 {__version__} configuration"]
    for section, values in config.to_dict(redact=redact).items():
        lines.append(f"\n[{section}]")
        for key, value in values.items():
            if value == "***":
                lines.append(f"# {key} = <redacted -- re-run with --show-secrets>")
            else:
                lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


# -- commands ----------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Record and serve until told to stop. The systemd entry point."""
    from vectra180.engine import Engine
    from vectra180.service import serve

    config = _load_config(args)
    if args.no_record:
        config.recording.enabled = False
    if args.no_serve:
        config.server.enabled = False

    engine = Engine(config)
    stopping = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stopping.set()

    # systemd sends SIGTERM on 'systemctl stop'; a terminal sends SIGINT. Both
    # must finalise the open segment rather than leave a truncated file.
    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is not None:
            signal.signal(handler, _handle)

    server = None
    try:
        engine.start()
        if config.recording.enabled:
            engine.begin_recording()
            log.info("recording to %s", config.recording.directory)
        if config.server.enabled:
            server = serve(engine, config, block=False)
            log.info("web interface on http://%s:%d/", config.server.host, config.server.port)

        stopping.wait(timeout=args.duration if args.duration else None)
        if args.duration:
            log.info("duration of %.0fs elapsed", args.duration)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        engine.stop()

    stats = engine.recorder.stats
    log.info(
        "wrote %d segment(s), %d frame(s), dropped %d",
        stats.segments_written,
        stats.written_frames,
        stats.dropped_frames,
    )
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    """Open the desktop control panel."""
    from vectra180.ui import launch

    return launch(_load_config(args))


def cmd_devices(args: argparse.Namespace) -> int:
    """List capture devices that respond to a probe."""
    from vectra180.capture import enumerate_devices

    devices = enumerate_devices(max_index=args.max_index)
    if args.json:
        print(json.dumps([device.__dict__ for device in devices], indent=2))
        return 0 if devices else 1

    if not devices:
        print("No capture devices responded.")
        print("On Linux, check that /dev/video* exists and that you are in the 'video' group.")
        return 1

    for device in devices:
        print(f"{device.label}  {device.width}x{device.height} @ {device.fps:.0f}fps via {device.backend}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that this machine can record with this camera."""
    from vectra180.doctor import run_diagnostics

    report = run_diagnostics(_load_config(args), probe_camera=not args.no_camera)
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return 0 if report.ok else 1


def cmd_decode(args: argparse.Namespace) -> int:
    """Decode the IMU block embedded in a captured frame."""
    import cv2

    from vectra180.imaging import strip_metadata
    from vectra180.telemetry import TelemetryDecoder, TelemetrySample

    path = Path(args.image)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    image = cv2.imread(str(path))
    if image is None:
        print(f"could not decode an image from {path}", file=sys.stderr)
        return 1

    # Checked here rather than left to strip_metadata, which raises on a strip
    # wider than the frame -- a mistyped flag deserves a sentence, not a stack.
    if args.metadata_width >= image.shape[1]:
        print(
            f"the image is {image.shape[1]}px wide -- too narrow for a {args.metadata_width}px strip",
            file=sys.stderr,
        )
        return 1

    _, strip = strip_metadata(image, args.metadata_width)
    if strip is None:
        print("--metadata-width must be at least 1 for there to be a strip to read", file=sys.stderr)
        return 1

    payload = TelemetryDecoder.payload_from_strip(strip)
    # Decoded directly rather than through a TelemetryDecoder, which accepts a
    # sample only once a second frame continues its clock. That guard is right
    # for the live pipeline and useless here: a still image has no next frame,
    # so the decoder would reject every file this command is ever given.
    sample: TelemetrySample | None = None
    reason = ""
    try:
        sample = TelemetrySample.from_bytes(payload)
    except ValueError as exc:
        reason = str(exc)

    if args.json:
        print(
            json.dumps(
                {
                    "payload_hex": payload.hex(),
                    "sample": sample.as_dict() if sample else None,
                    "error": reason or None,
                },
                indent=2,
            )
        )
        return 0 if sample else 1

    print(f"{path.name}: {image.shape[1]}x{image.shape[0]}")
    print(f"payload: {' '.join(f'{byte:02X}' for byte in payload)}")
    if sample is None:
        print(f"\nNo valid IMU block in the metadata strip: {reason}")
        print("Check --metadata-width against the camera, or the module may not embed telemetry at all.")
        return 1

    print(f"\ntimestamp : {sample.timestamp_us}")
    print(f"accel     : {sample.accel_x:+8.3f} {sample.accel_y:+8.3f} {sample.accel_z:+8.3f}  m/s^2")
    print(f"gyro      : {sample.gyro_x:+8.3f} {sample.gyro_y:+8.3f} {sample.gyro_z:+8.3f}  rad/s")
    print(f"magnitude : {sample.accel_magnitude_g:.3f} g")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Print the effective configuration."""
    config = _load_config(args)
    redact = not args.show_secrets
    if args.json:
        print(json.dumps(config.to_dict(redact=redact), indent=2, default=str))
    else:
        print(_to_toml(config, redact=redact), end="")
    if args.path:
        print(f"\n# config file: {args.config or default_config_path()}", file=sys.stderr)
    return 0


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vectra180",
        description="Dual-fisheye dashcam and stereoscopic depth engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  vectra180 doctor                 check the camera, encoder and disk\n"
            "  vectra180 run                    record and serve until stopped\n"
            "  vectra180 run --duration 30      record a 30 second test\n"
            "  vectra180 config > my.toml       capture the current settings\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"vectra180 {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", help="TOML config file (default: platform config directory)")
    common.add_argument("--camera", type=int, metavar="N", help="camera index override")
    common.add_argument("--device", metavar="PATH", help="explicit device path, e.g. /dev/video0")
    common.add_argument("--recording-dir", metavar="PATH", help="where clips are written")
    common.add_argument("-v", "--verbose", action="count", default=0, help="debug logging")
    common.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = subparsers.add_parser("run", parents=[common], help="record and serve (the service entry point)")
    run.add_argument("--duration", type=float, metavar="SECONDS", help="stop after this long instead of forever")
    run.add_argument("--no-record", action="store_true", help="serve the live view without writing clips")
    run.add_argument("--no-serve", action="store_true", help="record without starting the web interface")
    run.add_argument("--host", metavar="ADDR", help="bind address (0.0.0.0 exposes clips to the network)")
    run.add_argument("--port", type=int, metavar="PORT", help="listen port")
    run.add_argument("--token", metavar="SECRET", help="require this token on every request")
    run.set_defaults(func=cmd_run)

    view = subparsers.add_parser("view", parents=[common], help="open the desktop control panel")
    view.set_defaults(func=cmd_view)

    devices = subparsers.add_parser("devices", parents=[common], help="list attached cameras")
    devices.add_argument("--max-index", type=int, default=10, metavar="N", help="highest index to probe")
    devices.add_argument("--json", action="store_true", help="machine-readable output")
    devices.set_defaults(func=cmd_devices)

    doctor = subparsers.add_parser("doctor", parents=[common], help="check that this machine can record")
    doctor.add_argument("--no-camera", action="store_true", help="skip the checks that need hardware")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(func=cmd_doctor)

    decode = subparsers.add_parser("decode", parents=[common], help="read the IMU block out of a frame")
    decode.add_argument("image", help="an image file captured from the camera")
    decode.add_argument("--metadata-width", type=int, default=30, metavar="PX", help="width of the metadata strip")
    decode.add_argument("--json", action="store_true", help="machine-readable output")
    decode.set_defaults(func=cmd_decode)

    config = subparsers.add_parser("config", parents=[common], help="print the effective configuration")
    config.add_argument("--json", action="store_true", help="JSON instead of TOML")
    config.add_argument("--show-secrets", action="store_true", help="print the auth token instead of redacting it")
    config.add_argument("--path", action="store_true", help="also report which file was read, on stderr")
    config.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    try:
        result: int = args.func(args)
        return result
    except VectraError as exc:
        log.error("%s", exc)
        return 1
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    except ValueError as exc:
        # Raised by config validation and by backend name resolution: a user
        # mistake, not a crash, so it gets one line rather than a traceback.
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
