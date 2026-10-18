#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PREFIX="${XUNJIA_PREFIX:-/home/xunjia}"
PY="${XUNJIA_QA_PYTHON:-}"
export PYTHONDONTWRITEBYTECODE=1
if [ -z "$PY" ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    PY="$(command -v python3.12)"
  elif [ -x "$PREFIX/python/bin/python3.12" ]; then
    PY="$PREFIX/python/bin/python3.12"
  elif [ -x "$PREFIX/venv/bin/python" ] && "$PREFIX/venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3,12)' >/dev/null 2>&1; then
    PY="$PREFIX/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; assert sys.version_info[:2] == (3,12)' >/dev/null 2>&1; then
    PY="$(command -v python3)"
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; assert sys.version_info >= (3,7)' >/dev/null 2>&1; then
    exec python3 "$ROOT/deploy/bootstrap_runtime.py" --package-tests --bundle "$ROOT" "$@"
  else
    echo 'QA_PYTHON312_MISSING: bundled ARM64 runtime bootstrap requires system Python 3.7 or newer' >&2
    exit 2
  fi
fi
if ! "$PY" -c 'import sys; assert sys.version_info[:2] == (3,12)' >/dev/null 2>&1; then
  echo 'QA_PYTHON312_REQUIRED' >&2
  exit 2
fi
exec "$PY" "$ROOT/deploy/package_tests.py" --bundle "$ROOT" "$@"
