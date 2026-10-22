"""Recovery ticks and new Inno valuation rows reprice OUR boundary, not peers."""
from copy import deepcopy
from datetime import datetime,date
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import threading
import json
import pytest
from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.prices import LimitPolicy,Valuation,InvalidFact
from server.quote_runtime import QBMarket
from server.reference_data import ReferenceSnapshot
OWNER='synthetic-valuation-owner'
class Reference:
    def __init__(self):self.day='2026-10-16';self.value='2.5818';self.connected=True;self.reads=0
    def latest_valuation_day(self,_):return self.day
    def snapshot(self):
        self.reads+=1
        if not self.connected:raise ConnectionError('synthetic valuation-table connection interrupted')
        return ReferenceSnapshot('synthetic-'+self.day+'-'+self.value,self.day,
            bonds=[{'code':'SYN-A','valuation_date':self.day,'valuation_yield_pct':self.value}])
def setup(tmp_path):
    ref=Reference();now=[datetime(2026,10,16,23,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()]
    app=create_app(submission_guard=lambda _owner: None, database_url='sqlite:///'+str(tmp_path/'recovery.sqlite'),enable_background=False,
        auth_config=AuthConfig(clock=lambda:now[0]),reference_provider=ref,calendar=ref)
    store=app.state.repository
    group=store.create_group(OWNER,bond_code='SYN-A',our_side='BUY',target_volume='2000',trade_day='2026-10-16',
        policy=LimitPolicy('CHINABOND_BP','5'),valuation=Valuation('SYN-A',ref.day,ref.value,'initial'),
        expected_valuation_date=ref.day,send_allowed=True)
    member,thread=store.add_member(OWNER,group,account_uid='synthetic-account',peer_uid='synthetic-peer')
    return app,ref,now,group,member,thread
def group(app,g):return app.state.repository.group(OWNER,g)
def test_database_connection_recovery_recalculates_same_day_without_restart(tmp_path):
    app,ref,now,g,m,t=setup(tmp_path)
    app.state.repository.set_sending(OWNER,False)
    now[0]+=120;ref.day='2026-10-17';ref.connected=False
    app.state.rollover_once()
    first=group(app,g)
    assert first['trade_day']=='2026-10-17' and json.loads(first['boundary_json']) is None
    version=first['policy_version']
    ref.connected=True;ref.value='2.60'
    result=app.state.rollover_once()
    assert result['revalued']==1
    recovered=group(app,g)
    assert json.loads(recovered['boundary_json'])=={'kind':'YIELD','value':'2.65'}
    assert recovered['policy_version']==version+1
    snapshot=app.state.repository.snapshot(OWNER)
    assert not snapshot['send_allowed']
    assert snapshot['groups'][0]['peers'][0]['reconfirm_day']=='2026-10-17'
    assert snapshot['groups'][0]['summary']['final_confirmed_volume_wan']=='0'
    assert app.state.inquiry.sessions(OWNER)[0]['policy']['limit_yield_pct']=='2.65'
    stable=group(app,g)
    assert app.state.rollover_once()['revalued']==0
    assert group(app,g)==stable
def source_market(value,day='2026-10-17'):
    market=QBMarket.__new__(QBMarket)
    row={'cnbd_date':date.fromisoformat(day),'cnbd_yield_mat':Decimal(value),'seq_ns':12,
         'update_time':datetime.fromisoformat(day+'T10:00:00'),'ofr_yield':'9.99','bid_yield':'8.88'}
    class EmptyHistoryCursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,*args):pass
        def fetchone(self):return None
        def fetchall(self):return []
    empty_history=SimpleNamespace(cursor=lambda:EmptyHistoryCursor())
    market.source=SimpleNamespace(_lock=threading.RLock(),code2uni={'SYN-A':1},book={1:{1:row}},last_id=12,_c=lambda:empty_history)
    return market
def test_new_inno_chinabond_value_recovers_when_reference_table_stays_down(tmp_path):
    app,ref,now,g,m,t=setup(tmp_path)
    now[0]+=120;ref.day='2026-10-17';ref.connected=False
    app.state.rollover_once()
    market=source_market('2.9872')
    app.state.inquiry.valuation_source=market.valuation
    before=app.state.repository.quote(OWNER,m,'ofr').dump()
    result=app.state.inquiry.refresh_valuations(refresh_reference=False)
    assert result['revalued']==1
    assert json.loads(group(app,g)['boundary_json'])['value']=='3.04'
    assert app.state.repository.quote(OWNER,m,'ofr').dump()==before
    assert app.state.inquiry.valuation('SYN-A')[0].snapshot_id.startswith('innodealing-cnbd:')
    assert app.state.repository.snapshot(OWNER)['groups'][0]['peers'][0]['reconfirm_day']=='2026-10-17'
def test_inno_rows_without_price_change_still_signal_valuation_refresh():
    market=source_market('2.9872')
    def poll():market.source.last_id+=1;return []
    market.source.poll=poll
    assert market.poll()==[] and market.valuation_changed
    market.source.poll=lambda:[]
    assert market.poll()==[] and not market.valuation_changed
def test_only_labelled_valuation_fields_allowed_never_peer_quotes():
    market=source_market('2.9872')
    assert market.valuation('SYN-A').yield_pct=='2.9872'
    assert market.valuation('SYN-B') is None
    market.source.book[1][1]['cnbd_yield_mat']=None
    assert market.valuation('SYN-A') is None
