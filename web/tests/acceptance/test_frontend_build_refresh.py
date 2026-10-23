"""Two real production builds and one long-lived page; synthetic business APIs only."""
import hashlib
import json
import re
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope='session')
def production_builds(tmp_path_factory):
    folder = tmp_path_factory.mktemp('version-builds')
    builds = []
    source=folder/'web-source'
    shutil.copytree(ROOT/'web',source,ignore=shutil.ignore_patterns('node_modules','dist','.vite'))
    (source/'node_modules').symlink_to(ROOT/'web/node_modules',target_is_directory=True)
    for label in ['A', 'B']:
        if label == 'B':
            with (source/'src/main.ts').open('a') as stream:
                stream.write('\n// Synthetic second release changes build inputs.\n')
        source_snapshot = folder / ('source-' + label)
        shutil.copytree(source, source_snapshot, ignore=shutil.ignore_patterns('node_modules', 'dist', '.vite'))
        with (folder / ('build-' + label + '.log')).open('w') as log:
            result = subprocess.run(['npm', 'run', 'build'], cwd=source, stdout=log, stderr=subprocess.STDOUT, timeout=300)
        assert result.returncode == 0, 'Production build failed; inspect owned build log'
        destination = folder / label
        shutil.copytree(source / 'dist', destination)
        manifest = json.loads((destination / 'assets/build-version.json').read_text())
        html = (destination / 'index.html').read_text()
        asset = re.search(r'src="(/assets/[^\"]+\.js)"', html).group(1)
        assert manifest['version'] in (destination / asset.lstrip('/')).read_text()
        builds.append({'root': destination, 'source': source_snapshot, 'manifest': manifest, 'version': manifest['version'], 'asset': asset})
    assert builds[0]['version'] != builds[1]['version']
    return builds


@pytest.fixture
def version_page(browser, production_builds):
    a, b = production_builds
    state = {'root': a['root'], 'mode': 'file', 'documents': 0, 'manifests': [], 'writes': []}
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(state['root']), **kwargs)
        def log_message(self, *_):
            pass
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/':state['documents'] += 1
            if path == '/assets/build-version.json':
                state['manifests'].append(self.path)
                if state['mode'] != 'file':
                    body = b'{"version":"<invalid>"}'
                    self.send_response(503 if state['mode'] == 'unavailable' else 200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers();self.wfile.write(body);return
            super().do_GET()
        def end_headers(self):
            if urlsplit(self.path).path in ('/', '/assets/build-version.json'):
                self.send_header('Cache-Control', 'no-store')
            super().end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True);thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    context = browser.new_context(viewport={'width': 1920, 'height': 1080})
    context.add_init_script("localStorage.setItem('xj_auth_token','synthetic-build-token')")
    page = context.new_page();page.set_default_timeout(10000)
    selected = {'workbench_id': 'qa-wb', 'account_uid': 'qa-account', 'version': 1}
    def route(r):
        req = r.request;path = urlsplit(req.url).path
        if not req.url.startswith(base + '/'):
            r.abort();return
        if not path.startswith('/api/'):
            r.continue_();return
        if req.method != 'GET':
            state['writes'].append((req.method, path))
        data = []
        if path == '/api/auth/me':
            data = {'user_id': 'qa-version-owner', 'display_name': 'Version QA', 'account_uid': 'qa-account', 'binding': selected}
        elif path == '/api/im/catalog':
            data = {'selected': selected, 'workbenches': [{'workbench_id': 'qa-wb', 'stale': False, 'updated_at': 1800000000,
                'accounts': [{'account_uid': 'qa-account', 'account_nick': 'QA', 'stale': False, 'available': True}]}]}
        elif path == '/api/im/links':
            data = {'links': [], 'active_id': None, 'binding_version': 1}
        elif path == '/api/im/mq-config':
            data = {'editable': True, 'configured': True, 'backend': 'file_gateway', 'base_url': 'https://saved.invalid',
                'credentials_set': True, 'revision': 'qa-revision', 'applied': True, **selected}
        r.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'data': data}))
    page.route('**/*', route);page.route_web_socket('**/*', lambda ws: None)
    page.goto(base + '/#/link', wait_until='networkidle')
    expect(page.locator('[name=mq-base-url]')).to_have_value('https://saved.invalid')
    assert state['manifests'], 'Production entry did not start build version checks'
    try:
        yield page, state, a, b
    finally:
        page.unroute_all(behavior='wait');context.close()
        server.shutdown();server.server_close();thread.join(5)
        assert not thread.is_alive()


