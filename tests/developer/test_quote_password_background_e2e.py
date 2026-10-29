"""Password-login collector is owned by the service, not the portal session."""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from collector.qb_gateway import QuoteAuthenticationError
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.quote_control import QuoteControl
from server.quote_credentials import snapshot


def _app(tmp_path, *, session, login, clock):
    control = QuoteControl(directory=tmp_path / 'creds', reader=object(),
                           session=session, login=login, db_check=lambda: {'ok': True})
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'business.db'),
                     enable_background=False, quote_control=control,
                     auth_config=AuthConfig(sso_secret='synthetic', session_secret='k' * 40,
                                            clock=clock))
    return app, control


def test_password_collector_keeps_running_after_portal_logout_and_process_restart(tmp_path):
    now = [1_800_000_000]
    ticks = []
    logins = []

    def login(username, password):
        logins.append((username, password))
        return {'cookie': 'sid=e2e-' + str(len(logins)), 'sid': 'e2e-' + str(len(logins))}

    async def session(values, update):
        assert values['cookie'].startswith('sid=e2e-')
        update(state='streaming')
        try:
            while True:
                ticks.append(values['sid'])
                update(quotes_written=len(ticks), last_quote_at=time.time())
                await asyncio.sleep(0.01)
        finally:
            ticks.append('closed')

    app, control = _app(tmp_path, session=session, login=login, clock=lambda: now[0])
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + app.state.auth.issue_token(AuthUser('alice', 'Alice'))}
        saved = client.put('/api/im/quote', json={'account': 'anlin9', 'password': 'synthetic-pass'},
                           headers=headers)
        assert saved.status_code == 200, saved.text
        body = saved.json()['data']
        assert body['password_set'] and body['configured'] and body['collector_state'] == 'stopped'
        assert 'synthetic-pass' not in saved.text
        assert logins == [('anlin9', 'synthetic-pass')]
        started = client.post('/api/im/quote/start', headers=headers)
        assert started.status_code == 200, started.text
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and len([x for x in ticks if x != 'closed']) < 3:
            time.sleep(0.01)
        running = len([x for x in ticks if x != 'closed'])
        assert running >= 3
        assert control.status('system')['collector_active'] is True
        assert app.state.quote_hub.clients == {}
        now[0] += 86401
        assert client.get('/api/im/quote', headers=headers).status_code == 401
        assert client.post('/api/im/quote/stop', headers=headers).status_code == 401
        after_logout = len([x for x in ticks if x != 'closed'])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and len([x for x in ticks if x != 'closed']) <= after_logout:
            time.sleep(0.01)
        assert len([x for x in ticks if x != 'closed']) > after_logout
        assert control.status('system')['collector_active'] is True
        assert json.loads((control.directory / 'collector-state.json').read_text()) == {'enabled': True}

    assert ticks[-1] == 'closed'
    persisted = snapshot(control.directory)
    assert persisted['account'] == 'anlin9' and persisted['password'] == 'synthetic-pass'

    resumed, control2 = _app(tmp_path, session=session, login=login, clock=lambda: now[0])
    with TestClient(resumed) as client:
        assert client.get('/api/im/quote').status_code == 401
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not control2.status('system')['collector_active']:
            time.sleep(0.01)
        assert control2.status('system')['collector_active'] is True
        before = len([x for x in ticks if x != 'closed'])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and len([x for x in ticks if x != 'closed']) <= before:
            time.sleep(0.01)
        assert len([x for x in ticks if x != 'closed']) > before
        assert logins == [('anlin9', 'synthetic-pass')]


def test_innodealing_session_expiry_relogs_without_portal_user(tmp_path):
    import asyncio
    rounds = {'n': 0}
    logins = []

    def login(username, password):
        logins.append(username)
        return {'cookie': 'sid=refresh-' + str(len(logins)), 'sid': 'refresh-' + str(len(logins))}

    async def session(values, update):
        rounds['n'] += 1
        if rounds['n'] == 1:
            raise QuoteAuthenticationError('QUOTE_TOKEN_MISSING')
        assert values['sid'] == 'refresh-2'
        update(state='streaming')
        await asyncio.Event().wait()

    app, control = _app(tmp_path, session=session, login=login, clock=lambda: 1_800_000_000)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + app.state.auth.issue_token(AuthUser('alice', 'Alice'))}
        assert client.put('/api/im/quote', json={'account': 'anlin9', 'password': 'synthetic-pass'},
                          headers=headers).status_code == 200
        assert client.post('/api/im/quote/start', headers=headers).status_code == 200
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and control.status('system')['collector_state'] != 'streaming':
            time.sleep(0.01)
        assert control.status('system')['collector_state'] == 'streaming'
        assert control.status('system')['collector_active'] is True
    assert logins == ['anlin9', 'anlin9']
    assert snapshot(control.directory)['sid'] == 'refresh-2'
