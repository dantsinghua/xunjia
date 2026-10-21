"""Date/calendar are advisory; numeric authority and trade/send fences remain.

Actual create_app environment -> QBMarket -> QBSource -> cache. Only MySQL I/O
is substituted. All authorization is isolated SQLite with no transport/worker.
"""
from datetime import datetime,date,timedelta
from pathlib import Path
from decimal import Decimal,ROUND_HALF_UP
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pymysql
import pytest

from server import qb_source,market_cache
from server.app import create_app
from server.auth import AuthConfig,AuthUser
from server.quote_runtime import QBMarket

OWNER='bp-advisory-isolated-owner'


class QuoteRows:
    def __init__(self):
        now=datetime.now()
        self.rows=[dict(id=i,bond_uni_code=i,broker_id=2,seq_ns=100+i,
            update_time=now,capture_time=now,cnbd_date=None,cnbd_yield_mat=Decimal(value),
            bid_quote_type=1,ofr_quote_type=1,bid_yield=Decimal('9.99'),ofr_yield=Decimal('8.88'),
            cnbd_net_price=Decimal('199.99'),bid_vol=1000,ofr_vol=1000)
            for i,value in enumerate(['2.9872','1.6508','3.1234'],1)]
        self.meta=[(i,'QA-BP-'+str(i),'隔离中债估值券'+str(i)) for i in range(1,4)]
        self.connections=[]
    def connect(self,**options):
        self.connections.append(options);owner=self
        class Cursor:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def execute(self,sql,params=None):
                if sql.startswith('SELECT bond_uni_code, bond_code, bond_short_name'):
                    self.rows=owner.meta
                elif sql.startswith('SELECT broker_id, broker_name'):self.rows=[(bid,'样本经纪商'+str(bid)) for bid in {r['broker_id'] for r in owner.rows}]
                elif 'FROM qb_live_best_quote WHERE id >' in sql:
                    self.rows=[tuple(row.get(k) for k in qb_source._COLS) for row in owner.rows if row['id']>params[0]]
                elif sql.startswith('SELECT cnbd_yield_mat,cnbd_date,update_time,seq_ns,id FROM qb_live_best_quote'):
                    rows=[row for row in owner.rows if row['bond_uni_code']==params[0] and row.get('cnbd_yield_mat') is not None]
                    if 'update_time <=>' in sql:rows=[row for row in rows if row['update_time']==params[1] and row['seq_ns']==params[2]]
                    else:rows=sorted(rows,key=lambda row:(row['update_time'],row['seq_ns'],row['id']),reverse=True)[:1]
                    self.rows=[tuple(row.get(k) for k in ['cnbd_yield_mat','cnbd_date','update_time','seq_ns','id']) for row in rows]
                else:raise AssertionError('Unexpected SQL: '+sql)
            def fetchall(self):return self.rows
            def fetchone(self):return self.rows[0] if self.rows else None
        class Connection:
            def cursor(self):return Cursor()
            def ping(self,**kwargs):pass
            def close(self):pass
        return Connection()


