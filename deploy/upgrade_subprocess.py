"""Fixed-path installer child launched in its own temporary systemd service."""
import argparse
import os
from pathlib import Path
import re
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--job', required=True)
    args = parser.parse_args()
    prefix = args.prefix.absolute()
    if not re.fullmatch('[0-9a-f]{32}', args.job) or prefix.resolve() != prefix:
        raise SystemExit('INVALID_UPGRADE_JOB')
    stage = prefix / 'state/upgrade/jobs' / args.job
    if stage.resolve() != stage or not (stage / 'upgrade.py').is_file():
        raise SystemExit('INVALID_UPGRADE_STORAGE')
    os.umask(0o077)
    fd = os.open(stage / 'apply.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    env = dict(os.environ, XUNJIA_UPGRADE_JOB_ID=args.job, PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-I', '-B', str(stage / 'upgrade.py'), '--prefix', str(prefix), '--apply']
    os.execve(sys.executable, command, env)


if __name__ == '__main__':
    main()
