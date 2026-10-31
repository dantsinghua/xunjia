"""Independent paging/filter acceptance over the real Vue page and transport.

Only intercepted synthetic HTTP/WS peers; no live market, database or sending.
"""
from copy import deepcopy
from io import BytesIO
import json
from urllib.parse import parse_qs, urlsplit

from openpyxl import Workbook, load_workbook
from playwright.sync_api import expect


class Market:
    def __init__(self, page):
        self.page = page
        self.gets = []
        self.refreshes = []
        self.held = []
        self.sockets = []
        self.hold_refresh = False
        self.rows = [dict(code=f'{i+1:06d}.IB', name=f'合成分页-{i:03d}', bondUniCode=str(i+1),
            flowId=10000-i, issueTime=1800000000000, ob=2.1, oo=2.2, obv=1000, oov=2000,
            obs='合成经纪商', oos='合成经纪商', status='待询价', term='2Y',
            bondType='中票', clearing='+1', firmness='实盘', tags=['绿', '科创'],
            val_yield=2.15, net_price=100, issuer='合成发行人') for i in range(150)]
        # Exercise the supported main-thread transport with a real browser socket.
        page.add_init_script('window.SharedWorker = undefined; window.Worker = undefined;')
        page.route('**/api/bonds?*', self.get)
        page.route('**/api/bonds/resolve', self.resolve)
        page.route_web_socket('**/ws/quote', self.socket)

    @staticmethod
    def fulfill(route, data):
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data, ensure_ascii=False))

    def get(self, route):
        query = parse_qs(urlsplit(route.request.url).query)
        before = int(query.get('before', ['0'])[0])
        self.gets.append({'before': before, 'uni': query.get('beforeUni', ['0'])[0],
                          'filter': json.loads(query['filter'][0])})
        offset = 0 if before == 0 else 50 if before == 9951 else 100 if before == 9901 else None
        assert offset is not None, f'Unexpected moving boundary: {before}'
        rows = deepcopy(self.rows[offset:offset+50])
        self.fulfill(route, dict(ok=True, data=rows, total=150, hasMore=offset < 100,
            nextBefore=10000-offset-49, nextUni=str(offset+50), source='public'))

    def resolve(self, route):
        selection = route.request.post_data_json['selection']
        self.refreshes.append(deepcopy(selection))
        if self.hold_refresh:
            self.held.append(route)
        else:
            self.release(route)

    def release(self, route):
        selected = set(route.request.post_data_json['selection'].get('windcodes', []))
        rows = [dict(row, flowId=1000000+i, ob=3.25) for i, row in enumerate(self.rows) if row['code'] in selected]
        self.fulfill(route, {'ok': True, 'data': rows})

    def socket(self, websocket):
        self.sockets.append(websocket)
        def received(raw):
            message = json.loads(raw)
            if message.get('op') == 'auth':websocket.send(json.dumps({'op': 'auth', 'ok': True}))
            elif message.get('op') == 'ping':websocket.send(json.dumps({'op': 'pong'}))
        websocket.on_message(received)

    def burst(self, iteration):
        assert self.sockets, 'The real quote transport did not connect to its isolated WS peer'
        items = [dict(u=str(i+1), f=2000000+iteration*50+i, t=1800000000000,
                      bb=4.2222, bo=4.3333) for i in range(20)]
        items.extend(dict(u=str(1000+i), f=3000000+iteration*50+i, t=1800000000000, bb=8.8888)
                     for i in range(20))
        for websocket in self.sockets:
            websocket.send(json.dumps({'op': 'batch', 'group': 'bond-all', 'sendTime': 1800000000000, 'items': items}))


def page_number(page, number):
    expect(page.locator('.load-more')).to_contain_text(f'第 {number} 页')


def open_market(page, url, market):
    page.goto(url+'/#/quote')
    # Auth/binding initialization can invalidate the first in-flight read;
    # allow its debounced retry before measuring navigation traffic.
    page.clock.run_for(1600)
    page_number(page, 1)
    expect(page.locator('.mkt-row .name').first).to_have_text('合成分页-000')
    assert market.gets and all(item['before'] == 0 for item in market.gets)
    market.initial_gets = market.gets[:]
    market.gets.clear()


