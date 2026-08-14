#!/bin/sh
# A release may use bridge networking or the e606 host network.  Keep the
# binding decision explicit so the latter never exposes the backend directly.
set -eu

exec "${UVICORN_BIN:-uvicorn}" app.main:app \
    --host "${APP_BIND_HOST:-0.0.0.0}" \
    --port "${APP_PORT:-9000}" \
    --log-level "${LOG_LEVEL:-info}" \
    --no-proxy-headers
