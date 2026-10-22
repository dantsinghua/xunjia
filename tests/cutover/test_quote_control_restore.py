"""Regression for the restored V2.2 Innodealing settings. Synthetic credentials only."""
import asyncio
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from server.quote_credentials import save_quote,snapshot
from server.auth import AuthConfig,AuthUser
from server.app import create_app
from server.quote_control import QuoteControl,check_database
CREDS={'account':'synthetic-market','cookie':'sid=synthetic-cookie','sid':'synthetic-sid','device_id':'synthetic-device'}
@pytest.fixture
def setup(tmp_path):
    seen={'probe':0,'session':0,'closed':0}
    async def probe(values):
        seen['probe']+=1
        return {'ok':True,'wss_open':True,'note':'synthetic-connected'}
    async def session(values,update):
        seen['session']+=1
        update(state='streaming',quotes_received=2,quotes_written=2)
        try:await asyncio.Event().wait()
        finally:seen['closed']+=1
    control=QuoteControl(directory=tmp_path/'creds',reader=object(),probe=probe,session=session,db_check=lambda:{'ok':True,'note':'synthetic-db'})
    app=create_app(database_url='sqlite:///'+str(tmp_path/'state.db'),auth_config=AuthConfig(sso_secret='synthetic',session_secret='k'*40),enable_background=False,quote_control=control)
    headers={name:{'Authorization':'Bearer '+app.state.auth.issue_token(AuthUser(name,name))} for name in ('manager','viewer')}
    with TestClient(app) as client:yield client,control,headers,seen
@pytest.mark.parametrize('method,path,body',[('GET','/api/im/quote',None),('PUT','/api/im/quote',CREDS),('POST','/api/im/quote/test',{}),('POST','/api/im/quote/start',{}),('POST','/api/im/quote/stop',{})])
def test_auth_required(setup,method,path,body):
    c,_,_,_=setup
    assert c.request(method,path,json=body).status_code==401
@pytest.mark.parametrize('method,path',[('PUT','/api/im/quote'),('POST','/api/im/quote/test'),('POST','/api/im/quote/start'),('POST','/api/im/quote/stop')])
def test_any_signed_in_user_can_change_shared_source(setup,method,path):
    c,control,headers,_=setup
    save_quote(CREDS,control.directory)
    result=c.request(method,path,json=CREDS if method=='PUT' else {},headers=headers['viewer'])
    assert result.status_code==200, result.text
def test_save_public_flags_blank_retains_no_autostart(setup):
    c,control,headers,seen=setup
    result=c.put('/api/im/quote',json=CREDS,headers=headers['manager'])
    assert result.status_code==200
    for key in ('cookie','sid','device_id'):assert CREDS[key] not in result.text
    data=result.json()['data'];assert data['configured'] and 'can_manage' not in data
    assert data['collector_state']=='stopped' and seen['session']==0
    assert c.put('/api/im/quote',json={'account':'renamed','cookie':'','sid':'','device_id':''},headers=headers['manager']).status_code==200
    saved=snapshot(control.directory)
    assert saved['account']=='renamed' and saved['cookie']==CREDS['cookie']
def test_invalid_update_has_no_partial_writes(setup):
    c,control,headers,_=setup
    save_quote(CREDS,control.directory)
    for body in ({'account':'changed','cookie':'bad\r\nHeader: x'},{'cookie':True},{'unsupported':'x'}):
        assert c.put('/api/im/quote',json=body,headers=headers['manager']).status_code==400
        assert snapshot(control.directory)==CREDS
def test_probe_does_not_persist_or_start(setup):
    c,control,headers,seen=setup
    save_quote(CREDS,control.directory)
    result=c.post('/api/im/quote/test',json={'cookie':'sid=synthetic-new'},headers=headers['manager'])
    assert result.status_code==200 and result.json()['data']['ok']
    assert result.json()['data']['saved'] is False
    assert snapshot(control.directory)['cookie']==CREDS['cookie']
    assert seen=={'probe':1,'session':0,'closed':0}
def test_start_idempotent_stop_closes_and_preserves_data(setup):
    c,control,headers,seen=setup
    save_quote(CREDS,control.directory)
    for _ in range(2):assert c.post('/api/im/quote/start',headers=headers['manager']).status_code==200
    assert seen['session']==1
    assert c.get('/api/im/quote',headers=headers['manager']).json()['data']['collector_active']
    assert c.put('/api/im/quote',json={'cookie':'new'},headers=headers['manager']).status_code==400
    assert c.post('/api/im/quote/test',json={},headers=headers['manager']).json()['data']['wss_open']
    assert seen['probe']==0
    assert c.post('/api/im/quote/stop',headers=headers['manager']).status_code==200
    assert seen['closed']==1
    assert snapshot(control.directory)==CREDS
    assert json.loads((control.directory/'collector-state.json').read_text())=={'enabled':False}
def test_reader_not_configured_is_not_success(setup):
    c,control,headers,seen=setup;save_quote(CREDS,control.directory);control.reader=None
    r=c.post('/api/im/quote/start',headers=headers['manager'])
    assert r.status_code==400 and 'XUNJIA_QUOTE_SOURCE' in r.text and seen['session']==0
def test_schema_failure_is_not_started(setup):
    c,control,headers,seen=setup;save_quote(CREDS,control.directory)
    control._db_check=lambda:{'ok':False,'note':'synthetic missing cnbd_date','code':'QUOTE_SCHEMA_MIGRATION_REQUIRED'}
    r=c.post('/api/im/quote/start',headers=headers['manager'])
    assert r.status_code==400 and 'cnbd_date' in r.text and seen['session']==0
def test_every_user_sees_account_but_not_secrets(setup):
    c,control,headers,_=setup;save_quote(CREDS,control.directory)
    r=c.get('/api/im/quote',headers=headers['viewer'])
    assert r.status_code==200 and r.json()['data']['account']==CREDS['account']
    assert 'can_manage' not in r.json()['data']
    for key in ('cookie','sid','device_id'):assert CREDS[key] not in r.text
def test_configured_credentials_survive_new_controller(tmp_path):
    save_quote(CREDS,tmp_path)
    c=QuoteControl(directory=tmp_path)
    assert c.status('manager')['configured'] and not c.status('manager')['collector_active']
def test_configuration_error_is_explicit(monkeypatch):
    for key in ('HOST','USER','PASSWORD','DB'):monkeypatch.delenv('QB_MYSQL_'+key,raising=False)
    assert check_database()['code']=='QUOTE_DATABASE_NOT_CONFIGURED'
def test_cross_controller_lease(tmp_path):
    async def run():
        async def session(values,update):await asyncio.Event().wait()
        values=dict(directory=tmp_path,reader=object(),session=session,db_check=lambda:{'ok':True})
        a,b=QuoteControl(**values),QuoteControl(**values);save_quote(CREDS,tmp_path)
        try:
            await a.start('manager')
            with pytest.raises(ValueError,match='占用'):await b.start('manager')
        finally:await a.stop('manager')
        await b.start('manager');await b.stop('manager')
    asyncio.run(run())