def click_page(page, label):
    # Dispatch the real control's click without scrolling its footer into view;
    # scrolling is an independent lazy-load trigger tested below.
    control = page.get_by_role('button', name=label, exact=True)
    expect(control).to_be_enabled()
    control.evaluate('(button) => button.click()')


def test_high_rate_ws_and_slow_background_refresh_do_not_block_next_page_or_move_boundary(ui):
    page, url, _, _, _ = ui
    page.clock.install()
    market = Market(page)
    market.hold_refresh = True
    open_market(page, url, market)
    page.wait_for_function("document.querySelector('.table-scroll') !== null")
    for iteration in range(30):
        market.burst(iteration)
        page.clock.run_for(100)
    assert market.held and market.refreshes[0]['windcodes'] == [row['code'] for row in market.rows[:50]]
    expect(page.locator('.mkt-row').first).to_contain_text('4.2222')
    expect(page.get_by_role('button', name='下一页', exact=True)).to_be_enabled()
    click_page(page, '下一页')
    page_number(page, 2)
    expect(page.locator('.mkt-row .name').first).to_have_text('合成分页-050')
    assert [(item['before'], item['uni']) for item in market.gets] == [(9951, '50')]
    # Complete the old page's refresh after navigation: it cannot pull page 2 back.
    for held in market.held:market.release(held)
    market.held.clear(); market.hold_refresh = False
    for iteration in range(30, 50):
        market.burst(iteration)
        page.clock.run_for(100)
    page_number(page, 2)
    expect(page.locator('.mkt-row .name').first).to_have_text('合成分页-050')
    assert len(market.gets) == 1
    click_page(page, '上一页'); page_number(page, 1)
    expect(page.locator('.mkt-row .name').first).to_have_text('合成分页-000')
    click_page(page, '下一页'); page_number(page, 2)
    expect(page.locator('.mkt-row .name').first).to_have_text('合成分页-050')
    assert len(market.gets) == 1, 'Back/forward must restore the same cached page identities'


def test_background_updates_preserve_each_page_boundary_and_filter_change_invalidates_cache(ui):
    page, url, _, _, _ = ui
    page.clock.install(); market = Market(page)
    open_market(page, url, market)
    page.clock.run_for(3500)
    expect(page.locator('.mkt-row').first).to_contain_text('3.2500')
    click_page(page, '下一页'); page_number(page, 2)
    page.clock.run_for(3500)
    expect(page.locator('.mkt-row').first).to_contain_text('3.2500')
    click_page(page, '下一页'); page_number(page, 3)
    assert [item['before'] for item in market.gets] == [9951, 9901]
    search = page.get_by_placeholder('债券代码 / 简称 / 发行人')
    search.fill('合成分页'); search.press('Enter')
    page_number(page, 1)
    assert market.gets[-1]['before'] == 0 and market.gets[-1]['filter']['keyword'] == '合成分页'
    click_page(page, '下一页'); page_number(page, 2)
    assert [item['before'] for item in market.gets] == [9951, 9901, 0, 9951]
    assert market.gets[-1]['filter']['keyword'] == '合成分页'


def test_scroll_near_bottom_lazily_loads_next_page_once(ui):
    page, url, _, _, _ = ui
    page.clock.install(); market = Market(page)
    open_market(page, url, market)
    scroller = page.locator('.table-scroll')
    assert scroller.evaluate('(el) => el.scrollHeight > el.clientHeight')
    scroller.evaluate('(el) => {el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event("scroll"));}')
    page_number(page, 2)
    expect(page.locator('.mkt-row .name').first).to_have_text('合成分页-050')
    assert [item['before'] for item in market.gets] == [9951]
    assert scroller.evaluate('(el) => el.scrollTop') == 0


