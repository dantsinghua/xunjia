"""Independent real-source dictionary contracts; no external HTTP or database access."""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from collector.store import Q_COLS, T_COLS
from server import quote_control


def dictionary():
    return importlib.import_module('collector.bond_dictionary')


def source_row(uni=1001, code='260001.IB', name='合成验收债'):
    return {'bondUniCode': uni, 'bondCode': code, 'bondShortName': name}


def normalized(uni=1001, code='260001.IB', name='合成验收债'):
    return {'bond_uni_code': uni, 'bond_code': code, 'bond_short_name': name}


def test_dictionary_preserves_real_source_identity_and_leading_zero_code():
    module = dictionary()
    rows = module.normalize_rows([source_row(code='012600001.IB')], [1001])
    assert len(rows) == 1
    assert {key: rows[0][key] for key in normalized()} == normalized(code='012600001.IB')


@pytest.mark.parametrize('row', [source_row(uni=True), source_row(uni='1001'), source_row(uni=1001.0),
                               source_row(uni=1002), source_row(code='UNI1001'), source_row(code='—'),
                               source_row(code=''), source_row(name=''), source_row(name='未入字典1001'),
                               {'bondUniCode': 1001, 'bondCode': '260001.IB'}])
def test_dictionary_rejects_unrequested_or_fabricated_identity(row):
    module = dictionary()
    with pytest.raises(module.DictionaryError):
        module.normalize_rows([row], [1001])


def test_dictionary_conflicting_duplicate_does_not_choose_last_row():
    module = dictionary()
    with pytest.raises(module.DictionaryError):
        module.normalize_rows([source_row(), source_row(code='260002.IB')], [1001])


class HttpDouble:
    def __init__(self, body):
        self.body = body
        self.calls = []
        self.options = None
    def factory(self, **kwargs):
        self.options = kwargs
        return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.body)


def test_innodealing_batch_uses_reviewed_path_cookie_sign_and_datas_precedence():
    module = dictionary()
    http = HttpDouble({'code': 0, 'datas': [source_row()], 'data': [source_row(uni=9999)]})
    result = module.fetch_innodealing([1001, 1001], 'sid=synthetic-cookie', 'synthetic-device',
                                     client_factory=http.factory)
    assert len(result) == 1 and result[0]['bond_uni_code'] == 1001
    assert len(http.calls) == 1
    url, request = http.calls[0]
    assert url.endswith('/onshore-bond-basic/api/bond/filter/simple/v3/onshore/batch')
    assert request['headers']['Cookie'] == 'sid=synthetic-cookie'
    assert request['headers']['Content-Type'] == 'text/plain'
    body = request.get('content', request.get('data'))
    assert json.loads(body) == {'bondUniCodes': [1001]}
    from collector.qb_sign import verify
    assert verify(request['params'], 'synthetic-device')
    assert http.options['verify'] is True and http.options['trust_env'] is False


def test_innodealing_batch_uses_tcp_gateway_without_losing_tls_identity(monkeypatch):
    module = dictionary()
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '172.26.64.36')
    http = HttpDouble({'code': 0, 'data': [source_row()]})
    module.fetch_innodealing([1001], 'synthetic-cookie', 'synthetic-device', client_factory=http.factory)
    url, request = http.calls[0]
    assert url.startswith('https://172.26.64.36:12443/')
    assert request['headers']['Host'] == 'rest.innodealing.com'
    assert request['extensions']['sni_hostname'] == 'rest.innodealing.com'
    assert http.options['verify'] is True and http.options['trust_env'] is False


@pytest.mark.parametrize('body', [{'code': 1, 'data': [source_row()]},
                                {'data': [source_row()]}, {'code': 0, 'data': {'items': [source_row()]}},
                                {'code': 0, 'data': [source_row(uni=9999)]}])
def test_bad_batch_response_cannot_enter_dictionary(body):
    module = dictionary()
    http = HttpDouble(body)
    with pytest.raises(module.DictionaryError):
        module.fetch_innodealing([1001], 'synthetic-cookie', 'synthetic-device', client_factory=http.factory)


