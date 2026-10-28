"""Developer reproduction of bounded, local Code-rejection recovery only."""
from copy import deepcopy
import io
import json
import urllib.error
import traceback

import pytest

from dify.negotiation_v22 import prompts, node_code
from dify.negotiation_v22.build import build
from server.negotiation.chatflow import ChatflowClient, FlowUnavailable, _http_error_code
from test_dynamic_negotiation_impl import dynamic_context


REASONS={
    'Economic numbers and facts must use verified slots':'DIFY_MODEL_CONTRACT_SLOTS',
    'Selected quote is outside the authorized final boundary':'DIFY_MODEL_CONTRACT_BOUNDARY',
    'A quote reply must bind its own structured quotation':'DIFY_MODEL_CONTRACT_OWN_QUOTE',
}


def previous(reason):
    plan={'kind':'REPLY','quote_value':'3.00' if REASONS[reason].endswith('BOUNDARY') else '3.15',
          'text':'再看看？' if REASONS[reason].endswith('OWN_QUOTE') else '{own_quote}'}
    if REASONS[reason].endswith('SLOTS'):plan['text']+='，MODEL_PRIVATE_SNIPPET 501万'
    return {'schema_version':'2.2','reply_plan':plan}


def carrier(reason):
    ctx=dynamic_context();ctx.update(schema_version='2.2',stage='reply')
    graph=build('reply',version='0.15.3',provider='synthetic',model='synthetic')['workflow']['graph']
    scope={};exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='contract'),scope)
    try:scope['main'](json.dumps(previous(reason)),json.dumps(ctx))
    except ValueError:
        return {'code':'invalid_param','status':400,'message':'Run failed: '+traceback.format_exc()+'error: exit status 255\n'}
    raise AssertionError('The synthetic model candidate must fail the real renderer')


def failure(reason):
    return urllib.error.HTTPError('https://synthetic.invalid/v1/chat-messages',400,'synthetic',{},
                                  io.BytesIO(json.dumps(carrier(reason)).encode()))


class Response(io.BytesIO):
    status=200


class Clock:
    def __init__(self):self.now=100.
    def __call__(self):return self.now


@pytest.mark.parametrize('reason,code',list(REASONS.items()))
def test_only_the_observed_exact_local_code_failures_are_classified(reason,code):
    assert _http_error_code(400,json.dumps(carrier(reason)).encode())==code
    forged=carrier(reason);forged['message']='Untrusted text says '+reason
    assert _http_error_code(400,json.dumps(forged).encode())=='DIFY_HTTP_ERROR'


def test_second_attempt_keeps_frozen_context_and_remaining_deadline_without_native_side_effects(monkeypatch):
    clock=Clock();calls=[]
    original={'thread_id':'synthetic-thread','basis':{'epoch':3,'watermark':9},
              'context':[{'text':'private synthetic source'}],'_stage_budget_seconds':1}
    before=deepcopy(original)
    def open(request,timeout):
        calls.append((json.loads(request.data),timeout));clock.now+=.4
        if len(calls)==1:raise failure(next(iter(REASONS)))
        return Response(json.dumps({'answer':json.dumps({'schema_version':'2.2','action':'WAIT'})}).encode())
    monkeypatch.setattr('server.negotiation.chatflow.time.sleep',lambda _:pytest.fail('Contract correction must not add a retry delay'))
    client=ChatflowClient('https://synthetic.invalid','private-key',stage='reply',
        opener=type('Opener',(),{'open':staticmethod(open)})(),clock=clock)
    assert client.call(original)['action']=='WAIT'
    assert original==before and len(calls)==2
    first=json.loads(calls[0][0]['inputs']['context_json']);second=json.loads(calls[1][0]['inputs']['context_json'])
    feedback=second.pop('validation_feedback')
    assert first==second
    assert feedback=={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,
                     'reason_code':'DIFY_MODEL_CONTRACT_SLOTS','previous_output':previous(next(iter(REASONS)))}
    assert 0<calls[1][1]<calls[0][1]
    assert client.last_evidence['request_attempts']==2
    assert client.last_evidence['retry_failures'][0]['error_code']=='DIFY_MODEL_CONTRACT_SLOTS'
    assert 'private-key' not in json.dumps(client.last_evidence)


