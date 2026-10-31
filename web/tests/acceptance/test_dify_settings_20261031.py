"""Independent browser contracts for settings and persisted QA navigation.

The real Vue app runs against intercepted synthetic API data. No Dify model,
collector, database, upgrade or financial send is invoked by these checks.
"""
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from playwright.sync_api import expect


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def settings_ui(ui):
    page, base, *_ = ui
    calls = []
    states = {stage: {'stage': stage, 'available': True, 'configured': True, 'app_key_set': True,
                     'base_url': 'https://' + stage + '.synthetic.invalid/v1', 'revision': stage + '-r1', 'dsl_version': '0.6.0'}
              for stage in ('analysis', 'reply')}

    def intercept(route):
        request = route.request
        stage = urlsplit(request.url).path.split('/')[3]
        calls.append({'url': request.url, 'method': request.method, 'authorization': request.headers.get('authorization'),
                      'body': request.post_data_json if request.post_data else None})
        if request.url.endswith('/dsl'):
            route.fulfill(body=(ROOT / 'dify/site_workflows' / (stage + '.yml')).read_bytes(), content_type='application/yaml')
            return
        if request.method == 'PUT':
            body = request.post_data_json
            assert body['expected_revision'] == states[stage]['revision']
            states[stage].update(base_url=body['base_url'], revision=stage + '-r2')
        route.fulfill(json={'ok': True, 'data': states[stage]})

    page.route(base + '/api/dify/**', intercept)
    page.route(base + '/api/im/mq-config', lambda route: route.fulfill(json={'ok': True, 'data': {
        'editable': True, 'configured': False, 'applied': False, 'credentials_set': True,
        'base_url': '', 'revision': '', 'backend': 'file_gateway'}}))
    page.route(base + '/api/qa/workflow', lambda route: route.fulfill(json={'ok': True, 'data': {
        'available': True, 'enabled': True, 'configured': True, 'app_key_set': True,
        'base_url': 'https://qa.synthetic.invalid/v1', 'tool_base_url': 'https://app.synthetic.invalid',
        'revision': 'qa-r1', 'hint': '合成问答配置', 'tool_path': '/api/agent/tools/qa_market/query'}}))
    page.route(base + '/api/qa/workflow/dsl', lambda route: route.fulfill(
        body=(ROOT / 'dify/qa_workflow/workflow.yml').read_bytes(), content_type='application/yaml'))
    return page, base, states, calls


@pytest.mark.parametrize('stage', ['analysis', 'reply'])
def test_stage_draft_survives_navigation_and_save_keeps_key_out_of_browser_storage(settings_ui, stage):
    page, base, states, calls = settings_ui
    page.goto(base + '/#/link')
    page.locator('#settings-nav-' + stage).click()
    panel = page.get_by_test_id('dify-' + stage + '-settings')
    expect(panel).to_be_visible()
    secret = panel.locator('input[type=password]')
    expect(secret).to_have_value('')
    expect(secret).to_have_attribute('placeholder', '已保存，留空保留')
    panel.get_by_label('Dify API 地址').fill('https://unsaved.synthetic.invalid/v1')
    page.locator('#settings-nav-' + ('reply' if stage == 'analysis' else 'analysis')).click()
    page.locator('#settings-nav-' + stage).click()
    expect(panel.get_by_label('Dify API 地址')).to_have_value('https://unsaved.synthetic.invalid/v1')
    assert not [call for call in calls if call['method'] == 'PUT']
    assert panel.get_by_role('button', name='保存配置', exact=True).evaluate('button => button.form !== null')
    panel.get_by_role('button', name='保存配置', exact=True).click()
    expect(panel).to_contain_text('后续流程调用使用新配置')
    writes = [call for call in calls if call['method'] == 'PUT']
    assert len(writes) == 1 and writes[0]['body'] == {
        'base_url': 'https://unsaved.synthetic.invalid/v1', 'app_key': '', 'expected_revision': stage + '-r1'}
    secret.fill('synthetic-browser-entered-key')
    panel.get_by_role('button', name='保存配置', exact=True).click()
    expect(secret).to_have_value('')
    assert 'synthetic-browser-entered-key' not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
    assert all(call['authorization'] == 'Bearer synthetic-qa-token' for call in calls)