@pytest.mark.parametrize('ids', [list(range(1, 102)), [True], ['1001'], [1001.0], [-1]])
def test_batch_rejects_bad_or_oversized_request_before_network(ids):
    module = dictionary()
    http = HttpDouble({'code': 0, 'data': []})
    with pytest.raises(module.DictionaryError):
        module.fetch_innodealing(ids, 'synthetic-cookie', 'synthetic-device', client_factory=http.factory)
    assert http.calls == []


def private_json(path, body):
    path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf8')
    path.chmod(0o600)
    return path


def test_json_dictionary_preserves_source_and_resolver_fetches_only_missing(tmp_path):
    module = dictionary()
    private_json(tmp_path / 'dictionary.json', [normalized(code='012600001.IB')])
    config = private_json(tmp_path / 'config.json', {'source': 'json', 'file': 'dictionary.json'})
    external = module.load_external(config)
    calls = []
    def fetch(ids, *args, **kwargs):
        calls.append(list(ids))
        return [normalized(1002, '260002.IB')]
    resolver = module.DictionaryResolver('synthetic-cookie', 'synthetic-device', external=external, fetch=fetch)
    result = resolver.resolve([1001, 1002, 1002])
    assert calls == [[1002]]
    assert {row['bond_uni_code']: row['bond_code'] for row in result} == {1001: '012600001.IB', 1002: '260002.IB'}


@pytest.mark.parametrize('config', [{'source': 'json', 'file': 'absent.json'},
                                  {'source': 'unknown'}, {'source': 'json', 'file': 'x.json', 'sql': 'SELECT 1'},
                                  {'source': 'mysql', 'sql': 'SELECT * FROM deposit'}])
def test_wrong_external_configuration_fails_explicitly(tmp_path, config):
    module = dictionary()
    path = private_json(tmp_path / 'config.json', config)
    with pytest.raises(module.DictionaryError):
        module.load_external(path)


def test_external_dictionary_failure_is_not_silently_replaced_by_inno():
    module = dictionary()
    class Broken:
        def lookup(self, ids): raise module.DictionaryError('SYNTHETIC_EXTERNAL_FAILURE')
    def forbidden(*args, **kwargs): pytest.fail('Failed external source was silently bypassed')
    resolver = module.DictionaryResolver('synthetic-cookie', 'synthetic-device', external=Broken(), fetch=forbidden)
    with pytest.raises(module.DictionaryError):
        resolver.resolve([1001])


def test_resolver_requires_explicit_mode_and_no_external_is_valid():
    module = dictionary()
    assert module.resolver_from_env('synthetic-cookie', 'synthetic-device', environ={}) is None
    resolver = module.resolver_from_env('synthetic-cookie', 'synthetic-device',
                                        environ={'QB_DICTIONARY_SOURCE': 'innodealing'})
    assert isinstance(resolver, module.DictionaryResolver)


def test_resolver_rejects_configured_missing_external_file(tmp_path):
    module = dictionary()
    with pytest.raises(module.DictionaryError):
        module.resolver_from_env('synthetic-cookie', 'synthetic-device', environ={
            'QB_DICTIONARY_SOURCE': 'innodealing', 'QB_DICTIONARY_CONFIG': str(tmp_path / 'absent.json')})


def mysql_config(tmp_path):
    password = tmp_path / 'password.txt'
    password.write_text('synthetic-password-not-a-real-credential')
    password.chmod(0o600)
    return {'source': 'mysql', 'connection': {'host': 'synthetic-db', 'port': 3306,
            'user': 'readonly', 'database': 'reference', 'password_file': password.name},
            'table': 'reference.bond_view',
            'columns': {'bond_uni_code': 'real_uni', 'bond_code': 'real_code', 'bond_short_name': 'real_name'}}