def test_old_background_response_cannot_overwrite_new_filter_scope(ui):
    page, url, _, _, _ = ui
    page.clock.install(); market = Market(page); market.hold_refresh = True
    open_market(page, url, market)
    page.clock.run_for(3500)
    assert market.held and market.refreshes[0]['filter']['keyword'] == ''
    old_requests = market.held[:]; market.held.clear()
    search = page.get_by_placeholder('债券代码 / 简称 / 发行人')
    search.fill('合成分页'); search.press('Enter')
    page_number(page, 1)
    assert market.gets[-1]['filter']['keyword'] == '合成分页'
    for route in old_requests:market.release(route)
    page.clock.run_for(200)
    # The old scope resolves the same identities at 3.25: generation/scope
    # fencing must retain the new filter's fresh 2.10 snapshot.
    expect(page.locator('.mkt-row').first).to_contain_text('2.1000')
    expect(page.locator('.mkt-row').first).not_to_contain_text('3.2500')


def test_hidden_unconnected_filters_preserve_supported_indices_and_export_scope(ui, tmp_path):
    page, url, state, requests, _ = ui
    page.clock.install(); market = Market(page)
    workbook = Workbook(); workbook.active.append(['code', 'source']); workbook.active.append(['000001.IB', 'synthetic-paging'])
    output = BytesIO(); workbook.save(output); state['export_bytes'] = output.getvalue()
    open_market(page, url, market)
    groups = page.locator('.f-group')
    expect(groups.locator('.f-label')).to_have_text(['当前状态', '报价类型', '清算', '标签', '类型', '期限'])
    for label in ('可部分执行', '意向价', '公募', '私募'):
        expect(page.locator('.filter').get_by_text(label, exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='保存筛选为方案组（未开放）', exact=True)).to_have_count(0)
    def group(title):return groups.filter(has=page.get_by_text(title, exact=True))
    expect(group('报价类型').locator('.chip')).to_have_text(['全部', '实盘'])
    expect(group('标签').locator('.chip')).to_have_text(['全部', '绿色债', '科创债'])
    expect(group('清算').locator('.chip')).to_have_text(['全部', '+0', '+1', '+N'])
    expect(group('类型').locator('.chip')).to_have_text(['全部信用', '国债', '政金债', '中票', '短融', 'PPN', '公司债', '企业债', 'ABS', '次级债'])
    expect(group('期限').locator('.chip')).to_have_text(['不限', '<1Y', '1–3Y', '3–5Y', '5–7Y', '7–10Y', '10Y+'])
    for title, label in [('当前状态', '待询价'), ('报价类型', '实盘'), ('清算', '+1'),
                         ('标签', '科创债'), ('类型', '中票'), ('期限', '1–3Y')]:
        group(title).get_by_text(label, exact=True).click()
    page.get_by_placeholder('债券代码 / 简称 / 发行人').fill('合成分页')
    page.locator('.price-grid input').nth(0).fill('2.0')
    page.locator('.price-grid input').nth(1).fill('2.3')
    page.get_by_role('button', name='筛选行情', exact=True).click()
    page_number(page, 1)
    chosen = market.gets[-1]['filter']
    assert chosen['chips'] == {'当前状态': 1, '方案': 0, '报价类型': 1, '清算': 2, '标签': 2, '类型': 3, '期限': 2}
    assert chosen['keyword'] == '合成分页' and chosen['prices']['Bid'] == {'lo': '2.0', 'hi': '2.3'}
    assert set(chosen['groups']) == {'bond-type_中票', 'bond-tenor_1to3', 'bond-clearing_T1'}
    with page.expect_download() as pending:page.get_by_test_id('quote-export').click()
    downloaded = tmp_path/'paging-filter.xlsx'; pending.value.save_as(downloaded)
    assert load_workbook(downloaded, read_only=True).active['B2'].value == 'synthetic-paging'
    exported = json.loads(parse_qs(urlsplit(next(r.url for r in reversed(requests) if '/api/bonds/export?' in r.url)).query)['filter'][0])
    assert exported == chosen
