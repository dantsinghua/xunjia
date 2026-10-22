"""UI contract only: synthetic directory APIs; real Codec/HTTP gates are separate."""
import json
import time
from relay.gateway_paths import CONTRACT
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


@pytest.fixture
def selector(ui):
    page, base, *_ = ui
    stamp=int(time.time())
    state = {'selected': {'workbench_id': 'wb-a', 'account_uid': 'same-account', 'version': 1},
        'workbenches': [{'workbench_id': wid, 'name': 'Collector ' + wid, 'updated_at': stamp,
            'stale': False, 'accounts': [{'account_uid': uid, 'account_nick': wid + ':' + uid,
                'channel': 'qidian', 'stale': False, 'available': True, 'updated_at': stamp,
                'capabilities': ['negotiation-v22-watermark']} for uid in ['same-account' if wid=='wb-a' else 'other-account', wid + '-only']]}
            for wid in ['wb-a', 'wb-b']], 'error': None, 'refresh_error': None,
        'wrong_pair': False, 'hold_select': False, 'hold_contacts': False, 'held': [], 'calls': []}
    def book(pair=None):
        pair = pair or state['selected']
        stale = next(w for w in state['workbenches'] if w['workbench_id'] == pair['workbench_id'])['stale']
        return {'source': 'its-relay', 'workbench_id': pair['workbench_id'], 'account_uid': pair['account_uid'],
            'updated_at': stamp, 'stale': stale, 'org': {},
            'contacts': [{'uin': pair['workbench_id'] + ':' + pair['account_uid'],
                          'name': 'Person from ' + pair['workbench_id']}]}
    def fulfill(route, data, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))
    def route(r):
        req = r.request;path = urlsplit(req.url).path
        body = req.post_data_json if req.post_data else None
        state['calls'].append((req.method, path, body))
        if path == '/api/im/binding/select':
            if state['hold_select']:
                state['held'].append(r);return
            if state['error']:
                fulfill(r, {'ok': False, 'hint': state['error']}, 409);return
            selected = {'workbench_id': body['workbench_id'], 'account_uid': body['account_uid'],
                        'version': state['selected']['version'] + 1}
            contacts = book(selected)
            if state['wrong_pair']:
                contacts = {**contacts, 'workbench_id': 'wrong-workbench'}
            else:
                state['selected'] = selected
            fulfill(r, {'ok': True, 'data': {'binding': selected, 'selected': selected,
                'contacts': contacts, 'connected': not contacts['stale']}})
        elif path == '/api/im/contacts':
            if state['hold_contacts']:
                state['held'].append(r);return
            fulfill(r, {'ok': True, 'data': book()})
        elif path == '/api/im/mq-config':
            fulfill(r, {'ok':True,'data':{'editable':True,'configured':True,'applied':True,
                'backend':'file_gateway','base_url':'https://configured.synthetic.invalid',
                'credentials_set':True,'revision':'configured-fixture',
                'account_uid':state['selected']['account_uid'],'workbench_id':state['selected']['workbench_id'],'note':''}})
        elif path == '/api/im/links':
            current = state['selected']
            links = [{'id': current['account_uid'] + ':' + mode, 'mode': mode,
                'name': '', 'account_uid': current['account_uid'], 'workbench_id': current['workbench_id'],
                'active': mode == 'oss', 'configured': True, 'version': current['version'],
                'form': {mode: {}}, 'contacts': []} for mode in ['oss', 'mail']]
            fulfill(r, {'ok': True, 'data': {'active_id': links[0]['id'], 'links': links,
                'binding_version': current['version']}})
        else:
            fulfill(r, {'ok': True, 'data': {'contract':CONTRACT,'offline_seconds':60,'workbenches': state['workbenches'],
                'selected': state['selected'], 'refresh_requested': path.endswith('/refresh'),
                'refresh_error': state['refresh_error']}})
    for pattern in ['/api/im/catalog**', '/api/im/binding/select', '/api/im/contacts**', '/api/im/links', '/api/im/mq-config**']:
        page.route(base + pattern, route)
    return page, base, state, book, fulfill


