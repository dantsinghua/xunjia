"""Unbound collector UI: real Vue, dedicated Vite, intercepted synthetic APIs only."""
from datetime import datetime, timezone
import json
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


@pytest.fixture
def unbound(ui):
    page, base, *_ = ui
    moment = datetime(2026, 10, 27, 2, 0, 0, tzinfo=timezone.utc)
    page.clock.install(time=moment)
    stamp = int(moment.timestamp())
    state = {'selected': None, 'calls': [], 'catalog_count': 0,
             'workbenches': [{'workbench_id': 'synthetic-wb', 'name': '合成采集工作台',
                              'stale': False, 'updated_at': stamp,
                              'accounts': [{'account_uid': 'synthetic-account', 'account_nick': '合成企点号',
                                            'channel': 'qidian', 'stale': False, 'available': True,
                                            'updated_at': stamp, 'capabilities': ['negotiation-v22-watermark']}]}]}
    def fulfill(route, data):
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'data': data}, ensure_ascii=False))
    def contacts():
        return {**(state['selected'] or {}), 'source': 'its-relay', 'updated_at': stamp, 'stale': False,
                'contacts': [], 'org': {'departments': [], 'members': []}}
    def route(r):
        request = r.request
        path = urlsplit(request.url).path
        state['calls'].append((request.method, path))
        if path == '/api/auth/me':
            fulfill(r, {'user_id': 'synthetic-unbound-user', 'display_name': '独立验收用户',
                        'binding': None, 'account_uid': None})
        elif path.startswith('/api/im/catalog'):
            state['catalog_count'] += 1
            fulfill(r, {'contract': 'its-relay-path-v1', 'offline_seconds': 60,
                        'selected': state['selected'], 'workbenches': state['workbenches']})
        elif path == '/api/im/mq-config':
            fulfill(r, {'editable': True, 'configured': True, 'applied': True, 'backend': 'file_gateway',
                        'base_url': 'https://relay.synthetic.invalid', 'credentials_set': True,
                        'revision': 'synthetic-new-config', 'account_uid': '', 'workbench_id': '', 'note': ''})
        elif path == '/api/im/links':
            fulfill(r, {'active_id': '', 'links': []})
        elif path == '/api/im/binding/select':
            body = request.post_data_json
            state['selected'] = {'workbench_id': body['workbench_id'], 'account_uid': body['account_uid'], 'version': 1}
            fulfill(r, {'binding': state['selected'], 'contacts': contacts(), 'connected': True})
        else:
            fulfill(r, contacts())
    for pattern in ('/api/auth/me', '/api/im/catalog**', '/api/im/mq-config', '/api/im/links',
                    '/api/im/binding/select', '/api/im/contacts**'):
        page.route(base + pattern, route)
    return page, base, state


def open_unbound(unbound):
    page, base, _ = unbound
    page.goto(base + '/#/link')
    page.locator('#settings-nav-account').click()
    panel = page.get_by_test_id('collector-account-settings')
    expect(panel).to_be_visible()
    expect(page.get_by_test_id('collector-workbench').locator('option[value="synthetic-wb"]')).to_have_count(1)
    return page, panel


def test_no_selection_does_not_claim_collector_transport_failure(unbound):
    page, panel = open_unbound(unbound)
    expect(panel).to_contain_text('尚未选择采集账号')
    expect(page.get_by_test_id('collector-workbench')).to_have_value('')
    expect(panel).not_to_contain_text('采集端当前收发状态未确认', timeout=1500)
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    assert not any(method != 'GET' for method, _ in unbound[2]['calls'])


def test_unbound_unsaved_selection_survives_silent_directory_poll(unbound):
    page, _ = open_unbound(unbound)
    page.get_by_test_id('collector-workbench').select_option('synthetic-wb')
    page.get_by_test_id('collector-account').select_option('synthetic-account')
    expect(page.get_by_test_id('collector-connect')).to_be_enabled()
    before = unbound[2]['catalog_count']
    with page.expect_response(lambda response: urlsplit(response.url).path == '/api/im/catalog'):
        page.clock.run_for(6000)
    assert unbound[2]['catalog_count'] > before
    expect(page.get_by_test_id('collector-workbench')).to_have_value('synthetic-wb', timeout=1500)
    expect(page.get_by_test_id('collector-account')).to_have_value('synthetic-account', timeout=1500)
    expect(page.get_by_test_id('collector-connect')).to_be_enabled()
    assert not any(method != 'GET' for method, _ in unbound[2]['calls'])


@pytest.mark.parametrize('condition', ['stale', 'unavailable'])
def test_selected_stale_or_unavailable_account_still_cannot_be_submitted(unbound, condition):
    account = unbound[2]['workbenches'][0]['accounts'][0]
    if condition == 'stale':
        account['stale'] = True
    else:
        account['available'] = False
    page, panel = open_unbound(unbound)
    page.get_by_test_id('collector-workbench').select_option('synthetic-wb')
    page.get_by_test_id('collector-account').select_option('synthetic-account')
    expect(page.get_by_test_id('collector-connect')).to_be_disabled()
    expect(panel).to_contain_text('采集端当前收发状态未确认')
    panel.locator('form').dispatch_event('submit')
    page.clock.run_for(100)
    assert not any(method != 'GET' for method, _ in unbound[2]['calls'])
