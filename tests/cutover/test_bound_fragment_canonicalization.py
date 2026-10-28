"""Only exact redundant bound-field spelling may compile to an old legal P*."""
from copy import deepcopy
import json

import pytest

from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from server.negotiation.chatflow import FlowUnavailable
from server.negotiation.engine import Engine
from dify.negotiation_v22.build import build
from test_verified_literal_fact_binding import quantity_context, reply, assert_same_macro, validated, cash_context, settlement
from test_rendered_reply_plan_revision import with_public
from test_dify_output_contract_recovery import make_client, http_error, generated_error_carrier, sent_context, structured_payload


@pytest.mark.parametrize('side,direction', [('BUY','收'),('SELL','出')])
def test_redundant_matching_direction_after_own_quote_is_the_original_canonical_macro_body(side, direction):
    assert_same_macro(quantity_context(side),
        reply('这边{own_quote}'+direction+'，你再看看？'),
        reply('这边{own_quote}，你再看看？'))


def test_the_same_redundant_peer_lot_unit_before_a_clear_question_is_not_multiplied():
    assert_same_macro(quantity_context(),
        reply('{own_quote}，{peer_volume}万可以吗？'),
        reply('{own_quote}，{peer_volume}可以吗？'))


@pytest.mark.parametrize('kind,label', [('NET_PRICE','净价'),('DIRTY_PRICE','全价')])
def test_matching_cash_type_and_peer_direction_around_the_bound_quote_equal_the_original_fragment(kind, label):
    assert_same_macro(cash_context(kind),
        settlement('你刚才'+label+'{peer_quote}出，结算是{settlement_options}？'),
        settlement('你刚才{peer_quote}，结算是{settlement_options}？'))


def test_redundant_matching_full_stop_after_an_authorized_fact_keeps_the_same_fact_body():
    context, records = with_public('bond_info')
    chosen = next(row for row in records if row['source']['field'] == 'identity')
    assert chosen['text'].endswith('。')
    token = '{info:'+chosen['id']+'}'
    assert_same_macro(context,
        reply('我查到'+token+'。这边{own_quote}，再看看？'),
        reply('我查到'+token+'这边{own_quote}，再看看？'))


@pytest.mark.parametrize('case', ['opposite_side','wrong_cash_type','larger_unit','unbound_double_unit'])
def test_unverified_or_opposite_suffixes_are_not_discarded_to_make_a_candidate_legal(case):
    context = quantity_context()
    plan = reply('{own_quote}出，你再看看？')
    if case == 'wrong_cash_type':
        context = cash_context()
        plan = settlement('全价{peer_quote}，结算是{settlement_options}？')
    elif case == 'larger_unit': plan = reply('{own_quote}，{peer_volume}亿可以吗？')
    elif case == 'unbound_double_unit': plan = reply('{own_quote}，500万万可以吗？')
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, plan)


def test_normal_words_after_punctuation_keep_their_original_characters():
    for side, text in [('BUY','{own_quote}，收到你的意思了。'),
                       ('SELL','{own_quote}，出差回来再说。'),
                       ('BUY','{own_quote}，{peer_volume}，万一有新量再说。')]:
        context = quantity_context(side)
        before = render_reply_plan(context, reply(text))['proposal']['text']
        body = validated(context, reply(text))[1]
        expected = text.replace('{own_quote}', '3.15%'+('收' if side=='BUY' else '出')).replace('{peer_volume}', '500万')
        assert before == body['text'] == expected


def test_an_ambiguous_unseparated_normal_word_is_preserved_or_rejected_never_eaten():
    for side, text in [('BUY','{own_quote}收到你的意思了。'),
                       ('SELL','{own_quote}出差回来再说。'),
                       ('BUY','{own_quote}，{peer_volume}万一有新量再说。')]:
        try: body = validated(quantity_context(side), reply(text))[1]
        except (ValueError, InvalidFact): continue
        expected = text.replace('{own_quote}', '3.15%'+('收' if side=='BUY' else '出')).replace('{peer_volume}', '500万')
        assert body['text'] == expected


def test_rerendering_the_audited_raw_plan_is_idempotent_and_keeps_its_original_spelling():
    context = quantity_context()
    plan = reply('{own_quote}收，{peer_volume}万可以吗？')
    before = deepcopy((context, plan))
    first = render_reply_plan(context, plan)
    second = render_reply_plan(context, first['proposal']['render_plan'])
    assert first == second and (context, plan) == before
    assert first['proposal']['render_plan']['text'] == plan['text']


def test_redundancy_folding_does_not_shrink_an_overlimit_raw_model_plan_into_permission():
    prefix = '{own_quote}收，'
    text = prefix+'嗯'*(1501-len(prefix))
    assert len(text) == 1501
    with pytest.raises((ValueError, InvalidFact)):
        validated(quantity_context(), reply(text))


def test_folding_never_clips_the_structured_price_to_the_boundary():
    plan = reply('{own_quote}收，你再看看？'); plan['quote_value'] = '3.11'
    with pytest.raises((ValueError, InvalidFact)):
        validated(quantity_context(), plan)


