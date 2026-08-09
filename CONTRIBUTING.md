# Contributing

Thank you for looking. This is a small project with a narrow purpose, and the
fastest route to a merged change is knowing what that purpose is.

## Read this first

**Recording is the duty; everything else is garnish.** A missing IMU, an
unreachable web interface, a camera that vanished mid-drive — none of these may
stop clips being written. The target hardware is a Raspberry Pi Compute Module 5
with no hardware H.264 encoder, so every recorded frame is compressed by libx264
on four Cortex-A76 cores. Anything that competes with that budget has to earn
its place, and anything that could stop a recording will be turned down however
elegant it is.

**The frame carries the clock.** Every time the pipeline needs to know when it
is, it reads `Frame.monotonic` or `Frame.wall_time` rather than calling the
clock itself. Segment rollover, clip naming, telemetry offsets and retention all
follow from those two fields. This is what lets the integration suite replay
minutes of footage in milliseconds through a fake source. Calling
`time.monotonic()` inside the pipeline breaks that, and the tests will say so.

[AGENTS.md](AGENTS.md) has the rest of the orientation, including the directory
map and the telemetry wire format.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and, to exercise the recording path
for real, `ffmpeg` on `PATH`.

```bash
make install
```

That is `uv sync --all-extras` — the virtualenv, the runtime dependencies, the
dev tools, and the optional DearPyGui desktop panel. Then:

```bash
make hooks
```

`install.sh` (or `install.ps1` on Windows) does the same thing plus a first
verification run, if you prefer one command. Neither of them is how you install
this on a Pi — that is `deploy/install-pi.sh`, documented in
[deploy/README.md](deploy/README.md).

## The gate

```bash
make gate
```

Ruff format, ruff check, mypy over `src` and `tests`, then the full pytest
suite. CI runs exactly that, in that order. If it is green on your machine it is
green there.

Two shortcuts while you work:

```bash
make test-fast
```

skips the integration tests, which drive real files and real localhost sockets
and take most of the wall clock. Run the full suite before you open the pull
request.

```bash
make format
```

rewrites source to the project style rather than complaining about it.

### Markers

- `integration` — several subsystems together, against real files and sockets.
  Run by default; skip with `-m "not integration"`.
- `hardware` — needs a camera physically attached. **Deselected in CI.** If you
  add one, it must be skipped cleanly on a machine with no camera.

## What good looks like here

- **Type annotations everywhere.** `mypy src tests` is clean and stays clean —
  the suite is type-checked too, not just the package.
- **Tests describe behaviour, not implementation.** The docstring says why the
  test exists. If it only restates the assertion, it is not earning its place.
  A change in behaviour should come with a test that fails without it.
- **Comments explain why.** The code already says what. A comment that
  paraphrases the line below it is noise; a comment that records why the
  obvious approach was rejected is worth more than the code.
- **Surgical diffs.** Change what the issue requires. Reformatting an adjacent
  function, renaming something on the way past, or "improving" a comment you
  did not otherwise touch all make a change harder to review and harder to
  revert.
- **Documentation states what the code does today.** No changelog-in-prose, no
  "this used to be broken", no features that are planned but absent. If a
  document and the code disagree, that is a bug in one of them.
- Line length is 120. Ruff's rule set is in `pyproject.toml`; it includes
  `RUF005`, so build lists with unpacking rather than `+`.

## Changes that need a conversation first

Open an issue before writing the code if you are planning to:

- add a runtime dependency (there are currently two: OpenCV and NumPy)
- change the capture, encode or retention path
- change the on-disk layout, the sidecar JSON, or the HTTP API
- add anything that runs per frame

None of these are forbidden. They are just expensive to get wrong, and it is
kinder to find out in an issue than in a review.

## Hardware reports are contributions

Dual-fisheye USB cameras vary wildly, and the only way anyone can buy one with
confidence is if people say what worked. There is a
[hardware report template](.github/ISSUE_TEMPLATE/hardware_report.yml) — a
report that something *works* is as valuable as a report that it does not.

## Commits and pull requests

Write commit subjects in the imperative mood — "add the retention budget",
not "added" or "adds". Keep the subject under about 70 characters, and use the
body for why. No trailer or sign-off is required.

Fill in the pull request template. If the change touches the recording path,
say what you ran it against — a real camera, `ReplaySource`, or the fake source —
and for how long.

## Releasing

Maintainers only. Bump `__version__` in `src/vectra180/__init__.py`, update
`CHANGELOG.md`, then tag `vX.Y.Z`. The release workflow refuses to publish if
the tag and `__version__` disagree; it then builds the distributions, cuts a
GitHub release, and pushes a multi-architecture container image including
`linux/arm64` for the Pi.

## Licence

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE.md), the same terms as the project.
