"""Production assets at / and /xunjia/, with isolated synthetic HTTP/WS peers.

No gateway, database or message transport is contacted. The subpath server owns
only /xunjia/; root /api, /ws and /assets deliberately belong to a second app.
"""
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = Path('/home/anlin/.local/state/xunjia-subpath-20261027')


@pytest.fixture(scope='session')
def subpath_dist(tmp_path_factory):
    override = os.environ.get('XUNJIA_SUBPATH_DIST')
    if override:
        return Path(override)
    source = tmp_path_factory.mktemp('subpath-build') / 'web'
    shutil.copytree(ROOT / 'web', source, ignore=shutil.ignore_patterns('node_modules', 'dist', '.vite'))
    (source / 'node_modules').symlink_to(ROOT / 'web/node_modules', target_is_directory=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / 'independent-build.log').open('w') as log:
        result = subprocess.run(['npm', 'run', 'build'], cwd=source, stdout=log, stderr=subprocess.STDOUT, timeout=300)
    assert result.returncode == 0
    return source / 'dist'


@pytest.fixture(params=['/', '/xunjia/'])
def subpath_site(request, subpath_dist):
    prefix = request.param
    state = {'requests': [], 'writes': [], 'ws': [], 'ws_messages': [], 'version': None, 'stop': False}
    selected = {'workbench_id': 'synthetic-wb', 'account_uid': '910001', 'version': 1}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def response(self, body, status=200, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            state['requests'].append((self.command, path))
            if prefix != '/' and not path.startswith(prefix):
                self.response({'owner': 'synthetic-nanyin', 'path': path}, content_type='application/json')
                return
            relative = '/' + path[len(prefix):]
            if relative == '/ws/quote':
                state['ws'].append(path)
                key = self.headers.get('Sec-WebSocket-Key', '')
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', accept)
                self.end_headers()
                self.connection.settimeout(.25)
                def exact(count):
                    data = b''
                    while len(data) < count:
                        part = self.rfile.read(count-len(data))
                        if not part:
                            raise EOFError
                        data += part
                    return data
                try:
                    while not state['stop']:
                        first, second = exact(2)
                        size = second & 127
                        if size == 126: size = struct.unpack('!H', exact(2))[0]
                        if size == 127: size = struct.unpack('!Q', exact(8))[0]
                        assert size < 4096
                        mask = exact(4) if second & 128 else b''
                        raw = exact(size)
                        if mask: raw = bytes(v ^ mask[i % 4] for i, v in enumerate(raw))
                        if first & 15 == 8: break
                        if first & 15 != 1: continue
                        message = json.loads(raw)
                        state['ws_messages'].append(message)
                        if message.get('op') == 'auth':
                            payload = b'{"op":"auth","ok":true}'
                            self.wfile.write(bytes([129, len(payload)]) + payload)
                            self.wfile.flush()
                except (OSError, EOFError):
                    pass
                return
            if relative.startswith('/api/'):
                body = None
                if self.command != 'GET':
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                    state['writes'].append((relative, body))
                data = []
                if relative == '/api/auth/sso-config':
                    self.response({'ok': True, 'dev_login': False}); return
                if relative == '/api/auth/sso':
                    self.response({'ok': True, 'token': 'synthetic-subpath-token'}); return
                if relative == '/api/auth/me':
                    data = {'user_id': 'synthetic-owner', 'display_name': '子路径验收', 'account_uid': '910001', 'binding': selected}
                elif relative == '/api/im/links':
                    data = {'active_id': 'synthetic-link', 'links': [{**selected, 'id': 'synthetic-link', 'mode': 'oss', 'active': True,
                        'tested': True, 'catalog_stale': False, 'contacts': [{'uin': '910100', 'name': '合成收件人'}], 'form': {'oss': selected}}]}
                elif relative == '/api/im/catalog':
                    data = {'selected': selected, 'workbenches': [{'workbench_id': 'synthetic-wb', 'stale': False, 'updated_at': time.time(),
                        'accounts': [{'account_uid': '910001', 'account_nick': '合成账号', 'available': True, 'stale': False}]}]}
                elif relative == '/api/im/contacts':
                    data = {**selected, 'contacts': [{'uin': '910100', 'name': '合成收件人'}], 'source': 'contacts', 'org': {'departments': [], 'members': []}}
                elif relative == '/api/monitor/summary': data = {'bot': {'status': '正常'}, 'stats': []}
                elif relative == '/api/monitor/counterparties': data = [{'id': '910100', 'name': '合成对手甲', 'bonds': []}]
                elif relative == '/api/monitor/chat': data = [{'id': 'synthetic-message', 'role': 'peer', 'text': '合成历史', 'time': '10:00:00'}]
                elif relative == '/api/monitor/send': data = {'cmd_id': 'synthetic-subpath-command', 'status': 'queued'}
                elif relative.startswith('/api/im/receipts/'): data = {'status': 'queued'}
                elif relative == '/api/im/mq-config': data = {'editable': True, 'configured': True, 'backend': 'file_gateway', 'credentials_set': True, 'revision': 'synthetic', 'applied': True}
                elif relative in ('/api/im/rfq/status', '/api/im/inquiry/floor'): data = {'items': []}
                self.response({'ok': True, 'data': data, 'total': 0, 'hasMore': False, 'nextBefore': 0}); return
            if relative == '/assets/build-version.json' and state['version']:
                self.response({'version': state['version']}); return
            if relative == '/blank.html':
                self.response(b'<html><body>synthetic isolated worker harness</body></html>', content_type='text/html'); return
            file = subpath_dist / (relative.lstrip('/') or 'index.html')
            if not file.is_file() or not file.resolve().is_relative_to(subpath_dist.resolve()):
                self.response({'missing': relative}, 404); return
            self.response(file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    state.update(origin=origin, prefix=prefix, url=origin+prefix)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / 'servers.jsonl').open('a') as log:
        log.write(json.dumps({'port': server.server_port, 'prefix': prefix, 'dist': str(subpath_dist), 'server': 'isolated Python ThreadingHTTPServer backend'})+'\n')
    try:
        yield state
    finally:
        state['stop'] = True
        server.shutdown(); server.server_close(); thread.join(5)
        assert not thread.is_alive()
        with (EVIDENCE/'http-traces.jsonl').open('a') as evidence:
            evidence.write(json.dumps({'prefix': prefix, 'port': server.server_port, 'requests': state['requests'],
                'write_paths': [path for path, _ in state['writes']], 'ws_paths': state['ws'],
                'ws_ops': [message.get('op') for message in state['ws_messages']], 'owned_server_stopped': True})+'\n')


def context_for(browser, site):
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    context.add_init_script("Object.defineProperty(globalThis,'SharedWorker',{value:undefined,configurable:true})")
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(site['origin'] + '/') else route.abort())
    page = context.new_page(); page.set_default_timeout(7000)
    return context, page


