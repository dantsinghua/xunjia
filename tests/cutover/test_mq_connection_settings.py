"""Independent two-field MQ settings gates; actual app, synthetic pending only."""
import base64
import hashlib
import inspect
import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
import httpx
import pytest

from relay.file_gateway import FileGatewayClient, GatewayAuth
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.negotiation.application_store import ApplicationStore

OWNER = 'synthetic-mq-owner'
ACCOUNT = 'synthetic-mq-account'
WORKBENCH = 'synthetic-mq-workbench'
KEY = 'synthetic-mq-remote-key'
SM4 = '0123456789abcdef'
YAML = 'auth:\n  key: synthetic-new-remote-key\n  sm4_key: fedcba9876543210\n'


def coordinator_module(monkeypatch):
    from relay import file_gateway_coordinator as module
    assert Path(module.__file__).resolve().parent == Path(inspect.getfile(FileGatewayClient)).resolve().parent
    return module


@pytest.fixture
def mq(tmp_path, monkeypatch):
    database = 'sqlite:///' + str(tmp_path / 'state.db')
    store = ApplicationStore(database, initialize=True)
    store.bind_account(OWNER, ACCOUNT, WORKBENCH)
    store.bind_account('synthetic-other-owner', 'synthetic-other-account', 'synthetic-other-wb')
    monkeypatch.setenv('QA_MQ_SEAL', base64.b64encode(b's' * 32).decode())
    monkeypatch.setenv('QA_MQ_SIGN', base64.b64encode(b'h' * 32).decode())
    local_auth = tmp_path / 'local-auth.json'
    local_auth.write_text(json.dumps({'auth': {'key': 'synthetic-local-only-key', 'sm4_key': SM4}}))
    local_auth.chmod(0o600)
    module = coordinator_module(monkeypatch)
    from relay.file_gateway_journal import Journal
    journal = Journal(tmp_path / 'owned-journal.db', None)
    auth = GatewayAuth.from_file(local_auth)
    connection = tmp_path / 'mq-connection.json'
    server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
    base = 'http://127.0.0.1:' + str(server.server_port)
    profiles = [{'owner_user_id': OWNER, 'account_uid': ACCOUNT, 'workbench_id': WORKBENCH,
                 'seal_key_env': 'QA_MQ_SEAL', 'sign_key_env': 'QA_MQ_SIGN',
                 'file_gateway': {'uplink_url': base + '/up', 'downlink_url': base + '/down',
                                  'auth_file': local_auth.name, 'allow_http': True}}]
    transport = tmp_path / 'transport.json'
    transport.write_text(json.dumps(profiles));transport.chmod(0o600)
    credentials = tmp_path / 'upstream-auth.yaml'
    credentials.write_text('auth:\n  key: ' + KEY + '\n  sm4_key: ' + SM4 + '\n')
    credentials.chmod(0o600)
    connection.write_text(json.dumps({'owner_user_id': OWNER, 'account_uid': ACCOUNT,
        'workbench_id': WORKBENCH, 'base_url': 'https://mq.synthetic.invalid/its-relay/v1',
        'auth_file': credentials.name, 'hostname': 'mq.synthetic.invalid',
        'addresses': ['192.0.2.11'], 'allow_http': False,
        'uplink_url': base + '/up', 'downlink_url': base + '/down'}))
    connection.chmod(0o600)
    server.RequestHandlerClass = module.make_handler(journal, auth, connection)
    monkeypatch.setenv('XUNJIA_TRANSPORT_CONFIG', str(transport))
    monkeypatch.setenv('XUNJIA_STATE_DIR', str(tmp_path / 'transport-state'))
    monkeypatch.setenv('XUNJIA_MQ_CONNECTION_CONFIG', str(connection))
    original_request = FileGatewayClient._request
    def local_only(self, operation, *args, **kwargs):
        if self.base_url not in (base + '/up', base + '/down') or operation != 'pending':
            pytest.fail('Only owned local pending requests are allowed; remote I/O is forbidden')
        return original_request(self, operation, *args, **kwargs)
    monkeypatch.setattr(FileGatewayClient, '_request', local_only)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        app = create_app(database_url=database, enable_background=False, 
                         auth_config=AuthConfig(sso_secret='synthetic-only', session_secret='m' * 40))
        with TestClient(app) as client:
            client.headers['Authorization'] = 'Bearer ' + app.state.auth.issue_token(AuthUser(OWNER, OWNER))
            yield app, client, connection, transport, local_auth, credentials, (journal, base, auth, module, server)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        assert not thread.is_alive()


