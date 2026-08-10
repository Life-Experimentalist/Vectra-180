# Troubleshooting

Symptoms, causes, fixes. Start here:

```bash
vectra180 doctor
```

Every check exercises the real path — the camera is opened, frames are read, the
encoder is timed at the resolution the camera actually produced, the recording
volume is written to, and the whole chain is then run together for five seconds
to see what it sustains. Most of what follows is a longer explanation of
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

**Check for a `vectra180 run` you forgot about first.** A recorder left running
in another terminal holds the camera, and every other command then sees exactly
this. `vectra180 devices` lists such a device with `opened but streamed nothing`
rather than hiding it.

```bash
sudo fuser -v /dev/video0       # Linux: names the process holding it
```

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'vectra|python' }
```

If nothing is holding it, give it a dedicated USB 3 port. On a CM5 IO board, the
two USB-A ports share a controller with anything on the internal header.

### `doctor` says the driver gave a different mode

```
[warn] camera: 1280x720 via v4l2, 30.0 fps measured (30 requested); driver gave
       1280x720, not the requested 2560x720
```

The requested mode is unsupported at that pixel format, so the driver quietly
negotiated down — **provided this is the camera you meant**. A built-in webcam
answering in place of the fisheye produces the same warning, so if two cameras
are attached, settle identity first:
[The wrong camera is recording](#the-wrong-camera-is-recording).

List what the device actually offers:

```bash
v4l2-ctl --device /dev/video0 --list-formats-ext
```

Then either set `camera.width`/`camera.height` to a mode that appears in that
list, or fix `camera.fourcc`. The warning itself quotes the numbers the driver
chose, so the quickest fix is to paste those two lines into the config.

If the mode the driver picked is fine and you simply do not want to pin it, set
both to `0` and the size is left to the driver:

```toml
[camera]
width = 0
height = 0
```

**Otherwise this is nearly always a `fourcc` problem** — YUYV cannot sustain a
full-size stereo frame at 30 fps over USB 2.0, so a camera asked for YUYV at
that size will negotiate down to something it can do. `MJPG` is the default for
exactly this reason.

If the camera is already on `MJPG` and still slow, check what it actually
negotiated. `doctor` quotes the driver's own answer next to the request:

```
[warn] camera: 4000x1200 via msmf, 11.3 fps measured (30 requested)
       -> the USB link or the pixel format is the bottleneck: camera.fourcc
          asked for MJPG and the driver settled on YUY2
```

A driver that substitutes like this is not going to be talked out of it, and on
some modules the request is itself what pins the slow mode. Stop asking:

```toml
[camera]
fourcc = ""
```

That leaves the format to the driver, the same way `width = 0` leaves the size
to it.

### `this OpenCV build has no gstreamer support`

`camera.backend` names a driver the installed OpenCV was not compiled with. The
PyPI wheels (`opencv-contrib-python`) ship **without** GStreamer, which is the
backend a MIPI CSI camera needs — see [CSI cameras](#csi-cameras-camdisp-connectors)
below.

Set `camera.backend = "auto"`, or install an OpenCV built with the backend you
need. On Raspberry Pi OS the distribution package is built with GStreamer:

```bash
sudo apt install python3-opencv
```

Check what a build actually carries:

```bash
python -c "import cv2; print(cv2.getBuildInformation())" | grep -i gstreamer
```

### CSI cameras (CAM/DISP connectors)

The capture path is **USB/UVC**. It opens a device with OpenCV's V4L2 backend
and negotiates a UVC mode, which is what a USB dual-fisheye module presents.

A camera on the CM5's CAM/DISP connectors is MIPI CSI-2 and does not present a
UVC device. It is driven by `libcamera`, and reaching it from OpenCV means a
GStreamer pipeline rather than a device index:

```toml
[camera]
backend = "gstreamer"
device = "libcamerasrc ! video/x-raw,width=2560,height=720 ! videoconvert ! appsink"
width = 0
height = 0
```

This requires an OpenCV built with GStreamer (see above); it is not exercised by
the test suite and has not been verified on hardware. **USB/UVC is the supported
path.**

### The service will not start

```bash
sudo systemctl status vectra180
sudo journalctl -u vectra180 -n 50
```

| Message | Cause |
|---|---|
| `could not bind 0.0.0.0:8080: Address already in use` | Something else has the port, or a previous instance is still shutting down |
| `config file not found` | `--config` or `VECTRA_CONFIG` points at a file that does not exist |
| `camera.fourcc must be exactly four characters ... or empty` | A config value is invalid. The message names the key |
| `no such file or directory: /var/lib/vectra180/recordings` | The recording directory is missing or not owned by the service user |

Config errors are reported as one line and exit `1`. They are user mistakes, not
crashes, so there is no traceback to read — the line names the file and the key.

---

## The wrong camera is recording

The symptom is unmistakable on a laptop: the built-in webcam's LED lights up
while the fisheye sits idle. It is worth understanding because the same
mechanism can pick the wrong camera on a Pi with two devices attached.

**An index does not name a camera.** `camera.index = 0` means "whatever the
driver that answers calls device zero", and each driver enumerates in its own
order. On Windows, MSMF and DirectShow routinely number the same pair of cameras
in *opposite* order.

`camera.backend = "auto"` tries the platform's drivers in turn. When the first
one cannot open the camera — most often because another program is holding it —
it falls through to the next, and that next driver's device `0` can be entirely
different hardware. Recording then starts on the wrong camera, with no error,
because from the code's point of view nothing failed.

That fallback is logged, so it is visible rather than silent:

```
WARNING camera gave 1280x720, not the requested 2560x720
WARNING msmf could not be used, so camera 0 was opened via dshow instead --
        an index means a different device on a different backend
