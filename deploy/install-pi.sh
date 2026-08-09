#!/usr/bin/env bash
#
# Install Vectra-180 as a system service on Raspberry Pi OS.
#
# Written for a Compute Module 5 on the CM5 IO board with a dual-fisheye USB
# camera, but nothing here is CM5-specific: it works on any Debian-derived
# arm64 or amd64 system with systemd.
#
# Safe to re-run. An existing config is never overwritten and an existing
# install is upgraded in place.

set -euo pipefail

PREFIX=/opt/vectra180
VENV="$PREFIX/venv"
SRC="$PREFIX/src"
CONFIG_DIR=/etc/vectra180
CONFIG="$CONFIG_DIR/config.toml"
STATE_DIR=/var/lib/vectra180
RECORDINGS="$STATE_DIR/recordings"
UNIT=/etc/systemd/system/vectra180.service
SERVICE_USER=vectra
REPO_URL=https://github.com/Life-Experimentalist/Vectra-180.git

# Kept deliberately short. Everything Python needs comes from wheels.
APT_PACKAGES=(
	python3       # Bookworm ships 3.11, which is this project's floor
	python3-venv
	python3-pip
	ffmpeg        # smaller files than the OpenCV writer, and a reliable moov atom
	libgl1        # OpenCV links libGL even in a headless build
	libglib2.0-0  # and libglib, for its own reasons
	v4l-utils     # v4l2-ctl, for finding out which modes your camera really has
)

BOLD=$'\033[1m'
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
DIM=$'\033[2m'
OFF=$'\033[0m'

step() { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$1"; }
info() { printf '    %s%s%s\n' "$DIM" "$1" "$OFF"; }
warn() { printf '%s !! %s%s\n' "$YELLOW" "$1" "$OFF" >&2; }
die() {
	printf '%s !! %s%s\n' "$RED" "$1" "$OFF" >&2
	exit 1
}

usage() {
	cat <<-'EOF'
		Install Vectra-180 as a system service.

		  sudo ./deploy/install-pi.sh              install or upgrade
		  sudo ./deploy/install-pi.sh --uninstall  remove the service, keep footage
		  sudo ./deploy/install-pi.sh --purge      remove everything, footage included
		  ./deploy/install-pi.sh --help            this message

		Installs into /opt/vectra180, reads /etc/vectra180/config.toml and records
		into /var/lib/vectra180/recordings.
	EOF
}

# -- preflight ---------------------------------------------------------------

require_root() {
	[ "$(id -u)" -eq 0 ] || die "this needs root: sudo $0 ${*:-}"
}

check_platform() {
	command -v systemctl >/dev/null 2>&1 || die "no systemd here, and this installs a systemd unit"
	command -v apt-get >/dev/null 2>&1 || die "no apt-get here; this script targets Debian and Raspberry Pi OS"

	local arch
	arch="$(uname -m)"
	if [ "$arch" != "aarch64" ] && [ "$arch" != "x86_64" ]; then
		# armv7 has no opencv-contrib-python wheel on PyPI, so pip would fall
		# back to building OpenCV from source and spend hours failing at it.
		warn "architecture $arch has no prebuilt OpenCV wheel; expect pip to fail"
	fi

	if [ -r /proc/device-tree/model ]; then
		info "hardware: $(tr -d '\0' </proc/device-tree/model)"
	fi
}

# -- install -----------------------------------------------------------------

# Where the package and the deploy files are read from. Prefer the checkout
# this script was run out of; clone when there is none, which is what happens
# if the script was piped straight from the web.
resolve_source() {
	local here
	here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
	if [ -f "$here/pyproject.toml" ]; then
		SOURCE="$here"
		return
	fi

	command -v git >/dev/null 2>&1 || die "no local checkout, and git is not installed to fetch one"
	step "Fetching the source"
	if [ -d "$SRC/.git" ]; then
		git -C "$SRC" fetch --depth 1 origin main
		git -C "$SRC" reset --hard FETCH_HEAD
	else
		rm -rf "$SRC"
		git clone --depth 1 "$REPO_URL" "$SRC"
	fi
	SOURCE="$SRC"
}

install_packages() {
	step "Installing system packages"
	apt-get update -qq
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"
}

create_user() {
	if id "$SERVICE_USER" >/dev/null 2>&1; then
		info "service account $SERVICE_USER already exists"
	else
		step "Creating the $SERVICE_USER service account"
		useradd --system --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
	fi
	# /dev/video* is root:video, and the service is neither.
	usermod --append --groups video "$SERVICE_USER"
}

install_package() {
	step "Installing Vectra-180 from $SOURCE"
	[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
	"$VENV/bin/pip" install --quiet --upgrade pip
	# Not --editable: the venv must not depend on a checkout the operator is
	# free to move or delete afterwards.
	"$VENV/bin/pip" install --quiet --upgrade "$SOURCE"
	info "installed $("$VENV/bin/vectra180" --version)"
}

install_config() {
	install -d -m 0755 "$CONFIG_DIR"
	install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR" "$RECORDINGS"

	if [ -f "$CONFIG" ]; then
		step "Keeping the existing $CONFIG"
	else
		step "Writing $CONFIG"
		install -m 0644 "$SOURCE/deploy/config.example.toml" "$CONFIG"
	fi
	# Refresh the reference copy either way, so it matches what is installed.
	install -m 0644 "$SOURCE/deploy/config.example.toml" "$CONFIG_DIR/config.example.toml"
}

install_service() {
	step "Installing the systemd unit"
	install -m 0644 "$SOURCE/deploy/vectra180.service" "$UNIT"
	systemctl daemon-reload
	systemctl enable vectra180.service
	systemctl restart vectra180.service
}

report() {
	local host="" port=""
	# `|| true` because this is the last thing the script does: a summary that
	# cannot be printed must not turn a successful install into a failure.
	read -r host port < <("$VENV/bin/python" -c '
from vectra180.config import EngineConfig
server = EngineConfig.load("/etc/vectra180/config.toml", use_env=False).server
print(server.host, server.port)
') || true
	: "${host:=127.0.0.1}"
	: "${port:=8080}"

	printf '\n%sVectra-180 is installed and running.%s\n\n' "$GREEN" "$OFF"
	printf '  Check it over    %ssudo -u %s %s/bin/vectra180 doctor%s\n' "$DIM" "$SERVICE_USER" "$VENV" "$OFF"
	printf '  Follow the log   %sjournalctl -u vectra180 -f%s\n' "$DIM" "$OFF"
	printf '  Change settings  %ssudoedit %s && sudo systemctl restart vectra180%s\n' "$DIM" "$CONFIG" "$OFF"
	printf '  Footage          %s%s%s\n' "$DIM" "$RECORDINGS" "$OFF"
	printf '  Web interface    %shttp://%s:%s/%s\n\n' "$DIM" "$host" "$port" "$OFF"

	if [ "$host" = "127.0.0.1" ]; then
		info "That address reaches this machine only. To open it to your phone set"
		info "server.host = \"0.0.0.0\" and server.token in the config -- both, not one."
	fi
	if ! compgen -G '/dev/video*' >/dev/null; then
		warn "no /dev/video* device is present; plug the camera in and the service will pick it up"
	fi
}

# -- uninstall ---------------------------------------------------------------

uninstall() {
	local purge=$1

	step "Stopping the service"
	systemctl disable --now vectra180.service 2>/dev/null || true
	rm -f "$UNIT"
	systemctl daemon-reload

	step "Removing $PREFIX"
	rm -rf "$PREFIX"

	if [ "$purge" = "yes" ]; then
		step "Purging the configuration and the footage"
		rm -rf "$CONFIG_DIR" "$STATE_DIR"
		userdel "$SERVICE_USER" 2>/dev/null || true
		printf '\n%sRemoved.%s\n' "$GREEN" "$OFF"
	else
		printf '\n%sRemoved.%s Footage is still in %s and the config in %s.\n' \
			"$GREEN" "$OFF" "$RECORDINGS" "$CONFIG"
		printf 'Re-run with --purge to delete those as well.\n'
	fi
}

# -- entry point -------------------------------------------------------------

main() {
	case "${1:-}" in
	-h | --help)
		usage
		;;
	--uninstall)
		require_root "$@"
		uninstall no
		;;
	--purge)
		require_root "$@"
		uninstall yes
		;;
	"")
		require_root
		check_platform
		resolve_source
		install_packages
		create_user
		install_package
		install_config
		install_service
		report
		;;
	*)
		die "unknown option: $1 (try --help)"
		;;
	esac
}

main "$@"
