"""Independent acceptance: durable SQLite facts with mocked model/gateway/systemd.

Only tmp_path state is used. No real account, network, systemd or installation
directory is accessed; no product implementation is replaced by a fake store.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from server.qa_history import QAHistory
from server.qa_routes import qa_router
from server.qa_workflow import WorkflowError
from server import upgrade_automation as automation
from server import upgrade_control
from server.upgrade_control import UpgradeControl, UpgradeStore, ACTIVE
from server.upgrade_diagnostics import event
from relay.file_gateway import FileGatewayError, GatewayAuth
from relay import file_gateway_connection
from deploy import common


OWNER = 'synthetic-history-owner'
OTHER = 'synthetic-other-owner'
JOB = 'f' * 32


def shanghai(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def dump(path):
    with sqlite3.connect(path) as db:
        return '\n'.join(db.iterdump())


class Workflow:
    def __init__(self, prefix):
        self.path = prefix / 'config/qa-workflow.json'
        self.calls = []
        self.failure = False
        self.entered = threading.Event()
        self.release = None

    def status(self):
        return {'enabled': True, 'hint': 'synthetic workflow'}

    def answer(self, owner, question):
        self.calls.append((owner, question))
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5), 'Synthetic model release was not signalled'
        if self.failure:
            raise WorkflowError('SYNTHETIC_MODEL_FAILURE')
        return {'answer': '合成已保存回答：' + question, 'candidates': [],
                'chart': {'type': 'bar', 'categories': ['Bid'], 'series': [{'name': '收益率', 'data': [2.1]}]}}


def qa_client(workflow):
    app = FastAPI()

    @app.middleware('http')
    async def identity(request, call_next):
        owner = request.headers.get('X-Test-Owner')
        if owner:
            request.state.user = SimpleNamespace(user_id=owner)
        return await call_next(request)

    # Same domain-error boundary as server.app; identity is test-only middleware.
    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'ok': False, 'hint': str(error)}, status_code=400)

    app.include_router(qa_router(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), workflow=workflow))
    return TestClient(app)


@pytest.fixture
def qa(tmp_path):
    workflow = Workflow(tmp_path / 'qa-install')
    with qa_client(workflow) as client:
        yield SimpleNamespace(workflow=workflow, client=client, headers={'X-Test-Owner': OWNER},
                              path=workflow.path.parent.parent / 'state/qa-history.db')


def new_session(qa, owner=OWNER):
    response = qa.client.post('/api/qa/sessions', json={}, headers={'X-Test-Owner': owner})
    assert response.status_code == 200, response.text
    return response.json()['data']['id']


def ask(qa, sid, rid='request-0001', question='合成问题', owner=OWNER):
    return qa.client.post('/api/qa', json={'session_id': sid, 'request_id': rid, 'question': question},
                          headers={'X-Test-Owner': owner})


def test_history_survives_fresh_process_and_reads_do_not_change_sqlite(qa):
    sid = new_session(qa)
    result = ask(qa, sid)
    assert result.status_code == 200 and result.json()['data']['session_id'] == sid
    before = dump(qa.path)
    script = """import json,socket,sys
