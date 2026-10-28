"""Source-bound prompt responsibilities through actual generated closures."""
from copy import deepcopy
import json
import pytest

from dify.negotiation_v22 import prompts
from dify.negotiation_v22.build import build
from server.negotiation.engine import Engine
from server.negotiation.reply_render import render_reply_plan
from test_verified_literal_fact_binding import cash_context, settlement, quantity_context, reply
from test_dify_output_contract_recovery import (
    generated_error_carrier, structured_payload, make_client, http_error, sent_context)


def generated(stage,version):
    nodes={row['id']:row['data'] for row in build(stage,version=version,
        provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
    projection,contract={},{}
    exec(nodes['context']['code'],projection);exec(nodes['contract']['code'],contract)
    return projection['main'],contract['main']


def correction(context,candidate,version):
    context={**deepcopy(context),'schema_version':'2.2','stage':'reply'}
    carrier=generated_error_carrier(context,candidate,version=version)
    payload=structured_payload(carrier)
    context['validation_feedback']={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,
        'reason_code':payload['reason_code'],'previous_output':payload['previous_output']}
    return context,carrier


def decode(contract,context,plan):
    return json.loads(contract(json.dumps({'schema_version':'2.2','reply_plan':plan}),
                               json.dumps(context))['result'])


def test_settlement_prerequisite_selects_a_nonquote_role_without_the_full_negotiation_role():
    context=cash_context();context.update(schema_version='2.2',stage='reply')
    before=deepcopy(context)
    for version in ('0.15.3','1.17.1'):
        project,contract=generated('reply',version)
        output=project(json.dumps(context,ensure_ascii=False))
        assert prompts.REPLY not in output['system_prompt'], 'ASK_SETTLEMENT must not inherit the ordinary price-negotiation role'
        assert 'ASK_SETTLEMENT' in output['system_prompt']
        candidate={'schema_version':'2.2','reply_plan':settlement('请问您是按什么结算方式报价的？')}
        decoded=json.loads(contract(json.dumps(candidate),json.dumps(context))['result'])
        assert decoded['proposal']['kind']=='CLARIFY' and not decoded['proposal'].get('our_price')
    assert context==before


def test_actual_wrong_own_direction_selects_the_authorized_actor_direction_repair_task():
    base=quantity_context();candidate={'schema_version':'2.2','reply_plan':reply('这边{own_quote}出，你再看看？')}
    for version in ('0.15.3','1.17.1'):
        context,_carrier=correction(base,candidate,version);before=deepcopy(context)
        assert context['validation_feedback']['reason_code']=='DIFY_MODEL_CONTRACT_SLOTS'
        project,contract=generated('reply',version)
        system=project(json.dumps(context))['system_prompt']
        assert any('own_quote' in line and 'BUY' in line and '收' in line and '方向' in line
                   for line in system.splitlines()), 'The genuine direction rejection must state the current own actor direction'
        with pytest.raises(ValueError):render_reply_plan(context,candidate['reply_plan'])
        answer=decode(contract,context,reply('这边{own_quote}，你再看看？'))
        assert Engine.__new__(Engine)._validate_reply(context,answer['proposal'])['our_price']['value']=='3.15'
        assert context==before


def test_settlement_option_repair_uses_its_actual_question_permission_instead_of_preserving_a_bad_slot():
    base=quantity_context();candidate={'schema_version':'2.2','reply_plan':reply('这边{own_quote}，结算是{settlement_options}？')}
    for version in ('0.15.3','1.17.1'):
        context,_carrier=correction(base,candidate,version);before=deepcopy(context)
        project,contract=generated('reply',version)
        system=project(json.dumps(context))['system_prompt']
        assert any('settlement_options' in line and 'ASK_SETTLEMENT' in line
                   and any(word in line for word in ('仅','只在','只能','只允许'))
                   for line in system.splitlines()), 'The rejected slot is question-bound, not an unavailable counterparty quotation'
        with pytest.raises(ValueError):render_reply_plan(context,candidate['reply_plan'])
        answer=decode(contract,context,reply('这边{own_quote}，你再看看？'))
        assert answer['proposal']['kind']=='REPLY' and answer['proposal']['render_plan']['question']=='NONE'
        assert Engine.__new__(Engine)._validate_reply(context,answer['proposal'])['our_price']['value']=='3.15'
        legitimate=decode(contract,context,settlement('请问结算是{settlement_options}？'))
        assert legitimate['proposal']['kind']=='CLARIFY' and not legitimate['proposal'].get('our_price')
        assert context==before


def test_wrong_own_slot_in_cash_clarification_stays_rejected_and_can_be_removed_without_changing_nonquote_kind():
    base=cash_context();candidate={'schema_version':'2.2','reply_plan':settlement('请问{own_quote}是按什么结算方式报价的？')}
    for version in ('0.15.3','1.17.1'):
        context,_carrier=correction(base,candidate,version);before=deepcopy(context)
        project,contract=generated('reply',version)
        system=project(json.dumps(context))['system_prompt']
        assert prompts.REPLY not in system
        assert 'own_quote' in system and any(word in system for word in ('非报价','CLARIFY'))
        with pytest.raises(ValueError,match='Nonquote plan cannot insert an own price'):
            render_reply_plan(context,candidate['reply_plan'])
        answer=decode(contract,context,settlement('请问您是按什么结算方式报价的？'))
        assert answer['proposal']['kind']=='CLARIFY' and not answer['proposal'].get('our_price')
        assert Engine.__new__(Engine)._validate_reply(context,answer['proposal'])['settlement_question']['side']=='ofr'
        assert context==before


def test_nonquote_settlement_role_retains_all_public_tool_entrypoints_and_the_full_twenty_raw_rows():
    context=cash_context();context.update(schema_version='2.2',stage='reply')
    context['context'] += [{'message_id':'synthetic-history-'+str(i),'direction':'in',
        'text':'原始历史  '+str(i)+'\n不得裁剪','ingest_revision':22+i,'trade_day':context['trade_day']}
        for i in range(19)]
    before=deepcopy(context)
    tools={'bond_info':{'schema_version':'1','mode':'get','bond_id':'SYN-A'},
        'issuer_info':{'schema_version':'1','mode':'get','issuer_id':'SYN-ISSUER'},
        'similar_bonds':deepcopy(prompts.SIMILAR_EXAMPLE['arguments']),
        'bond_calc':deepcopy(prompts.CALC_EXAMPLE['arguments'])}
    for version in ('0.15.3','1.17.1'):
        project,contract=generated('reply',version)
        output=project(json.dumps(context,ensure_ascii=False));projected=json.loads(output['context'])
        assert [row['text'] for row in projected['context']]==[row['text'] for row in before['context']]
        assert len(projected['context'])==20 and projected['quotes']==before['quotes']
        for name,args in tools.items():
            assert name in output['system_prompt']
            request={'schema_version':'2.2','tool_requests':[{'name':name,'arguments':args}]}
            assert json.loads(contract(json.dumps(request),json.dumps(context))['result'])==request
    assert context==before


def test_precise_repair_preserves_private_candidate_and_original_same_context_two_request_cap(monkeypatch):
    base=quantity_context();base.update(schema_version='2.2',stage='reply')
    candidate={'schema_version':'2.2','reply_plan':reply('这边{own_quote}出，你再看看？')}
    carrier=generated_error_carrier(base,candidate);before=deepcopy((base,candidate))
    _project,contract=generated('reply','1.17.1')
    answer=decode(contract,base,reply('这边{own_quote}，你再看看？'))
    client,opener,_clock=make_client(monkeypatch,[(http_error(carrier),.1),
        (json.dumps({'answer':json.dumps(answer)}).encode(),.1)])
    assert client.call(base)==answer and len(opener.calls)==2
    second=sent_context(opener.calls[1]);feedback=second.pop('validation_feedback')
    assert feedback['previous_output']==candidate and second==base
    assert (base,candidate)==before
    client,opener,_clock=make_client(monkeypatch,[(http_error(carrier),.1),(http_error(carrier),.1)])
    from server.negotiation.chatflow import FlowUnavailable
    with pytest.raises(FlowUnavailable):client.call(base)
    assert len(opener.calls)==2 and (base,candidate)==before


def test_reference_confirmation_and_sent_waiting_use_nonquote_roles_without_losing_wait_reask_or_tools():
    from server.negotiation.reply_render import reply_requirements
    for state in ('reference','waiting'):
        context=quantity_context();context.update(schema_version='2.2',stage='reply')
        if state=='reference':context['reference_confirmation_required']=True
        else:context.update(reconfirm_day=context['trade_day'],reconfirmation_waiting=True,reconfirmation_sent_at=1792202400)
        before=deepcopy(context)
        for version in ('0.15.3','1.17.1'):
            project,contract=generated('reply',version)
            output=project(json.dumps(context));view=json.loads(output['context'])
            assert prompts.REPLY not in output['system_prompt'], 'An already-required nonquote decision must not inherit positive ordinary quotation examples'
            assert view['reply_requirements']==reply_requirements(before)
            assert view['quotes']==before['quotes'] and view['boundary']==before['boundary'] and view['plan']==before['plan']
            assert all(name in output['system_prompt'] for name in ('bond_info','issuer_info','similar_bonds','bond_calc'))
            request={'schema_version':'2.2','tool_requests':[{'name':'issuer_info','arguments':{
                'schema_version':'1','mode':'get','issuer_id':'SYN-ISSUER'}}]}
            assert json.loads(contract(json.dumps(request),json.dumps(context))['result'])==request
            if state=='waiting':
                waiting={'schema_version':'2.2','action':'WAIT'}
                assert json.loads(contract(json.dumps(waiting),json.dumps(context))['result'])==waiting
                plan={'kind':'RECONFIRM','question':'ASK_CURRENT_QUOTE','text':'请再确认您本人现在的报价和整量？'}
            else:
                plan={'kind':'CLARIFY','question':'ASK_CURRENT_QUOTE','text':'您之前{peer_quote}，请确认本人现在的报价和整量？'}
            answer=decode(contract,context,plan)
            assert answer['proposal']['kind']==plan['kind'] and not answer['proposal'].get('our_price')
            assert Engine.__new__(Engine)._validate_reply(context,answer['proposal'])['kind']==plan['kind']
        assert context==before


def test_nonsettlement_nonquote_also_gets_question_scoped_settlement_option_correction_without_changing_private_draft():
    base=quantity_context();base['reference_confirmation_required']=True
    candidate={'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','question':'ASK_CURRENT_QUOTE',
        'text':'请确认您本人现在的报价和整量，结算是{settlement_options}？'}}
    for version in ('0.15.3','1.17.1'):
        context,_carrier=correction(base,candidate,version);before=deepcopy(context)
        project,contract=generated('reply',version)
        output=project(json.dumps(context));system=output['system_prompt']
        assert any('settlement_options' in line and 'ASK_SETTLEMENT' in line
                   and any(word in line for word in ('仅','只在','只能','只允许'))
                   for line in system.splitlines()), 'The slot permission depends on the actual question, including nonquote drafts'
        expected_feedback=deepcopy(before['validation_feedback'])
        del expected_feedback['previous_output']['reply_plan']['text']
        assert json.loads(output['context'])['validation_feedback']==expected_feedback
        assert context['validation_feedback']==before['validation_feedback']
        with pytest.raises(ValueError):render_reply_plan(context,candidate['reply_plan'])
        good={'kind':'CLARIFY','question':'ASK_CURRENT_QUOTE','text':'请确认您本人现在的报价和整量？'}
        answer=decode(contract,context,good)
        assert answer['proposal']['kind']=='CLARIFY' and not answer['proposal'].get('our_price')
        assert answer['proposal']['render_plan']['question']=='ASK_CURRENT_QUOTE'
        assert Engine.__new__(Engine)._validate_reply(context,answer['proposal'])['kind']=='CLARIFY'
        assert context==before
