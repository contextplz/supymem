#!/bin/sh
# Drop root after fixing the data volume so SQLite files are writable by appuser.
set -e
DATA_DIR="${COMPANY_OS_DATA_DIR:-/workspace/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  chown -R appuser:appuser "$DATA_DIR" 2>/dev/null || true
  if [ -n "${SUPYMEM_LOCAL_PROXY_SECRET_FILE:-}" ]; then
    PROXY_DIR="$(dirname "$SUPYMEM_LOCAL_PROXY_SECRET_FILE")"
    mkdir -p "$PROXY_DIR"
    chmod 700 "$PROXY_DIR"
    chown appuser:appuser "$PROXY_DIR"
  fi
  exec gosu appuser "$0" "$@"
fi
if [ -n "${SUPYMEM_LOCAL_PROXY_SECRET_FILE:-}" ]; then
  python -m app.core.local_owner
fi
exec "$@"
