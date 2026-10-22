"""Independent real-Vue RFQ contract; intercepted synthetic API, zero real sending."""
import json
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


@pytest.fixture
def multibond(ui):
    page, base, state, requests, counts = ui
    state['rfq_enabled'] = True
    control = {'floors': [], 'hold_preview': False, 'held': [], 'version': 1, 'workbench': 'qa-wb'}
    bonds = [{'i': i + 1, 'code': code, 'name': name, 'val': '2.30/100.00',
              'tags': [], 'status': '待询价'} for i, (code, name) in enumerate([
                  ('SYN-A.IB', '合成甲券'), ('SYN-B.IB', '合成乙券')])]
    def intercept(route):
        req = route.request;path = urlsplit(req.url).path
        requests.append(req);counts[path] += 1
        if path in ['/api/bonds', '/api/bonds/resolve']:
            data = bonds
        elif path == '/api/im/inquiry/floor':
            data = {'items': control['floors']}
        elif path == '/api/strategy':
            data = {'configured': True, 'active_template_id': 'synthetic-strategy',
                'templates': [{'template_id': 'synthetic-strategy', 'start_offset_bp': 20,
                    'boundary_bp': 5, 'step_bp': 2, 'strict_match': True}],
                'system': {'s1_round_lot_wan': 1000, 's2_crossday_bp': 3}}
        elif path == '/api/im/links':
            data = {'active_id': 'qa-link', 'links': [{'id': 'qa-link', 'name': '合成当前号',
                'mode': 'oss', 'mode_label': '文件中转', 'active': True, 'tested': True,
                'account_uid': '910001', 'workbench_id': control['workbench'], 'version': control['version'],
                'contacts': [{'uin': '910100', 'name': '合成收件人'}], 'form': {'oss': {}}}]}
        elif path == '/api/im/rfq/preview':
            if control['hold_preview']:
                control['held'].append(route);return
            body = req.post_data_json
            data = {'batch_id': 'synthetic-multibond', 'version': 1, 'status': 'PREVIEW',
                'items': [{**item, 'peer_id': peer} for item in body['items'] for peer in item['peer_ids']],
                'commands_preview': [{'peer_id': '910100', 'text': '合成逐券服务端预览'}]}
        else:
            raise AssertionError(path)
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'data': data,
            **({'total': len(bonds), 'hasMore': False, 'nextBefore': 0} if path == '/api/bonds' else {})},ensure_ascii=False))
    for pattern in ['/api/bonds**', '/api/im/inquiry/floor', '/api/strategy',
                    '/api/im/links', '/api/im/rfq/preview']:
        page.route(base + pattern, intercept)
    return ui, control


def open_two(multibond):
    ui, _ = multibond;page, base, *_ = ui
    page.goto(base + '/#/quote')
    expect(page.locator('.mkt-row')).to_have_count(2)
    for checkbox in page.locator('.mkt-row input[type=checkbox]').all():
        checkbox.check()
    page.get_by_role('button', name='开始询价', exact=True).click()
    modal = page.get_by_test_id('rfq-modal');expect(modal).to_be_visible()
    expect(modal.locator('.bond')).to_have_count(2)
    return page, modal


def fill_active(modal, *, limit='2.40'):
    modal.get_by_test_id('rfq-side').select_option('BUY')
    for field, value in [('volume', '1000'), ('opening', '2.50'), ('limit', limit), ('step', '2')]:
        modal.get_by_test_id('rfq-' + field).fill(value)
    modal.locator('.person[data-uin="910100"]').click()


def writes(ui):
    return [req for req in ui[3] if req.method not in ['GET', 'HEAD', 'OPTIONS']
            and urlsplit(req.url).path != '/api/bonds/resolve']


def test_two_bonds_expose_all_missing_authority_immediately_and_each_edit_removes_its_issue(multibond):
    page, modal = open_two(multibond)
    summary = modal.get_by_test_id('rfq-validation-summary');expect(summary).to_be_visible()
    expect(summary).to_contain_text('合成甲券');expect(summary).to_contain_text('合成乙券')
    expect(modal.get_by_test_id('rfq-preview')).to_be_disabled()
    fill_active(modal)
    expect(modal.locator('[data-testid=rfq-bond-validation][data-code="SYN-A.IB"] [data-testid=rfq-validation-issue]')).to_have_count(0)
    expect(modal.locator('[data-testid=rfq-bond-validation][data-code="SYN-B.IB"] [data-field=our_side]')).to_be_visible()
    modal.locator('[data-testid=rfq-bond-validation][data-code="SYN-B.IB"] [data-field=our_side]').click()
    expect(modal.locator('.bond.on')).to_contain_text('合成乙券')
    fill_active(modal)
    expect(modal.get_by_test_id('rfq-validation-issue')).to_have_count(0)
    expect(modal.get_by_test_id('rfq-preview')).to_be_enabled()
    assert writes(multibond[0]) == []