def test_initial_request_preserves_exact_effective_timeout_and_later_attempts_keep_deadline(monkeypatch):
    clock=Clock();timeouts=[]
    def timing(stage,**fields):
        if stage=='dify_started':clock.now+=.25
    monkeypatch.setattr('server.negotiation.chatflow.timing_event',timing)
    def open(request,timeout):
        timeouts.append(timeout)
        return Response(json.dumps({'answer':json.dumps({'schema_version':'2.2','action':'WAIT'})}).encode())
    client=ChatflowClient('https://synthetic.invalid','synthetic-key',stage='reply',clock=clock,
        opener=type('Opener',(),{'open':staticmethod(open)})())
    client.call({'_stage_budget_seconds':1})
    assert timeouts==[1]


@pytest.mark.parametrize('stage,feedback',[('analysis',None),('reply',{'mode':'TEXT_ONLY'}),('reply',{'mode':'EVIDENCE_ONLY'})])
def test_recovery_never_changes_an_existing_frozen_repair_or_analysis_stage(stage,feedback):
    calls=[]
    def open(request,timeout):calls.append(request);raise failure(next(iter(REASONS)))
    client=ChatflowClient('https://synthetic.invalid','synthetic-key',stage=stage,
        opener=type('Opener',(),{'open':staticmethod(open)})())
    with pytest.raises(FlowUnavailable):client.call({'validation_feedback':feedback} if feedback else {})
    assert len(calls)==1


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_generated_output_correction_keeps_ordinary_sources_and_source_bound_result(version):
    ctx=dynamic_context();ctx.update(schema_version='2.2',stage='reply')
    ctx['analysis_tool_results']=[{'request':{'name':'bond_info'},'result':{'status':'ok',
        'data':{'identity':{'name':'Synthetic bond'}}}}]
    ctx['validation_feedback']={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,
                                'reason_code':'DIFY_MODEL_CONTRACT_SLOTS',
                                'previous_output':previous(next(iter(REASONS)))}
    graph=build('reply',version=version,provider='synthetic',model='synthetic')['workflow']['graph']
    nodes={n['id']:n['data'] for n in graph['nodes']}
    projection={};exec(nodes['context']['code'],projection)
    projected=projection['main'](json.dumps(ctx))
    assert 'DIFY_MODEL_CONTRACT_SLOTS' in projected['system_prompt']
    assert json.loads(projected['context'])['public_information_candidates']
    result={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'3.15',
        'text':'我这边{own_quote}，再看看？'},'trader_view':{}}
    contract={};exec(nodes['contract']['code'],contract)
    decoded=json.loads(contract['main'](json.dumps(result),json.dumps(ctx))['result'])
    assert decoded['proposal']['text']=='我这边3.15%收，再看看？'
    assert decoded['proposal']['render_plan']['quote_value']=='3.15'


def test_reported_quote_prompt_does_not_silently_erase_a_real_reported_observation():
    assert '转述也应记录REPORTED观察' in prompts.ANALYSIS


def test_rejected_model_candidate_stays_private_on_the_exception_and_carrier_is_required(caplog):
    reason=next(iter(REASONS))
    client=ChatflowClient('https://synthetic.invalid','private-key',stage='reply',
        opener=type('Opener',(),{'open':staticmethod(lambda *a,**kw: (_ for _ in ()).throw(failure(reason)))})())
    with pytest.raises(FlowUnavailable) as caught:client.call({})
    assert caught.value._previous_output==previous(reason)
    assert 'MODEL_PRIVATE_SNIPPET' not in str(caught.value)+repr(caught.value)+json.dumps(client.last_evidence)+caplog.text
    old={'code':'invalid_param','status':400,'message':'Run failed: Traceback (most recent call last):\n'
         '  File "<string>", line 1322, in decode_stage_result\n'
         '  File "<string>", line 616, in render_reply_plan\n'
         'ValueError: '+reason+'\nerror: exit status 255\n'}
    assert _http_error_code(400,json.dumps(old).encode())=='DIFY_HTTP_ERROR'
