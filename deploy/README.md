# Running Vectra-180 as a car dashcam

This directory is everything needed to turn a Raspberry Pi Compute Module 5 and
a dual-fisheye USB camera into a dashcam that starts on ignition, loops until
the card is full, protects footage when it feels an impact, and serves the
result to a phone.

| File | What it is |
| --- | --- |
| [`install-pi.sh`](install-pi.sh) | Idempotent installer, upgrader and uninstaller |
| [`vectra180.service`](vectra180.service) | The systemd unit, sandboxed |
| [`config.example.toml`](config.example.toml) | Every setting, with the reasoning next to it |

Nothing here is CM5-specific — it installs on any Debian-derived arm64 or amd64
box with systemd. The CM5 is simply the hardware it was tuned against.

---

## 1. Hardware

| Part | Notes |
| --- | --- |
| Compute Module 5 | 4 GB is enough. The 8/16 GB parts buy nothing here. |
| CM5 IO Board | Full-size USB, PCIe, and an RTC battery header |
| Cooling | **Not optional.** See [thermals](#8-thermals) |
| Dual-fisheye USB camera | Must present as UVC and offer MJPG at your capture size |
| Storage for footage | A USB SSD, or NVMe in the IO board's M.2 slot |
| RTC coin cell | So clip timestamps survive a night parked up |
| 12 V → 5 V supply | 5 A capable, and tolerant of cranking. See [power](#7-power-and-ignition) |

### Why a separate disk for footage

A dashcam writes continuously, forever. An SD card is built for a duty cycle
nothing like that and will eventually fail — usually silently, usually
producing unreadable clips before it produces errors. Boot from the eMMC or the
card, record to something else.

If footage does not live under `/var/lib/vectra180`, the unit needs to be told,
because it is sandboxed. See [storage](#5-storage).

---

## 2. Operating system

Raspberry Pi OS **Bookworm, 64-bit**. The Lite image is the right one — there
is no desktop in a car, and the web interface is the interface.

Flash it with Raspberry Pi Imager. Use the settings dialog before writing to
set the hostname, enable SSH, and set the **time zone** — clip filenames and
sidecars are written in UTC, but the timestamp burned into the picture is local
time, and that is the one you will be reading back after an incident.

For an eMMC module, put the IO board in USB boot mode (the `nRPIBOOT` jumper)
and use `rpiboot` to expose the eMMC as a disk. For a Lite module, an SD card
in the IO board slot works with no jumper.

First boot:

```bash
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

---

## 3. Find the camera

Plug it in and confirm the kernel sees a UVC device:

```bash
lsusb && v4l2-ctl --list-devices
```

Then ask that device what it can actually do. This step is worth the minute it
takes — capture geometry is the setting most installs get wrong:

```bash
v4l2-ctl -d /dev/video0 --list-formats-ext
```

Look for an **MJPG** entry at the side-by-side resolution you want, with the
frame rate you want beside it. A dual-fisheye module usually reports one wide
frame holding both eyes: `2560x720` is `1280x720` per eye.

YUYV will be listed too, and at these sizes it will be capped around 5 fps —
uncompressed 2560×720 at 30 fps is about 880 Mbit/s, well past the 480 Mbit/s
USB 2.0 ceiling that most UVC modules present. MJPG is what makes the mode
possible, which is why it is the default in the config.

Whatever the listing says, put *those* numbers in the config. Asking for a mode
the camera does not have gets you whatever it decides to substitute.

---

## 4. Install

```bash
git clone https://github.com/Life-Experimentalist/Vectra-180.git
cd Vectra-180
sudo ./deploy/install-pi.sh
```

The script installs the apt dependencies, creates a `vectra` system account in
the `video` group, builds a virtualenv at `/opt/vectra180/venv`, writes
`/etc/vectra180/config.toml`, installs the unit, and starts it.

It is safe to re-run — that is also how you upgrade. An existing config is
never overwritten; only `/etc/vectra180/config.example.toml` is refreshed, so
you can diff yours against the current reference after an upgrade.

```bash
sudo ./deploy/install-pi.sh --uninstall   # remove the service, keep footage
sudo ./deploy/install-pi.sh --purge       # remove everything, footage included
```

### Configure

```bash
sudoedit /etc/vectra180/config.toml
sudo systemctl restart vectra180
```

Every key is documented in place. The ones that matter on day one:

```toml
[camera]
# /dev/video0 is assigned in enumeration order and moves when a second UVC
# device appears. The by-id link does not. `ls -l /dev/v4l/by-id/`
device = "/dev/v4l/by-id/usb-..._Dual_Fisheye-video-index0"
width = 2560       # whatever --list-formats-ext told you
height = 720
fps = 30

[recording]
directory = "/var/lib/vectra180/recordings"
segment_seconds = 60
max_bytes = 34359738368    # 32 GiB of loop footage

[incident]
threshold_g = 0.6          # lower catches more; see below

[server]
host = "127.0.0.1"         # see access from a phone, below
```

---

## 5. Storage

To record to a USB SSD, mount it and point the config at it — then tell the
sandbox about it, or the service will not be able to write there.

```bash
sudo blkid                       # find the UUID
sudo mkdir -p /media/footage
```

Add to `/etc/fstab`:

```
UUID=xxxx-xxxx  /media/footage  ext4  defaults,noatime,nofail,x-systemd.device-timeout=10  0  2
```

`nofail` matters: without it a disk that has been unplugged stops the boot
entirely, and a car is not a place to discover that.

```bash
sudo mount -a
sudo chown vectra:vectra /media/footage
```

Set `recording.directory = "/media/footage"` in the config, then grant the unit
write access to it:

```bash
sudo systemctl edit vectra180
```

```ini
[Service]
ReadWritePaths=/media/footage
```

```bash
sudo systemctl restart vectra180
```

The unit runs with `ProtectSystem=strict`, which makes the whole filesystem
read-only apart from the paths explicitly listed. Skipping this step produces a
permission error in the journal rather than silence, but it is still the most
common way a first install fails to record.

### How much footage fits

Two independent budgets, so an incident can never be deleted to make room for
ordinary driving:

| Setting | Governs |
| --- | --- |
| `recording.max_bytes` | The loop. Oldest ordinary clips are deleted past this. |
| `recording.min_free_bytes` | A floor of free space, whatever `max_bytes` says |
| `recording.max_event_bytes` | Protected clips. Reclaimed only against each other. |

At the default 8000 kbit/s, footage costs about **3.6 GB per hour**. A 32 GiB
loop is therefore around nine and a half hours of driving, and the 8 GiB event
budget holds a little under two and a half hours of protected clips.

---

## 6. Time

Clip filenames are wall-clock times, so a machine that boots believing it is
1970 fills the card with clips dated 1970 — and a dashcam whose timestamps are
wrong is worth much less than one whose timestamps are right.

There is often no network in a car, so fit the coin cell to the RTC battery
header on the IO board. The CM5 keeps time across a power cut with it, and the
service waits for `time-set.target` before starting.

Without a battery, `fake-hwclock` (installed by default on Raspberry Pi OS)
restores the time of the last clean shutdown — which is approximately right and
much better than the epoch.

Check it after a night parked up:

```bash
timedatectl
```

---

## 7. Power and ignition

The simple arrangement is a 12 V → 5 V converter on an ignition-switched
circuit: turn the key, the Pi boots and records; turn it off, the Pi loses
power.

Two consequences worth planning for.

**Cranking.** Starter motors drag the battery down hard enough to reset a
converter without hold-up capacitance. If the Pi reboots every time the engine
starts, that is what happened. A supply rated for automotive transients fixes
it; so does powering from a circuit that stays up during cranking.

**Cutting power mid-write.** A hard cut costs the segment being written and
nothing else. That is bounded by `segment_seconds`, so 60 risks a minute and 15
risks fifteen seconds, at the cost of four times as many files. Everything
already closed is intact.

If losing that last minute is unacceptable, a small UPS HAT and a shutdown
script are the proper fix. `systemctl stop vectra180` sends `SIGTERM`, which
finalises the open segment before exiting:

```bash
sudo systemctl stop vectra180 && sudo poweroff
```

**Parked recording** is out of scope here: this records while it has power. It
does not sleep, wake on motion, or manage a battery.

---

## 8. Thermals

The CM5 has **no hardware H.264 encoder**. Every recorded frame is compressed
by libx264 on the Cortex-A76 cores, and a car in the sun is the worst thermal
environment a Pi is likely to meet.

So: a heatsink at minimum, and the official active cooler or equivalent if the
car parks outdoors. Without it the SoC throttles, throughput drops below the
capture rate, and frames are dropped — visible as a rising `dropped_frames` in
`/api/status`.

```bash
vcgencmd measure_temp     # comfortable below 70°C
vcgencmd get_throttled    # 0x0 is what you want
```

`recording.preset` stays at `ultrafast` for this reason. `superfast` is worth
trying if you have thermal headroom and want smaller files; anything slower
will not hold 30 fps at 2560×720. `vectra180 doctor` measures the encoder on
your own frames and tells you the number rather than guessing.

---

## 9. Verify

```bash
sudo -u vectra /opt/vectra180/venv/bin/vectra180 doctor
```

This opens the real camera, reads real frames, decodes the IMU strip, times the
encoder at the size your camera actually produced, and writes to the recording
directory. It is the fastest way to find out whether the install will work
before trusting it with a drive.

```
[ ok ] environment: vectra180 1.0.0 on Linux aarch64, python 3.11.2, ...
[ ok ] ffmpeg: /usr/bin/ffmpeg
[ ok ] storage: /media/footage: 412.7 GB free, 0 loop clip(s), 0 locked clip(s)
[ ok ] service: http://0.0.0.0:8080 (network, token required)
[ ok ] devices: [0] Camera 0 (/dev/video0)
[ ok ] camera: 2560x720 via v4l2, 29.8 fps measured (30 requested)
[ ok ] telemetry: IMU present: 1.00 g total, gyro +0.00/-0.01/+0.00 rad/s
[ ok ] encoder: FFmpegWriter at 2560x720 preset 'ultrafast': 44.2 fps (30 needed)

All checks passed.
```

Then watch it run:

```bash
journalctl -u vectra180 -f
```

Take a short test drive and confirm clips are appearing:

```bash
ls -lh /media/footage/normal/
```

---

## 10. Reaching it from a phone

By default the service binds `127.0.0.1` and is reachable only from the Pi
itself. Opening it up is deliberate, and takes two settings, not one:

```toml
[server]
host = "0.0.0.0"
token = "paste-a-long-random-string-here"
```

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Recorded footage is sensitive — it is a record of where you have been. On
`0.0.0.0` every device on the network can list, stream and download it, so
`vectra180 doctor` reports a public bind with no token as a **failure**, not a
warning.

Join the Pi to the car's hotspot, or have it run its own:

```bash
sudo nmcli device wifi hotspot ssid vectra password "choose-something-long"
```

Then browse to `http://<pi-address>:8080/?token=<your-token>` from the phone.

| Endpoint | What it does |
| --- | --- |
| `/` | The interface: live view, clip browser, storage, lock button |
| `/stream.mjpg` | Live MJPEG stream |
| `/snapshot.jpg` | Single frame (`?overlay=0` for no HUD) |
| `/depth.jpg` | Stereo depth map, computed on request only |
| `/api/status` | Everything the pipeline knows, as JSON |
| `/api/clips` | The clip listing |
| `/healthz` | Liveness, and the one route that never needs the token |

---

## 11. Incidents

The g-sensor watches total acceleration rather than any single axis, so it does
not care how the module is angled in the car. When the deviation from 1 g
exceeds `incident.threshold_g`, the open clip is protected and — with
`lock_previous_segment` on, which it is by default — so is the one that just
closed. The run-up to a collision is usually the part that matters, and by the
time the sensor fires that footage is in the previous file.

Protected clips move to `events/` and are never touched by ordinary pruning.

Tuning, from a stationary start:

| `threshold_g` | Behaviour |
| --- | --- |
| 0.35 – 0.45 | Sensitive. Also fires on hard braking and speed bumps. |
| **0.6** | Default. Real impacts and heavy potholes. |
| 0.8 – 1.0 | Collisions only. |

Start at the default, drive a week, look at what landed in `events/`, and
adjust. Firm, hard-mounted installations read higher than ones on a
suction-cup, so there is no universal number.

You can always protect the current clip by hand — the lock button in the web
interface, or:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://<pi>:8080/api/lock
```

---

## 12. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Service restarts in a loop | `journalctl -u vectra180 -n 50`. Usually the camera path or a config typo. |
| `doctor` says no device responded | Camera unplugged, or the account is not in `video`. `id vectra` |
| Capture is ~5 fps | The camera negotiated YUYV. Confirm MJPG at that size with `v4l2-ctl --list-formats-ext`. |
| Resolution is not what was asked for | The mode does not exist. `doctor` reports what the driver gave instead. |
| `dropped_frames` climbing | Encoder or disk cannot keep up. Check `vcgencmd get_throttled`, then cooling, then `bitrate_kbps`. |
| Read-only file system in the journal | `recording.directory` is outside `ReadWritePaths`. See [storage](#5-storage). |
| Clips dated 1970 | No RTC battery and no network at boot. See [time](#6-time). |
| Nothing at `http://<pi>:8080` | `server.host` is still `127.0.0.1`, or the token is missing from the URL. |
| Web interface unreachable, recording fine | Correct by design — recording never depends on the service being reachable. |

The status endpoint is the fastest diagnostic once it is running:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://<pi>:8080/api/status | python3 -m json.tool
```

---

## 13. Optional: read-only root

Continuous power cuts are hard on a filesystem. Raspberry Pi OS can run its
root filesystem read-only with an overlay in RAM:

```bash
sudo raspi-config     # Performance Options -> Overlay File System
```

Do this **only** with footage on a separate writable disk, and remember that
the root filesystem then discards changes at every reboot — including config
edits, which must be made with the overlay temporarily disabled.

It is a real hardening step, and it is also the thing most likely to make a
later change confusing. Get the install working first; harden it after.
