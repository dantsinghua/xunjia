"""Unbound production bootstrap gates: actual app/codec/local gateway, vendor HTTP only replaced."""
import base64
import hashlib
import json
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from relay.file_gateway import FileGatewayClient
from relay.protocol import Binding, Codec, envelope
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from tests.cutover import file_gateway_fakes as vendor_fixture

OWNER = 'qa-bootstrap-owner'
HOST = 'bootstrap-vendor.invalid'
BASE = 'https://' + HOST + '/its-relay/v1'
SEAL = b'b' * 32
SIGN = b'd' * 32
YAML = 'auth:\n  key: synthetic-file-gateway\n  sm4_key: 0123456789abcdef\n'


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    for name in ['XUNJIA_TRANSPORT_CONFIG', 'XUNJIA_MQ_CONNECTION_CONFIG', 'XUNJIA_WEB_DIST',
                 'XUNJIA_QUOTE_SOURCE', 'XUNJIA_QUOTE_AUTOSTART', 'XUNJIA_REFERENCE_CONFIG']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('XUNJIA_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('XUNJIA_MQ_ALLOW_HTTP', '0')
    monkeypatch.setenv('XUNJIA_SEAL_KEY', base64.b64encode(SEAL).decode())
    monkeypatch.setenv('XUNJIA_SIGN_KEY', base64.b64encode(SIGN).decode())
    monkeypatch.setattr(vendor_fixture, 'NOW', int(time.time()))
    vendor = vendor_fixture.Gateway()
    real_open = urllib.request.OpenerDirector.open
    def only_vendor_or_loopback(opener, request, *args, **kwargs):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        host = urlsplit(url).hostname
        if host == HOST:
            assert urlsplit(url).path in ['/its-relay/v1/api/files/' + op for op in ['pending', 'upload', 'download']]
            return vendor.open(request, kwargs.get('timeout', 15))
        assert host in ('127.0.0.1', 'localhost', '::1'), 'External network outside synthetic vendor forbidden'
        return real_open(opener, request, *args, **kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', only_vendor_or_loopback)
    database = 'sqlite:///' + str(tmp_path / 'business.db')
    def make_app():
        return create_app(database_url=database, enable_background=False, 
            auth_config=AuthConfig(sso_secret='synthetic-bootstrap-sso', session_secret='q' * 40))
    app = make_app()
    with TestClient(app) as client:
        token = app.state.auth.issue_token(AuthUser(OWNER, OWNER))
        client.headers['Authorization'] = 'Bearer ' + token
        def seed(wid, uid, *, owner=OWNER, updated_at=None):
            now = time.time()
            b = Binding(owner, uid, wid, SEAL, SIGN)
            producer = FileGatewayClient(BASE, vendor_fixture.AUTH, opener=vendor, clock=lambda: vendor_fixture.NOW)
            for kind in ['catalog', 'contacts']:
                payload = {'account_uid': uid, 'workbench_id': wid, 'updated_at': now if updated_at is None else updated_at}
                if kind == 'catalog':
                    payload.update(accounts=[{'account_uid': uid, 'channel': 'qidian'}], capabilities=['negotiation-v22-watermark'])
                else:
                    payload.update(contacts=[{'uin': 'peer-' + wid + '-' + uid, 'name': 'Person ' + wid}], org={})
                blob = Codec(b, clock=lambda: now).pack(envelope(b, kind, payload,
                    message_id=kind + '-' + wid + '-' + uid + '-' + hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:12],
                    created_at=now, expires_at=now + 600))
                producer.upload(kind + '-' + wid + '-' + uid + '.bin', blob)
        yield {'app': app, 'client': client, 'vendor': vendor, 'seed': seed, 'make_app': make_app,
               'token': token, 'tmp': tmp_path, 'monkeypatch': monkeypatch}


def status(s):
    response = s['client'].get('/api/im/mq-config')
    assert response.status_code == 200, response.text
    return response.json()['data']


def save(s, **changes):
    body = {'base_url': BASE, 'config_yaml': YAML, 'expected_revision': status(s)['revision'], **changes}
    return s['client'].put('/api/im/mq-config', json=body)


def discover(s):
    response = s['client'].post('/api/im/catalog/refresh')
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_initial_unbound_owner_can_edit_two_fields_without_private_serve_configuration(bootstrap):
    s = bootstrap
    assert s['app'].state.repository.binding_for(OWNER) is None
    view = status(s)
    assert view['editable'] is True and view['configured'] is False and view['revision'] == ''
    assert not view.get('account_uid') and not view.get('workbench_id')
    assert getattr(s['app'].state, 'managed_mq', None) is not None
    response = s['client'].get('/api/im/catalog')
    assert response.status_code == 200, response.text
    assert response.json()['data']['selected'] is None
    assert response.json()['data']['workbenches'] == []
    assert s['vendor'].calls == []


def test_valid_save_and_pending_test_do_not_dequeue_or_fabricate_an_initial_binding(bootstrap):
    s = bootstrap
    s['seed']('wb-a', 'account-a');s['vendor'].calls.clear()
    response = save(s)
    assert response.status_code == 200, response.text
    view = response.json()['data']
    assert view['saved'] and view['configured'] and view['credentials_set']
    assert s['app'].state.repository.binding_for(OWNER) is None
    assert all(op not in ('upload', 'download') for _, op in s['vendor'].calls)
    response = s['client'].post('/api/im/mq-config/test', json={'base_url': BASE, 'config_yaml': ''})
    assert response.status_code == 200 and response.json()['data']['passed'], response.text
    assert all(op not in ('upload', 'download') for _, op in s['vendor'].calls)
    assert len(s['vendor'].queues[HOST]) == 2
    catalog = s['client'].get('/api/im/catalog').json()['data']
    assert catalog['workbenches'] == [] and catalog['selected'] is None
    text = json.dumps(view)
    assert vendor_fixture.AUTH.key not in text and '0123456789abcdef' not in text


def test_explicit_discovery_real_signed_four_pairs_first_selection_and_restart_persist(bootstrap):
    s = bootstrap
    for wid in ['wb-a', 'wb-b']:
        for uid in ['account-a', 'account-b']:
            s['seed'](wid, uid)
    s['vendor'].calls.clear()
    assert save(s).status_code == 200
    data = discover(s)
    assert data['selected'] is None
    assert {(w['workbench_id'], a['account_uid']) for w in data['workbenches'] for a in w['accounts']} == {
        (w, u) for w in ['wb-a', 'wb-b'] for u in ['account-a', 'account-b']}
    assert len(s['vendor'].queues[HOST]) == 0
    assert sum(op == 'download' for _, op in s['vendor'].calls) == 8
    response = s['client'].post('/api/im/binding/select', json={
        'workbench_id': 'wb-b', 'account_uid': 'account-a', 'expected_version': 0})
    assert response.status_code == 200, response.text
    selected = response.json()['data']['selected']
    assert selected == {'workbench_id': 'wb-b', 'account_uid': 'account-a', 'version': 1}
    book = s['client'].get('/api/im/contacts').json()['data']
    assert book['workbench_id'] == 'wb-b' and book['contacts'][0]['uin'] == 'peer-wb-b-account-a'
    current = next(x for x in s['client'].get('/api/im/links').json()['data']['links'] if x['active'])
    assert current['tested'] and current['workbench_id'] == 'wb-b'
    # Finish the first application's lifespan before restarting its owned gateway.
    s['client'].__exit__(None, None, None)
    with TestClient(s['make_app']()) as reopened:
        reopened.headers['Authorization'] = 'Bearer ' + s['token']
        assert reopened.get('/api/im/catalog').json()['data']['selected'] == selected
        assert reopened.get('/api/im/contacts').json()['data']['contacts'][0]['uin'] == 'peer-wb-b-account-a'
        assert reopened.get('/api/im/mq-config').json()['data']['configured']


def test_initial_blank_credentials_are_rejected_without_a_profile_or_directory(bootstrap):
    s = bootstrap
    response = save(s, config_yaml='')
    assert response.status_code in (400, 422), response.text
    assert s['app'].state.repository.binding_for(OWNER) is None
    assert s['vendor'].calls == []
    assert s['client'].get('/api/im/catalog').json()['data']['workbenches'] == []


def test_duplicate_save_keeps_same_local_gateway_and_no_duplicate_remote_consumer(bootstrap):
    s = bootstrap
    assert save(s).status_code == 200
    first = status(s)
    assert save(s, config_yaml='').status_code == 200
    second = status(s)
    assert first['configured'] and second['configured']
    s['seed']('wb-a', 'account-a');s['vendor'].calls.clear()
    assert len(discover(s)['workbenches']) == 1
    assert sum(op == 'download' for _, op in s['vendor'].calls) == 2
    assert s['client'].get('/api/im/catalog').json()['data']['selected'] is None


def test_other_owner_cannot_claim_same_logical_remote_queue_and_start_second_consumer(bootstrap):
    s = bootstrap
    assert save(s).status_code == 200
    before = list(s['vendor'].calls)
    other = s['app'].state.auth.issue_token(AuthUser('qa-bootstrap-other', 'qa-bootstrap-other'))
    s['client'].headers['Authorization'] = 'Bearer ' + other
    response = save(s)
    assert response.status_code in (400, 403, 409), response.text
    assert s['vendor'].calls == before
    assert s['app'].state.repository.binding_for(OWNER) is None


def test_missing_protocol_keys_still_allows_credentials_and_pending_but_never_decrypts_or_takes_directory(bootstrap):
    base = bootstrap
    base['seed']('wb-real', 'account-real')
    base['monkeypatch'].delenv('XUNJIA_SEAL_KEY', raising=False)
    base['monkeypatch'].delenv('XUNJIA_SIGN_KEY', raising=False)
    app = base['make_app']()
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + base['token']
        s = {**base, 'app': app, 'client': client}
        assert save(s).status_code == 200
        response = client.post('/api/im/mq-config/test', json={'base_url': BASE, 'config_yaml': ''})
        assert response.status_code == 200 and response.json()['data']['passed']
        base['vendor'].calls.clear()
        response = client.post('/api/im/catalog/refresh')
        assert response.status_code in (200, 400, 503), response.text
        body = response.json()
        if response.status_code == 200:
            assert body['data']['workbenches'] == []
            assert body['data'].get('refresh_error') or body['data'].get('note')
        assert any(term in json.dumps(body, ensure_ascii=False) for term in ['密钥', '协议', 'KEY', 'key'])
        assert not any(op == 'download' for _, op in base['vendor'].calls)
        assert len(base['vendor'].queues[HOST]) == 2
        assert app.state.repository.binding_for(OWNER) is None


def test_real_new_publication_updates_selected_contacts_and_stale_badge_without_fake_cache_time(bootstrap):
    s = bootstrap
    old_stamp = time.time() - 600
    s['seed']('wb-a', 'account-a', updated_at=old_stamp)
    assert save(s).status_code == 200
    old = discover(s)
    assert old['workbenches'][0]['accounts'][0]['stale']
    response = s['client'].post('/api/im/binding/select', json={
        'workbench_id': 'wb-a', 'account_uid': 'account-a', 'expected_version': 0})
    assert response.status_code == 200 and response.json()['data']['connected'] is False, response.text
    old_selected = response.json()['data']['selected']
    old_book = s['client'].get('/api/im/contacts').json()['data']
    assert old_book['updated_at'] == old_stamp and old_book['stale']
    # Refreshing an empty queue must not turn an old cached publication into "now".
    still_old = discover(s)
    assert still_old['workbenches'][0]['accounts'][0]['updated_at'] == old_stamp
    assert s['client'].get('/api/im/contacts').json()['data']['updated_at'] == old_stamp
    # A genuinely new authenticated publication can move source freshness.
    s['seed']('wb-a', 'account-a')
    fresh = discover(s)
    account = fresh['workbenches'][0]['accounts'][0]
    assert account['stale'] is False and account['updated_at'] > old_stamp
    book = s['client'].get('/api/im/contacts').json()['data']
    assert book['stale'] is False and book['updated_at'] > old_stamp
    current = next(x for x in s['client'].get('/api/im/links').json()['data']['links'] if x['active'])
    assert current['tested'] and current['catalog_stale'] is False
    assert fresh['selected'] == old_selected
    with s['app'].state.repository.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 0


def test_foreign_owner_publications_never_become_unbound_users_candidates(bootstrap):
    s = bootstrap
    s['seed']('wb-foreign', 'foreign-account', owner='qa-foreign-owner')
    assert save(s).status_code == 200
    response = s['client'].post('/api/im/catalog/refresh')
    assert response.status_code in (200, 400, 503), response.text
    catalog = s['client'].get('/api/im/catalog')
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()['data']['workbenches'] == [] and catalog.json()['data']['selected'] is None
    assert s['app'].state.repository.binding_for(OWNER) is None


def test_saved_credentials_and_profile_references_are_private_and_unchanged_after_reload_reads(bootstrap):
    s = bootstrap
    assert save(s).status_code == 200
    root = s['tmp'] / 'state'
    secrets = [p for p in root.rglob('*') if p.is_file() and p.suffix in ('.yaml', '.yml', '.json')]
    assert secrets
    assert all(p.stat().st_mode & 0o077 == 0 for p in secrets)
    before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in secrets}
    for _ in range(2):
        status(s)
        s['client'].get('/api/im/catalog')
    assert {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in secrets} == before
    assert not any(OWNER in p.parts for p in root.rglob('*'))


