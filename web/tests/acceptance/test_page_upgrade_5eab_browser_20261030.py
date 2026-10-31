"""Immutable field 5eab JS in Chromium, isolated HTTP/WS and upgrade peers.

The mocked gateway never executes a package, systemd, database or network call.
Final package metadata supplies the target build when delivery inputs are set.
"""
import hashlib
import json
import os
import zipfile

import pytest
from playwright.sync_api import expect

from test_subpath_deployment import subpath_site
from tests.acceptance.test_page_upgrade_5eab_20261030 import FIELD_BUILD, FIELD_ROOT, field_source_verified, final_inputs, expected_baselines


@pytest.fixture(scope='session')
def subpath_dist():
    dist = FIELD_ROOT / 'frontend/dist'
    metadata = json.loads((dist / 'assets/build-version.json').read_text())
    before = {str(path.relative_to(dist)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in dist.rglob('*') if path.is_file()}
    for name, digest in metadata['assets'].items():
        field_source_verified('frontend/dist/' + name)
        assert before[name] == digest
    yield dist
    after = {str(path.relative_to(dist)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in dist.rglob('*') if path.is_file()}
    assert before == after, 'Field frontend bundle bytes must remain immutable'


@pytest.fixture
def field_upgrade(browser, subpath_site):
    site = subpath_site
    target_build = 'b' * 64
    if os.environ.get('QA_FINAL_UPGRADE_INPUTS') or os.environ.get('XUNJIA_FINAL_UPGRADE_PACKAGE'):
        package, _, _ = final_inputs()
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read('manifest.json'))
        assert set(manifest['base_builds']) == set(expected_baselines())
        assert FIELD_BUILD in manifest['base_builds'], 'This immutable browser gate exercises the 5eab installation'
        target_build = manifest['target_build']
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    context.add_init_script("localStorage.setItem('xj_auth_token','synthetic-field-token')")
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(site['origin'] + '/') else route.abort())
    page = context.new_page(); page.clock.install()
    page.route_web_socket('**/*', lambda websocket: None)
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    status = {'enabled': True, 'can_upgrade': True, 'retention_days': 30,
              'current_build': FIELD_BUILD, 'job': None}
    behavior = {'get': 'ok', 'post': 'ok', 'held': []}
    calls = []
    endpoint = site['url'] + 'api/system/upgrade'

    def job(state):
        return {'id': 'field-entry-job', 'state': state, 'message': '合成任务 ' + state,
                'target_build': target_build, 'created_at': 1_800_000_000, 'updated_at': 1_800_000_001}

    def route(intercept):
        request = intercept.request
        calls.append({'method': request.method, 'url': request.url,
                      'authorization': request.headers.get('authorization'),
                      'body': request.post_data_json if request.post_data else None})
        action = behavior['post' if request.method == 'POST' else 'get']
        if action == 'lost':intercept.abort('connectionreset'); return
        if action == 'hold':behavior['held'].append(intercept); return
        if action == 'unauthorized':intercept.fulfill(status=401, json={'ok': False, 'hint': 'synthetic expired'}); return
        if request.method == 'POST':status['job'] = job('queued')
        intercept.fulfill(json={'ok': True, 'data': status})

    page.route(endpoint, route)
    try:
        yield {'page': page, 'site': site, 'status': status, 'behavior': behavior,
               'calls': calls, 'job': job, 'target': target_build, 'errors': errors}
    finally:
        context.close()
        assert errors == [], errors


def open_panel(case):
    page = case['page']; page.goto(case['site']['url'] + '#/link')
    panel = page.get_by_test_id('upgrade-sync-settings')
    expect(panel).to_be_visible(); expect(panel).to_contain_text(FIELD_BUILD)
    return page, panel, page.get_by_test_id('upgrade-sync')


def posts(case):
    return [call for call in case['calls'] if call['method'] == 'POST']


def test_field_page_auth_fixed_route_and_single_empty_post(field_upgrade):
    case = field_upgrade; case['behavior']['post'] = 'hold'
    page, panel, button = open_panel(case)
    expect(button).to_be_enabled(); assert posts(case) == []
    with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/api/system/upgrade')):
        button.evaluate('(button) => {button.click();button.click()}')
    expect(button).to_be_disabled(); assert len(posts(case)) == 1
    assert posts(case)[0]['body'] == {}
    case['status']['job'] = case['job']('queued')
    case['behavior']['held'].pop().fulfill(json={'ok': True, 'data': case['status']})
    expect(button).to_have_text('升级进行中…')
    page.clock.run_for(10001)
    assert len(posts(case)) == 1
    assert all(call['authorization'] == 'Bearer synthetic-field-token' for call in case['calls'])
    assert all(call['url'] == case['site']['url'] + 'api/system/upgrade' for call in case['calls'])
    assert not any(path == '/api/system/upgrade' for _, path in case['site']['requests'])


def test_field_page_disconnect_recovers_success_without_post_replay(field_upgrade):
    case = field_upgrade; page, panel, button = open_panel(case)
    button.click(); expect(button).to_have_text('升级进行中…')
    case['behavior']['get'] = 'lost'; page.clock.run_for(5001)
    expect(panel).to_contain_text('等待服务恢复'); expect(button).to_be_disabled()
    case['behavior']['get'] = 'ok'
    case['status'].update(job=case['job']('succeeded'), current_build=case['target'])
    page.clock.run_for(5001)
    expect(panel).to_contain_text(case['target']); expect(panel).to_contain_text('合成任务 succeeded')
    expect(button).to_be_enabled(); assert len(posts(case)) == 1


def test_field_page_old_renderer_accepts_new_rollback_detail_and_message(field_upgrade):
    case = field_upgrade; page, panel, button = open_panel(case)
    button.click(); expect(button).to_have_text('升级进行中…')
    failed = case['job']('failed')
    failed.update(message='升级失败，已回退旧版；旧版服务健康检查通过',
                  detail={'phase': 'rollback_check', 'rollback': 'restored', 'health': 'ready',
                          'future_field': {'opaque': True}})
    case['status']['job'] = failed
    page.clock.run_for(5001)
    expect(panel).to_contain_text(failed['message']); expect(panel).to_contain_text(FIELD_BUILD)
    expect(button).to_be_enabled(); assert len(posts(case)) == 1


def test_field_page_lost_post_remains_fenced_after_reload(field_upgrade):
    case = field_upgrade; case['behavior']['post'] = 'lost'
    page, panel, button = open_panel(case)
    button.click(); expect(panel).to_contain_text('已暂停再次同步')
    page.reload(); expect(panel).to_contain_text('已暂停再次同步')
    expect(button).to_be_disabled(); page.clock.run_for(10001)
    assert len(posts(case)) == 1
    case['status']['job'] = case['job']('queued'); page.clock.run_for(5001)
    expect(button).to_have_text('升级进行中…'); assert len(posts(case)) == 1


def test_field_page_expired_login_does_not_submit(field_upgrade):
    case = field_upgrade; case['behavior']['get'] = 'unauthorized'
    page = case['page']; page.goto(case['site']['url'] + '#/link')
    page.wait_for_function("localStorage.getItem('xj_auth_token') === null")
    page.clock.run_for(10001)
    assert posts(case) == []