def status(client):
    response = client.get('/api/im/mq-config')
    assert response.status_code == 200, response.text
    return response.json()['data']


def body(connection, **changes):
    return {'base_url': 'https://new-mq.synthetic.invalid/its-relay/v1',
            'config_yaml': YAML,
            'expected_revision': hashlib.sha256(connection.read_bytes()).hexdigest(), **changes}


def files(path):
    return {str(p.relative_to(path.parent)): p.read_bytes() for p in path.parent.rglob('*')
            if p.is_file() and 'transport-state' not in p.relative_to(path.parent).parts
            and not p.name.startswith('owned-journal.db')
            and not p.name.startswith('state.db')}


@pytest.mark.parametrize('method,suffix', [('GET', ''), ('PUT', ''), ('POST', '/test')])
def test_api_requires_authentication(mq, method, suffix):
    _, client, connection, *_ = mq
    response = client.request(method, '/api/im/mq-config' + suffix,
                              headers={'Authorization': ''}, json=body(connection) if method != 'GET' else None)
    assert response.status_code == 401


def test_two_field_status_hides_credential_and_identity_is_fixed(mq):
    _, client, connection, *_ = mq
    view = status(client)
    assert view['editable'] and view['configured'] and view['backend'] == 'file_gateway'
    assert view['base_url'] == 'https://mq.synthetic.invalid/its-relay/v1'
    assert view['credentials_set'] and view['account_uid'] == ACCOUNT and view['workbench_id'] == WORKBENCH
    assert view['revision'] == hashlib.sha256(connection.read_bytes()).hexdigest()
    assert not {'config_yaml', 'auth_key', 'sm4_key', 'token', 'bucket', 'sts_url'} & view.keys()
    assert KEY not in json.dumps(view) and SM4 not in json.dumps(view)


def test_save_only_updates_upstream_config_not_local_router_or_identity(mq):
    app, client, connection, transport, local_auth, *_ = mq
    old_profile, old_local = transport.read_bytes(), local_auth.read_bytes()
    key = (OWNER, ACCOUNT, 'oss')
    adapter = app.state.transport.adapters[key]
    binding = app.state.repository.binding_for(OWNER)
    request = body(connection)
    response = client.put('/api/im/mq-config', json=request)
    assert response.status_code == 200, response.text
    assert transport.read_bytes() == old_profile and local_auth.read_bytes() == old_local
    assert app.state.transport.adapters[key] is adapter
    assert app.state.repository.binding_for(OWNER) == binding and app.state.enable_background is False
    saved = json.loads(connection.read_text())
    assert saved['base_url'] == 'https://new-mq.synthetic.invalid/its-relay/v1'
    assert saved['owner_user_id'] == OWNER and saved['account_uid'] == ACCOUNT and saved['workbench_id'] == WORKBENCH
    assert saved.get('hostname') != 'mq.synthetic.invalid' and not saved.get('addresses')
    auth_path = Path(saved['auth_file'])
    if not auth_path.is_absolute(): auth_path = connection.parent / auth_path
    assert auth_path.resolve().is_relative_to(connection.parent.resolve())
    assert auth_path.stat().st_mode & 0o777 == 0o600 and connection.stat().st_mode & 0o777 == 0o600
    auth = GatewayAuth.from_file(auth_path)
    assert auth.key == 'synthetic-new-remote-key' and auth.sm4_key == b'fedcba9876543210'
    assert 'synthetic-new-remote-key' not in connection.read_text() and 'fedcba9876543210' not in response.text
    assert status(client)['revision'] != request['expected_revision']


