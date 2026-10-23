"""Production bundle + real context-gated HTTP; synthetic DB/identity/files only."""
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_frontend_build_refresh import production_builds
from tests.institution_test_support import selection, OWNER


@pytest.fixture
def release_desk(selection, browser, production_builds):
    s = selection
    app, client = s['app'], s['api']
    # Do not inherit fixture context into HTTP: only the actual browser header counts.
    expected_context = client.headers.pop('X-Xunjia-Context')
    app.state.market.rows_provider = lambda channel, broker: [
        {'code': 'SYN-A.IB', 'name': 'Release synthetic bond', 'bondUniCode': 1,
         'flowId': 1, 'val_yield': '2.30', 'net_price': '100.00', 'tags': []}]
    root = production_builds[0]['root']
    class Static(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Static)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    context = browser.new_context(viewport={'width': 1920, 'height': 1080})
    context.add_init_script("localStorage.setItem('xj_auth_token'," + json.dumps(s['bearer']) + ")")
    page = context.new_page();page.set_default_timeout(10000)
    control = {'posts': [], 'drop_context': False, 'lose_first': False, 'responses': []}
    def forward(route):
        req = route.request; parsed = urlsplit(req.url)
        if not req.url.startswith(base + '/'):
            route.abort();return
        if not parsed.path.startswith('/api/'):
            route.continue_();return
        if parsed.path == '/api/negotiation/updates':
            route.abort();return
        is_send = parsed.path in ('/api/im/rfq/send-direct', '/api/im/rfq/send')
        headers = {key: value for key, value in req.headers.items()
                   if key.lower() not in ('host', 'content-length')}
        if is_send:
            control['posts'].append({'path': parsed.path, 'body': req.post_data_json,
                                     'context': headers.get('x-xunjia-context')})
            if control['drop_context']:
                headers.pop('x-xunjia-context', None)
        response = client.request(req.method, parsed.path + ('?' + parsed.query if parsed.query else ''),
                                  content=req.post_data_buffer, headers=headers)
        if is_send:
            control['responses'].append((response.status_code, response.json()))
            if control['lose_first'] and len(control['posts']) == 1:
                assert response.status_code == 200, response.text
                route.abort('failed');return
        route.fulfill(status=response.status_code, headers={'content-type': response.headers.get('content-type', 'application/json')}, body=response.content)
    page.route('**/*', forward)
    page.route_web_socket('**/*', lambda ws: None)
    try:
        yield s, page, base, expected_context, control
    finally:
        page.unroute_all(behavior='wait');context.close()
        server.shutdown();server.server_close();thread.join(5)
        assert not thread.is_alive()


def command_count(s):
    with s['repository'].db.transaction(write=False) as tx:
        return tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']


def ready(release_desk, flow):
    s, page, base, _, _ = release_desk
    page.goto(base + '/#/quote')
    expect(page.locator('.mkt-row')).to_have_count(1)
    page.locator('.mkt-row input[type=checkbox]').check()
    page.get_by_role('button', name='开始询价', exact=True).click()
    modal = page.get_by_test_id('rfq-modal')
    expect(modal).to_be_visible()
    modal.get_by_test_id('rfq-side').select_option('BUY')
    for name, value in [('volume', '1000'), ('opening', '1.10'), ('limit', '2.40'), ('step', '2')]:
        modal.get_by_test_id('rfq-' + name).fill(value)
    modal.locator('.person[data-uin="peer-wb-a-account-a"]').click()
    if flow == 'preview':
        modal.get_by_test_id('rfq-preview').click()
        expect(modal.get_by_test_id('rfq-server-preview')).to_be_visible()
        expect(modal.get_by_test_id('rfq-confirm')).to_be_enabled()
    return modal, modal.get_by_test_id('rfq-direct-send' if flow == 'direct' else 'rfq-confirm')


def test_old_client_missing_context_is_explicitly_not_submitted_and_creates_zero_commands(release_desk):
    s, _, _, _, _ = release_desk
    body = {'request_id': 'old-dist-missing-context', 'items': [{
        'code': 'SYN-A.IB', 'name': 'Synthetic', 'our_side': 'BUY', 'volume_wan': '1000',
        'opening_yield_pct': '1.10', 'limit_yield_pct': '2.40', 'step_bp': '2',
        'peer_ids': ['peer-wb-a-account-a'], 'pricing_basis': 'absolute'}]}
    before = command_count(s)
    response = s['api'].post('/api/im/rfq/send-direct', json=body)
    assert response.status_code == 400, response.text
    assert response.json()['submitted'] is False
    assert response.json()['code'] == 'ACCOUNT_CONTEXT_CHANGED'
    assert command_count(s) == before == 0


@pytest.mark.parametrize('flow', ['direct', 'preview'])
def test_built_browser_sends_current_context_and_queues_once_without_native_send(release_desk, flow):
    s, _, _, context_id, control = release_desk
    modal, button = ready(release_desk, flow)
    button.click()
    expect(modal.get_by_test_id('rfq-submission')).to_contain_text('已入队，尚未发送')
    assert len(control['posts']) == 1
    assert control['posts'][0]['context'] == context_id
    assert command_count(s) == 1
    assert control['responses'][0][0] == 200
    # No dispatcher runs in this fixture. Signed activation is the only outbound manifest.
    with s['repository'].db.transaction(write=False) as tx:
        assert tx.execute('SELECT state FROM xj2_command').fetchone()['state'] == 'QUEUED'


@pytest.mark.parametrize('flow', ['direct', 'preview'])
def test_definitive_context_rejection_never_shows_unknown_and_unlocks_authority(release_desk, flow):
    s, _, _, context_id, control = release_desk
    modal, button = ready(release_desk, flow)
    control['drop_context'] = True
    button.click()
    expect(modal).to_contain_text('未提交')
    expect(modal).not_to_contain_text('提交结果未确认')
    expect(modal).not_to_contain_text('重试核对本次发送')
    expect(modal.get_by_test_id('rfq-opening')).to_be_enabled()
    assert len(control['posts']) == 1 and control['posts'][0]['context'] == context_id
    assert control['responses'][0][1]['submitted'] is False
    assert control['responses'][0][1]['code'] == 'ACCOUNT_CONTEXT_CHANGED'
    assert command_count(s) == 0


@pytest.mark.parametrize('flow', ['direct', 'preview'])
def test_lost_response_reuses_identical_request_identity_and_one_real_command(release_desk, flow):
    s, _, _, context_id, control = release_desk
    modal, button = ready(release_desk, flow)
    control['lose_first'] = True
    button.click()
    expect(modal).to_contain_text('提交结果未确认')
    assert command_count(s) == 1
    expect(button).to_be_enabled()
    button.click()
    expect(modal.get_by_test_id('rfq-submission')).to_contain_text('已入队，尚未发送')
    assert len(control['posts']) == 2
    assert control['posts'][0] == control['posts'][1]
    assert control['posts'][0]['context'] == context_id
    assert command_count(s) == 1