@pytest.mark.parametrize('damage', ['table-sql', 'column-sql', 'inline-password', 'public-password', 'public-config'])
def test_external_mysql_configuration_rejects_injection_and_unprotected_secret(tmp_path, damage):
    module = dictionary()
    config = mysql_config(tmp_path)
    if damage == 'table-sql': config['table'] = 'bond_view; DELETE FROM deposit'
    elif damage == 'column-sql': config['columns']['bond_code'] = 'code FROM deposit --'
    elif damage == 'inline-password': config['connection']['password'] = 'synthetic-sensitive-value'
    elif damage == 'public-password': (tmp_path / 'password.txt').chmod(0o644)
    path = private_json(tmp_path / 'config.json', config)
    if damage == 'public-config': path.chmod(0o644)
    with pytest.raises(module.DictionaryError):
        module.load_external(path, connect=lambda **kwargs: pytest.fail('Invalid configuration opened a database'))


def test_external_mysql_uses_only_parameterized_read_and_closes_transaction(tmp_path):
    module = dictionary()
    config = private_json(tmp_path / 'config.json', mysql_config(tmp_path))
    calls, connections = [], []
    class Connection:
        closed = False
        rolled_back = False
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args=()): calls.append((sql, args))
        def fetchall(self): return [normalized()]
        def rollback(self): self.rolled_back = True
        def close(self): self.closed = True
    def connect(**kwargs):
        assert kwargs['password'] == 'synthetic-password-not-a-real-credential'
        connection = Connection()
        connections.append(connection)
        return connection
    source = module.load_external(config, connect=connect)
    result = source.lookup([1001])
    assert result[0]['bond_uni_code'] == 1001
    assert all(sql.upper().startswith(('SELECT ', 'SET TRANSACTION READ ONLY', 'START TRANSACTION READ ONLY')) for sql, _ in calls)
    selects = [(sql, args) for sql, args in calls if sql.upper().startswith('SELECT ')]
    assert len(selects) == 1 and '%s' in selects[0][0]
    assert list(selects[0][1]) == [1001]
    assert '1001' not in selects[0][0]
    assert connections and all(conn.closed and conn.rolled_back for conn in connections)


class StoreDouble:
    def __init__(self):
        self._lock = threading.RLock()
        self.rows = {}
        self.statements = []
        store = self
        class Cursor:
            selected = ()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, args=()):
                store.statements.append((sql, args))
                assert sql.lstrip().upper().startswith('SELECT '), sql
                self.selected = args
            def fetchall(self):
                return [(uni, store.rows[uni]['bond_code'], store.rows[uni]['bond_short_name'])
                        for uni in self.selected if uni in store.rows]
            def executemany(self, sql, values):
                assert sql.lstrip().upper().startswith('INSERT INTO QB_BOND_DICT '), sql
                assert '%s' in sql
                values = list(values)
                store.statements.append((sql, values))
                for row in values:
                    store.rows[row[0]] = normalized(*row[:3])
                return len(values)
            def close(self): pass
        self.conn = SimpleNamespace(cursor=Cursor, begin=lambda: None, commit=lambda: None, rollback=lambda: None,
                                    ping=lambda **kwargs: None)


def test_worker_batches_unique_ids_at_most_100_and_partial_response_stays_pending():
    module = dictionary()
    store = StoreDouble()
    calls = []
    class Resolver:
        def resolve(self, ids):
            calls.append(list(ids))
            return [normalized(ids[0])]
    worker = module.DictionaryWorker(Resolver(), store, retry_seconds=0)
    for uni in list(range(1, 206)) * 2:
        worker.enqueue(uni)
    assert len(worker.pending) == 205
    asyncio.run(worker.run_once())
    assert len(calls) == 1 and 1 <= len(calls[0]) <= 100
    assert len(calls[0]) == len(set(calls[0]))
    assert set(store.rows) == {calls[0][0]}
    assert set(worker.pending) == set(range(1, 206)) - {calls[0][0]}


