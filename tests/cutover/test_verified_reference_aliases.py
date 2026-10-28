"""Canonical public reference spellings reuse existing verified macro authority."""
from copy import deepcopy
import json

import pytest

from test_verified_literal_fact_binding import quantity_context, reply, validated, assert_same_macro
from test_rendered_reply_plan_revision import with_public, public_record
from server.negotiation.reply_render import public_information
from server.negotiation.prices import InvalidFact
from server.negotiation.chatflow import FlowUnavailable
from server.negotiation.slots import direct_priced_calculation
from dify.negotiation_v22.prompts import select_system_prompt
from test_direct_calculation_route_revision import DIRECT
from test_dify_output_contract_recovery import make_client, http_error, generated_error_carrier, sent_context


@pytest.mark.parametrize('value', ['3.10', '-0.05'])
def test_current_canonical_peer_yield_literal_is_exactly_the_existing_peer_quote_reference(value):
    context = quantity_context()
    context['quotes']['ofr']['price']['value'] = value
    context['context'][0]['text'] = 'SYN-A '+value+'出500万'
    before = deepcopy(context)
    assert_same_macro(context,
        reply('你刚才'+value+'%出，我这边{own_quote}，你再看看？'),
        reply('你刚才{peer_quote}，我这边{own_quote}，你再看看？'))
    assert context == before


def test_only_a_real_public_information_id_without_namespace_is_equivalent_to_its_canonical_slot():
    context, information = with_public('issuer_info')
    chosen = information[0]['id']
    assert_same_macro(context,
        reply('我查到{'+chosen+'}，这边{own_quote}，你再看看？'),
        reply('我查到{info:'+chosen+'}，这边{own_quote}，你再看看？'))


def test_exact_current_public_information_digest_with_namespace_equals_the_original_complete_id():
    context, information = with_public('issuer_info')
    chosen=information[0]['id']
    assert chosen.startswith('info-') and len(chosen[5:])==24
    assert_same_macro(context,
        reply('我查到{info:'+chosen[5:]+'}，这边{own_quote}，你再看看？'),
        reply('我查到{info:'+chosen+'}，这边{own_quote}，你再看看？'))


def test_the_original_source_correct_peer_price_and_whole_lot_negative_is_now_an_equivalent_positive():
    assert_same_macro(quantity_context(),
        reply('{own_quote}，你刚才3.10%出500万。'),
        reply('{own_quote}，你刚才{peer_quote}{peer_volume}。'))


@pytest.mark.parametrize('literal', ['3.11%出', '3.10%收', '3.1%出', '3.10出', '我报3.10%出'])
def test_yield_alias_does_not_guess_a_wrong_value_format_direction_or_speaker(literal):
    with pytest.raises((ValueError, InvalidFact)):
        validated(quantity_context(), reply('你刚才'+literal+'，这边{own_quote}。'))


def test_unknown_info_id_still_cannot_publish_a_fact():
    context, _ = with_public('issuer_info')
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('我查到{info-'+'0'*24+'}，{own_quote}。'))


def test_info_namespace_sugar_cannot_bypass_the_original_virtual_plan_length_limit():
    context, records = with_public('bond_info')
    chosen = next(row for row in records if row['source']['field'] == 'identity')['id']
    literal = reply(('{'+chosen+'}')*44+'{own_quote}')
    macro = reply(('{info:'+chosen+'}')*44+'{own_quote}')
    assert len(literal['text']) <= 1500 < len(macro['text'])
    with pytest.raises((ValueError, InvalidFact)): validated(context, macro)
    with pytest.raises((ValueError, InvalidFact)): validated(context, literal)


def unavailable_candidate():
    return {'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','question':'NONE',
        'text':'你刚才{peer_quote}，能再确认一下吗？'}}


