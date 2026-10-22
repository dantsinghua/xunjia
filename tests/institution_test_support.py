"""Offline institution harness: real runtime/protocol/HTTP, synthetic vendor HTTP only."""
from collections import defaultdict
from contextlib import ExitStack
import json
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit, parse_qs
from email import policy
from email.parser import BytesParser
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.tls_material import tls_config
from relay.file_gateway import FileGatewayClient
from relay.gateway_paths import CONTRACT, DIRECTORY_PATH, ORGANIZATION_PATH, InstitutionKeys, account_paths
from relay.protocol import Codec, envelope
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.institution_transport import InstitutionTransport
from server.quote_control import QuoteControl
from tests.cutover.file_gateway_fakes import AUTH, Response, decode_token


class Paths:
    """Vendor HTTP seam; queue identity includes the explicit built-in path."""
    def __init__(self):
        self.queues = defaultdict(list)
        self.calls = []
        self.now = time.time()
        self.lose_download = False
        self.accepted_auth=[AUTH]

    def client(self, path):
        return FileGatewayClient(BASE, AUTH, path=path, opener=self, clock=lambda: self.now)

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        path = parse_qs(url.query)['path'][0]
        operation = url.path.rsplit('/', 1)[-1]
        headers = {k.lower(): v for k, v in request.header_items()}
        authorized=False
        for candidate in self.accepted_auth:
            try:authorized=decode_token(headers['accesstoken'],candidate)['key']==candidate.key
            except (ValueError,KeyError):continue
            if authorized:break
        if not authorized:return Response(401,b'{}')
        self.calls.append((path, operation))
        queue = self.queues[path]
        if operation == 'pending':
            return Response(200, json.dumps({'pending': len(queue)}).encode())
        if operation == 'upload':
            message = BytesParser(policy=policy.default).parsebytes(
                ('Content-Type: ' + headers['content-type'] + '\r\n\r\n').encode() + request.data)
            part = next(message.iter_parts())
            assert part.get_param('name', header='content-disposition') == 'file'
            name = part.get_filename()
            queue.append((name, part.get_payload(decode=True)))
            return Response(200, json.dumps({'file_key': str(uuid.uuid4()), 'file_name': name}).encode())
        assert operation == 'download'
        if not queue:
            return Response(204)
        name, raw = queue.pop(0)
        if self.lose_download:
            raise TimeoutError('synthetic consumed file')
        return Response(200, raw, {'Content-Type': 'application/octet-stream',
                                   'Content-Disposition': 'attachment; filename="' + name + '"'})

OWNER = 'qa-directory-owner'
BASE = 'https://relay.invalid'
YAML = 'auth:\n  key: ' + AUTH.key + '\n  sm4_key: ' + AUTH.sm4_key.decode() + '\n'


