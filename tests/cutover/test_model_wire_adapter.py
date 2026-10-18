"""Independent model-wire gates: representation may change, evidence may not."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import node_code
from dify.negotiation_v22.build import build
from server.negotiation.chatflow import ChatflowClient
from test_production_flow import OWNER, desk
from test_acceptance_contract import current
from test_analysis_evidence_repair import (
    repair_case, first_result, repaired, run as run_analysis, assert_baseline,
)
from test_reply_text_repair import (
    reply_case, response, FIXED_TEXT, run as run_reply, replies, unchanged_facts,
)


def decoder():
    method = getattr(node_code, 'decode_stage_result', None)
    assert callable(method), 'The explicit model-wire decoder is missing'
    return method


def sample():
    text = 'SYN-A 3.10出500万；SYN-B 3.11出700万'
    context = {'schema_version': '2.2', 'stage': 'analysis',
        'messages': [{'message_id': 'in', 'direction': 'in', 'text': text}],
        'members': [{'member_id': 'a', 'context': [{'message_id': 'old',
            'direction': 'out', 'text': 'SYN-A 3.10%收', 'native_id': 'synthetic-native'}]},
                    {'member_id': 'b', 'context': []}]}
    observations = []
    for member, price, amount, fragment in [('a', '3.10', '500', text.split('；')[0]),
                                          ('b', '3.11', '700', text.split('；')[1])]:
        changes = {'price': {'kind': 'YIELD', 'value': price}, 'volume_wan': amount,
                   'assertion': 'QUOTE', 'validity': 'ACTIVE', 'finality': 'UNKNOWN',
                   'confirmed_day': '2026-10-17'}
        observations.append({'member_id': member, 'side': 'ofr', 'changes': changes,
            'evidence': {field: ['in'] for field in changes},
            'segments': [{'message_id': 'in', 'text': fragment}],
            'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                              'negotiation_space': 'UNKNOWN', 'reason': 'Synthetic literal quotation'}})
    value = {'schema_version': '2.2', 'scopes': {'in': ['a', 'b']}, 'unresolved': {},
             'observations': observations, 'corrections': [], 'tool_requests': [], 'history_requests': []}
    return context, value


def paired(value):
    result = deepcopy(value)
    for item in result['observations']:
        changes, evidence = item.pop('changes'), item.pop('evidence')
        item['fields'] = [{'field': field, 'value': deepcopy(value), 'evidence': deepcopy(evidence[field])}
                          for field, value in changes.items()]
    return result


def feedback_context(*, bad_sibling=False):
    ctx, original = sample()
    if bad_sibling:
        original['observations'][1] = {'validation_error': 'Every changed field needs evidence'}
    ctx['validation_feedback'] = {'mode': 'EVIDENCE_ONLY', 'attempt': 1, 'max_attempts': 1,
        'targets': [{'index': 0, 'member_id': 'a', 'side': 'ofr',
                     'reason': 'Price is not supported by its classified raw evidence'}],
        'original_response': deepcopy(original)}
    patch = {'schema_version': '2.2', 'evidence_repairs': [{'index': 0,
        'add_evidence': {'price': ['old']},
        'add_segments': [{'message_id': 'old', 'text': 'SYN-A 3.10%收'}]}]}
    return ctx, original, patch


class CodeResponse:
    status = 200
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return json.dumps(self.value, ensure_ascii=False).encode()[:limit]


class SyntheticDify:
    """Only HTTP/model are synthetic; execute the actual generated Code node."""
    def __init__(self, stage, model, version='1.17.1'):
        doc = build(stage, version=version, provider='synthetic', model='synthetic')
        contract = next(n['data']['code'] for n in doc['workflow']['graph']['nodes'] if n['id'] == 'contract')
        self.namespace = {}
        exec(contract, self.namespace)
        self.stage, self.model, self.calls, self.outputs = stage, model, [], []
        self.client = ChatflowClient('http://synthetic.invalid', 'synthetic-only', stage=stage, opener=self)

    def open(self, request, timeout):
        payload = json.loads(request.data)
        context_json = payload['inputs']['context_json']
        context = json.loads(context_json)
        self.calls.append(deepcopy(context))
        assert len(self.calls) <= 2, 'Wire decoding must not add an extra model round'
        result = self.namespace['main'](json.dumps(self.model(context), ensure_ascii=False), context_json)
        self.outputs.append(json.loads(result['result']))
        return CodeResponse({'answer': result['result'], 'message_id': 'synthetic-'+str(len(self.calls)),
                             'conversation_id': 'synthetic', 'metadata': {}})


def test_paired_fields_are_exactly_lossless_and_do_not_mutate_the_supplied_objects():
    decode = decoder()
    ctx, canonical = sample()
    wire = paired(canonical)
    before = deepcopy((ctx, wire))
    assert decode(wire, ctx, 'analysis') == canonical
    assert (ctx, wire) == before
    assert decode(deepcopy(canonical), ctx, 'analysis') == canonical


@pytest.mark.parametrize('failure', ['missing_evidence', 'missing_value', 'duplicate', 'unknown',
                                    'extra_key', 'mixed_canonical', 'not_a_list'])
def test_invalid_pair_slots_remain_rejected_without_erasing_the_good_sibling(failure):
    decode = decoder()
    ctx, canonical = sample()
    wire = paired(canonical)
    slot = wire['observations'][0]
    if failure == 'missing_evidence': slot['fields'][0].pop('evidence')
    elif failure == 'missing_value': slot['fields'][0].pop('value')
    elif failure == 'duplicate': slot['fields'].append(deepcopy(slot['fields'][0]))
    elif failure == 'unknown': slot['fields'][0]['field'] = 'invented'
    elif failure == 'extra_key': slot['fields'][0]['fallback_evidence'] = ['in']
    elif failure == 'mixed_canonical': slot['changes'] = deepcopy(canonical['observations'][0]['changes'])
    else: slot['fields'] = {'price': slot['fields'][0]}
    result = decode(wire, ctx, 'analysis')
    assert len(result['observations']) == 2
    assert result['observations'][0].get('validation_error')
    assert result['observations'][1] == canonical['observations'][1]


def test_old_canonical_missing_evidence_still_fails_the_original_gate():
    ctx, value = sample()
    value['observations'][0]['evidence'].pop('price')
    result = node_code.validate_result(value, ctx, 'analysis')
    assert result['observations'][0] == {'validation_error': 'Every changed field needs evidence'}
    assert result['observations'][1] == value['observations'][1]


def test_decoder_does_not_rescue_an_invalid_legacy_canonical_observation():
    decode = decoder()
    ctx, value = sample()
    value['observations'][0]['evidence'].pop('price')
    assert decode(value, ctx, 'analysis') == node_code.validate_result(value, ctx, 'analysis')


@pytest.mark.parametrize('bad_sibling', [False, True])
def test_evidence_patch_only_appends_to_its_target_and_preserves_even_rejected_siblings(bad_sibling):
    decode = decoder()
    ctx, original, patch = feedback_context(bad_sibling=bad_sibling)
    before = deepcopy(ctx)
    expected = deepcopy(original)
    expected['observations'][0]['evidence']['price'].append('old')
    expected['observations'][0]['segments'].extend(patch['evidence_repairs'][0]['add_segments'])
    assert decode(patch, ctx, 'analysis') == expected
    assert ctx == before


@pytest.mark.parametrize('failure', ['no_feedback', 'wrong_mode', 'attempt_two', 'bool_attempt',
    'bool_index', 'other_index', 'duplicate_index', 'self_original', 'mixed_full', 'extra_change',
    'unloaded', 'nonliteral', 'wrong_field', 'no_addition'])
def test_evidence_patch_cannot_expand_feedback_authority(failure):
    decode = decoder()
    ctx, original, patch = feedback_context()
    slot = patch['evidence_repairs'][0]
    if failure == 'no_feedback': ctx.pop('validation_feedback')
    elif failure == 'wrong_mode': ctx['validation_feedback']['mode'] = 'TEXT_ONLY'
    elif failure == 'attempt_two': ctx['validation_feedback']['attempt'] = 2
    elif failure == 'bool_attempt': ctx['validation_feedback']['attempt'] = True
    elif failure == 'bool_index': slot['index'] = False
    elif failure == 'other_index': slot['index'] = 1
    elif failure == 'duplicate_index': patch['evidence_repairs'].append(deepcopy(slot))
    elif failure == 'self_original': patch['original_response'] = original
    elif failure == 'mixed_full': patch.update(original)
    elif failure == 'extra_change': slot['changes'] = {'volume_wan': '999'}
    elif failure == 'unloaded': slot['add_evidence']['price'] = ['not-loaded']
    elif failure == 'nonliteral': slot['add_segments'][0]['text'] = 'SYN-A forged price'
    elif failure == 'wrong_field': slot['add_evidence'] = {'volume_wan': ['old']}
    else: slot['add_evidence'] = {}; slot['add_segments'] = []
    with pytest.raises((ValueError, TypeError, KeyError)):
        decode(patch, ctx, 'analysis')


def test_a_segment_only_patch_is_bounded_and_never_fills_evidence_by_itself():
    decode = decoder()
    ctx, original, patch = feedback_context()
    patch['evidence_repairs'][0]['add_evidence'] = {}
    result = decode(patch, ctx, 'analysis')
    assert result['observations'][0]['evidence'] == original['observations'][0]['evidence']
    assert len(result['observations'][0]['segments']) == len(original['observations'][0]['segments']) + 1


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_code_and_client_feed_pair_facts_to_real_engine_then_one_evidence_patch(desk, version):
    decoder()
    case = repair_case(desk, siblings=True)
    originals = []
    def model(ctx):
        if not ctx.get('validation_feedback'):
            value = first_result(case, ctx)
            originals.append(deepcopy(value))
            return paired(value)
        return {'schema_version': '2.2', 'evidence_repairs': [{'index': 0,
            'add_evidence': {'price': [case.anchor['message_id']]},
            'add_segments': [{'message_id': case.anchor['message_id'], 'text': case.anchor['fragment']}]}]}
    endpoint = SyntheticDify('analysis', model, version)
    case.flows.analyze = endpoint.client.call
    result = run_analysis(case)
    assert result['facts_committed'] and not result['rejected'], result
    assert len(endpoint.calls) == 2 and result['repair']['succeeded']
    assert endpoint.outputs[0] == originals[0]
    assert endpoint.outputs[1] == repaired(case, originals[0])
    assert current(case.app, case.peer).price.value == '3.20'
    assert current(case.app, case.peer).volume_wan == '500'
    assert current(case.app, case.sibling).price.value == '3.10'
    assert run_analysis(case) is None
    assert len(desk[3].sent) == 1


@pytest.mark.parametrize('invalid_source', ['known_nonnumeric', 'other_bond', 'retracted'])
def test_decoded_patch_still_fails_real_evidence_semantics(desk, invalid_source):
    decoder()
    case = repair_case(desk, siblings=True, retracted=invalid_source == 'retracted')
    def model(ctx):
        if not ctx.get('validation_feedback'): return first_result(case, ctx)
        anchor_id, fragment = case.anchor['message_id'], case.anchor['fragment']
        if invalid_source == 'known_nonnumeric': anchor_id, fragment = case.warm, 'SYN-A 稍后继续沟通'
        elif invalid_source == 'other_bond':
            fragment = next(line for line in case.anchor['text'].splitlines() if 'SYN-B' in line)
        return {'schema_version': '2.2', 'evidence_repairs': [{'index': 0,
            'add_evidence': {'price': [anchor_id]},
            'add_segments': [{'message_id': anchor_id, 'text': fragment}]}]}
    endpoint = SyntheticDify('analysis', model)
    case.flows.analyze = endpoint.client.call
    result = run_analysis(case)
    assert len(endpoint.calls) == 2
    assert_baseline(case, result)


@pytest.mark.parametrize('change', ['interpretation', 'price'])
def test_legacy_full_a_repair_is_not_projected_to_hide_unauthorized_changes(desk, change):
    decoder()
    case = repair_case(desk, siblings=True)
    def model(ctx):
        value = first_result(case, ctx)
        if not ctx.get('validation_feedback'): return value
        value = repaired(case, value)
        if change == 'interpretation': value['observations'][0]['interpretation']['reason'] += ' changed'
        else: value['observations'][0]['changes']['price']['value'] = '3.19'
        return value
    endpoint = SyntheticDify('analysis', model)
    case.flows.analyze = endpoint.client.call
    result = run_analysis(case)
    assert len(endpoint.calls) == 2
    assert_baseline(case, result)


@pytest.mark.parametrize('failure', ['extra_price', 'self_original', 'no_feedback', 'wrong_stage', 'mixed_full'])
def test_text_patch_has_no_authority_over_any_nontext_field(failure):
    decode = decoder()
    ctx = {'schema_version': '2.2', 'stage': 'reply', 'member_id': 'a',
        'validation_feedback': {'mode': 'TEXT_ONLY', 'attempt': 1, 'max_attempts': 1,
            'reason': 'Public yield text differs from validated quote', 'original_response': response()}}
    patch = {'schema_version': '2.2', 'text_repair': {'text': FIXED_TEXT}}
    if failure == 'extra_price': patch['text_repair']['our_price'] = {'kind': 'YIELD', 'value': '3.18'}
    elif failure == 'self_original': patch['original_response'] = response()
    elif failure == 'no_feedback': ctx.pop('validation_feedback')
    elif failure == 'wrong_stage': ctx['stage'] = 'analysis'
    else: patch.update(response())
    with pytest.raises((ValueError, TypeError, KeyError)):
        decode(patch, ctx, 'reply')


@pytest.mark.parametrize('mode', ['valid_patch', 'invalid_direction', 'legacy_changed_price'])
def test_real_reply_keeps_facts_economics_and_original_guard_after_wire_decoding(desk, mode, monkeypatch):
    decoder()
    case = reply_case(desk)
    # This archived free-proposal wire remains supported only explicitly;
    # the current policy's ordinary SEND requires a source-bound reply_plan.
    monkeypatch.setattr(case.app.state.negotiation_engine, 'context_policy_revision', 'explicit_anchors_v0')
    def model(ctx):
        if not ctx.get('validation_feedback'): return response()
        if mode == 'legacy_changed_price':
            value = response(); value['proposal']['our_price']['value'] = '3.18'
            value['proposal']['text'] = 'SYN-A 3.18%收500万'
            return value
        return {'schema_version': '2.2', 'text_repair': {
            'text': FIXED_TEXT if mode == 'valid_patch' else 'SYN-A 3.19%出500万'}}
    endpoint = SyntheticDify('reply', model)
    case.flows.decide = endpoint.client.call
    result = run_reply(case)
    assert len(endpoint.calls) == 2
    if mode == 'valid_patch':
        assert result['state'] == 'QUEUED' and result['repair']['succeeded']
        assert len(replies(case)) == 1
        body = json.loads(replies(case)[0]['body_json'])
        assert body['text'] == FIXED_TEXT and body['our_price']['value'] == '3.19'
        expected = response(); expected['proposal']['text'] = FIXED_TEXT
        assert endpoint.outputs[1] == expected
        assert run_reply(case) is None
    else:
        assert result['state'] == 'REJECTED' and not replies(case)
    unchanged_facts(case)
    assert len(case.native.sent) == 1, 'Wire tests never dispatch the repaired candidate'