def test_empty_yaml_retains_existing_credentials_and_same_host_dns_mapping(mq):
    _, client, connection, _, _, credentials, *_ = mq
    old = credentials.read_bytes()
    response = client.put('/api/im/mq-config', json=body(connection,
        base_url='https://mq.synthetic.invalid/new-prefix', config_yaml=''))
    assert response.status_code == 200, response.text
    saved = json.loads(connection.read_text())
    auth_path = Path(saved['auth_file'])
    if not auth_path.is_absolute(): auth_path = connection.parent / auth_path
    auth = GatewayAuth.from_file(auth_path)
    assert auth.key == KEY and auth.sm4_key == SM4.encode() and credentials.read_bytes() == old
    assert saved['hostname'] == 'mq.synthetic.invalid' and saved['addresses'] == ['192.0.2.11']


@pytest.mark.parametrize('change', [
    {'config_yaml': 'auth: [broken'},
    {'config_yaml': 'auth:\n  key: synthetic-secret\n  key: duplicate\n  sm4_key: 0123456789abcdef\n'},
    {'config_yaml': 'auth:\n  key: synthetic-secret\n  sm4_key: short\n'},
    {'config_yaml': 'auth:\n  key: synthetic-secret\n  sm4_key: ' + '短' * 16 + '\n'},
    {'config_yaml': True}, {'base_url': 'https://user:private@mq.synthetic.invalid'},
    {'base_url': 'http://mq.synthetic.invalid/its-relay/v1'},
    {'base_url': 'https://mq.synthetic.invalid/api/files'},
    {'expected_revision': 'stale'}, {'account_uid': 'synthetic-stolen-account'},
    {'owner_user_id': 'synthetic-other-owner'}, {'workbench_id': 'synthetic-other-wb'},
])
def test_invalid_config_or_revision_is_rejected_without_partial_writes(mq, change):
    _, client, connection, *_ = mq
    old = files(connection)
    response = client.put('/api/im/mq-config', json=body(connection, **change))
    assert response.status_code in (400, 403, 409, 422), response.text
    assert files(connection) == old
    assert 'synthetic-secret' not in response.text and 'private@' not in response.text


def test_other_bound_owner_cannot_change_the_single_active_mq_connection(mq):
    app, client, connection, *_ = mq
    token = app.state.auth.issue_token(AuthUser('synthetic-other-owner', 'synthetic-other-owner'))
    old = files(connection)
    response = client.put('/api/im/mq-config', headers={'Authorization': 'Bearer ' + token}, json=body(connection))
    assert response.status_code in (400, 403), response.text
    assert files(connection) == old


@pytest.fixture
def pending_only(monkeypatch):
    calls = []
    def pending(self):
        calls.append((self.base_url, self.auth.key, self.auth.sm4_key))
        return 7
    def forbidden(*_, **__): pytest.fail('Connection settings must never upload/download')
    monkeypatch.setattr(FileGatewayClient, 'pending', pending)
    monkeypatch.setattr(FileGatewayClient, 'upload', forbidden)
    monkeypatch.setattr(FileGatewayClient, 'download', forbidden)
    return calls


def test_test_pending_uses_unsaved_form_but_never_persists_or_swaps_router(mq, pending_only):
    app, client, connection, *_ = mq
    adapter = app.state.transport.adapters[(OWNER, ACCOUNT, 'oss')]
    old = files(connection)
    response = client.post('/api/im/mq-config/test', json=body(connection))
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['passed'] and data['connected'] and data['saved'] is False
    assert data['steps'] and all(step['ok'] for step in data['steps'])
    assert pending_only == [('https://new-mq.synthetic.invalid/its-relay/v1', 'synthetic-new-remote-key', b'fedcba9876543210')]
    assert files(connection) == old and app.state.transport.adapters[(OWNER, ACCOUNT, 'oss')] is adapter
    assert 'synthetic-new-remote-key' not in response.text and 'fedcba9876543210' not in response.text


def test_failed_config_commit_does_not_leave_unreferenced_new_credentials(mq, monkeypatch):
    from server import mq_settings
    _, client, connection, *_ = mq
    before = files(connection)
    original = mq_settings.atomic_write
    def write(path, data):
        if path == connection: raise OSError('synthetic config commit failure')
        return original(path, data)
    monkeypatch.setattr(mq_settings, 'atomic_write', write)
    with pytest.raises(OSError, match='synthetic config commit failure'):
        client.put('/api/im/mq-config', json=body(connection))
    assert files(connection) == before


