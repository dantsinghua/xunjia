"""Only synthetic, task-owned processes for the process runner acceptance gate."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def identity():
    stat = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': os.getpid(), 'pgid': os.getpgid(0), 'sid': os.getsid(0),
            'start_ticks': int(stat[19]),
            'token': os.environ['P0_FIXTURE_TOKEN']}


def record():
    data = (json.dumps(identity()) + '\n').encode()
    fd = os.open(os.environ['P0_FIXTURE_REGISTRY'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def wait_records(count):
    deadline = time.monotonic() + 3
    path = Path(os.environ['P0_FIXTURE_REGISTRY'])
    while time.monotonic() < deadline:
        if path.exists() and len(path.read_text().splitlines()) >= count:
            return
        time.sleep(0.005)
    raise RuntimeError('Synthetic descendants did not become ready')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default=os.environ.get('P0_FIXTURE_MODE', 'success'))
    parser.add_argument('--depth', type=int, default=0)
    args, _ = parser.parse_known_args()
    if args.mode == 'silent':
        return 0
    if args.mode == 'success':
        print('FIXTURE_BEGIN', flush=True)
        Path(os.environ['P0_FIXTURE_ARTIFACT']).write_text('synthetic-result\n')
        print('FIXTURE_END', flush=True)
        return 0
    if args.mode == 'error':
        print('SYNTHETIC_ORIGINAL_STDERR', file=sys.stderr, flush=True)
        return 23
    if args.mode in ('ignore-tree', 'worker'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    record()
    if args.mode in ('term-tree', 'ignore-tree') or args.depth:
        depth = 1 if args.mode in ('term-tree', 'ignore-tree') else args.depth - 1
        subprocess.Popen([sys.executable, __file__, '--mode', 'worker', '--depth', str(depth)])
    if args.mode in ('term-tree', 'ignore-tree'):
        wait_records(3)
    print('FIXTURE_READY', flush=True)
    while True:
        signal.pause()


if __name__ == '__main__':
    raise SystemExit(main())
