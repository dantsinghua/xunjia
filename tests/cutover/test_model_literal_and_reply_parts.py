"""Literal spelling and reply sections may change syntax, never trade facts."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from dify.negotiation_v22 import node_code
from dify.negotiation_v22.cases import reference_snapshot
from test_production_flow import OWNER, desk, raw
from test_acceptance_contract import current, observation, parser, setup_groups
from test_model_wire_adapter import sample, paired, SyntheticDify, feedback_context
from test_reply_text_repair import response as legacy_reply
from test_reply_text_repair import reply_case, run as run_reply, replies, unchanged_facts


def literal_sample(literal):
    context, canonical = sample()
    wire = paired(canonical)
    wire['observations'][0]['fields'][0]['value'] = deepcopy(literal)
    return context, canonical, wire


@pytest.mark.parametrize('literal,expected', [
    ({'basis': '收益率', 'value': '3.10'}, {'kind': 'YIELD', 'value': '3.10', 'notation': 'ABSOLUTE'}),
    ({'basis': '收益率', 'value': '-0.25'}, {'kind': 'YIELD', 'value': '-0.25', 'notation': 'ABSOLUTE'}),
    ({'basis': '净价', 'value': '34.19'}, {'kind': 'NET_PRICE', 'value': '34.19', 'notation': 'ABSOLUTE'}),
    ({'basis': '全价', 'value': '99.32', 'settlement_date': '2026-10-17'},
     {'kind': 'DIRTY_PRICE', 'value': '99.32', 'notation': 'ABSOLUTE', 'settlement_date': '2026-10-17'}),
    ({'basis': '收益率尾数', 'value': '04'}, {'kind': 'YIELD', 'value': '04', 'notation': 'YIELD_SUFFIX'}),
    ({'basis': '中债估值', 'operation': '加', 'bp': '5.00'}, {'kind': 'YIELD', 'value': '5.00', 'notation': 'CHINABOND_BP'}),
    ({'basis': '中债估值', 'operation': '减', 'bp': '5'}, {'kind': 'YIELD', 'value': '-5', 'notation': 'CHINABOND_BP'}),
])
def test_literal_price_is_an_exact_field_alias_and_sign_mapping(literal, expected):
    context, canonical, wire = literal_sample(literal)
    before = deepcopy((context, wire))
    result = node_code.decode_stage_result(wire, context, 'analysis')
    canonical['observations'][0]['changes']['price'] = expected
    assert result == canonical
    assert (context, wire) == before


@pytest.mark.parametrize('literal', [
    {'basis': '中债估值', 'operation': '减', 'bp': '-5'},
    {'basis': '中债估值', 'operation': '减', 'bp': True},
    {'basis': '中债估值', 'operation': '加', 'bp': 5.0},
    {'basis': '中债估值', 'operation': '加', 'bp': '5e0'},
    {'basis': '中债估值', 'operation': '加', 'bp': '+5'},
    {'basis': '中债估值', 'operation': 'subtract', 'bp': '5'},
    {'basis': '中债估值', 'operation': '减', 'bp': '5', 'settlement_date': '2026-10-17'},
    {'basis': '未知口径', 'value': '3.10'},
    {'basis': '收益率', 'value': 3.10},
    {'basis': '收益率', 'value': '3.10', 'settlement_date': '2026-10-17'},
    {'basis': '收益率尾数', 'value': '4'},
    {'basis': '全价', 'value': '99.32', 'kind': 'NET_PRICE'},
    {'basis': '净价', 'value': '99.32', 'notation': 'ABSOLUTE'},
    {'basis': '收益率', 'value': '3.10', 'computed_yield': '3.10', 'calculation_ref': 'model-created'},
])
def test_invalid_literal_shape_is_rejected_without_dropping_its_valid_sibling(literal):
    context, canonical, wire = literal_sample(literal)
    result = node_code.decode_stage_result(wire, context, 'analysis')
    assert len(result['observations']) == 2
    assert result['observations'][0].get('validation_error')
    assert result['observations'][1] == canonical['observations'][1]


@pytest.mark.parametrize('operation,side,case_id,expected', [
    ('减', 'bid', 'bp-minus', '2.53'), ('加', 'ofr', 'bp-plus', '3.04')])
def test_generated_code_literal_bp_flows_through_the_real_engine_valuation(desk, operation, side, case_id, expected):
    app, _, flows, native, _ = desk
    peer = setup_groups(desk, stopped=False)['SYN-A']['peers'][0]
    app.state.inquiry.reference_provider = SimpleNamespace(snapshot=lambda: reference_snapshot(case_id))
    app.state.inquiry.calendar = SimpleNamespace(latest_valuation_day=lambda _stamp: '2026-10-17')
    raw(app, peer['thread_id'], 'SYN-A 中债估值'+operation+'5BP'+('收' if side == 'bid' else '出')+'1000万')
    canonical = parser(lambda _snapshot, message: [observation(peer, message, price=expected,
        side=side, volume='1000', segment=message['text'], assertion='QUOTE')])
    def model(context):
        wire = paired(canonical(context))
        wire['observations'][0]['fields'][0]['value'] = {'basis': '中债估值', 'operation': operation, 'bp': '5'}
        return wire
    endpoint = SyntheticDify('analysis', model)
    flows.analyze = endpoint.client.call
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer['thread_id'], force=True)
    assert result['facts_committed'] and not result['rejected'], result
    assert len(endpoint.calls) == 1
    assert current(app, peer, side).price.value == expected
    assert len(native.sent) == 1


def parts_response(information, negotiation, *, kind='REPLY'):
    proposal = {'kind': kind, 'information_text': information, 'negotiation_text': negotiation}
    if kind == 'REPLY': proposal['our_price'] = {'kind': 'YIELD', 'value': '3.19'}
    return {'schema_version': '2.2', 'action': 'SEND', 'proposal': proposal,
            'trader_view': {'reason': 'Synthetic unchanged decision'}, 'tool_requests': [], 'history_requests': []}


@pytest.mark.parametrize('information,negotiation,kind', [
    (' 发行主体为SYN-ISSUER。 ', 'SYN-A 3.19%收500万。', 'REPLY'),
    ('  ', 'SYN-A 3.19%收500万。', 'REPLY'),
    ('', 'SYN-A 3.19%收500万。', 'REPLY'),
    ('发行主体为SYN-ISSUER。', '', 'SOCIAL'),
])
def test_reply_parts_join_only_nonempty_strings_in_order_without_trimming(information, negotiation, kind):
    wire = parts_response(information, negotiation, kind=kind)
    original = deepcopy(wire)
    result = node_code.decode_stage_result(wire, {'schema_version': '2.2', 'stage': 'reply'}, 'reply')
    expected = deepcopy(wire)
    expected['proposal'].pop('information_text')
    expected['proposal'].pop('negotiation_text')
    expected['proposal']['text'] = '\n'.join(part for part in (information, negotiation) if part != '')
    assert result == expected and wire == original


def test_two_model_sections_create_one_exact_message_through_generated_code_and_real_engine(desk, monkeypatch):
    case = reply_case(desk)
    # Explicit historical-wire compatibility; shared production desk stays current.
    monkeypatch.setattr(case.app.state.negotiation_engine, 'context_policy_revision', 'explicit_anchors_v0')
    information, negotiation = '发行主体为SYN-ISSUER。', 'SYN-A 3.19%收500万。'
    endpoint = SyntheticDify('reply', lambda _context: parts_response(information, negotiation))
    case.flows.decide = endpoint.client.call
    result = run_reply(case)
    assert result['state'] == 'QUEUED' and len(endpoint.calls) == 1
    assert len(replies(case)) == 1
    body = json.loads(replies(case)[0]['body_json'])
    assert body['text'] == information+'\n'+negotiation
    assert body['our_price']['value'] == '3.19'
    unchanged_facts(case)
    assert len(case.native.sent) == 1


@pytest.mark.parametrize('change', ['missing_information', 'missing_negotiation', 'mixed_text',
    'information_not_string', 'negotiation_not_string', 'both_empty', 'joined_too_long'])
def test_reply_parts_are_strictly_paired_exclusive_strings_with_one_combined_length_limit(change):
    wire = parts_response('发行主体为SYN-ISSUER。', 'SYN-A 3.19%收500万。')
    proposal = wire['proposal']
    if change == 'missing_information': proposal.pop('information_text')
    elif change == 'missing_negotiation': proposal.pop('negotiation_text')
    elif change == 'mixed_text': proposal['text'] = 'Do not choose between parallel bodies'
    elif change == 'information_not_string': proposal['information_text'] = {'text': '不可猜转文本'}
    elif change == 'negotiation_not_string': proposal['negotiation_text'] = False
    elif change == 'both_empty': proposal['information_text'] = proposal['negotiation_text'] = ''
    else: proposal['information_text'] = '资'*750; proposal['negotiation_text'] = '料'*750
    with pytest.raises((ValueError, TypeError, KeyError)):
        node_code.decode_stage_result(wire, {'schema_version': '2.2', 'stage': 'reply'}, 'reply')


def test_one_nonempty_reply_part_can_use_the_original_exact_1500_character_limit():
    wire = parts_response('资'*1500, '', kind='SOCIAL')
    result = node_code.decode_stage_result(wire, {'schema_version': '2.2', 'stage': 'reply'}, 'reply')
    assert result['proposal']['text'] == '资'*1500
    assert 'our_price' not in result['proposal']


@pytest.mark.parametrize('phase', ['WAIT', 'HANDOFF', 'repair', 'tool', 'history'])
def test_new_reply_parts_cannot_be_used_in_another_phase(phase):
    context = {'schema_version': '2.2', 'stage': 'reply', 'member_id': 'a'}
    wire = parts_response('主体资料。', 'SYN-A 3.19%收500万。')
    if phase in ('WAIT', 'HANDOFF'): wire['action'] = phase
    elif phase == 'repair':
        context['validation_feedback'] = {'mode': 'TEXT_ONLY', 'attempt': 1, 'max_attempts': 1,
            'reason': 'Public yield text differs from validated quote', 'original_response': legacy_reply()}
    elif phase == 'tool':
        wire['tool_requests'] = [{'name': 'bond_info', 'arguments': {'schema_version': '1', 'mode': 'get', 'bond_id': 'SYN-A'}}]
    else: wire['history_requests'] = [{'member_id': 'a', 'message_ids': ['old']}]
    with pytest.raises((ValueError, TypeError, KeyError)):
        node_code.decode_stage_result(wire, context, 'reply')


@pytest.mark.parametrize('phase', ['tool', 'history'])
def test_new_literal_price_is_final_only_but_old_typed_request_rounds_are_unchanged(phase):
    context, canonical, wire = literal_sample({'basis': '收益率', 'value': '3.10'})
    field = 'tool_requests' if phase == 'tool' else 'history_requests'
    request = ([{'name': 'bond_info', 'arguments': {'schema_version': '1', 'mode': 'get', 'bond_id': 'SYN-A'}}]
               if phase == 'tool' else [{'member_id': 'a', 'message_ids': ['old']}])
    canonical[field] = deepcopy(request)
    wire[field] = deepcopy(request)
    assert node_code.decode_stage_result(deepcopy(canonical), context, 'analysis') == canonical
    with pytest.raises((ValueError, TypeError, KeyError)):
        node_code.decode_stage_result(wire, context, 'analysis')


def test_new_literal_price_does_not_translate_inside_a_repair_or_legacy_canonical_shape():
    context, original, _patch = feedback_context()
    wire = paired(original)
    literal = {'basis': '收益率', 'value': '3.10'}
    wire['observations'][0]['fields'][0]['value'] = literal
    with pytest.raises(ValueError):
        node_code.decode_stage_result(wire, context, 'analysis')
    context.pop('validation_feedback')
    original['observations'][0]['changes']['price'] = literal
    legacy = node_code.decode_stage_result(original, context, 'analysis')
    assert legacy == node_code.validate_result(original, context, 'analysis')
    assert legacy['observations'][0]['changes']['price'] == literal, 'Only the declared paired new shape may be decoded'


@pytest.mark.parametrize('style', ['literal_wrong_operation', 'legacy_wrong_sign', 'legacy_precomputed'])
def test_decoder_never_repairs_wrong_operation_or_old_typed_price_from_reason_or_raw_text(desk, style):
    app, _, flows, native, _ = desk
    peer = setup_groups(desk, stopped=False)['SYN-A']['peers'][0]
    app.state.inquiry.reference_provider = SimpleNamespace(snapshot=lambda: reference_snapshot('bp-minus'))
    app.state.inquiry.calendar = SimpleNamespace(latest_valuation_day=lambda _stamp: '2026-10-17')
    raw(app, peer['thread_id'], 'SYN-A 中债估值减5BP收1000万')
    def model(context):
        if context.get('validation_feedback'):
            return deepcopy(context['validation_feedback']['original_response'])
        value = parser(lambda _snapshot, message: [observation(peer, message, price='2.53', side='bid',
            volume='1000', segment=message['text'], assertion='QUOTE')])(context)
        value['observations'][0]['interpretation']['reason'] = '原文明确减5BP，但解码器不能读取这句话改错字段'
        wrong = {'kind': 'YIELD', 'value': '2.53' if style == 'legacy_precomputed' else '5',
                 'notation': 'ABSOLUTE' if style == 'legacy_precomputed' else 'CHINABOND_BP'}
        value['observations'][0]['changes']['price'] = wrong
        if style == 'literal_wrong_operation':
            value = paired(value)
            value['observations'][0]['fields'][0]['value'] = {'basis': '中债估值', 'operation': '加', 'bp': '5'}
        return value
    endpoint = SyntheticDify('analysis', model)
    flows.analyze = endpoint.client.call
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer['thread_id'], force=True)
    assert result['facts_committed'] and result['rejected']
    assert current(app, peer, 'bid').price is None
    assert len(native.sent) == 1


@pytest.mark.parametrize('information', [
    '贵方3.10%出500万，所问价差为10BP。',
    'SYN-A 3.19%出500万。',
    '本次600万。',
    '确认成交。',
    '内部底价3.19%。',
])
def test_information_section_does_not_bypass_the_original_full_public_text_guard(desk, information, monkeypatch):
    case = reply_case(desk)
    monkeypatch.setattr(case.app.state.negotiation_engine, 'context_policy_revision', 'explicit_anchors_v0')
    first = parts_response(information, 'SYN-A 3.19%收500万。')
    def model(context):
        if context.get('validation_feedback'):
            return deepcopy(context['validation_feedback']['original_response'])
        return deepcopy(first)
    endpoint = SyntheticDify('reply', model)
    case.flows.decide = endpoint.client.call
    result = run_reply(case)
    assert result['state'] == 'REJECTED' and not replies(case)
    unchanged_facts(case)
    assert len(case.native.sent) == 1


def test_real_pure_information_reply_keeps_the_existing_no_price_permission(desk, monkeypatch):
    case = reply_case(desk)
    monkeypatch.setattr(case.app.state.negotiation_engine, 'context_policy_revision', 'explicit_anchors_v0')
    info = '公开主体为Synthetic Issuer。'
    endpoint = SyntheticDify('reply', lambda _context: parts_response(info, '', kind='SOCIAL'))
    case.flows.decide = endpoint.client.call
    result = run_reply(case)
    assert result['state'] == 'QUEUED' and len(endpoint.calls) == 1
    command = case.app.state.inquiry.commands_core.get(OWNER, result['command_id'])
    body = json.loads(command['body_json'])
    assert body['kind'] == 'SOCIAL' and body['text'] == info and 'our_price' not in body
    unchanged_facts(case)
    assert len(case.native.sent) == 1


def test_whitespace_only_joined_message_still_fails_the_original_whole_message_gate(desk, monkeypatch):
    case = reply_case(desk)
    monkeypatch.setattr(case.app.state.negotiation_engine, 'context_policy_revision', 'explicit_anchors_v0')
    endpoint = SyntheticDify('reply', lambda _context: parts_response(' \t', '', kind='SOCIAL'))
    case.flows.decide = endpoint.client.call
    result = run_reply(case)
    assert endpoint.outputs[0]['proposal']['text'] == ' \t', 'Do not trim or invent content during decoding'
    assert result['state'] == 'REJECTED' and result.get('command_id') is None
    unchanged_facts(case)
    assert len(case.native.sent) == 1
