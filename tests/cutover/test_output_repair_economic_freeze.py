"""A controlled output correction preserves a legal plan, not only hard limits."""
from copy import deepcopy
import json
import traceback

import pytest

from test_reply_output_repair_projection import context_with_feedback,generated
from server.negotiation.engine import Engine
from server.negotiation.reply_render import render_reply_plan


def plan(value='1.55',text='这边{own_quote}，请再看看？',**fields):
    return {'kind':'REPLY','question':'NONE','quote_value':value,'text':text,**fields}


def correction(reason='SLOTS'):
    context=context_with_feedback()
    if reason=='UNAVAILABLE':
        context['validation_feedback']['reason_code']='DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT'
        context['validation_feedback']['previous_output']['reply_plan']['text']='这边{own_quote}，{info:unavailable-synthetic}。'
    return context


def ref(context):
    return render_reply_plan(context,plan())['proposal']['render_plan']['context_ref']


def individually_legal(context,value):
    proposal=render_reply_plan(context,value)['proposal']
    return Engine.__new__(Engine)._validate_reply(context,deepcopy(proposal))


@pytest.mark.parametrize('reason',['SLOTS','UNAVAILABLE'])
@pytest.mark.parametrize('change',['price','price_spelling','kind','question','ref_add','ref_drop'])
def test_nonboundary_output_correction_cannot_change_quoted_fields_or_upgrade_nonquote(reason,change):
    context=correction(reason);candidate=plan()
    previous=context['validation_feedback']['previous_output']['reply_plan']
    if change=='price':candidate['quote_value']='1.56'
    elif change=='price_spelling':candidate['quote_value']='1.5500'
    elif change=='kind':
        # A malformed nonquote must not gain authority to issue a new own price.
        previous.update(kind='CLARIFY',question='NONE')
        previous.pop('quote_value',None)
    elif change=='question':candidate.update(question='NEGOTIATE',text='这边{own_quote}，这个报价可以再看看吗？')
    elif change=='ref_add':candidate['context_ref']=ref(context)
    else:previous['context_ref']=ref(context)
    individually_legal(context,candidate)
    before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        _project,decode=generated(version)
        with pytest.raises(ValueError):decode(json.dumps({'schema_version':'2.2','reply_plan':candidate}),json.dumps(context))
    assert context==before


@pytest.mark.parametrize('previous_explicit,candidate_explicit',[(False,True),(True,False),(True,True)])
def test_effective_none_question_and_unchanged_context_ref_remain_legal(previous_explicit,candidate_explicit):
    context=correction();previous=context['validation_feedback']['previous_output']['reply_plan']
    previous['context_ref']=ref(context)
    if not previous_explicit:previous.pop('question',None)
    candidate=plan(context_ref=previous['context_ref'])
    if not candidate_explicit:candidate.pop('question')
    before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        _project,decode=generated(version)
        result=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':candidate}),json.dumps(context))['result'])
        assert result['proposal']['render_plan']['context_ref']==previous['context_ref']
        assert result['proposal']['our_price']['value']=='1.55'
    assert context==before



def test_reply_can_conservatively_clarify_settlement_without_issuing_a_new_own_quote():
    from test_prompt_mode_responsibilities import correction as sourced_correction
    from test_verified_literal_fact_binding import quantity_context, reply, settlement
    base=quantity_context()
    malformed={'schema_version':'2.2','reply_plan':reply('这边{own_quote}，结算是{settlement_options}？')}
    for version in ['0.15.3','1.17.1']:
        context,_carrier=sourced_correction(base,malformed,version)
        before=deepcopy(context)
        candidate=settlement('请问结算是{settlement_options}？')
        expected=individually_legal(context,candidate)
        assert expected['kind']=='CLARIFY' and not expected.get('our_price')
        _project,decode=generated(version)
        fixed=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':candidate}),json.dumps(context))['result'])
        assert Engine.__new__(Engine)._validate_reply(context,fixed['proposal'])==expected
        assert 'quote_value' not in fixed['proposal']['render_plan']
        # Downgrading action does not declassify a new price, policy or commitment.
        for bad in [dict(candidate,quote_value='3.15'),dict(candidate,text='请问{own_quote}是按什么结算方式报价的？'),
                    dict(candidate,text='结算是{settlement_options}吗，我给您锁量。')]:
            with pytest.raises(ValueError):decode(json.dumps({'schema_version':'2.2','reply_plan':bad}),json.dumps(context))
        assert context==before


