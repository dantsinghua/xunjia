"""A narrow prompt route is not a fact, a retry, or permission to erase requests."""
from copy import deepcopy
from functools import lru_cache
import json

import pytest

from dify.negotiation_v22 import node_code, prompts
from dify.negotiation_v22.build import build
from server.negotiation import slots
from server.negotiation.facts import Quote


CALC = ('另外请用债券计算工具核算：假设同一到期收益率口径，3.10和你之前3.20收的差是多少BP？'
        '答复不重复这两个原始收益率，不作成交。')
DIRECT = 'SYN-A 3.10出500万。'+CALC


def context(text=DIRECT):
    history = [{'message_id': 'history-'+str(i), 'ingest_revision': i+1,
                'direction': 'out' if i % 2 else 'in', 'historical': True,
                'text': 'SYN-A '+('3.20%收' if i % 2 else '3.10出500万'),
                'trade_day': '2026-10-17'} for i in range(20)]
    return {'schema_version': '2.2', 'stage': 'analysis', 'thread_id': 'thread-a',
        'context_policy_revision': 'latest_quote_confirmation_v1',
        'from_revision': 20, 'through_revision': 21, 'context_messages': 20,
        'messages': [{'message_id': 'current', 'ingest_revision': 21, 'direction': 'in',
                      'historical': False, 'text': text, 'trade_day': '2026-10-17'}],
        'members': [{'member_id': 'a', 'bond_code': 'SYN-A', 'our_side': 'BUY',
                     'context': history, 'quotes': {'bid': Quote('bid').dump(), 'ofr': Quote('ofr').dump()}}],
        'tool_results': [], 'history_results': []}


def eligible(value):
    method = getattr(slots, 'direct_priced_calculation', None)
    assert callable(method), 'The conservative direct-calculation route predicate is missing'
    before = deepcopy(value)
    result = method(value)
    assert type(result) is bool and value == before
    return result


def direct_system():
    result = getattr(prompts, 'ANALYSIS_DIRECT', None)
    assert isinstance(result, str) and result.strip(), 'The separate direct-analysis prompt is missing'
    assert result != prompts.ANALYSIS
    return result