@pytest.fixture
def boot(tmp_path,monkeypatch,request):
    for name in ['XUNJIA_REFERENCE_CONFIG','XUNJIA_TRANSPORT_CONFIG','XUNJIA_MQ_CONNECTION_CONFIG','XUNJIA_STATE_DIR']:
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setattr(market_cache,'_conn',None)
    monkeypatch.setattr(qb_source,'REPLAY',0);monkeypatch.setattr(qb_source,'DIRTY',set());monkeypatch.setattr(qb_source,'INTENT',{})
    for k,v in {'XUNJIA_QUOTE_SOURCE':'qb','XUNJIA_QUOTE_CACHE':str(tmp_path/'quote.db'),
        'QB_MYSQL_HOST':'isolated-old-db.invalid','QB_MYSQL_PORT':'3306','QB_MYSQL_USER':'synthetic-reader',
        'QB_MYSQL_PASSWORD':'synthetic-only','QB_MYSQL_DB':'synthetic-bondtrade','XUNJIA_QUOTE_AUTOSTART':'0'}.items():monkeypatch.setenv(k,v)
    now=[datetime.now().timestamp()]
    database=QuoteRows()
    if str(getattr(request,'param','')).startswith('recorded'):
        recorded=json.loads((Path(__file__).parent/'fixtures/bp_three_bonds_recorded.json').read_text())
        database.rows=[];database.meta=[]
        for row in recorded['rows']:
            source=dict(row)
            for field in ['update_time','capture_time']:source[field]=datetime.fromisoformat(source[field])
            for field in ['cnbd_yield_mat','cnbd_net_price','cnbd_duration','coupon_rate','bid_yield','ofr_yield','bid_net_price','ofr_net_price','bid_vol','ofr_vol']:
                if source.get(field) is not None:source[field]=Decimal(source[field])
            database.rows.append(source)
            database.meta.append((source['bond_uni_code'],source['bond_code'],source['bond_short_name']))
    monkeypatch.setattr(pymysql,'connect',database.connect)
    transport=None
    if getattr(request,'param',None)=='recorded-ui':
        from server.transports import TransportRouter
        class ReadOnlyAddressBook:
            def contacts(self):return {'account_uid':'synthetic-account','contacts':[{'uin':'synthetic-peer','name':'隔离预览对手'}],'org':{}}
        transport=TransportRouter({(OWNER,'synthetic-account','oss'):ReadOnlyAddressBook()})
    app=create_app(transport=transport,database_url='sqlite:///'+str(tmp_path/'business.db'),enable_background=False,
        auth_config=AuthConfig(sso_secret='synthetic-advisory',session_secret='s'*40,clock=lambda:now[0]))
    app.state.repository.bind_account(OWNER,'synthetic-account','synthetic-workbench')
    app.state.repository.save_preferences(OWNER,{'floors':{code:{'kind':'bp','value':'5'} for uni,code,name in database.meta}})
    reader=app.state.quote_control.reader;assert isinstance(reader,QBMarket)
    reader.poll()
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+app.state.auth.issue_token(AuthUser(OWNER,OWNER))
        yield {'app':app,'client':client,'reader':reader,'database':database,'tmp':tmp_path,'now':now}
    if reader.source.conn:reader.source.conn.close()
    if market_cache._conn is not None:market_cache._conn.close()


