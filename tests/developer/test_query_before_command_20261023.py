"""Queries observe delivery; only execute may establish a durable native claim."""
from types import SimpleNamespace

import pytest

from relay.collector_bridge import CollectorBridge
from relay.protocol import Binding, ProtocolError
from server.file_gateway_transport import FileGatewayTransport
from server.receiver_ledger import ReceiverLedger
from tests.cutover.file_gateway_fakes import Gateway, NOW


B = Binding('synthetic-query-owner', 'synthetic-account', 'synthetic-workbench', b'k' * 32, b's' * 32)


def command(**changes):
    return {'command_id': 'synthetic-query-command', 'owner_user_id': B.owner_user_id,
        'account_uid': B.account_uid, 'workbench_id': B.workbench_id, 'peer_id': 'synthetic-peer',
        'content_hash': 'a' * 64, 'control_version': 1, 'expires_at': NOW + 120,
        'text': 'synthetic authorized text', 'transport': 'oss', 'context_id': 'active-context',
        'business_version': '2.2', 'source_watermark': 0, **changes}


def receiver(tmp_path):
    ledger = ReceiverLedger(tmp_path / 'receiver.db', B, clock=lambda: NOW)
    ledger.activate_context('active-context', NOW * 1000000)
    return ledger


def retained(ledger):
    with ledger._db() as db:
        return {name: [dict(row) for row in db.execute('SELECT * FROM ' + name + ' WHERE scope=? ORDER BY rowid', (ledger.scope,))]
            for name in ('rx_command', 'rx_audit', 'rx_tombstone')}


def wire(tmp_path):
    gateway = Gateway()
    service = FileGatewayTransport(B, gateway.client('up'), gateway.client('down'), tmp_path / 'service.db',
        role='service', clock=lambda: NOW)
    collector = FileGatewayTransport(B, gateway.client('down'), gateway.client('up'), tmp_path / 'collector.db',
        role='collector', clock=lambda: NOW)
    ledger = receiver(tmp_path)
    sends, queries = [], []
    def send(value):
        sends.append(value['command_id'])
        return {'status': 'sent', 'evidence': 'synthetic exact native receipt', 'native_id': 'native-once'}
    bridge = CollectorBridge(ledger=ledger, adapters={'oss': collector}, native_send=send,
        native_reconcile=lambda value: queries.append(value['command_id']))
    return SimpleNamespace(service=service, collector=collector, ledger=ledger, bridge=bridge,
        sends=sends, queries=queries, gateway=gateway)


def test_same_batch_command_then_query_control_priority_still_executes_once(tmp_path):
    w = wire(tmp_path)
    assert w.service.send(command())['status'] == 'accepted'
    w.service.reconcile(command())
    result = w.bridge.run_once()
    assert result == {'processed': 2, 'errors': []}
    assert w.sends == [command()['command_id']] and w.queries == []
    history = retained(w.ledger)
    assert [row['status'] for row in history['rx_audit']] == ['pending', 'sent']
    assert history['rx_command'][0]['status'] == 'sent'
    w.bridge.run_once()
    assert len(w.sends) == 1


def test_query_in_earlier_batch_and_restart_does_not_occupy_execution_identity(tmp_path):
    w = wire(tmp_path)
    w.service.reconcile(command())
    assert w.bridge.run_once() == {'processed': 1, 'errors': []}
    assert retained(w.ledger)['rx_command'] == retained(w.ledger)['rx_audit'] == []
    assert w.sends == w.queries == []
    w.bridge.ledger = ReceiverLedger(w.ledger.path, B, clock=lambda: NOW)
    assert w.service.send(command())['status'] == 'accepted'
    assert w.bridge.run_once() == {'processed': 1, 'errors': []}
    assert w.sends == [command()['command_id']]
    assert [row['status'] for row in retained(w.ledger)['rx_audit']] == ['pending', 'sent']


def test_query_without_claim_never_invokes_native_lookup_or_records_a_native_outcome(tmp_path):
    ledger = receiver(tmp_path)
    before = retained(ledger)
    result = ledger.reconcile(command(), lambda value: pytest.fail('No native claim exists to reconcile'))
    assert result['status'] == 'pending' and result['evidence'] == 'awaiting-command-delivery'
    assert retained(ledger) == before
    assert ledger.permission(command()) is False


