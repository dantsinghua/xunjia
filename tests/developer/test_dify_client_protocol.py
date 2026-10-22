"""Current A/B App client contract; no old AgentTurn or streaming compatibility."""
import io
import json
import urllib.error
import pytest
from server.negotiation.chatflow import ChatflowClient, FlowUnavailable, _NoRedirect

class Response(io.BytesIO):
    status=200

class Opener:
    def __init__(self,body):self.body=body;self.calls=[]
    def open(self,request,timeout):
        self.calls.append((request,timeout))
        if isinstance(self.body,Exception):raise self.body
        return Response(self.body)

def make(answer=None, *, raw=None, stage='analysis'):
    if raw is None:
        raw=json.dumps({'answer':answer or json.dumps({'schema_version':'2.2','observations':[]}),
                        'message_id':'synthetic-message','workflow_run_id':'synthetic-run'}).encode()
    opener=Opener(raw)
    return ChatflowClient('https://dify.example.invalid/v1','synthetic-app-secret',stage=stage,opener=opener),opener

@pytest.mark.parametrize('stage',['analysis','reply'])
def test_stage_receives_complete_versioned_context_without_conversation_memory(stage):
    client,opener=make(stage=stage)
    value=client.call({'thread_id':'synthetic-thread','revision':7,'messages':[{'text':'synthetic current input'}]})
    request,timeout=opener.calls[0];body=json.loads(request.data);context=json.loads(body['inputs']['context_json'])
    assert request.full_url=='https://dify.example.invalid/v1/chat-messages'
    assert body['response_mode']=='blocking' and 'conversation_id' not in body
    assert context['schema_version']=='2.2' and context['stage']==stage and context['revision']==7
    assert context['messages']==[{'text':'synthetic current input'}]
    assert value=={'schema_version':'2.2','observations':[]}
    assert client.last_evidence['workflow_run_id']=='synthetic-run'
    assert 'synthetic-app-secret' not in str(client.last_evidence)
    assert len(opener.calls)==1

@pytest.mark.parametrize('answer',[
    '{"schema_version":"1"}', '[]', '{"x":NaN}',
    '{"schema_version":"2.2","schema_version":"2.2"}',
    '{"schema_version":"2.2","trader_view":{"gap":1e999}}',
    '```json\n{}', 'not-json'])
def test_malformed_nonfinite_or_retired_contract_never_returns_stage(answer):
    client,opener=make(answer)
    with pytest.raises(FlowUnavailable):client.call({})
    assert len(opener.calls)==1

@pytest.mark.parametrize('raw',[b'{',b'\xff',b'{}',b'data: {"event":"message_end"}\n\n'])
def test_incomplete_wrapper_or_retired_stream_is_not_a_stage_result(raw):
    client,opener=make(raw=raw)
    with pytest.raises(FlowUnavailable,match='DIFY_INVALID_STAGE_JSON'):client.call({})
    assert len(opener.calls)==1

def test_complete_json_fence_is_supported_by_current_contract():
    client,_=make('```json\n{"schema_version":"2.2","observations":[]}\n```')
    assert client.call({})['schema_version']=='2.2'

def test_timeout_is_sanitized_and_never_retried():
    client,opener=make(raw=TimeoutError('PRIVATE-URL-KEY'))
    with pytest.raises(FlowUnavailable,match='DIFY_TIMEOUT') as caught:client.call({})
    assert 'PRIVATE' not in str(caught.value) and len(opener.calls)==1

def test_bounded_response_read_rejects_oversized_body():
    client,opener=make(raw=b'x'*(2*1024*1024+1))
    with pytest.raises(FlowUnavailable,match='DIFY_RESPONSE_TOO_LARGE'):client.call({})
    assert len(opener.calls)==1

def test_redirect_refused_without_forwarding_app_credentials():
    failure=urllib.error.HTTPError('https://dify.example.invalid/v1/chat-messages',302,'redirect',{},io.BytesIO(b'{}'))
    client,opener=make(raw=failure)
    with pytest.raises(FlowUnavailable,match='DIFY_HTTP_ERROR'):client.call({})
    assert len(opener.calls)==1
    assert _NoRedirect().redirect_request(opener.calls[0][0],None,302,'redirect',{},'https://foreign.invalid') is None

def test_private_wrapper_logging_is_not_returned_as_model_output():
    client,_=make(raw=json.dumps({'answer':'{"schema_version":"2.2","observations":[]}',
                                 'agent_log':'PRIVATE-THOUGHT','message_id':'synthetic'}).encode())
    result=client.call({})
    assert 'PRIVATE' not in str(result)+str(client.last_evidence)