def test_unsaved_boundary_is_for_this_batch_and_never_saves_a_floor_implicitly(multibond):
    _, modal = open_two(multibond)
    fill_active(modal, limit='2.41')
    modal.locator('.bond').nth(1).locator('.bond-hd').click();fill_active(modal, limit='2.42')
    modal.get_by_test_id('rfq-preview').click()
    expect(modal.get_by_test_id('rfq-server-preview')).to_be_visible()
    commands = writes(multibond[0])
    assert [urlsplit(x.url).path for x in commands] == ['/api/im/rfq/preview']
    items = commands[0].post_data_json['items']
    assert [(i['code'], i['limit_yield_pct']) for i in items] == [('SYN-A.IB', '2.41'), ('SYN-B.IB', '2.42')]
    assert all(i['pricing_basis'] == 'absolute' for i in items)
    assert multibond[0][4]['/api/im/rfq/send'] == 0


def test_saved_decimal_yield_and_bp_are_separate_per_bond_references(multibond):
    ui, control = multibond
    control['floors'] = [{'code': 'SYN-A.IB', 'kind': 'yield', 'value': '2.4012'},
                         {'code': 'SYN-B.IB', 'kind': 'bp', 'value': '5'}]
    _, modal = open_two(multibond)
    expect(modal.get_by_test_id('rfq-limit')).to_have_value('2.4012')
    expect(modal.get_by_test_id('rfq-side')).to_have_value('')
    expect(modal.locator('.bond').first).to_contain_text('2.40')
    modal.locator('.bond').nth(1).locator('.bond-hd').click()
    expect(modal.get_by_test_id('rfq-pricing-basis')).to_have_value('bp')
    expect(modal.get_by_test_id('rfq-limit')).to_have_value('')
    expect(modal.get_by_test_id('rfq-side')).to_have_value('')
    expect(modal.locator('.bond').nth(1)).to_contain_text('5BP')
    modal.get_by_test_id('rfq-side').select_option('BUY')
    expect(modal.get_by_test_id('rfq-limit')).to_have_value('2.35')
    modal.get_by_role('button', name='用已保存底价/策略补充空值', exact=True).click()
    expect(modal.get_by_test_id('rfq-limit')).to_have_value('2.35')
    expect(modal.get_by_test_id('rfq-opening')).to_have_value('2.50')
    expect(modal.get_by_test_id('rfq-pricing-basis')).to_have_value('bp')
    modal.get_by_test_id('rfq-limit').fill('2.36')
    modal.get_by_role('button', name='用已保存底价/策略补充空值', exact=True).click()
    expect(modal.get_by_test_id('rfq-limit')).to_have_value('2.36')
    assert writes(ui) == []


def test_late_preview_for_prior_binding_version_cannot_enable_confirmation(multibond):
    ui, control = multibond;control['hold_preview'] = True
    page, modal = open_two(multibond);fill_active(modal)
    modal.locator('.bond').nth(1).locator('.bond-hd').click();fill_active(modal)
    modal.get_by_test_id('rfq-preview').click()
    page.wait_for_function('true')
    assert len(control['held']) == 1
    control['version'] = 2
    # Existing main bar polls real link state every 5s. No direct Vue state mutation.
    expect(modal).to_contain_text('当前账号已变化', timeout=9000)
    control['held'].pop().fulfill(content_type='application/json', body=json.dumps({'ok':True,'data':{
        'batch_id':'late-old-account-preview','version':1,'status':'PREVIEW','items':[],
        'commands_preview':[{'peer_id':'910100','text':'不准采用旧账号预览'}]}}))
    expect(modal.get_by_test_id('rfq-server-preview')).to_have_count(0)
    expect(modal.get_by_test_id('rfq-confirm')).to_be_disabled()
    assert ui[4]['/api/im/rfq/send'] == 0


def test_quote_compact_and_monitor_retains_book_actions_without_detail_list(multibond):
    ui, _ = multibond;page, base, *_ = ui
    page.goto(base + '/#/quote')
    expect(page.locator('.im-bar')).to_be_visible()
    expect(page.locator('.im-bar').get_by_role('button',name='读取通讯录',exact=True)).to_have_count(0)
    expect(page.locator('.im-bar .im-book')).to_have_count(0)
    page.goto(base + '/#/monitor')
    bar = page.locator('.im-bar');expect(bar).to_be_visible()
    bar.get_by_role('button',name='读取通讯录',exact=True).click()
    expect(page.get_by_text('已读取 1 人（通讯录）', exact=True)).to_be_visible()
    expect(bar.locator('.im-book, .im-person')).to_have_count(0)
    expect(bar.get_by_role('button',name='刷新通讯录',exact=True)).to_be_enabled()
    expect(bar).not_to_contain_text('910100')


from tests.cutover.test_im_catalog_selection import selection
from tests.institution_test_support import OWNER


