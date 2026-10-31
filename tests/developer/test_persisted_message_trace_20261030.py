"""Synthetic, isolated SQLite trace evidence; no collector, model or live DB."""
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.fresh_state import clear_workspace
from server.negotiation.application_store import ApplicationStore
from server.negotiation.commands import Commands
from server.negotiation.http import router
from server.negotiation.prices import LimitPolicy
from server.negotiation.service import Inquiry


OWNER = 'trace-owner'
NOW = 1793318400.0
DAY = '2026-10-30'


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / 'trace.sqlite'
    store = ApplicationStore('sqlite:///' + str(path), initialize=True, clock=lambda: NOW)
    store.bind_account(OWNER, 'trace-account', 'trace-workbench')
    group = store.create_group(OWNER, bond_code='SYN-A', our_side='BUY', target_volume='1000',
                               trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '2.00'), send_allowed=True)
    member, thread = store.add_member(OWNER, group, account_uid='trace-account', peer_uid='trace-peer')
    return store, path, group, member, thread


def ingest(store, thread, n=1, **options):
    return store.ingest(OWNER, thread, event_id='event-' + str(n), source_seq=n,
                        text='合成券2.1收1000', trade_day=DAY, occurred_at=NOW - 1,
                        native_id='native-' + str(n), **options)


def analyze(store, thread, message, member):
    job = store.claim_analysis(OWNER, thread, force=True)
    store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
                          scopes={message: [member]}, observations=[])
    return job


def dump(path):
    with sqlite3.connect(path) as db:
        return '\n'.join(db.iterdump())


def client(store):
    class Offline:
        def __getattr__(self, name):
            raise AssertionError('Trace attempted collector/network access: ' + name)

    app = FastAPI()

    @app.middleware('http')
    async def auth(request, call_next):
        request.state.user = SimpleNamespace(user_id=request.headers.get('X-Test-Owner', OWNER))
        return await call_next(request)

    app.include_router(router(Inquiry(store), Offline(), Offline(), Offline(), collector_selection=Offline()))
    return TestClient(app)


def test_raw_message_is_durable_without_inventing_analysis(desk):
    store, path, _, _, thread = desk
    message = ingest(store, thread)
    before = dump(path)
    trace = store.message_trace(OWNER, message)
    assert trace['id'] == message and trace['status'] == '已持久化'
    assert trace['text'] == '合成券2.1收1000'
    assert [s['title'] for s in trace['steps']] == ['消息已落库']
    assert trace['evidence']['message']['event_id'] == 'event-1'
    assert trace['evidence']['message']['native_id'] == 'native-1'
    assert trace['evidence']['analyses'] == trace['evidence']['decisions'] == trace['evidence']['commands'] == []
    assert before == dump(path)


def test_analysis_decision_and_receipt_link_by_real_ids(desk):
    store, _, _, member, thread = desk
    message = ingest(store, thread)
    job = analyze(store, thread, message, member)
    store.set_sending(OWNER, True)
    commands = Commands(store)
    queued = commands.propose(OWNER, member, basis=commands.basis(OWNER, member), request_id='trace-reply',
        proposal={'kind': 'SOCIAL', 'text': '合成回复'}, validator=lambda p: p, expires_at=NOW + 60)
    assert queued['state'] == 'QUEUED'
    trace = store.message_trace(OWNER, message)
    assert [a['analysis_id'] for a in trace['evidence']['analyses']] == [job['analysis_id']]
    assert [d['decision_id'] for d in trace['evidence']['decisions']] == [queued['decision_id']]
    assert not any(s['title'] == '发送回执' for s in trace['steps'])
    command = commands.get(OWNER, queued['command_id'])
    commands.receipt(OWNER, command['command_id'], account_uid='trace-account', peer_uid='trace-peer',
                     content_hash=command['content_hash'], status='SENT', evidence='synthetic-native-proof',
                     native_id='native-reply', occurred_at=NOW)
    trace = store.message_trace(OWNER, message)
    assert trace['evidence']['commands'][0]['result']['receipts'][0]['evidence'] == 'synthetic-native-proof'
    assert sum(s['title'] == '发送回执' for s in trace['steps']) == 1
    with store.db.transaction(write=False) as tx:
        sent = tx.execute("SELECT message_id FROM xj2_message WHERE native_id='native-reply'").fetchone()['message_id']
    outbound = store.message_trace(OWNER, sent)
    assert outbound['dirLabel'] == '发送'
    assert outbound['evidence']['commands'][0]['command_id'] == command['command_id']
    assert outbound['evidence']['decisions'][0]['decision_id'] == queued['decision_id']