def open_selector(selector):
    page, base, *_ = selector
    page.set_viewport_size({'width': 1920, 'height': 1080})
    page.goto(base + '/#/link')
    expect(page.get_by_test_id('collector-account-settings')).to_be_visible()
    expect(page.get_by_test_id('collector-workbench')).to_have_value('wb-a')
    return page


def pick_b(page):
    page.get_by_test_id('collector-workbench').select_option('wb-b')
    expect(page.get_by_test_id('collector-account')).to_have_value('')
    page.get_by_test_id('collector-account').select_option('other-account')


def selection_writes(s):
    return [x for x in s['calls'] if x[0] != 'GET']


def test_real_options_are_two_level_and_draft_workbench_change_resets_account_without_post(selector):
    page = open_selector(selector);s = selector[2]
    expect(page.get_by_test_id('collector-workbench').locator('option')).to_have_count(3)
    page.get_by_test_id('collector-workbench').select_option('wb-b')
    expect(page.get_by_test_id('collector-account')).to_have_value('')
    assert page.get_by_test_id('collector-account').locator('option').evaluate_all('(items) => items.map(x => x.value)') == [
        '', 'other-account', 'wb-b-only']
    expect(page.get_by_test_id('collector-account').locator('option[value="wb-a-only"]')).to_have_count(0)
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    assert selection_writes(s) == []


def test_save_distinct_online_pair_reads_correct_contacts_and_survives_refresh(selector):
    page = open_selector(selector);s = selector[2]
    pick_b(page);page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-b')
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('Person from wb-b')
    expect(page.get_by_test_id('collector-contacts')).not_to_contain_text('Person from wb-a')
    assert selection_writes(s) == [('POST', '/api/im/binding/select', {
        'workbench_id': 'wb-b', 'account_uid': 'other-account', 'expected_version': 1})]
    page.reload();expect(page.get_by_test_id('collector-current')).to_contain_text('wb-b')
    expect(page.get_by_test_id('collector-workbench')).to_have_value('wb-b')
    expect(page.get_by_test_id('collector-account')).to_have_value('other-account')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_authenticated_offline_directory_is_labeled_and_cannot_be_selected(selector):
    state=selector[2];state['workbenches'][1]['stale']=True
    for row in state['workbenches'][1]['accounts']:row.update(stale=True,available=False)
    page=open_selector(selector);pick_b(page)
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    expect(page.get_by_test_id('collector-account-settings')).to_contain_text('采集端当前收发状态未确认')
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-a')
    assert selection_writes(state)==[]


def test_empty_directory_preserves_saved_identity_and_does_not_invent_options(selector):
    page, base, s, *_ = selector;s['workbenches'] = []
    page.goto(base + '/#/link')
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-a')
    expect(page.get_by_test_id('collector-current')).to_contain_text('目录暂不可用')
    expect(page.get_by_test_id('collector-workbench').locator('option')).to_have_count(1)
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    assert selection_writes(s) == []


def test_explicit_catalog_refresh_and_refresh_failure_do_not_silently_change_binding(selector):
    page = open_selector(selector);s = selector[2];s['refresh_error'] = 'RECONCILIATION_REQUIRED'
    page.get_by_test_id('collector-refresh').click()
    expect(page.get_by_test_id('collector-account-settings')).to_contain_text('RECONCILIATION_REQUIRED')
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-a')
    assert selection_writes(s) == [('POST', '/api/im/catalog/refresh', {})]


def test_rejected_selection_keeps_previous_pair_and_does_not_display_wrong_contacts(selector):
    page = open_selector(selector);s = selector[2];s['error'] = '先停止机器人并对账 UNKNOWN'
    pick_b(page);page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-account-settings')).to_contain_text(s['error'])
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-a')
    expect(page.get_by_test_id('collector-contacts')).to_have_count(0)
    assert len(selection_writes(s)) == 1


def test_same_uid_wrong_workbench_contact_response_is_not_applied(selector):
    page = open_selector(selector);selector[2]['wrong_pair'] = True
    pick_b(page);page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-account-settings')).to_contain_text('不属于所选工作台')
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-a')
    expect(page.get_by_test_id('collector-contacts')).to_have_count(0)


