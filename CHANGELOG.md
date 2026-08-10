# Changelog

Notable changes to Vectra-180. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — unreleased

The first public release, being road-tested before it is tagged. Everything
below is the shape of that release rather than a history of how it got there.

### Recording

- Continuous segmented recording from a dual-fisheye USB (UVC) camera, writing
  fixed-length clips to a loop directory.
- Two independent retention budgets — a loop budget that is pruned oldest-first,
  and a separate event budget, so protected clips are never deleted to make room
  for loop footage and are reclaimed only against each other.
- An incident detector driven by the camera's IMU. When acceleration crosses the
  configured threshold, the clip that contains the moment is moved out of the
  loop directory and into `events/`.
- A JSON sidecar beside every clip carrying its UTC start, duration, frame
  count, frame rate and dimensions, whether it is locked and why, and the
  telemetry samples collected while it was being written.
- FFmpeg is used for encoding when it is on `PATH`, with an OpenCV `VideoWriter`
  fallback when it is not. A camera that disappears mid-drive is reconnected
  without losing the recording.
- `recording.scale` shrinks each frame before encoding, for modules that offer a
  single mode larger than the encoder can carry. Preview, depth and the HUD keep
  working from the full frame.

### Telemetry

- Decoder for the ICM42688 block that the camera embeds in the leftmost pixel
  column of each frame: a microsecond timestamp plus six axes of accelerometer
  and gyroscope data.
- A complementary orientation filter that leans on gyroscope integration for
  responsiveness and on measured gravity for drift correction, and ignores
  gravity when the accelerometer says the vehicle is manoeuvring.
- `vectra180 decode` reads one frame and prints what it found, so a camera can
  be checked before it is trusted.

### Imaging

- A panoramic view: both eyes are dewarped through a cached remap, joined by a
  fixed-geometry alpha blend, and counter-rotated by the filtered roll angle so
  the horizon stays level. The lenses are rigid on one PCB, so there is no
  feature-matching stitch to pay for.
- The panorama is a viewing transform, requested per frame by whoever is
  watching. Clips are always the raw side-by-side frame, which is the closest
  thing to what the sensors saw.
- Stereo depth on demand rather than on the recording path — `/depth.jpg`
  computes a disparity map when something asks for one, so the encoder keeps its
  CPU the rest of the time.
- A heads-up display overlay carrying the raw accelerometer and gyroscope axes,
  the filtered roll, pitch and yaw, an artificial horizon, the frame rate, and
  the recorder's state — current clip, segment position, free space, incident
  lock and dropped frames.

### Interfaces

- A headless HTTP service: live MJPEG preview at `/stream.mjpg`, stills at
  `/snapshot.jpg` and `/depth.jpg`, a clip browser and downloads, and a JSON API
  covering status, configuration, storage, clip listing, clip protection and
  deletion, manual incident locking, and starting or stopping the recording.
- `?view=pano` on the preview and snapshot endpoints switches to the joined,
  levelled panorama; the web UI carries a button for it.
- Bearer-token authentication, off by default on loopback and required by
  `vectra180 doctor` whenever the service is bound to a public address.
- `/healthz` answers without a token so a monitor can watch the service without
  holding the operator's secret.
- An optional DearPyGui desktop panel for desk testing, installed only with the
  `desktop` extra: five view modes, live matcher sliders, and keyboard control.

### Command line

- `run`, `view`, `devices`, `doctor`, `decode` and `config`, installed as both
  `vectra180` and the shorter alias `vectra`.
- `vectra180 doctor` runs eight checks — environment, ffmpeg, storage, service,
  devices, camera, telemetry and encoder — against the real capture and encode
  path, and prints a remedy under anything that is not `ok`.
- `vectra180 devices` probes every capture driver the platform offers and labels
  each result with the backend that saw it, because an index names different
  hardware on different drivers. A device that opens but streams nothing — what
  a camera held by another program looks like — is listed as such rather than
  omitted.
- `--backend` pins the capture driver for one invocation, alongside the existing
  `--camera` and `--device`.
- Configuration resolves through defaults, then a TOML file, then `VECTRA_*`
  environment variables, then command-line flags. `vectra180 config` prints the
  result of that merge.
- `SIGINT`, `SIGTERM` and, on Windows, `SIGBREAK` finalise the open segment
  before exiting. A second interrupt during that pause is ignored, so the reflex
  to press Ctrl-C again cannot truncate the clip.

### Deployment

- `deploy/install-pi.sh` installs, upgrades and uninstalls Vectra-180 as a
  systemd service on Raspberry Pi OS, running as an unprivileged user under
  `ProtectSystem=strict`.
- A hardened `vectra180.service` unit, a commented `config.example.toml`, and a
  runbook in `deploy/README.md` covering camera selection, storage, power,
  thermals and phone access.
- A container image, published for `linux/amd64` and `linux/arm64`.

[1.0.0]: https://github.com/Life-Experimentalist/Vectra-180/releases/tag/v1.0.0
