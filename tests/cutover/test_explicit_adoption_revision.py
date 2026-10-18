"""A narrow adoption contradiction gate must preserve the actual clause owner."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import node_code, prompts
from dify.negotiation_v22.build import build
from server.negotiation.prices import InvalidFact
from server.negotiation.slots import collect_candidates
from server.negotiation.validation import validate_observation


def adoption(text, *, selected='3.10', side='ofr', fragment=None):
    own, peer = ('收', '出') if side == 'ofr' else ('出', '收')
    previous = [{'message_id': 'peer-old', 'direction': 'in', 'ingest_revision': 1,
                 'text': 'SYN-A 3.10'+peer+'500万'},
                {'message_id': 'our-native', 'direction': 'out', 'ingest_revision': 2,
                 'native_id': 'synthetic-native', 'text': 'SYN-A 3.20%'+own}]
    message = {'message_id': 'current', 'direction': 'in', 'ingest_revision': 3, 'text': text}
    snapshot = {'members': [{'member_id': 'a', 'context': previous}], 'messages': [message]}
    source = 'our-native' if selected == '3.20' else 'peer-old'
    observation = {'member_id': 'a', 'side': side,
        'changes': {'price': {'kind': 'YIELD', 'value': selected}, 'assertion': 'QUOTE'},
        'evidence': {'price': ['current', source], 'assertion': ['current']},
        'segments': [{'message_id': 'current', 'text': fragment or text}],
        'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                          'negotiation_space': 'UNKNOWN', 'reason': '当前本人明确报价片段'}}
    return snapshot, observation


@pytest.mark.parametrize('side,text', [
    ('ofr', 'SYN-A 按你刚才3.20收的价，我出500万'),
    ('bid', 'SYN-A 按你之前3.20出的价，我收500万'),
])
def test_current_affirmative_adoption_rejects_old_price_without_rewriting_any_fact(side, text):
    snapshot, observation = adoption(text, side=side)
    original = deepcopy((snapshot, observation))
    with pytest.raises(InvalidFact, match='explicitly adopted'):
        validate_observation(snapshot, observation, {'syn-a': {'a'}})
    assert (snapshot, observation) == original
    valid_snapshot, valid_observation = adoption(text, selected='3.20', side=side)
    assert validate_observation(valid_snapshot, valid_observation, {'syn-a': {'a'}})['changes']['price']['value'] == '3.20'


@pytest.mark.parametrize('preface', [
    '客户说：“SYN-A 按你3.20收的价，我出500万”。',
    '如果 SYN-A 按你3.20收的价，我出500万，只是一个假设。',
    '不是说 SYN-A 按你3.20收的价，我出500万。',
    'SYN-A 按你3.20收的价，我出500万可以吗？',
])
def test_reported_conditional_negated_or_question_clause_cannot_override_separate_current_quote(preface):
    current = 'SYN-A 我方3.10出500万。'
    snapshot, observation = adoption(preface+current, fragment=current)
    original = deepcopy((snapshot, observation))
    result = validate_observation(snapshot, observation, {'syn-a': {'a'}})
    assert result['changes']['price']['value'] == '3.10'
    assert (snapshot, observation) == original


def test_an_earlier_message_adoption_does_not_override_a_later_explicit_quote():
    snapshot, observation = adoption('SYN-A 我方3.10出500万')
    earlier = {'message_id': 'earlier-adoption', 'direction': 'in', 'ingest_revision': 2,
               'text': 'SYN-A 按你3.20收的价，我出500万'}
    snapshot['members'][0]['context'].append(earlier)
    observation['evidence']['price'].append(earlier['message_id'])
    result = validate_observation(snapshot, observation, {'syn-a': {'a'}})
    assert result['changes']['price']['value'] == '3.10'


def test_explicit_adoption_for_another_bond_does_not_change_this_bonds_quote():
    text = 'SYN-A 按你3.20收的价，我出500万；SYN-B 我方3.10出700万'
    snapshot, observation = adoption(text)
    snapshot['members'].append({'member_id': 'b', 'context': [{
        'message_id': 'b-old', 'direction': 'in', 'ingest_revision': 1, 'text': 'SYN-B 3.10出700万'}]})
    observation.update(member_id='b', evidence={'price': ['current', 'b-old'], 'assertion': ['current']},
                       segments=[{'message_id': 'current', 'text': text.split('；')[1]}])
    result = validate_observation(snapshot, observation, {'syn-a': {'a'}, 'syn-b': {'b'}})
    assert result['member_id'] == 'b' and result['changes']['price']['value'] == '3.10'


def gap_context(*, sibling=False):
    members = [{'member_id': 'a', 'bond_code': 'SYN-A', 'our_side': 'BUY', 'context': []},
               {'member_id': 'b', 'bond_code': 'SYN-B', 'our_side': 'SELL', 'context': []}]
    messages = [{'message_id': 'in-a', 'direction': 'in', 'ingest_revision': 30,
                 'trade_day': '2026-10-18', 'text': 'SYN-A 按消息 unloaded-A123 的报价出500万'},
                {'message_id': 'in-b', 'direction': 'in', 'ingest_revision': 31,
                 'trade_day': '2026-10-18', 'text': ('SYN-B 3.15收800万' if sibling else
                     'SYN-B 按消息 unloaded-B123 的报价收800万')}]
    return {'schema_version': '2.2', 'stage': 'analysis',
            'context_policy_revision': 'latest_quote_confirmation_v1', 'messages': messages, 'members': members}


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_all_gap_prompt_is_only_pending_confirmation_and_does_not_authorize_mixed_facts(version):
    context = gap_context()
    original = deepcopy(context)
    nodes = {node['id']: node['data'] for node in
             build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    projected = projection['main'](json.dumps(context))
    system = projected['system_prompt']
    assert prompts.ANALYSIS not in system and 'evidence_repairs' not in system
    response = json.loads(system.splitlines()[-1])
    assert response['scopes'] == {'in-a': ['a'], 'in-b': ['b']}
    assert response['observations'] == []
    assert response['confirmation_requests'] == [
        {'member_id': 'a', 'side': 'ofr', 'source_ids': ['in-a']},
        {'member_id': 'b', 'side': 'bid', 'source_ids': ['in-b']}]
    assert not response.get('tool_requests') and not response.get('history_requests')
    decoded = json.loads(contract['main'](json.dumps(response), json.dumps(context))['result'])
    assert {row['member_id'] for row in decoded['observations']} == {'a', 'b'}
    assert all(row['validation_error'] == 'REFERENCE_CONFIRMATION_REQUIRED' and 'changes' not in row
               for row in decoded['observations'])
    response['observations'] = [{'member_id': 'a', 'source_ids': ['in-a'], 'side': 'ofr', 'slots': {},
        'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE', 'negotiation_space': 'FINAL', 'reason': '不可混入新事实'}}]
    with pytest.raises(ValueError, match='Unresolved reference cannot also commit'):
        contract['main'](json.dumps(response), json.dumps(context))
    assert context == original


def test_valid_sibling_keeps_ordinary_analysis_and_multi_member_identity_is_required():
    context = gap_context(sibling=True)
    assert prompts.ANALYSIS in prompts.select_system_prompt(context, 'analysis')
    wire = {'schema_version': '2.2', 'scopes': {'in-a': ['a'], 'in-b': ['b']}, 'unresolved': {},
        'confirmation_requests': [{'member_id': 'a', 'side': 'ofr', 'source_ids': ['in-a']}],
        'observations': [{'member_id': 'b', 'source_ids': ['in-b'], 'side': 'bid', 'slots': {},
            'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE', 'negotiation_space': 'UNKNOWN', 'reason': '当前本人收价'}}]}
    decoded = node_code.decode_stage_result(wire, context, 'analysis')
    assert decoded['observations'][0]['member_id'] == 'b' and 'changes' in decoded['observations'][0]
    assert decoded['observations'][1]['member_id'] == 'a' and 'changes' not in decoded['observations'][1]
    for missing in ('member_id', 'source_ids'):
        invalid = deepcopy(wire)
        invalid['observations'][0].pop(missing)
        refused = node_code.decode_stage_result(invalid, context, 'analysis')
        assert refused['observations'][0].get('validation_error')


def test_current_first_projection_keeps_the_existing_visible_candidates_and_all_raw_rows():
    context = gap_context(sibling=True)
    history = [{'message_id': 'old-'+str(index), 'direction': 'out' if index % 2 == 0 else 'in',
                'ingest_revision': index, 'text': 'SYN-A 3.20%收500万，原文  '+str(index)}
               for index in range(20)]
    context['members'][0]['context'] = history
    original = deepcopy(context)
    candidates = collect_candidates(context)
    current = {row['message_id'] for row in context['messages']}
    numeric = {'YIELD', 'YIELD_SUFFIX', 'CHINABOND_BP', 'NET_PRICE', 'DIRTY_PRICE', 'QUANTITY'}
    expected = {row['id']: row for row in candidates if row['message_id'] in current or numeric.intersection(row['roles'])}
    projected = node_code.compact_context(context)
    rows = projected['slot_candidates']
    assert {row['id'] for row in rows} == set(expected)
    assert len(rows) == len(expected) == 70
    ordered = sorted(expected.values(), key=lambda row: row['message_id'] not in current)
    assert [row['id'] for row in rows] == [row['id'] for row in ordered]
    regions = [row['source_region'] for row in rows]
    assert regions == sorted(regions, key=lambda region: region != 'CURRENT')
    expected_hints = {('in-b', '3.15'): ['b'], ('in-b', '800万'): ['b']}
    expected_hints.update({(message['message_id'], token): ['a']
                           for message in history for token in ('3.20%', '500万')})
    for row in rows:
        source = expected[row['id']]
        visible = {key: value for key, value in source.items() if key not in ('lexical', 'value')}
        derived = {'source_region': 'CURRENT' if row['message_id'] in current else 'HISTORY'}
        hint = expected_hints.get((source['message_id'], source['token']))
        if hint is not None:
            derived['explicit_bond_members'] = hint
        assert set(row) == set(visible) | set(derived)
        assert {key: value for key, value in row.items() if key not in derived} == visible
        assert {key: row[key] for key in derived} == derived
        assert row['source_region'] == ('CURRENT' if row['message_id'] in current else 'HISTORY')
    assert projected['messages'] == context['messages']
    assert projected['members'][0]['context'] == history and context == original
