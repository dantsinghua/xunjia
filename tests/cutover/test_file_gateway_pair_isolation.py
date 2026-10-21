"""Independent authenticated pair-isolation gates; synthetic packets only."""
import pytest

from relay.protocol import Binding, Codec, ProtocolError, envelope

NOW = 1800000000
OWNER = 'qa-directory-owner'
SEAL = b's' * 32
SIGN = b'h' * 32


def binding(wid='wb-a', uid='account-a', *, owner=OWNER):
    return Binding(owner, uid, wid, SEAL, SIGN)


def packet(wid='wb-a', uid='account-a', *, owner=OWNER, kind='catalog', expires=NOW + 60):
    selected = binding(wid, uid, owner=owner)
    payload = {'workbench_id': wid, 'account_uid': uid}
    return Codec(selected, clock=lambda: NOW - 10).pack(envelope(
        selected, kind, payload, message_id=f'{kind}-{wid}-{uid}',
        created_at=NOW - 10, expires_at=expires))


@pytest.mark.parametrize('wid,uid', [('wb-a', 'account-a'), ('wb-a', 'account-b'),
                                    ('wb-b', 'account-a'), ('wb-b', 'account-b')])
def test_explicit_owned_decode_authenticates_pair_without_weakening_fixed_binding(wid, uid):
    codec = Codec(binding(), clock=lambda: NOW)
    blob = packet(wid, uid)
    owned = codec.unpack_owned(blob)
    assert (owned['workbench_id'], owned['account_uid']) == (wid, uid)
    if (wid, uid) == ('wb-a', 'account-a'):
        assert codec.unpack(blob) == owned
    else:
        with pytest.raises(ProtocolError):
            codec.unpack(blob)


@pytest.mark.parametrize('mode', ['foreign-owner', 'wrong-sign-key', 'corrupted-ciphertext'])
def test_owned_decode_still_rejects_unauthenticated_or_other_owner_packets(mode):
    codec = Codec(binding(), clock=lambda: NOW)
    blob = packet('wb-b', 'account-a', owner='qa-foreign-owner' if mode == 'foreign-owner' else OWNER)
    if mode == 'wrong-sign-key':
        codec = Codec(Binding(OWNER, 'account-a', 'wb-a', SEAL, b'x' * 32), clock=lambda: NOW)
    if mode == 'corrupted-ciphertext':
        blob = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(ProtocolError):
        codec.unpack_owned(blob)


@pytest.mark.parametrize('kind', ['catalog', 'contacts'])
def test_expired_signed_directory_is_explicit_stale_cache_only(kind):
    codec = Codec(binding(), clock=lambda: NOW)
    blob = packet('wb-b', 'account-a', kind=kind, expires=NOW - 1)
    with pytest.raises(ProtocolError):
        codec.unpack(blob)
    with pytest.raises(ProtocolError):
        codec.unpack_owned(blob)
    value = codec.unpack_owned(blob, allow_expired=True)
    assert value['expires_at'] < NOW
    assert value['workbench_id'] == 'wb-b'


