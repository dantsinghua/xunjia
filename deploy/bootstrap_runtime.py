"""Python 3.7 stdlib bootstrap for the verified, bundled ARM64 interpreter.

Only a new temporary directory is populated. The permanent interpreter and
release remain the deployment manager's responsibility; PATH is never changed.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


RUNTIME = 'runtime/python-runtime-aarch64.tar.gz'


class BootstrapError(ValueError):
    pass


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError('BOOTSTRAP_DUPLICATE_MANIFEST_ENTRY')
        result[key] = value
    return result


def verify_inputs(bundle, target):
    source_path = bundle / 'SOURCE-HASHES.json'
    checksum_path = bundle / 'SHA256SUMS.txt'
    for path in (source_path, checksum_path, bundle / RUNTIME, bundle / target):
        if path.is_symlink() or not path.is_file():
            raise BootstrapError('BOOTSTRAP_REQUIRED_INPUT_MISSING')
    source = json.loads(source_path.read_text(encoding='utf8'), object_pairs_hook=unique_object)
    if not isinstance(source, dict):
        raise BootstrapError('BOOTSTRAP_INVALID_SOURCE_MANIFEST')
    checksums = {}
    for line in checksum_path.read_text(encoding='utf8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match or match[2] in checksums:
            raise BootstrapError('BOOTSTRAP_INVALID_CHECKSUM_MANIFEST')
        checksums[match[2]] = match[1]
    if checksums.get('SOURCE-HASHES.json') != digest(source_path):
        raise BootstrapError('BOOTSTRAP_SOURCE_MANIFEST_HASH_MISMATCH')
    for name in (RUNTIME, target):
        expected = source.get(name)
        if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
                or checksums.get(name) != expected or digest(bundle / name) != expected):
            raise BootstrapError('BOOTSTRAP_PAYLOAD_HASH_MISMATCH')


def runtime_path(name):
    path = Path(name)
    if (not name or '\\' in name or path.is_absolute() or '..' in path.parts
            or not path.parts or path.parts[0] != 'python'):
        raise BootstrapError('BOOTSTRAP_RUNTIME_PATH_UNSAFE')
    return path


def extract_runtime(archive, destination):
    with tarfile.open(str(archive)) as package:
        members = package.getmembers()
        names, links = set(), {}
        for member in members:
            path = runtime_path(member.name)
            name = path.as_posix()
            if name in names:
                raise BootstrapError('BOOTSTRAP_DUPLICATE_RUNTIME_PATH')
            names.add(name)
            if member.issym() or member.islnk():
                base = posixpath.dirname(name) if member.issym() else ''
                if member.linkname.startswith('/') or '\\' in member.linkname:
                    raise BootstrapError('BOOTSTRAP_RUNTIME_LINK_UNSAFE')
                links[name] = runtime_path(posixpath.normpath(posixpath.join(base, member.linkname)))
            elif not (member.isfile() or member.isdir()):
                raise BootstrapError('BOOTSTRAP_RUNTIME_SPECIAL_FILE')
        # Never extract a file through a link, even if that link appears later.
        for name in names:
            if any(parent.as_posix() in links for parent in Path(name).parents):
                raise BootstrapError('BOOTSTRAP_RUNTIME_LINK_PARENT')
        for member in members:
            dest = destination / runtime_path(member.name)
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, dest.open('xb') as output:
                    shutil.copyfileobj(source, output)
                dest.chmod(member.mode & 0o777)
        for member in members:
            if not (member.issym() or member.islnk()):
                continue
            name = runtime_path(member.name).as_posix()
            dest = destination / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                dest.symlink_to(member.linkname)
            else:
                source = destination / links[name]
                try:
                    source.resolve().relative_to(destination.resolve())
                except ValueError:
                    raise BootstrapError('BOOTSTRAP_RUNTIME_LINK_UNSAFE')
                if source.is_symlink() or not source.is_file():
                    raise BootstrapError('BOOTSTRAP_RUNTIME_HARDLINK_TARGET_INVALID')
                os.link(str(source), str(dest))
    python = destination / 'python/bin/python3.12'
    if python.is_symlink() or not python.is_file() or not python.stat().st_mode & 0o111:
        raise BootstrapError('BOOTSTRAP_RUNTIME_INTERPRETER_INVALID')
    with python.open('rb') as stream:
        header = stream.read(64)
    if (len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01'
            or int.from_bytes(header[16:18], 'little') not in (2, 3)
            or int.from_bytes(header[18:20], 'little') != 183
            or int.from_bytes(header[20:24], 'little') != 1):
        raise BootstrapError('BOOTSTRAP_RUNTIME_NOT_AARCH64_ELF64')
    return python


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    target = 'deploy/manager.py'
    if args and args[0] == '--package-tests':
        args.pop(0)
        target = 'deploy/package_tests.py'
    bundle = Path(__file__).resolve().parent.parent
    try:
        if platform.machine() != 'aarch64':
            raise BootstrapError('BOOTSTRAP_REQUIRES_AARCH64')
        libc = os.confstr('CS_GNU_LIBC_VERSION')
        match = re.fullmatch(r'glibc (\d+)\.(\d+)', libc or '')
        if not match or (int(match[1]), int(match[2])) < (2, 28):
            raise BootstrapError('BOOTSTRAP_REQUIRES_GLIBC_228')
        verify_inputs(bundle, target)
        with tempfile.TemporaryDirectory(prefix='xunjia-bootstrap-') as temporary:
            python = extract_runtime(bundle / RUNTIME, Path(temporary))
            subprocess.run([str(python), '-I', '-B', '-c',
                'import sys; assert sys.version_info[:2] == (3,12)'], check=True, timeout=60)
            completed = subprocess.run([str(python), '-I', '-B', str(bundle / target), *args], timeout=3600)
            return completed.returncode if completed.returncode >= 0 else 128 - completed.returncode
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        # Never echo child stderr or operator configuration values.
        print(str(exc) if isinstance(exc, BootstrapError) else 'BOOTSTRAP_RUNTIME_UNAVAILABLE', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