def focus_check(page):
    with page.expect_response(lambda response: '/assets/build-version.json' in response.url):
        page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_load_state('networkidle')


def test_same_production_version_is_silent_and_uses_uncached_manifest(version_page):
    page, state, a, _ = version_page
    focus_check(page)
    expect(page.get_by_test_id('app-update-banner')).to_have_count(0)
    assert all('_check=' in path for path in state['manifests'])
    assert page.locator('script[type=module]').get_attribute('src') == a['asset']
    assert state['writes'] == []


def test_later_build_only_prompts_without_reload_draft_loss_or_logout(version_page):
    page, state, a, b = version_page
    page.locator('[name=mq-base-url]').fill('https://unsaved.invalid')
    page.locator('[name=mq-client-cert]').fill('/synthetic/private-unsaved-draft.crt')
    documents = state['documents'];state['root'] = b['root'];focus_check(page)
    expect(page.get_by_test_id('app-update-banner')).to_be_visible()
    expect(page.get_by_test_id('app-update-banner')).to_contain_text('未提交')
    expect(page.locator('[name=mq-base-url]')).to_have_value('https://unsaved.invalid')
    expect(page.locator('[name=mq-client-cert]')).to_have_value('/synthetic/private-unsaved-draft.crt')
    assert state['documents'] == documents and state['writes'] == []
    assert page.locator('script[type=module]').get_attribute('src') == a['asset']
    assert page.evaluate("localStorage.getItem('xj_auth_token')") == 'synthetic-build-token'
    assert 'private-unsaved-draft' not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')


def test_dirty_connection_blocks_manual_reload_until_user_clears_draft(version_page):
    page, state, _, b = version_page
    page.locator('[name=mq-client-cert]').fill('/synthetic/user-owned-unsaved.crt')
    documents = state['documents'];state['root'] = b['root'];focus_check(page)
    page.get_by_test_id('app-update-reload').click()
    expect(page.get_by_test_id('app-update-banner')).to_contain_text('已阻止刷新')
    expect(page.locator('[name=mq-client-cert]')).to_have_value('/synthetic/user-owned-unsaved.crt')
    assert state['documents'] == documents and state['writes'] == []


def test_explicit_clean_reload_gets_new_document_bundle_and_clears_update_prompt(version_page):
    page, state, _, b = version_page
    documents = state['documents'];state['root'] = b['root'];focus_check(page)
    expect(page.get_by_test_id('app-update-banner')).to_be_visible()
    with page.expect_navigation(wait_until='networkidle'):
        page.get_by_test_id('app-update-reload').click()
    expect(page.get_by_test_id('app-update-banner')).to_have_count(0)
    assert page.locator('script[type=module]').get_attribute('src') == b['asset']
    assert state['documents'] > documents and state['writes'] == []
    assert page.evaluate("localStorage.getItem('xj_auth_token')") == 'synthetic-build-token'


@pytest.mark.parametrize('mode', ['unavailable', 'invalid'])
def test_bad_manifest_is_not_evidence_of_new_build_or_auth_failure(version_page, mode):
    page, state, a, b = version_page
    state.update(root=b['root'], mode=mode);focus_check(page)
    expect(page.get_by_test_id('app-update-banner')).to_have_count(0)
    assert page.locator('script[type=module]').get_attribute('src') == a['asset']
    assert page.evaluate("localStorage.getItem('xj_auth_token')") == 'synthetic-build-token'
    assert state['writes'] == []