@pytest.mark.parametrize('command_state',['QUEUED','PENDING','UNKNOWN'])
def test_new_boundary_invalidates_old_commands_and_preserves_stop(tmp_path,command_state):
    app,ref,now,g,m,t=setup(tmp_path);store=app.state.repository;commands=app.state.inquiry.commands_core
    with store.write(OWNER) as tx:
        cid=commands._insert(tx,OWNER,m,commands._basis(tx,OWNER,m,False),'synthetic-old-candidate',
            {'kind':'REPLY','text':'synthetic never sent'},now[0]+120)
        tx.execute('UPDATE xj2_command SET state=? WHERE command_id=?',(command_state,cid))
    old=group(app,g)['policy_version'];ref.value='2.65'
    assert app.state.rollover_once()['revalued']==1
    updated=commands.get(OWNER,cid)
    if command_state=='QUEUED':assert updated['state']=='CANCELLED'
    else:assert updated['state']==command_state and updated['cancel_requested']==1
    assert group(app,g)['policy_version']==old+1
def test_absolute_boundary_never_moves_with_source_or_cross_day(tmp_path):
    app,ref,now,g,m,t=setup(tmp_path);store=app.state.repository
    absolute=store.create_group(OWNER,bond_code='SYN-B',our_side='SELL',target_volume='1000',trade_day='2026-10-16',
        policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=False)
    store.add_member(OWNER,absolute,account_uid='synthetic-account',peer_uid='synthetic-peer-b')
    now[0]+=120;ref.day='2026-10-17';ref.value='2.9872'
    app.state.rollover_once()
    assert json.loads(group(app,absolute)['boundary_json'])=={'kind':'YIELD','value':'3.12'}
    assert group(app,absolute)['send_allowed']==0
def test_older_refresh_cannot_overwrite_newer_policy(tmp_path):
    app,ref,now,g,m,t=setup(tmp_path);store=app.state.repository
    version=group(app,g)['policy_version']
    assert store.refresh_valuation(OWNER,g,valuation=Valuation('SYN-A',ref.day,'2.90','new'),
        expected_valuation_date=ref.day,expected_policy_version=version)
    assert not store.refresh_valuation(OWNER,g,valuation=Valuation('SYN-A',ref.day,'2.60','late-old'),
        expected_valuation_date=ref.day,expected_policy_version=version)
    assert json.loads(group(app,g)['boundary_json'])['value']=='2.95'
@pytest.mark.parametrize('inno_value,expected_boundary',[('2.60','2.65'),('2.50','2.55')])
def test_inno_explicit_value_has_fixed_priority_without_formal_cross_check(tmp_path,inno_value,expected_boundary):
    # 安琳新契约：明确Inno中债值足够，不再为日期/跨源对账调用正式源。
    app,ref,now,g,m,t=setup(tmp_path)
    market=source_market(inno_value,day=ref.day)
    app.state.inquiry.valuation_source=market.valuation
    before_peer=app.state.repository.quote(OWNER,m,'ofr').dump()
    value,_=app.state.inquiry.valuation('SYN-A')
    assert value.yield_pct==inno_value and value.source=='innodealing'
    result=app.state.rollover_once()
    assert result['pending']==[] and result['revalued']==1
    assert json.loads(group(app,g)['boundary_json'])=={'kind':'YIELD','value':expected_boundary}
    assert ref.reads==0, '合法Inno存在时不访问可能不同/不可用的正式源'
    assert app.state.repository.quote(OWNER,m,'ofr').dump()==before_peer


def test_application_market_loop_consumes_new_valuation_without_best_quote_delta(tmp_path,monkeypatch):
    """Run the actual lifespan market loop; all remote adapters are synthetic."""
    import time
    from fastapi.testclient import TestClient
    import server.quote_runtime as module
    ref=Reference();ref.connected=False
    now=datetime(2026,10,16,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    market=source_market('2.70',day=ref.day)
    market.db=SimpleNamespace(static_all=lambda:[])
    market.rows=lambda channel,broker:[]
    market.source.detail=lambda code:{}
    market.source.broker_choices=lambda:[]
    calls=[]
    def poll():
        calls.append(1)
        if len(calls)==1:market.source.last_id+=1
        return []
    market.source.poll=poll
    def market_factory(path, *, connection_options=None):
        assert path == str(tmp_path/'unused.sqlite')
        assert connection_options['host'] == 'synthetic.invalid'
        assert connection_options['database'] == 'synthetic'
        return market
    monkeypatch.setattr(module,'QBMarket',market_factory)
    for key,value in {'XUNJIA_QUOTE_SOURCE':'qb','QB_MYSQL_HOST':'synthetic.invalid',
        'QB_MYSQL_USER':'synthetic','QB_MYSQL_PASSWORD':'synthetic','QB_MYSQL_DB':'synthetic',
        'XUNJIA_QUOTE_CACHE':str(tmp_path/'unused.sqlite')}.items():monkeypatch.setenv(key,value)
    app=create_app(database_url='sqlite:///'+str(tmp_path/'market-loop.sqlite'),enable_background=True,
        auth_config=AuthConfig(clock=lambda:now,sso_secret='synthetic-only',session_secret='synthetic-session-at-least-32-characters'),
        reference_provider=ref,calendar=ref)
    store=app.state.repository
    gid=store.create_group(OWNER,bond_code='SYN-A',our_side='BUY',target_volume='1000',trade_day=ref.day,
        policy=LimitPolicy('CHINABOND_BP','5'),valuation=Valuation('SYN-A',ref.day,'2.5818','initial'),
        expected_valuation_date=ref.day,send_allowed=False)
    member,thread=store.add_member(OWNER,gid,account_uid='synthetic-account',peer_uid='synthetic-peer')
    with TestClient(app):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if json.loads(store.group(OWNER,gid)['boundary_json'])['value']=='2.75':break
            time.sleep(.02)
        assert calls and json.loads(store.group(OWNER,gid)['boundary_json'])['value']=='2.75'
        assert store.quote(OWNER,member,'ofr').price is None
        assert not store.group(OWNER,gid)['send_allowed']
