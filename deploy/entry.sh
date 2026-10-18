#!/usr/bin/env bash
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd -P)"
PREFIX="${XUNJIA_PREFIX:-/home/xunjia}"
if command -v python3.12 >/dev/null 2>&1; then
  PY="$(command -v python3.12)"
elif [ -x "$PREFIX/python/bin/python3.12" ]; then
  PY="$PREFIX/python/bin/python3.12"
elif [ -x "$PREFIX/venv/bin/python" ]; then
  PY="$PREFIX/venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import sys; assert sys.version_info >= (3,9)" >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import sys; assert sys.version_info >= (3,7)" >/dev/null 2>&1; then
  export PYTHONDONTWRITEBYTECODE=1
  exec python3 "$HERE/bootstrap_runtime.py" "$@"
else
  echo "DEPLOY_BOOTSTRAP_PYTHON_MISSING: bundled ARM64 runtime bootstrap requires Python 3.7 or newer" >&2
  exit 2
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$PY" "$HERE/manager.py" "$@"