def test_production_html_assets_stay_inside_deployment_prefix(subpath_site):
    site = subpath_site
    with urlopen(site['url'], timeout=3) as response: html = response.read().decode()
    from urllib.parse import urljoin
    assets = re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', html)
    assert assets
    for asset in assets:
        url = urljoin(site['url'], asset)
        assert urlsplit(url).path.startswith(site['prefix'] + 'assets/'), asset
        with urlopen(url, timeout=3) as response:
            assert 'javascript' in response.headers['Content-Type'] or 'css' in response.headers['Content-Type']


def test_dedicated_worker_uses_prefixed_quote_socket_and_static_api(browser, subpath_site, subpath_dist):
    site = subpath_site
    worker = next((subpath_dist / 'assets').glob('quoteWorker-*.js'))
    context, page = context_for(browser, site)
    try:
        page.goto(site['url'] + 'blank.html')
        page.evaluate("""url => {
          window.workerMessages=[]; window.testWorker=new Worker(url,{type:'module'});
          testWorker.onmessage=e=>workerMessages.push(e.data);
          testWorker.postMessage({type:'INIT_CONNECTIONS',identifiers:{authToken:'synthetic-only-token',userScope:'synthetic-owner',clientId:'qa'}});
        }""", site['url'] + 'assets/' + worker.name)
        page.wait_for_function("workerMessages.some(m=>m.type==='STAT')")
        page.wait_for_timeout(300)
        assert site['prefix'] + 'ws/quote' in site['ws'], site['requests']
        assert ('GET', site['prefix'] + 'api/bond-static/all') in site['requests']
        assert any(m.get('op') == 'auth' for m in site['ws_messages'])
        assert not [p for _, p in site['requests'] if site['prefix'] != '/' and p.startswith(('/api/', '/ws/', '/assets/'))]
    finally:
        context.close()