from tests.cutover.test_production_mq_bootstrap import bootstrap, OWNER, BASE, YAML


def test_unbound_user_reuses_server_auth_for_mtls_setup_discovery_selection_and_quote_bar_with_real_backend(bootstrap, browser, frontend_url):
    import socket
    import time
    import uvicorn
    s = bootstrap
    from tests.tls_material import tls_config
    provisioned = s['client'].put('/api/im/mq-config', json={
        'base_url': BASE, 'config_yaml': YAML, 'expected_revision': '', **tls_config()})
    assert provisioned.status_code == 200, provisioned.text
    assert s['app'].state.repository.binding_for(OWNER) is None
    s['runtime'].background_enabled = True
    for wid,uids in [('wb-a',['account-a','account-b']),('wb-b',['account-c','account-d'])]:
        for uid in uids:
            s['seed'](wid, uid)
    s['vendor'].calls.clear()
    listener = socket.socket();listener.bind(('127.0.0.1', 0));listener.listen(128)
    backend = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(s['app'], log_level='error', access_log=False, lifespan='off'))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True);thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    context = browser.new_context(viewport={'width': 1920, 'height': 1080})
    context.add_init_script("localStorage.setItem('xj_auth_token'," + json.dumps(s['token']) + ")")
    page = context.new_page();page.set_default_timeout(10000);calls = []
    def forward(route):
        req = route.request;parsed = urlsplit(req.url)
        if not req.url.startswith(frontend_url + '/'):
            route.abort();return
        if not parsed.path.startswith('/api/'):
            route.continue_();return
        if parsed.path == '/api/negotiation/updates':
            # 本门只核联系人读取；不代理独立长连接，避免同步fetch等待SSE结束。
            route.abort();return
        calls.append((req.method, parsed.path))
        response = route.fetch(url=backend + parsed.path + ('?' + parsed.query if parsed.query else ''))
        route.fulfill(response=response)
    page.route('**/*', forward);page.route_web_socket('**/*', lambda ws: None)
    try:
        page.goto(frontend_url + '/#/link', wait_until='networkidle')
        expect(page.get_by_test_id('mq-config')).to_be_visible()
        expect(page.get_by_test_id('mq-save')).to_be_enabled()
        expect(page.get_by_test_id('collector-current')).to_have_count(0)
        page.locator('[name=mq-base-url]').fill(BASE)
        expect(page.locator('[name=mq-config-yaml]')).to_have_count(0)
        from tests.tls_material import tls_config
        certificate = tls_config()
        page.locator('[name=mq-client-cert]').fill(certificate['client_cert_file'])
        page.locator('[name=mq-client-key]').fill(certificate['client_key_file'])
        page.locator('[name=mq-ca-file]').fill(certificate['ca_file'])
        assert not any(op == 'download' for _, op in s['vendor'].calls)
        page.get_by_test_id('mq-save').click()
        expect(page.locator('[name=mq-config-yaml]')).to_have_count(0)
        expect(page.get_by_test_id('mq-config')).to_contain_text('已保存')
        assert s['app'].state.repository.binding_for(OWNER) is None
        # This user-initiated save now explicitly discovers the directory once.
        # Backend save/test remain pending-only, covered independently.

        expect(page.get_by_test_id('collector-workbench').locator('option')).to_have_count(3)
        page.get_by_test_id('collector-workbench').select_option('wb-b')
        page.get_by_test_id('collector-account').select_option('account-c')
        page.get_by_test_id('collector-connect').click()
        expect(page.get_by_test_id('collector-current')).to_contain_text('wb-b')
        deadline=time.monotonic()+5
        while not s['runtime'].directory.contacts('wb-b','account-c',OWNER)['contacts'] and time.monotonic()<deadline:
            time.sleep(.02)
        page.get_by_test_id('collector-read-saved').click()
        expect(page.get_by_test_id('collector-contacts')).to_contain_text('peer-wb-b-account-c')
        page.goto(frontend_url + '/#/quote')
        bar = page.locator('.im-bar')
        expect(bar.locator('.im-sel')).to_have_value('account-c:oss')
        expect(bar).to_contain_text('wb-b')
        expect(bar.get_by_role('button', name='读取通讯录', exact=True)).to_have_count(0)
        expect(bar.locator('.im-book')).to_have_count(0)
        page.goto(frontend_url + '/#/monitor')
        bar = page.locator('.im-bar')
        expect(bar).to_contain_text('wb-b')
        bar.get_by_role('button', name='读取通讯录', exact=True).click()
        expect(bar.locator('.im-person, .im-book')).to_have_count(0)
        expect(page.get_by_text('已读取 1 人（通讯录）', exact=True)).to_be_visible()
        expect(bar).not_to_contain_text('peer-wb-b-account-c')
        assert [x for x in calls if x[0] != 'GET'] == [
            ('PUT', '/api/im/mq-config'), ('POST', '/api/im/catalog/refresh'), ('POST', '/api/im/binding/select')]
        assert all(path and len(path)<=20 for path,op in s['vendor'].calls)
        assert any(op=='download' for path,op in s['vendor'].calls)
        assert s['app'].state.repository.binding_for(OWNER)['workbench_id'] == 'wb-b'
    finally:
        page.unroute_all(behavior='wait');context.close();server.should_exit=True;thread.join(10);listener.close()
        assert not thread.is_alive()


