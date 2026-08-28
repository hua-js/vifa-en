#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/userdata/holo/pyfiles/vifa-m3"
RUN_DIR="/userdata/holo/pyfiles/vifa-m3/run"
CONFIG_DIR="/etc/vifa-m3"

if [[ ${EUID} -ne 0 ]]; then
  echo "prepare-server.sh must run as root" >&2
  exit 77
fi

install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o 10001 -g 10001 -m 0750 "$RUN_DIR"
install -d -o root -g root -m 0700 "$CONFIG_DIR"
