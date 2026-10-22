"""Single-action RFQ submission; synthetic accounts and transport, no real IM."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import contextmanager
import json
import time
import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.prices import Valuation

OWNER='direct-send-synthetic'

class LocalTransport:
    supports_v22=True
    adapters={}
    def __init__(self): self.sent=[]
    def send(self, wire):
        self.sent.append(dict(wire))
        return {**{k:wire[k] for k in ('command_id','account_uid','peer_id','content_hash')},
                'status':'sent','native_id':'synthetic-native-'+wire['command_id'],
                'evidence':'synthetic-owned-transport'}
    def cancel(self, wire):
        return {**{k:wire[k] for k in ('command_id','account_uid','peer_id','content_hash')},
                'status':'cancelled','evidence':'synthetic-cancel'}
    def reconcile(self, wire):
        return {**{k:wire[k] for k in ('command_id','account_uid','peer_id','content_hash')},
                'status':'pending','evidence':'synthetic-pending'}

@contextmanager
def desk(tmp_path, *, enabled=False, real_admission=False):
    transport=LocalTransport()
    app=create_app(submission_guard=None if enabled or real_admission else (lambda _owner:None), database_url='sqlite:///'+str(tmp_path/'direct.sqlite'),enable_background=enabled,
        transport=transport,auth_config=AuthConfig(sso_secret='synthetic-sso',
        session_secret='synthetic-session-at-least-32-characters'))
    app.state.repository.bind_account(OWNER,'synthetic-account','synthetic-workbench')
    user=type('User',(),{'user_id':OWNER,'display_name':'Synthetic direct operator'})()
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+app.state.auth.issue_token(user)
        yield app,client,transport

def item(code='SYN-DIRECT',side='BUY',opening='1.10',limit='2.00',peers=None):
    return dict(code=code,name='Synthetic inquiry '+code,our_side=side,volume_wan='2000',
                opening_yield_pct=opening,limit_yield_pct=limit,step_bp='2',
                peer_ids=peers or ['synthetic-peer'],pricing_basis='absolute')

def send(client,items,request='direct-request'):
    return client.post('/api/im/rfq/send-direct',json={'request_id':request,'items':items})

def counts(app):
    with app.state.repository.db.transaction(write=False) as tx:
        return {name:tx.execute('SELECT COUNT(*) AS n FROM '+name).fetchone()['n']
                for name in ('xj2_batch','xj2_group','xj2_command')}

@pytest.mark.parametrize('side,opening,limit',[('BUY','1.10','2.00'),('SELL','2.50','1.90'),('BUY','-0.10','0')])
def test_direct_accepts_explicit_initial_price_without_boundary_comparison(tmp_path,side,opening,limit):
    with desk(tmp_path) as (app,client,transport):
        r=send(client,[item(side=side,opening=opening,limit=limit)])
        assert r.status_code==200,r.text
        c=r.json()['data']['commands'][0]
        assert c['status']=='queued' and opening+'%' in c['text']
        assert counts(app)=={'xj2_batch':1,'xj2_group':1,'xj2_command':1}
        assert transport.sent==[]

@pytest.mark.parametrize('side,opening,boundary',[('BUY','1.10','2.02'),('SELL','2.50','1.98')])
def test_bp_uses_sourced_boundary_ignores_browser_conversion_and_preserves_opening(tmp_path,side,opening,boundary):
    with desk(tmp_path) as (app,client,transport):
        app.state.repository.save_preferences(OWNER,{'floors':{'SYN-DIRECT':{'kind':'bp','value':'2'}}})
        app.state.inquiry.valuation_source=lambda code:Valuation(code,None,'1.9954','innodealing-cnbd:synthetic')
        value=item(side=side,opening=opening,limit='9.99');value['pricing_basis']='bp'
        r=send(client,[value]);assert r.status_code==200,r.text
        group=next(g for g in app.state.repository.snapshot(OWNER)['groups'] if g['group_id']==r.json()['data']['group_ids'][0])
        assert group['boundary']['value']==boundary
        assert group['plan']['opening_yield_pct']==opening
        assert transport.sent==[]

def test_previews_remain_optional_and_zero_send_with_removed_check(tmp_path):
    with desk(tmp_path) as (app,client,transport):
        r=client.post('/api/im/rfq/preview',json={'request_id':'optional','items':[item()]})
        assert r.status_code==200,r.text
        assert counts(app)['xj2_command']==0 and transport.sent==[]

def test_direct_merges_multiple_bonds_per_recipient_without_browser_preview(tmp_path):
    with desk(tmp_path) as (app,client,transport):
        r=send(client,[item('SYN-A',peers=['p1','p2']),item('SYN-B',peers=['p1'])])
        assert r.status_code==200,r.text
        rows=r.json()['data']['commands'];assert len(rows)==2
        first=next(c for c in rows if c['peer_id']=='p1')
        assert 'SYN-A' in first['text'] and 'SYN-B' in first['text']
        assert len(first['session_ids'])==2 and counts(app)['xj2_command']==2

def test_direct_replay_after_committed_response_loss_never_revalues_or_requeues(tmp_path,monkeypatch):
    with desk(tmp_path) as (app,client,transport):
        body=[item()];first=send(client,body);assert first.status_code==200
        monkeypatch.setattr(app.state.inquiry,'normalize_item',lambda *a,**kw:pytest.fail('Committed retry must not normalize again'))
        again=send(client,body);assert again.json()==first.json()
        assert counts(app)['xj2_command']==1
        changed=deepcopy(body);changed[0]['volume_wan']='3000'
        bad=send(client,changed);assert bad.status_code==400
        assert counts(app)['xj2_command']==1 and transport.sent==[]

def test_parallel_same_direct_request_has_exactly_one_batch_and_command(tmp_path):
    with desk(tmp_path) as (app,client,transport):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:app.state.inquiry.send_direct(OWNER,'parallel',[item()]),range(4)))
        assert len({r['batch_id'] for r in results})==1
        assert counts(app)=={'xj2_batch':1,'xj2_group':1,'xj2_command':1}

@pytest.mark.parametrize('change',[{'our_side':''},{'volume_wan':'0'},{'step_bp':'nan'},{'opening_yield_pct':'Infinity'},{'peer_ids':[]}])
def test_direct_keeps_required_values_and_invalid_input_creates_no_commands(tmp_path,change):
    with desk(tmp_path) as (app,client,transport):
        value={**item(),**change};r=send(client,[value]);assert r.status_code==400,r.text
        assert counts(app)['xj2_command']==0 and transport.sent==[]

def test_direct_still_requires_login(tmp_path):
    with desk(tmp_path) as (app,client,transport):
        client.headers.pop('Authorization')
        assert send(client,[item()]).status_code==401
        assert counts(app)['xj2_command']==0

def test_dispatch_disabled_rejects_without_queuing_or_claiming_normal(tmp_path):
    with desk(tmp_path,real_admission=True) as (app,client,transport):
        r=send(client,[item()]);assert r.status_code==503,r.text
        assert r.json()['submitted'] is False and r.json()['code']=='DISPATCH_DISABLED'
        assert counts(app)=={'xj2_batch':0,'xj2_group':0,'xj2_command':0}
        assert transport.sent==[]


def test_background_lifecycle_dispatches_direct_request_and_records_receipt(tmp_path):
    with desk(tmp_path,enabled=True) as (app,client,transport):
        r=send(client,[item()]);assert r.status_code==200,r.text
        cid=r.json()['data']['commands'][0]['command_id'];deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            row=app.state.inquiry.commands_core.get(OWNER,cid)
            if row['state']=='SENT':break
            time.sleep(.05)
        assert row['state']=='SENT',row
        assert len(transport.sent)==1 and transport.sent[0]['command_id']==cid
        replay=send(client,[item()]);assert replay.status_code==200
        assert replay.json()['data']['commands'][0]['command_id']==cid
        assert len(transport.sent)==1
