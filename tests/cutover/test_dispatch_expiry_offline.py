"""Independent expiry maintenance: synthetic SQLite only, no real transports.

Expired QUEUED rows were never submitted and can be terminalized locally even
while sending is disabled. PENDING/UNKNOWN retain their reconciliation boundary.
"""
import json
import time
from fastapi.testclient import TestClient
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.runtime import Dispatcher

OWNER = 'synthetic-expiry-owner'
NOW = datetime(2026, 10, 20, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class RecordedTransport:
    """No sockets/native work; transport acceptance never fabricates a receipt."""
    supports_v22 = True

    def __init__(self):
        self.calls = []

    def result(self, op, command):
        self.calls.append((op, dict(command)))
        return {**{k: command[k] for k in ('command_id', 'account_uid', 'peer_id', 'content_hash')},
                'status': 'accepted', 'evidence': 'isolated-test-accepted-only'}

    def send(self, command):
        return self.result('send', command)

    def cancel(self, command):
        return self.result('cancel', command)

    def reconcile(self, command):
        return self.result('reconcile', command)


@pytest.fixture
def case(tmp_path, request):
    clock = [NOW]
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'expiry.sqlite'),
                     enable_background=getattr(request, 'param', False), 
                     auth_config=AuthConfig(sso_secret='isolated-expiry-sso',
                         session_secret='isolated-expiry-session-at-least-32-characters', clock=lambda: clock[0]))
    service = app.state.inquiry
    store = app.state.repository
    store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
    counter = [0]

    def queued(*, peers=1, bonds=1):
        counter[0] += 1
        request = 'isolated-' + str(counter[0])
        preview = service.preview(OWNER, request, [
            {'code': 'SYN-' + str(counter[0]) + '-' + str(i),
             'peer_ids': ['synthetic-peer-' + str(counter[0]) + '-' + str(p) for p in range(peers)],
             'our_side': 'BUY', 'volume_wan': '1000', 'opening_yield_pct': '3.20',
             'limit_yield_pct': '3.12', 'step_bp': '2'}
            for i in range(bonds)])
        value = service.confirm(OWNER, preview['batch_id'], preview['version'], request + '-confirm')
        return [service.commands_core.get(OWNER, x['command_id']) for x in value['commands']]

    def snapshot(cid):
        with store.db.transaction(write=False) as tx:
            command = dict(tx.execute('SELECT * FROM xj2_command WHERE command_id=?', (cid,)).fetchone())
            members = [dict(x) for x in tx.execute(
                'SELECT p.* FROM xj2_peer p JOIN xj2_command_member cm ON cm.member_id=p.member_id '
                'WHERE cm.command_id=? ORDER BY p.member_id', (cid,)).fetchall()]
            thread = dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (command['thread_id'],)).fetchone())
        return command, members, thread

    return SimpleNamespace(app=app, service=service, store=store, clock=clock,
                           queued=queued, snapshot=snapshot)


@pytest.mark.parametrize('has_transport', [False, True])
@pytest.mark.parametrize('at_deadline', [False, True])
def test_expired_never_submitted_commands_cancel_offline_and_clear_all_member_refs(case, has_transport, at_deadline):
    row, = case.queued(bonds=2)
    before, members, thread = case.snapshot(row['command_id'])
    assert before['state'] == 'QUEUED' and len(members) == 2
    assert all(x['pending_command_id'] == row['command_id'] for x in members)
    case.clock[0] = row['expires_at'] + (0 if at_deadline else 1)
    transport = RecordedTransport() if has_transport else None
    dispatcher = Dispatcher(case.service, transport)
    dispatcher.run_once()
    after, members, after_thread = case.snapshot(row['command_id'])
    assert after['state'] == 'CANCELLED'
    assert json.loads(after['result_json'])['reason'] == 'EXPIRED_BEFORE_DISPATCH'
    assert all(x['pending_command_id'] is None for x in members)
    assert after_thread['dispatch_version'] == thread['dispatch_version'] == 0
    assert all(json.loads(x['our_quote_json']) == {} for x in members)
    assert not transport or not transport.calls
    stable = case.snapshot(row['command_id'])
    assert dispatcher.run_once() == 0
    assert case.snapshot(row['command_id']) == stable


def test_unexpired_queued_is_not_claimed_when_send_host_unavailable(case):
    row, = case.queued()
    before = case.snapshot(row['command_id'])
    transport = None
    dispatcher = Dispatcher(case.service, transport)
    assert dispatcher.run_once() == 0
    assert case.snapshot(row['command_id']) == before
    assert not transport or not transport.calls


