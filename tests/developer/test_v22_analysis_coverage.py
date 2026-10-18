"""Coverage rejection preserves raw history without inventing omitted facts."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.prices import LimitPolicy
from server.reference_data import ReferenceSnapshot


OWNER = 'synthetic-coverage-owner'
DAY = '2026-10-17'


def desk(tmp_path):
    flows = SimpleNamespace(analyze=lambda context: None, decide=lambda context: None)
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'coverage.db'),
        enable_background=False, auth_config=AuthConfig(clock=lambda: 1792202400.0), chatflows=flows)
    store = app.state.repository
    group = store.create_group(OWNER, bond_code='SYN-A', our_side='BUY', target_volume='1000',
        trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'))
    mid, thread = store.add_member(OWNER, group, account_uid='synthetic-account', peer_uid='synthetic-peer')
    store.set_sending(OWNER, False)
    def ingest(seq, text, **kw):
        return store.ingest(OWNER, thread, event_id='synthetic-' + str(seq), source_seq=seq,
                            trade_day=DAY, text=text, **kw)
    def analyze(observations):
        flows.analyze = lambda snap: {'schema_version': '2.2', 'scopes': {
            message['message_id']: [mid] for message in snap['messages']},
            'observations': deepcopy(observations), 'unresolved': {}}
        return app.state.negotiation_engine.analyze_thread(OWNER, thread, force=True)
    def quote(ref, **changes):
        return {'member_id': mid, 'side': 'ofr', 'changes': changes,
                'evidence': {field: [ref] for field in changes}}
    return app, mid, ingest, analyze, quote


def test_evidence_for_earlier_message_does_not_cover_a_later_explicit_quote(tmp_path):
    app, mid, ingest, analyze, quote = desk(tmp_path)
    first = ingest(1, 'SYN-A 3.10出500万')
    second = ingest(2, 'SYN-A 3.05出800万')
    saved = analyze([quote(first, price={'kind': 'YIELD', 'value': '3.10'})])
    assert saved['facts_committed'] and saved['accepted']
    assert any(row['member_id'] == mid for row in saved['rejected'])
    assert app.state.repository.quote(OWNER, mid, 'ofr').price.value == '3.10'
    with app.state.repository.db.transaction(write=False) as tx:
        assert tx.execute('SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?',
                          (second, mid)).fetchone()['relation'] == 'INVALID'


@pytest.mark.parametrize('metadata', [{'historical': True}, {'direction': 'out', 'native_id': 'synthetic-native'}])
def test_history_or_our_native_quote_does_not_require_new_counterparty_facts(tmp_path, metadata):
    app, mid, ingest, analyze, _ = desk(tmp_path)
    ingest(1, 'SYN-A 3.10出500万', **metadata)
    result = analyze([])
    assert result['facts_committed'] and not result['rejected']
    assert app.state.repository.quote(OWNER, mid, 'ofr').price is None


def test_unique_reference_alias_has_the_same_coverage_gate_as_the_bond_code(tmp_path):
    app, mid, ingest, analyze, _ = desk(tmp_path)
    app.state.inquiry.reference_provider = SimpleNamespace(snapshot=lambda: ReferenceSnapshot(
        'synthetic-aliases', DAY, bonds=[{'code': 'SYN-A', 'aliases': ['合成甲券']}]))
    ingest(1, '合成甲券 3.10出500万')
    result = analyze([])
    assert result['facts_committed']
    assert any(row['member_id'] == mid and row['side'] == 'ofr' for row in result['rejected'])


def test_current_volume_observation_covers_unchanged_explicit_price(tmp_path):
    app, mid, ingest, analyze, quote = desk(tmp_path)
    old = ingest(1, 'SYN-A 3.10出500万')
    assert not analyze([quote(old, price={'kind': 'YIELD', 'value': '3.10'}, volume_wan='500')])['rejected']
    changed = ingest(2, 'SYN-A 3.10出800万')
    result = analyze([quote(changed, volume_wan='800')])
    assert not result['rejected']
    assert app.state.repository.quote(OWNER, mid, 'ofr').volume_wan == '800'


def test_later_valid_quote_clears_the_coverage_invalid_relation(tmp_path):
    app, mid, ingest, analyze, quote = desk(tmp_path)
    omitted = ingest(1, 'SYN-A 3.10出500万')
    rejected = analyze([])
    assert rejected['rejected']
    fresh = ingest(2, 'SYN-A 3.15出800万')
    accepted = analyze([quote(fresh, price={'kind': 'YIELD', 'value': '3.15'}, volume_wan='800')])
    assert accepted['facts_committed'] and not accepted['rejected']
    with app.state.repository.db.transaction(write=False) as tx:
        assert tx.execute('SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?',
                          (omitted, mid)).fetchone()['relation'] == 'MENTION'


def test_direct_economic_fragment_with_a_trailing_question_still_requires_classification(tmp_path):
    _, mid, ingest, analyze, _ = desk(tmp_path)
    ingest(1, 'SYN-A 3.05出800万，能做吗？')
    result = analyze([])
    assert result['facts_committed']
    assert any(row['member_id'] == mid and row['side'] == 'ofr' for row in result['rejected'])


def test_later_current_observation_covers_superseded_quotes_in_the_same_batch(tmp_path):
    app, mid, ingest, analyze, quote = desk(tmp_path)
    ingest(1, 'SYN-A 3.10出500万')
    latest = ingest(2, 'SYN-A 3.05出')
    saved = analyze([quote(latest, price={'kind': 'YIELD', 'value': '3.05'})])
    assert saved['facts_committed'] and not saved['rejected']
    assert app.state.repository.quote(OWNER, mid, 'ofr').price.value == '3.05'


def test_omitted_quote_still_permits_a_non_economic_clarification(tmp_path):
    app, mid, ingest, analyze, _ = desk(tmp_path)
    ingest(1, 'SYN-A 3.05出800万')
    assert analyze([])['rejected']
    store = app.state.repository
    store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
    store.set_sending(OWNER, True)
    with store.write(OWNER) as tx:
        tx.execute("UPDATE xj2_group SET send_allowed=1,plan_json=? WHERE owner_id=?",
                   ('{"volume_wan":"1000"}', OWNER))
        tx.execute('UPDATE xj2_peer SET send_allowed=1 WHERE member_id=?', (mid,))
    app.state.negotiation_engine.client.decide = lambda context: {
        'schema_version': '2.2', 'action': 'SEND',
        'proposal': {'kind': 'CLARIFY', 'text': '请确认这只券当前的报价和可出数量。'}}
    saved = app.state.negotiation_engine.decide_member(OWNER, mid)
    assert saved['state'] == 'QUEUED'
    with store.db.transaction(write=False) as tx:
        row = tx.execute('SELECT body_json FROM xj2_command WHERE command_id=?',
                         (saved['command_id'],)).fetchone()
        assert json.loads(row['body_json'])['kind'] == 'CLARIFY'
    assert store.quote(OWNER, mid, 'ofr').price is None
