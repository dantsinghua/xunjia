"""Independent production gates for one bounded, evidence-only A repair.

Only model responses, time and native transport are synthetic. Normalization,
source validation, SQL transactions, leases and controls remain production.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Event
from types import SimpleNamespace

import pytest

import server.negotiation.engine as engine_module
from server.negotiation.engine import Engine
from server.reference_data import ReferenceSnapshot
from test_production_flow import OWNER, desk, raw
from test_acceptance_contract import current, observation, setup_groups
from test_private_history_requests import scope_only
from test_persistent_retry_http import latest_row


def repair_case(desk, *, siblings=False, retracted=False, sibling_text=None):
    app, client, flows, _, _ = desk
    groups = setup_groups(desk, two_bonds=siblings, stopped=False)
    peer = groups['SYN-A']['peers'][0]
    sibling = groups['SYN-B']['peers'][0] if siblings else None
    anchor = next(m for m in app.state.repository.context(OWNER, peer['member_id']) if m['direction'] == 'out')
    assert anchor['native_id']
    anchor['fragment'] = next(line for line in anchor['text'].splitlines() if 'SYN-A' in line)
    assert client.post('/api/bot/toggle', json={'status': '停止'}).status_code == 200
    warm = raw(app, peer['thread_id'], 'SYN-A 稍后继续沟通', seq=1)
    seq = 2
    if retracted:
        raw(app, peer['thread_id'], 'SYN-A 此前我方报价已撤回', seq=seq,
            kind='retract', direction='out', native_id='synthetic-retracted-native',
            target_event_id=anchor['event_id'])
        seq += 1
    flows.analyze = scope_only
    assert app.state.negotiation_engine.analyze_thread(OWNER, peer['thread_id'])['facts_committed']
    text = 'SYN-A 按你刚才那个3.20收的价，我出500万，不能再改善了'
    if sibling:
        text += '；' + (sibling_text or 'SYN-B 3.10出700万，还可以谈')
    incoming = raw(app, peer['thread_id'], text, seq=seq)
    return SimpleNamespace(app=app, flows=flows, peer=peer, sibling=sibling,
                           anchor=anchor, warm=warm, incoming=incoming, next_seq=seq+1)


def first_result(case, context):
    message = next(m for m in context['messages'] if m['message_id'] == case.incoming)
    bad = observation(case.peer, message, price='3.20', volume='500', finality='FINAL',
                      segment=message['text'].split('；')[0], assertion='QUOTE')
    # Both original references must survive a repair; removing the earlier
    # nonnumeric inbound anchor would otherwise still pass source validation.
    bad['evidence']['price'].append(case.warm)
    observations = [bad]
    if case.sibling:
        observations.append(observation(case.sibling, message, price='3.10', volume='700',
            segment=message['text'].split('；')[1], assertion='QUOTE'))
    return {'schema_version': '2.2', 'scopes': {case.incoming: [p['member_id'] for p in
            (case.peer, case.sibling) if p]}, 'unresolved': {}, 'corrections': [],
            'observations': observations}


def repaired(case, original):
    value = deepcopy(original)
    slot = value['observations'][0]
    slot['evidence']['price'].append(case.anchor['message_id'])
    slot['segments'].append({'message_id': case.anchor['message_id'], 'text': case.anchor['fragment']})
    return value


def install(case, transform=None, *, first_hook=None, repair_hook=None):
    calls, originals = [], []
    def analyze(context):
        calls.append(deepcopy(context))
        assert len(calls) <= 2, 'Only one extra final A response may be requested'
        if len(calls) == 1:
            assert not context.get('validation_feedback')
            result = first_result(case, context)
            originals.append(deepcopy(result))
            if first_hook:
                first_hook(context, result)
                originals[0] = deepcopy(result)
            return result
        assert context.get('validation_feedback'), 'A repair must carry explicit validation feedback'
        assert context['messages'] == calls[0]['messages']
        if repair_hook:
            repair_hook(context)
        result = repaired(case, originals[0])
        return transform(result) if transform else result
    case.flows.analyze = analyze
    return calls, originals


def run(case, engine=None):
    return (engine or case.app.state.negotiation_engine).analyze_thread(OWNER, case.peer['thread_id'])


def configured(case, clock, monkeypatch, seconds=360):
    monkeypatch.setattr(engine_module, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    old = case.app.state.negotiation_engine
    return Engine(case.app.state.inquiry, case.flows, case.app.state.bond_tools,
                  aggregation=old.aggregation, stage_budget_seconds=seconds)


def assert_baseline(case, result):
    assert result['facts_committed'] and result['rejected'], result
    assert current(case.app, case.peer).revision == 0
    if case.sibling:
        good = current(case.app, case.sibling)
        assert good.revision == 1 and good.price.value == '3.10' and good.volume_wan == '700'
        assert good.evidence['price'] == [case.incoming]


def test_missing_loaded_native_price_anchor_repairs_once_before_one_fact_commit(desk):
    case = repair_case(desk, siblings=True)
    calls, originals = install(case)
    result = run(case)
    assert len(calls) == 2, 'The first price-evidence rejection must receive one bounded repair opportunity'
    assert result['facts_committed'] and not result['rejected'], result
    quote = current(case.app, case.peer)
    assert quote.revision == 1 and quote.price.value == '3.20' and quote.volume_wan == '500'
    assert quote.finality == 'FINAL'
    assert set(quote.evidence['price']) == {case.incoming, case.warm, case.anchor['message_id']}
    good = current(case.app, case.sibling)
    assert good.revision == 1 and good.price.value == '3.10' and good.volume_wan == '700'
    assert good.evidence['price'] == originals[0]['observations'][1]['evidence']['price']
    assert run(case) is None and len(calls) == 2
    assert len(desk[3].sent) == 1, 'Analysis repair cannot dispatch while sending is paused'


@pytest.mark.parametrize('change', ['scopes', 'unresolved', 'corrections', 'remove_slot', 'reorder',
    'member', 'side', 'price', 'volume', 'interpretation', 'good_slot', 'drop_original_evidence'])
def test_repair_cannot_change_frozen_facts_envelope_identity_or_original_references(desk, change):
    case = repair_case(desk, siblings=True)
    def mutate(value):
        slot = value['observations'][0]
        if change == 'scopes': value['scopes'][case.incoming] = [case.peer['member_id']]
        elif change == 'unresolved': value['unresolved'] = {case.incoming: [case.sibling['member_id']]}
        elif change == 'corrections': value['corrections'] = [{'message_id': case.warm,
            'member_ids': [case.peer['member_id']], 'evidence': [case.incoming]}]
        elif change == 'remove_slot': value['observations'].pop()
        elif change == 'reorder': value['observations'].reverse()
        elif change == 'member': slot['member_id'] = case.sibling['member_id']
        elif change == 'side': slot['side'] = 'bid'
        elif change == 'price': slot['changes']['price']['value'] = '3.19'
        elif change == 'volume': slot['changes']['volume_wan'] = '600'
        elif change == 'interpretation': slot['interpretation']['reason'] += ' 修复另作解释'
        elif change == 'good_slot': value['observations'][1]['interpretation']['reason'] += ' 修改合法兄弟'
        else: slot['evidence']['price'].remove(case.warm)
        return value
    calls, _ = install(case, mutate)
    result = run(case)
    assert len(calls) == 2
    assert_baseline(case, result)


@pytest.mark.parametrize('failure', ['invalid_again', 'exception', 'tool', 'history', 'forged_anchor'])
def test_failed_repair_keeps_good_sibling_and_never_starts_another_model_or_tool_round(desk, monkeypatch, failure):
    case = repair_case(desk, siblings=True)
    invoked = []
    original_invoke = case.app.state.bond_tools.invoke
    def counted(*args):
        invoked.append(args)
        return original_invoke(*args)
    monkeypatch.setattr(case.app.state.bond_tools, 'invoke', counted)
    calls = []
    def analyze(context):
        calls.append(deepcopy(context))
        assert len(calls) <= 2
        if len(calls) == 1:
            return first_result(case, context)
        assert context.get('validation_feedback')
        if failure == 'exception': raise TimeoutError('synthetic repair timeout before stage deadline')
        if failure == 'tool': return {'schema_version': '2.2', 'tool_requests': [{'name': 'bond_info',
            'arguments': {'schema_version': '1', 'mode': 'get', 'bond_id': 'SYN-A'}}]}
        if failure == 'history': return {'schema_version': '2.2', 'history_requests': [
            {'member_id': case.peer['member_id'], 'message_ids': [case.anchor['message_id']]}]}
        value = first_result(case, context)
        if failure == 'forged_anchor':
            value['observations'][0]['evidence']['price'].append('unloaded-forged-anchor')
            value['observations'][0]['segments'].append({'message_id': 'unloaded-forged-anchor', 'text': 'SYN-A 3.20%收'})
        return value
    case.flows.analyze = analyze
    result = run(case)
    assert len(calls) == 2 and not invoked
    assert_baseline(case, result)


@pytest.mark.parametrize('error', ['quantity', 'direction', 'unlocatable'])
def test_only_locatable_price_source_failure_can_trigger_repair(desk, error):
    case = repair_case(desk)
    calls = []
    def analyze(context):
        calls.append(context)
        value = repaired(case, first_result(case, context))
        slot = value['observations'][0]
        if error == 'quantity': slot['changes']['volume_wan'] = '999'
        elif error == 'direction':
            slot['side'] = 'bid'
            slot['segments'] = [{'message_id': case.incoming, 'text': '我出500万'}]
        else: value['observations'][0] = {'validation_error': 'Invalid quote ownership or intent'}
        return value
    case.flows.analyze = analyze
    result = run(case)
    assert len(calls) == 1 and result['rejected']
    assert current(case.app, case.peer).revision == 0


def test_repaired_price_still_cannot_use_an_already_retracted_native_anchor(desk):
    case = repair_case(desk, retracted=True)
    calls, _ = install(case)
    result = run(case)
    assert len(calls) == 2
    assert_baseline(case, result)


def test_repair_uses_remaining_original_deadline_and_does_not_extend_lease(desk, monkeypatch):
    case = repair_case(desk)
    clock = desk[4]
    engine = configured(case, clock, monkeypatch)
    initial_lease = []
    def first_hook(context, result):
        initial_lease.append(latest_row(case.app.state.repository, 'xj2_analysis')['lease_until'])
        clock.value += 200
    def repair_hook(context):
        assert 0 < context['_stage_budget_seconds'] <= 160
        assert latest_row(case.app.state.repository, 'xj2_analysis')['lease_until'] == initial_lease[0]
    calls, _ = install(case, first_hook=first_hook, repair_hook=repair_hook)
    result = run(case, engine)
    assert len(calls) == 2 and result['facts_committed'] and not result['rejected']
    assert latest_row(case.app.state.repository, 'xj2_analysis')['lease_until'] == initial_lease[0]


def test_late_repair_discards_new_candidate_but_commits_early_valid_baseline_under_same_lease(desk, monkeypatch):
    case = repair_case(desk, siblings=True)
    clock = desk[4]
    engine = configured(case, clock, monkeypatch)
    def repair_hook(context): clock.value += 361
    calls, _ = install(case, repair_hook=repair_hook)
    result = run(case, engine)
    assert len(calls) == 2
    assert_baseline(case, result)
    assert latest_row(case.app.state.repository, 'xj2_analysis')['lease_until'] > clock.value


@pytest.mark.parametrize('late_at', ['first_model', 'first_normalize'])
def test_no_repair_or_fact_commit_when_first_validated_baseline_itself_is_late(desk, monkeypatch, late_at):
    case = repair_case(desk, siblings=True)
    clock = desk[4]
    engine = configured(case, clock, monkeypatch)
    if late_at == 'first_normalize':
        original = engine._normalize
        def slow_normalize(*args):
            result = original(*args)
            clock.value += 361
            return result
        monkeypatch.setattr(engine, '_normalize', slow_normalize)
    def first_hook(context, result):
        if late_at == 'first_model': clock.value += 361
    calls, _ = install(case, first_hook=first_hook)
    result = run(case, engine)
    assert len(calls) == 1 and result['facts_committed'] is False
    assert current(case.app, case.peer).revision == current(case.app, case.sibling).revision == 0
    assert latest_row(case.app.state.repository, 'xj2_analysis')['state'] == 'RETRY'


def test_replaced_lease_fences_both_repair_and_early_baseline(desk):
    case = repair_case(desk, siblings=True)
    clock = desk[4]
    claimed = {}
    def repair_hook(context):
        old = latest_row(case.app.state.repository, 'xj2_analysis')
        clock.value = old['lease_until'] + .01
        newer = case.app.state.repository.claim_analysis(OWNER, case.peer['thread_id'], force=True, lease_seconds=180)
        assert newer['analysis_id'] == old['analysis_id'] and newer['lease_token'] != old['lease_token']
        claimed.update(newer)
    calls, _ = install(case, repair_hook=repair_hook)
    result = run(case)
    assert len(calls) == 2 and result['facts_committed'] is False
    row = latest_row(case.app.state.repository, 'xj2_analysis')
    assert row['state'] == 'RUNNING' and row['lease_token'] == claimed['lease_token'] and row['attempts'] == 2
    assert current(case.app, case.peer).revision == current(case.app, case.sibling).revision == 0


def test_new_inbound_during_repair_is_not_locked_or_absorbed_by_old_snapshot(desk):
    case = repair_case(desk)
    entered, release = Event(), Event()
    def repair_hook(context):
        entered.set()
        assert release.wait(4), 'Synthetic repair was not released'
    calls, _ = install(case, repair_hook=repair_hook)
    with ThreadPoolExecutor(max_workers=2) as pool:
        analysis = pool.submit(run, case)
        try:
            assert entered.wait(2), 'Expected bounded repair to reach the model'
            inbound = pool.submit(raw, case.app, case.peer['thread_id'], 'SYN-A 新价3.15出600万', case.next_seq)
            newer = inbound.result(timeout=2)
            assert newer not in {m['message_id'] for m in calls[-1]['messages']}
        finally:
            release.set()
        result = analysis.result(timeout=3)
    assert result['facts_committed'] and not result['rejected'] and len(calls) == 2
    assert current(case.app, case.peer).price.value == '3.20'
    thread = case.app.state.repository.raw_page(OWNER, case.peer['thread_id'])['thread']
    assert thread['ingest_revision'] > thread['analyzed_revision']
    assert len(desk[3].sent) == 1


@pytest.mark.parametrize('succeeds', [True, False])
def test_first_rejection_and_repair_or_fallback_remain_observable(desk, succeeds):
    case = repair_case(desk, siblings=True)
    def transform(value):
        if not succeeds:
            value['observations'][0]['interpretation']['reason'] += ' 不允许改动'
        return value
    calls, originals = install(case, transform)
    result = run(case)
    assert len(calls) == 2
    evidence = result['repair']
    assert evidence['mode'] == 'EVIDENCE_ONLY'
    assert evidence['attempted'] is True and evidence['succeeded'] is succeeds
    assert evidence['fallback'] is (not succeeds)
    assert evidence['initial_response'] == originals[0]
    assert evidence['initial_errors'] == [{'index': 0, 'member_id': case.peer['member_id'], 'side': 'ofr',
        'reason': 'Price is not supported by its classified raw evidence'}]
    assert evidence['targets'] == evidence['initial_errors']
    assert evidence['repair_response'] is not None
    assert evidence['initial_elapsed_ms'] >= 0 and evidence['repair_elapsed_ms'] >= 0
    if not succeeds:
        assert evidence['repair_error']
    job = latest_row(case.app.state.repository, 'xj2_analysis')
    with case.app.state.repository.db.transaction(write=False) as tx:
        audits = tx.execute("SELECT data_json FROM xj2_audit WHERE entity_id=? AND kind='analysis_evidence_repair'",
                            (job['analysis_id'],)).fetchall()
    assert len(audits) == 1 and json.loads(audits[0]['data_json']) == evidence


def test_valid_bp_sibling_keeps_first_verified_price_when_reference_changes_during_repair(desk):
    case = repair_case(desk, siblings=True, sibling_text='SYN-B 中债估值加5BP出700万，还可以谈')
    class References:
        value = '3.05'
        def snapshot(self):
            return ReferenceSnapshot('synthetic-repair-valuation', '2026-10-17',
                bonds=[{'bond_id': 'SYN-B', 'code': 'SYN-B', 'valuation_yield_pct': self.value,
                        'valuation_date': '2026-10-17'}])
        def latest_valuation_day(self, now):
            return '2026-10-17'
    reference = References()
    case.app.state.inquiry.reference_provider = reference
    case.app.state.inquiry.calendar = reference
    def first_hook(context, result):
        result['observations'][1]['changes']['price'] = {
            'kind': 'YIELD', 'value': '5', 'notation': 'CHINABOND_BP'}
    def repair_hook(context):
        reference.value = '3.25'
    calls, _ = install(case, first_hook=first_hook, repair_hook=repair_hook)
    result = run(case)
    assert len(calls) == 2 and result['facts_committed'] and not result['rejected']
    good = current(case.app, case.sibling)
    assert good.revision == 1 and good.price.value == '3.10' and good.volume_wan == '700'
    assert current(case.app, case.peer).price.value == '3.20'


def test_repair_keeps_all_six_completed_tool_results_without_resetting_stage_state(desk, monkeypatch):
    case = repair_case(desk)
    invoked, calls = [], []
    original_invoke = case.app.state.bond_tools.invoke
    def counted(*args):
        invoked.append(args)
        return original_invoke(*args)
    monkeypatch.setattr(case.app.state.bond_tools, 'invoke', counted)
    initial = []
    def analyze(context):
        calls.append(deepcopy(context))
        assert len(calls) <= 3, 'Six tool requests plus two final A responses are the full scenario'
        if len(calls) == 1:
            return {'schema_version': '2.2', 'tool_requests': [{'name': 'bond_info', 'arguments': {
                'schema_version': '1', 'mode': 'search', 'query': 'synthetic-'+str(index)}} for index in range(6)]}
        assert len(context['tool_results']) == 6
        if len(calls) == 2:
            initial.append(first_result(case, context))
            return deepcopy(initial[0])
        assert context.get('validation_feedback')
        assert context['tool_results'] == calls[1]['tool_results']
        return repaired(case, initial[0])
    case.flows.analyze = analyze
    result = run(case)
    assert len(calls) == 3 and len(invoked) == 6
    assert result['facts_committed'] and not result['rejected']