@pytest.mark.parametrize('state', ['PENDING', 'UNKNOWN'])
@pytest.mark.parametrize('has_transport', [False, True])
def test_expired_submitted_or_uncertain_is_not_locally_terminalized(case, state, has_transport):
    row, = case.queued()
    claimed = case.service.commands_core.claim(OWNER, row['command_id'])
    assert claimed and claimed['state'] == 'PENDING'
    if state == 'UNKNOWN':
        case.service.commands_core.receipt(OWNER, row['command_id'], account_uid='synthetic-account',
            peer_uid=claimed['peer_uid'], content_hash=row['content_hash'], status='UNKNOWN',
            evidence='isolated-test-uncertain')
    case.clock[0] = row['expires_at'] + 1
    before, before_members, thread = case.snapshot(row['command_id'])
    transport = RecordedTransport() if has_transport else None
    Dispatcher(case.service, transport).run_once()
    after, members, after_thread = case.snapshot(row['command_id'])
    assert after['state'] == state
    assert [x['pending_command_id'] for x in members] == [x['pending_command_id'] for x in before_members]
    assert after_thread['dispatch_version'] == thread['dispatch_version'] == 1
    if has_transport:
        assert [op for op, _ in transport.calls] == ['cancel'], 'Existing submitted reconciliation is preserved'
    else:
        assert after == before


def test_enabled_unexpired_command_still_submits_once_and_stays_pending_until_receipt(case):
    row, = case.queued()
    transport = RecordedTransport()
    dispatcher = Dispatcher(case.service, transport)
    dispatcher.run_once()
    after, members, thread = case.snapshot(row['command_id'])
    assert after['state'] == 'PENDING' and thread['dispatch_version'] == 1
    assert [op for op, _ in transport.calls] == ['send']
    assert members[0]['pending_command_id'] == row['command_id']
    assert json.loads(members[0]['our_quote_json']) == {}, 'Accepted is not a native send receipt'
    dispatcher.run_once()
    assert [op for op, _ in transport.calls] == ['send', 'reconcile']
    assert case.snapshot(row['command_id'])[0]['state'] == 'PENDING'


def test_expired_row_is_not_hidden_behind_twenty_unexpired_commands(case):
    rows = case.queued(peers=22)
    ordered = sorted(rows, key=lambda x: (x['created_at'], x['command_id']))
    expired = ordered[-1]
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_command SET expires_at=? WHERE command_id=?', (NOW - 1, expired['command_id']))
    Dispatcher(case.service, None).run_once()
    assert case.snapshot(expired['command_id'])[0]['state'] == 'CANCELLED'
    for row in ordered[:-1]:
        assert case.snapshot(row['command_id'])[0]['state'] == 'QUEUED'


@pytest.mark.parametrize('case', [False, True], indirect=True)
def test_actual_application_lifespan_runs_expiry_without_transport_only_when_background_enabled(case):
    expired, = case.queued()
    submitted, = case.queued()
    assert case.service.commands_core.claim(OWNER, submitted['command_id'])['state'] == 'PENDING'
    case.clock[0] = expired['expires_at'] + 1
    fresh, = case.queued()
    assert case.app.state.transport is None
    assert not hasattr(case.app.state.dispatcher, 'live_enabled')
    enabled = case.app.state.enable_background
    with TestClient(case.app):
        if enabled:
            deadline = time.monotonic() + 4
            while case.snapshot(expired['command_id'])[0]['state'] == 'QUEUED' and time.monotonic() < deadline:
                time.sleep(.02)
            row, members, thread = case.snapshot(expired['command_id'])
            assert row['state'] == 'CANCELLED'
            assert json.loads(row['result_json']) == {'reason': 'EXPIRED_BEFORE_DISPATCH'}
            assert all(x['pending_command_id'] is None for x in members)
            assert thread['dispatch_version'] == 0
        else:
            time.sleep(.1)
            assert case.snapshot(expired['command_id'])[0]['state'] == 'QUEUED'
        assert case.snapshot(submitted['command_id'])[0]['state'] == 'PENDING'
        assert case.snapshot(fresh['command_id'])[0]['state'] == 'QUEUED'
        assert not case.app.state.runtime_status['send_enabled']
        assert case.app.state.transport is None
