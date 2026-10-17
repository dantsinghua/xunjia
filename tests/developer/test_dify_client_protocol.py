import io
import importlib
import json
import urllib.error
import pytest


def proposal(**changes):
    return dict(schema_version='1',turn_id='turn-1',conversation_epoch=3,source_message_id='source-1',
        observations=[],proposed_actions=[{'type':'WAIT'}],public_message=None,trader_view={},
        memory_patch=None,history_request=None,validation_ref=None,**changes)


class Response(io.BytesIO):
    def __init__(self,body,content_type='application/json'):
        super().__init__(body); self.status=200;self.headers={'Content-Type':content_type}


@pytest.fixture
def module():
    assert importlib.util.find_spec('server.dify_client') is not None, 'Chat client not implemented'
    return importlib.import_module('server.dify_client')


def envelopes():
    common=dict(conversation_id='conversation-1',message_id='message-1',task_id='task-1')
    return [dict(common,event='workflow_started',workflow_run_id='run-1',data={'id':'run-1'}),
        dict(common,event='agent_log',data={'content':'SECRET-THOUGHT'}),
        dict(common,event='message',answer=json.dumps(proposal())),
        dict(common,event='workflow_finished',workflow_run_id='run-1',data={'id':'run-1','status':'succeeded'}),
        dict(common,event='message_end')]


def stream(events):
    return ''.join('data: '+json.dumps(e)+'\r\n\r\n' for e in events).encode()


def make(module,body,mode='streaming',**kwargs):
    calls=[]
    def opener(req,timeout):
        calls.append((req,timeout))
        return Response(body,'text/event-stream' if mode=='streaming' else 'application/json')
    return module.DifyChatClient('https://dify.example/v1','app-secret',opener=opener,**kwargs),calls


def chat(client,**kw):
    return client.chat(query='合成询价',user='trusted-user',inputs={'schema_version':'1','turn_ref':'opaque','context_epoch':'3'},**kw)


def test_chat_path_identity_and_complete_turn_are_explicit(module):
    c,calls=make(module,stream(envelopes()))
    result=chat(c,conversation_id='conversation-1')
    req=calls[0][0];body=json.loads(req.data)
    assert req.full_url=='https://dify.example/v1/chat-messages'
    assert body['user']=='trusted-user' and body['conversation_id']=='conversation-1'
    assert body['inputs']['turn_ref']=='opaque'
    assert result.proposal==proposal()
    assert result.workflow_run_id=='run-1'
    assert 'SECRET-THOUGHT' not in str(result)
    assert len(calls)==1


@pytest.mark.parametrize('events', [envelopes()[:-1],envelopes()[:3]+envelopes()[4:],
    envelopes()+[dict(event='error',message='SECRET-ERROR')],
    [dict(e,conversation_id='another') if e.get('event')=='message_end' else e for e in envelopes()],
    [dict(e,data={'status':'failed','error':'SECRET-ERROR'}) if e['event']=='workflow_finished' else e for e in envelopes()]])
def test_error_drift_or_incomplete_stream_never_returns_proposal(module,events):
    c,_=make(module,stream(events))
    with pytest.raises(module.DifyClientError) as exc:chat(c,conversation_id='conversation-1')
    assert 'SECRET' not in str(exc.value)


def test_success_terminal_order_can_be_reversed(module):
    events=envelopes();events[-2:]=events[-2:][::-1]
    c,_=make(module,stream(events));assert chat(c).proposal==proposal()


def test_blocking_message_semantics_and_schema(module):
    body=dict(event='message',conversation_id='conversation-1',message_id='m',task_id='t',answer=json.dumps(proposal()))
    c,calls=make(module,json.dumps(body).encode(),mode='blocking')
    assert chat(c,response_mode='blocking').proposal==proposal()
    assert json.loads(calls[0][0].data)['response_mode']=='blocking'


@pytest.mark.parametrize('answer', ['{"schema_version":"1"}', '```json\n{}\n```', '{"x":NaN}', '{"schema_version":"1","schema_version":"1"}', json.dumps(dict(proposal(),execute=True))])
def test_non_contract_answer_is_rejected(module,answer):
    events=envelopes();events[2]['answer']=answer
    c,_=make(module,stream(events))
    with pytest.raises(module.DifyClientError):chat(c)


def test_timeout_sanitized_and_no_workflow_retry(module):
    calls=[]
    def opener(req,timeout):calls.append(req);raise TimeoutError('PRIVATE-URL-KEY')
    c=module.DifyChatClient('https://dify.example','secret',opener=opener)
    with pytest.raises(module.DifyClientError) as e:chat(c)
    assert e.value.code=='TIMEOUT'
    assert 'PRIVATE' not in str(e.value) and len(calls)==1


def test_oversized_answer_rejected(module):
    c,_=make(module,stream(envelopes()),max_response_bytes=128)
    with pytest.raises(module.DifyClientError) as e:chat(c)
    assert e.value.code=='RESPONSE_TOO_LARGE'


def test_redirect_refused_without_sending_credentials_elsewhere(module):
    def opener(req,timeout):raise urllib.error.HTTPError(req.full_url,302,'redirect',{'Location':'https://evil.invalid'},None)
    c=module.DifyChatClient('https://dify.example','secret',opener=opener)
    with pytest.raises(module.DifyClientError) as e:chat(c)
    assert e.value.code=='HTTP_ERROR'


def test_exponent_overflow_is_not_a_finite_structured_fact(module):
    events=envelopes();events[2]['answer']=json.dumps(proposal()).replace('"trader_view": {}','"trader_view": {"gap": 1e999}')
    c,_=make(module,stream(events))
    with pytest.raises(module.DifyClientError):chat(c)


def test_stop_ack_is_separate_from_chat_proposal(module):
    c,calls=make(module,b'{"result":"success"}',mode='blocking')
    assert c.stop('task-1','trusted-user') is True
    assert calls[0][0].full_url=='https://dify.example/v1/chat-messages/task-1/stop'
    assert json.loads(calls[0][0].data)=={'user':'trusted-user'}