def policies(s):
    result=[]
    for row,(uni,code,name) in zip(s['database'].rows,s['database'].meta):
        value=Decimal(str(row['cnbd_yield_mat'])) if row['cnbd_yield_mat'] is not None else Decimal('2.9872')
        result.append({'code':code,'name':name,'peer_ids':['synthetic-peer'],'our_side':'BUY',
            'volume_wan':'1000','opening_yield_pct':str((value+Decimal('.15')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),
            'limit_yield_pct':str((value+Decimal('.05')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),
            'step_bp':'2','pricing_basis':'bp'})
    return result


def preview(s,key='advisory-preview'):
    r=s['client'].post('/api/im/rfq/preview',json={'request_id':key,'items':policies(s)})
    assert r.status_code==200,r.text
    return r.json()['data']


def confirm(s,value):
    return s['client'].post('/api/im/rfq/send',json={'batch_id':value['batch_id'],'expected_version':value['version'],'request_id':'isolated-confirm'})


def test_old_quote_rows_null_dates_and_no_calendar_environment_reach_three_bond_preview_and_isolated_confirm(boot):
    s=boot;assert s['app'].state.inquiry.calendar is None
    assert all(row['cnbd_date'] is None for row in s['database'].rows)
    value=preview(s)
    assert len(value['items'])==3
    assert [item['limit_yield_pct'] for item in value['items']]==['3.04','1.70','3.17']
    answer=confirm(s,value);assert answer.status_code==200,answer.text
    assert len(answer.json()['data']['group_ids'])==3
    assert len(answer.json()['data']['commands'])==1
    assert answer.json()['data']['commands'][0]['status']=='queued'


@pytest.mark.parametrize('raw_date',[None,'not-a-date','2001-01-01','2099-12-31'])
def test_missing_invalid_old_future_source_date_does_not_block_usable_chinabond_value(boot,raw_date):
    s=boot
    for rows in s['reader'].source.book.values():
        for row in rows.values():row['cnbd_date']=raw_date
    value=preview(s)
    assert len(value['items'])==3
    if raw_date is None:
        assert s['reader'].valuation('QA-BP-1').trading_date is None


def test_calendar_timeout_and_optional_formal_source_outage_do_not_block_inno(boot):
    s=boot
    def offline(*args):raise ConnectionError('synthetic optional source unavailable')
    s['app'].state.inquiry.calendar=SimpleNamespace(latest_valuation_day=offline)
    s['app'].state.inquiry.reference_provider=SimpleNamespace(snapshot=offline)
    assert len(preview(s)['items'])==3


@pytest.mark.parametrize('change',['date-only','snapshot-id-only'])
def test_same_value_new_date_or_revision_does_not_revoke_existing_preview(boot,change,monkeypatch):
    s=boot;value=preview(s);real=s['reader'].valuation
    if change=='date-only':
        for rows in s['reader'].source.book.values():
            for row in rows.values():row['cnbd_date']='2002-02-02'
    else:
        from dataclasses import replace
        s['app'].state.inquiry.valuation_source=lambda code:replace(real(code),snapshot_id='changed-publication-same-economics')
    response=confirm(s,value);assert response.status_code==200,response.text


@pytest.mark.parametrize('change',['valuation-number','valuation-number-same-rounded-boundary','wrong-bond','saved-boundary'])
def test_real_numeric_identity_or_boundary_changes_still_reject_old_preview(boot,change):
    s=boot;value=preview(s)
    if change=='valuation-number':
        s['reader'].source.book[1][2]['cnbd_yield_mat']=Decimal('3.9872')
    elif change=='valuation-number-same-rounded-boundary':
        s['reader'].source.book[1][2]['cnbd_yield_mat']=Decimal('2.9873')
    elif change=='wrong-bond':
        from dataclasses import replace
        real=s['reader'].valuation
        s['app'].state.inquiry.valuation_source=lambda code:replace(real(code),bond_code='WRONG-CODE')
    else:s['app'].state.repository.save_preferences(OWNER,{'floors':{'QA-BP-1':{'kind':'bp','value':'6'}}})
    response=confirm(s,value);assert response.status_code in [400,409],response.text
    assert s['app'].state.repository.group_rows(OWNER)==[]
    assert s['app'].state.inquiry.commands(OWNER)==[]


def test_unlabelled_bid_offer_or_net_price_never_substitute_for_missing_chinabond_value(boot):
    s=boot;intended=policies(s)
    for rows in s['reader'].source.book.values():
        for row in rows.values():row['cnbd_yield_mat']=None
    for row in s['database'].rows:row['cnbd_yield_mat']=None
    response=s['client'].post('/api/im/rfq/preview',json={'request_id':'no-valid-valuation','items':intended})
    assert response.status_code==400
    assert s['app'].state.inquiry.commands(OWNER)==[]


@pytest.mark.parametrize('boot',['recorded'],indirect=True)
def test_recorded_real_three_bond_null_dates_use_actual_environment_entry_without_calendar(boot):
    s=boot
    assert s['app'].state.inquiry.calendar is None and s['app'].state.inquiry.reference_provider is None
    assert all(row['cnbd_date'] is None for row in s['database'].rows)
    data=preview(s,'recorded-real-input-preview')
    bycode={row['code']:row for row in data['items']}
    for source,(uni,code,name) in zip(s['database'].rows,s['database'].meta):
        item=bycode[code];snapshot=item['valuation_snapshot']
        assert snapshot['source']=='innodealing' and snapshot['provider']=='CHINABOND'
        assert snapshot['valuation_date'] is None
        assert Decimal(snapshot['yield_pct'])==source['cnbd_yield_mat']
        assert item['valuation_warnings']
        assert Decimal(item['limit_yield_pct'])==(source['cnbd_yield_mat']+Decimal('.05')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    assert s['app'].state.inquiry.commands(OWNER)==[]


def test_valid_inno_never_queries_optional_calendar_or_formal_source(boot):
    s=boot;calls=[]
    def forbidden_calendar(*args):calls.append('calendar');raise TimeoutError('calendar must not gate estimate')
    def forbidden_reference(*args):calls.append('reference');raise TimeoutError('formal source not needed')
    s['app'].state.inquiry.calendar=SimpleNamespace(latest_valuation_day=forbidden_calendar)
    s['app'].state.inquiry.reference_provider=SimpleNamespace(snapshot=forbidden_reference)
    assert len(preview(s)['items'])==3
    assert calls==[]


@pytest.mark.parametrize('invalid',[None,'invalid','NaN','Infinity',True])
def test_no_valid_inno_number_does_not_use_side_quote_or_net_price(boot,invalid):
    s=boot;intended=policies(s)
    for rows in s['reader'].source.book.values():
        for row in rows.values():row['cnbd_yield_mat']=invalid
    for row in s['database'].rows:row['cnbd_yield_mat']=invalid
    response=s['client'].post('/api/im/rfq/preview',json={'request_id':'invalid-value','items':intended})
    assert response.status_code==400
    assert s['app'].state.inquiry.commands(OWNER)==[]


def test_future_date_with_lower_quote_revision_never_wins_numeric_selection(boot):
    from copy import deepcopy
    s=boot;book=s['reader'].source.book
    old=deepcopy(book[1][2]);old.update(seq_ns=0,cnbd_date='2099-12-31',cnbd_yield_mat=Decimal('0.01'))
    book[1][3]=old
    assert Decimal(s['reader'].valuation('QA-BP-1').yield_pct)==Decimal('2.9872')


def test_date_and_snapshot_only_refresh_are_noop_even_with_existing_authorized_command(boot):
    from dataclasses import replace
    s=boot;answer=confirm(s,preview(s));assert answer.status_code==200
    store=s['app'].state.repository;group_id=answer.json()['data']['group_ids'][0]
    original=store.group(OWNER,group_id);code=original['bond_code']
    val=s['reader'].valuation(code)
    before=store.snapshot(OWNER);commands=s['app'].state.inquiry.commands(OWNER)
    changed=replace(val,trading_date='2099-12-31',snapshot_id='metadata-only-new-publication')
    assert not store.refresh_valuation(OWNER,group_id,valuation=changed,expected_valuation_date='invalid-calendar-date')
    assert store.snapshot(OWNER)==before
    assert s['app'].state.inquiry.commands(OWNER)==commands


def test_cross_day_revaluation_ignores_source_date_but_preserves_pause_and_peer_reconfirmation(boot):
    s=boot;answer=confirm(s,preview(s));assert answer.status_code==200
    service=s['app'].state.inquiry;store=s['app'].state.repository
    store.set_sending(OWNER,False)
    for bybroker in s['reader'].source.book.values():
        for row in bybroker.values():row['cnbd_date']='2099-12-31'
    s['now'][0]+=86400
    result=s['app'].state.rollover_once()
    assert result['rolled']==3 and result['pending']==[]
    assert store.snapshot(OWNER)['send_allowed'] is False
    for group in store.group_rows(OWNER):
        assert group['trade_day']==service.trade_day()
        assert group['boundary'] is not None
        for peer in group['peers']:
            assert peer['reconfirm_day']==service.trade_day()
            assert all(quote['confirmed_day'] is None for quote in peer['quotes'].values())
    assert all(command['status']=='cancelled' for command in service.commands(OWNER))


def test_actual_reader_midnight_book_clear_preserves_side_isolation_and_original_db_valuation(boot,monkeypatch):
    from types import SimpleNamespace
    from datetime import date as RealDate
    import datetime as real_datetime
    s=boot;reader=s['reader'];assert reader.source.book
    class NextWallDay(RealDate):
        @classmethod
        def today(cls):
            value=RealDate.today()+timedelta(days=1)
            return cls(value.year,value.month,value.day)
    class SourceTimeOnly:
        date=NextWallDay
        def __getattr__(self,name):return getattr(real_datetime,name)
    monkeypatch.setattr(qb_source,'dt',SourceTimeOnly())
    reader.poll()
    assert reader.source.book=={}
    cached=reader.db.conn().execute('SELECT best_bid,best_ofr FROM quote_latest').fetchall()
    assert all(row['best_bid'] is None and row['best_ofr'] is None for row in cached)
    value=reader.valuation('QA-BP-1')
    assert Decimal(value.yield_pct)==Decimal('2.9872') and value.trading_date is None and value.source=='innodealing'


def test_original_db_fallback_checks_all_equal_revisions_for_numeric_conflict(boot):
    from copy import deepcopy
    s=boot;reader=s['reader'];reader.source.book={}
    duplicate=deepcopy(s['database'].rows[0]);duplicate.update(id=999999,cnbd_yield_mat=Decimal('0.01'),cnbd_date='2099-12-31')
    s['database'].rows.append(duplicate)
    from server.negotiation.prices import InvalidFact
    with pytest.raises(InvalidFact,match='INNO_CHINABOND_REVISION_CONFLICT'):reader.valuation('QA-BP-1')
