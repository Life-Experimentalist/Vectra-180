# Security

## Reporting a vulnerability

Please report privately, through
[GitHub's advisory form](https://github.com/Life-Experimentalist/Vectra-180/security/advisories/new).
Do not open a public issue for a security problem.

Include what you can: how to reproduce it, what an attacker gains, and the
version you tested. You should get a first reply within seven days. If a fix is
needed, it ships in a patch release and the advisory is published once users
have had a chance to upgrade.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | Yes       |
| < 1.0   | No        |

## What this software is, in security terms

Vectra-180 is a dashcam. Its threat model is dominated by one fact: **the files
it produces are video of you, your passengers, your route and everyone around
you.** That is the asset worth protecting, and it is far more sensitive than
anything in the process itself.

### The HTTP interface

`vectra180 run` starts a small threaded HTTP server. It serves a live preview,
a clip browser, clip downloads, and a JSON API that can protect and delete
clips.

Defaults, and what they mean:

- **`server.host` is `127.0.0.1`.** Out of the box the interface is reachable
  only from the machine itself.
- **`server.token` is empty.** With no token, every request is accepted. On
  loopback that is reasonable; anywhere else it is not.
- **There is no TLS.** Traffic is plain HTTP.

Two of those combine into the one configuration you must not ship:

> Binding to `0.0.0.0` — or any address other than loopback — **without**
> setting `server.token` publishes every recorded clip, and the delete
> endpoint, to everyone who can reach the port.

The software tells you so twice: `vectra180 run` logs a warning at startup, and
`vectra180 doctor` reports it as a hard failure. Set `server.token` whenever you
widen `server.host`.

Because there is no TLS, a token travels in clear text. Over a phone hotspot or
a home network that is a considered trade-off; across the open internet it is
not. If you need remote access, put it behind a VPN such as WireGuard or
Tailscale, or a reverse proxy that terminates TLS. Do not port-forward it.

### What is already handled

- Tokens are compared with `hmac.compare_digest`, so a wrong guess costs the
  same time as any other wrong guess.
- `Authorization: Bearer <token>` and `?token=` are both accepted. The query
  parameter exists because an `<img>` element cannot send a header; it does mean
  a token can end up in a proxy log, which is a reason to prefer the header
  wherever the client can send one.
- `/healthz` is the one route that answers without a token, so a monitor can
  watch the service without holding the operator's secret. It reports liveness
  and nothing about the footage.
- `POST` and `DELETE` are rejected when the request carries an `Origin` header
  that does not match `Host`. A browser attaches `Origin` to every cross-site
  request and a page cannot forge it, so another site open in the driver's
  browser cannot make their own dashcam delete a clip. A missing header is
  accepted, because non-browser clients — `curl`, the CLI — send none.
- Clip names are validated and then matched against the actual inventory rather
  than joined onto a path, so a crafted name cannot escape the recording
  directory. Static assets are resolved and checked to be inside `static/`.
- Request bodies are capped at 64 KiB and drained before the response. No
  endpoint takes one; an unread body would desynchronise the next request on a
  keep-alive connection.
- The systemd unit in `deploy/` runs as an unprivileged `vectra` user with
  `ProtectSystem=strict`, `NoNewPrivileges`, and a `ReadWritePaths` allowlist
  covering only the recording directory.

### What is not

- No encryption at rest. Anyone holding the SD card, the eMMC or the USB stick
  holds the footage. If that matters for your threat model, use full-disk
  encryption underneath — the software does not do it for you.
- No user accounts or roles. One token, all rights.
- No rate limiting. It is meant for one operator on a private network.
- No signing of clips. The sidecar JSON records what the software believed at
  the time; it is not tamper-evident and should not be treated as evidence of
  authenticity.

### Dependencies

The runtime is OpenCV and NumPy, plus an `ffmpeg` binary if one is present.
CI runs `pip-audit --strict` against the resolved dependency set on every push
to `main` and every pull request, and Dependabot opens weekly update pull
requests for the Python, GitHub Actions and Docker ecosystems. CodeQL analyses
the Python source on the same triggers plus a weekly schedule, so a newly
published advisory is found without waiting for the next commit.

## Privacy, briefly

**No audio is recorded.** The encoder is invoked with `-an`; there is no
microphone path in the codebase at all. In several jurisdictions in-cabin audio
is the part of dashcam recording that is regulated most tightly, so this is
worth knowing.

Video is another matter. Recording other people is regulated differently in
different places — some require signage, some restrict what you may publish,
some limit how long you may keep it. The default `.gitignore` excludes
`recordings/`, `*.mp4` and `*.mkv` so footage does not reach a repository by
accident, but the rest is yours to get right.
