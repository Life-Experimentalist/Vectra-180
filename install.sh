#!/usr/bin/env bash
#
# Set up a Vectra-180 development checkout on Linux or macOS.
#
# This is for working on the project. To *run* it on a Raspberry Pi, use
# deploy/install-pi.sh instead -- that one installs a service, not a toolchain.

set -euo pipefail

BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
RED=$'\033[0;31m'
OFF=$'\033[0m'

step() { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$1"; }
warn() { printf '%s !! %s%s\n' "$YELLOW" "$1" "$OFF" >&2; }
die() {
	printf '%s !! %s%s\n' "$RED" "$1" "$OFF" >&2
	exit 1
}

cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || die "cannot enter the project directory"

# -- uv ----------------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
	step "Installing uv"
	curl -LsSf https://astral.sh/uv/install.sh | sh
	# The installer puts uv in ~/.local/bin and edits your shell profile, but
	# this shell has already read its profile, so add it by hand for now.
	export PATH="$HOME/.local/bin:$PATH"
fi

command -v uv >/dev/null 2>&1 || die "uv is still not on PATH -- open a new shell and re-run this"

# -- the project -------------------------------------------------------------

step "Installing dependencies"
uv sync --all-extras

step "Installing the pre-commit hooks"
uv run pre-commit install

# -- optional system tools ---------------------------------------------------

if ! command -v ffmpeg >/dev/null 2>&1; then
	case "$(uname -s)" in
	Linux) warn "ffmpeg is not installed. Recording falls back to the OpenCV writer: sudo apt install ffmpeg" ;;
	Darwin) warn "ffmpeg is not installed. Recording falls back to the OpenCV writer: brew install ffmpeg" ;;
	*) warn "ffmpeg is not installed. Recording falls back to the OpenCV writer." ;;
	esac
fi

# -- prove it works ----------------------------------------------------------

step "Running the checks"
uv run ruff check src tests
uv run mypy src tests
uv run pytest -q -m "not integration"

printf '\n%sReady.%s\n\n' "$GREEN" "$OFF"
printf '  make help                 every development task\n'
printf '  uv run vectra180 doctor   check this machine can record\n'
printf '  uv run vectra180 run      record and serve until stopped\n'
