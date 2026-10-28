"""Service lifetime, not browser lifetime, owns the public market collector."""
import asyncio
import json
import pytest
from server.quote_control import QuoteControl
from server.quote_credentials import save_quote

CREDS = {'account':'synthetic','cookie':'sid=test','sid':'test','device_id':'device'}

def controller(tmp_path, seen):
    async def collect(values, update):
        seen.append('started')
        update(state='streaming')
        try:
            await asyncio.Event().wait()
        finally:
            seen.append('closed')
    return QuoteControl(directory=tmp_path, reader=object(), session=collect,
                        db_check=lambda: {'ok': True})

@pytest.mark.parametrize('legacy_flag', [None, '0', '1'])
def test_explicitly_enabled_source_resumes_on_service_restart_without_page(tmp_path, monkeypatch, legacy_flag):
    if legacy_flag is None: monkeypatch.delenv('XUNJIA_QUOTE_AUTOSTART', raising=False)
    else: monkeypatch.setenv('XUNJIA_QUOTE_AUTOSTART', legacy_flag)
    async def run():
        seen=[]; first=controller(tmp_path, seen); save_quote(CREDS, tmp_path)
        await first.start('alice'); await asyncio.sleep(0)
        await first.shutdown()
        assert json.loads((tmp_path/'collector-state.json').read_text())['enabled'] is True
        second=controller(tmp_path, seen)
        try:
            await second.startup(); await asyncio.sleep(0)
            assert second.status('bob')['collector_active'] is True
            assert seen == ['started','closed','started']
        finally: await second.shutdown()
    asyncio.run(run())

def test_saved_credentials_alone_never_start_and_explicit_stop_survives_restart(tmp_path):
    async def run():
        seen=[]; c=controller(tmp_path, seen); save_quote(CREDS, tmp_path)
        await c.startup(); assert not c.status('alice')['collector_active']
        await c.start('alice'); await asyncio.sleep(0); await c.stop('bob')
        c2=controller(tmp_path, seen); await c2.startup()
        assert not c2.status('alice')['collector_active']
        assert seen == ['started', 'closed']
    asyncio.run(run())

def test_unexpected_normal_disconnect_is_backed_off_not_a_hot_loop(tmp_path):
    async def run():
        calls=[]
        async def collect(values, update): calls.append(1)
        c=QuoteControl(directory=tmp_path,reader=object(),session=collect,db_check=lambda:{'ok':True})
        save_quote(CREDS,tmp_path)
        await c.start('alice')
        try:
            await asyncio.sleep(.02)
            assert len(calls)==1
            assert c.status('alice')['collector_state']=='retrying'
        finally: await c.shutdown()
    asyncio.run(run())


def test_no_page_or_valid_browser_token_is_required_after_explicit_start(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig,AuthUser
    now=[1_800_000_000]
    ticks=[]
    async def collect(values,update):
        while True:
            ticks.append(1);update(state='streaming',quotes_written=len(ticks))
            await asyncio.sleep(.01)
    c=QuoteControl(directory=tmp_path/'creds',reader=object(),session=collect,db_check=lambda:{'ok':True})
    save_quote(CREDS,c.directory)
    app=create_app(database_url='sqlite:///'+str(tmp_path/'business.db'),enable_background=False,
                   quote_control=c,auth_config=AuthConfig(sso_secret='synthetic',session_secret='k'*40,clock=lambda:now[0]))
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+app.state.auth.issue_token(AuthUser('alice','Alice'))}
        assert client.post('/api/im/quote/start',headers=headers).status_code==200
        first=len(ticks);time.sleep(.05)
        assert len(ticks)>first and app.state.quote_hub.clients=={}
        now[0]+=86401
        assert client.get('/api/im/quote',headers=headers).status_code==401
        first=len(ticks);time.sleep(.05)
        assert len(ticks)>first and c.status('system')['collector_active']
    assert json.loads((c.directory/'collector-state.json').read_text())['enabled'] is True