@pytest.mark.parametrize('document', [
    'key: synthetic-flat-key\nsm4_key: 0123456789abcdef\n',
    'auth:\n  key: synthetic-nested-key\n  sm4_key: 0123456789abcdef\n',
])
def test_yaml_parser_supports_flat_and_auth_root_without_secret_echo(document):
    auth = GatewayAuth.from_yaml(document)
    assert auth.key in ('synthetic-flat-key', 'synthetic-nested-key') and auth.sm4_key == SM4.encode()


@pytest.mark.parametrize('document', [
    'auth:\n  key: synthetic-never-echo\n  sm4_key: short\n',
    'key: synthetic-never-echo\nkey: duplicate\nsm4_key: 0123456789abcdef\n',
    'auth:\n  key: synthetic-never-echo\n  sm4_key: [sensitive-broken\n',
    'x' * 16385,
])
def test_yaml_parser_rejects_invalid_documents_with_fixed_safe_errors(document):
    from relay.file_gateway import FileGatewayError
    with pytest.raises(FileGatewayError) as caught: GatewayAuth.from_yaml(document)
    assert caught.value.code.startswith('FILE_AUTH_')
    assert 'synthetic-never-echo' not in str(caught.value) and 'sensitive-broken' not in str(caught.value)


@pytest.mark.parametrize('allowed', [False, True])
def test_shared_loader_requires_both_explicit_http_permissions(mq, allowed):
    from relay.file_gateway_connection import load_connection
    from relay.file_gateway import FileGatewayError
    _, _, connection, *_ = mq
    config = json.loads(connection.read_text())
    config.update(base_url='http://mq.synthetic.invalid/its-relay/v1', allow_http=allowed)
    connection.write_text(json.dumps(config))
    if not allowed:
        with pytest.raises(FileGatewayError): load_connection(connection, allow_http=True)
    else:
        assert load_connection(connection, allow_http=True).base_url == config['base_url']
        with pytest.raises(FileGatewayError): load_connection(connection, allow_http=False)


@pytest.fixture
def coordinator(mq, monkeypatch):
    """Observe the real owned Journal and HTTP handler; never call a consumer."""
    journal, base, auth, module, _ = mq[6]
    def forbidden(*_, **__): pytest.fail('No queue upload or download is allowed')
    monkeypatch.setattr(journal, 'upload', forbidden)
    monkeypatch.setattr(journal, 'download', forbidden)
    calls = []
    loader = module.load_connection
    def load(*args, **kwargs):
        calls.append(1)
        return loader(*args, **kwargs)
    monkeypatch.setattr(module, 'load_connection', load)
    yield journal, base, auth, calls


def local_pending(base, auth):
    with httpx.Client(trust_env=False, timeout=10) as client:
        return client.get(base + '/up/api/files/pending', headers={'access_token': auth.token()})


def test_local_coordinator_reloads_remote_snapshot_but_keeps_local_authentication(mq, coordinator):
    _, client, connection, *_ = mq
    journal, base, local_auth, loads = coordinator
    assert local_pending(base, local_auth).status_code == 200
    old_remote = journal.upstream
    assert (journal.upstream.base_url,journal.upstream.auth.key,journal.upstream.auth.sm4_key) == ('https://mq.synthetic.invalid/its-relay/v1', KEY, SM4.encode())
    assert client.put('/api/im/mq-config', json=body(connection)).status_code == 200
    assert local_pending(base, local_auth).status_code == 200
    assert journal.upstream is not old_remote
    assert (journal.upstream.base_url,journal.upstream.auth.key,journal.upstream.auth.sm4_key) == ('https://new-mq.synthetic.invalid/its-relay/v1', 'synthetic-new-remote-key', b'fedcba9876543210')
    count = len(loads)
    wrong_local = GatewayAuth('synthetic-new-remote-key', b'fedcba9876543210')
    assert local_pending(base, wrong_local).status_code == 401
    assert len(loads) == count


