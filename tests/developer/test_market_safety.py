import datetime as dt
import struct
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from collector import qb_gateway, store
from collector.qb_apollo import parse_push_quote
from server import market_cache as db
from server.qb_source import QBSource
from server import qb_source


def test_typed_zero_and_negative_yields_are_valid():
    src = QBSource()
    for value in (0.0, -0.25):
        raw = bytes([128, 1, 3, 113]) + struct.pack('<d', value)
        row = parse_push_quote(raw)
        assert row['bid_yield'] == value
        row.update(broker_id=2, update_time=dt.datetime(2026, 10, 13, 10), bid_vol=100)
        assert src._best_of([row])['best_bid'] == value


def test_static_batch_rolls_back_if_second_row_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_conn', None)
    connection = db.init_db(path=str(tmp_path / 'market.db'))
    try:
        rows = [dict(bond_uni_code='1', windcode='1.IB', sec_name='one'),
                dict(bond_uni_code='2', windcode=None, sec_name='two')]
        with pytest.raises(Exception):
            QBSource()._upsert_static(rows)
        assert not connection.in_transaction
        assert connection.execute('SELECT count(*) FROM bond_static').fetchone()[0] == 0
        QBSource()._upsert_static(rows[:1])
        assert connection.execute('SELECT count(*) FROM bond_static').fetchone()[0] == 1
    finally:
        connection.close()


@pytest.fixture
def local_market(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_conn', None)
    monkeypatch.setattr(qb_source, 'DIRTY', set())
    monkeypatch.setattr(qb_source, 'INTENT', {})
    connection = db.init_db(path=str(tmp_path / 'local.db'))
    rows = []
    src = QBSource()
    src.dict = {100: ('100.IB', 'synthetic'), 200: ('200.IB', 'synthetic2')}
    monkeypatch.setattr(src, '_refresh_dict', lambda: None)
    monkeypatch.setattr(src, '_fetch', lambda: [r for r in rows if r['id'] > src.last_id])
    def add(index, day=13, uni=100, **values):
        when = dt.datetime(2026, 10, day, 10, 0, index)
        rows.append(dict(id=index, seq_ns=index, bond_uni_code=uni, broker_id=2,
                         bid_yield=values.pop('bid_yield', 2.1), bid_vol=100, bid_quote_type=3,
                         ofr_yield=None, ofr_quote_type=0, bid_pending=0, ofr_pending=0,
                         update_time=when, capture_time=when, **values))
    try:
        yield src, connection, add
    finally:
        connection.close()


def test_withdrawal_preserves_internal_intent(local_market):
    src, connection, add = local_market
    qb_source.INTENT[100] = {'ib': 2.3, 'ibv': 500}
    add(1)
    src.poll()
    add(2, bid_yield=None)
    src.poll()
    row = connection.execute('SELECT * FROM quote_latest').fetchone()
    assert row['best_bid'] is None
    assert row['ib'] == 2.3 and row['ibv'] == 500


def test_next_day_invalidates_bonds_without_new_rows(local_market):
    src, connection, add = local_market
    add(1, day=12)
    src.poll()
    add(2, day=13, uni=200)
    src.poll()
    row = connection.execute("SELECT * FROM quote_latest WHERE bond_uni_code='100'").fetchone()
    assert row['best_bid'] is None
    assert 100 not in src.book


def test_post_commit_exception_resumes_from_committed_checkpoint(local_market, monkeypatch):
    src, connection, add = local_market
    add(1)
    class UncertainCommit:
        failed = False
        def __getattr__(self, key):
            return getattr(connection, key)
        def execute(self, sql, *args):
            result = connection.execute(sql, *args)
            if sql == 'COMMIT' and not self.failed:
                self.failed = True
                raise OSError('commit reply lost')
            return result
    monkeypatch.setattr(db, '_conn', UncertainCommit())
    with pytest.raises(OSError, match='commit reply lost'):
        src.poll()
    assert connection.execute('SELECT count(*) FROM quote_flow').fetchone()[0] == 1
    assert src.poll() == []
    assert src.last_id == 1
    assert connection.execute('SELECT count(*) FROM quote_flow').fetchone()[0] == 1


def test_replay_resets_book_before_next_cycle(monkeypatch):
    monkeypatch.setattr(qb_source, 'REPLAY', 100)
    src = QBSource()
    when = dt.datetime.now()
    src._rp = [dict(id=1, update_time=when), dict(id=2, update_time=when)]
    src._rp_i = 0
    src._rp_t0 = time.time() - 1
    src._rp_d0 = when
    src.stats['replay'] = {}
    assert len(src._fetch_replay(1)) == 1
    assert len(src._fetch_replay(1)) == 1
    src.book = {100: {2: {'seq_ns': 50}}}
    src._fetch_replay(1)
    assert src.book == {}


def test_store_requires_explicit_schema_migration(monkeypatch):
    executed = []
    class OldSchema:
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def execute(self, sql):
            executed.append(sql)
        def fetchall(self):
            return [('bid_yield',)]
        def close(self):
            pass
    monkeypatch.setattr(qb_source.pymysql, 'connect', lambda **_: OldSchema())
    with pytest.raises(RuntimeError, match='migrate_quote_truth.sql'):
        store.Store()
    assert not any(sql.startswith('ALTER TABLE') for sql in executed)


def test_intent_changed_during_poll_remains_dirty(local_market, monkeypatch):
    src, connection, add = local_market
    add(1)
    qb_source.INTENT[100] = {'ib': 2.3}
    qb_source.DIRTY.add(100)
    write = db.upsert_quotes
    def change_intent_after_write(quotes):
        ids = write(quotes)
        qb_source.INTENT[100] = {'ib': 2.4}
        qb_source.DIRTY.add(100)
        return ids
    monkeypatch.setattr(db, 'upsert_quotes', change_intent_after_write)
    src.poll()
    assert 100 in qb_source.DIRTY
    monkeypatch.setattr(db, 'upsert_quotes', write)
    src.poll()
    assert connection.execute('SELECT ib FROM quote_latest').fetchone()[0] == 2.4


def test_source_detail_connection_serializes_five_readers(monkeypatch):
    src = QBSource()
    row = dict(broker_id=2, bid_yield=-.1, bid_quote_type=3, bid_vol=100,
               ofr_yield=None, ofr_quote_type=0, ofr_vol=None,
               bid_pending=0, ofr_pending=0, bid_vol_str='+1', ofr_vol_str=None,
               update_time=dt.datetime(2026, 10, 13))
    src.book = {100: {2: row}}
    class Feed:
        active = 0
        peak = 0
        def cursor(self):
            return self
        def __enter__(self):
            self.active += 1
            self.peak = max(self.peak, self.active)
            time.sleep(.005)
            return self
        def __exit__(self, *_):
            self.active -= 1
        def execute(self, *args):
            pass
        def fetchall(self):
            return []
    feed = Feed()
    monkeypatch.setattr(src, '_c', lambda: feed)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: src.detail('100'), range(5)))
    assert feed.peak == 1
    assert all(result['quotes'][0]['best_bid'] == -.1 for result in results)
    assert all(result['quotes'][0]['best_ofr_side'] is None for result in results)