def test_standard_runtime_shared_env_enables_unbound_bootstrap_without_private_launcher(bootstrap, monkeypatch):
    from deploy import runtime
    s = bootstrap
    prefix = s['tmp'] / 'installed'
    app_dir = prefix / 'releases' / 'qa' / 'app'
    (app_dir / 'deploy').mkdir(parents=True)
    static = app_dir / 'frontend' / 'dist'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<!doctype html><title>synthetic runtime gate</title>')
    interpreter = app_dir.parent / 'venv' / 'bin' / 'python'
    interpreter.parent.mkdir(parents=True);interpreter.write_text('synthetic path marker, never executed')
    (app_dir / '.deployment.json').write_text(json.dumps({'prefix': str(prefix)}))
    config = prefix / 'config' / 'xunjia.env'
    config.parent.mkdir(parents=True)
    values = {'XUNJIA_DATABASE_URL': 'oceanbase://qa:synthetic-valid-password@db.invalid:2881/xunjia_bootstrap',
        'XUNJIA_SSO_SECRET': 'synthetic-bootstrap-sso', 'XUNJIA_AUTH_SECRET': 'q' * 40,
        'XUNJIA_STATE_DIR': '', 'XUNJIA_TRANSPORT_CONFIG': '', 'XUNJIA_MQ_CONNECTION_CONFIG': '',
        'XUNJIA_RUN_BACKGROUND': '0',
        'XUNJIA_SEAL_KEY': base64.b64encode(SEAL).decode(), 'XUNJIA_SIGN_KEY': base64.b64encode(SIGN).decode()}
    config.write_text('\n'.join(key + '=' + repr(value) for key, value in values.items()) + '\n');config.chmod(0o600)
    monkeypatch.setattr(runtime, '__file__', str(app_dir / 'deploy' / 'runtime.py'))
    monkeypatch.delenv('XUNJIA_CONFIG_FILE', raising=False)
    monkeypatch.setenv('XUNJIA_TRANSPORT_CONFIG', '/parent-export-must-not-be-used')
    monkeypatch.setenv('XUNJIA_MQ_CONNECTION_CONFIG', '/parent-export-must-not-be-used')
    monkeypatch.chdir(s['tmp'])
    captured = {}
    class CapturedExec(Exception):
        pass
    def intercept_exec(path, args, env):
        captured.update(path=path, args=args, env=dict(env))
        raise CapturedExec()
    real_exec = runtime.os.execve
    monkeypatch.setattr(runtime.os, 'execve', intercept_exec)
    with pytest.raises(CapturedExec):
        runtime.main()
    monkeypatch.setattr(runtime.os, 'execve', real_exec)
    env = captured['env']
    assert env['XUNJIA_STATE_DIR'] == str(prefix / 'state')
    assert env['XUNJIA_TRANSPORT_CONFIG'] == env['XUNJIA_MQ_CONNECTION_CONFIG'] == ''
    assert captured['args'][1:5] == ['-m', 'uvicorn', 'server.app:create_app', '--factory']
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Same real application factory that the standard launcher would exec;
    # only the explicitly supported DB DI uses temporary SQLite instead of a real vendor DB.
    app = create_app(database_url='sqlite:///' + str(prefix / 'business.db'),
        auth_config=AuthConfig(sso_secret='synthetic-bootstrap-sso', session_secret='q' * 40),
        enable_background=False)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + app.state.auth.issue_token(AuthUser(OWNER, OWNER))
        response = client.get('/api/im/mq-config')
        assert response.status_code == 200, response.text
        assert response.json()['data']['editable'] and not response.json()['data']['configured']
        assert getattr(app.state, 'managed_mq', None) is not None


