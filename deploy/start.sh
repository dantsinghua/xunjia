#!/usr/bin/env bash
set -euo pipefail
umask 077
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PY="$APP_ROOT/../venv/bin/python"
if [ ! -x "$PY" ]; then
  echo 'RELEASE_PYTHON_MISSING: no matching venv beside this application release' >&2
  exit 2
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$PY" "$APP_ROOT/deploy/runtime.py"