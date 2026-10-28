"""HTTP capability and live-binding regressions over real Vue with synthetic APIs."""
import json
from urllib.parse import urlsplit

from playwright.sync_api import expect
from test_unbound_collector_draft import unbound, open_unbound


def test_monitor_send_without_random_uuid_reaches_api_and_retry_reuses_request_id(ui):
    page, base, state, *_ = ui
    state['rfq_enabled'] = True
    page.add_init_script("Object.defineProperty(globalThis.crypto, 'randomUUID', {value: undefined, configurable: true})")
    calls = []
    def send(route):
        calls.append(route.request.post_data_json)
        if len(calls) == 1:
            route.fulfill(status=503, json={'ok': False, 'hint': '合成提交响应未确认'})
        else:
            route.fulfill(json={'ok': True, 'data': {'cmd_id': 'synthetic-command', 'status': 'queued'}})
    page.route(base + '/api/monitor/send', send)
    page.route(base + '/api/im/receipts/synthetic-command',
               lambda route: route.fulfill(json={'ok': True, 'data': {'status': 'queued'}}))
    page.goto(base + '/#/monitor')
    expect(page.locator('.cp-panel')).to_contain_text('合成对手甲')
    assert page.evaluate("location.protocol === 'http:' && typeof crypto.randomUUID === 'undefined' && typeof crypto.getRandomValues === 'function'")
    page.locator('.send-input').fill('合成人工询价，不代表真实发送')
    button = page.locator('.send-btn')
    expect(button).to_be_enabled()
    with page.expect_request(base + '/api/monitor/send', timeout=2500):
        button.click()
    expect(button).to_be_enabled()
    expect(page.locator('.send-input')).to_have_value('合成人工询价，不代表真实发送')
    with page.expect_request(base + '/api/monitor/send', timeout=2500):
        button.click()
    expect(button).to_have_text('已提交')
    assert len(calls) == 2 and calls[0] == calls[1]
    assert isinstance(calls[0]['request_id'], str) and calls[0]['request_id']
    assert calls[0]['request_id'] == calls[0]['idempotency_key']


def test_binding_warning_tracks_real_selection_then_server_revocation_without_relogin(unbound):
    page, base, state = unbound
    def contacts():
        return {**(state['selected'] or {}), 'contacts': [{'uin': 'synthetic-peer', 'name': '合成已绑定好友'}],
                'source': 'its-relay', 'org': {'departments': [], 'members': []}, 'stale': False}
    def links(route):
        selected = state['selected']
        items = [{**selected, 'id': 'synthetic-link', 'mode': 'oss', 'active': True, 'tested': True,
                  'configured': True, 'contacts': contacts()['contacts'], 'form': {'oss': {}}}] if selected else []
        route.fulfill(json={'ok': True, 'data': {'links': items, 'active_id': 'synthetic-link' if selected else ''}})
    def select(route):
        body = route.request.post_data_json
        state['selected'] = {'workbench_id': body['workbench_id'], 'account_uid': body['account_uid'], 'version': 1}
        route.fulfill(json={'ok': True, 'data': {'binding': state['selected'], 'contacts': contacts(), 'connected': True}})
    page.route(base + '/api/im/links', links)
    page.route(base + '/api/im/binding/select', select)
    page.route(base + '/api/im/contacts**', lambda route: route.fulfill(json={'ok': True, 'data': contacts()}))
    open_unbound(unbound)
    warning = page.get_by_text('当前本人账号尚未绑定企点，发送功能不可用。', exact=True)
    expect(warning).to_be_visible()
    page.get_by_test_id('collector-workbench').select_option('synthetic-wb')
    page.get_by_test_id('collector-account').select_option('synthetic-account')
    page.get_by_test_id('collector-connect').click()
    expect(page.get_by_test_id('collector-current')).to_contain_text('synthetic-account')
    expect(page.get_by_test_id('collector-contacts')).to_contain_text('合成已绑定好友')
    expect(warning).to_have_count(0, timeout=1500)
    # The login response remains binding=null. Only the authoritative collector
    # catalog changes; the warning must follow its live binding without login.
    state['selected'] = None
    with page.expect_response(lambda response: urlsplit(response.url).path == '/api/im/catalog'):
        page.clock.run_for(6000)
    expect(page.get_by_test_id('collector-current')).to_have_count(0)
    expect(warning).to_be_visible()