def test_login_directory_send_version_sso_and_coexisting_routes(browser, subpath_site):
    site = subpath_site
    context, page = context_for(browser, site)
    try:
        page.goto(site['url'] + '#/monitor')
        expect(page.get_by_test_id('xj-login-gate')).to_be_visible()
        assert ('GET', site['prefix']+'api/auth/sso-config') in site['requests']
        page.goto(site['url'] + '?userName=synthetic-owner&nickName=QA&ts=1800000000&sign=synthetic-proof#/monitor')
        expect(page.locator('.cp-panel')).to_contain_text('合成对手甲')
        assert urlsplit(page.url).path == site['prefix']
        assert 'sign=' not in page.url and 'userName=' not in page.url
        assert page.url.endswith('#/monitor')
        expect(page.locator('img.logo')).to_have_attribute('src', site['prefix']+'logo.png')
        page.locator('.send-input').fill('合成子路径发送，不是真实IM')
        page.locator('.send-btn').click()
        expect(page.locator('.send-btn')).to_have_text('已提交')
        assert any(path == '/api/monitor/send' for path, _ in site['writes'])
        page.goto(site['url'] + '#/link')
        expect(page.get_by_test_id('collector-current')).to_contain_text('910001')
        for _ in range(30):
            if ('GET', site['prefix']+'api/im/catalog') in site['requests']: break
            page.wait_for_timeout(100)
        assert ('GET', site['prefix']+'api/im/catalog') in site['requests']
        site['version'] = 'synthetic-later-build'
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.get_by_test_id('app-update-banner')).to_be_visible()
        assert ('GET', site['prefix']+'assets/build-version.json') in site['requests']
        if site['prefix'] != '/':
            assert not [p for _, p in site['requests'] if p.startswith(('/api/', '/ws/', '/assets/'))]
            for path in ('/api/synthetic-nanyin', '/ws/synthetic-nanyin', '/assets/synthetic-nanyin.js'):
                with urlopen(site['origin']+path, timeout=3) as response:
                    assert json.load(response) == {'owner': 'synthetic-nanyin', 'path': path}
    finally:
        context.close()


