#!/usr/bin/env bash
# flop-pc-miner - native Linux installer / updater (https://github.com/Makabeez/flop-pc-miner, MIT)
# Checks an Ubuntu 24.04-class host and the NVIDIA GPU, copies flop-miner.sh and flop-dash.py
# from this repo checkout into your home directory, and prints the sha1 of each copied file.
# It downloads nothing. Run it again any time to update, from the repo folder:
#   ./linux/install.sh
set -euo pipefail

fail() { echo "error: $1" >&2; exit 1; }

# install into the invoking user's home when run through sudo
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  HOME_DIR=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
  HOME_DIR=$HOME
fi
[ -n "$HOME_DIR" ] || fail "could not determine the home directory"

# 1. Ubuntu 24.04-class host
[ -r /etc/os-release ] || fail "not an Ubuntu 24.04-class host: /etc/os-release is missing"
. /etc/os-release
case "${ID:-}" in
  ubuntu) ;;
  *)
    case "${ID_LIKE:-}" in
      *ubuntu*|*debian*) ;;
      *) fail "\"${PRETTY_NAME:-unknown}\" is not an Ubuntu 24.04-class host" ;;
    esac
    ;;
esac
case "${VERSION_ID:-}" in
  24.04*) ;;
  *) fail "Ubuntu 24.04 is required, found ${VERSION_ID:-unknown} (${PRETTY_NAME:-unknown})" ;;
esac

# 2. NVIDIA GPU visible
command -v nvidia-smi >/dev/null 2>&1 || fail "nvidia-smi not found: install the latest NVIDIA driver for your GPU first"
nvidia-smi -L >/dev/null 2>&1 || fail "nvidia-smi is present but lists no GPU: check your NVIDIA driver"
echo "GPU: $(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | head -n 1)"

# 3. Copy the scripts from this repo checkout (sha1 of what landed)
REPO=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
copy_file() {
  src=$1 dest=$2 mode=$3
  [ -f "$REPO/$src" ] || fail "missing $REPO/$src (run this script from the repo folder)"
  cp -f "$REPO/$src" "$HOME_DIR/$dest"
  chmod "$mode" "$HOME_DIR/$dest"
  if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    chown "$SUDO_USER:$(id -gn "$SUDO_USER")" "$HOME_DIR/$dest"
  fi
  sha1sum "$HOME_DIR/$dest"
}
echo
echo "Copying scripts into $HOME_DIR (sha1 of what landed):"
copy_file flop-miner.sh flop-miner.sh 755
copy_file flop-dash.py  flop-dash.py  644

echo
echo "Done. Next (first install only), in this terminal:"
echo "  cd $HOME_DIR"
echo "  ./flop-miner.sh prereqs"
echo "  WORKER_NAME=yourname-gpu ./flop-miner.sh setup"
echo "  ./flop-miner.sh harden"
echo "  ./flop-miner.sh up"
echo "Stop before switching the PC off: ./flop-miner.sh down. Full guide: README.md"
