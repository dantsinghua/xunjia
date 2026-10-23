"""A bounded multi-round model stage owns a lease for its configured budget."""
import pytest
from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.prices import LimitPolicy
from server.negotiation.settings import Aggregation


def test_configured_longer_stage_keeps_analysis_lease_for_realistic_model_delay(tmp_path,monkeypatch):
    monkeypatch.setenv('XUNJIA_DIFY_STAGE_BUDGET_SECONDS','360')
    clock=[1792202400.0]
    class Flows:
        def analyze(self,snapshot):
            clock[0]+=181
            message=snapshot['messages'][0];mid=snapshot['members'][0]['member_id']
            return {'schema_version':'2.2','scopes':{message['message_id']:[mid]},'observations':[
                {'member_id':mid,'side':'ofr','changes':{'price':{'kind':'YIELD','value':'3.10'}},
                 'evidence':{'price':[message['message_id']]}}]}
        def decide(self,context):raise AssertionError('Sending is paused')
    app=create_app(database_url='sqlite:///'+str(tmp_path/'budget.db'),enable_background=False,
        auth_config=AuthConfig(clock=lambda:clock[0]),chatflows=Flows(),
        aggregation=Aggregation(0,0,1000,'synthetic-fixture'))
    store=app.state.repository;owner='synthetic-owner';day=app.state.inquiry.trade_day()
    store.bind_account(owner,'synthetic-account','synthetic-budget-workbench',transport='oss')
    group=store.create_group(owner,bond_code='SYN-A',our_side='BUY',target_volume='1000',trade_day=day,
                             policy=LimitPolicy('ABSOLUTE','3.12'))
    member,thread=store.add_member(owner,group,account_uid='synthetic-account',peer_uid='synthetic-peer',workbench_id='synthetic-budget-workbench')
    store.ingest(owner,thread,event_id='first',source_seq=1,text='SYN-A 3.10出',trade_day=day)
    result=app.state.negotiation_engine.analyze_thread(owner,thread)
    assert result['facts_committed'],result
    assert store.quote(owner,member,'ofr').price.value=='3.10'


@pytest.mark.parametrize('budget',['0','-1','541','inf'])
def test_invalid_stage_budget_is_not_silently_accepted(tmp_path,monkeypatch,budget):
    monkeypatch.setenv('XUNJIA_DIFY_STAGE_BUDGET_SECONDS',budget)
    with pytest.raises(ValueError):
        create_app(database_url='sqlite:///'+str(tmp_path/'bad.db'),enable_background=False)
