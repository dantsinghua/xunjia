"""Bounded local regression runner; no live service/credential configuration.
Run with a Python environment that has requirements-dev.lock installed:
    python -B scripts/check_negotiation.py v22
    python -B scripts/check_negotiation.py all
Only this invocation's temporary test data is removed. Other worktrees,
services, deployment credentials, and existing databases are untouched.
"""
from __future__ import annotations
import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite',choices=('v22','all'),default='v22',nargs='?')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    code=root/'app' if (root/'app/server').is_dir() else root
    print('SCRIPT_STARTED',args.suite,flush=True)
    for directory in (code/'server/negotiation',root/'tests/negotiation'):
        for path in directory.glob('*.py'):
            ast.parse(path.read_text(encoding='utf8'),filename=str(path))
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=os.pathsep.join((str(code),str(root))))
    target='tests/negotiation' if args.suite=='v22' else 'tests'
    start=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='xunjia-v22-pytest-') as directory:
        argv=[sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider',
              '--basetemp',str(Path(directory)/'pytest'),target,'--disable-warnings','--tb=short']
        try:
            result=subprocess.run(argv,cwd=root,env=env,timeout=110 if args.suite=='all' else 65)
            status=result.returncode
        except subprocess.TimeoutExpired:
            print('SCRIPT_TEST_TIMEOUT',flush=True)
            status=124
    assert not Path(directory).exists()
    print('OWNED_TEST_DATA_REMOVED',flush=True)
    print('SCRIPT_COMPLETED','exit='+str(status),'seconds='+str(round(time.monotonic()-start,2)),flush=True)
    return status
if __name__=='__main__':
    raise SystemExit(main())