@lru_cache(maxsize=2)
def generated(version):
    doc = build('analysis', version=version, provider='synthetic', model='synthetic')
    nodes = {node['id']: node['data'] for node in doc['workflow']['graph']['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    return nodes, projection['main'], contract['main']


def observation(value, space='UNKNOWN'):
    candidates = slots.collect_candidates(value)
    price = next(row for row in candidates if row['message_id'] == 'current'
                 and row['roles'] == ['YIELD'] and row['value'] == '3.10')
    volume = next(row for row in candidates if row['message_id'] == 'current' and row['roles'] == ['QUANTITY'])
    return {'schema_version': '2.2', 'scopes': {'current': ['a']}, 'observations': [{
        'member_id': 'a', 'source_ids': ['current'], 'side': 'ofr',
        'slots': {'price': price['id'], 'volume_wan': volume['id']},
        'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE', 'negotiation_space': space,
                    'reason': '按本轮原文解释谈判空间'}}]}


@pytest.mark.parametrize('text', [
    DIRECT,
    'SYN-A 2.85收800万。另外请核算：假设同一到期收益率口径，2.85与3.00相差多少BP？',
    'SYN-A 3.10%出500万。另外请计算：假设同一收益率口径，3.10和3.20相差多少BP？',
])
def test_only_complete_direct_quotes_with_two_explicit_calculation_operands_select_direct(text):
    value = context(text)
    assert eligible(value)
    system = prompts.select_system_prompt(value, 'analysis')
    assert direct_system() in system and prompts.ANALYSIS not in system


@pytest.mark.parametrize('quote', [
    'SYN-A 能3.10出500万吗？',
    'SYN-A 3.10出500万，可以吗？',
    '不是SYN-A 3.10出500万。',
    '听说别家有SYN-A 3.10出500万。',
    '如果SYN-A 3.10出500万。',
    'SYN-A 3.10出500万，撤了。',
    'SYN-A 3.10出500万，改3.05。',
    'SYN-A 原价出500万。',
    'SYN-A 按你那个价出500万。',
    'SYN-A 净价34.19出500万。',
    'SYN-A 10出500万。',
    'SYN-A 3.10出。',
    'SYN-A 3.10 500。',
    'SYN-A 2026年10月17日，335天。',
    'SYN-A 3.10出500万；3.08收800万。',
    'SYN-B 3.10出500万。',
])
def test_quote_uncertainty_or_other_economic_role_falls_back_without_inventing_facts(quote):
    value = context(quote+CALC)
    assert eligible(value) is False
    assert direct_system() not in prompts.select_system_prompt(value, 'analysis')


@pytest.mark.parametrize('calculation', [
    '另外请核算：假设同一收益率口径，3.10和之前那个价差多少BP？',
    '另外请核算一下差多少BP。',
    '另外评级日期2026年10月17日，剩余335天。',
    '另外请计算：假设同一收益率口径，3.10和3.20相差多少BP？SYN-B 3.05出800万。',
    '另外请计算：假设同一收益率口径，SYN-B 3.10和SYN-C 3.20相差多少BP？',
    '另外按消息 outside-anchor-123 的报价，我出500万。',
])
def test_missing_operands_reference_or_second_bond_do_not_become_direct_from_other_numbers(calculation):
    value = context('SYN-A 3.10出500万。'+calculation)
    assert eligible(value) is False
    assert direct_system() not in prompts.select_system_prompt(value, 'analysis')


@pytest.mark.parametrize('change', ['two-members', 'two-inbound', 'inbound-and-outbound', 'historical', 'outbound'])
def test_direct_route_is_bounded_to_one_member_and_one_current_actual_inbound(change):
    value = context()
    if change == 'two-members':
        other = deepcopy(value['members'][0]); other.update(member_id='b', bond_code='SYN-B')
        value['members'].append(other)
    elif change in ('two-inbound', 'inbound-and-outbound'):
        other = deepcopy(value['messages'][0]); other.update(message_id='second', ingest_revision=22)
        if change == 'inbound-and-outbound': other['direction'] = 'out'
        value['messages'].append(other)
    elif change == 'historical': value['messages'][0]['historical'] = True
    else: value['messages'][0]['direction'] = 'out'
    assert eligible(value) is False


def test_untrusted_precomputed_route_flags_or_tool_text_do_not_replace_actual_quote():
    value = context('SYN-A 按之前那个价出500万。')
    value.update(direct_priced_calculation=True, analysis_route='DIRECT')
    value['tool_results'] = [{'request': {'name': 'bond_info', 'arguments': {}},
                              'result': {'status': 'ok', 'data': {'text': DIRECT}}}]
    assert eligible(value) is False
    assert direct_system() not in prompts.select_system_prompt(value, 'analysis')


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_direct_route_preserves_raw_twenty_history_candidates_and_one_model(version):
    value = context()
    before = deepcopy(value)
    candidates = slots.collect_candidates(value)
    nodes, project, _ = generated(version)
    output = project(json.dumps(value, ensure_ascii=False))
    assert direct_system() in output['system_prompt'] and prompts.ANALYSIS not in output['system_prompt']
    projected = json.loads(output['context'])
    assert projected == node_code.compact_context(value)
    assert projected['messages'] == value['messages']
    assert projected['members'][0]['context'] == value['members'][0]['context']
    assert len(projected['members'][0]['context']) == 20
    assert {c['id'] for c in projected['slot_candidates']} == {c['id'] for c in candidates
        if c['message_id'] == 'current' or set(c['roles']) &
           {'YIELD', 'YIELD_SUFFIX', 'CHINABOND_BP', 'NET_PRICE', 'DIRTY_PRICE', 'QUANTITY'}}
    assert all(c['token'] == next(m for m in value['messages']+value['members'][0]['context']
        if m['message_id'] == c['message_id'])['text'][c['start']:c['end']] for c in candidates)
    assert len([n for n in nodes.values() if n.get('type') == 'llm']) == 1
    assert nodes['model']['prompt_template'][0]['text'] == '{{#context.system_prompt#}}'
    assert 'observations' not in projected and value == before


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_loaded_window_gap_and_trusted_repair_still_take_precedence_over_direct_route(version):
    value = context(DIRECT+'按消息 outside-anchor-123 的报价执行。')
    nodes, project, decode = generated(version)
    selected = project(json.dumps(value))['system_prompt']
    assert direct_system() not in selected and 'confirmation_requests' in selected
    confirmation = json.loads(selected.splitlines()[-1])
    decoded = json.loads(decode(json.dumps(confirmation), json.dumps(value))['result'])
    assert decoded['observations'][0]['validation_error'] == 'REFERENCE_CONFIRMATION_REQUIRED'
    assert 'changes' not in decoded['observations'][0]
    repair = context()
    repair['validation_feedback'] = {'mode': 'EVIDENCE_ONLY', 'attempt': 1, 'max_attempts': 1,
        'targets': [{'index': 0, 'member_id': 'a', 'side': 'ofr', 'reason': 'synthetic rejection'}],
        'original_response': observation(repair)}
    assert project(json.dumps(repair))['system_prompt'] == prompts.ANALYSIS_REPAIR


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_actual_code_keeps_model_meaning_choice_and_rejects_same_member_confirmation_mix(version):
    value = context()
    _, _, decode = generated(version)
    for space in ('UNKNOWN', 'NEGOTIABLE', 'FINAL'):
        raw = observation(value, space)
        decoded = json.loads(decode(json.dumps(raw), json.dumps(value))['result'])
        assert decoded['observations'][0]['changes']['finality'] == space
        assert decoded['observations'][0]['changes']['price']['value'] == '3.10'
        raw['confirmation_requests'] = [{'member_id': 'a', 'side': 'ofr', 'source_ids': ['current']}]
        with pytest.raises(ValueError, match='Unresolved reference cannot also commit'):
            decode(json.dumps(raw), json.dumps(value))


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_direct_route_does_not_silently_strip_mixed_model_requests_or_expand_request_budget(version):
    value = context()
    _, _, decode = generated(version)
    raw = observation(value)
    request = {'name': 'bond_calc', 'arguments': {'schema_version': '1', 'operation': 'yield_gap'}}
    raw['tool_requests'] = [request]
    before = deepcopy(raw)
    assert json.loads(decode(json.dumps(raw), json.dumps(value))['result']) == raw
    assert raw == before
    raw['tool_requests'] = [request for _ in range(7)]
    with pytest.raises(ValueError, match='bounded tool requests|budget'):
        decode(json.dumps(raw), json.dumps(value))