@pytest.fixture
def directory_http(tmp_path, monkeypatch):
    """Real local handler and codec; only upstream wire is an in-memory files API."""
    import json
    import threading
    import time
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from pathlib import Path
    from relay import file_gateway_coordinator as coordinator
    from relay.file_gateway import FileGatewayClient, GatewayAuth
    from relay.file_gateway_journal import Journal
    from tests.cutover.file_gateway_fakes import Gateway

    now = time.time()
    wire = Gateway()
    upstream = wire.client('directory-upstream')
    monkeypatch.setattr(coordinator, 'load_connection', lambda *args, **kwargs: upstream)
    journal = Journal(tmp_path / 'central.db', upstream)
    codec = Codec(binding(), clock=lambda: now)
    auth = GatewayAuth('synthetic-local-directory', b'0123456789abcdef')
    server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
    base = 'http://127.0.0.1:' + str(server.server_port)
    connection = tmp_path / 'connection.json'
    connection.write_text(json.dumps({'owner_user_id': OWNER, 'account_uid': 'account-a',
        'workbench_id': 'wb-a', 'base_url': 'https://directory-upstream.invalid',
        'auth_file': 'remote-auth.yaml', 'allow_http': False,
        'uplink_url': base + '/up', 'downlink_url': base + '/down'}))
    connection.chmod(0o600)
    (tmp_path / 'remote-auth.yaml').write_text('key: ' + upstream.auth.key + '\nsm4_key: ' + upstream.auth.sm4_key.decode() + '\n')
    (tmp_path / 'remote-auth.yaml').chmod(0o600)
    server.RequestHandlerClass = coordinator.make_handler(journal, auth, connection,
        directory_codec=codec, default_binding=binding())
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    original = FileGatewayClient._request
    def only_owned(self, operation, *args, **kwargs):
        if self.opener is wire:
            return original(self, operation, *args, **kwargs)
        assert self.base_url.startswith(base + '/'), 'No external network permitted'
        return original(self, operation, *args, **kwargs)
    monkeypatch.setattr(FileGatewayClient, '_request', only_owned)
    def client(wid=None, uid=None, lane='up'):
        suffix = '/' + lane if wid is None else '/' + lane + '/' + wid + '/' + uid
        return FileGatewayClient(base + suffix, auth, allow_http=True)
    def publish(wid, uid, kind, *, expired=False, capabilities=True, owner=OWNER, extra=None):
        b = binding(wid, uid, owner=owner)
        payload = {'workbench_id': wid, 'account_uid': uid, 'updated_at': now - 10}
        if kind == 'catalog':
            payload.update(workbench_name='Collector ' + wid,
                accounts=[{'account_uid': uid, 'account_nick': wid + '-' + uid}],
                capabilities=['negotiation-v22-watermark'] if capabilities else [])
        elif kind == 'contacts':
            payload.update(contacts=[{'uin': 'peer-' + wid + '-' + uid, 'name': wid + ':' + uid}], org={})
        else:
            payload.update(event_id='event-' + wid + '-' + uid, peer_id='peer', direction='in', text='synthetic event')
        payload.update(extra or {})
        env = envelope(b, kind, payload, message_id=kind + '-' + wid + '-' + uid,
            created_at=now - 10, expires_at=now - 1 if expired else now + 600)
        blob = Codec(b, clock=lambda: now - 10).pack(env)
        if expired:
            # Seed a legitimate old receive preimage, then rebuild the authenticated cache.
            import hashlib
            with journal.db() as db:
                db.execute('INSERT INTO receive(state,h,name,body) VALUES(?,?,?,?)',
                    ('DELIVERED', hashlib.sha256(blob).hexdigest(), 'old.bin', blob))
            journal.configure_directory(codec, binding())
        else:
            client(wid, uid).upload(kind + '-' + wid + '-' + uid + '.bin', blob)
        return blob
    try:
        yield {'journal': journal, 'wire': wire, 'upstream': upstream, 'client': client,
               'publish': publish, 'base': base, 'auth': auth, 'connection': connection,
               'tmp': tmp_path, 'now': now, 'codec': codec}
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        assert not thread.is_alive()


def directory_request(stack, *, wid=None, uid=None, refresh=False):
    params = []
    if wid is not None:
        params.extend([('workbench_id', wid), ('account_uid', uid)])
    if refresh:
        params.append(('refresh', '1'))
    from urllib.parse import urlencode
    status, _, body = stack['client']()._request('directory' + ('?' + urlencode(params) if params else ''))
    assert status == 200
    return body


def seed_four_pairs(stack):
    for wid in ['wb-a', 'wb-b']:
        for uid in ['account-a', 'account-b']:
            for kind in ['catalog', 'contacts']:
                stack['publish'](wid, uid, kind)
    directory_request(stack, refresh=True)


def test_real_http_directory_aggregates_four_authenticated_pairs_and_contacts_do_not_cross_workbenches(directory_http):
    import json
    s = directory_http
    seed_four_pairs(s)
    before_calls = list(s['wire'].calls)
    value = json.loads(directory_request(s))
    assert {(w['workbench_id'], a['account_uid']) for w in value['workbenches'] for a in w['accounts']} == {
        (w, u) for w in ['wb-a', 'wb-b'] for u in ['account-a', 'account-b']}
    for wid in ['wb-a', 'wb-b']:
        selected = json.loads(directory_request(s, wid=wid, uid='account-a'))
        assert selected['workbench_id'] == wid and selected['account_uid'] == 'account-a'
        assert selected['contacts'][0]['uin'] == 'peer-' + wid + '-account-a'
    assert s['wire'].calls == before_calls, 'Directory GET must not consume or acknowledge upstream'