def test_pending_selection_disables_repeat_and_releases_only_matching_pair(selector):
    page = open_selector(selector);s = selector[2];s['hold_select'] = True
    pick_b(page);page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    expect(page.get_by_test_id('collector-workbench')).to_be_disabled()
    expect(page.get_by_test_id('collector-account')).to_be_disabled()
    expect(page.get_by_test_id('collector-refresh')).to_be_disabled()
    assert len(selection_writes(s)) == 1 and len(s['held']) == 1
    selected = {'workbench_id': 'wb-b', 'account_uid': 'other-account', 'version': 2}
    s['selected'] = selected
    selector[4](s['held'].pop(), {'ok': True, 'data': {'binding': selected, 'selected': selected,
        'contacts': selector[3](selected), 'connected': True}})
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-b')
    expect(page.get_by_test_id('collector-connect')).to_be_enabled()


def test_late_contact_response_from_unmounted_old_pair_cannot_overwrite_new_pair(selector):
    page = open_selector(selector);s = selector[2];s['hold_contacts'] = True
    old_book = selector[3]()
    page.get_by_test_id('collector-read-saved').click()
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    assert len(s['held']) == 1
    page.goto(selector[1] + '/#/quote')
    s['selected'] = {'workbench_id': 'wb-b', 'account_uid': 'other-account', 'version': 2}
    s['hold_contacts'] = False
    page.goto(selector[1] + '/#/link')
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-b')
    selector[4](s['held'].pop(), {'ok': True, 'data': old_book})
    page.get_by_test_id('collector-read-saved').click()
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('Person from wb-b')
    expect(page.get_by_test_id('collector-contacts')).not_to_contain_text('Person from wb-a')


def test_mq_identity_summary_tracks_saved_pair_without_discarding_unsaved_connection_draft(selector):
    page, base, s, *_ = selector
    calls = []
    def mq(route):
        request = route.request;calls.append(request.method)
        selected = s['selected']
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'data': {
            'editable': True, 'configured': True, 'backend': 'file_gateway',
            'base_url': 'https://saved.synthetic.invalid', 'credentials_set': True, 'applied': True,
            'account_uid': selected['account_uid'], 'workbench_id': selected['workbench_id'],
            'revision': 'synthetic-revision', 'note': 'MQ configured'}}))
    page.route(base + '/api/im/mq-config**', mq)
    open_selector(selector)
    panel = page.get_by_test_id('mq-config')
    expect(panel.locator('.summary')).to_contain_text('wb-a')
    page.locator('[name=mq-base-url]').fill('https://unsaved-draft.synthetic.invalid')
    page.locator('[name=mq-config-yaml]').fill('key: synthetic-draft-retained\nsm4_key: 0123456789abcdef\n')
    pick_b(page);page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-current')).to_contain_text('wb-b')
    expect(panel.locator('.summary')).to_contain_text('wb-b')
    expect(page.locator('[name=mq-base-url]')).to_have_value('https://unsaved-draft.synthetic.invalid')
    expect(page.locator('[name=mq-config-yaml]')).to_have_value('key: synthetic-draft-retained\nsm4_key: 0123456789abcdef\n')
    assert set(calls) == {'GET'}, 'Changing collector identity must not save or test the remote MQ connection'


# These two cases forward every API request to the real ASGI application. The
# upstream files wire remains synthetic, exactly as the backend integration gates.
from tests.cutover.test_im_catalog_selection import selection
from tests.institution_test_support import directory_request, OWNER