def test_unbound_links_projection_is_empty_and_never_a_fixed_placeholder_account(bootstrap):
    response = bootstrap['client'].get('/api/im/links')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['links'] == [] and not data.get('active_id')
    assert bootstrap['app'].state.repository.binding_for(OWNER) is None


def test_http_vendor_requires_explicit_shared_deployment_opt_in(bootstrap):
    s = bootstrap
    response = save(s, base_url=BASE.replace('https:', 'http:'))
    assert response.status_code in (400, 422), response.text
    assert not status(s)['configured']
    assert s['vendor'].calls == []
    s['monkeypatch'].setenv('XUNJIA_MQ_ALLOW_HTTP', '1')
    response = save(s, base_url=BASE.replace('https:', 'http:'))
    assert response.status_code == 200, response.text
    assert response.json()['data']['configured']
    assert not any(op in ('upload', 'download') for _, op in s['vendor'].calls)


def test_managed_local_gateway_has_independent_private_auth_not_vendor_credentials(bootstrap):
    from relay.file_gateway import GatewayAuth, FileGatewayError
    s = bootstrap
    assert save(s).status_code == 200
    paths = s['app'].state.managed_mq.paths(OWNER)
    local = GatewayAuth.from_file(paths.local_auth)
    config = json.loads(paths.connection.read_text())
    remote_path = paths.connection.parent / config['auth_file']
    remote = GatewayAuth.from_file(remote_path)
    assert local.key != remote.key and local.sm4_key != remote.sm4_key
    assert remote.key == vendor_fixture.AUTH.key and remote.sm4_key == vendor_fixture.AUTH.sm4_key
    assert paths.local_auth.stat().st_mode & 0o077 == 0
    assert remote_path.stat().st_mode & 0o077 == 0
    before = list(s['vendor'].calls)
    with pytest.raises(FileGatewayError) as denied:
        FileGatewayClient(config['uplink_url'], remote, allow_http=True).pending()
    assert denied.value.status == 401 and s['vendor'].calls == before
    assert local.key not in json.dumps(status(s)) and remote.key not in json.dumps(status(s))


