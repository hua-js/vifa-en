#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/userdata/holo/pyfiles/vifa-m3"
PYTHON_BIN="$APP_DIR/.venv/bin/python"

if [[ $# -ne 1 ]]; then
  echo "usage: host-entrypoint.sh worker|dashboard" >&2
  exit 64
fi

case "$1" in
  worker)
    application="m3_worker.main:app"
    socket_path="/userdata/holo/pyfiles/vifa-m3/run/worker.sock"
    ;;
  dashboard)
    application="m3_worker.persisted_dashboard_app:app"
    socket_path="/userdata/holo/pyfiles/vifa-m3/run/dashboard.sock"
    ;;
  *)
    echo "unsupported M3 process mode" >&2
    exit 64
    ;;
esac

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "M3 Python virtual environment is missing" >&2
  exit 72
fi

if [[ ! -d "$APP_DIR/run" ]]; then
  echo "M3 socket directory is missing" >&2
  exit 73
fi

if [[ -e "$socket_path" ]]; then
  if test -S "$socket_path"; then
    rm -f -- "$socket_path"
  else
    echo "M3 socket path is occupied by a non-socket" >&2
    exit 73
  fi
fi

umask 0007
exec "$PYTHON_BIN" -m uvicorn "$application" \
  --workers 1 \
  --uds "$socket_path" \
  --no-access-log