@pytest.mark.parametrize('stage,title,filename', [
    ('analysis', '事实解析工作流', 'xunjia-analysis-0.6.0.yml'),
    ('reply', '谈判回复工作流', 'xunjia-reply-0.6.0.yml'),
    ('workflow', '问答工作流', 'xunjia-qa-workflow.yml'),
])
def test_each_dsl_download_is_reachable_from_its_fixed_action_bar(settings_ui, stage, title, filename):
    page, base, _, calls = settings_ui
    page.goto(base + '/#/link')
    page.locator('#settings-nav-' + stage).click()
    panel = page.locator('[data-settings-panel="' + stage + '"]')
    button = panel.get_by_role('button', name='下载' + title, exact=True)
    expect(button).to_be_in_viewport()
    assert button.evaluate('button => button.closest(".settings-actions") !== null')
    with page.expect_download() as download:
        button.click()
    value = download.value
    assert value.suggested_filename == filename
    document = yaml.safe_load(Path(value.path()).read_bytes())
    assert document['version'] == '0.6.0'
    assert document['workflow']['environment_variables'] == []
    assert not [call for call in calls if call['method'] != 'GET']


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 640}])
def test_left_and_right_scroll_independently_with_viewport_fixed_form_actions(settings_ui, viewport):
    page, base, _, _ = settings_ui
    page.set_viewport_size(viewport)
    page.goto(base + '/#/link')
    page.locator('#settings-nav-qt').click()
    expect(page.get_by_test_id('qt-save')).to_be_visible()
    result = page.evaluate('''() => {
      const nav=document.querySelector('.settings-nav'), detail=document.querySelector('.settings-detail');
      const actions=document.querySelector('[data-settings-panel="qt"] .settings-actions');
      const before=actions.getBoundingClientRect(), navBefore=nav.scrollTop;
      detail.scrollTop=detail.scrollHeight;
      const after=actions.getBoundingClientRect(), detailAfter=detail.scrollTop, navAfterDetail=nav.scrollTop;
      nav.scrollTop=nav.scrollHeight;
      return {navOverflow:getComputedStyle(nav).overflowY, detailOverflow:getComputedStyle(detail).overflowY,
        position:getComputedStyle(actions).position, stable:Math.abs(before.y-after.y)<1,
        bottom:after.bottom, height:innerHeight, detailScrolled:detailAfter>0,
        navUnaffected:navBefore===navAfterDetail,
        detailUnaffected:detailAfter===detail.scrollTop, navCanScroll:nav.scrollHeight>nav.clientHeight,
        width:document.documentElement.scrollWidth, viewport:innerWidth,
        padding:parseFloat(getComputedStyle(detail).paddingBottom), actionHeight:after.height,
        formOwned:!!actions.querySelector('[type=submit]').form};
    }''')
    assert result['navOverflow'] == result['detailOverflow'] == 'auto'
    assert result['position'] == 'fixed' and result['stable'] and result['formOwned']
    assert abs(result['bottom'] - result['height']) <= 1
    assert result['detailScrolled'] and result['detailUnaffected'] and result['navUnaffected']
    assert result['padding'] >= result['actionHeight'] and result['width'] <= result['viewport']
    if viewport['width'] == 390:
        assert result['navCanScroll']