def test_operation_waits_for_journal_lock_and_uses_one_new_snapshot(mq, coordinator):
    _, _, connection, *_ = mq
    journal, base, local_auth, _ = coordinator
    server=mq[6][4];original=server.RequestHandlerClass;started=threading.Event()
    class Observed(original):
        def do_GET(self):
            started.set()
            return super().do_GET()
    server.RequestHandlerClass=Observed
    previous=journal.upstream
    with ThreadPoolExecutor(max_workers=1) as pool:
        with journal.lock:
            config = json.loads(connection.read_text())
            config['base_url'] = 'https://new-mq.synthetic.invalid/its-relay/v1'
            connection.write_text(json.dumps(config))
            first=pool.submit(local_pending,base,local_auth)
            assert started.wait(5)
            assert journal.upstream is previous
            assert not first.done()
        assert first.result(timeout=10).status_code == 200
    assert journal.upstream.base_url == 'https://new-mq.synthetic.invalid/its-relay/v1'
    applied=journal.upstream
    assert local_pending(base, local_auth).status_code == 200
    assert journal.upstream is applied


@pytest.mark.parametrize('invalid', ['{', '[]'])
def test_invalid_hot_config_returns_503_without_mutating_journal(mq, coordinator, invalid):
    _, _, connection, *_ = mq
    journal, base, local_auth, _ = coordinator
    assert local_pending(base, local_auth).status_code == 200
    previous, snapshot = journal.upstream, journal.stats()
    connection.write_text(invalid)
    response = local_pending(base, local_auth)
    assert response.status_code == 503
    assert journal.upstream is previous and journal.stats() == snapshot


def test_config_pointer_not_used_by_active_handler_cannot_be_edited(mq):
    _, client, connection, *_ = mq
    journal, _, auth, module, server = mq[6]
    other = connection.with_name('other-connection.json')
    other.write_bytes(connection.read_bytes());other.chmod(0o600)
    server.RequestHandlerClass = module.make_handler(journal, auth, other)
    old = files(connection)
    view = status(client)
    assert view['editable'] is False and view['applied'] is False
    denied = client.put('/api/im/mq-config', json=body(connection))
    assert denied.status_code in (400, 403) and files(connection) == old


def test_handler_without_hotload_metadata_cannot_claim_editable_or_applied(mq):
    _, client, connection, *_ = mq
    journal, _, auth, _, server = mq[6]
    from relay.file_gateway_coordinator import valid
    class Legacy(BaseHTTPRequestHandler):
        """The original pending-only response contract, with real local auth."""
        def log_message(self,*_):pass
        def do_GET(self):
            good=valid(self.headers.get('access_token',''),auth)
            payload=json.dumps({'pending':0}).encode() if good else b'{}'
            self.send_response(200 if good else 401)
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server.RequestHandlerClass = Legacy
    view = status(client)
    assert not view['editable'] and not view['applied']
    before = files(connection)
    assert client.put('/api/im/mq-config', json=body(connection)).status_code in (400, 403)
    assert files(connection) == before


def test_config_local_routes_must_match_the_bound_adapter(mq):
    _, client, connection, *_ = mq
    config = json.loads(connection.read_text());config['uplink_url'] += '-wrong'
    connection.write_text(json.dumps(config))
    assert status(client)['editable'] is False
    before = files(connection)
    assert client.put('/api/im/mq-config', json=body(connection)).status_code in (400, 403)
    assert files(connection) == before


@pytest.mark.parametrize('change_key', [False, True])
def test_existing_unknown_rejects_switch_to_a_different_queue_identity(mq, change_key):
    _, client, connection, *_ = mq
    journal, *_ = mq[6]
    with journal.db() as db:
        db.execute('INSERT INTO upload VALUES(?,?,?,?,?,?)', ('synthetic-historical-unknown', 'up', 'old.bin', '', 'UNKNOWN', 0))
    before = files(connection)
    request = body(connection)
    if change_key: request['base_url'] = json.loads(connection.read_text())['base_url']
    response = client.put('/api/im/mq-config', json=request)
    assert response.status_code in (400, 409), response.text
    assert files(connection) == before
    with journal.db() as db:
        assert db.execute('SELECT state FROM upload WHERE h=?', ('synthetic-historical-unknown',)).fetchone()[0] == 'UNKNOWN'