from server.qa_history import QAHistory
def offline(*args,**kwargs):raise AssertionError('Network forbidden')
socket.socket=offline
print(json.dumps(QAHistory(sys.argv[1]).read(sys.argv[2],sys.argv[3]),ensure_ascii=False))
"""
    process = subprocess.run([sys.executable, '-c', script, str(qa.path), OWNER, sid],
                             capture_output=True, text=True, timeout=20, check=True)
    saved = json.loads(process.stdout)
    assert [row['role'] for row in saved['messages']] == ['user', 'ai']
    assert saved['messages'][0]['text'] == '合成问题'
    assert saved['messages'][1]['result'] == result.json()['data']
    assert saved['messages'][1]['request_id'] == 'request-0001'
    assert qa.path.stat().st_mode & 0o777 == 0o600
    assert dump(qa.path) == before


def test_session_authentication_owner_isolation_and_404_do_not_leak(qa):
    sid = new_session(qa)
    assert ask(qa, sid).status_code == 200
    for path in ('/api/qa/sessions', '/api/qa/sessions/' + sid):
        assert qa.client.get(path).status_code == 401
    assert qa.client.post('/api/qa/sessions', json={}).status_code == 401
    foreign = {'X-Test-Owner': OTHER}
    assert qa.client.get('/api/qa/sessions', headers=foreign).json()['data']['items'] == []
    assert qa.client.get('/api/qa/sessions/' + sid, headers=foreign).status_code == 404
    assert ask(qa, sid, owner=OTHER).status_code == 404
    assert qa.client.get('/api/qa/sessions/missing', headers=qa.headers).status_code == 404
    assert len(qa.workflow.calls) == 1


def test_request_identity_replay_is_owner_scoped_and_conflicting_payload_is_rejected(qa):
    sid = new_session(qa)
    first = ask(qa, sid)
    assert first.status_code == 200
    assert ask(qa, sid).json() == first.json()
    assert ask(qa, sid, question='另一问题').status_code == 400
    assert ask(qa, new_session(qa)).status_code == 400
    assert len(qa.workflow.calls) == 1
    other = new_session(qa, OTHER)
    assert ask(qa, other, owner=OTHER).status_code == 200
    assert qa.workflow.calls[-1][0] == OTHER
    assert len(QAHistory(qa.path).read(OWNER, sid)['messages']) == 2


def test_pending_duplicate_does_not_reinvoke_model_and_finish_is_durable(qa):
    sid = new_session(qa)
    qa.workflow.release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(ask, qa, sid)
        try:
            assert qa.workflow.entered.wait(3)
            duplicate = ask(qa, sid)
            assert duplicate.status_code == 409
            snapshot = qa.client.get('/api/qa/sessions/' + sid, headers=qa.headers).json()['data']
            assert snapshot['messages'][-1]['state'] == 'pending'
            assert len(qa.workflow.calls) == 1
        finally:
            qa.workflow.release.set()
        assert pending.result(timeout=5).status_code == 200
    assert QAHistory(qa.path).read(OWNER, sid)['messages'][-1]['state'] == 'complete'


def test_failed_turn_survives_restart_and_explicit_same_request_retry_reuses_turn(qa):
    sid = new_session(qa)
    qa.workflow.failure = True
    assert ask(qa, sid).status_code == 502
    failed = QAHistory(qa.path).read(OWNER, sid)['messages']
    assert len(failed) == 2 and failed[-1]['state'] == 'failed'
    qa.workflow.failure = False
    assert ask(qa, sid).status_code == 200
    final = QAHistory(qa.path).read(OWNER, sid)['messages']
    assert [row['id'] for row in final] == [row['id'] for row in failed]
    assert final[-1]['state'] == 'complete' and len(qa.workflow.calls) == 2


@pytest.mark.parametrize('question', ['甲' * 35, '新会话'])
def test_title_is_first_question_and_list_is_latest_owner_session(qa, question):
    first, second = new_session(qa), new_session(qa)
    assert ask(qa, first, question=question).status_code == 200
    assert ask(qa, first, rid='request-0002', question='后续标题不得覆盖').status_code == 200
    rows = qa.client.get('/api/qa/sessions', headers=qa.headers).json()['data']['items']
    assert [row['id'] for row in rows] == [first, second]
    assert rows[0]['title'] == question[:30]
    assert rows[1]['title'] == '新会话'
    assert all(row['updated_at'] >= row['created_at'] for row in rows)
    assert qa.client.post('/api/qa/sessions', json={'owner': OTHER}, headers=qa.headers).status_code == 400


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    prefix = tmp_path / 'upgrade-install'
    (prefix / 'config').mkdir(parents=True)
    (prefix / 'config/upgrade-channel.json').write_text('{}')
    control = UpgradeControl.__new__(UpgradeControl)
    control.prefix = prefix
    control.app = prefix / 'app'
    config = {'operators': ['*'], 'queue': 'xjup_158', 'site_id': 'synthetic'}
    monkeypatch.setattr(control, 'configured', lambda: config)
    now = [shanghai('2026-10-31T00:00:00')]
    monkeypatch.setattr(automation.time, 'time', lambda: now[0])
    launches, deliveries = [], []

    def systemd(command, **options):
        assert command[0] == '/usr/bin/systemd-run'
        assert command[-4:] == ['--prefix', str(prefix), '--job', command[-1]]
        assert str(prefix / 'app/deploy/remote_upgrade.py') in command
        launches.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(upgrade_control.subprocess, 'run', systemd)
    monkeypatch.setattr(automation, 'upload_log', lambda p, job: deliveries.append((p, job)))
    return SimpleNamespace(control=control, now=now, launches=launches, deliveries=deliveries, config=config)


@pytest.mark.parametrize('local,expected', [
    ('2026-10-30T23:59:59', 0), ('2026-10-31T00:00:00', 1), ('2026-10-31T00:09:59', 1),
    ('2026-10-31T00:10:00', 0), ('2026-10-31T08:59:59', 0), ('2026-10-31T12:00:00', 0),
    ('2026-10-31T15:59:59', 0), ('2026-10-31T23:59:59', 0),
])
def test_shanghai_midnight_window_and_business_hours_never_catch_up(upgrade, local, expected):
    upgrade.now[0] = shanghai(local)
    automation.tick(upgrade.control, now=upgrade.now[0])
    assert len(upgrade.launches) == expected
    assert upgrade.control.store().get('scheduled_day') == ('2026-10-31' if expected else None)


def test_schedule_is_once_per_day_across_restart_and_concurrent_ticks(upgrade):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: automation.tick(upgrade.control, now=upgrade.now[0]), range(2)))
    assert len(upgrade.launches) == 1
    store = UpgradeStore(upgrade.control.prefix / 'state/upgrade')
    store.update(store.latest()['id'], 'empty')
    upgrade.now[0] += 60
    automation.tick(upgrade.control, now=upgrade.now[0])
    assert len(upgrade.launches) == 1
    upgrade.now[0] = shanghai('2026-11-01T00:00:00')
    automation.tick(upgrade.control, now=upgrade.now[0])
    assert len(upgrade.launches) == 2
    assert store.get('scheduled_day') == '2026-11-01'


@pytest.mark.parametrize('state', [*ACTIVE, 'unknown'])
def test_manual_active_or_unknown_blocks_automatic_without_consuming_day(upgrade, state):
    upgrade.control.start(OWNER)
    store = upgrade.control.store()
    store.update(store.latest()['id'], state)
    automation.tick(upgrade.control, now=upgrade.now[0])
    assert len(upgrade.launches) == 1 and store.get('scheduled_day') is None
    with pytest.raises(ValueError):
        upgrade.control.start(OWNER)
    store.update(store.latest()['id'], 'succeeded')
    upgrade.now[0] += 60
    automation.tick(upgrade.control, now=upgrade.now[0])
    assert len(upgrade.launches) == 2 and store.get('scheduled_day') == '2026-10-31'


def test_schedule_config_permissions_disable_and_fixed_time_queue(upgrade):
    upgrade.config['operators'] = [OWNER]
    with pytest.raises(PermissionError):
        automation.save_schedule(upgrade.control, OTHER, {'enabled': False})
    for body in ({'enabled': 'false'}, {'enabled': True, 'time': '10:00'}, {'enabled': True, 'log_queue': 'business'}, {}):
        with pytest.raises(ValueError):
            automation.save_schedule(upgrade.control, OWNER, body)
    saved = automation.save_schedule(upgrade.control, OWNER, {'enabled': False})
    assert saved['time'] == '00:00' and saved['timezone'] == 'Asia/Shanghai'
    assert saved['log_queue'] == 'xjup_158_logs'
    automation.tick(upgrade.control, now=upgrade.now[0])
    assert upgrade.launches == []
    automation.save_schedule(upgrade.control, OWNER, {'enabled': True})
    automation.tick(upgrade.control, now=upgrade.now[0])
    assert len(upgrade.launches) == 1 and upgrade.control.store().latest()['owner'] == OWNER


@pytest.fixture
def logs(tmp_path, monkeypatch):
    prefix = tmp_path / 'log-install'
    config = prefix / 'config'
    config.mkdir(parents=True)
    env = config / 'xunjia.env'
    env.write_text('XUNJIA_AUTH_SECRET=synthetic-private-secret\n')
    env.chmod(0o600)
    gateway = config / 'institution.json'
    gateway.write_text(json.dumps({'base_url': 'https://synthetic.invalid', 'auth_file': 'auth.json'}))
    gateway.chmod(0o600)
    auth = config / 'auth.json'
    auth.write_text('{}'); auth.chmod(0o600)
    store = UpgradeStore(prefix / 'state/upgrade')
    with store.db() as db:
        db.execute('INSERT INTO job(id,state,owner,created_at,updated_at) VALUES(?,?,?,?,?)',
                   (JOB, 'failed', 'synthetic-private-owner', 1, 2))
    event(store, JOB, 'failed', 'INSTALLER_TIMEOUT', exit_code=17)
    folder = store.root / 'jobs' / JOB
    folder.mkdir(parents=True)
    (folder / 'apply.log').write_text('Authorization: Bearer synthetic-sensitive-token\n'
        'arbitrary output synthetic-private-secret\n' + json.dumps({'status': 'failed', 'password': 'synthetic-private-secret', 'exit_code': 17}) + '\n')
    now = [shanghai('2026-10-31T00:00:00')]
    monkeypatch.setattr(automation.time, 'time', lambda: now[0])
    monkeypatch.setattr(common, 'read_env', lambda path: {} if path == env else pytest.fail('Unexpected config path'))
    monkeypatch.setattr(common, 'effective_env', lambda values, p, app: {'XUNJIA_INSTITUTION_CONFIG': str(gateway)})
    monkeypatch.setattr(GatewayAuth, 'from_file', lambda path: object() if path == auth else pytest.fail('Unexpected auth path'))
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '')
    calls, queues = [], []
    behavior = {'error': None, 'entered': threading.Event(), 'release': None}

    def upload(name, content):
        calls.append((name, content))
        behavior['entered'].set()
        if behavior['release'] is not None:
            assert behavior['release'].wait(5)
        if behavior['error']:
            raise behavior['error']

    def client(config, credentials, *, path):
        queues.append(path)
        assert path == 'xjup_158_logs'
        return SimpleNamespace(upload=upload)

    monkeypatch.setattr(file_gateway_connection, 'connection_client', client)
    return SimpleNamespace(prefix=prefix, store=store, now=now, calls=calls, queues=queues, behavior=behavior)


def test_uploaded_log_is_redacted_and_deduplicated_across_store_reopen(logs):
    result = automation.upload_log(logs.prefix, JOB)
    assert result['state'] == 'uploaded'
    content = logs.calls[0][1].decode()
    for private in ('synthetic-sensitive-token', 'synthetic-private-secret', 'synthetic-private-owner', 'arbitrary output'):
        assert private not in content
    assert 'INSTALLER_TIMEOUT' in content and '17' in content
    logs.now[0] += 3600
    assert automation.upload_log(logs.prefix, JOB) == result
    assert UpgradeStore(logs.store.root).get('log_delivery:' + JOB) == result
    assert len(logs.calls) == 1 and logs.queues == ['xjup_158_logs']


@pytest.mark.parametrize('error', [FileGatewayError('UPLOAD_TIMEOUT', ambiguous=True), OSError('synthetic disconnect')])
def test_unknown_upload_is_never_blindly_retried_even_with_new_log_content(logs, error):
    logs.behavior['error'] = error
    result = automation.upload_log(logs.prefix, JOB)
    assert result['state'] == 'unknown'
    event(logs.store, JOB, 'recovery', 'UPGRADE_RECOVERY_START_PENDING')
    logs.now[0] += 86400
    logs.behavior['error'] = None
    assert automation.upload_log(logs.prefix, JOB) == result
    assert len(logs.calls) == 1


def test_definitive_upload_failure_is_throttled_and_can_retry(logs):
    logs.behavior['error'] = FileGatewayError('UPLOAD_REJECTED', ambiguous=False)
    assert automation.upload_log(logs.prefix, JOB)['state'] == 'failed'
    logs.behavior['error'] = None
    logs.now[0] += 299
    assert automation.upload_log(logs.prefix, JOB)['state'] == 'failed' and len(logs.calls) == 1
    logs.now[0] += 1
    assert automation.upload_log(logs.prefix, JOB)['state'] == 'uploaded' and len(logs.calls) == 2


def test_concurrent_upload_claim_does_not_send_twice(logs):
    logs.behavior['release'] = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(automation.upload_log, logs.prefix, JOB)
        try:
            assert logs.behavior['entered'].wait(3)
            assert automation.upload_log(logs.prefix, JOB)['state'] == 'sending'
            assert len(logs.calls) == 1
        finally:
            logs.behavior['release'].set()
        assert pending.result(timeout=5)['state'] == 'uploaded'
