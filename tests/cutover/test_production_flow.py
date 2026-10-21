"""Tests enter server.app and observe only new xj2_* business writes.
All messages, tools, clocks and native sends below are explicitly synthetic.
"""
from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.settings import Aggregation
from server.negotiation.prices import Price
NOW=datetime(2026,10,17,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
OWNER='cutover-test-owner'
class User:
    user_id=OWNER
    display_name='Synthetic trader'
class Clock:
    value=NOW
    def __call__(self):return self.value
class FakeFlows:
    def __init__(self):self.analysis_calls=[];self.reply_calls=[];self.bad_reply=False;self.on_reply=None
    def analyze(self,context):
        self.analysis_calls.append(deepcopy(context));scopes={};observations=[]
        for msg in context['messages']:
            hits=[p for p in context['members'] if p['bond_code'] in msg['text']]
            # Synthetic fixture association, NOT a language model implementation.
            scopes[msg['message_id']]=[p['member_id'] for p in hits]
            if msg['direction']!='in' or msg['historical'] or msg['kind']!='message':continue
            for member in hits:
                data={'price':Price('YIELD','3.10').dump(),'volume_wan':'500','confirmed_day':'2026-10-17','validity':'ACTIVE','finality':'NEGOTIABLE'}
                observations.append({'member_id':member['member_id'],'side':'ofr','changes':data,'evidence':{k:[msg['message_id']] for k in data}})
        return {'schema_version':'2.2','scopes':scopes,'observations':observations,'unresolved':{}}
    def decide(self,context):
        self.reply_calls.append(deepcopy(context))
        if self.on_reply:self.on_reply(context)
        price='3.19';text='3.99%收，500万' if self.bad_reply else '3.19%收，500万'
        return {'schema_version':'2.2','action':'SEND','proposal':{'kind':'REPLY','text':text,'our_price':Price('YIELD',price).dump()},'trader_view':{'reason':'synthetic fixture'}}
class FakeNative:
    supports_v22=True
    def __init__(self):self.sent=[];self.cancelled=[];self.adapters={}
    def send(self,command):
        self.sent.append(dict(command))
        return {**{k:command[k] for k in ('command_id','account_uid','peer_id','content_hash')},'status':'sent','evidence':'synthetic native','native_id':'native-'+command['command_id']}
    def cancel(self,command):
        self.cancelled.append(dict(command))
        return {**{k:command[k] for k in ('command_id','account_uid','peer_id','content_hash')},'status':'cancelled','evidence':'synthetic cancel'}
    def reconcile(self,command):return self.cancel(command)
@pytest.fixture
def desk(tmp_path):
    clock=Clock();flows=FakeFlows();transport=FakeNative()
    app=create_app(database_url='sqlite:///'+str(tmp_path/'current.db'),auth_config=AuthConfig(sso_secret='synthetic-sso',session_secret='synthetic-session-at-least-32-characters',clock=clock),
                   enable_background=False,chatflows=flows,transport=transport,
                   # This is a TEST policy, not a measured production profile.
                   aggregation=Aggregation(0,0,1000,'synthetic-fixture-only'))
    token=app.state.auth.issue_token(User())
    app.state.repository.bind_account(OWNER,'synthetic-account','synthetic-wb')
    with TestClient(app) as client:
        client.headers.update({'Authorization':'Bearer '+token})
        yield app,client,flows,transport,clock
def draft(client,items=None,key='rfq-1'):
    items=items or [item('SYN-A',['peer-a'])]
    response=client.post('/api/im/rfq/preview',json={'request_id':key,'items':items})
    assert response.status_code==200,response.text
    return response.json()['data']
def item(code,peers):
    return {'code':code,'name':code,'peer_ids':peers,'our_side':'BUY','volume_wan':'1000',
            'opening_yield_pct':'3.20','limit_yield_pct':'3.12','step_bp':'2'}
def confirm(client,value,key='confirm-1'):
    response=client.post('/api/im/rfq/send',json={'batch_id':value['batch_id'],'expected_version':value['version'],'request_id':key})
    assert response.status_code==200,response.text
    return response.json()['data']
def raw(app,thread,text,seq=1,**kwargs):
    return app.state.repository.ingest(OWNER,thread,event_id='event-'+str(seq),source_seq=seq,text=text,trade_day='2026-10-17',occurred_at=NOW+seq,**kwargs)
def test_production_creates_new_tables_only_and_exposes_new_engine(desk):
    app,c,*_=desk
    assert app.state.business_engine=='v22'
    assert app.state.inquiry.__class__.__module__=='server.negotiation.service'
    assert app.state.inbox.__class__.__module__=='server.negotiation.runtime'
    with app.state.repository.db.transaction(write=False) as tx:
        names={v['name'] for v in tx.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert names and all(n.startswith('xj2_') for n in names)
    assert c.get('/api/health/ready').status_code==200
    assert c.get('/api/health/live').json()['data']['engine']=='v22'
    assert c.post('/internal/agent/context',json={'turn_ref':'old-contract'}).status_code==410
def test_preview_confirm_creates_one_shared_group_per_bond_and_combined_first_message(desk):
    app,c,flows,transport,clock=desk
    preview=draft(c,[item('SYN-A',['peer-a','peer-b']),item('SYN-B',['peer-a'])])
    first=confirm(c,preview)
    assert len(first['group_ids'])==2 and len(first['commands'])==2
    replay=confirm(c,preview)
    assert replay==first
    groups=c.get('/api/negotiation/state').json()['data']['groups']
    assert len(groups)==2 and sorted(len(g['peers']) for g in groups)==[1,2]
    assert all(g['target_volume']=='1000' for g in groups)
    a_command=next(x for x in first['commands'] if x['peer_id']=='peer-a')
    assert 'SYN-A' in a_command['text'] and 'SYN-B' in a_command['text']
    app.state.dispatcher.run_once()
    assert len(transport.sent)==2
    for cmd in transport.sent:
        assert cmd['business_version']=='2.2' and cmd['source_watermark']==0
    for g in app.state.repository.group_rows(OWNER):
        for p in g['peers']:assert p['our_quote']['price']['value']=='3.20'
def test_pause_only_sending_raw_visible_before_parser_and_quote_after_analysis(desk):
    app,c,flows,transport,clock=desk
    confirmed=confirm(c,draft(c));g=app.state.repository.group_rows(OWNER)[0];p=g['peers'][0]
    assert c.post('/api/bot/toggle',json={'status':'停止'}).status_code==200
    raw(app,p['thread_id'],'SYN-A 3.10出 500万')
    before=c.get('/api/negotiation/messages/'+p['thread_id']).json()['data']
    assert before['messages'][-1]['text']=='SYN-A 3.10出 500万'
    assert before['messages'][-1]['analysis_state']=='QUEUED'
    assert not flows.analysis_calls
    app.state.negotiation_engine.run_once()
    after=c.get('/api/negotiation/state').json()['data']
    assert after['groups'][0]['peers'][0]['quotes']['ofr']['volume_wan']=='500'
    assert after['groups'][0]['peers'][0]['quotes']['ofr']['price']['value']=='3.10'
    assert len(flows.analysis_calls)==1 and not flows.reply_calls
    app.state.dispatcher.run_once();assert not transport.sent
    assert c.get('/api/monitor/detail?cp=peer-a').json()['data'][0]['theirVol']=='500'
def test_bad_reply_does_not_rollback_real_fact_commit(desk):
    app,c,flows,transport,clock=desk
    confirm(c,draft(c));app.state.dispatcher.run_once()
    g=app.state.repository.group_rows(OWNER)[0];p=g['peers'][0]
    flows.bad_reply=True
    raw(app,p['thread_id'],'SYN-A 3.10出 500万')
    result=app.state.negotiation_engine.run_once()
    assert result['analyzed']==1
    quote=app.state.repository.quote(OWNER,p['member_id'],'ofr')
    assert quote.price.value=='3.10' and quote.volume_wan=='500'
    decisions=c.get('/api/negotiation/members/'+p['member_id']+'/decisions').json()['data']
    assert decisions and decisions[0]['state']=='REJECTED'
    assert len(transport.sent)==1
    app.state.dispatcher.run_once();assert len(transport.sent)==1
def test_new_message_during_reply_invalidates_candidate_not_quotes(desk):
    app,c,flows,transport,clock=desk
    confirm(c,draft(c));app.state.dispatcher.run_once()
    p=app.state.repository.group_rows(OWNER)[0]['peers'][0]
    raw(app,p['thread_id'],'SYN-A 3.10出 500万')
    flows.on_reply=lambda ctx:raw(app,p['thread_id'],'SYN-A 新价3.15出',seq=2)
    app.state.negotiation_engine.run_once()
    assert app.state.repository.quote(OWNER,p['member_id'],'ofr').price.value=='3.10'
    assert app.state.repository.group_rows(OWNER)[0]['peers'][0]['analysis_pending']
    app.state.dispatcher.run_once();assert len(transport.sent)==1
def test_bond_pause_does_not_pause_other_bond_in_same_thread(desk):
    app,c,flows,transport,clock=desk
    confirm(c,draft(c,[item('SYN-A',['peer-a']),item('SYN-B',['peer-a'])]));app.state.dispatcher.run_once()
    groups=app.state.repository.group_rows(OWNER)
    a=next(g for g in groups if g['bond_code']=='SYN-A');b=next(g for g in groups if g['bond_code']=='SYN-B')
    response=c.post('/negotiation/wrong',json={})
    assert c.post('/api/negotiation/groups/'+a['group_id']+'/sending',json={'allowed':False}).status_code==200
    assert app.state.repository.group(OWNER,a['group_id'])['send_allowed']==0
    assert app.state.repository.group(OWNER,b['group_id'])['send_allowed']==1
    raw(app,b['peers'][0]['thread_id'],'SYN-B 3.10出500万')
    app.state.negotiation_engine.run_once();app.state.dispatcher.run_once()
    assert len(transport.sent)==2
def test_retract_immediately_invalidates_quote_and_coverage_but_keeps_raw_history(desk):
    app,c,flows,transport,clock=desk
    confirm(c,draft(c));assert c.post('/api/bot/toggle',json={'status':'停止'}).status_code==200
    p=app.state.repository.group_rows(OWNER)[0]['peers'][0]
    message=raw(app,p['thread_id'],'SYN-A 3.10出500万')
    app.state.negotiation_engine.run_once()
    raw(app,p['thread_id'],'SYN-A撤',seq=2,kind='retract',target_event_id='event-1')
    q=app.state.repository.quote(OWNER,p['member_id'],'ofr')
    assert q.validity=='WITHDRAWN' and q.finality=='UNKNOWN'
    assert len(c.get('/api/negotiation/messages/'+p['thread_id']).json()['data']['messages'])==2
def test_parallel_confirm_is_one_batch_with_no_duplicate_groups_or_commands(desk):
    app,c,flows,transport,clock=desk
    preview=draft(c)
    def work(n):return app.state.inquiry.confirm(OWNER,preview['batch_id'],preview['version'],'same-confirm')
    with ThreadPoolExecutor(max_workers=5) as pool:values=list(pool.map(work,range(5)))
    assert len({v['group_ids'][0] for v in values})==1
    assert len(app.state.inquiry.commands(OWNER))==1
def test_no_measured_profile_disables_auto_analysis_without_fake_window(tmp_path):
    app=create_app(database_url='sqlite:///'+str(tmp_path/'p.db'),enable_background=False,chatflows=FakeFlows())
    with TestClient(app) as c:
        data=c.get('/api/health').json()['data']
        assert data['engine']=='v22' and not data['analysis_ready']
        assert 'AGGREGATION_MEASUREMENT_REQUIRED' in data['analysis_blockers']