def test_failed_analysis_is_not_success_and_snapshot_holes_are_not_linked(desk):
    store, _, _, _, thread = desk
    # Workspace retirement activates the real historical-message exclusion.
    with store.write(OWNER) as tx:
        clear_workspace(tx, OWNER, clock=lambda: NOW)
    historical = ingest(store, thread, historical=True)
    actual = ingest(store, thread, 2)
    job = store.claim_analysis(OWNER, thread, force=True)
    store.fail_analysis(OWNER, job['analysis_id'], job['lease_token'], reason='SyntheticFailure')
    trace = store.message_trace(OWNER, actual)
    assert trace['evidence']['analyses'][0]['state'] == 'RETRY'
    assert trace['evidence']['analyses'][0]['result'] == {'failure': 'SyntheticFailure'}
    assert trace['evidence']['decisions'] == []
    assert store.message_trace(OWNER, historical)['evidence']['analyses'] == []


def test_offline_restart_archived_binding_switch_and_owner_isolation(desk):
    store, path, _, member, thread = desk
    message = ingest(store, thread)
    analyze(store, thread, message, member)
    with store.write(OWNER) as tx:
        store.audit(tx, OWNER, 'trace-test', message, {'persisted': True})
        store.audit(tx, 'another-owner', 'foreign-audit', message, {'private': 'must-not-leak'})
        clear_workspace(tx, OWNER, clock=lambda: NOW)
        tx.execute('UPDATE xj2_binding SET account_uid=?,workbench_id=? WHERE owner_id=?',
                   ('new-account', 'new-workbench', OWNER))
    # A new owner of the old native account must not inherit its prior owner's evidence.
    store.bind_account('another-owner', 'trace-account', 'trace-workbench')
    before = dump(path)
    restarted = ApplicationStore('sqlite:///' + str(path))
    with client(restarted) as http:
        url = '/api/negotiation/messages/' + message + '/trace'
        result = http.get(url)
        assert result.status_code == 200, result.text
        trace = result.json()['data']
        assert trace['evidence']['message']['account_uid'] == 'trace-account'
        assert [a['kind'] for a in trace['evidence']['audits']] == ['trace-test']
        assert trace['evidence']['analyses'][0]['state'] == 'COMMITTED'
        assert http.get(url, headers={'X-Test-Owner': 'another-owner'}).status_code == 404
        assert http.get('/api/negotiation/messages/missing/trace').status_code == 404
    assert dump(path) == before
    # Fresh process, no service/collector assembly: only persisted SQLite facts.
    script = """import json,socket,sys
from server.negotiation.application_store import ApplicationStore
def offline(*args, **kwargs): raise AssertionError('Network is forbidden')
socket.socket=offline
store=ApplicationStore(sys.argv[1])
print(json.dumps(store.message_trace(sys.argv[2],sys.argv[3]),ensure_ascii=False))
"""
    result = subprocess.run([sys.executable, '-c', script, 'sqlite:///' + str(path), OWNER, message],
                            capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout) == trace
    assert dump(path) == before


def test_later_analysis_and_other_member_decisions_do_not_attach(desk):
    store, _, group, member, thread = desk
    message = ingest(store, thread)
    analyze(store, thread, message, member)
    store.set_sending(OWNER, True)
    commands = Commands(store)
    other_group = store.create_group(OWNER, bond_code='SYN-B', our_side='BUY', target_volume='1000',
                                    trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '2.00'), send_allowed=True)
    other_member, _ = store.add_member(OWNER, other_group, account_uid='trace-account', peer_uid='trace-peer')
    commands.propose(OWNER, other_member, basis=commands.basis(OWNER, other_member), request_id='other-member',
        proposal={'kind': 'SOCIAL', 'text': '另一券'}, validator=lambda p: p, expires_at=NOW + 60)
    second = ingest(store, thread, 2)
    analyze(store, thread, second, member)
    commands.propose(OWNER, member, basis=commands.basis(OWNER, member), request_id='later-message',
        proposal={'kind': 'SOCIAL', 'text': '后一条'}, validator=lambda p: p, expires_at=NOW + 60)
    trace = store.message_trace(OWNER, message)
    assert trace['evidence']['decisions'] == []
    assert trace['evidence']['commands'] == []
