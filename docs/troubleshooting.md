# Troubleshooting

Symptoms, causes, fixes. Start here:

```bash
vectra180 doctor
```

Every check exercises the real path — the camera is opened, frames are read, the
encoder is timed at the resolution the camera actually produced, and the
recording volume is written to. Most of what follows is a longer explanation of
something `doctor` has already told you in one line.

---

## Nothing records at all

### `doctor` says no capture device responded

```
[FAIL] devices: no capture device responded to a probe
```

In order:

```bash
lsusb                    # is the camera enumerated at all?
ls -l /dev/video*        # did the kernel create nodes for it?
groups                   # are you in the 'video' group?
```

| Cause | Fix |
|---|---|
| Not in the `video` group | `sudo usermod -aG video $USER`, then log out and back in |
| No `/dev/video*` at all | A power problem, not a software one — see [Power](#the-camera-drops-out-on-bumps-or-when-the-engine-starts) |
| Another process holds it | Close it. `sudo fuser -v /dev/video0` names it |
| Running in a container | Pass the device: `--device /dev/video0` |

### `doctor` says the device opened but returned no frames

The camera enumerated, negotiated a mode, and delivered nothing. Almost always
another process holding it, or a USB bandwidth problem on a shared hub.

Give it a dedicated USB 3 port. On a CM5 IO board, the two USB-A ports share a
controller with anything on the internal header.

### `doctor` says the driver gave a different mode

```
[warn] camera: 1280x720 via V4L2, 30.0 fps measured; driver gave 1280x720,
       not the requested 2560x720
```

The requested mode is unsupported at that pixel format, so the driver quietly
negotiated down. List what the device actually offers:

```bash
v4l2-ctl --device /dev/video0 --list-formats-ext
```

Then either set `camera.width`/`camera.height` to a mode that appears in that
list, or fix `camera.fourcc`. **This is nearly always a `fourcc` problem** —
YUYV cannot sustain 2560×720 at 30 fps over USB 2.0, so a camera asked for YUYV
at that size will negotiate down to something it can do. `MJPG` is the default
for exactly this reason.

### The service will not start

```bash
sudo systemctl status vectra180
sudo journalctl -u vectra180 -n 50
```

| Message | Cause |
|---|---|
| `could not bind 0.0.0.0:8080: Address already in use` | Something else has the port, or a previous instance is still shutting down |
| `config file not found` | `--config` or `VECTRA_CONFIG` points at a file that does not exist |
| `camera.fourcc must be exactly four characters` | A config value is invalid. The message names the key |
| `no such file or directory: /var/lib/vectra180/recordings` | The recording directory is missing or not owned by the service user |

Config errors are reported as one line and exit `1`. They are user mistakes, not
crashes, so there is no traceback to read — the line names the file and the key.

---

## Dropped frames

```json
{ "recorder": { "dropped_frames": 4127, "written_frames": 71483 } }
```

Visible in `/api/status`, on the HUD, and in the summary `run` prints on exit.

**Dropped frames mean the encoder cannot keep up.** The recorder's queue holds
about two seconds of footage; when it fills, `submit()` drops the frame rather
than blocking the camera or growing without limit. Nothing is wrong with the
camera — the frames arrived and there was nowhere to put them.

The Compute Module 5 has **no hardware H.264 encoder**. libx264 runs on the
Cortex-A76 cores, and 2560×720 at 30 fps is genuinely close to the limit.

Confirm with the benchmark:

```bash
vectra180 doctor --json | jq '.checks[] | select(.name=="encoder")'
```

Then, in order of how much you lose:

| Change | Cost |
|---|---|
| `recording.preset = "ultrafast"` | Nothing, if you had moved off it. This is the default |
| `recording.bitrate_kbps` down to 6000 | Slightly softer image |
| `camera.fps = 24` | Fewer frames, same coverage |
| `camera.width`/`height` to a smaller mode | Real detail loss — do this last |

Check for a thermal problem before changing anything:

```bash
vcgencmd measure_temp && vcgencmd get_throttled
```

`get_throttled` returning anything but `0x0` means the SoC has been clocked down.
A CM5 encoding continuously in a car in summer **will** throttle without a
heatsink, and a throttled CM5 drops frames it handled fine on the bench. See
[Thermals](../deploy/README.md#8-thermals).

Background load matters too — a system update running mid-drive is enough.

---

## The camera drops out on bumps or when the engine starts

The recorder handles this: after `camera.read_failure_limit` consecutive failed
reads (default 30, about a second at 30 fps) the source closes, waits
`camera.reconnect_delay`, and reopens. It retries forever, because a camera that
browns out over a speed bump should come back on its own rather than end the
drive.

If it happens often, it is a **power or connector** problem, not software:

- Use a short, thick USB cable. Long thin ones drop enough voltage to reset a UVC camera under load.
- Avoid unpowered hubs.
- A car's 12 V rail sags hard during cranking. See [Power and ignition](../deploy/README.md#7-power-and-ignition).

Look for the reconnects in the log:

```bash
sudo journalctl -u vectra180 | grep -i reconnect
```

---

## Telemetry

### `doctor` says no IMU block decoded

```
[warn] telemetry: no IMU block decoded from the metadata strip
```

**Not every dual-fisheye module embeds one.** This is a warning, not a failure.
Recording, preview, the panorama, depth and the web UI all work without it. The
one thing you lose is automatic incident detection — the manual lock button still
works.

Before concluding your camera has none, check the strip width. Capture a frame
straight from the device in a **lossless** format and inspect it:

```bash
ffmpeg -f v4l2 -input_format mjpeg -video_size 2560x720 -i /dev/video0 -frames:v 1 raw.png
vectra180 decode raw.png
vectra180 decode raw.png --metadata-width 16
vectra180 decode raw.png --metadata-width 64
```

**Do not use JPEG for this.** The metadata column is not image data, and chroma
subsampling and DCT quantisation will average it into its neighbours — a JPEG
round trip destroys the payload even at quality 100.

If a width produces a sensible reading — `magnitude` near `1.00 g` on a
stationary camera, small gyro values — set `telemetry.metadata_width` to it.

If nothing works, your module does not emit the block:

```toml
[telemetry]
enabled = false
```

That stops `doctor` warning about it, and stops the crop taking pixels off the
left of every frame.

### A stripe of noise down the left edge of every clip

`telemetry.metadata_width` is smaller than the real strip. The leftover columns
are being recorded as if they were picture. Widen it.

### A slice of the left fisheye is missing

The opposite: `metadata_width` is larger than the real strip, so it is eating
image. Narrow it.

### The horizon is level but slowly rotates

That is yaw drift, and it is expected. Roll and pitch have gravity as an absolute
reference; yaw has none without a magnetometer. It is bled toward zero with a
20-second time constant (`telemetry.yaw_leak_seconds`), which keeps it useful for
*rate* of turn while refusing to pretend it is a compass.

### The horizon tilts into corners

`telemetry.gravity_tolerance_g` is too wide, so the filter is trusting
accelerometer readings that are mostly cornering force rather than gravity.
Lower it — `0.15` is a reasonable tighter value. The cost is slower recovery
after a genuine tilt.

---

## Incidents

### It never triggers

In order:

1. `curl .../api/status | jq '.telemetry.present'` — `false` means there is no IMU to trigger from.
2. `jq '.incidents.peak_g'` — the largest deviation seen so far. If your hardest brake produced 0.3 and `threshold_g` is 0.6, it was never going to fire.
3. Lower `incident.threshold_g` toward that peak.

### It triggers constantly

`events/` fills with clips from ordinary driving. `incident.threshold_g` is too
low for your roads and mounting. Raise it — `0.8` is a good next step — and check
the mount is rigid. A camera on a flexing suction mount amplifies every bump into
the accelerometer.

### A locked clip is missing

Locking marks the **current** segment; the move to `events/` happens when that
segment closes. With `segment_seconds = 60`, a clip locked at second 3 appears in
`events/` 57 seconds later. `POST /api/lock` returns immediately regardless.

---

## Storage

### The card filled up anyway

`recording.max_bytes` counts **loop footage only**. `events/` has its own budget,
`max_event_bytes`, which the loop pruner never touches. If incidents are firing
often, events can grow to that budget on top of your loop budget.

Total worst case is `max_bytes + max_event_bytes`, plus whatever else lives on
the card. Check the split:

```bash
curl -s .../api/storage | jq
```

Then either lower one of the budgets, raise `min_free_bytes`, or raise
`incident.threshold_g` so fewer clips get locked.

### Old clips are not being pruned

| Cause | Check |
|---|---|
| Both budgets are already satisfied | `/api/storage` — pruning only runs when it needs to |
| The clips are in `events/` | Locked clips are exempt by design. Delete them explicitly |
| The names do not match the pattern | Renamed files are invisible to the pruner *and* to the API |
| Permissions | `journalctl -u vectra180 \| grep "could not delete"` |

Only one file matching `^VEC_\d{8}_\d{6}(_[A-Za-z0-9-]+)?\.(mp4\|mkv\|avi)$` is
ever touched. That is deliberate: your own files in those directories are never
deleted.

Pruning also always leaves one clip behind, so a directory with a single enormous
clip in it will not be emptied.

### A clip will not play, or has no duration

| Symptom | Cause |
|---|---|
| Duration reads `0` in the browser | The `.json` sidecar is missing or unreadable. The video is fine |
| The last clip of a drive is broken | Power was cut mid-segment. `+faststart` makes most of these still playable |
| Every clip is broken | The OpenCV fallback writer is in use with a codec your player does not know. Install ffmpeg |

Check which encoder is running:

```bash
curl -s .../api/status | jq '.recorder.encoder'
```

`OpenCVWriter` when you expected `FFmpegWriter` means ffmpeg is not on `PATH`:

```bash
sudo apt install ffmpeg
```

### `stats.last_error` is non-empty

A segment failed to write. The file was discarded, a fresh segment opened on the
next frame, and the session continued — one bad segment never ends a recording.
The message says why; ffmpeg's own stderr is included when it is ffmpeg that
died.

---

## The web interface

### It will not load from my phone

Two changes, and neither works alone:

```toml
[server]
host = "0.0.0.0"
token = "a-long-random-secret"
```

Then `sudo systemctl restart vectra180`. `doctor` **fails** if `host` is public
and `token` is empty — that is not advice, it is the exit code, because otherwise
anyone on the network could download and delete your footage.

If it still will not connect:

```bash
ss -ltnp | grep 8080          # is it listening on 0.0.0.0, or still 127.0.0.1?
sudo ufw status               # is a firewall in the way?
```

Full procedure, including why a hotspot beats the car park's open Wi-Fi:
[Reaching it from a phone](../deploy/README.md#10-reaching-it-from-a-phone).

### `401 unauthorized`

The token is wrong or missing. Confirm what the service is actually using:

```bash
sudo -u vectra /opt/vectra180/venv/bin/vectra180 config --show-secrets | grep token
```

Remember `--token` on the command line is overridden by nothing but is also
visible in `ps`; the config file is the durable place for it.

### `403 cross-origin request refused`

A `POST` or `DELETE` arrived with an `Origin` header that does not match `Host`.
That is the CSRF guard doing its job — without it, any page open in the driver's
browser could submit a form that stops the recording.

If you are scripting with `curl`, send no `Origin` at all; non-browser clients
are accepted precisely because they do not set one. If you are reaching the UI
through a reverse proxy, make sure it rewrites `Host` to match.

### The preview is black, or `503 no frame captured yet`

No frame has arrived yet. Either the camera has not opened, or it is between
reconnect attempts. `/api/status` → `camera.open` tells you which.

### The preview stutters, or stalls when I open the depth map

Depth is expensive — two dewarps and a semi-global block match — and it runs on
an HTTP handler thread, per request. That is by design: it must never cost a
recorded frame. Lower `depth.working_width` if you want it snappier, and do not
poll `/depth.jpg` in a loop.

Several viewers at once each cost a JPEG encode. `server.preview_fps` (default
10) bounds that; lower it if phones are competing.

### The panorama looks warped or seams badly

`depth.focal_scale` is the fisheye focal length as a fraction of frame width, and
it is a guess about your lens until you tune it. The desktop panel's live sliders
are the fastest way to find the right value by eye:

```bash
vectra180 view
```

Then set it in the config. Note this affects the panorama and the depth map only
— **clips are always the raw side-by-side frame**, so getting it wrong costs you
nothing permanent.

---

## Time

### Clip names are in 1970

The Pi has no battery-backed clock and NTP has not answered yet. Clips written
before it does are named from the wrong wall time.

This does **not** affect segment length, incident cooldown or anything else that
measures duration — those all use the monotonic clock, which cannot jump. Only
the names are wrong.

Fixes, in order of preference: give the Pi network time on boot, fit an RTC
module to the CM5 IO board's battery header, or accept it and rely on file order.
See [Time](../deploy/README.md#6-time).

### The burned timestamp and the file name disagree

They are supposed to. The **file name is UTC** so it sorts and is unambiguous;
the **burned overlay is local time** so a person reading the footage recognises
it.

---

## Still stuck

Collect the two things that answer most questions:

```bash
vectra180 doctor --json > doctor.json
sudo journalctl -u vectra180 -n 200 --no-pager > vectra.log
```

Then open an issue — [bug report](https://github.com/Life-Experimentalist/Vectra-180/issues/new?template=bug_report.yml)
or, if it is about a specific camera,
[hardware report](https://github.com/Life-Experimentalist/Vectra-180/issues/new?template=hardware_report.yml),
so the next person knows what to buy.

`doctor --json` contains your recording path and bind address but **not** your
token — `/api/config` and `vectra180 config` redact it unless you pass
`--show-secrets`. Check the journal before pasting it anywhere.

## Related

- [Deployment runbook](../deploy/README.md) — hardware, power, thermals, phone access
- [Configuration](configuration.md) — every setting and its validation rule
- [Recording and retention](recording.md) — segments, sidecars, pruning
- [Telemetry format](telemetry.md) — the IMU block and the orientation filter
- [HTTP API](api.md) — every route and status code
