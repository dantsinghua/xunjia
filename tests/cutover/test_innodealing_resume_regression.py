"""Independent offline recovery gates; synthetic credentials/feed, real app/cache.

These gates prove wiring and lifecycle, not authentication or live vendor traffic.
"""
import asyncio
from datetime import datetime
import json
import time

from fastapi.testclient import TestClient
import httpx
import pymysql
import pytest

from collector import qb_gateway
from collector.store import Q_COLS, T_COLS
from imlink.quote_source import save_quote, snapshot
from server import market_cache, qb_source
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.quote_control import check_database
from server.quote_runtime import QBMarket


@pytest.mark.parametrize('failure,expected_state,expected_code', [
    ('missing-token', 'auth_failed', 'QUOTE_AUTH_REJECTED'),
    ('network-timeout', 'retrying', 'QUOTE_CONNECTION_FAILED'),
])
def test_authentication_envelope_and_network_failures_are_distinct(
        tmp_path, monkeypatch, failure, expected_state, expected_code):
    """Use the actual authenticate -> managed collector -> controller error path."""
    from collector import managed_session
    from server.quote_control import QuoteControl

    class Response:
        def raise_for_status(self): pass
        def json(self): return {'data': None}

    class Client:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, *_, **__):
            if failure == 'network-timeout':
                raise httpx.ConnectTimeout('synthetic private diagnostic detail')
            return Response()

    monkeypatch.setattr(qb_gateway.httpx, 'Client', Client)
    save_quote({'cookie': 'sid=synthetic-review-secret',
                'device_id': 'synthetic-review-device'}, tmp_path)

    async def run():
        async def session(values, update):
            await managed_session.collect(values, update, {})

        control = QuoteControl(directory=tmp_path, reader=object(), managers=['manager'],
                               session=session, db_check=lambda: {'ok': True})
        try:
            await control.start('manager')
            for _ in range(100):
                await asyncio.sleep(.01)
                status = control.status('manager')
                if status['collector_state'] in ('auth_failed', 'retrying'): break
            assert status['collector_state'] == expected_state
            assert status['code'] == expected_code
            assert 'synthetic-review-secret' not in status['note']
            assert 'synthetic private diagnostic detail' not in status['note']
            if failure == 'missing-token':
                assert not status['collector_active']
                # A rejected account can be corrected without a phantom live task.
                await control.save('manager', {'cookie': 'sid=synthetic-corrected'})
            else:
                assert status['collector_active']
        finally:
            await control.stop('manager')

    asyncio.run(run())


def test_saved_cookie_sid_wins_over_older_sid_file(tmp_path, monkeypatch):
    monkeypatch.setenv('QB_CONFIG_DIR', str(tmp_path))
    save_quote({'cookie': 'uid=synthetic; sid=old-cookie',
                'sid': 'old-independent-sid', 'device_id': 'old-device'}, tmp_path)
    # The caller parses the newly authorized Cookie and saves its device as well.
    current = 'uid=synthetic; sid=current-cookie; device_id=current-device'
    save_quote({'cookie': current, 'device_id': 'current-device'}, tmp_path)
    cookie, device = qb_gateway.load_creds()
    assert cookie == current
    assert device == 'current-device'
    assert snapshot(tmp_path)['sid'] == 'old-independent-sid'
    assert 'old-independent-sid' not in cookie


