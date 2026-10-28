"""Wrap a verified deployment tarball with a no-Python/no-tar ARM64 extractor."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile

BUSYBOX_SHA256 = "7d93682be37cf6ed46699f6fff546b80bf133cf8ec342c2d04bc23387f78bc34"
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_UNPACKED_BYTES = 8 * 1024**3


class BuildError(ValueError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_busybox(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 64 <= path.stat().st_size <= 8 * 1024**2:
        raise BuildError("BOOTSTRAP_BINARY_INVALID")
    raw = path.read_bytes()
    if (raw[:7] != b"\x7fELF\x02\x01\x01" or int.from_bytes(raw[18:20], "little") != 183
            or int.from_bytes(raw[16:18], "little") not in (2, 3)):
        raise BuildError("BOOTSTRAP_AARCH64_ELF_REQUIRED")
    if hashlib.sha256(raw).hexdigest() != BUSYBOX_SHA256:
        raise BuildError("BOOTSTRAP_BINARY_HASH_MISMATCH")
    return raw


def inspect_archive(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise BuildError("ARCHIVE_INVALID")
    roots, names = set(), set()
    size = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for number, member in enumerate(archive, 1):
                name = member.name
                parts = PurePosixPath(name).parts
                if (number > 50000 or not parts or name.startswith("/") or ".." in parts
                        or "\\" in name or any(ord(char) < 32 or ord(char) == 127 for char in name)
                        or not (member.isfile() or member.isdir()) or getattr(member, "sparse", None)
                        or member.mode & 0o7000
                        or len(parts) == 1 and not member.isdir()):
                    raise BuildError("ARCHIVE_MEMBER_UNSAFE")
                normalized = PurePosixPath(name).as_posix()
                if normalized in names:
                    raise BuildError("ARCHIVE_DUPLICATE_MEMBER")
                names.add(normalized)
                roots.add(parts[0])
                size += member.size
                if size > MAX_UNPACKED_BYTES:
                    raise BuildError("ARCHIVE_TOO_LARGE")
    except (tarfile.TarError, OSError, EOFError):
        raise BuildError("ARCHIVE_INVALID") from None
    if len(roots) != 1:
        raise BuildError("ARCHIVE_SINGLE_ROOT_REQUIRED")
    root = next(iter(roots))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", root):
        raise BuildError("ARCHIVE_ROOT_INVALID")
    return root


HEADER = r'''#!/usr/bin/env bash
# Offline ARM64 extraction only. No installer or database command is run here.
set -euo pipefail
umask 077
fail() { printf '%s\n' "$1" >&2; exit 2; }
[[ $# -eq 2 && "$1" == '--extract' ]] || fail 'USAGE: bash delivery.run --extract NEW_DIRECTORY'
for command_name in uname dirname basename mktemp base64 sha256sum tail chmod mkdir rm rmdir; do
  command -v "$command_name" >/dev/null 2>&1 || fail "REQUIRED_COREUTIL_MISSING:$command_name"
done
[[ "$(uname -m)" == 'aarch64' ]] || fail 'AARCH64_REQUIRED'
[[ -f "$0" && ! -L "$0" ]] || fail 'REGULAR_SCRIPT_FILE_REQUIRED'
SELF="$(cd -P -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")"
REQUESTED="$2"
case "$REQUESTED" in
  *$'\n'*|*$'\r'*|*$'\t'*|../*|*/../*|*/..|..) fail 'UNSAFE_TARGET_PATH' ;;
esac
REQUESTED="${REQUESTED%/}"
[[ -n "$REQUESTED" ]] || fail 'UNSAFE_TARGET_PATH'
if [[ "$REQUESTED" == /* ]]; then
  ABSOLUTE="$REQUESTED"
else
  ABSOLUTE="$(pwd -P)/${REQUESTED#./}"
fi
PARENT="${ABSOLUTE%/*}"
NAME="${ABSOLUTE##*/}"
[[ -n "$NAME" && "$NAME" != '.' && "$NAME" != '..' && -d "$PARENT" ]] || fail 'TARGET_PARENT_REQUIRED'
REAL_PARENT="$(cd -P -- "$PARENT" && pwd)" || fail 'TARGET_PARENT_REQUIRED'
[[ "$REAL_PARENT" == "$PARENT" && "$REAL_PARENT" != '/' ]] || fail 'TARGET_PARENT_SYMLINK_OR_UNSAFE'
TARGET="$REAL_PARENT/$NAME"
case "$TARGET" in
  /etc/*|/usr/*|/bin/*|/sbin/*|/boot/*|/proc/*|/sys/*|/dev/*) fail 'UNSAFE_TARGET_PATH' ;;
esac
[[ ! -e "$TARGET" && ! -L "$TARGET" ]] || fail 'TARGET_ALREADY_EXISTS'
WORK=''
cleanup() {
  status=$?
  trap - EXIT
  set +e
  if [[ -n "$WORK" && "$WORK" == */xunjia-self-extract.* && -d "$WORK" && ! -L "$WORK" ]]; then
    for owned in busybox-aarch64 payload.tar.gz archive.list; do
      if [[ -f "$WORK/$owned" || -L "$WORK/$owned" ]]; then rm -f -- "$WORK/$owned"; fi
    done
    rmdir -- "$WORK" 2>/dev/null
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
WORK="$(mktemp -d -- "${TMPDIR:-/tmp}/xunjia-self-extract.XXXXXXXX")" || fail 'TEMPORARY_DIRECTORY_FAILED'
chmod 700 -- "$WORK"
TOOL="$WORK/busybox-aarch64"
PAYLOAD="$WORK/payload.tar.gz"
base64 -d > "$TOOL" <<'XUNJIA_BUSYBOX_BASE64_END'
@@BUSYBOX_BASE64@@
XUNJIA_BUSYBOX_BASE64_END
actual="$(sha256sum < "$TOOL")"
[[ "${actual%% *}" == '@@BUSYBOX_SHA256@@' ]] || fail 'BOOTSTRAP_HASH_MISMATCH'
chmod 700 -- "$TOOL"
tail -c +@@PAYLOAD_OFFSET@@ -- "$SELF" > "$PAYLOAD"
actual="$(sha256sum < "$PAYLOAD")"
[[ "${actual%% *}" == '@@PAYLOAD_SHA256@@' ]] || fail 'PAYLOAD_HASH_MISMATCH'
"$TOOL" tar -tzf "$PAYLOAD" > "$WORK/archive.list" || fail 'PAYLOAD_LIST_FAILED'
IFS= read -r first < "$WORK/archive.list" || fail 'PAYLOAD_EMPTY'
[[ "${first%%/*}" == '@@ARCHIVE_ROOT@@' ]] || fail 'PAYLOAD_ROOT_MISMATCH'
mkdir -m 700 -- "$TARGET" || fail 'TARGET_CREATE_FAILED'
if ! "$TOOL" tar --no-same-owner --no-same-permissions -xzf "$PAYLOAD" -C "$TARGET"; then
  printf 'EXTRACTION_FAILED_PARTIAL_DIRECTORY=%s\n' "$TARGET" >&2
  exit 3
fi
[[ -d "$TARGET/@@ARCHIVE_ROOT@@" && ! -L "$TARGET/@@ARCHIVE_ROOT@@" ]] || fail 'EXTRACTED_ROOT_INVALID'
printf 'EXTRACTED_ROOT=%s\n' "$TARGET/@@ARCHIVE_ROOT@@"
exit 0
'''


def stable_header(busybox_bytes, payload_sha256, archive_root):
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise BuildError("PAYLOAD_HASH_INVALID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", archive_root):
        raise BuildError("ARCHIVE_ROOT_INVALID")
    encoded = base64.b64encode(busybox_bytes).decode("ascii")
    encoded = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
    template = (HEADER.replace("@@BUSYBOX_BASE64@@", encoded)
                .replace("@@BUSYBOX_SHA256@@", BUSYBOX_SHA256)
                .replace("@@PAYLOAD_SHA256@@", payload_sha256)
                .replace("@@ARCHIVE_ROOT@@", archive_root))
    offset = 0
    for _ in range(10):
        header = template.replace("@@PAYLOAD_OFFSET@@", str(offset)).encode("ascii")
        next_offset = len(header) + 1
        if next_offset == offset:
            return header, offset
        offset = next_offset
    raise BuildError("HEADER_OFFSET_DID_NOT_STABILIZE")


def build(archive, busybox, output):
    archive, busybox, output = Path(archive), Path(busybox), Path(output)
    sidecar = output.with_name(output.name + ".sha256")
    if output.exists() or output.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise BuildError("OUTPUT_ALREADY_EXISTS")
    if archive.is_symlink() or not archive.is_file():
        raise BuildError("ARCHIVE_INVALID")
    binary = validate_busybox(busybox)
    payload_hash = digest(archive)
    root = inspect_archive(archive)
    if digest(archive) != payload_hash:
        raise BuildError("ARCHIVE_CHANGED_DURING_BUILD")
    header, offset = stable_header(binary, payload_hash, root)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="." + output.name + ".incomplete-", dir=output.parent)
    temporary = Path(temporary_name)
    complete_hash, copied_hash = hashlib.sha256(), hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as stream, archive.open("rb") as source:
            stream.write(header)
            complete_hash.update(header)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                stream.write(chunk)
                complete_hash.update(chunk)
                copied_hash.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if copied_hash.hexdigest() != payload_hash:
            raise BuildError("ARCHIVE_CHANGED_DURING_BUILD")
        temporary.chmod(0o755)
        # Link creates the new output atomically and never replaces an existing file.
        os.link(temporary, output)
        with sidecar.open("x", encoding="ascii") as stream:
            stream.write(complete_hash.hexdigest() + "  " + output.name + "\n")
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(output), "sha256": complete_hash.hexdigest(), "bytes": output.stat().st_size,
            "header_bytes": len(header), "payload_offset_1_based": offset, "payload_sha256": payload_hash,
            "bootstrap_sha256": BUSYBOX_SHA256, "archive_root": root, "mode": "extract_only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(args.archive, args.busybox, args.output)
    except (OSError, BuildError) as exc:
        print(json.dumps({"status": "FAILED", "code": str(exc) if isinstance(exc, BuildError) else "SELF_EXTRACT_BUILD_UNAVAILABLE"}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