def build_harness(tmp_path, monkeypatch, *, configured=True, selected=True):
    root = Path(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    clock = [time.time()]
    wire = Paths()
    wire.now = clock[0]
    config = root / 'institution.json'
    auth_file = root / 'institution-auth.yaml'
    if configured:
        auth_file.write_text(YAML)
        auth_file.chmod(0o600)
        config.write_text(json.dumps({'contract': CONTRACT, 'base_url': BASE,
                                     'auth_file': auth_file.name, **tls_config()}))
        config.chmod(0o600)
    for name in ('XUNJIA_TRANSPORT_CONFIG', 'XUNJIA_MQ_CONNECTION_CONFIG',
                 'XUNJIA_WEB_DIST', 'XUNJIA_QUOTE_SOURCE', 'XUNJIA_REFERENCE_CONFIG',
                 'XUNJIA_DIFY_URL', 'XUNJIA_AGGREGATION_PROFILE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('XUNJIA_STATE_DIR', str(root / 'state'))
    monkeypatch.setenv('XUNJIA_INSTITUTION_CONFIG', str(config))
    monkeypatch.setenv('XUNJIA_MQ_ALLOW_HTTP', '0')

    def only_synthetic(opener, request, *args, **kwargs):
        url = urlsplit(request.full_url)
        assert url.hostname and url.hostname.endswith('.invalid'), 'Only synthetic vendor HTTP allowed'
        assert url.path.endswith(('/api/files/pending', '/api/files/upload', '/api/files/download'))
        return wire.open(request, kwargs.get('timeout', 15))
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', only_synthetic)

    database = 'sqlite:///' + str(root / 'business.db')
    def make_app():
        monkeypatch.setenv('XUNJIA_STATE_DIR', str(root / 'state'))
        monkeypatch.setenv('XUNJIA_INSTITUTION_CONFIG', str(config))
        # Business contract fixtures inject admission only; real worker/queue
        # rejection and signed context activation are tested separately.
        app = create_app(database_url=database, enable_background=False, submission_guard=lambda _owner: None,
                         quote_control=QuoteControl(directory=root / 'quote'),
                         auth_config=AuthConfig(sso_secret='synthetic-institution-sso',
                                                session_secret='s' * 40, clock=lambda: clock[0]))
        if app.state.institution.configured:
            app.state.institution.restore()
        return app

    app = make_app()
    stack = ExitStack()
    api = stack.enter_context(TestClient(app))
    bearer = app.state.auth.issue_token(AuthUser(OWNER, OWNER))
    api.headers['Authorization'] = 'Bearer ' + bearer
    keys = InstitutionKeys.from_auth(AUTH)
    s = {'app': app, 'api': api, 'repository': app.state.repository,
         'runtime': app.state.institution, 'wire': wire, 'vendor': wire,
         'tmp': root, 'now': clock[0], 'clock': clock, 'keys': keys, 'auth': AUTH,
         'base': BASE, 'connection': config, 'profiles': config, 'auth_file': auth_file,
         'make_app': make_app, 'bearer': bearer, 'token': bearer,
         'client': api, 'stack': stack, 'monkeypatch': monkeypatch}
    revisions = defaultdict(int)
    accounts = defaultdict(dict)

    def upload(wb, uid, kind, payload, path, stamp):
        binding = keys.binding(wb, uid)
        data = envelope(binding, kind, payload,
                        message_id='fixture-' + kind + '-' + wb + '-' + uid + '-' + str(payload['revision']),
                        created_at=stamp, expires_at=stamp + 86400)
        wire.client(path).upload(wb + '-' + uid + '-' + kind + '.bin',
                                 Codec(binding, clock=lambda: clock[0]).pack(data))
        return data

    def publish(wid, uid, kind, *, extra=None, expired=False, capabilities=True, online=True,
                updated_at=None, owner=None):
        extra = dict(extra or {})
        stamp = updated_at if updated_at is not None else clock[0] - (61 if expired else 0)
        if kind == 'catalog':
            revisions[('directory', wid)] += 1
            accounts[wid][uid] = {'account_id': 'native-' + uid, 'account_uid': uid,
                'channel': 'qidian', 'online': online, 'paths': account_paths(wid, uid),
                'capabilities': extra.get('capabilities', ['negotiation-v22-watermark', 'negotiation-v22-session-reset'] if capabilities else [])}
            return upload(wid, 'directory', 'manifest', {'contract': CONTRACT, 'op': 'directory',
                'revision': revisions[('directory', wid)], 'updated_at': stamp,
                'workbench_name': 'Collector ' + wid, 'accounts': list(accounts[wid].values())}, DIRECTORY_PATH, stamp)
        revisions[('friends', wid, uid)] += 1
        data = upload(wid, uid, 'contacts', {'contract': CONTRACT, 'op': 'friends',
            'revision': revisions[('friends', wid, uid)], 'updated_at': stamp,
            'contacts': extra.get('contacts', [{'uin': 'peer-' + wid + '-' + uid,
                                               'name': 'Person ' + wid}])}, account_paths(wid, uid)['friends_path'], stamp)
        if 'org_state' in extra:
            revisions['organization'] += 1
            upload(wid, 'organization', 'manifest', {'contract': CONTRACT, 'op': 'organization',
                'revision': revisions['organization'], 'updated_at': extra.get('org_updated_at') or stamp,
                'state': extra['org_state'], 'org': extra.get('org', {})}, ORGANIZATION_PATH, stamp)
        return data

    s['publish'] = publish
    s['seed'] = lambda wid, uid, **kw: (publish(wid, uid, 'catalog', **kw), publish(wid, uid, 'contacts', **kw))
    def refresh():
        runtime = s['app'].state.institution
        runtime.tick()
        for wb, rows in accounts.items():
            for uid in rows:
                runtime.directory.consume(runtime.client(account_paths(wb, uid)['friends_path']))
    s['refresh'] = refresh
    s['advance'] = lambda seconds: (clock.__setitem__(0, clock[0] + seconds), setattr(wire, 'now', clock[0]))
    if selected:
        for wb, uids in [('wb-a', ['account-a', 'account-b']), ('wb-b', ['account-c', 'account-d'])]:
            for uid in uids:
                s['seed'](wb, uid)
        refresh()
        response = api.post('/api/im/binding/select', json={
            'workbench_id': 'wb-a', 'account_uid': 'account-a', 'expected_version': 0})
        assert response.status_code == 200, response.text
        api.headers["X-Xunjia-Context"]=response.json()["data"]["binding"]["context_id"]
        # A selected fixture represents a fully connected current-protocol peer.
        # Use real signed manifests/receiver ledger; no business command is sent.
        initial_collector=collector(s,filename='fixture-context.db')
        initial_collector.poll();app.state.inbox.poll_once()
        assert app.state.institution.current_adapter(OWNER).context_ready
    return s


@pytest.fixture
def selection(tmp_path, monkeypatch):
    s = build_harness(tmp_path, monkeypatch)
    try:
        yield s
    finally:
        s['stack'].close()


def directory_request(s, *, refresh=False, wid=None, uid=None):
    if refresh:
        s['refresh']()
    if wid is not None:
        return json.dumps(s['app'].state.institution.directory.contacts(wid, uid, OWNER))
    return s['api'].get('/api/im/catalog').text


def collector(s, wid='wb-a', uid='account-a', *, filename='collector.db'):
    keys = s['keys']
    paths = account_paths(wid, uid)
    channel=InstitutionTransport(keys.binding(wid, uid),
        s['wire'].client(paths['downlink_path']), s['wire'].client(paths['uplink_path']),
        s['tmp'] / filename, role='collector', institution_owner=keys.owner_id,
        clock=lambda: s['clock'][0])
    from server.receiver_ledger import ReceiverLedger
    ledger=ReceiverLedger(s['tmp']/(filename+'.context-receiver'),channel.binding,clock=lambda:s['clock'][0])
    def manifests(data):
        payload=data['payload']
        if payload.get('op')=='activate_context':
            state=ledger.activate_context(payload['context_id'],payload['context_version'],published_source_highwater=channel.store.source_highwater)
            channel.publish_manifest({'contract':CONTRACT,'op':'context_ready',**state})
        elif payload.get('op')=='sync_contacts':return
        else:raise AssertionError('Unexpected synthetic collector manifest')
    channel.manifest_handler=manifests
    return channel
