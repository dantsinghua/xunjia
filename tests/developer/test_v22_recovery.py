"""Rejected facts block stale economics but still permit a clarification."""
from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.settings import Aggregation


def test_rejected_price_can_prompt_clarification_instead_of_stalling_forever(tmp_path):
    class Flows:
        calls=0
        def analyze(self,snapshot):
            m=snapshot['messages'][0];p=snapshot['members'][0]
            return {'schema_version':'2.2','scopes':{m['message_id']:[p['member_id']]},
                'observations':[{'member_id':p['member_id'],'side':'ofr','changes':{
                    'price':{'kind':'YIELD','value':'3.99'}},'evidence':{'price':[m['message_id']]}}]}
        def decide(self,context):
            self.calls+=1
            return {'schema_version':'2.2','action':'SEND','proposal':{
                'kind':'CLARIFY','text':'请再确认这只券的可做价格和数量。'}}
    flows=Flows()
    app=create_app(submission_guard=lambda _owner: None, database_url='sqlite:///'+str(tmp_path/'recovery.db'),enable_background=False,
        chatflows=flows,aggregation=Aggregation(0,0,1000,'synthetic-fixture'),auth_config=AuthConfig(clock=lambda:1792202400))
    service=app.state.inquiry;store=app.state.repository;owner='synthetic-owner'
    store.bind_account(owner,'synthetic-account','synthetic-wb')
    preview=service.preview(owner,'preview',[{'code':'SYN-A','our_side':'BUY','volume_wan':'1000',
        'opening_yield_pct':'3.20','limit_yield_pct':'3.12','step_bp':'2','peer_ids':['synthetic-peer']}])
    first=service.confirm(owner,preview['batch_id'],preview['version'],'confirm')['commands'][0]
    command=service.commands_core.get(owner,first['command_id'])
    service.commands_core.receipt(owner,command['command_id'],account_uid='synthetic-account',peer_uid='synthetic-peer',
        content_hash=command['content_hash'],status='CANCELLED',evidence='synthetic not submitted')
    store.ingest(owner,command['thread_id'],event_id='event-1',source_seq=1,text='SYN-A 3.10出',trade_day=service.trade_day())
    app.state.negotiation_engine.run_once()
    assert flows.calls==1
    commands=service.commands(owner)
    assert len(commands)==2 and any(c['state']=='QUEUED' and '请再确认' in c['text'] for c in commands)
    assert store.quote(owner,command['member_id'],'ofr').price is None
