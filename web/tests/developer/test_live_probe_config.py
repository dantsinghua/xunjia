"""Live probes must fail before touching data or network without explicit configuration."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[3]


def test_live_checker_missing_environment_never_reads_data_or_connects(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith('QB_MYSQL_'):
            monkeypatch.delenv(key)
    touched = []
    fake = types.ModuleType('pymysql')
    def connect(*args, **kwargs):
        touched.append('network')
        raise RuntimeError('unexpected connection')
    fake.connect = connect
    monkeypatch.setitem(sys.modules, 'pymysql', fake)
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == 'qb-live-e2e.raw.json':
            touched.append('data')
            return '{}'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    exit_code = None
    try:
        runpy.run_path(str(ROOT / 'web/tests/qb_live_e2e_check.py'))
    except SystemExit as error:
        exit_code = error.code
    except Exception:
        # Never render old source literals or driver arguments in a failure report.
        exit_code = 'unexpected processing before configuration'
    assert exit_code == 2
    assert touched == []


def test_live_shell_missing_environment_fails_before_starting_wait_loop():
    env = {key: value for key, value in os.environ.items() if not key.startswith('QB_MYSQL_')}
    result = subprocess.run(['bash', str(ROOT / 'web/tests/run-live-when-open.sh')],
                            env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 2
    assert 'QB_MYSQL_' in result.stderr
