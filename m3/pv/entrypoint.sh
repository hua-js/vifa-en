#!/bin/sh
set -eu
case "${1:-pv}" in
    worker|dashboard)
        exec /usr/local/bin/vifa-m3-entrypoint "$@"
        ;;
    pv)
        [ "$#" -le 1 ] || exit 64
        ;;
    *)
        echo 'usage: worker|dashboard|pv' >&2
        exit 64
        ;;
esac
test -d /run/vifa-pv
if [ -e /run/vifa-pv/pv.sock ]; then
    test -S /run/vifa-pv/pv.sock || exit 73
    rm /run/vifa-pv/pv.sock
fi
umask 0007
exec python -m uvicorn m3.worker.pv_service_app:app --workers 1 \
    --uds /run/vifa-pv/pv.sock --no-access-log