def test_explicit_refresh_uses_existing_single_journal_and_only_buffers_not_delivers(directory_http):
    import json
    s = directory_http
    seed_four_pairs(s)
    result = json.loads(directory_request(s, refresh=True))
    assert result['refresh_requested'] and result['refresh_error'] is None
    with s['journal'].db() as db:
        assert db.execute("SELECT COUNT(*) FROM receive WHERE state='STORED'").fetchone()[0] == 8
        assert db.execute('SELECT SUM(delivered) FROM upload').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM receive').fetchone()[0] == 8
    directory_request(s, refresh=True)
    with s['journal'].db() as db:
        assert db.execute('SELECT COUNT(*) FROM receive').fetchone()[0] == 8


def test_default_route_stays_pair_a_and_scoped_download_cannot_take_other_pair(directory_http):
    s = directory_http
    seed_four_pairs(s)
    directory_request(s, refresh=True)
    for client, pair in [(s['client'](), ('wb-a', 'account-a')),
                         (s['client']('wb-b', 'account-a'), ('wb-b', 'account-a'))]:
        for _ in range(2):
            item = client.download()
            env = s['codec'].unpack_owned(item.content)
            assert (env['workbench_id'], env['account_uid']) == pair
        assert client.download() is None
    assert s['client']('wb-a', 'account-b').pending() == 2
    assert s['client']('wb-b', 'account-b').pending() == 2


@pytest.mark.parametrize('mode', ['other-pair', 'other-owner', 'bad-ciphertext'])
def test_local_scoped_upload_rejects_wrong_pair_owner_or_ciphertext_before_remote_write(directory_http, mode):
    from relay.file_gateway import FileGatewayError
    s = directory_http
    b = binding('wb-b', 'account-a', owner='foreign-owner' if mode == 'other-owner' else OWNER)
    env = envelope(b, 'catalog', {'account_uid': b.account_uid, 'workbench_id': b.workbench_id},
        message_id='wrong-target', created_at=s['now'] - 1, expires_at=s['now'] + 100)
    blob = Codec(b, clock=lambda: s['now']).pack(env)
    if mode == 'bad-ciphertext':
        blob = blob[:-1] + bytes([blob[-1] ^ 1])
    calls = list(s['wire'].calls)
    with pytest.raises(FileGatewayError):
        s['client']().upload('invalid.bin', blob)
    assert s['wire'].calls == calls
    with s['journal'].db() as db:
        assert db.execute('SELECT COUNT(*) FROM upload').fetchone()[0] == 0


def test_old_unknown_preserves_cached_directory_and_allows_new_delivery_without_deletion(directory_http):
    import json
    from relay.file_gateway import FileGatewayError
    s = directory_http
    seed_four_pairs(s)
    with s['journal'].db() as db:
        row_id = db.execute("INSERT INTO receive(state,h,name,body) VALUES('UNKNOWN',NULL,'old-unknown',NULL)").lastrowid
        before = tuple(db.execute('SELECT * FROM receive WHERE id=?', (row_id,)).fetchone())
    calls = list(s['wire'].calls)
    assert len(json.loads(directory_request(s))['workbenches']) == 2
    assert json.loads(directory_request(s, wid='wb-b', uid='account-a'))['contacts'][0]['uin'] == 'peer-wb-b-account-a'
    refresh = json.loads(directory_request(s, refresh=True))
    assert not refresh.get('refresh_error')
    assert s['client']('wb-b', 'account-a').download() is not None
    assert len(s['wire'].calls)>len(calls)
    with s['journal'].db() as db:
        assert tuple(db.execute('SELECT * FROM receive WHERE id=?', (row_id,)).fetchone()) == before