def test_real_vite_public_logo_is_hashed_and_verified_with_exact_outputs(production_builds):
    from deploy.frontend_build import verify_frontend_build
    for build in production_builds:
        manifest = verify_frontend_build(build['source'], build['root'])
        assert manifest == build['manifest']
        assert (build['root']/'logo.png').read_bytes() == (build['source']/'public/logo.png').read_bytes()
        assert manifest['assets']['logo.png'] == hashlib.sha256((build['root']/'logo.png').read_bytes()).hexdigest()


@pytest.mark.parametrize('mutation', ['modify', 'delete', 'extra'])
def test_real_public_output_tampering_is_rejected(production_builds, tmp_path, mutation):
    from deploy.common import DeployError
    from deploy.frontend_build import verify_frontend_build
    build = production_builds[0]
    dist = tmp_path/'dist'
    shutil.copytree(build['root'], dist)
    if mutation == 'modify':
        (dist/'logo.png').write_bytes(b'synthetic-tampered-logo')
    elif mutation == 'delete':
        (dist/'logo.png').unlink()
    else:
        (dist/'unlisted.png').write_bytes(b'synthetic-extra-output')
    with pytest.raises(DeployError, match='FRONTEND_ASSET_MISMATCH'):
        verify_frontend_build(build['source'], dist)


def test_public_source_change_requires_rebuild_even_with_untouched_dist(production_builds, tmp_path):
    from deploy.common import DeployError
    from deploy.frontend_build import verify_frontend_build
    build = production_builds[0]
    source = tmp_path/'source'
    shutil.copytree(build['source'], source)
    (source/'public/logo.png').write_bytes(b'synthetic-updated-source-logo')
    with pytest.raises(DeployError, match='FRONTEND_SOURCE_MISMATCH'):
        verify_frontend_build(source, build['root'])


def test_old_dist_cannot_pass_new_source_by_replacing_only_version_file(production_builds, tmp_path):
    from deploy.common import DeployError
    from deploy.frontend_build import verify_frontend_build
    a, b = production_builds
    assert a['manifest']['source_hash'] != b['manifest']['source_hash']
    dist = tmp_path/'old-dist'
    shutil.copytree(a['root'], dist)
    with pytest.raises(DeployError, match='FRONTEND_SOURCE_MISMATCH'):
        verify_frontend_build(b['source'], dist)
    shutil.copyfile(b['root']/'assets/build-version.json', dist/'assets/build-version.json')
    with pytest.raises(DeployError, match='FRONTEND_ASSET_MISMATCH'):
        verify_frontend_build(b['source'], dist)