def test_collector_uses_configured_directory_and_sid(tmp_path, monkeypatch):
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    (legacy / 'cookie.txt').write_text('old=1')
    (legacy / 'device_id.txt').write_text('old-device')
    monkeypatch.setattr(qb_gateway, 'CFG', legacy)
    monkeypatch.setenv('QB_CONFIG_DIR', str(tmp_path))
    (tmp_path / 'cookie.txt').write_text('x=1; other_sid=ignored')
    (tmp_path / 'sid.txt').write_text('fresh-session')
    (tmp_path / 'device_id.txt').write_text('fake-device')
    cookie, device = qb_gateway.load_creds()
    assert cookie == 'x=1; other_sid=ignored; sid=fresh-session'
    assert device == 'fake-device'


def test_store_keeps_quote_type_and_input_raw():
    source = store.Store(use_db=False)
    row = {'raw': {'sample': 1}, 'bid_quote_type': 1, 'ofr_quote_type': 3}
    source.save(store.BEST_QUOTES, [row])
    assert row['raw'] == {'sample': 1}
    assert {'bid_quote_type', 'ofr_quote_type'} <= set(store.Q_COLS)


def test_collector_connection_serializes_writes_and_rolls_back():
    source = store.Store(use_db=False)

    class Connection:
        active = 0
        peak = 0
        commits = 0
        rollbacks = 0
        fail = False

        def ping(self, **kwargs):
            pass

        def begin(self):
            self.active += 1
            self.peak = max(self.peak, self.active)

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def executemany(self, *args):
            time.sleep(.01)
            if self.fail:
                raise OSError('fake write failure')
            return 1

        def commit(self):
            self.commits += 1
            self.active -= 1

        def rollback(self):
            self.rollbacks += 1
            self.active -= 1

    source.conn = connection = Connection()
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: source.save(store.BEST_QUOTES, [{}]), range(5)))
    assert connection.peak == 1
    assert connection.commits == 5
    connection.fail = True
    with pytest.raises(OSError):
        source.save(store.BEST_QUOTES, [{}])
    assert connection.rollbacks == 1


def test_poll_atomic_state_and_restart_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_conn', None)
    monkeypatch.setattr(qb_source, 'DIRTY', set())
    connection = db.init_db(path=str(tmp_path / 'checkpoint.db'))
    when = dt.datetime(2026, 10, 13, 10)
    rows = [dict(id=1, seq_ns=1, bond_uni_code=100, broker_id=2, bid_yield=2.1,
                 bid_vol=100, bid_quote_type=3, ofr_yield=None, ofr_quote_type=0,
                 bid_pending=0, ofr_pending=0, update_time=when, capture_time=when)]

    def source():
        src = QBSource()
        src.dict = {100: ('100.IB', 'synthetic')}
        monkeypatch.setattr(src, '_refresh_dict', lambda: None)
        monkeypatch.setattr(src, '_fetch', lambda: [r for r in rows if r['id'] > src.last_id])
        return src

    src = source()
    write = db.upsert_quotes

    def fail_after_write(quotes):
        write(quotes)
        raise OSError('after flow before outer commit')

    try:
        monkeypatch.setattr(db, 'upsert_quotes', fail_after_write)
        with pytest.raises(OSError):
            src.poll()
        assert src.last_id == 0 and not src.book and not src.last_best and not src.static_done
        assert connection.execute('SELECT count(*) FROM bond_static').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM quote_flow').fetchone()[0] == 0
        monkeypatch.setattr(db, 'upsert_quotes', write)
        assert len(src.poll()) == 1
        restarted = source()
        assert restarted.poll() == []
        assert restarted.last_id == 1
        assert restarted.book[100][2]['bid_yield'] == 2.1
        assert connection.execute('SELECT count(*) FROM quote_flow').fetchone()[0] == 1
    finally:
        connection.close()