def test_withdrawn_quote_remains_rejected_but_its_exact_carrier_can_correct_once(monkeypatch):
    context = quantity_context()
    context.update(schema_version='2.2',stage='reply')
    context['quotes']['ofr']['validity'] = 'WITHDRAWN'
    before = deepcopy(context)
    with pytest.raises(ValueError,match='^Counterparty reference is not an actual quotation$'):
        validated(context, unavailable_candidate()['reply_plan'])
    body = generated_error_carrier(context, unavailable_candidate())
    safe_plan = {'kind':'CLARIFY','question':'NONE','text':'请确认你本人现在的报价和可出整量好吗？'}
    rendered, _ = validated(context, safe_plan)
    expected = {'schema_version':'2.2','action':'SEND','proposal':rendered['proposal']}
    response = json.dumps({'answer':json.dumps(expected,ensure_ascii=False)}).encode()
    client, opener, _clock = make_client(monkeypatch, [(http_error(body),.1),(response,.1)])
    assert client.call(context) == expected and context == before
    assert len(opener.calls) == 2
    assert sent_context(opener.calls[1])['validation_feedback']['reason_code'] == 'DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT'
    assert sent_context(opener.calls[1])['validation_feedback']['previous_output'] == unavailable_candidate()


def test_same_unavailable_slot_on_correction_fails_closed_after_two_requests(monkeypatch):
    context = quantity_context(); context.update(schema_version='2.2',stage='reply')
    context['quotes']['ofr']['validity'] = 'WITHDRAWN'
    body = generated_error_carrier(context, unavailable_candidate())
    client, opener, _clock = make_client(monkeypatch, [(http_error(body),.1),(http_error(body),.1)])
    with pytest.raises(FlowUnavailable): client.call(context)
    assert len(opener.calls) == 2


def test_no_real_calculation_result_never_authorizes_a_public_ten_bp_claim():
    context = quantity_context()
    context['tool_results'] = [{'request':{'name':'bond_calc','arguments':{}},
        'result':{'status':'unavailable','data':{'method':'yield_gap','result':{}}}}]
    assert not any(row['source']['tool']=='bond_calc' for row in public_information(context))
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，两者差10BP。'))
    context['tool_results'] = [public_record('bond_calc')]
    info = next(row for row in public_information(context) if row['source']['tool']=='bond_calc')
    assert_same_macro(context, reply('{'+info['id']+'}，{own_quote}。'),
                      reply('{info:'+info['id']+'}，{own_quote}。'))


def calc_context():
    context = quantity_context(); context.update(schema_version='2.2',stage='reply')
    context['context'].append({'message_id':'synthetic-current-calculation','direction':'in',
        'historical':False,'ingest_revision':22,'text':DIRECT})
    return context


def test_real_latest_inbound_calculation_only_prompts_a_tool_and_does_not_invent_a_result():
    context = calc_context(); before = deepcopy(context)
    assert direct_priced_calculation({'members':[{'member_id':context['member_id'],'bond_code':context['bond_code']}],
                                     'messages':[context['context'][-1]]})
    first_line = select_system_prompt(context,'reply').splitlines()[0]
    assert 'bond_calc' in first_line and 'requests-only' in first_line
    assert context == before and context.get('tool_results',[]) == []
    assert not any(row['source']['tool']=='bond_calc' for row in public_information(context))


@pytest.mark.parametrize('change', ['attempted', 'unavailable', 'old_question'])
def test_attempted_calculation_or_old_history_does_not_force_the_same_tool_again(change):
    context = calc_context()
    if change == 'old_question':
        context['context'].append({'message_id':'newer-ack','direction':'in','historical':False,
            'ingest_revision':23,'text':'稍等'})
    else:
        context['tool_results'] = [{'request':{'name':'bond_calc','arguments':{}},
            'result':{'status':'ok' if change=='attempted' else 'unavailable','data':{}}}]
    baseline = deepcopy(context)
    baseline['context'][-1]['text'] = '稍等'
    assert select_system_prompt(context,'reply') == select_system_prompt(baseline,'reply')
