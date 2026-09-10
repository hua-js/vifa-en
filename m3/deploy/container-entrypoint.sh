#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: vifa-m3-entrypoint worker|dashboard" >&2
  exit 64
fi

case "$1" in
  worker)
    application="m3.worker.main:app"
    socket_path="/run/vifa-m3/worker.sock"
    ;;
  dashboard)
    application="m3.worker.persisted_dashboard_app:app"
    socket_path="/run/vifa-m3/dashboard.sock"
    ;;
  *)
    echo "unsupported M3 process mode" >&2
    exit 64
    ;;
esac

if [[ ! -d /run/vifa-m3 ]]; then
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
exec python -m uvicorn "$application" \
  --workers 1 \
  --uds "$socket_path" \
  --no-access-log
