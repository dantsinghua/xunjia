"""Direct submission recovery through real temporary batch/command transactions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

import server.negotiation.service as module
from server.negotiation.application_store import ApplicationStore
from server.negotiation.prices import InvalidFact, Valuation

OWNER = 'synthetic-recovery-owner'


def item(code='SYN-A', *, basis='bp'):
    return {'code': code, 'our_side': 'BUY', 'peer_ids': ['synthetic-peer'],
            'volume_wan': '1000', 'opening_yield_pct': '1.1', 'limit_yield_pct': '2.0',
            'step_bp': '2', 'pricing_basis': basis}


@pytest.fixture
def desk(tmp_path):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'recovery.db'), initialize=True)
    store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
    store.save_preferences(OWNER, {'floors': {code: {'kind': 'bp', 'value': 5} for code in ('SYN-A', 'SYN-B')}})
    state = {'yield': '2.4', 'calls': 0, 'fail': False}
    def value(code):
        state['calls'] += 1
        if state['fail']:
            raise OSError('synthetic source offline')
        return Valuation(code, '2026-10-22', state['yield'], 'snapshot-' + state['yield'])
    return SimpleNamespace(store=store, service=module.Inquiry(store, valuation_source=value), state=state)


def counts(d):
    with d.store.db.transaction(write=False) as tx:
        return {table: tx.execute('SELECT COUNT(*) AS n FROM ' + table).fetchone()['n']
                for table in ('xj2_batch', 'xj2_group', 'xj2_command')}


def test_direct_prepares_and_commits_one_valuation_without_second_read(desk):
    result = desk.service.send_direct(OWNER, 'one-snapshot', [item()])
    assert result['commands'][0]['status'] == 'queued'
    assert desk.state['calls'] == 1
    assert counts(desk) == {'xj2_batch': 1, 'xj2_group': 1, 'xj2_command': 1}
    with desk.store.db.transaction(write=False) as tx:
        plan = tx.execute('SELECT plan_json FROM xj2_group').fetchone()['plan_json']
    assert '1.1' in plan, 'Direct submit must preserve the explicit initial yield'


def test_multibond_direct_uses_one_formal_reference_snapshot(desk):
    calls = []
    def snapshot():
        calls.append(1)
        return SimpleNamespace(snapshot_id='formal-once', as_of='2026-10-22', bonds=[
            {'code': code, 'valuation_yield_pct': '2.4'} for code in ('SYN-A', 'SYN-B')])
    desk.service.valuation_source = None
    desk.service.reference_provider = SimpleNamespace(snapshot=snapshot)
    result = desk.service.send_direct(OWNER, 'multi', [item(), item('SYN-B')])
    assert len(result['group_ids']) == 2 and len(result['commands']) == 1
    assert calls == [1]


def test_uncommitted_historical_direct_preparation_can_recover_current_valuation(desk):
    request = 'old-uncommitted'
    prepared = desk.service.preview(OWNER, 'direct:' + module.digest(request), [item()])
    desk.state.update({'yield': '2.5', 'calls': 0})
    result = desk.service.send_direct(OWNER, request, [item()])
    assert result['batch_id'] == prepared['batch_id'] and desk.state['calls'] == 1
    with desk.store.db.transaction(write=False) as tx:
        group = tx.execute('SELECT valuation_json FROM xj2_group').fetchone()['valuation_json']
    assert '2.5' in group


def test_failed_direct_commit_leaves_no_prepared_batch_and_same_request_can_recover(desk, monkeypatch):
    original = desk.service.commands_core._insert
    def fail(*args, **kwargs):
        raise InvalidFact('synthetic validation failure before command persistence')
    monkeypatch.setattr(desk.service.commands_core, '_insert', fail)
    with pytest.raises(ValueError):
        desk.service.send_direct(OWNER, 'retry-after-rejection', [item()])
    assert counts(desk) == {'xj2_batch': 0, 'xj2_group': 0, 'xj2_command': 0}
    monkeypatch.setattr(desk.service.commands_core, '_insert', original)
    desk.state['yield'] = '2.6'
    assert desk.service.send_direct(OWNER, 'retry-after-rejection', [item()])['commands']


def test_confirmed_confirm_retry_returns_before_guard_and_live_valuation(desk):
    preview = desk.service.preview(OWNER, 'preview', [item()])
    first = desk.service.confirm(OWNER, preview['batch_id'], 1, 'confirm')
    calls = desk.state['calls']
    desk.state['fail'] = True
    desk.service.submit_guard = lambda owner: pytest.fail('Committed retry cannot check live submission conditions')
    assert desk.service.confirm(OWNER, preview['batch_id'], 1, 'confirm') == first
    assert desk.state['calls'] == calls


def test_explicit_preview_confirm_still_rejects_changed_valuation(desk):
    preview = desk.service.preview(OWNER, 'explicit-preview', [item()])
    desk.state['yield'] = '2.6'
    with pytest.raises(InvalidFact, match='valuation changed'):
        desk.service.confirm(OWNER, preview['batch_id'], 1, 'confirm')
    assert counts(desk)['xj2_command'] == 0


@pytest.mark.parametrize('operation', ['confirm', 'send_direct', 'manual_send'])
def test_new_command_guard_rejects_without_batch_command_or_pause_side_effects(desk, operation):
    preview = desk.service.preview(OWNER, 'guarded-preview', [item()]) if operation == 'confirm' else None
    if operation == 'manual_send':
        desk.service.send_direct(OWNER, 'seed-manual-conversation', [item(basis='absolute')])
    before = counts(desk)
    with desk.store.db.transaction(write=False) as tx:
        states = {table: [dict(row) for row in tx.execute('SELECT * FROM ' + table)]
                  for table in ('xj2_group', 'xj2_peer', 'xj2_command')}
    calls = []
    def guard(owner):
        calls.append(owner)
        raise ValueError('synthetic dispatcher disabled')
    desk.service.submit_guard = guard
    with pytest.raises(ValueError) as caught:
        if operation == 'confirm':
            desk.service.confirm(OWNER, preview['batch_id'], 1, 'guarded-confirm')
        elif operation == 'send_direct':
            desk.service.send_direct(OWNER, 'guarded-direct', [item()])
        else:
            desk.service.manual_send(OWNER, 'synthetic-peer', 'synthetic manual text', 'guarded-manual')
    assert calls == [OWNER] and counts(desk) == before
    with desk.store.db.transaction(write=False) as tx:
        assert {table: [dict(row) for row in tx.execute('SELECT * FROM ' + table)] for table in states} == states
    assert isinstance(caught.value, module.SubmitRejected)
    assert caught.value.submitted is False and caught.value.code and caught.value.hint


def test_committed_direct_retry_checks_payload_before_guard_and_never_requeues(desk):
    first = desk.service.send_direct(OWNER, 'committed', [item()])
    desk.service.submit_guard = lambda owner: pytest.fail('No guard on committed read')
    desk.state['fail'] = True
    assert desk.service.send_direct(OWNER, 'committed', [item()]) == first
    changed = deepcopy(item()); changed['opening_yield_pct'] = '9.1'
    with pytest.raises(ValueError):
        desk.service.send_direct(OWNER, 'committed', [changed])
    assert counts(desk)['xj2_command'] == 1


def test_manual_committed_retry_bypasses_guard_and_conflicting_text_is_rejected(desk):
    desk.service.send_direct(OWNER, 'manual-conversation', [item(basis='absolute')])
    first = desk.service.manual_send(OWNER, 'synthetic-peer', 'synthetic manual', 'manual-id')
    desk.service.submit_guard = lambda owner: pytest.fail('No guard on existing manual command')
    assert desk.service.manual_send(OWNER, 'synthetic-peer', 'synthetic manual', 'manual-id')['cmd_id'] == first['cmd_id']
    with pytest.raises(ValueError):
        desk.service.manual_send(OWNER, 'synthetic-peer', 'different text', 'manual-id')
    assert counts(desk)['xj2_command'] == 2


def test_concurrent_duplicate_direct_calls_create_one_batch_and_run_guard_once(desk):
    calls = []
    desk.service.submit_guard = lambda owner: calls.append(owner)
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda _: desk.service.send_direct(OWNER, 'parallel', [item()]), range(4)))
    assert len({x['batch_id'] for x in values}) == 1
    assert calls == [OWNER] and desk.state['calls'] == 1
    assert counts(desk) == {'xj2_batch': 1, 'xj2_group': 1, 'xj2_command': 1}


def test_guard_and_valuation_do_not_hold_a_write_transaction(desk):
    source = desk.service.valuation_source
    def separate_write():
        with desk.store.write('synthetic-independent-owner') as tx:
            tx.execute('SELECT owner_id FROM xj2_owner').fetchall()
    def guard(owner):
        separate_write()
    def value(code):
        separate_write()
        return source(code)
    desk.service.submit_guard = guard
    desk.service.valuation_source = value
    assert desk.service.send_direct(OWNER, 'outside-transaction', [item()])['commands']


def test_database_outcome_error_is_not_claimed_as_definitive_unsubmitted(desk, monkeypatch):
    original = desk.store.write
    @contextmanager
    def unknown(owner):
        with original(owner) as tx:
            yield tx
            raise OSError('synthetic commit outcome unknown')
    monkeypatch.setattr(desk.store, 'write', unknown)
    with pytest.raises(OSError):
        desk.service.send_direct(OWNER, 'unknown-commit', [item(basis='absolute')])


def test_submit_rejected_exposes_safe_machine_readable_metadata():
    error = module.SubmitRejected('SUBMIT_UNAVAILABLE', 'synthetic unavailable', field='pricing_basis', bond_code='SYN-A')
    assert isinstance(error, ValueError) and error.submitted is False
    assert (error.code, error.hint, error.field, error.bond_code) == ('SUBMIT_UNAVAILABLE', 'synthetic unavailable', 'pricing_basis', 'SYN-A')


def test_external_admission_error_preserves_specific_machine_readable_reason(desk):
    error = ValueError('synthetic channel unavailable')
    error.code = 'DISPATCHER_STOPPED'
    error.hint = 'synthetic channel unavailable'
    error.field = 'channel'
    error.bond_code = 'SYN-A'
    def guard(owner):
        raise error
    desk.service.submit_guard = guard
    with pytest.raises(module.SubmitRejected) as caught:
        desk.service.send_direct(OWNER, 'structured-guard', [item()])
    assert caught.value.code == 'DISPATCHER_STOPPED'
    assert caught.value.field == 'channel' and caught.value.bond_code == 'SYN-A'
    assert counts(desk)['xj2_command'] == 0


@pytest.mark.parametrize('operation', ['send_direct', 'manual_send'])
def test_guard_changes_binding_cannot_retarget_a_new_submission(desk, operation):
    if operation == 'manual_send':
        desk.service.send_direct(OWNER, 'seed-before-binding-race', [item(basis='absolute')])
    before = counts(desk)
    def guard(owner):
        with desk.store.write(owner) as tx:
            tx.execute('UPDATE xj2_binding SET version=version+1 WHERE owner_id=?', (owner,))
    desk.service.submit_guard = guard
    with pytest.raises(module.SubmitRejected, match='账号'):
        if operation == 'manual_send':
            desk.service.manual_send(OWNER, 'synthetic-peer', 'synthetic manual', 'binding-race')
        else:
            desk.service.send_direct(OWNER, 'binding-race', [item()])
    assert counts(desk) == before


def test_independent_service_instances_still_commit_one_duplicate_batch(desk):
    second = module.Inquiry(desk.store, valuation_source=desk.service.valuation_source)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(service.send_direct, OWNER, 'cross-instance', [item()])
                   for service in (desk.service, second)]
        values = [future.result(timeout=10) for future in futures]
    assert values[0] == values[1]
    assert counts(desk) == {'xj2_batch': 1, 'xj2_group': 1, 'xj2_command': 1}


@pytest.mark.parametrize('change', ['binding', 'preferences'])
def test_historical_uncommitted_request_cannot_inherit_changed_binding_or_price_authority(desk, change):
    request = 'historical-fenced'
    desk.service.preview(OWNER, 'direct:' + module.digest(request), [item()])
    if change == 'binding':
        with desk.store.write(OWNER) as tx:
            tx.execute('UPDATE xj2_binding SET version=version+1 WHERE owner_id=?', (OWNER,))
    else:
        desk.store.save_preferences(OWNER, {'floors': {'SYN-A': {'kind': 'bp', 'value': 20}}})
    with pytest.raises(module.SubmitRejected):
        desk.service.send_direct(OWNER, request, [item()])
    assert counts(desk) == {'xj2_batch': 1, 'xj2_group': 0, 'xj2_command': 0}