def test_worker_failure_preserves_successful_dictionary_and_pending_ids():
    module = dictionary()
    store = StoreDouble()
    store.rows[1001] = normalized()
    class Resolver:
        def resolve(self, ids): raise module.DictionaryError('SYNTHETIC_HTTP_FAILURE')
    worker = module.DictionaryWorker(Resolver(), store, retry_seconds=0)
    worker.enqueue(1001)
    worker.enqueue(1002)
    try:
        asyncio.run(worker.run_once())
    except module.DictionaryError:
        pass
    assert store.rows == {1001: normalized()}
    assert 1002 in worker.pending
    assert not any(sql.upper().startswith(('DELETE ', 'TRUNCATE ', 'UPDATE ')) for sql, _ in store.statements)


def test_worker_start_is_idempotent_and_close_leaves_no_owned_task():
    module = dictionary()
    class Resolver:
        def resolve(self, ids): pytest.fail('Idle worker made a request')
    worker = module.DictionaryWorker(Resolver(), StoreDouble(), interval=0.01)
    async def verify():
        before = asyncio.all_tasks()
        worker.start()
        first = asyncio.all_tasks() - before
        worker.start()
        assert len(first) == 1 and asyncio.all_tasks() - before == first
        await worker.close()
        assert all(task.done() for task in first)
    asyncio.run(verify())


def test_managed_quote_is_persisted_before_dictionary_enqueue_and_worker_is_closed(monkeypatch):
    module = dictionary()
    monkeypatch.setenv('QB_DICTIONARY_SOURCE', 'innodealing')
    from collector import managed_session as session
    import pymysql
    events = []
    class Worker:
        def __init__(self, *args, **kwargs): events.append('worker-created')
        def start(self): events.append('worker-started')
        def enqueue(self, uni):
            assert uni == 1001
            assert 'quote-saved' in events
            events.append('dictionary-enqueued')
        async def close(self): events.append('worker-closed')
    class Store:
        def __init__(self, *args):
            self._lock = threading.RLock()
            self.conn = None
        def save(self, path, rows):
            assert rows == [{'bond_uni_code': 1001}]
            events.append('quote-saved')
            return 1
    class Websocket:
        read = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def send(self, payload): pass
        async def recv(self):
            if self.read: raise asyncio.CancelledError
            self.read = True
            return b'synthetic-frame'
    resolver = lambda *args, **kwargs: object()
    monkeypatch.setattr(module, 'resolver_from_env', resolver)
    monkeypatch.setattr(module, 'DictionaryWorker', Worker)
    for name, replacement in [('resolver_from_env', resolver), ('DictionaryWorker', Worker)]:
        if hasattr(session, name): monkeypatch.setattr(session, name, replacement)
    monkeypatch.setattr(session, 'Store', Store)
    monkeypatch.setattr(session, 'authenticate', lambda *args: {'t': 'synthetic-token'})
    monkeypatch.setattr(session, 'market_connect', lambda *args: Websocket())
    monkeypatch.setattr(pymysql, 'connect', lambda **kwargs: SimpleNamespace(close=lambda: events.append('store-closed')))
    monkeypatch.setattr(session.qb_ws, 'decode_packet', lambda raw: {'eventType': 16, 'data': b'synthetic'})
    monkeypatch.setattr(session.qb_ws, 'decode_group_message', lambda data: {'messageType': 1, 'messageContent': b'synthetic'})
    monkeypatch.setattr(session.qb_apollo, 'parse_push_quote', lambda payload: {'bond_uni_code': 1001})
    async def run():
        try:
            await session.collect({'cookie': 'synthetic-cookie', 'device_id': 'synthetic-device', 'sid': ''},
                                  lambda **kwargs: None, {})
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    assert events.count('worker-created') == 1 and events.count('dictionary-enqueued') == 1
    assert events.index('quote-saved') < events.index('dictionary-enqueued')
    assert events.index('worker-closed') < events.index('store-closed')


