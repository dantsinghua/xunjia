"""Literal indexing preserves ambiguity and never becomes fact authority."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import node_code
from dify.negotiation_v22.build import build
from server.negotiation.slots import collect_candidates, literal_member_hints


def context(text):
    members = []
    for mid, code in [('a', 'SYN-A'), ('b', 'SYN-B')]:
        members.append({'member_id': mid, 'bond_code': code, 'our_side': 'BUY', 'quotes': {},
            'context': [{'message_id': mid+'-old-'+str(i), 'direction': 'in' if i % 2 else 'out',
                'text': code+' 3.00出700万', 'ingest_revision': i+1, 'historical': True,
                'trade_day': '2026-10-17'} for i in range(20)]})
    return {'schema_version': '2.2', 'stage': 'analysis',
        'context_policy_revision': 'latest_quote_confirmation_v1', 'context_messages': 20,
        'members': members, 'messages': [{'message_id': 'current', 'direction': 'in',
            'text': text, 'ingest_revision': 41, 'historical': False, 'trade_day': '2026-10-17'}],
        'tool_results': [], 'history_results': []}


def current_hints(value):
    before = deepcopy(value)
    candidates = collect_candidates(value)
    original = deepcopy(candidates)
    hints = literal_member_hints(value, candidates)
    assert value == before and candidates == original
    assert set(hints) <= {row['id'] for row in candidates}
    return [(row, hints.get(row['id'], [])) for row in candidates if row['message_id'] == 'current']


def hints_for(value, token):
    found = [hints for row, hints in current_hints(value) if row['token'] == token]
    assert found, token
    return found


def test_two_explicit_bond_clauses_index_their_own_price_and_quantity_only():
    value = context('SYN-A 3.10出500万；SYN-B 3.05收800万')
    for token in ('3.10', '500万'): assert hints_for(value, token) == [['a']]
    for token in ('3.05', '800万'): assert hints_for(value, token) == [['b']]


@pytest.mark.parametrize('label', ['XSYN-A', 'SYN-AX', 'SYN-A-TAIL', 'SYN-A_more', 'SYN-A.EXTRA'])
def test_known_code_must_not_match_inside_a_longer_identifier(label):
    value = context(label+' 3.10出500万')
    assert hints_for(value, '3.10') == [[]]
    assert hints_for(value, '500万') == [[]]


@pytest.mark.parametrize('text,token', [
    ('SYN-A。按刚才那个价3.10出500万', '3.10'),
    ('SYN-A 3.10出500万，另一只3.05出800万', '3.05'),
    ('SYN-A 3.10出500万另一只3.05出800万', '3.05'),
    ('SYN-A和SYN-B 两只同价3.10出500万', '3.10'),
    ('SYN-A 按SYN-B的3.10价出500万', '3.10'),
    ('SYN-A 3.10两只同价出500万', '3.10'),
    ('SYN-A 3.10是另一只的报价', '3.10'),
    ('SYN-A 3.10按SYN-B的报价', '3.10'),
])
def test_shared_or_referenced_price_is_not_strongly_assigned_to_the_nearest_prefix(text, token):
    assert hints_for(context(text), token) == [[]]


def test_duplicate_known_code_does_not_pick_one_member_arbitrarily():
    value = context('SYN-A 3.10出500万')
    value['members'][1]['bond_code'] = 'SYN-A'
    assert hints_for(value, '3.10') == [[]]
    assert hints_for(value, '500万') == [[]]


def test_unmarked_no_price_status_change_is_retained_as_original_candidates():
    value = context('另一只不出了，不能再改善')
    rows = current_hints(value)
    assert all(not hints for _, hints in rows)
    assert {'WITHDRAWAL', 'NEGOTIATION_SPACE'} <= {role for row, _ in rows for role in row['roles']}


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_projection_preserves_both_twenty_windows_and_all_existing_candidate_fields(version):
    value = context('SYN-A 3.10出500万，另一只我再看一下')
    value['current_member_candidates'] = [{'member_id': 'b', 'candidate_ids': ['fabricated']}]
    before = deepcopy(value)
    candidates = collect_candidates(value)
    nodes = {node['id']: node['data'] for node in build('analysis', version=version,
        provider='synthetic', model='synthetic')['workflow']['graph']['nodes']}
    namespace = {};exec(nodes['context']['code'], namespace)
    projected = json.loads(namespace['main'](json.dumps(value))['context'])
    assert projected == node_code.compact_context(value) and value == before
    assert projected['messages'] == value['messages']
    for original, member in zip(value['members'], projected['members']):
        assert member['context'] == original['context'] and len(member['context']) == 20
    numeric = {'YIELD', 'YIELD_SUFFIX', 'CHINABOND_BP', 'NET_PRICE', 'DIRTY_PRICE', 'QUANTITY'}
    expected = {row['id']: row for row in candidates if row['message_id'] == 'current' or numeric.intersection(row['roles'])}
    assert {row['id'] for row in projected['slot_candidates']} == set(expected)
    for row in projected['slot_candidates']:
        assert {k: v for k, v in row.items() if k not in ('source_region', 'explicit_bond_members')} == {
            k: v for k, v in expected[row['id']].items() if k not in ('lexical', 'value')}
    groups = {group['member_id']: group['candidate_ids'] for group in projected['current_member_candidates']}
    assert groups['a'] and groups['b'] == [] and 'fabricated' not in groups['a']
    assert set(groups['a']) == {row['id'] for row in projected['slot_candidates']
        if row['message_id'] == 'current' and row.get('explicit_bond_members') == ['a']}
    assert 'observations' not in projected
