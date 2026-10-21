"""Independent synthetic quote attribution and legacy-checkpoint recovery gates."""
from datetime import datetime, timedelta
import json
import struct

import pytest

from collector import qb_apollo, qb_ws
from server import market_cache as cache, qb_source
from server.quote_runtime import QBMarket, QuoteHub


def row(broker=2, *, bid=2.10, ofr=2.20, bid_vol=None, ofr_vol=None,
        offset=0, pending=0, clearing='+1'):
    when = datetime.now().replace(microsecond=0) + timedelta(seconds=offset)
    return {'id': broker, 'seq_ns': broker, 'bond_uni_code': 100, 'broker_id': broker,
            'bid_quote_type': 3 if bid is not None else None,
            'ofr_quote_type': 3 if ofr is not None else None,
            'bid_yield': bid, 'ofr_yield': ofr, 'bid_vol': bid_vol, 'ofr_vol': ofr_vol,
            'bid_net_price': None, 'ofr_net_price': None,
            'bid_pending': pending, 'ofr_pending': pending,
            'bid_vol_str': clearing, 'ofr_vol_str': clearing,
            'update_time': when, 'capture_time': when, 'cnbd_date': None}


def source():
    value = qb_source.QBSource({'host': 'synthetic-unreachable.invalid'})
    value.brokers = {2: '合成经纪甲', 9: '合成经纪乙', 6: '合成经纪丙'}
    return value


@pytest.mark.parametrize('side', ['bid', 'ofr'])
@pytest.mark.parametrize('volume', [None, 0])
def test_valid_price_without_size_retains_its_actual_broker(side, volume):
    r = row(**{side + '_vol': volume})
    answer = source()._best_side([r], side + '_yield', side + '_vol', side == 'ofr')
    assert answer['side'] == '合成经纪甲'
    assert answer['vol'] is None
    assert answer['yield'] == r[side + '_yield']
    assert answer['row'] is r


@pytest.mark.parametrize('side', ['bid', 'ofr'])
def test_all_unsized_ties_name_every_actual_broker_but_keep_original_metadata_row(side):
    first = row(2, offset=-2, pending=0, clearing='--(+1)')
    later = row(9, offset=-1, pending=1, clearing='--(+0)')
    rows = [first, later]
    result = source()._best_side(rows, side + '_yield', side + '_vol', side == 'ofr')
    assert set(result['side'].split('/')) == {'合成经纪甲', '合成经纪乙'}
    assert result['vol'] is None
    assert result['row'] is first, 'Attribution sorting must not change the legacy metadata representative'
    # Check externally visible firmness/clearing, not only the helper's identity.
    best = source()._best_of(rows)
    assert best['firmness'] == '实盘' and best['clearing'] == '+1'


@pytest.mark.parametrize('side', ['bid', 'ofr'])
def test_positive_size_ties_still_exclude_unsized_names_and_sum_only_positive_size(side):
    rows = [row(2, bid_vol=1000, ofr_vol=1000, pending=0, clearing='+1'),
            row(9, bid_vol=3000, ofr_vol=3000, pending=1, clearing='+0'),
            row(6, bid_vol=None, ofr_vol=0)]
    result = source()._best_side(rows, side + '_yield', side + '_vol', side == 'ofr')
    assert result['side'] == '合成经纪乙/合成经纪甲'
    assert result['vol'] == 4000
    assert result['row'] is rows[1], 'Sized representative must keep the existing size/time/id sort'
    best = source()._best_of(rows)
    assert best['firmness'] == '请示盘' and best['clearing'] == '+0'


@pytest.mark.parametrize('side', ['bid', 'ofr'])
def test_best_unsized_price_is_not_replaced_by_inferior_sized_price(side):
    best = row(2)
    # The lower bid yield / higher offer yield remain the existing best-price rule.
    best[side + '_yield'] = 2.0
    other = row(9, bid_vol=9000, ofr_vol=9000)
    other[side + '_yield'] = 2.1 if side == 'bid' else 1.9
    result = source()._best_side([best, other], side + '_yield', side + '_vol', side == 'ofr')
    assert result['yield'] == 2.0 and result['vol'] is None
    assert result['side'] == '合成经纪甲'


def test_bid_and_offer_attribution_never_borrow_the_opposite_side_volume():
    result = source()._best_of([row(2, ofr=None, bid_vol=2000),
                                row(9, bid=None, ofr_vol=0)])
    assert result['best_bid_side'] == '合成经纪甲' and result['best_bid_vol'] == 2000
    assert result['best_ofr_side'] == '合成经纪乙' and result['best_ofr_vol'] is None