def test_expired_login_during_config_read_clears_identity_without_saving(settings_ui):
    page, base, _, calls = settings_ui
    page.goto(base + '/#/link')
    page.locator('#settings-nav-analysis').click()
    panel = page.get_by_test_id('dify-analysis-settings')
    expect(panel).to_contain_text('密钥不回显')
    page.route(base + '/api/dify/analysis/config', lambda route: route.fulfill(status=401, json={'ok': False, 'hint': '合成登录过期'}))
    panel.get_by_role('button', name='重新读取', exact=True).click()
    expect(page.get_by_test_id('xj-login-gate')).to_be_visible()
    assert page.evaluate("localStorage.getItem('xj_auth_token')") is None
    assert not [call for call in calls if call['method'] == 'PUT']


@pytest.fixture
def qa_history_ui(settings_ui):
    page, base, _, _ = settings_ui
    calls = []
    sessions = [{'id': 'session-' + str(index), 'title': '合成会话 ' + str(index), 'created_at': 100,
                 'updated_at': 1000 - index} for index in range(51)]
    behavior = {'detail_failure': None}

    def route(intercept):
        request = intercept.request
        parsed = urlsplit(request.url)
        calls.append({'method': request.method, 'url': request.url, 'authorization': request.headers.get('authorization')})
        assert request.method == 'GET', 'History navigation must not create or replay a question'
        if parsed.path == '/api/qa/sessions':
            before = parse_qs(parsed.query).get('before', [None])[0]
            if before is None:
                data = {'items': sessions[:50], 'has_more': True, 'next_before': 'session-49'}
            else:
                assert before == 'session-49'
                data = {'items': sessions[49:], 'has_more': False, 'next_before': None}
        else:
            sid = parsed.path.rsplit('/', 1)[1]
            if sid == behavior['detail_failure']:
                intercept.fulfill(status=503, json={'ok': False, 'hint': '合成历史读取失败'})
                return
            session = next(item for item in sessions if item['id'] == sid)
            answer = '服务端历史回答 ' + sid
            data = {'session': session, 'messages': [
                {'id': sid + '-user', 'role': 'user', 'text': '历史问题 ' + sid, 'state': 'complete', 'created_at': 100},
                {'id': sid + '-ai', 'role': 'ai', 'text': answer, 'state': 'complete', 'created_at': 101,
                 'result': {'answer': answer, 'candidates': [], 'requires_selection': False}}]}
        intercept.fulfill(json={'ok': True, 'data': deepcopy(data)})

    page.route(base + '/api/qa/sessions**', route)
    return page, base, calls, behavior


def test_qa_history_paginates_deduplicates_and_reload_reads_latest_server_session(qa_history_ui):
    page, base, calls, _ = qa_history_ui
    page.goto(base + '/#/chat')
    expect(page.get_by_text('服务端历史回答 session-0', exact=True)).to_be_visible()
    expect(page.locator('.conv-list .conv')).to_have_count(50)
    page.get_by_role('button', name='加载更早会话', exact=True).click()
    expect(page.locator('.conv-list .conv')).to_have_count(51)
    page.get_by_role('button', name='合成会话 50', exact=True).click()
    expect(page.get_by_text('服务端历史回答 session-50', exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_text('服务端历史回答 session-0', exact=True)).to_be_visible()
    assert sum(call['url'].endswith('/session-0') for call in calls) == 2
    assert any('before=session-49' in call['url'] for call in calls)
    assert all(call['authorization'] == 'Bearer synthetic-qa-token' for call in calls)


def test_failed_history_switch_keeps_original_message_and_active_session(qa_history_ui):
    page, base, _, behavior = qa_history_ui
    page.goto(base + '/#/chat')
    expect(page.get_by_text('服务端历史回答 session-0', exact=True)).to_be_visible()
    behavior['detail_failure'] = 'session-1'
    page.get_by_role('button', name='合成会话 1', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('合成历史读取失败')
    expect(page.get_by_text('服务端历史回答 session-0', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='合成会话 0', exact=True)).to_have_attribute('aria-current', 'page')
    behavior['detail_failure'] = None
    page.get_by_role('button', name='合成会话 1', exact=True).click()
    expect(page.get_by_text('服务端历史回答 session-1', exact=True)).to_be_visible()