@pytest.fixture
def nginx_site(subpath_site, tmp_path):
    """Real nginx strips /xunjia/ before the root-mounted synthetic application."""
    binary = EVIDENCE / 'tools/root/usr/sbin/nginx'
    assert binary.is_file(), 'Explicit isolated nginx binary is required for this acceptance'
    assert subpath_site['prefix'] == '/'
    run = tmp_path / 'nginx'; run.mkdir()
    for name in ('body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'):
        (run / name).mkdir()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    source = EVIDENCE / 'tools/xunjia-locations.conf'
    locations = source.read_text().replace('http://127.0.0.1:18770/', subpath_site['origin'] + '/')
    config = f'''pid {run}/nginx.pid;
error_log {run}/error.log;
worker_processes 1;
events {{ worker_connections 64; }}
http {{
  access_log {run}/access.log;
  client_body_temp_path {run}/body;
  proxy_temp_path {run}/proxy;
  fastcgi_temp_path {run}/fastcgi;
  uwsgi_temp_path {run}/uwsgi;
  scgi_temp_path {run}/scgi;
  include {EVIDENCE}/tools/xunjia-http-map.conf;
  server {{
    listen 127.0.0.1:{port};
    {locations}
    location / {{ default_type application/json; return 200 '{{"owner":"synthetic-nanyin","path":"$uri"}}'; }}
  }}
}}
'''
    (run / 'nginx.conf').write_text(config)
    command = [str(binary), '-p', str(run)+'/', '-c', str(run/'nginx.conf'), '-e', str(run/'error.log'), '-g', 'daemon off;']
    log = (run/'stdout.log').open('w')
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    origin = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            assert process.poll() is None, (run/'stdout.log').read_text()
            try:
                with urlopen(origin+'/', timeout=.25) as response:
                    assert json.load(response)['owner'] == 'synthetic-nanyin'
                break
            except OSError: time.sleep(.05)
        else: pytest.fail('isolated nginx did not start')
        with (EVIDENCE/'nginx-runs.jsonl').open('a') as evidence:
            evidence.write(json.dumps({'command': command, 'port': port, 'config': str(run/'nginx.conf'), 'logs': str(run)})+'\n')
        yield {'origin': origin, 'url': origin+'/xunjia/', 'prefix': '/xunjia/', 'backend': subpath_site, 'run': run}
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        with (EVIDENCE/'nginx-runs.jsonl').open('a') as evidence:
            evidence.write(json.dumps({'port': port, 'exit': process.returncode, 'owned_process_stopped': True})+'\n')


@pytest.mark.parametrize('subpath_site', ['/'], indirect=True)
def test_real_nginx_subpath_browser_flow_and_south_bank_routes(browser, nginx_site):
    site = nginx_site
    context, page = context_for(browser, site)
    workers = []
    page.on('worker', lambda worker: workers.append(worker.url))
    try:
        # Exact /xunjia redirect must preserve all portal arguments before JS
        # consumes only its own authentication fields.
        page.goto(site['origin']+'/xunjia?keep=1&userName=synthetic-owner&nickName=QA&ts=1800000000&sign=synthetic-proof#/monitor')
        expect(page.locator('.cp-panel')).to_contain_text('合成对手甲')
        assert urlsplit(page.url).path == '/xunjia/'
        assert urlsplit(page.url).query == 'keep=1'
        assert page.url.endswith('#/monitor')
        page.locator('.send-input').fill('nginx 隔离合成发送')
        page.locator('.send-btn').click()
        expect(page.locator('.send-btn')).to_have_text('已提交')
        page.goto(site['url']+'#/link')
        expect(page.get_by_test_id('collector-current')).to_contain_text('910001')
        page.goto(site['url']+'#/quote')
        page.wait_for_function("document.querySelector('.shell') !== null")
        for _ in range(40):
            if site['backend']['ws_messages'] and ('GET','/api/bond-static/all') in site['backend']['requests']: break
            page.wait_for_timeout(100)
        assert workers and all('/xunjia/assets/quoteWorker-' in url for url in workers), workers
        assert '/ws/quote' in site['backend']['ws']
        assert any(m.get('op') == 'auth' for m in site['backend']['ws_messages'])
        assert ('GET','/api/bond-static/all') in site['backend']['requests']
        assert any(p == '/api/monitor/send' for p, _ in site['backend']['writes'])
        site['backend']['version'] = 'nginx-synthetic-new-version'
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.get_by_test_id('app-update-banner')).to_be_visible()
        context.close()
        for _ in range(60):
            access = (site['run']/'access.log').read_text()
            if 'GET /xunjia/ws/quote' in access: break
            time.sleep(.05)
        assert 'GET /xunjia/assets/build-version.json' in access
        assert 'GET /xunjia/api/bond-static/all' in access
        assert '"GET /xunjia/ws/quote HTTP/1.1" 101' in access
        assert 'GET /api/' not in access and 'POST /api/' not in access
        assert 'GET /assets/' not in access and 'GET /ws/' not in access
        for path in ('/api/synthetic-nanyin', '/ws/synthetic-nanyin', '/assets/synthetic-nanyin.js'):
            with urlopen(site['origin']+path, timeout=3) as response:
                assert json.load(response) == {'owner':'synthetic-nanyin', 'path':path}
    finally:
        context.close()