def test_real_saved_floor_decimal_http_projection_reaches_two_bond_vue_and_trusted_bp_preview(selection,browser,frontend_url):
    """Actual save-floor/GET, Vue and actual RFQ preview; no confirmation/dispatch."""
    import socket
    import threading
    import time
    import uvicorn
    from server.reference_data import ReferenceSnapshot
    s=selection;app=s['app']
    synthetic_rows=[{'code':code,'name':name,'bondUniCode':i+1,'flowId':i+1,
        'val_yield':'2.30','net_price':'100.00','tags':[]} for i,(code,name) in enumerate([
            ('SYN-A.IB','真实接口合成甲券'),('SYN-B.IB','真实接口合成乙券')])]
    app.state.market.rows_provider=lambda channel,broker:synthetic_rows
    day=app.state.inquiry.trade_day()
    class TrustedSyntheticReference:
        def latest_valuation_day(self,stamp):return day
        def snapshot(self):
            return ReferenceSnapshot('synthetic-trusted-'+day,day,bonds=[{
                'code':'SYN-B.IB','valuation_yield_pct':'2.30','valuation_date':day}])
    provider=TrustedSyntheticReference();app.state.inquiry.reference_provider=provider;app.state.inquiry.calendar=provider
    assert s['api'].post('/api/im/inquiry/floor',json={'codes':['SYN-A.IB'],'kind':'yield','value':'2.4012'}).status_code==200
    assert s['api'].post('/api/im/inquiry/floor',json={'codes':['SYN-B.IB'],'kind':'bp','bp':'5'}).status_code==200
    stored=s['api'].get('/api/im/inquiry/floor').json()['data']['items']
    assert [(i['code'],i['kind'],i['value']) for i in stored]==[
        ('SYN-A.IB','yield','2.4012'),('SYN-B.IB','bp','5')]
    assert all(type(i['value']) is str for i in stored)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
    backend='http://127.0.0.1:'+str(listener.getsockname()[1])
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
    assert server.started
    context=browser.new_context(viewport={'width':1920,'height':1080})
    context.add_init_script("localStorage.setItem('xj_auth_token',"+json.dumps(s['bearer'])+")")
    page=context.new_page();calls=[]
    def forward(route):
        req=route.request;parsed=urlsplit(req.url)
        if not req.url.startswith(frontend_url+'/'):route.abort();return
        if not parsed.path.startswith('/api/'):route.continue_();return
        calls.append((req.method,parsed.path))
        response=route.fetch(url=backend+parsed.path+('?' + parsed.query if parsed.query else ''))
        route.fulfill(response=response)
    page.route('**/*',forward);page.route_web_socket('**/*',lambda websocket:None)
    try:
        page.goto(frontend_url+'/#/quote')
        expect(page.locator('.mkt-row')).to_have_count(2)
        for cb in page.locator('.mkt-row input[type=checkbox]').all():cb.check()
        page.get_by_role('button',name='开始询价',exact=True).click()
        modal=page.get_by_test_id('rfq-modal');expect(modal).to_be_visible()
        modal.locator('.bond').filter(has_text='真实接口合成甲券').locator('.bond-hd').click()
        expect(modal.get_by_test_id('rfq-limit')).to_have_value('2.4012')
        modal.get_by_test_id('rfq-side').select_option('BUY')
        for field,value in [('volume','1000'),('opening','2.50'),('step','2')]:modal.get_by_test_id('rfq-'+field).fill(value)
        modal.locator('.person[data-uin="peer-wb-a-account-a"]').click()
        modal.locator('.bond').filter(has_text='真实接口合成乙券').locator('.bond-hd').click()
        expect(modal.get_by_test_id('rfq-pricing-basis')).to_have_value('bp')
        modal.get_by_test_id('rfq-side').select_option('BUY')
        expect(modal.get_by_test_id('rfq-limit')).to_have_value('2.35')
        for field,value in [('volume','1000'),('opening','2.50'),('step','2')]:modal.get_by_test_id('rfq-'+field).fill(value)
        modal.locator('.person[data-uin="peer-wb-a-account-a"]').click()
        expect(modal.get_by_test_id('rfq-preview')).to_be_enabled()
        modal.get_by_test_id('rfq-preview').click()
        expect(modal.get_by_test_id('rfq-server-preview')).to_be_visible()
        expect(modal.get_by_test_id('rfq-confirm')).to_be_enabled()
        assert [x for x in calls if x[0]!='GET' and x[1]!='/api/bonds/resolve']==[('POST','/api/im/rfq/preview')]
        with app.state.repository.db.transaction(write=False) as tx:
            batch=tx.execute('SELECT preview_json FROM xj2_batch WHERE owner_id=?',(OWNER,)).fetchone()
            assert batch is not None
            normalized={item['plan']['code']:item for item in json.loads(batch['preview_json'])['items']}
            assert normalized['SYN-A.IB']['plan']['limit_yield_pct']=='2.4012'
            assert normalized['SYN-B.IB']['policy']['mode']=='CHINABOND_BP'
            assert normalized['SYN-B.IB']['valuation']['snapshot_id']=='synthetic-trusted-'+day
            assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']==0
            assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?',(OWNER,)).fetchone()['send_allowed']==0
    finally:
        page.unroute_all(behavior='wait');context.close();server.should_exit=True;thread.join(10);listener.close()
        assert not thread.is_alive()
