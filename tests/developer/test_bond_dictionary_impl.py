"""Synthetic dictionary resolution and single-worker queue tests; no external I/O."""
import asyncio
import datetime as dt
import importlib
import json
from pathlib import Path
import threading
import sqlite3

import pytest


def module():
    assert (Path(__file__).resolve().parents[2] / 'collector/bond_dictionary.py').is_file(), 'Dictionary resolver missing'
    return importlib.import_module('collector.bond_dictionary')


def row(uni):
    return {'bond_uni_code': uni, 'bond_code': f'{uni:06}.IB', 'bond_short_name': '合成测试债券', 'com_short_name': None}


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_protocol_body_dedup_and_datas_precedence(monkeypatch):
    mod = module()
    monkeypatch.delenv('XUNJIA_EGRESS_GATEWAY', raising=False)
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'code': 0, 'datas': [{'bondUniCode': 7, 'bondCode': '000007.IB', 'bondShortName': '合成测试债券'}], 'data': []}
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['verify'] is True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    result = mod.fetch_innodealing([7, 7], 'sid=synthetic', 'synthetic-device', client_factory=Client)
    assert result == [row(7)]
    url, args = calls[0]
    assert url.endswith('/onshore-bond-basic/api/bond/filter/simple/v3/onshore/batch')
    assert json.loads(args['content']) == {'bondUniCodes': [7]}
    assert args['headers']['Content-Type'] == 'text/plain'
    assert args['headers']['Cookie'] == 'sid=synthetic'
    assert 'sign' in args['params']


@pytest.mark.parametrize('bad', [
    {'bond_uni_code': True}, {'bond_uni_code': 9}, {'bond_code': ''},
    {'bond_code': 'UNI7'}, {'bond_short_name': ''}, {'bond_short_name': '未入字典7'},
])
def test_invalid_identity_never_becomes_dictionary_entry(bad):
    mod = module()
    with pytest.raises(mod.DictionaryError):
        mod.normalize_rows([{**row(7), **bad}], [7])


def test_json_external_is_first_and_only_missing_ids_reach_inno(tmp_path):
    mod = module()
    private_json(tmp_path / 'rows.json', [row(1)])
    config = private_json(tmp_path / 'dict.json', {'source': 'json', 'file': 'rows.json'})
    external = mod.load_external(config)
    seen = []
    def fetch(ids, cookie, device):
        seen.append(ids)
        return [row(2)]
    resolver = mod.DictionaryResolver('synthetic', 'device', external=external, fetch=fetch)
    assert resolver.resolve([1, 2]) == [row(1), row(2)]
    assert seen == [[2]]


def test_explicit_mode_required_and_invalid_configuration_fails(tmp_path):
    mod = module()
    assert mod.resolver_from_env('synthetic', 'device', {}) is None
    config = private_json(tmp_path / 'dict.json', {'source': 'sql', 'query': 'SELECT anything'})
    with pytest.raises(mod.DictionaryError):
        mod.resolver_from_env('synthetic', 'device', {'QB_DICTIONARY_SOURCE': 'innodealing', 'QB_DICTIONARY_CONFIG': str(config)})


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self.rows = {}
        self.conn = self
    def ping(self, **kwargs): pass
    def begin(self): self.before = dict(self.rows)
    def commit(self): pass
    def rollback(self): self.rows = self.before
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, parameters):
        assert sql.startswith('SELECT')
        self.selected = [tuple(self.rows[x].values()) for x in parameters if x in self.rows]
    def fetchall(self): return self.selected
    def executemany(self, sql, values):
        assert sql.startswith('INSERT') and 'ON DUPLICATE KEY UPDATE' in sql
        for values_row in values:
            self.rows[values_row[0]] = dict(zip(row(1), values_row))
        return len(values)


