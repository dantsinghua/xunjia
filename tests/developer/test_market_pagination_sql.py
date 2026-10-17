import json

import pytest

from server import market_cache as db
from server.market import MarketService, parse_filter
from server.quote_runtime import QBMarket
from server.repository import Repository


@pytest.fixture
def market_sql(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_conn', None)
    rows = [dict(bond_uni_code=str(i), windcode=f'{i:06}.IB', sec_name='券' + str(i),
                 bond_type='中票', residual_days=730, bond_rating='AAA', prov='广东',
                 tags=['绿色债'], val_yield=2.3, issuer='发行人甲') for i in range(1, 2052)]
    connection = db.init_db(rows, path=str(tmp_path / 'quotes.db'))
    db.upsert_quotes([dict(bond_uni_code=str(i), group_name='bond-clearing_T1', issue_time=i,
                          best_bid=2.4 if i % 2 else None, best_ofr=2.2,
                          best_bid_vol=1000, best_ofr_vol=2000, clearing='+1', firmness='实盘',
                          status='已成交', ib=99) for i in range(1, 2052)])
    source = QBMarket.__new__(QBMarket)
    source.db = db
    repo = Repository('sqlite:///' + str(tmp_path / 'business.db'))
    repo.bind_account('a', 'a1', 'wb')
    service = MarketService(repo, rows_provider=source.rows)
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        yield service, source, connection, statements
    finally:
        connection.close()


def test_sql_filters_count_and_limit_before_hydration(market_sql, monkeypatch):
    service, source, connection, statements = market_sql
    monkeypatch.setattr(source, 'rows', lambda *_: pytest.fail('Production must not materialize all rows'))
    # The service retained the bound provider object; its query capability is used.
    page = service.page('a', filter_spec={'prices': {'Bid': {'lo': '2.35'}}}, limit=50)
    assert page['total'] == 1026
    assert len(page['data']) == 50
    assert all(row['ob'] == 2.4 and row['ib'] is None and row['status'] == '待询价' for row in page['data'])
    selects = [sql for sql in statements if sql.startswith('SELECT') and 'quote_latest' in sql]
    assert any('COUNT(*)' in sql and 'best_bid' in sql for sql in selects)
    hydrated = [sql for sql in selects if 'COUNT(*)' not in sql]
    assert len(hydrated) == 1 and 'LIMIT 51' in hydrated[0]
    assert 'WHERE' in hydrated[0] and 'best_bid' in hydrated[0]


def test_sql_complete_2051_keyset_pages_and_ties(market_sql):
    service, _, connection, _ = market_sql
    # Ties exercise the string bondUniCode ordering used in the existing contract.
    connection.execute('UPDATE quote_latest SET flow_id=42')
    seen = []
    before, uni = 0, 0
    while True:
        page = service.page('a', before=before, before_uni=uni, limit=50)
        assert page['total'] == 2051
        seen.extend(row['code'] for row in page['data'])
        if not page['hasMore']:
            break
        before, uni = page['nextBefore'], page['nextUni']
    assert len(seen) == len(set(seen)) == 2051


def test_resolve_uses_code_in_and_never_full_table_projection(market_sql):
    service, _, _, statements = market_sql
    assert [r['code'] for r in service.resolve('a', {'windcodes': ['000001.IB', '002051.IB']})] == ['002051.IB', '000001.IB']
    queries = [s for s in statements if s.startswith('SELECT') and 'quote_latest' in s]
    assert queries and all('s.windcode IN' in sql for sql in queries)
    statements.clear()
    assert service.resolve('a', {'windcodes': []}) == []
    assert not any('quote_latest' in sql for sql in statements)


@pytest.mark.parametrize('spec', [
    {'groups': ['bond-all', 'unknown_dimension']},
    {'groups': ['bond-tenor_unknown']},
    {'chips': {'当前状态': 9}},
    {'chips': {'方案': 1}},
    {'prices': {'Bid': {'lo': '2.5'}}},
    {'keyword': "' OR 1=1 --"},
])
def test_unknown_missing_and_injection_never_expand_sql_result(market_sql, spec):
    service, *_ = market_sql
    assert service.page('a', filter_spec=spec)['total'] == 0


def test_full_filters_match_injected_reference_including_missing_and_unicode(market_sql):
    service, source, connection, _ = market_sql
    changes = [
        ('SEMÄ一', '政金债', 0, None, '["绿色债"]', '广东', 'AAA', '发行人Ä', 0., -.1, 0., '+0', '实盘'),
        ('SEM二', '中票', 365, None, '["科创债"]', '江苏', 'AA+', '发行人乙', 2.4, 2.2, 2.3, '+1', '可部分执行'),
        ('SEM三', '公司债', None, '3.0Y', '["私募"]', None, None, None, None, None, None, None, None),
        ('SEM四', None, None, None, '{"绿色债":true}', '广东', 'AAA', None, 'bad', 2.1, 2.3, '+N', '意向价'),
        ('SEM五', '中票', None, '10Y', '[1,"绿色债"]', '江苏', 'AAA', '发行人甲', 2.4, 2.2, 2.3, '+1', '实盘'),
        ('SEM六', '中票', -1, '0Y', 'invalid-json', '广东', 'AAA', None, 2.4, 2.2, 2.3, None, '实盘'),
    ]
    for i, row in enumerate(changes, 1):
        name, kind, days, term, tags, prov, rating, issuer, bid, ofr, value, clearing, firmness = row
        connection.execute('UPDATE bond_static SET sec_name=?,bond_type=?,residual_days=?,residual_maturity=?,tags=?,prov=?,bond_rating=?,issuer=?,val_yield=? WHERE bond_uni_code=?',
                           (name, kind, days, term, tags, prov, rating, issuer, value, str(i)))
        connection.execute('UPDATE quote_latest SET best_bid=?,best_ofr=?,clearing=?,firmness=? WHERE bond_uni_code=?',
                           (bid, ofr, clearing, firmness, str(i)))
    from server.market import CHIPS
    from server.quote_runtime import public_row
    raw, _ = db.latest_by_groups(['bond-all'])
    public = [public_row(row['latest'], row['static']) for row in raw]
    reference = MarketService(service.repo, rows_provider=lambda *_: public)
    cases = [{'keyword': 'ä'}, {'keyword': '发行人ä'}, {'keyword': '%'}, {'groups': []},
             {'groups': ['bond-all', 'bond-clearing_bad']},
             {'groups': ['bond-type_中票', 'bond-type_政金债', 'bond-prov_广东']},
             {'groups': ['bond-rating_AAA', 'bond-tenor_lt1']},
             {'groups': ['bond-tenor_3to5']}, {'groups': ['bond-tenor_gt10']}]
    cases += [{'chips': {name: index}} for name, values in CHIPS.items() for index in range(len(values))]
    cases += [{'prices': {name: bound}} for name in ('Bid','Ofr','Bid-中债','中债-Ofr','买量','卖量')
              for bound in ({'lo':'0','hi':'10'}, {'hi':'0'}, {'lo':'2.4'}, {'lo':'','hi':''})]
    for spec in cases:
        spec = {'keyword': 'SEM', **spec}
        actual = service.page('a', filter_spec=spec)
        expected = reference.page('a', filter_spec=spec)
        assert actual == expected, spec


def test_sql_status_only_comes_from_current_owner_and_floor_is_not_cached(market_sql, monkeypatch):
    service, _, _, statements = market_sql
    service.repo.bind_account('b', 'b1', 'wb')
    from server.business import InquiryService
    session = dict(session_id='s1', batch_id='b1', bond_code='000001.IB', pending_command_id='cmd',
                   state='NEGOTIATING', conversation_id='c1', revision=1)
    monkeypatch.setattr(InquiryService, 'sessions', lambda self, who: [session] if who == 'a' else [])
    monkeypatch.setattr(InquiryService, 'status_commands', lambda self, who: [dict(command_id='cmd', status='unknown',
                            conversation_id='c1', session_ids=['s1'])] if who == 'a' else [])
    monkeypatch.setattr(service.repo, 'preferences', lambda who: {'floors': {'000001.IB': {'kind': 'yield', 'value': 2.5}}} if who == 'a' else {})
    private = service.page('a', filter_spec={'chips': {'当前状态': 14}})
    assert private['total'] == 1 and private['data'][0]['status'] == '待核对'
    assert private['data'][0]['floor']['value'] == 2.5
    assert service.page('a', filter_spec={'chips': {'当前状态': 1}})['total'] == 2050
    assert service.page('b', filter_spec={'chips': {'当前状态': 14}})['total'] == 0
    assert service.page('b', filter_spec={'chips': {'当前状态': 1}})['total'] == 2051
    assert 'floor' not in service.resolve('b', {'windcodes': ['000001.IB']})[0]
    projected = [sql.split(' FROM ')[0] for sql in statements if sql.startswith('SELECT') and 'quote_latest' in sql]
    assert all('l.ib' not in sql and 'l.io' not in sql and 'l.status' not in sql for sql in projected)


def test_select_all_limit_is_explicit_and_exclusions_precede_limit(market_sql, monkeypatch):
    service, *_ = market_sql
    monkeypatch.setattr(service, 'MAX_SELECTION', 2050)
    with pytest.raises(ValueError, match='缩小范围'):
        service.resolve('a', {'selectAll': True})
    with pytest.raises(ValueError, match='缩小范围'):
        service.rows('a', parse_filter({}))
    assert len(service.resolve('a', {'selectAll': True, 'except': ['000001.IB']})) == 2050


def test_sql_plan_uses_code_index_and_keyset_order_index(market_sql):
    service, _, connection, statements = market_sql
    service.page('a', before=2000, before_uni='2000')
    page_sql = next(sql for sql in reversed(statements) if sql.startswith('SELECT l.'))
    plan = [str(row['detail']) for row in connection.execute('EXPLAIN QUERY PLAN ' + page_sql)]
    assert any('SEARCH l USING INDEX idx_latest_page' in detail for detail in plan)
    assert not any('TEMP B-TREE FOR ORDER BY' in detail for detail in plan)
    service.resolve('a', {'windcodes': ['000001.IB', '002051.IB']})
    resolve_sql = next(sql for sql in reversed(statements) if sql.startswith('SELECT l.'))
    plan = [str(row['detail']) for row in connection.execute('EXPLAIN QUERY PLAN ' + resolve_sql)]
    assert any('idx_static_windcode' in detail for detail in plan)


def test_broker_snapshot_filters_before_page_and_fails_explicitly_at_capacity(market_sql):
    import datetime as dt
    from server.qb_source import QBSource
    service, source, *_ = market_sql
    source.source = QBSource()
    now = dt.datetime(2026, 10, 13)
    for i in range(1, 104):
        source.source.dict[i] = (f'{i:06}.IB', '经纪券' + str(i))
        source.source.book[i] = {2: dict(broker_id=2, update_time=now, bid_quote_type=3, bid_vol=100,
                                        ofr_quote_type=3, ofr_vol=200, bid_yield=2.1 if i % 2 else 2.3,
                                        ofr_yield=2.0, bid_pending=0, ofr_pending=0)}
    spec = {'channel':'broker', 'broker':'2', 'prices': {'Bid': {'lo':'2.2'}}}
    first = service.page('a', filter_spec=spec, limit=25)
    assert first['total'] == 51 and len(first['data']) == 25 and first['hasMore']
    second = service.page('a', filter_spec=spec, before=first['nextBefore'], before_uni=first['nextUni'], limit=50)
    assert second['total'] == 51 and len(second['data']) == 26 and not second['hasMore']
    seen = [r['code'] for r in first['data'] + second['data']]
    assert len(seen) == len(set(seen)) == 51
    assert all(r['ob'] == 2.3 for r in first['data'] + second['data'])
    assert service.page('a', filter_spec={**spec, 'groups':['bond-unknown_x']})['total'] == 0
    source.MAX_BROKER_BOOK = 102
    with pytest.raises(ValueError, match='容量'):
        service.page('a', filter_spec=spec)