def test_previously_received_quote_becomes_publishable_when_real_identity_arrives(monkeypatch):
    from server import qb_source
    source = qb_source.QBSource({'host': 'synthetic-never-connected'})
    source._purged_unnamed = True
    statics, published = [], []
    monkeypatch.setattr(source, '_upsert_static', lambda rows: statics.extend(rows))
    def persist(rows):
        published.extend(rows)
        return list(range(1, len(rows) + 1))
    monkeypatch.setattr(qb_source.db, 'upsert_quotes', persist)
    now = datetime.now()
    quote = {'id': 1, 'bond_uni_code': 1001, 'broker_id': 1, 'seq_ns': 1,
             'bid_quote_type': 3, 'ofr_quote_type': 3, 'bid_yield': 2.1, 'ofr_yield': 2.2,
             'bid_vol': 1000, 'ofr_vol': 2000, 'bid_pending': 0, 'ofr_pending': 0,
             'bid_vol_str': '+1', 'ofr_vol_str': '+1', 'update_time': now, 'capture_time': now}
    assert source._poll_rows([quote], set()) == []
    assert source.book[1001][1] == quote and not published
    source.dict[1001] = ('012600001.IB', '合成真实源身份')
    result = source._poll_rows([], set())
    assert len(result) == 1 and len(published) == 1
    assert statics[0]['windcode'] == '012600001.IB' and statics[0]['sec_name'] == '合成真实源身份'
    assert published[0]['best_bid'] == 2.1 and published[0]['best_ofr'] == 2.2


def test_restart_recovery_queries_only_bounded_today_unknown_ids():
    module = dictionary()
    connection = sqlite3.connect(':memory:')
    for table in ('qb_live_best_quote', 'qb_live_trade'):
        connection.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY,bond_uni_code INTEGER,capture_time TEXT)')
    connection.execute('CREATE TABLE qb_bond_dict (bond_uni_code INTEGER PRIMARY KEY,bond_code TEXT,bond_short_name TEXT)')
    since = datetime(2026, 10, 26)
    today, yesterday = since.isoformat(' '), (since - timedelta(days=1)).isoformat(' ')
    connection.executemany('INSERT INTO qb_live_best_quote VALUES(?,?,?)',
                           [(1, 1001, today), (2, 1002, yesterday), (3, 1003, today), (4, 1004, today)])
    connection.executemany('INSERT INTO qb_live_trade VALUES(?,?,?)', [(1, 1001, today), (2, 1005, today)])
    connection.executemany('INSERT INTO qb_bond_dict VALUES(?,?,?)',
                           [(1003, '260003.IB', '合成已知债'), (1004, 'UNI1004', '未入字典')])
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args):
            calls.append((sql, args))
            assert sql.startswith('SELECT ')
            self.result = connection.execute(sql.replace('%s', '?'),
                                             tuple(value.isoformat(' ') if isinstance(value, datetime) else value for value in args))
        def fetchall(self): return self.result.fetchall()
    store = SimpleNamespace(_lock=threading.RLock(), conn=SimpleNamespace(cursor=Cursor, ping=lambda **kwargs: None))
    try:
        assert module.load_pending_ids(store, since=since, limit=10) == [1001, 1004, 1005]
        assert len(module.load_pending_ids(store, since=since, limit=2)) <= 2
        assert all(sql.count('LIMIT') >= 2 and 'ORDER BY id DESC' in sql for sql, _ in calls)
        assert all(args[0] <= 10 and args[1] == since for _, args in calls)
        assert connection.execute('SELECT COUNT(*) FROM qb_live_best_quote').fetchone()[0] == 4
    finally:
        connection.close()