def test_worker_batches_deduplicates_and_retries_only_missing():
    mod = module()
    store = Store()
    store.rows[1] = row(1)
    seen = []
    class Resolver:
        def resolve(self, ids):
            seen.append(ids)
            return [row(x) for x in ids if x != 2 or len(seen) > 2]
    async def run():
        worker = mod.DictionaryWorker(Resolver(), store, retry_seconds=0)
        for i in list(range(1, 151)) * 2:
            worker.enqueue(i)
        assert len(worker.pending) == 150
        await worker.run_once()
        assert 1 not in worker.pending and 2 in worker.pending
        await worker.run_once()
        await worker.run_once()
        assert all(len(batch) <= 100 and len(batch) == len(set(batch)) for batch in seen)
        assert set(store.rows) == set(range(1, 151))
        assert not worker.pending
        await worker.close()
    asyncio.run(run())


def test_worker_failure_preserves_pending_and_existing_success():
    mod = module()
    store = Store()
    store.rows[1] = row(1)
    class Broken:
        def resolve(self, ids): raise RuntimeError('synthetic outage')
    async def run():
        worker = mod.DictionaryWorker(Broken(), store, retry_seconds=0)
        worker.enqueue(1)
        worker.enqueue(2)
        await worker.run_once()
        assert 2 in worker.pending
        assert store.rows == {1: row(1)}
        worker.start()
        first = worker.task
        worker.start()
        assert worker.task is first
        await worker.close()
        assert worker.task.done()
    asyncio.run(run())


def test_restart_recovery_selects_bounded_current_day_real_ids_only():
    mod = module()
    class Cursor:
        def __init__(self, connection): self.cursor = connection.cursor()
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def execute(self, sql, params):
            assert sql.startswith('SELECT DISTINCT') and 'ORDER BY id DESC LIMIT %s' in sql
            self.cursor.execute(sql.replace('%s', '?'), tuple(x.isoformat(' ') if isinstance(x, dt.datetime) else x for x in params))
        def fetchall(self): return self.cursor.fetchall()
    class Connection:
        def __init__(self): self.db = sqlite3.connect(':memory:')
        def ping(self, **kwargs): pass
        def cursor(self): return Cursor(self.db)
    store = Store()
    store.conn = Connection()
    connection = store.conn.db
    for table in ('qb_live_best_quote', 'qb_live_trade'):
        connection.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY, bond_uni_code INTEGER, capture_time TEXT)')
    connection.execute('CREATE TABLE qb_bond_dict(bond_uni_code INTEGER PRIMARY KEY, bond_code TEXT, bond_short_name TEXT)')
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    connection.executemany('INSERT INTO qb_live_best_quote VALUES(?,?,?)',
                          [(1,100,str(today)), (2,101,str(today-dt.timedelta(days=1))), (3,102,str(today)), (4,103,str(today))])
    connection.executemany('INSERT INTO qb_live_trade VALUES(?,?,?)', [(1,104,str(today)), (2,100,str(today))])
    connection.execute('INSERT INTO qb_bond_dict VALUES(102,?,?)', ('000102.IB','真实已有简称'))
    try:
        assert set(mod.load_pending_ids(store, limit=10)) == {100,103,104}
        assert len(mod.load_pending_ids(store, limit=2)) <= 2
    finally:
        connection.close()


def test_restart_seed_uses_same_worker_and_finishes_without_new_frame(monkeypatch):
    mod = module()
    store = Store()
    calls = []
    monkeypatch.setattr(mod, 'load_pending_ids', lambda store, **kw: [7,7])
    class Resolver:
        def resolve(self, ids):
            calls.append(ids)
            return [row(7)]
    async def run():
        worker = mod.DictionaryWorker(Resolver(),store,interval=0.01)
        task = worker.start()
        assert worker.start() is task
        for _ in range(100):
            if 7 in store.rows: break
            await asyncio.sleep(0.01)
        else: pytest.fail('Persisted unnamed quote did not seed dictionary work')
        await worker.close()
        assert task.done() and calls == [[7]]
    asyncio.run(run())
