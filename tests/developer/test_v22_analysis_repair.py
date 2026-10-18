"""Evidence-only repair keeps the first validated facts and the original clock."""
from copy import deepcopy
import json
from types import SimpleNamespace

from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.engine import Engine
from server.negotiation.prices import LimitPolicy
from server.reference_data import ReferenceSnapshot
import server.negotiation.engine as engine_module


OWNER = 'synthetic-repair-owner'
DAY = '2026-10-17'


def desk(tmp_path):
    clock = [1792202400.0]
    flows = SimpleNamespace(analyze=lambda context: None, decide=lambda context: None)
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'repair.db'),
        enable_background=False, auth_config=AuthConfig(clock=lambda: clock[0]), chatflows=flows)
    store = app.state.repository
    members = {}
    for code in ('SYN-A', 'SYN-B'):
        gid = store.create_group(OWNER, bond_code=code, our_side='BUY', target_volume='1000',
            trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'))
        mid, thread = store.add_member(OWNER, gid, account_uid='synthetic-account', peer_uid='synthetic-peer')
        members[code] = mid
    store.set_sending(OWNER, False)
    def ingest(seq, text, **kw):
        return store.ingest(OWNER, thread, event_id='synthetic-' + str(seq), source_seq=seq,
            trade_day=DAY, text=text, **kw)
    anchor = ingest(1, 'SYN-B 3.20收500万', direction='out', native_id='synthetic-native-anchor')
    cash = ingest(2, 'SYN-A 净价97出500万，确认2026-10-17结算')
    quoted = ingest(3, 'SYN-B 按你刚才那个3.20收的价，我出500万，最终了')
    good = {'member_id': members['SYN-A'], 'side': 'ofr',
        'changes': {'price': {'kind': 'NET_PRICE', 'value': '97', 'settlement_date': DAY}},
        'evidence': {'price': [cash]}}
    bad = {'member_id': members['SYN-B'], 'side': 'ofr',
        'changes': {'price': {'kind': 'YIELD', 'value': '3.20'}},
        'evidence': {'price': [quoted]}}
    first = {'schema_version': '2.2', 'scopes': {anchor: [members['SYN-B']],
        cash: [members['SYN-A']], quoted: [members['SYN-B']]},
        'observations': [good, bad], 'unresolved': {}, 'corrections': []}
    fixed = deepcopy(first)
    fixed['observations'][1]['evidence']['price'].append(anchor)
    return app, flows, clock, members, thread, first, fixed


def test_feedback_contains_only_original_model_contract_without_private_snapshot(tmp_path):
    app, flows, _, members, thread, first, fixed = desk(tmp_path)
    first['_internal_marker'] = 'synthetic-private-marker'
    calls = []
    def analyze(context):
        calls.append(deepcopy(context))
        return deepcopy(fixed if context.get('validation_feedback') else first)
    flows.analyze = analyze
    saved = app.state.negotiation_engine.analyze_thread(OWNER, thread, force=True)
    assert len(calls) == 2
    feedback = calls[1]['validation_feedback']
    assert set(feedback['original_response']) == {
        'schema_version', 'scopes', 'observations', 'unresolved', 'corrections'}
    assert '_internal_marker' not in json.dumps(feedback)
    assert 'step_bp' not in json.dumps(feedback) and 'limit' not in json.dumps(feedback)
    assert feedback['original_response']['observations'][1] == first['observations'][1]
    assert saved['repair']['attempted'] and saved['repair']['succeeded']
    assert saved['repair']['initial_errors'][0]['reason'] == 'Price is not supported by its classified raw evidence'
    assert app.state.repository.quote(OWNER, members['SYN-B'], 'ofr').price.value == '3.20'


def test_first_normalization_finishing_after_deadline_has_no_committable_baseline(tmp_path, monkeypatch):
    app, flows, clock, members, thread, first, _ = desk(tmp_path)
    engine = Engine(app.state.inquiry, flows, app.state.bond_tools, stage_budget_seconds=10)
    flows.analyze = lambda context: deepcopy(first)
    monkeypatch.setattr(engine_module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    normalize = engine._normalize
    def slow_normalize(*args):
        value = normalize(*args)
        clock[0] += 11
        return value
    monkeypatch.setattr(engine, '_normalize', slow_normalize)
    saved = engine.analyze_thread(OWNER, thread, force=True)
    assert saved['facts_committed'] is False
    assert app.state.repository.quote(OWNER, members['SYN-A'], 'ofr').revision == 0
    assert app.state.repository.quote(OWNER, members['SYN-B'], 'ofr').revision == 0


def test_cash_sibling_keeps_first_verified_calculation_during_other_slot_repair(tmp_path, monkeypatch):
    app, flows, _, members, thread, first, fixed = desk(tmp_path)
    terms = {'terms_snapshot_id': 'synthetic-terms', 'quality': 'verified',
        'source_ref': 'synthetic-source', 'bond_type': 'zero_coupon',
        'convention': 'ACT/365F-compounded-v1', 'compounding_frequency': 1,
        'yield_basis': 'maturity', 'settlement_cashflow': 'exclude',
        'cashflows': [{'payment_date': '2027-10-17', 'amount_per_100': '100'}],
        'accrued_interest_per_100': '0', 'accrued_interest_settlement_date': DAY}
    class Source:
        def snapshot(self):
            return ReferenceSnapshot('synthetic-snapshot', DAY, sources=[{'id': 'synthetic-source'}],
                bonds=[{'bond_id': 'SYN-A', 'code': 'SYN-A'}], terms={'SYN-A': terms})
    from server.bond_tools import BondTools
    source = Source()
    app.state.inquiry.reference_provider = source
    engine = Engine(app.state.inquiry, flows, BondTools(provider=source))
    calculations = []
    convert = engine._cash_conversion
    def observe_conversion(snapshot, observation, member):
        value = convert(snapshot, observation, member)
        if member['member_id'] == members['SYN-A']:
            calculations.append(deepcopy(value))
        return value
    monkeypatch.setattr(engine, '_cash_conversion', observe_conversion)
    flows.analyze = lambda context: deepcopy(fixed if context.get('validation_feedback') else first)
    saved = engine.analyze_thread(OWNER, thread, force=True)
    assert saved['facts_committed'] and not saved['rejected']
    assert len(calculations) == 1, 'A repair must not recalculate a previously valid cash sibling'
    quote = app.state.repository.quote(OWNER, members['SYN-A'], 'ofr')
    assert quote.price.computed_yield == '3.09'
    assert quote.price.calculation_ref == calculations[0]['calculation_ref']
    with app.state.repository.db.transaction(write=False) as tx:
        rows = tx.execute("SELECT data_json FROM xj2_audit WHERE kind='cash_conversion_evidence'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0]['data_json'])['calculations'] == calculations