@pytest.mark.parametrize('bad_plan', [
    {'kind':'CLARIFY','question':'NONE','quote_value':'3.00','text':'请确认你本人当前报价和整量？'},
    {'kind':'CLARIFY','question':'NONE','text':'请确认{own_quote}是否可行？'},
])
def test_exact_nonquote_price_error_can_be_carried_once_without_local_price_removal_or_clamping(monkeypatch, bad_plan):
    context = quantity_context(); context.update(schema_version='2.2',stage='reply')
    before = deepcopy((context, bad_plan))
    with pytest.raises(ValueError,match='^Nonquote plan cannot insert an own price$'):
        render_reply_plan(context, bad_plan)
    candidate = {'schema_version':'2.2','reply_plan':bad_plan}
    body = generated_error_carrier(context, candidate)
    assert structured_payload(body)['reason_code'] == 'DIFY_MODEL_CONTRACT_OWN_QUOTE'
    assert structured_payload(body)['previous_output'] == candidate
    safe_plan = {'kind':'CLARIFY','question':'NONE','text':'请确认你本人当前报价和整量？'}
    expected = {'schema_version':'2.2','action':'SEND','proposal':render_reply_plan(context,safe_plan)['proposal']}
    raw = json.dumps({'answer':json.dumps(expected)}).encode()
    client, opener, _clock = make_client(monkeypatch, [(http_error(body),.1),(raw,.1)])
    assert client.call(context) == expected and len(opener.calls) == 2
    feedback = sent_context(opener.calls[1])['validation_feedback']
    assert feedback['mode'] == 'OUTPUT_CONTRACT' and feedback['previous_output'] == candidate
    assert expected['proposal'].get('our_price') is None and (context,bad_plan) == before


def test_repeating_the_nonquote_price_error_stops_after_the_same_two_request_cap(monkeypatch):
    context = quantity_context(); context.update(schema_version='2.2',stage='reply')
    candidate = {'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','quote_value':'3.00',
        'question':'NONE','text':'请确认你本人的报价？'}}
    body = generated_error_carrier(context, candidate)
    client, opener, _clock = make_client(monkeypatch, [(http_error(body),.1),(http_error(body),.1)])
    with pytest.raises(FlowUnavailable): client.call(context)
    assert len(opener.calls) == 2


def test_the_new_nonquote_reason_cannot_open_output_correction_inside_text_only(monkeypatch):
    context = quantity_context(); context.update(schema_version='2.2',stage='reply')
    candidate = {'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','quote_value':'3.00',
        'question':'NONE','text':'请确认你本人的报价？'}}
    body = generated_error_carrier(context, candidate)
    context['validation_feedback'] = {'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
        'reason':'Reply direction differs from our authorized side'}
    before = deepcopy(context)
    client, opener, _clock = make_client(monkeypatch, [(http_error(body),.1)])
    with pytest.raises(FlowUnavailable): client.call(context)
    assert len(opener.calls) == 1 and context == before


def test_both_generated_context_and_contract_closures_execute_correction_without_losing_authority():
    for version in ['0.15.3', '1.17.1']:
        context = quantity_context(); context.update(schema_version='2.2',stage='reply')
        candidate = {'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','question':'NONE',
            'quote_value':'3.00','text':'请确认你本人的报价和整量？'}}
        corrected = {**deepcopy(context),'validation_feedback':{'mode':'OUTPUT_CONTRACT','attempt':1,
            'max_attempts':1,'reason_code':'DIFY_MODEL_CONTRACT_OWN_QUOTE','previous_output':candidate}}
        before = deepcopy(corrected)
        graph = build('reply',version=version,provider='synthetic',model='synthetic')['workflow']['graph']
        nodes = {row['id']:row['data'] for row in graph['nodes']}
        projection, contract = {}, {}
        exec(nodes['context']['code'],projection); exec(nodes['contract']['code'],contract)
        projected = projection['main'](json.dumps(corrected))
        expected_feedback=deepcopy(corrected['validation_feedback'])
        del expected_feedback['previous_output']['reply_plan']['text']
        assert projected['system_prompt'] and json.loads(projected['context'])['validation_feedback'] == expected_feedback
        assert corrected==before
        ordinary = {'schema_version':'2.2','reply_plan':reply('{own_quote}，你再看看？')}
        ordinary_result = json.loads(contract['main'](json.dumps(ordinary),json.dumps(context))['result'])
        assert Engine.__new__(Engine)._validate_reply(context,ordinary_result['proposal'])['our_price']['value']=='3.15'
        model = {'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','question':'NONE',
            'text':'请确认你本人的报价和整量？'}}
        decoded = json.loads(contract['main'](json.dumps(model),json.dumps(corrected))['result'])
        body = Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])
        assert body['kind']=='CLARIFY' and body.get('our_price') is None and corrected==before
        bad = {'schema_version':'2.2','reply_plan':{**reply('{own_quote}'),'quote_value':'3.11'}}
        with pytest.raises(ValueError): contract['main'](json.dumps(bad),json.dumps(corrected))
        original = {'schema_version':'2.2','action':'SEND','proposal':render_reply_plan(context,
            reply('{own_quote}，我出给你。'))['proposal']}
        text_only = {**deepcopy(context),'validation_feedback':{'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
            'reason':'Reply direction differs from our authorized side','original_response':original}}
        selected = projection['main'](json.dumps(text_only))
        assert json.loads(selected['context'])['validation_feedback'] == text_only['validation_feedback']
        patch = {'schema_version':'2.2','text_repair':{'text':'{own_quote}，请再看看？'}}
        repaired = json.loads(contract['main'](json.dumps(patch),json.dumps(text_only))['result'])
        assert Engine.__new__(Engine)._validate_reply(context,repaired['proposal'])['our_price']['value']=='3.15'