class ExistingQuoteDatabase:
    """Minimal MySQL protocol seam, retaining an old quote across schema repair."""
    def __init__(self):
        self.missing = {'bid_quote_type', 'ofr_quote_type', 'cnbd_date'}
        self.connections = []
        self.statements = []
        now = datetime.now()
        self.row = dict(id=11, bond_uni_code=100, broker_id=2, seq_ns=11,
                        bid_quote_type=3, ofr_quote_type=1, bid_yield=2.125,
                        bid_vol=1500, bid_pending=0, bid_vol_str='+1',
                        ofr_yield=None, ofr_net_price=99.8, ofr_vol=2000,
                        ofr_pending=0, ofr_vol_str='+1', cnbd_date=None,
                        update_time=now, capture_time=now)

    def connect(self, **options):
        self.connections.append(options)
        owner = self

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def close(self): pass
            def ping(self, **_): pass
            def cursor(self): return Cursor()

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql, params=None):
                owner.statements.append(sql)
                if sql.startswith('SHOW COLUMNS FROM '):
                    table = sql.split()[-1]
                    columns = {'qb_live_best_quote': set(Q_COLS) | {'id'},
                               'qb_live_trade': set(T_COLS) | {'id'},
                               'qb_bond_dict': {'bond_uni_code', 'bond_code', 'bond_short_name'},
                               'qb_broker': {'broker_id', 'broker_name'}}[table]
                    if table == 'qb_live_best_quote': columns -= owner.missing
                    self.rows = [(name,) for name in columns]
                elif sql == 'SELECT 1 FROM qb_bond_dict LIMIT 1':
                    self.rows = [(1,)]
                elif sql.startswith('SELECT bond_uni_code, bond_code, bond_short_name'):
                    self.rows = [(100, '000100.IB', '独立合成债券')]
                elif sql.startswith('SELECT broker_id, broker_name'):
                    self.rows = [(2, '独立合成经纪')]
                elif 'FROM qb_live_best_quote WHERE id >' in sql:
                    if owner.missing: raise pymysql.err.OperationalError(1054, 'synthetic old schema')
                    self.rows = [tuple(owner.row.get(k) for k in qb_source._COLS)] if owner.row['id'] > params[0] else []
                else:
                    raise AssertionError('Unexpected database statement: ' + sql)
            def fetchall(self): return self.rows
            def fetchone(self): return self.rows[0] if self.rows else None

        return Connection()


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(market_cache, '_conn', None)
    monkeypatch.setattr(qb_source, 'DIRTY', set())
    monkeypatch.setattr(qb_source, 'INTENT', {})
    monkeypatch.setattr(qb_source, 'REPLAY', 0)
    values = {'QB_CONFIG_DIR': str(tmp_path / 'credentials'),
              'QB_MYSQL_HOST': 'synthetic-current-host.invalid', 'QB_MYSQL_PORT': '3337',
              'QB_MYSQL_USER': 'synthetic-user', 'QB_MYSQL_PASSWORD': 'synthetic-password',
              'QB_MYSQL_DB': 'synthetic-existing-db', 'XUNJIA_QUOTE_SOURCE': 'qb',
              'XUNJIA_QUOTE_CACHE': str(tmp_path / 'market.db'),
              'XUNJIA_QUOTE_MANAGERS': 'manager', 'XUNJIA_QUOTE_AUTOSTART': '1'}
    for key, value in values.items(): monkeypatch.setenv(key, value)
    database = ExistingQuoteDatabase()
    monkeypatch.setattr(pymysql, 'connect', database.connect)
    save_quote({'cookie': 'sid=synthetic-current', 'sid': 'synthetic-old',
                'device_id': 'synthetic-device'}, tmp_path / 'credentials')
    created = []

    def app():
        value = create_app(database_url='sqlite:///' + str(tmp_path / 'business.db'),
                           auth_config=AuthConfig(sso_secret='synthetic', session_secret='s' * 40),
                           enable_background=False)
        created.append(value)
        return value

    yield database, app, tmp_path
    for value in created:
        source = value.state.quote_control.reader.source
        if source.conn: source.conn.close()
    if market_cache._conn is not None: market_cache._conn.close()


def test_current_environment_reaches_reader_after_module_import(recovery):
    _, make_app, _ = recovery
    app = make_app()
    source = app.state.quote_control.reader
    assert isinstance(source, QBMarket)
    expected = {'host': 'synthetic-current-host.invalid', 'port': 3337,
                'user': 'synthetic-user', 'password': 'synthetic-password',
                'database': 'synthetic-existing-db'}
    assert {key: source.source.connection_options[key] for key in expected} == expected


