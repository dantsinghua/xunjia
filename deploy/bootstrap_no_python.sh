#!/usr/bin/env bash
# First-stage bootstrap for ARM64 hosts without a system Python.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd -P)"
ARCHIVE="$HERE/../runtime/python-runtime-aarch64.tar.gz"
EXPECTED='c2083943c86dbb21ca0211238362fd922de7b0475688f26c135cf5d20a1c2f48'
fail() { echo "$1" >&2; exit 2; }
[ "$(uname -m)" = aarch64 ] || fail BOOTSTRAP_REQUIRES_AARCH64
LIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
if [[ ! "$LIBC" =~ ^glibc\ ([0-9]+)\.([0-9]+)$ ]]; then
  fail BOOTSTRAP_REQUIRES_GLIBC_228
fi
if (( 10#${BASH_REMATCH[1]} < 2 || (10#${BASH_REMATCH[1]} == 2 && 10#${BASH_REMATCH[2]} < 28) )); then
  fail BOOTSTRAP_REQUIRES_GLIBC_228
fi
[ -f "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || fail BOOTSTRAP_RUNTIME_MISSING
HASH="$(sha256sum -- "$ARCHIVE")"
[ "${HASH%% *}" = "$EXPECTED" ] || fail BOOTSTRAP_RUNTIME_HASH_MISMATCH
TOOL="$HERE/../runtime/tools/busybox-aarch64"
TOOL_EXPECTED='7d93682be37cf6ed46699f6fff546b80bf133cf8ec342c2d04bc23387f78bc34'
EXTRACTOR=(tar)
if [ -e "$TOOL" ] || [ -L "$TOOL" ]; then
  [ -f "$TOOL" ] && [ -x "$TOOL" ] && [ ! -L "$TOOL" ] || fail BOOTSTRAP_TAR_INVALID
  HASH="$(sha256sum -- "$TOOL")"
  [ "${HASH%% *}" = "$TOOL_EXPECTED" ] || fail BOOTSTRAP_TAR_HASH_MISMATCH
  EXTRACTOR=("$TOOL" tar)
elif ! command -v tar >/dev/null 2>&1; then
  fail BOOTSTRAP_TAR_MISSING
fi
TEMP_PARENT="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
TEMP_ROOT="$(mktemp -d "$TEMP_PARENT/xunjia-no-python-XXXXXXXXXX")"
chmod 700 "$TEMP_ROOT"
IDENTITY="$(stat -c '%d:%i' -- "$TEMP_ROOT")"
# If extraction fails before a verified interpreter exists, retain the exact
# owned directory for diagnosis instead of recursive shell deletion.
trap 'echo "BOOTSTRAP_PREPARATION_RETAINED: $TEMP_ROOT" >&2' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp -- "$ARCHIVE" "$TEMP_ROOT/runtime.tar.gz"
HASH="$(sha256sum -- "$TEMP_ROOT/runtime.tar.gz")"
[ "${HASH%% *}" = "$EXPECTED" ] || fail BOOTSTRAP_RUNTIME_HASH_MISMATCH
# Only the audited, fixed-hash archive reaches tar. Copying before verification
# prevents source changes during extraction from changing the accepted payload.
"${EXTRACTOR[@]}" -xzf "$TEMP_ROOT/runtime.tar.gz" --no-same-owner --no-same-permissions -C "$TEMP_ROOT"
PY="$TEMP_ROOT/python/bin/python3.12"
[ -f "$PY" ] && [ -x "$PY" ] && [ ! -L "$PY" ] || fail BOOTSTRAP_RUNTIME_INTERPRETER_INVALID
"$PY" -I -B -c 'import sys; assert sys.version_info[:2] == (3, 12)'
exec "$PY" -I -B - "$TEMP_ROOT" "$IDENTITY" "$HERE/bootstrap_runtime.py" "$@" <<'PY'
import os
from pathlib import Path
import runpy
import shutil
import signal
import stat
import sys

root = Path(sys.argv[1])
identity = sys.argv[2]
bootstrap, arguments = sys.argv[3], sys.argv[4:]
info = root.lstat()
if (root.is_symlink() or not stat.S_ISDIR(info.st_mode)
        or root.resolve() != root or not root.name.startswith('xunjia-no-python-')
        or f'{info.st_dev}:{info.st_ino}' != identity
        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
    raise SystemExit('BOOTSTRAP_TEMP_IDENTITY_MISMATCH')
received_signal = 0

def interrupted(signum, frame):
    global received_signal
    received_signal = signum
    raise KeyboardInterrupt

signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, interrupted)
try:
    sys.argv = [bootstrap, *arguments]
    # Keep the existing manifest, archive and manager verification unchanged.
    runpy.run_path(bootstrap, run_name='__main__')
except KeyboardInterrupt:
    raise SystemExit(128 + (received_signal or signal.SIGINT))
finally:
    # This Python came from the pinned archive. Delete only the same owned root.
    current = root.lstat()
    if root.is_symlink() or f'{current.st_dev}:{current.st_ino}' != identity:
        raise SystemExit('BOOTSTRAP_TEMP_IDENTITY_MISMATCH')
    shutil.rmtree(root)
PY
