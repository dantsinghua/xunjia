#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PREFIX="${XUNJIA_PREFIX:-/home/xunjia}"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  echo 'INSTALLED_RUNTIME_MISSING: run the offline installer first' >&2
  exit 2
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
exec "$PREFIX/venv/bin/python" -I -B "$ROOT/deploy/runtime_check.py" --bundle "$ROOT" --prefix "$PREFIX" "$@"
