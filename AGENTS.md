# Working on Vectra-180

Orientation for anyone — human or agent — changing this codebase.

## What it is

A dashcam. It reads a dual-fisheye USB (UVC) camera, records segmented clips to
disk on an endless loop, protects clips when its g-sensor feels an impact, and
serves a live view and a clip browser over HTTP. Stereoscopic depth is a
secondary capability, computed only when something asks for it.

The target hardware is a Raspberry Pi Compute Module 5 on the CM5 IO board,
running headless in a car. That constraint decides most of the design: the CM5
has no hardware H.264 encoder, so every recorded frame is compressed by libx264
on the CPU, and anything that competes with that budget is suspect.

Everything is Python. There is no GPU pipeline and no browser-side renderer —
the web interface is served by the package itself from
`src/vectra180/service/static/`.

## Layout

```
src/vectra180/
  cli.py          argparse entry point; every subcommand lives here
  config.py       dataclass config; defaults -> file -> VECTRA_* env -> flags
  engine.py       the capture thread; owns the pipeline and the frame clock
  doctor.py       hardware pre-flight, exercising the real path
  errors.py       VectraError and its five subclasses; everything raised here
  capture/        device enumeration, backend selection, reconnection
  telemetry/      ICM42688 decoder and the orientation filter
  imaging/        dewarp, stitch, stabilise, depth, HUD, layout
  recorder/       segmenter, writers, retention, incident detection
  service/        threaded HTTP server, JSON API, MJPEG, static UI
  ui/             optional DearPyGui desktop panel
```

## Two rules worth knowing before you change anything

**The frame carries the clock.** Every time the pipeline needs to know when it
is, it reads `Frame.monotonic` or `Frame.wall_time` rather than calling the
clock itself. Segment rollover, clip naming, telemetry offsets and retention all
follow from those two fields. This is what lets the integration suite replay
minutes of footage in milliseconds through a fake source. Calling
`time.monotonic()` inside the pipeline breaks that, and the tests will say so.

**Recording is the duty; everything else is garnish.** A missing IMU, an
unreachable web interface, a camera that vanished mid-drive — none of these may
stop clips being written. Degrade, log, keep recording.

## Telemetry format

The camera module embeds an ICM-42688 block in the frame's **first pixel
column**, one byte per row. The wider metadata strip that `telemetry.metadata_width`
crops away — 30 columns by default — contains it. The payload is 20 bytes:

- 8 bytes: little-endian `uint64`, timestamp in microseconds
- 12 bytes: six **big-endian** `int16` — accel X/Y/Z then gyro X/Y/Z

Scales are `16384 LSB/g` for the accelerometer and `16.4 LSB/(deg/s)` for the
gyroscope. See `docs/telemetry.md` for the full derivation.

## Conventions

- `make gate` before you claim anything works: ruff format, ruff check, mypy,
  then the full pytest suite. CI runs exactly that.
- Type annotations everywhere. `mypy src tests` is clean and stays clean.
- Tests describe behaviour, not implementation. The docstring says why the test
  exists; if it only restates the assertion, it is not earning its place.
- Comments explain *why*. The code already says what.
- Documentation states what the code does today. No changelog-in-prose, no
  "previously this was broken", no aspirational features.