```

### Sorting it out

**1. See everything, per driver.** Every usable backend is probed, so a camera
appears once per driver that can see it:

```bash
vectra180 devices
```

```
msmf[0] Camera 0 (index 0)  4000x1200 @ 30fps
msmf[1] Camera 1 (index 1)  640x480 @ 30fps   (opened but streamed nothing -- another program may hold it)
dshow[0] Camera 0 (index 0)  640x480 @ -1fps
dshow[1] Camera 1 (index 1)  4000x1200 @ -1fps
```

The resolution identifies the hardware: **a dual-fisheye module is the wide
one**. Above, the fisheye is `msmf[0]` *and* `dshow[1]` — the same camera, two
numbers.

**2. Close whatever is holding it.** An entry marked `opened but streamed
nothing` is being held by another program — including a `vectra180 run` in
another terminal. Nothing below will work reliably until it is closed.

**3. Pin the pair.** Both halves matter; a backend without an index is as
ambiguous as an index without a backend.

```toml
[camera]
backend = "msmf"
index = 0
```

Or for one run:

```bash
vectra180 doctor --backend msmf --camera 0
```

**4. On Linux, prefer a path and skip all of this.** `camera.device` addresses
the hardware directly, so no enumeration order can misdirect it:

```toml
[camera]
device = "/dev/v4l/by-path/platform-xhci-hcd.1-usb-0:1:1.0-video-index0"
```

```bash
ls -l /dev/v4l/by-path/    # the USB socket the camera is in
ls -l /dev/v4l/by-id/      # the make, model and serial it reports
```

`/dev/videoN` numbering is assigned at enumeration and can move between boots.
Neither of the stable paths does — but they mean different things, and which one
you want depends on how many cameras you have:

- **`by-path` names the socket.** "The camera plugged into this port", whatever
  it is. This is the one to use on a fixed install, where the module is cabled to
  one connector and stays there.
- **`by-id` names the device.** Better if you move the camera between ports —
  but it only distinguishes two modules of the same make if they report distinct
  serial numbers, and inexpensive UVC modules frequently do not. Two identical
  cameras that both report no serial produce colliding names, and which one wins
  is again down to enumeration order. Run the `ls` above and look before relying
  on it.

### Do not "fix" a mode mismatch before checking identity

`doctor` warns when the driver returns a different size than requested, and
suggests matching the config to it. **Confirm it is the right camera first.** A
built-in webcam answering in place of the fisheye looks exactly like a mode
substitution, and taking the advice pins the mistake into the config
permanently.

---

## Dropped frames

```json
{ "recorder": { "dropped_frames": 4127, "written_frames": 71483 } }
```

Visible in `/api/status`, on the HUD, and in the summary `run` prints on exit.

**Dropped frames mean the encoder cannot keep up.** The recorder's queue holds
about two seconds of footage, and no more than 256 MB of it; when either bound is
reached, `submit()` drops the frame rather than blocking the camera or growing
without limit. Nothing is wrong with the camera — the frames arrived and there
was nowhere to put them.

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

## Clips play back faster than the drive happened

```
[warn] pipeline: 19.4 fps captured, prepared and encoded together (30 requested)
         -> the whole pipeline is slower than the camera alone, so clips play faster
            than real time and their sidecars are marked discontinuous. Lower
            recording.scale to encode fewer pixels, or set camera.fps to about 19 so
            the header matches what is recorded
```

A clip's header declares `camera.fps`. If the machine only sustains 19 of those
30 frames, the file still says 30, so a minute of road plays back in thirty-eight
seconds — and the sidecar's `continuous` flag is `false` because `covers_seconds`
does not match `duration_seconds`.

This can happen while the `camera` and `encoder` checks are both green. Those
time one stage each with nothing else running; the `pipeline` check is the one
that runs them together, and contention between them is real.

Two different fixes, and they do different things:

| Change | Effect |
|---|---|
| `recording.scale` down | Encodes fewer pixels, so the rate actually comes back up |
| `camera.fps` down to the measured rate | Rate is unchanged, but the header now matches, so playback speed is correct |

Do both: lower the scale until `pipeline` reports close to the rate you want, then
leave `camera.fps` at that rate.

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
| The clips are in `events/` | Locked clips are exempt from the loop pass by design; they are reclaimed only once they exceed `max_event_bytes` between them. Delete them explicitly, or lower that budget |
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
| The last clip of a drive has no duration in the browser | Power was cut mid-segment, so the sidecar was never written. On the ffmpeg path the video itself still plays, up to about two seconds before the cut |
| The last clip of a drive will not open at all | Power was cut mid-segment *and* the OpenCV fallback writer was in use. That path cannot produce a recoverable partial file. Install ffmpeg |
| Every clip is broken | The OpenCV fallback writer is in use with a codec your player does not know. Install ffmpeg |

Check which encoder is running:

```bash
curl -s .../api/status | jq '.recorder.encoder'
```

`OpenCVWriter` when you expected `FFmpegWriter` means ffmpeg is not on `PATH`:

```bash
sudo apt install ffmpeg
```

On a workstation used for bench testing, `brew install ffmpeg` on macOS and
`winget install Gyan.FFmpeg` on Windows do the same job. The Windows installer
extends `PATH` for future processes only, so open a new terminal before
re-running `vectra180 doctor` — an existing shell keeps the `PATH` it started
with and will still report ffmpeg missing.

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
