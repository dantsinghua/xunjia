"""QT V1.0.1 business fields, owner isolation, detail identity and best quote provenance.
Only synthetic records and isolated SQLite; no live FIX, IM or market calls.
"""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
import asyncio
import json
import sqlite3
import pytest
from server.qt_quotes import QtQuoteBook, FIELDS, COLUMNS, normal_fields
from server.market import MarketService,parse_filter

NOW=datetime(2026,10,28,10,0,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def record(negotiation='n1', **changes):
    value={'MsgType':'S','MarketIndicator':'4','QuoteType':'3','QuoteID':'-10000',
           'NegotiationID':negotiation,'OwnQtNO':'qt-alice','TraderName':'Synthetic Alice',
           'Operator':'2','Side':'y','SecurityID':'000001','Symbol':'合成债A','ExDestination':'XCFE',
           'BidPxType':'1','BidPx':'2.00','BidSize':'10000000',
           'OfferPxType':'1','OfferPx':'2.20','OfferSize':'20000000',
           'BidTradeDate':'20261028','OfrTradeDate':'20261028','BidSettlType':'2','OfrSettlType':'2',
           'TransactTime':'20261028-10:00:00','TransType':'N','Intermediary':'2'}
    value.update(changes)
    return value

@pytest.fixture
def book(tmp_path):
    return QtQuoteBook(tmp_path/'private.sqlite',source_id='synthetic',
                       account_owners={'qt-alice':'alice','qt-bob':'bob'},clock=lambda:NOW)


def put(book, fields,seq):return book.ingest(fields,event_id='event-'+str(seq),sequence=seq)


def service(book, *, broker=None, floor=None):
    source=broker if broker is not None else [
        {'code':'000001.IB','name':'合成债A','bondUniCode':'1','flowId':100,'issueTime':int(NOW*1000),
         'ob':'2.05','obv':'999','obs':'国际','oo':'2.15','oov':'888','oos':'国利','clearing':'+1','val_yield':'2.1'}]
    repo=SimpleNamespace(market_overlays=lambda owner: ({},floor or {}))
    return MarketService(repo,rows_provider=lambda channel,broker: deepcopy(source),qt_provider=book)


def test_same_bond_multiple_dialogues_and_fixed_minus10000_quoteid_are_not_collapsed(book):
    put(book,record('n1'),1);put(book,record('n2'),2);put(book,record('n1',Operator='1'),3)
    rows=service(book).page('alice',filter_spec={'channel':'qt'})
    assert rows['total']==3 and len({r['rowId'] for r in rows['data']})==3
    assert {r['qt_fields']['NegotiationID'] for r in rows['data']}=={'n1','n2'}
    assert {r['qt_fields']['Operator'] for r in rows['data']}=={'1','2'}
    assert len(COLUMNS)==49 and {r['key'] for r in rows['qtColumns']}=={x[1] for x in FIELDS}


def test_one_best_row_keeps_each_sides_price_volume_and_qt_provenance_together(book):
    put(book,record('bid-winner',BidPx='1.98',BidSize='30000000',OfferPx='2.10'),1)
    put(book,record('ofr-winner',BidPx='2.01',BidSize='99000000',OfferPx='2.22',OfferSize='50000000'),2)
    rows=service(book).page('alice')['data'];assert len(rows)==1
    row=rows[0]
    assert (row['obs'],row['ob'],row['obv'])==('QT','1.98','3000')
    assert (row['oos'],row['oo'],row['oov'])==('QT','2.22','5000')
    details={r['qt_fields']['NegotiationID']:r['rowId'] for r in book.rows('alice')}
    assert row['winning_bid']['row_id']==details['bid-winner']
    assert row['winning_ofr']['row_id']==details['ofr-winner']
    assert 'qt_fields' not in row


def test_bid_and_ofr_can_choose_different_sources_without_quantity_crossing(book):
    put(book,record(BidPx='2.10',BidSize='90000000',OfferPx='2.21',OfferSize='7000000'),1)
    row=service(book).page('alice')['data'][0]
    assert (row['obs'],row['ob'],row['obv'])==('国际','2.05','999')
    assert (row['oos'],row['oo'],row['oov'])==('QT','2.21','700')


def test_equal_qt_prices_keep_one_quote_not_sum_or_highest_quantity(book):
    put(book,record('old',BidSize='30000000'),1)
    put(book,record('new',BidSize='10000000'),2)
    row=service(book,broker=[]).page('alice')['data'][0]
    assert row['obv']=='1000' and row['obs']=='QT'


def test_broker_price_tie_is_stable_and_does_not_add_private_lots(book):
    put(book,record(BidPx='2.05',OfferPx='2.15'),1)
    row=service(book).page('alice')['data'][0]
    assert (row['obs'],row['obv'],row['oos'],row['oov'])==('国际','999','国利','888')


def test_owner_rows_and_merged_best_cannot_cross_users_or_mutate_public_rows(book):
    put(book,record('a',BidPx='1.90'),1)
    put(book,record('b',OwnQtNO='qt-bob',BidPx='1.80'),2)
    s=service(book)
    assert s.page('alice')['data'][0]['ob']=='1.90'
    assert s.page('bob')['data'][0]['ob']=='1.80'
    assert s.page('other')['data'][0]['ob']=='2.05'
    assert len(s.page('alice',filter_spec={'channel':'qt'})['data'])==1
    assert s.page('other',filter_spec={'channel':'qt'})['data']==[]
    assert s.page('alice',filter_spec={'channel':'broker'})['data'][0]['ob']=='2.05'
    assert s.rows_provider('best','')[0]['ob']=='2.05'


@pytest.mark.parametrize('price_type',['2','3','4'])
def test_exercise_yield_and_cash_prices_stay_in_detail_not_maturity_best(book,price_type):
    put(book,record(BidPxType=price_type,BidPx='0.01',OfferPxType=price_type,OfferPx='99.99'),1)
    s=service(book);detail=s.page('alice',filter_spec={'channel':'qt'})['data'][0]
    assert detail['qt_fields']['BidPxType']==price_type and detail['qt_fields']['OfferPx']=='99.99'
    best=s.page('alice')['data'][0]
    assert (best['ob'],best['oo'])==('2.05','2.15')


@pytest.mark.parametrize('changes',[
    {'Operator':'1'}, {'ValidUntilTime':'20261028-09:59:59'},
    {'TransactTime':'20261027-10:00:00'}, {'BidTradeDate':'20261029','OfrTradeDate':'20261029'},
    {'BidSettlType':'1','OfrSettlType':'1'}, {'BidPriceOri':'1.90*','OfferPriceOri':'2.30*'}])
def test_ineligible_quotes_are_visible_but_do_not_replace_comparable_broker(book,changes):
    put(book,record(BidPx='1.90',OfferPx='2.30',**changes),1)
    s=service(book);assert len(s.page('alice',filter_spec={'channel':'qt'})['data'])==1
    row=s.page('alice')['data'][0]
    assert (row['obs'],row['ob'],row['oos'],row['oo'])==('国际','2.05','国利','2.15')


def test_full_h_update_clears_missing_economics_but_keeps_static_identity(book):
    put(book,record(),1)
    update=record(TransType='H',BidPx='1.99',TransactTime='20261028-10:00:01')
    del update['BidSize'];del update['OfferSize'];del update['OfferPx']
    put(book,update,2);row=book.rows('alice')[0]
    assert row['ob']=='1.99' and row['obv'] is None and row['oo'] is None
    assert row['qt_fields']['Intermediary']=='2' and row['qt_broker_name']=='信唐'


def test_late_replay_does_not_revive_cancelled_dialogue(book):
    put(book,record(),1)
    cancel={k:v for k,v in record(TransType='Z',TransTypeDtl='8',TransactTime='20261028-10:00:02').items()
            if k in ('MsgType','MarketIndicator','QuoteType','OwnQtNO','NegotiationID','Operator','TransType','TransTypeDtl','TransactTime')}
    put(book,cancel,3);put(book,record(TransactTime='20261028-10:00:01'),2)
    assert book.rows('alice')[0]['qt_status']=='ENDED'
    assert service(book).page('alice')['data'][0]['obs']=='国际'
    put(book,record(TransType='H',TransactTime='20261028-10:00:03'),4)
    assert book.rows('alice')[0]['qt_status']=='ENDED'


def test_own_side_recall_does_not_cancel_counterparty(book):
    put(book,record(),1);put(book,record(Operator='1'),2)
    put(book,record(Operator='1',TransType='Z',TransTypeDtl='3',TransactTime='20261028-10:00:01'),3)
    rows={row['qt_fields']['Operator']:row for row in book.rows('alice')}
    assert rows['1']['qt_status']=='WITHDRAWN' and rows['2']['qt_status']=='ACTIVE'


def test_price_floor_is_private_and_is_not_the_qt_bid_price(book):
    put(book,record(BidPx='1.90'),1)
    row=service(book,floor={'000001.IB':{'kind':'yield','value':'2.00'}}).page('alice',filter_spec={'channel':'qt'})['data'][0]
    assert row['ib']==row['io']=='2.00'
    assert row['qt_fields']['BidPx']=='1.90' and row['ibv']=='1000'
    empty=service(book).page('alice',filter_spec={'channel':'qt'})['data'][0]
    assert empty['ib'] is empty['io'] is None


def test_raw_quantity_with_qualifiers_is_not_fabricated_into_a_number(book):
    value=record(BidSizeOri='2000+5000*');del value['BidSize'];put(book,value,1)
    row=service(book).page('alice',filter_spec={'channel':'qt'})['data'][0]
    assert row['ibv'] is None and row['qt_fields']['BidSizeOri']=='2000+5000*'


def test_same_timestamp_pagination_and_row_selection_keep_all_details(book):
    for i in range(1,8):put(book,record('n'+str(i)),i)
    s=service(book);first=s.page('alice',filter_spec={'channel':'qt'},limit=3)
    second=s.page('alice',filter_spec={'channel':'qt'},before=first['nextBefore'],before_uni=first['nextUni'],limit=3)
    assert len({r['rowId'] for r in first['data']+second['data']})==6
    key=first['data'][1]['rowId']
    chosen=s.resolve('alice',{'rowIds':[key],'filter':{'channel':'qt'}})
    assert len(chosen)==1 and chosen[0]['rowId']==key
    assert s.resolve('bob',{'rowIds':[key],'filter':{'channel':'qt'}})==[]
    rest=s.resolve('alice',{'selectAll':True,'except':[key],'filter':{'channel':'qt'}})
    assert len(rest)==6 and key not in {r['rowId'] for r in rest}


def test_filters_are_applied_after_merge_not_before_and_export_matches_page(book):
    put(book,record(BidPx='1.90'),1);s=service(book)
    f={'channel':'best','prices':{'Bid':{'hi':'1.95'}}}
    assert s.page('alice',filter_spec=f)['total']==1
    assert s.rows('alice',parse_filter(f))[0]['ob']=='1.90'


def test_qt_only_bond_present_without_broker_and_other_broker_bonds_preserved(book):
    put(book,record(SecurityID='000002',Symbol='合成债B'),1)
    rows=service(book).page('alice')['data']
    assert {r['code'] for r in rows}=={'000001.IB','000002.IB'}


@pytest.mark.parametrize('changes',[
 {'OwnQtNO':'unknown'}, {'QuoteType':'0'}, {'BidSize':'-1'}, {'BidPx':'NaN'},
 {'BidPx':'Infinity'}, {'BidPx':True}, {'password':'secret'}, {'owner':'bob'},
 {'ExDestination':'UNKNOWN'}, {'Side':'unknown'}, {'Operator':'9'}])
def test_bad_identity_economics_or_secret_fields_fail_closed(book,changes):
    with pytest.raises(ValueError):put(book,record(**changes),1)
    assert book.rows('alice')==[]


def test_tag_mapping_retains_all_fields_and_rejects_conflicting_aliases(book):
    raw=record();tags={tag:raw[name] for tag,name,_ in FIELDS if name in raw}
    assert normal_fields(tags)==raw
    with pytest.raises(ValueError):normal_fields({**tags,'BidPx':'9.99'})


def test_restart_is_durable_and_duplicate_event_or_sequence_cannot_overwrite(book):
    assert put(book,record(),1) is True
    assert put(book,record(),1) is False
    with pytest.raises(ValueError):put(book,record(BidPx='1.80'),1)
    restored=QtQuoteBook(book.path,source_id='synthetic',account_owners=book.owners,clock=book.clock)
    assert restored.rows('alice')==book.rows('alice')


def test_journal_partial_line_retry_and_owner_mapping_do_not_need_a_page(book,tmp_path):
    path=tmp_path/'input.jsonl';book.journal=path
    item={'event_id':'e1','sequence':1,'fields':record()};raw=json.dumps(item).encode()
    path.write_bytes(raw[:-3]);path.chmod(0o600)
    assert book.poll_once()==0 and book.rows('alice')==[]
    with path.open('ab') as f:f.write(raw[-3:]+b'\n')
    assert book.poll_once()==1 and len(book.rows('alice'))==1
    assert book.poll_once()==0
    with path.open('ab') as f:f.write(json.dumps({**item,'event_id':'e2','sequence':2,'fields':record('n2')}).encode()+b'\n')
    assert book.poll_once()==1 and len(book.rows('alice'))==2


def test_actual_authenticated_api_and_export_keep_private_qt_scope(book,tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig,AuthUser
    from openpyxl import load_workbook
    from io import BytesIO
    put(book,record('alice-unique',Content='=SYNTHETIC_FORMULA_MUST_STAY_TEXT'),1)
    put(book,record('bob-secret',OwnQtNO='qt-bob'),2)
    market=service(book)
    app=create_app(database_url='sqlite:///'+str(tmp_path/'business.db'),enable_background=False,
                   market=market,auth_config=AuthConfig(sso_secret='synthetic',session_secret='a'*40))
    with TestClient(app) as client:
        assert client.get('/api/bonds?channel=qt').status_code==401
        headers={'Authorization':'Bearer '+app.state.auth.issue_token(AuthUser('alice','Alice'))}
        response=client.get('/api/bonds?channel=qt',headers=headers)
        assert response.status_code==200,response.text
        data=response.json();assert data['total']==1 and 'bob-secret' not in response.text
        key=data['data'][0]['rowId']
        bheaders={'Authorization':'Bearer '+app.state.auth.issue_token(AuthUser('bob','Bob'))}
        rejected=client.post('/api/bonds/resolve',json={'rowIds':[key],'filter':{'channel':'qt'}},headers=bheaders)
        assert rejected.status_code==200 and rejected.json()['data']==[]
        exported=client.get('/api/bonds/export',params={'filter':json.dumps({'channel':'qt'})},headers=headers)
        assert exported.status_code==200,exported.text[:100]
        workbook=load_workbook(BytesIO(exported.content),read_only=True)
        rows=list(workbook.active.rows);assert len(rows)==2
        names=[cell.value for cell in rows[0]]
        assert len(names)==56 and 'QT对话报价ID [18033]' in names
        cell=rows[1][names.index('原始语料 [18026]')]
        assert cell.value.startswith('=SYNTHETIC') and cell.data_type=='s'
        assert not app.state.quote_hub.clients


def test_all_price_basis_and_h_update_fields_are_retained_without_legacy_values(book):
    put(book,record(CreditRating='AAA',BidPriceOri='2.0*'),1)
    update=record(TransType='H',TransactTime='20261028-10:00:01')
    put(book,update,2)
    f=book.rows('alice')[0]['qt_fields']
    assert 'CreditRating' not in f and 'BidPriceOri' not in f


def test_broken_optional_qt_config_does_not_break_public_market(tmp_path,monkeypatch):
    from server.app import create_app
    from server.auth import AuthConfig
    monkeypatch.setenv('XUNJIA_QT_CONFIG',str(tmp_path/'missing-private-config.json'))
    # No state path is needed to prove the optional feed is isolated.
    market=service(None)
    app=create_app(database_url='sqlite:///'+str(tmp_path/'business.sqlite'),enable_background=False,
                   market=market,auth_config=AuthConfig(sso_secret='synthetic',session_secret='x'*40))
    assert app.state.market.page('alice')['data'][0]['obs']=='国际'
    data=app.state.market.page('alice',filter_spec={'channel':'qt'})
    assert data['data']==[] and data['qtStatus']['code']=='QT_CONFIGURATION_INVALID'
