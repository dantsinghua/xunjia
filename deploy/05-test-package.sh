#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PY="${XUNJIA_QA_PYTHON:-$(command -v python3.12 || true)}"
if [ -z "$PY" ]; then echo 'QA_PYTHON312_MISSING' >&2;exit 2;fi
export PYTHONDONTWRITEBYTECODE=1
exec "$PY" "$ROOT/deploy/package_tests.py" --bundle "$ROOT" "$@"