def test_html_and_unhashed_build_manifest_are_not_cacheable_on_real_app_entry(bootstrap, monkeypatch):
    s = bootstrap
    dist = s['tmp'] / 'frontend'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<!doctype html><title>cache contract</title>')
    (dist / 'assets' / 'build-version.json').write_text('{"version":"qa-build"}')
    monkeypatch.setenv('XUNJIA_WEB_DIST', str(dist))
    with TestClient(s['make_app']()) as client:
        for path in ['/', '/quote', '/link', '/assets/build-version.json']:
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'no-store' in response.headers.get('cache-control', ''), path


def wait_for_preserved_downloads(s, count, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sum(op == 'download' for _, op in s['vendor'].calls) >= count:
            return
        time.sleep(.05)
    pytest.fail('Existing coordinator service loop did not preserve the newly signed publication')


def test_explicit_subscription_follows_real_new_packets_without_intake_or_manual_book_read(bootstrap):
    s = bootstrap
    old_stamp = time.time() - 600
    s['seed']('wb-a', 'account-a', updated_at=old_stamp)
    assert save(s).status_code == 200
    journal = s['app'].state.managed_mq._running[OWNER][2]
    with journal.db() as db:
        assert not db.execute('SELECT enabled FROM directory_subscription WHERE singleton=1').fetchone()
    discover(s)
    response = s['client'].post('/api/im/binding/select', json={
        'workbench_id': 'wb-a', 'account_uid': 'account-a', 'expected_version': 0})
    assert response.status_code == 200 and not response.json()['data']['connected']
    adapter = s['app'].state.transport.adapters[(OWNER, 'account-a', 'oss')]
    with adapter.store.db() as db:
        seq = db.execute("INSERT INTO fg_download(scope,state,name,blob,code) VALUES(?,'UNKNOWN','old',NULL,'FILE_GATEWAY_HTTP_ERROR')", (adapter.store.scope,)).lastrowid
        original = tuple(db.execute('SELECT * FROM fg_download WHERE seq=?', (seq,)).fetchone())
    s['seed']('wb-a', 'account-a')
    # No refresh, contacts GET, or second selection is issued before the badge check.
    wait_for_preserved_downloads(s, 4)
    link = next(x for x in s['client'].get('/api/im/links').json()['data']['links'] if x['active'])
    assert link['tested'] and link['catalog_stale'] is False
    catalog = s['client'].get('/api/im/catalog').json()['data']
    assert catalog['workbenches'][0]['accounts'][0]['updated_at'] > old_stamp
    assert s['client'].get('/api/im/contacts').json()['data']['updated_at'] > old_stamp
    with journal.db() as db:
        assert db.execute("SELECT COUNT(*) FROM receive WHERE state='STORED'").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM receive WHERE state='DELIVERED'").fetchone()[0] == 0
    with adapter.store.db() as db:
        assert tuple(db.execute('SELECT * FROM fg_download WHERE seq=?', (seq,)).fetchone()) == original
    assert len(s['app'].state.managed_mq._running) == 1


def test_explicit_subscription_survives_service_restart_without_another_refresh(bootstrap):
    s = bootstrap
    s['seed']('wb-a', 'account-a');assert save(s).status_code == 200;discover(s)
    s['client'].__exit__(None, None, None)
    app = s['make_app']()
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + s['token']
        s['vendor'].calls.clear();s['seed']('wb-b', 'account-b')
        wait_for_preserved_downloads(s, 2)
        result = client.get('/api/im/catalog')
        assert result.status_code == 200
        assert {w['workbench_id'] for w in result.json()['data']['workbenches']} == {'wb-a', 'wb-b'}
        assert result.json()['data']['selected'] is None
        journal = app.state.managed_mq._running[OWNER][2]
        with journal.db() as db:
            assert db.execute('SELECT enabled FROM directory_subscription WHERE singleton=1').fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM receive WHERE state='DELIVERED'").fetchone()[0] == 0


def test_subscription_preserves_old_unknown_and_continues_new_authenticated_directory(bootstrap):
    s = bootstrap
    assert save(s).status_code == 200
    journal = s['app'].state.managed_mq._running[OWNER][2]
    with journal.db() as db:
        seq = db.execute("INSERT INTO receive(state,h,name,body) VALUES('UNKNOWN',NULL,'old-uncertain',NULL)").lastrowid
        original = tuple(db.execute('SELECT * FROM receive WHERE id=?', (seq,)).fetchone())
    s['seed']('wb-a', 'account-a');s['vendor'].calls.clear()
    response = s['client'].post('/api/im/catalog/refresh')
    assert response.status_code == 200, response.text
    assert not response.json()['data'].get('refresh_error')
    assert response.json()['data']['workbenches'][0]['workbench_id']=='wb-a'
    for _ in range(2):
        cached = s['client'].get('/api/im/catalog').json()['data']
        assert not cached.get('refresh_error')
        assert cached['workbenches'][0]['workbench_id']=='wb-a'
    assert any(operation=='download' for _,operation in s['vendor'].calls)
    with journal.db() as db:
        assert tuple(db.execute('SELECT * FROM receive WHERE id=?', (seq,)).fetchone()) == original