@pytest.mark.parametrize('recovery_error', [False, True])
def test_worker_recovers_without_new_quote_or_survives_recovery_timeout(monkeypatch, recovery_error):
    module = dictionary()
    calls = []
    def recover(store, **kwargs):
        if recovery_error:
            raise TimeoutError('synthetic recovery timeout')
        return [1001, 1001]
    monkeypatch.setattr(module, 'load_pending_ids', recover)
    async def verify():
        observed = asyncio.Event()
        loop = asyncio.get_running_loop()
        class Resolver:
            def resolve(self, ids):
                calls.append(list(ids))
                loop.call_soon_threadsafe(observed.set)
                return []
        worker = module.DictionaryWorker(Resolver(), StoreDouble(), interval=0.01, retry_seconds=60)
        if recovery_error:
            worker.enqueue(1002)
        worker.start()
        task = worker.task
        try:
            await asyncio.wait_for(observed.wait(), timeout=2)
            assert worker.task is task
            assert calls == [[1002 if recovery_error else 1001]]
            assert (1002 if recovery_error else 1001) in worker.pending
            if recovery_error:
                assert worker.failures >= 1
        finally:
            await worker.close()
        assert task.done()
    asyncio.run(verify())


class SchemaDatabase:
    def __init__(self, *, missing=None, named=False):
        self.missing = missing
        self.named = named
        self.sql = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def cursor(self): return self
    def execute(self, sql):
        self.sql.append(sql)
        self.current = sql
    def fetchall(self):
        required = {'qb_live_best_quote': set(Q_COLS) | {'id'},
                    'qb_live_trade': set(T_COLS) | {'id'},
                    'qb_bond_dict': {'bond_uni_code', 'bond_code', 'bond_short_name', 'com_short_name'},
                    'qb_broker': {'broker_id', 'broker_name'}}
        table = self.current.removeprefix('SHOW COLUMNS FROM ')
        fields = required[table]
        if self.missing and self.missing[0] == table:
            fields = fields - {self.missing[1]}
        return [(field,) for field in fields]
    def fetchone(self): return (1,) if self.named else None


@pytest.mark.parametrize('mode,allowed', [(None, False), ('innodealing', True)])
def test_empty_dictionary_requires_explicit_innodealing_mode(tmp_path, monkeypatch, mode, allowed):
    if mode is None:
        monkeypatch.delenv('QB_DICTIONARY_SOURCE', raising=False)
    else:
        monkeypatch.setenv('QB_DICTIONARY_SOURCE', mode)
    database = SchemaDatabase()
    import pymysql
    monkeypatch.setattr(pymysql, 'connect', lambda **kwargs: database)
    monkeypatch.setattr(quote_control, 'merged', lambda *args: {'cookie': 'synthetic-cookie',
                                                            'sid': '', 'device_id': 'synthetic-device'})
    async def session(*args):
        await asyncio.Event().wait()
    control = quote_control.QuoteControl(directory=tmp_path / 'credentials', reader=SimpleNamespace(),
                                         session=session,
                                         db_check=lambda: quote_control.check_database({'host': 'synthetic'}))
    async def verify():
        try:
            if allowed:
                result = await control.start('synthetic-user', persist=False)
                assert result['collector_active'] is True
            else:
                with pytest.raises(ValueError):
                    await control.start('synthetic-user', persist=False)
                assert control._task is None
        finally:
            await control.stop('synthetic-user', persist=False)
    asyncio.run(verify())
    assert all(sql.startswith(('SHOW ', 'SELECT ')) for sql in database.sql)


@pytest.mark.parametrize('missing', [('qb_bond_dict', 'bond_code'),
                                   ('qb_live_best_quote', 'bid_quote_type')])
def test_automatic_dictionary_never_bypasses_missing_schema(monkeypatch, missing):
    monkeypatch.setenv('QB_DICTIONARY_SOURCE', 'innodealing')
    database = SchemaDatabase(missing=missing)
    import pymysql
    monkeypatch.setattr(pymysql, 'connect', lambda **kwargs: database)
    result = quote_control.check_database({'host': 'synthetic'})
    assert result['ok'] is False
    assert result['code'] == 'QUOTE_SCHEMA_MIGRATION_REQUIRED'
    assert all(sql.startswith(('SHOW ', 'SELECT ')) for sql in database.sql)