def test_net_price_without_yield_does_not_create_a_public_yield_or_broker_side():
    integer = lambda n, value: qb_ws._varint(n << 3) + qb_ws._varint(value)
    floating = lambda n, value: qb_ws._varint((n << 3) | 1) + struct.pack('<d', value)
    parsed = qb_apollo.parse_push_quote(integer(16, 1) + floating(14, 99.85))
    assert parsed['bid_net_price'] == 99.85 and parsed['bid_yield'] is None
    result = source()._best_of([{**row(ofr=None), **parsed, 'broker_id': 2}])
    assert result is None


@pytest.fixture
def old_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, '_conn', None)
    monkeypatch.setattr(qb_source, 'DIRTY', set())
    monkeypatch.setattr(qb_source, 'INTENT', {})
    monkeypatch.setattr(qb_source, 'REPLAY', 0)
    path = str(tmp_path / 'existing-market.db')
    db = cache.init_db(path=path)
    seed = source()
    original = row(2, bid_vol=None, ofr_vol=0)
    seed.book = {100: {2: original}}
    seed.dict = {100: ('000100.IB', '合成旧缓存券')}
    seed._upsert_static([seed.static_row(100)])
    old_best = {'best_bid': 2.10, 'best_bid_vol': None, 'best_bid_side': '',
                'best_ofr': 2.20, 'best_ofr_vol': None, 'best_ofr_side': '',
                'firmness': '实盘', 'clearing': '+1',
                'issue_time': int(original['update_time'].timestamp() * 1000)}
    old_flow = {'bond_uni_code': '100', 'group_name': 'bond-all', 'status': '待询价', **old_best}
    cache.upsert_quotes([old_flow])
    key = tuple(old_best.get(k) for k in cache.DYN_COLS) + (old_best['issue_time'], None, None, None, None)
    payload = {'last_id': original['id'], 'book_day': datetime.now().date().isoformat(),
               'wall_day': datetime.now().date().isoformat(), 'book': seed.book,
               'static_done': {100: True}, 'last_best': {100: key}}
    encoded = json.dumps(payload, default=lambda value: value.isoformat())
    with cache.transaction() as tx: tx.execute('INSERT INTO qb_source_checkpoint VALUES(1,?)', (encoded,))
    original_flow = dict(db.execute('SELECT * FROM quote_flow').fetchone())

    def restart():
        market = QBMarket(path, connection_options={'host': 'synthetic-unreachable.invalid'})
        def dictionary(*_, **__):
            market.source.dict = dict(seed.dict)
            market.source.code2uni = {'000100.IB': 100}
            market.source.brokers = dict(seed.brokers)
        monkeypatch.setattr(market.source, '_refresh_dict', dictionary)
        monkeypatch.setattr(market.source, '_fetch', lambda *_, **__: [])
        return market

    try: yield db, restart, original_flow
    finally: db.close()


def test_old_checkpoint_repairs_attribution_on_first_poll_without_a_new_quote(old_checkpoint):
    db, restart, original_flow = old_checkpoint
    market = restart()
    changes = market.poll()
    assert len(changes) == 1, 'Persisted old attribution must repair before a new vendor push'
    delta, groups = changes[0]
    assert delta['bbs'] == delta['bos'] == '合成经纪甲'
    assert delta['bb'] == 2.10 and delta['bo'] == 2.20
    assert delta['bbv'] is None and delta['bov'] is None
    assert delta['t'] == original_flow['issue_time']
    assert delta['f'] > original_flow['flow_id'] and 'bond-all' in groups
    # Original history is immutable; correction goes through the original new-flow chain.
    assert dict(db.execute('SELECT * FROM quote_flow WHERE flow_id=?', (original_flow['flow_id'],)).fetchone()) == original_flow
    assert db.execute('SELECT count(*) FROM quote_flow').fetchone()[0] == 2
    latest = dict(db.execute('SELECT * FROM quote_latest').fetchone())
    assert latest['best_bid_side'] == latest['best_ofr_side'] == '合成经纪甲'
    public = market.rows('best', '')[0]
    assert public['obs'] == public['oos'] == '合成经纪甲'
    assert public['obv'] is None and public['oov'] is None
    hub = QuoteHub()
    peer = {'groups': {'bond-all'}, 'pending': {}, 'overflow': False}
    hub.clients[object()] = peer
    hub.publish(delta, groups)
    assert peer['pending']['bond-all']['100']['bbs'] == '合成经纪甲'
    assert market.poll() == []
    assert restart().poll() == [], 'A second restart must not fabricate another correction'
    assert db.execute('SELECT count(*) FROM quote_flow').fetchone()[0] == 2
    state = json.loads(db.execute('SELECT payload FROM qb_source_checkpoint').fetchone()[0])
    assert state['last_id'] == 2
    assert state['book']['100']['2']['bid_vol'] is None
    assert state['book']['100']['2']['ofr_vol'] == 0
