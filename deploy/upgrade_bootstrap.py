"""One-command first install; the verified capsule discovers its own baseline."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys


def install(directory, prefix, *, runner=None):
    directory, prefix = Path(directory).absolute(), Path(prefix).absolute()
    if directory.resolve() != directory or prefix.resolve() != prefix:
        raise ValueError('BOOTSTRAP_PATH_INVALID')
    entries = {}
    for line in (directory / 'SHA256SUMS.txt').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in entries or match[2] in ('.', '..', 'SHA256SUMS.txt'):
            raise ValueError('BOOTSTRAP_MANIFEST_INVALID')
        entries[match[2]] = match[1]
    if not {'install.py', 'install.sh', 'upgrade.py', 'channel.json'} <= entries.keys():
        raise ValueError('BOOTSTRAP_MANIFEST_INCOMPLETE')
    for name, digest in entries.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('BOOTSTRAP_INTEGRITY_FAILED')
    runner = runner or subprocess.run
    runner([sys.executable, '-I', '-B', str(directory / 'upgrade.py'),
            '--prefix', str(prefix), '--apply'], check=True)
    # The newly installed interpreter and helper are selected after the release
    # switch. No remembered path or directory-name guess is authoritative.
    runner([str(prefix / 'venv/bin/python'), '-I', '-B',
            str(prefix / 'app/deploy/enable_remote_upgrade.py'),
            '--prefix', str(prefix), '--channel', str(directory / 'channel.json')], check=True)
    return {'status': 'PAGE_UPGRADE_AND_RETENTION_ENABLED'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path('/home/xunjia'))
    args = parser.parse_args()
    try:
        print(install(Path(__file__).parent, args.prefix)['status'])
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        code = str(exc)
        print(code if re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', code) else 'BOOTSTRAP_FAILED', file=sys.stderr)
        raise SystemExit(2)