def test_same_business_key_sm4_rotation_preserves_unknown_and_reports_actual_revision(mq):
    _, client, connection, *_ = mq
    journal, *_ = mq[6]
    with journal.db() as db:
        db.execute('INSERT INTO upload VALUES(?,?,?,?,?,?)', ('synthetic-historical-unknown', 'up', 'old.bin', '', 'UNKNOWN', 0))
    request = body(connection, base_url=json.loads(connection.read_text())['base_url'],
                   config_yaml='auth:\n  key: ' + KEY + '\n  sm4_key: fedcba9876543210\n')
    response = client.put('/api/im/mq-config', json=request)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['saved'] and data['applied']
    assert journal.upstream.auth.key == KEY and journal.upstream.auth.sm4_key == b'fedcba9876543210'
    with journal.db() as db:
        assert db.execute('SELECT state FROM upload WHERE h=?', ('synthetic-historical-unknown',)).fetchone()[0] == 'UNKNOWN'
    assert data['revision'] == hashlib.sha256(connection.read_bytes()).hexdigest()


def test_saved_but_failed_hotload_is_reported_accurately(mq, monkeypatch):
    _, client, connection, *_ = mq
    journal, _, _, module, _ = mq[6]
    original = module.load_connection
    previous = journal.upstream
    def fail_new(path, *args, **kwargs):
        if json.loads(Path(path).read_text())['base_url'].startswith('https://new-mq.'):
            raise ValueError('synthetic-sensitive-hotload-error')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(module, 'load_connection', fail_new)
    response = client.put('/api/im/mq-config', json=body(connection))
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['saved'] is True and data['applied'] is False
    assert '保存' in data['note'] and 'synthetic-sensitive-hotload-error' not in response.text
    assert json.loads(connection.read_text())['base_url'].startswith('https://new-mq.')
    assert journal.upstream is previous


def test_old_oss_settings_api_is_retired(mq):
    app, client, *_ = mq
    assert not any(route.path.startswith('/api/im/oss-config') for route in app.routes if hasattr(route, 'path'))
    assert client.get('/api/im/oss-config').status_code == 404


def test_receive_unknown_does_not_hide_config_or_block_same_key_sm4_rotation(mq):
    _,client,connection,*_=mq
    journal,*_=mq[6]
    with journal.db() as db:db.execute("INSERT INTO receive(state) VALUES('UNKNOWN')")
    assert status(client)['editable']
    response=client.put('/api/im/mq-config',json=body(connection,
        base_url=json.loads(connection.read_text())['base_url'],
        config_yaml='auth:\n  key: '+KEY+'\n  sm4_key: fedcba9876543210\n'))
    assert response.status_code==200 and response.json()['data']['applied'],response.text
    with journal.db() as db:
        assert db.execute('SELECT state FROM receive').fetchone()[0]=='UNKNOWN'
    assert not hasattr(journal,'ready')
    assert journal.pending('up',diagnostic=True)==0


def test_old_nonempty_journal_without_identity_refuses_automatic_adoption(mq):
    _,_,connection,*_=mq
    journal,_,auth,module,_=mq[6]
    with journal.db() as db:
        db.execute('INSERT INTO upload VALUES(?,?,?,?,?,?)',('synthetic-unattributed','up','old.bin','','UNKNOWN',0))
        db.execute('DELETE FROM connection_identity')
    # A restart has no cached metadata: reproduce the original persistent state.
    del journal._mq_connection_meta
    from relay.file_gateway_journal import QueueStop
    with pytest.raises(QueueStop,match='IDENTITY_REQUIRED'):
        module.make_handler(journal,auth,connection)
    with journal.db() as db:
        assert not db.execute('SELECT * FROM connection_identity').fetchone()
        assert db.execute('SELECT state FROM upload').fetchone()[0]=='UNKNOWN'


def test_disagreeing_up_down_metadata_never_reports_applied(mq):
    _,client,connection,*_=mq
    server=mq[6][4];original=server.RequestHandlerClass
    class Disagreeing(original):
        def answer(self,code,body=b'',typ='application/json',name=None):
            if code==200 and self.path.startswith('/down/'):
                value=json.loads(body);value['connection_revision']='0'*64
                body=json.dumps(value).encode()
            return super().answer(code,body,typ,name)
    server.RequestHandlerClass=Disagreeing
    view=status(client)
    assert view['applied'] is False and view['editable'] is False
    before=files(connection)
    assert client.put('/api/im/mq-config',json=body(connection)).status_code in (400,403)
    assert files(connection)==before