def test_old_schema_saved_credentials_current_entrypoint_and_resume(recovery):
    database, make_app, folder = recovery
    sessions = {'started': 0, 'closed': 0}

    async def session(values, update):
        assert qb_gateway.merge_sid(values['cookie'], values['sid']) == 'sid=synthetic-current'
        sessions['started'] += 1
        update(state='streaming')
        try: await asyncio.Event().wait()
        finally: sessions['closed'] += 1

    app = make_app()
    control = app.state.quote_control
    control._session = session  # No vendor connection; lifecycle seam only.
    token = app.state.auth.issue_token(AuthUser('manager', 'manager'))
    headers = {'Authorization': 'Bearer ' + token}
    before = snapshot(control.directory)
    with TestClient(app) as client:
        result = check_database()
        assert result['code'] == 'QUOTE_SCHEMA_MIGRATION_REQUIRED'
        for name in sorted(database.missing): assert name in result['note']
        denied = client.post('/api/im/quote/start', headers=headers)
        assert denied.status_code == 400
        assert sessions['started'] == 0
        # Represent the explicitly applied three-column migration at the SQL seam.
        database.missing.clear()
        assert check_database()['code'] == 'QUOTE_DATABASE_READY'
        for _ in range(2):
            assert client.post('/api/im/quote/start', headers=headers).status_code == 200
        assert sessions['started'] == 1
        deadline = time.monotonic() + 4
        while True:
            response = client.get('/api/bonds', headers=headers)
            if response.status_code == 200 and response.json()['data']: break
            assert time.monotonic() < deadline, response.text
            time.sleep(.05)
        row = response.json()['data'][0]
        assert row['code'] == '000100.IB' and row['name'] == '独立合成债券'
        assert row['ob'] == 2.125 and row['obv'] == 1500
        assert row['oo'] is None  # A net price must not appear as yield.
        assert row['issueTime'] == int(database.row['update_time'].timestamp() * 1000)
        with client.websocket_connect('/ws/quote') as ws:
            ws.send_json({'op': 'auth', 'token': token})
            assert ws.receive_json() == {'op': 'auth', 'ok': True}
            ws.send_json({'op': 'sub', 'groups': ['bond-all']})
            ws.send_json({'op': 'ping'})
            assert ws.receive_json()['op'] == 'pong'
            database.row.update(id=12, seq_ns=12, bid_yield=2.25)
            update = ws.receive_json()
            assert update['op'] == 'batch' and update['group'] == 'bond-all'
            assert update['items'][0]['bb'] == 2.25 and update['items'][0]['bbv'] == 1500
            assert update['items'][0]['s']['windcode'] == row['code']
        assert client.post('/api/im/quote/stop', headers=headers).status_code == 200
        assert sessions['closed'] == 1
        assert client.post('/api/im/quote/start', headers=headers).status_code == 200
        assert sessions['started'] == 2
    # Service shutdown releases its lease but preserves the explicit resume intent.
    assert sessions['closed'] == 2
    assert json.loads((control.directory / 'collector-state.json').read_text()) == {'enabled': True}
    second = make_app()
    second.state.quote_control._session = session
    with TestClient(second) as client:
        for _ in range(2):
            status = client.get('/api/im/quote', headers=headers).json()['data']
            assert status['configured'] and status['collector_active'] and status['reader_configured']
        assert sessions['started'] == 3
        assert client.post('/api/im/quote/stop', headers=headers).status_code == 200
    assert sessions['closed'] == 3
    assert snapshot(control.directory) == before
    assert database.row['id'] == 12
    assert not any(sql.startswith(('DELETE', 'TRUNCATE', 'DROP', 'ALTER')) for sql in database.statements)
