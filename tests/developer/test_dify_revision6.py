"""Reduced analysis work preserves the existing reference and fact contracts."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import node_code, prompts
from dify.negotiation_v22.build import build
from server.negotiation.slots import collect_candidates


def context():
    return {'schema_version': '2.2', 'stage': 'analysis', 'context_policy_revision': 'latest_quote_confirmation_v1',
        'messages': [{'message_id': 'current', 'direction': 'in', 'ingest_revision': 22,
            'trade_day': '2026-10-18', 'text': 'SYN-A 按消息 unloaded12345 的报价，我出500万，不能再改善了'}],
        'members': [{'member_id': 'a', 'our_side': 'BUY', 'bond_code': 'SYN-A', 'context': []}]}


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_all_gap_generated_route_emits_valid_pending_confirmation_without_quote_work(version):
    ctx = context()
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    nodes = {node['id']: node['data'] for node in graph['nodes']}
    namespace = {}
    exec(nodes['context']['code'], namespace)
    projected = namespace['main'](json.dumps(ctx))
    template = json.loads(projected['system_prompt'].splitlines()[-1])
    assert template['observations'] == [] and not template.get('tool_requests') and not template.get('history_requests')
    assert template['confirmation_requests'] == [{'member_id': 'a', 'side': 'ofr', 'source_ids': ['current']}]
    namespace = {}
    exec(nodes['contract']['code'], namespace)
    decoded = json.loads(namespace['main'](json.dumps(template), json.dumps(ctx))['result'])
    assert decoded['scopes'] == {'current': ['a']}
    assert decoded['observations'][0]['validation_error'] == 'REFERENCE_CONFIRMATION_REQUIRED'
    assert 'changes' not in decoded['observations'][0]
    assert json.loads(projected['context'])['messages'] == ctx['messages']


def test_reference_gap_with_another_member_keeps_joint_analysis_for_the_sibling():
    ctx = context()
    ctx['members'].append({'member_id': 'b', 'our_side': 'BUY', 'bond_code': 'SYN-B', 'context': []})
    ctx['messages'].append({'message_id': 'sibling', 'direction': 'in', 'ingest_revision': 23,
                            'trade_day': '2026-10-18', 'text': 'SYN-B 3.15出800万'})
    system = prompts.select_system_prompt(ctx, 'analysis')
    assert prompts.ANALYSIS in system
    assert '其他成员仍独立分析' in system


def test_conflicting_confirmation_and_final_observation_is_still_rejected():
    ctx = context()
    result = json.loads(prompts.select_system_prompt(ctx, 'analysis').splitlines()[-1])
    result['observations'] = [{'member_id': 'a', 'side': 'ofr', 'source_ids': ['current'],
        'slots': {}, 'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE', 'negotiation_space': 'FINAL', 'reason': '不能再改善'}}]
    with pytest.raises(ValueError, match='Unresolved reference cannot also commit'):
        node_code.decode_stage_result(result, ctx, 'analysis')


def test_candidate_presentation_preserves_all_sources_ids_and_literals_with_current_first():
    ctx = context()
    ctx['members'][0]['context'] = [{'message_id': 'history', 'direction': 'out', 'ingest_revision': 1,
        'text': 'SYN-A 3.20收，请报量'}]
    original = deepcopy(ctx)
    all_candidates = collect_candidates(ctx)
    projected = node_code.compact_context(ctx)['slot_candidates']
    assert projected[0]['message_id'] == 'current' and projected[0]['source_region'] == 'CURRENT'
    numeric = {'YIELD', 'YIELD_SUFFIX', 'CHINABOND_BP', 'NET_PRICE', 'DIRTY_PRICE', 'QUANTITY'}
    expected = [c for c in all_candidates if c['message_id'] == 'current' or numeric.intersection(c['roles'])]
    assert {c['id'] for c in projected} == {c['id'] for c in expected}
    for row in projected:
        original_row = next(c for c in all_candidates if c['id'] == row['id'])
        assert {key: row[key] for key in ('message_id', 'revision', 'start', 'end', 'token', 'roles', 'direction')} == {
            key: original_row[key] for key in ('message_id', 'revision', 'start', 'end', 'token', 'roles', 'direction')}
    assert ctx == original
