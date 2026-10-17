import json
import pytest
from fastapi.testclient import TestClient
from server.legacy_app import create_app
from server.auth import AuthConfig,AuthUser
from server.runtime import Dispatcher


class Transport:
    def send(self,cmd):
        return {k:cmd[k] for k in ('command_id','account_uid','peer_id','content_hash')}|{'status':'sent','evidence':'synthetic-native'}
    def reconcile(self,cmd):return self.send(cmd)
    def cancel(self,cmd):return self.send(cmd)|{'status':'cancelled'}


def test_manual_reply_takes_over_and_requires_real_receipt(tmp_path):
    app=create_app(database_url='sqlite:///'+str(tmp_path/'x.db'),auth_config=AuthConfig(sso_secret='fake',session_secret='fake-secret-with-more-than-thirty-two-bytes'),enable_background=False)
    repo=app.state.repository;repo.bind_account('a','a1','wb')
    svc=app.state.inquiry
    svc.ingest('a',{'event_id':'in1','account_uid':'a1','peer_id':'p1','kind':'private','source_seq':1,'text':'你好','occurred_at':svc.clock(),'historical':False})
    sent=svc.manual_send('a','p1','您好，请问今天方便询价吗？','req1')
    assert sent['status']=='queued'
    assert svc.manual_send('a','p1','您好，请问今天方便询价吗？','req1')==sent
    assert not repo.binding_for('a')['bot_enabled']
    Dispatcher(svc,Transport()).run_once()
    assert svc.commands('a')[0]['status']=='sent'
    assert svc.conversations('a')[0]['mode']=='HUMAN'
    with pytest.raises(ValueError):svc.manual_send('a','p1','这笔成交了','req2')


def test_http_fixed_nodes_and_distinct_public_budget(tmp_path):
    private='private-test-service-token-at-least-32-chars'
    public='public-test-service-token-at-least-32-chars'
    class Agent:
        def chat(self,**kwargs):
            turn=kwargs['inputs']['turn_ref']
            result=client.post('/internal/agent/context',headers={'Authorization':'Bearer '+private},json={'turn_ref':turn})
            assert result.status_code==200
            context=result.json()['context'];decision=context['decision_context']
            ticket=decision['tool_budget_ref']
            assert ticket!=turn and 'turn_ref' not in decision
            assert client.post('/internal/agent/context',headers={'Authorization':'Bearer '+public},json={'turn_ref':turn}).status_code==403
            body={'arguments_json':json.dumps({'schema_version':'1','mode':'get','bond_id':'SYN001.IB'}),'tool_budget_ref':ticket}
            assert client.post('/api/agent/tools/bond_info',json=body).json()['status']=='unavailable'
            assert client.post('/internal/agent/context',headers={'Authorization':'Bearer '+private},json={'turn_ref':ticket}).status_code==403
            assert client.post('/api/agent/tools/bond_info',json={**body,'arguments_json':'{"schema_version":"1","schema_version":"2"}'}).status_code==400
            value={k:context[k] for k in ('turn_id','conversation_epoch','source_message_id')}|{'schema_version':'1','observations':[],'proposed_actions':[{'type':'SOCIAL_REPLY'}],'public_message':'您好，很高兴保持联系。','trader_view':{},'memory_patch':None,'history_request':None,'validation_ref':None}
            checked=client.post('/internal/agent/validate',headers={'Authorization':'Bearer '+private},json={'turn_ref':turn,'proposal':value})
            assert checked.status_code==200
            value['validation_ref']=checked.json()['validation_ref']
            return value
    app=create_app(database_url='sqlite:///'+str(tmp_path/'x.db'),auth_config=AuthConfig(sso_secret='fake',session_secret='fake-secret-with-more-than-thirty-two-bytes'),enable_background=False,chat_client=Agent(),agent_service_token=private)
    svc=app.state.inquiry;svc.repo.bind_account('a','a1','wb')
    draft=svc.preview('a','p',[{'code':'SYN001.IB','name':'合成债','peer_ids':['p1'],'our_side':'BUY','volume_wan':'1000','opening_yield_pct':'2.50','limit_yield_pct':'2.40','step_bp':'2'}]);svc.confirm('a',draft['batch_id'],1,'c')
    Dispatcher(svc,Transport()).run_once()
    message=svc.ingest('a',{'event_id':'in1','account_uid':'a1','peer_id':'p1','kind':'private','source_seq':1,'text':'您好','occurred_at':svc.clock(),'historical':False})
    with TestClient(app) as client:
        assert app.state.chat_runner.process(message['message_id'])['status']=='queued'
    assert len(svc.commands('a'))==2