@pytest.mark.parametrize('status', ['pending', 'unknown'])
def test_existing_uncertainty_remains_reconciliation_only_and_preserves_original_history(tmp_path, status):
    ledger = receiver(tmp_path)
    sends = []
    def native(value):
        sends.append(value['command_id'])
        if status == 'pending':
            raise SystemExit('synthetic crash after durable claim')
        return {'status': 'unknown', 'evidence': 'actual native result was uncertain'}
    if status == 'pending':
        with pytest.raises(SystemExit):
            ledger.execute(command(), native)
    else:
        assert ledger.execute(command(), native)['status'] == 'unknown'
    before = retained(ledger)
    restart = ReceiverLedger(ledger.path, B, clock=lambda: NOW)
    assert restart.reconcile(command(), lambda value: None)['status'] == 'unknown'
    assert restart.execute(command(), lambda value: pytest.fail('Never retry an existing uncertain claim'))['status'] == 'unknown'
    after = retained(ledger)
    assert len(sends) == 1
    for key in ('fingerprint', 'command_json'):
        assert after['rx_command'][0][key] == before['rx_command'][0][key]
    assert after['rx_audit'][:len(before['rx_audit'])] == before['rx_audit']
    assert after['rx_command'][0]['status'] == 'unknown'


@pytest.mark.parametrize('barrier', ['tombstone', 'epoch', 'context', 'expiry', 'control', 'manual-fence'])
def test_unclaimed_query_checks_existing_rejection_barriers_without_writing_execution(tmp_path, barrier):
    ledger = receiver(tmp_path)
    value = command()
    if barrier == 'tombstone':
        with ledger._db() as db:
            db.execute('INSERT INTO rx_tombstone VALUES(?,?,?,?)',
                (ledger.scope, value['command_id'], ledger._fingerprint(value), NOW))
    elif barrier == 'epoch':
        value['account_epoch'] = 1
    elif barrier == 'context':
        value['context_id'] = 'retired-context'
    elif barrier == 'expiry':
        value['expires_at'] = NOW - 1
    elif barrier == 'control':
        ledger.cancel(command(command_id='another-cancelled-command'))
    else:
        ledger.stage_out({'event_id': 'synthetic-human', 'peer_id': value['peer_id'], 'ext_msg_id': 'human-native',
            'direction': 'out', 'role': 'human', 'occurred_at': NOW, 'text': 'human'}, 5)
    before = retained(ledger)
    result = ledger.reconcile(value, lambda data: pytest.fail('Rejected query must not call native lookup'))
    assert result['status'] == ('failed' if barrier == 'expiry' else 'cancelled')
    assert retained(ledger) == before
    assert ledger.permission(value) is False


@pytest.mark.parametrize('field', ['owner_user_id', 'account_uid', 'workbench_id', 'channel'])
def test_query_rejects_wrong_native_scope_before_any_ledger_mutation(tmp_path, field):
    ledger = receiver(tmp_path)
    value = command(**{field: 'wechat' if field == 'channel' else 'foreign-scope'})
    before = retained(ledger)
    with pytest.raises(ProtocolError):
        ledger.reconcile(value, lambda data: pytest.fail('Wrong native scope'))
    assert retained(ledger) == before


@pytest.mark.parametrize('tombstone_only', [False, True])
def test_query_preserves_existing_command_or_tombstone_fingerprint_rejection(tmp_path, tombstone_only):
    ledger = receiver(tmp_path)
    value = command()
    if tombstone_only:
        with ledger._db() as db:
            db.execute('INSERT INTO rx_tombstone VALUES(?,?,?,?)',
                (ledger.scope, value['command_id'], ledger._fingerprint(value), NOW))
    else:
        ledger.execute(value, lambda data: {'status': 'unknown', 'evidence': 'original uncertainty'})
    before = retained(ledger)
    with pytest.raises(ProtocolError):
        ledger.reconcile({**value, 'text': 'different immutable text'}, lambda data: None)
    assert retained(ledger) == before


def test_legacy_query_created_unknown_is_retained_without_trying_native_execution(tmp_path):
    ledger = receiver(tmp_path)
    value = command()
    with ledger._db() as db:
        ledger._record(db, value, ledger._fingerprint(value), 'unknown', 'no-conclusive-native-evidence')
    before = retained(ledger)
    assert [row['status'] for row in before['rx_audit']] == ['unknown']
    assert ledger.execute(value, lambda data: pytest.fail('Never repair old UNKNOWN by executing'))['status'] == 'unknown'
    assert retained(ledger) == before
    assert ledger.reconcile(value, lambda data: None)['status'] == 'unknown'
    after = retained(ledger)
    assert after['rx_command'][0]['command_json'] == before['rx_command'][0]['command_json']
    assert after['rx_audit'][:len(before['rx_audit'])] == before['rx_audit']
    assert not any(row['evidence'] == 'native-execution-claimed' for row in after['rx_audit'])