def test_unregistered_authenticated_remote_catalog_can_be_refreshed_but_foreign_owner_cannot(directory_http):
    import json
    s = directory_http
    def remote(wid, owner):
        b = binding(wid, 'remote-account', owner=owner)
        env = envelope(b, 'catalog', {'account_uid': b.account_uid, 'workbench_id': wid,
            'accounts': [{'account_uid': b.account_uid}], 'capabilities': ['negotiation-v22-watermark']},
            message_id='remote-' + wid, created_at=s['now'] - 1, expires_at=s['now'] + 300)
        s['upstream'].upload(wid + '.bin', Codec(b, clock=lambda: s['now']).pack(env))
    remote('wb-unregistered', OWNER)
    result = json.loads(directory_request(s, refresh=True))
    assert result['workbenches'][0]['workbench_id'] == 'wb-unregistered'
    remote('wb-foreign', 'different-owner')
    result = json.loads(directory_request(s, refresh=True))
    assert not result.get('refresh_error')
    assert {w['workbench_id'] for w in result['workbenches']} == {'wb-unregistered'}
    with s['journal'].db() as db:
        assert db.execute("SELECT COUNT(*) FROM receive WHERE state='FOREIGN' AND body IS NOT NULL").fetchone()[0] == 1


def test_explicit_owned_decode_never_renews_expired_business_events():
    codec = Codec(binding(), clock=lambda: NOW)
    with pytest.raises(ProtocolError):
        codec.unpack_owned(packet(kind='event', expires=NOW - 1), allow_expired=True)


def test_expired_directory_cache_survives_restart_but_is_never_delivered_or_acknowledged(directory_http):
    import json
    from relay.file_gateway_journal import Journal
    s = directory_http
    for kind in ['catalog', 'contacts']:
        s['publish']('wb-stale', 'account-a', kind, expired=True)
    with s['journal'].db() as db:
        before = [tuple(x) for x in db.execute('SELECT * FROM receive ORDER BY id')]
    fresh = Journal(s['journal'].path, s['upstream'])
    fresh.configure_directory(s['codec'], binding())
    value = fresh.directory('wb-stale', 'account-a')
    assert value['stale'] and value['contacts'][0]['uin'] == 'peer-wb-stale-account-a'
    account = next(w for w in fresh.directory()['workbenches'] if w['workbench_id'] == 'wb-stale')['accounts'][0]
    assert account['stale'] and not account['available']
    assert fresh.pending('up', ('wb-stale', 'account-a')) == 0
    assert fresh.download('up', ('wb-stale', 'account-a')) is None
    with fresh.db() as db:
        assert [tuple(x) for x in db.execute('SELECT * FROM receive ORDER BY id')] == before


def test_service_commands_are_scoped_by_pair_and_never_returned_to_service_up_lane(directory_http):
    s = directory_http
    blobs = {}
    for wid in ['wb-a', 'wb-b']:
        b = binding(wid, 'account-a')
        command = {'command_id': 'command-' + wid, 'owner_user_id': OWNER,
            'workbench_id': wid, 'account_uid': 'account-a', 'peer_id': 'peer',
            'content_hash': 'a' * 64, 'control_version': 1, 'expires_at': s['now'] + 100,
            'text': 'synthetic command no native sender', 'transport': 'oss'}
        env = envelope(b, 'command', command, message_id='command-' + wid,
            peer_id='peer', command_id=command['command_id'], content_hash=command['content_hash'],
            control_version=1, created_at=s['now'] - 1, expires_at=command['expires_at'])
        blob = Codec(b, clock=lambda: s['now']).pack(env)
        s['client'](wid, 'account-a', lane='down').upload('command-' + wid + '.bin', blob)
        blobs[wid] = blob
    directory_request(s, refresh=True)
    assert s['client']().download() is None
    assert s['client']('wb-b', 'account-a', lane='down').download().content == blobs['wb-b']
    assert s['client'](lane='down').download().content == blobs['wb-a']
    assert s['client']('wb-b', 'account-a', lane='down').download() is None


@pytest.mark.parametrize('stamp', ['old', 'future', 'string', 'boolean', 'null'])
def test_signed_timestamp_validity_cannot_be_replaced_by_fresh_envelope_time(directory_http, stamp):
    import json
    s = directory_http
    value = {'old': s['now'] - 301, 'future': s['now'] + 61,
             'string': 'today', 'boolean': True, 'null': None}[stamp]
    s['publish']('wb-stamp', 'account-a', 'catalog', extra={'updated_at': value})
    s['publish']('wb-stamp', 'account-a', 'contacts', extra={'updated_at': value})
    directory_request(s, refresh=True)
    result = json.loads(directory_request(s))['workbenches'][0]['accounts'][0]
    assert result['stale'] and not result['available']
    assert json.loads(directory_request(s, wid='wb-stamp', uid='account-a'))['stale']


