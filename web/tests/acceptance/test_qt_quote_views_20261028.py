"""Real Vue page with owner-scoped QT/public API fixtures; no native/model calls."""
from copy import deepcopy
import json
from urllib.parse import parse_qs,urlsplit
from playwright.sync_api import expect
from server.qt_quotes import COLUMNS


def install_views(page):
    captured=[]
    common={'code':'000001.IB','name':'合成债A','bondUniCode':'1','flowId':100,
            'obs':'QT','ob':'1.98','obv':'3000','oo':'2.15','oov':'800','oos':'国际',
            'status':'待询价','tags':[]}
    rows=[]
    for i in range(2):
        fields={col['key']:'' for col in COLUMNS}
        fields.update(NegotiationID='dialogue-'+str(i),OwnQtNO='qt-alice',Operator='2',BidPx='1.98',
                      OfferPx='2.20',BidPxType='1',OfferPxType='1',BidSize='10000000',OfferSize='20000000',
                      Content='原始语料-'+str(i),TransType='N',ExDestination='XCFE',QuoteType='3')
        rows.append({**common,'rowId':'qt-'+str(i+1)*64,'flowId':10-i,'qt_fields':fields,
                     'ibv':'1000','ib':None,'io':None,'iov':'2000','qt_status':'ACTIVE'})
    def route(r):
        channel=json.loads(parse_qs(urlsplit(r.request.url).query).get('filter',['{}'])[0]).get('channel','best')
        qt=channel=='qt'
        body={'ok':True,'data':rows if qt else [common],'total':2 if qt else 1,'hasMore':False,'nextBefore':0,'nextUni':0,
              'source':'qt_private' if qt else 'public_with_private_qt','privateCombined':not qt,
              'qtColumns':COLUMNS if qt else [],'qtStatus':{'configured':True,'mapped':True},'sourceNotice':'合成QT联验'}
        r.fulfill(status=200,content_type='application/json',body=json.dumps(body,ensure_ascii=False))
    def resolve(r):
        data=r.request.post_data_json;captured.append(data['selection'])
        selected=[row for row in rows if row['rowId'] in data['selection'].get('rowIds',[])]
        r.fulfill(status=200,content_type='application/json',body=json.dumps({'ok':True,'data':selected}))
    page.route('**/api/bonds?*',route)
    page.route('**/api/bonds/resolve',resolve)
    return rows,captured


def test_all_quotes_only_common_six_quote_fields_and_bid_ofr_sources(ui):
    page,url,_,requests,_=ui;install_views(page)
    page.goto(url+'/#/quote')
    table=page.get_by_test_id('quote-common-table')
    expect(table.locator('.mkt-row')).to_have_count(1)
    expect(table.locator('th',has_text='bid 底价')).to_have_count(0)
    expect(table.locator('th',has_text='ofr 底价')).to_have_count(0)
    row=table.locator('.mkt-row')
    expect(row).to_contain_text('QT');expect(row).to_contain_text('1.98')
    expect(row).to_contain_text('3000');expect(row).to_contain_text('国际')
    assert len(table.locator('thead tr').nth(1).locator('th').all())==8
    page.goto(url+'/#/monitor')
    assert not any('/api/im/quote/stop' in request.url for request in requests)


def test_qt_tab_preserves_same_bond_rows_and_all_manual_fields(ui):
    page,url,_,_,_=ui;install_views(page)
    page.goto(url+'/#/quote')
    page.get_by_text('QT意向',exact=True).click()
    table=page.get_by_test_id('qt-detail-table')
    expect(table).to_be_visible();expect(table.get_by_test_id('qt-detail-row')).to_have_count(2)
    expect(page.get_by_test_id('quote-common-table')).to_have_count(0)
    assert table.locator('thead th[data-qt-field]').count()==49
    expect(table.locator('tbody [data-qt-field="NegotiationID"]')).to_have_text(['dialogue-0','dialogue-1'])
    expect(table.locator('tbody [data-qt-field="Content"]')).to_have_text(['原始语料-0','原始语料-1'])
    expect(table.get_by_test_id('qt-bid-volume')).to_have_text(['1000','1000'])
    expect(table.get_by_test_id('qt-bid-floor')).to_have_text(['—','—'])


def test_qt_selection_uses_dialogue_row_identity_not_bond_code(ui):
    page,url,state,_,_=ui;rows,captured=install_views(page);state['rfq_enabled']=True
    page.goto(url+'/#/quote');page.get_by_text('QT意向',exact=True).click()
    table=page.get_by_test_id('qt-detail-table')
    expect(table.get_by_test_id('qt-detail-row')).to_have_count(2)
    table.get_by_test_id('qt-detail-row').nth(1).get_by_role('checkbox').check()
    expect(table.get_by_test_id('qt-detail-row').nth(0).get_by_role('checkbox')).not_to_be_checked()
    page.get_by_role('button',name='开始询价',exact=True).click()
    page.wait_for_timeout(200)
    assert captured and captured[0]['rowIds']==[rows[1]['rowId']]
    assert captured[0]['filter']['channel']=='qt'


def test_switching_back_to_all_quotes_restores_aggregate_layout(ui):
    page,url,_,_,_=ui;install_views(page)
    page.goto(url+'/#/quote');page.get_by_text('QT意向',exact=True).click()
    expect(page.get_by_test_id('qt-detail-row')).to_have_count(2)
    page.get_by_text('全部报价',exact=True).click()
    expect(page.get_by_test_id('qt-detail-table')).to_have_count(0)
    expect(page.get_by_test_id('quote-common-table').locator('.mkt-row')).to_have_count(1)