def test_boundary_can_correct_illegal_price_and_nonquote_own_error_can_delete_illegal_price():
    from test_dify_output_contract_recovery import failed_candidate,REASONS
    context=correction();context['validation_feedback'].update(reason_code='DIFY_MODEL_CONTRACT_BOUNDARY',
        previous_output=failed_candidate(REASONS[1][0]))
    nonquote=correction();nonquote['validation_feedback'].update(reason_code='DIFY_MODEL_CONTRACT_OWN_QUOTE',
        previous_output={'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','quote_value':'1.55',
            'question':'NONE','text':'请确认您当前的报价？'}})
    before=deepcopy((context,nonquote))
    for version in ['0.15.3','1.17.1']:
        _project,decode=generated(version)
        fixed=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':plan()}),json.dumps(context))['result'])
        assert individually_legal(context,plan())['our_price']['value']=='1.55'
        assert fixed['proposal']['render_plan']['quote_value']=='1.55'
        safe={'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','question':'NONE','text':'请确认您本人现在的报价和整量？'}}
        fixed=json.loads(decode(json.dumps(safe),json.dumps(nonquote))['result'])
        assert fixed['proposal']['kind']=='CLARIFY' and not fixed['proposal'].get('our_price')
        assert 'quote_value' not in fixed['proposal']['render_plan']
    assert (context,nonquote)==before


def test_wait_handoff_and_requests_only_keep_original_wire_forms():
    context=correction();before=deepcopy(context)
    request={'schema_version':'2.2','tool_requests':[{'name':'issuer_info','arguments':{
        'schema_version':'1','mode':'get','issuer_id':'SYN-ISSUER'}}]}
    for version in ['0.15.3','1.17.1']:
        _project,decode=generated(version)
        assert json.loads(decode(json.dumps(request),json.dumps(context))['result'])==request
        for action in ['WAIT','HANDOFF']:
            result=json.loads(decode(json.dumps({'schema_version':'2.2','action':action}),json.dumps(context))['result'])
            assert result['action']==action and not result.get('proposal')
    assert context==before


def test_unauthorized_second_plan_drift_stops_at_original_two_request_cap(monkeypatch):
    from test_dify_output_contract_recovery import valid_context,failed_candidate,generated_error_carrier
    from test_dify_output_contract_recovery import Clock,Response,http_error
    from server.negotiation.chatflow import ChatflowClient,FlowUnavailable
    context=valid_context();before=deepcopy(context);clock=Clock()
    monkeypatch.setattr('server.negotiation.chatflow.time.sleep',clock.sleep)
    _project,decode=generated('1.17.1');bad=failed_candidate()
    class Opener:
        calls=[]
        def open(self,request,timeout):
            self.calls.append(json.loads(request.data));clock.sleep(.1);assert len(self.calls)<=2
            if len(self.calls)==1:raise http_error(generated_error_carrier(context,bad))
            current=json.loads(self.calls[-1]['inputs']['context_json'])
            try:result=decode(json.dumps({'schema_version':'2.2','reply_plan':plan('1.56')}),json.dumps(current))['result']
            except ValueError:
                raise http_error({'code':'invalid_param','status':400,'message':'Run failed: '+traceback.format_exc()+'error: exit status 255\n'})
            return Response(json.dumps({'answer':result}).encode())
    opener=Opener();client=ChatflowClient('https://synthetic.invalid','synthetic-not-credential',stage='reply',timeout=5,opener=opener,clock=clock)
    with pytest.raises(FlowUnavailable):client.call(context)
    assert len(opener.calls)==2 and client.last_evidence['request_attempts']==2 and context==before
