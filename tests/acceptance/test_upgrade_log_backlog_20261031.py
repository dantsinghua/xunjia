"""Independent bounded/fair diagnostic return acceptance over temporary SQLite.

No real gateway, systemd, installed directory or credentials are accessed.
"""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from deploy import common
from relay.file_gateway import GatewayAuth
from relay import file_gateway_connection
from server import upgrade_automation as automation
from server.upgrade_control import UpgradeStore


ROOT = Path(__file__).resolve().parents[2]
DAYTIME = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def control(prefix):
    def forbidden_start(*args, **kwargs):
        raise AssertionError('Log scanning must not launch an upgrade during business hours')
    return SimpleNamespace(prefix=prefix, store=lambda: UpgradeStore(prefix / 'state/upgrade'),
                           start=forbidden_start, configured=lambda: {'operators': ['*']})


def add_job(store, number, state):
    identity = format(number, '032x')
    with store.db() as db:
        db.execute('INSERT INTO job(id,state,owner,created_at,updated_at) VALUES(?,?,?,?,?)',
                   (identity, state, 'synthetic-owner', DAYTIME + number, DAYTIME + number))
    return identity


@pytest.fixture
def backlog(tmp_path):
    prefix = tmp_path / 'installed'
    config = prefix / 'config'
    config.mkdir(parents=True)
    (config / 'upgrade-channel.json').write_text('{}')
    store = UpgradeStore(prefix / 'state/upgrade')
    states = ('succeeded', 'failed', 'unknown')
    identities = [add_job(store, index + 1, states[index % 3]) for index in range(12)]
    return SimpleNamespace(prefix=prefix, store=store, identities=identities)


def run_tick(backlog, visited):
    start = len(visited)
    # A fresh control/store instance must use the durable scan position.
    automation.tick(control(backlog.prefix), now=DAYTIME)
    current = visited[start:]
    assert len(current) <= 5, 'One scheduler tick may process at most five tasks'
    return current


def test_twelve_terminal_tasks_get_a_turn_and_retry_after_wrap_with_bounded_batches(backlog, monkeypatch):
    visited = []
    def failed_upload(prefix, identity):
        assert prefix == backlog.prefix
        visited.append(identity)
        return {'state': 'failed'}
    monkeypatch.setattr(automation, 'upload_log', failed_upload)
    for _ in range(3):
        run_tick(backlog, visited)
    assert Counter(visited) == Counter(backlog.identities), 'Old terminal jobs must not starve behind the latest five'
    for _ in range(3):
        run_tick(backlog, visited)
    assert Counter(visited) == Counter({identity: 2 for identity in backlog.identities})


def test_scan_position_survives_a_fresh_process(backlog, monkeypatch):
    visited = []
    monkeypatch.setattr(automation, 'upload_log', lambda prefix, identity: visited.append(identity) or {'state': 'failed'})
    first = run_tick(backlog, visited)
    assert len(first) == 5
    script = '''import json,sys
from pathlib import Path
from types import SimpleNamespace
from server.upgrade_control import UpgradeStore
from server import upgrade_automation as automation
prefix=Path(sys.argv[1]);seen=[]
automation.upload_log=lambda prefix,identity: seen.append(identity) or {'state':'failed'}
def forbidden(*args,**kwargs):raise AssertionError('Upgrade dispatch forbidden')
control=SimpleNamespace(prefix=prefix,store=lambda:UpgradeStore(prefix/'state/upgrade'),start=forbidden)
automation.tick(control,now=float(sys.argv[2]))
print(json.dumps(seen))
'''
    result = subprocess.run([sys.executable, '-c', script, str(backlog.prefix), str(DAYTIME)],
                            cwd=ROOT, capture_output=True, text=True, timeout=15, check=True)
    second = json.loads(result.stdout)
    assert len(second) == 5 and not set(first) & set(second)
    third = run_tick(backlog, visited)
    assert Counter(first + second + third) == Counter(backlog.identities)


def test_unknown_and_raising_uploads_do_not_block_later_tasks(backlog, monkeypatch):
    visited = []
    def upload(prefix, identity):
        visited.append(identity)
        if identity == backlog.identities[0]:
            raise OSError('Synthetic unavailable diagnostic')
        return {'state': 'unknown' if identity == backlog.identities[1] else 'failed'}
    monkeypatch.setattr(automation, 'upload_log', upload)
    for _ in range(3):
        run_tick(backlog, visited)
    assert Counter(visited) == Counter(backlog.identities)


def test_real_upload_keeps_unknown_and_sending_untouched_while_servicing_the_backlog(backlog, monkeypatch):
    config = backlog.prefix / 'config'
    gateway = config / 'institution.json'
    gateway.write_text(json.dumps({'base_url': 'https://synthetic.invalid', 'auth_file': 'auth.json'}))
    gateway.chmod(0o600)
    auth = config / 'auth.json'
    auth.write_text('{}'); auth.chmod(0o600)
    monkeypatch.setattr(common, 'read_env', lambda path: {})
    monkeypatch.setattr(common, 'effective_env', lambda *args: {'XUNJIA_INSTITUTION_CONFIG': str(gateway)})
    monkeypatch.setattr(GatewayAuth, 'from_file', lambda path: object() if path == auth else pytest.fail('Unexpected auth source'))
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '')
    uploaded, queues, visited = [], [], []
    def upload_file(name, payload):
        match = re.fullmatch(r'xj-upgrade-([a-f0-9]{32})-[a-f0-9]{8}\.txt', name)
        assert match and isinstance(payload, bytes) and payload
        uploaded.append(match[1])
    def client(configuration, credentials, *, path):
        queues.append(path)
        assert path == 'xjup_158_logs'
        return SimpleNamespace(upload=upload_file)
    monkeypatch.setattr(file_gateway_connection, 'connection_client', client)
    preserved = {}
    for identity, state in zip(backlog.identities[:2], ('unknown', 'sending')):
        record = {'state': state, 'sha256': 'a' * 64, 'at': 1, 'queue': 'xjup_158_logs'}
        backlog.store.set('log_delivery:' + identity, record)
        preserved[identity] = record
    original = automation.upload_log
    def observed(prefix, identity):
        visited.append(identity)
        return original(prefix, identity)
    monkeypatch.setattr(automation, 'upload_log', observed)
    for _ in range(6):
        run_tick(backlog, visited)
    assert Counter(visited) == Counter({identity: 2 for identity in backlog.identities})
    assert Counter(uploaded) == Counter(backlog.identities[2:]), 'Unknown or identical already-uploaded logs must not be resent'
    assert queues == ['xjup_158_logs'] * 10
    for identity, record in preserved.items():
        assert UpgradeStore(backlog.store.root).get('log_delivery:' + identity) == record


def test_previously_active_older_job_is_revisited_after_it_becomes_terminal(tmp_path, monkeypatch):
    prefix = tmp_path / 'installed'
    (prefix / 'config').mkdir(parents=True)
    (prefix / 'config/upgrade-channel.json').write_text('{}')
    store = UpgradeStore(prefix / 'state/upgrade')
    active = add_job(store, 1, 'installing')
    identities = [add_job(store, number, 'failed') for number in range(2, 14)]
    data = SimpleNamespace(prefix=prefix)
    visited = []
    monkeypatch.setattr(automation, 'upload_log', lambda prefix, identity: visited.append(identity) or {'state': 'failed'})
    for _ in range(3):
        run_tick(data, visited)
    assert Counter(visited) == Counter(identities) and active not in visited
    store.update(active, 'failed')
    assert active in run_tick(data, visited)