def test_catalog_cannot_advertise_unpublished_accounts_as_authenticated_members(directory_http):
    import json
    s = directory_http
    s['publish']('wb-advertised', 'account-a', 'catalog', extra={'accounts': [
        {'account_uid': 'account-a'}, {'account_uid': 'unpublished-account'}]})
    directory_request(s, refresh=True)
    result = json.loads(directory_request(s))
    assert [a['account_uid'] for a in result['workbenches'][0]['accounts']] == ['account-a']


def test_directory_refresh_requires_local_authentication_before_any_upstream_operation(directory_http):
    import httpx
    s = directory_http
    old_calls = list(s['wire'].calls)
    with httpx.Client() as client:
        for suffix in ['', '?refresh=1']:
            response = client.get(s['base'] + '/up/api/files/directory' + suffix)
            assert response.status_code == 401
    assert s['wire'].calls == old_calls
    with s['journal'].db() as db:
        assert db.execute('SELECT COUNT(*) FROM receive').fetchone()[0] == 0


@pytest.mark.parametrize('channel', ['qq', 'wechat'])
@pytest.mark.parametrize('where', ['catalog-payload', 'catalog-account', 'contacts-payload'])
def test_explicit_non_qidian_pair_never_appears_via_undeclared_companion_document(directory_http, channel, where):
    import json
    from relay.file_gateway import FileGatewayError
    s = directory_http
    wid, uid = 'wb-nonqidian', 'account-a'
    for kind in ['catalog', 'contacts']:
        extra = {}
        if where == kind + '-payload':
            extra['channel'] = channel
        if kind == 'catalog' and where == 'catalog-account':
            extra['accounts'] = [{'account_uid': uid, 'channel': channel}]
        s['publish'](wid, uid, kind, extra=extra)
    directory_request(s, refresh=True)
    assert json.loads(directory_request(s))['workbenches'] == []
    with pytest.raises(FileGatewayError) as error:
        directory_request(s, wid=wid, uid=uid)
    assert error.value.status == 404


def test_nonmatching_account_channel_cannot_override_authenticated_qidian_account(directory_http):
    import json
    s = directory_http
    s['publish']('wb-qidian', 'account-a', 'catalog', extra={'accounts': [
        {'account_uid': 'account-a', 'channel': 'qidian'}, {'account_uid': 'unpublished', 'channel': 'qq'}]})
    s['publish']('wb-qidian', 'account-a', 'contacts')
    directory_request(s, refresh=True)
    row = json.loads(directory_request(s))['workbenches'][0]['accounts'][0]
    assert row['channel'] == 'qidian' and row['account_uid'] == 'account-a'
    assert json.loads(directory_request(s, wid='wb-qidian', uid='account-a'))['contacts']


def test_successful_local_upload_is_not_a_directory_fact_until_real_mq_download_preserves_it(directory_http):
    import json
    s = directory_http
    s['publish']('wb-not-yet-received', 'account-a', 'catalog')
    assert json.loads(directory_request(s))['workbenches'] == []
    with s['journal'].db() as db:
        assert db.execute('SELECT COUNT(*) FROM receive').fetchone()[0] == 0
    refreshed = json.loads(directory_request(s, refresh=True))
    assert refreshed['workbenches'][0]['workbench_id'] == 'wb-not-yet-received'
    with s['journal'].db() as db:
        assert db.execute("SELECT state FROM receive").fetchone()[0] == 'STORED'
        assert db.execute('SELECT delivered FROM upload').fetchone()[0] == 0


def test_ambiguous_local_upload_without_real_mq_receive_does_not_fabricate_directory_member(directory_http):
    import json
    from relay.file_gateway import FileGatewayError
    s = directory_http
    s['wire'].lose_upload = True
    with pytest.raises(FileGatewayError):
        s['publish']('wb-upload-unknown', 'account-a', 'catalog')
    assert json.loads(directory_request(s))['workbenches'] == []
    with s['journal'].db() as db:
        assert db.execute('SELECT state FROM upload').fetchone()[0] == 'UNKNOWN'
        assert db.execute('SELECT COUNT(*) FROM receive').fetchone()[0] == 0