@pytest.mark.parametrize('stale', [False, True])
def test_real_backend_online_contacts_reach_quote_and_offline_selection_is_blocked(selection, browser, frontend_url, stale):
    import socket
    import threading
    import time
    import uvicorn
    s = selection
    wid, uid = 'wb-main-quote', 'main-account'
    people = [{'uin': 'main-peer-' + str(i), 'name': 'Main pair person ' + str(i)} for i in range(5)]
    s['publish'](wid, uid, 'catalog', expired=stale)
    s['publish'](wid, uid, 'contacts', expired=stale, extra={'contacts': people})
    directory_request(s, refresh=True)
    # This test owns only the synthetic institution directory worker, not business tasks.
    s['runtime'].background_enabled = True
    s['runtime'].start()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0));listener.listen(128)
    backend_base = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(s['app'], log_level='error', access_log=False, lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started, 'Owned ASGI HTTP server did not start'
    context = browser.new_context(viewport={'width': 1920, 'height': 1080})
    context.add_init_script("localStorage.setItem('xj_auth_token', " + json.dumps(s['bearer']) + ")")
    page = context.new_page()
    page.set_default_timeout(10000)
    calls = []
    def forward(route):
        req = route.request
        if not req.url.startswith(frontend_url + '/'):
            route.abort();return
        parsed = urlsplit(req.url)
        if not parsed.path.startswith('/api/'):
            route.continue_();return
        if parsed.path == '/api/negotiation/updates':
            # 本门只核联系人读取；不代理独立长连接，避免同步fetch等待SSE结束。
            route.abort();return
        calls.append((req.method, parsed.path))
        # Use only this test's own ASGI listener. A synchronous stdlib connection
        # does not re-enter Playwright while fulfilling concurrent intercepted routes.
        # Vendor HTTP remains isolated by the institution fixture's opener.
        import http.client
        host, port = listener.getsockname()[:2]
        assert host == '127.0.0.1'
        headers = {key:value for key,value in req.headers.items()
                   if key.lower() not in ('host','content-length','connection','accept-encoding')}
        connection = http.client.HTTPConnection(host, port, timeout=15)
        try:
            connection.request(req.method, parsed.path + ('?' + parsed.query if parsed.query else ''),
                               body=req.post_data_buffer, headers=headers)
            response = connection.getresponse()
            status, body = response.status, response.read()
            response_headers = {key:value for key,value in response.getheaders()
                                if key.lower() not in ('content-length','transfer-encoding','connection')}
        finally:
            connection.close()
        route.fulfill(status=status, headers=response_headers, body=body)
    page.route('**/*', forward)
    # This contacts gate leaves the browser quote socket local and unforwarded;
    # closing synchronously during its handshake causes a Playwright reentry loop.
    page.route_web_socket('**/*', lambda ws: None)
    try:
        page.goto(frontend_url + '/#/link')
        expect(page.get_by_test_id('collector-account-settings')).to_be_visible()
        page.get_by_test_id('collector-workbench').select_option(wid)
        page.get_by_test_id('collector-account').select_option(uid)
        if stale:
            expect(page.get_by_test_id('collector-connect')).to_be_disabled()
            expect(page.get_by_test_id('collector-account-settings')).to_contain_text('采集端当前收发状态未确认')
            assert s['repository'].binding_for(OWNER)['workbench_id']=='wb-a'
            assert [c for c in calls if c[0]!='GET']==[]
            return
        page.get_by_test_id('collector-connect').click()
        expect(page.get_by_test_id('collector-current')).to_contain_text(wid)
        expect(page.get_by_test_id('collector-contacts').locator('.person')).to_have_count(5)
        page.goto(frontend_url + '/#/quote')
        bar = page.locator('.im-bar')
        expect(bar.locator('.im-sel')).to_have_value('main-account:oss')
        expect(bar).to_contain_text(wid)
        expect(bar).to_contain_text('目录已校验')
        # Quote保持紧凑；Monitor仅保留通讯录动作，完整列表在采集通道。
        expect(bar.get_by_role('button', name='读取通讯录', exact=True)).to_have_count(0)
        expect(bar.locator('.im-book')).to_have_count(0)
        page.goto(frontend_url + '/#/monitor')
        bar = page.locator('.im-bar')
        expect(bar).to_contain_text(wid)
        read = bar.get_by_role('button', name='读取通讯录', exact=True)
        expect(read).to_be_enabled();read.click()
        expect(bar.locator('.im-person, .im-book')).to_have_count(0)
        expect(page.get_by_text('已读取 5 人（通讯录）', exact=True)).to_be_visible()
        expect(page.get_by_text('已读取 5 人（私聊会话）', exact=True)).to_have_count(0)
        for person in people:
            expect(bar).not_to_contain_text(person['name'])
        expect(bar).not_to_contain_text('发送成功')
        assert [c for c in calls if c[0] != 'GET'] == [('POST', '/api/im/binding/select')]
        with s['repository'].db.transaction(write=False) as tx:
            assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 0
            assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?', (OWNER,)).fetchone()['send_allowed'] == 0
    finally:
        page.unroute_all(behavior='wait')
        context.close()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
