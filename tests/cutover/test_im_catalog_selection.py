"""Independent real-handler catalog selection gates; no production network or accounts."""
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay.protocol import Binding
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.negotiation.application_store import ApplicationStore
from server.negotiation.prices import LimitPolicy
from tests.cutover.test_file_gateway_pair_isolation import (
    OWNER, SEAL, SIGN, directory_http, seed_four_pairs, directory_request,
)


@pytest.fixture
def selection(directory_http, monkeypatch):
    s = directory_http
    seed_four_pairs(s)
    directory_request(s, refresh=True)
    tmp = s['tmp']
    database = 'sqlite:///' + str(tmp / 'business.db')
    repository = ApplicationStore(database, initialize=True)
    repository.bind_account(OWNER, 'account-a', 'wb-a')
    repository.bind_account('other-owner', 'other-account', 'other-workbench')
    local_auth = tmp / 'local-auth.json'
    local_auth.write_text(json.dumps({'key': s['auth'].key, 'sm4_key': s['auth'].sm4_key.decode()}))
    local_auth.chmod(0o600)
    profiles = tmp / 'transport.json'
    profiles.write_text(json.dumps([{'owner_user_id': OWNER, 'account_uid': 'account-a',
        'workbench_id': 'wb-a', 'seal_key_env': 'QA_SELECTION_SEAL', 'sign_key_env': 'QA_SELECTION_SIGN',
        'file_gateway': {'uplink_url': s['base'] + '/up', 'downlink_url': s['base'] + '/down',
                         'auth_file': local_auth.name, 'allow_http': True}}]))
    profiles.chmod(0o600)
    monkeypatch.setenv('QA_SELECTION_SEAL', base64.b64encode(SEAL).decode())
    monkeypatch.setenv('QA_SELECTION_SIGN', base64.b64encode(SIGN).decode())
    monkeypatch.setenv('XUNJIA_TRANSPORT_CONFIG', str(profiles))
    monkeypatch.setenv('XUNJIA_MQ_CONNECTION_CONFIG', str(s['connection']))
    monkeypatch.setenv('XUNJIA_STATE_DIR', str(tmp / 'service-state'))
    def make_app():
        return create_app(database_url=database, enable_background=False, 
            auth_config=AuthConfig(sso_secret='synthetic-selection', session_secret='s' * 40))
    app = make_app()
    with TestClient(app) as client:
        token = app.state.auth.issue_token(AuthUser(OWNER, OWNER))
        client.headers['Authorization'] = 'Bearer ' + token
        yield {**s, 'app': app, 'api': client, 'repository': repository, 'profiles': profiles,
               'make_app': make_app, 'bearer': token}


def choose(s, wid='wb-b', uid='account-a', version=None):
    if version is None:
        version = s['repository'].binding_for(OWNER)['version']
    return s['api'].post('/api/im/binding/select', json={
        'workbench_id': wid, 'account_uid': uid, 'expected_version': version})


def snapshot(s):
    return (s['repository'].binding_for(OWNER), s['profiles'].read_bytes(),
            dict(s['app'].state.transport.adapters))


def assert_rejected_without_change(s, response, before):
    assert response.status_code in (400, 403, 409, 422), response.text
    assert snapshot(s) == before


def seed_business(s):
    store = s['repository']
    group = store.create_group(OWNER, bond_code='QA001.IB', our_side='BUY', target_volume='1000',
        trade_day='2026-10-20', policy=LimitPolicy(mode='ABSOLUTE', value='2'), send_allowed=False)
    member, _ = store.add_member(OWNER, group, account_uid='account-a', peer_uid='old-peer')
    store.set_sending(OWNER, False, member_id=member)
    with store.db.transaction(write=False) as tx:
        peer = dict(tx.execute('SELECT * FROM xj2_peer WHERE member_id=?', (member,)).fetchone())
    return group, member, peer['thread_id']


def test_authenticated_directory_aggregates_real_signed_pairs_and_exact_selected_binding(selection):
    s = selection
    before_calls = list(s['wire'].calls)
    response = s['api'].get('/api/im/catalog')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['selected'] == {'workbench_id': 'wb-a', 'account_uid': 'account-a', 'version': 1}
    assert {(w['workbench_id'], a['account_uid']) for w in data['workbenches'] for a in w['accounts']} == {
        (w, u) for w in ['wb-a', 'wb-b'] for u in ['account-a', 'account-b']}
    assert s['wire'].calls == before_calls


def test_select_same_uid_different_workbench_changes_actual_adapter_contacts_and_persists(selection):
    s = selection
    original = s['repository'].binding_for(OWNER)
    response = choose(s)
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['selected'] == {'workbench_id': 'wb-b', 'account_uid': 'account-a', 'version': 2}
    selected = s['repository'].binding_for(OWNER)
    assert selected['owner_user_id'] == original['owner_user_id']
    assert (selected['workbench_id'], selected['account_uid']) == ('wb-b', 'account-a')
    adapter = s['app'].state.transport.adapters[(OWNER, 'account-a', 'oss')]
    assert adapter.binding.workbench_id == 'wb-b'
    assert adapter.inbound.base_url == s['base'] + '/up/wb-b/account-a'
    assert adapter.outbound.base_url == s['base'] + '/down/wb-b/account-a'
    assert result['contacts']['contacts'][0]['uin'] == 'peer-wb-b-account-a'
    book = s['api'].get('/api/im/contacts').json()['data']
    assert book['workbench_id'] == 'wb-b' and book['account_uid'] == 'account-a'
    assert book['contacts'][0]['uin'] == 'peer-wb-b-account-a'
    with TestClient(s['make_app']()) as reopened:
        reopened.headers['Authorization'] = 'Bearer ' + s['bearer']
        assert reopened.get('/api/im/catalog').json()['data']['selected'] == result['selected']
        assert reopened.get('/api/im/contacts').json()['data']['contacts'][0]['uin'] == 'peer-wb-b-account-a'


@pytest.mark.parametrize('wid,uid', [('wb-missing', 'account-a'), ('wb-a', 'not-a-member')])
def test_missing_workbench_or_account_cannot_be_filled_in_by_client(selection, wid, uid):
    s = selection
    before = snapshot(s)
    assert_rejected_without_change(s, choose(s, wid, uid), before)


def test_stale_binding_version_rejects_without_second_write(selection):
    s = selection
    assert choose(s).status_code == 200
    before = snapshot(s)
    assert_rejected_without_change(s, choose(s, 'wb-a', 'account-b', version=1), before)


@pytest.mark.parametrize('field,value', [('expected_version', True), ('expected_version', -1),
    ('workbench_id', '../outside'), ('account_uid', ''), ('unexpected_identity', 'spoofed')])
def test_exact_selection_schema_rejects_malformed_identity_or_version(selection, field, value):
    s = selection
    body = {'workbench_id': 'wb-b', 'account_uid': 'account-a', 'expected_version': 1, field: value}
    before = snapshot(s)
    assert_rejected_without_change(s, s['api'].post('/api/im/binding/select', json=body), before)


@pytest.mark.parametrize('guard', ['owner', 'group', 'member'])
def test_any_remaining_send_approval_blocks_source_switch_without_revoking_it(selection, guard):
    s = selection
    group, member, _ = seed_business(s)
    table, column, value = {'owner': ('xj2_owner', 'owner_id', OWNER),
        'group': ('xj2_group', 'group_id', group), 'member': ('xj2_peer', 'member_id', member)}[guard]
    with s['repository'].write(OWNER) as tx:
        tx.execute('UPDATE ' + table + ' SET send_allowed=1 WHERE ' + column + '=?', (value,))
    before = snapshot(s)
    response = choose(s)
    assert_rejected_without_change(s, response, before)
    with s['repository'].db.transaction(write=False) as tx:
        assert tx.execute('SELECT send_allowed FROM ' + table + ' WHERE ' + column + '=?', (value,)).fetchone()['send_allowed'] == 1


@pytest.mark.parametrize('state', ['QUEUED', 'PENDING', 'UNKNOWN'])
def test_unsettled_business_commands_block_selection_and_remain_unchanged(selection, state):
    s = selection
    group, member, thread = seed_business(s)
    with s['repository'].write(OWNER) as tx:
        tx.execute('INSERT INTO xj2_command(command_id,request_id,member_id,group_id,thread_id,state,content_hash,body_json,basis_json,result_json,cancel_requested,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('old-command', 'old-request', member, group, thread, state, 'a' * 64, '{}', '{}', '{}', 0, s['now'], s['now'] + 50))
        old = dict(tx.execute("SELECT * FROM xj2_command WHERE command_id='old-command'").fetchone())
    before = snapshot(s)
    assert_rejected_without_change(s, choose(s), before)
    with s['repository'].db.transaction(write=False) as tx:
        assert dict(tx.execute("SELECT * FROM xj2_command WHERE command_id='old-command'").fetchone()) == old


def test_noop_does_not_bump_binding_version_or_replace_adapter(selection):
    s = selection
    before = snapshot(s)
    response = choose(s, 'wb-a', 'account-a')
    assert response.status_code == 200, response.text
    after = snapshot(s)
    assert {k: v for k, v in after[0].items() if k != 'config_json'} == {
        k: v for k, v in before[0].items() if k != 'config_json'}
    assert after[1:] == before[1:]
    check = s['repository'].preferences(OWNER)['collector_directory_checks']['oss']
    assert (check['workbench_id'], check['account_uid'], check['version']) == ('wb-a', 'account-a', 1)


@pytest.mark.parametrize('reason', ['missing-contacts', 'missing-v22'])
def test_new_selection_requires_authentic_catalog_capabilities_and_real_selected_book(selection, reason):
    s = selection
    wid = 'wb-unavailable'
    s['publish'](wid, 'account-a', 'catalog', capabilities=reason != 'missing-v22')
    if reason != 'missing-contacts':
        s['publish'](wid, 'account-a', 'contacts')
    directory_request(s, refresh=True)
    before = snapshot(s)
    assert_rejected_without_change(s, choose(s, wid), before)


@pytest.mark.parametrize('expired_kind', ['catalog', 'contacts'])
def test_authenticated_stale_cache_can_be_selected_without_claiming_live_connection_or_consuming(selection, expired_kind):
    s = selection
    wid = 'wb-offline'
    for kind in ['catalog', 'contacts']:
        s['publish'](wid, 'account-a', kind, expired=kind == expired_kind)
    directory_request(s, refresh=True)
    with s['journal'].db() as db:
        old_receive = [tuple(x) for x in db.execute('SELECT * FROM receive ORDER BY id')]
    old_calls = list(s['wire'].calls)
    response = choose(s, wid)
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['selected']['workbench_id'] == wid
    assert result['connected'] is False
    assert result['contacts']['contacts'][0]['uin'] == 'peer-wb-offline-account-a'
    assert result['contacts']['updated_at'] == s['now'] - 10
    if expired_kind == 'contacts':
        assert result['contacts']['stale'] is True
    assert s['wire'].calls == old_calls
    with s['journal'].db() as db:
        assert [tuple(x) for x in db.execute('SELECT * FROM receive ORDER BY id')] == old_receive
    with s['repository'].db.transaction(write=False) as tx:
        assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?', (OWNER,)).fetchone()['send_allowed'] == 0


def test_old_transport_unknown_does_not_hide_catalog_or_get_deleted_by_selection(selection):
    s = selection
    adapter = s['app'].state.transport.adapters[(OWNER, 'account-a', 'oss')]
    with adapter.store.db() as db:
        seq = db.execute("INSERT INTO fg_download(scope,state,name,blob,code) VALUES(?,'UNKNOWN','old',NULL,'FILE_GATEWAY_HTTP_ERROR')", (adapter.store.scope,)).lastrowid
        before = tuple(db.execute('SELECT * FROM fg_download WHERE seq=?', (seq,)).fetchone())
    assert len(s['api'].get('/api/im/catalog').json()['data']['workbenches']) == 2
    assert choose(s).status_code == 200
    with adapter.store.db() as db:
        assert tuple(db.execute('SELECT * FROM fg_download WHERE seq=?', (seq,)).fetchone()) == before


def test_select_failure_before_config_commit_preserves_binding_profile_and_router(selection, monkeypatch):
    import server.collector_selection as module
    s = selection
    before = snapshot(s)
    def fail(*args, **kwargs):
        raise OSError('synthetic configuration write failure')
    monkeypatch.setattr(module, 'atomic_write', fail)
    # A failed config write must surface, not claim a persisted selection.
    with pytest.raises(OSError):
        s['app'].state.collector_selection.select(OWNER, {'workbench_id': 'wb-b', 'account_uid': 'account-a', 'expected_version': 1})
    assert snapshot(s) == before


@pytest.mark.parametrize('path,method', [('/api/im/catalog', 'GET'),
    ('/api/im/catalog/refresh', 'POST'), ('/api/im/binding/select', 'POST')])
def test_catalog_and_selection_routes_require_login(selection, path, method):
    s = selection
    before = snapshot(s)
    response = s['api'].request(method, path, headers={'Authorization': ''}, json={})
    assert response.status_code == 401
    assert snapshot(s) == before


def test_other_owner_cannot_discover_or_select_this_owner_directory(selection):
    s = selection
    s['api'].headers['Authorization'] = 'Bearer ' + s['app'].state.auth.issue_token(AuthUser('other-owner', 'other-owner'))
    before = snapshot(s)
    assert s['api'].get('/api/im/catalog').status_code == 403
    assert choose(s).status_code == 403
    assert snapshot(s) == before


def test_snapshot_refresh_is_explicit_and_never_changes_binding(selection):
    s = selection
    before = snapshot(s)
    response = s['api'].post('/api/im/catalog/refresh')
    assert response.status_code == 200, response.text
    assert response.json()['data']['refresh_requested']
    assert snapshot(s) == before


def test_stage_adapter_failure_preserves_persisted_binding_and_profile(selection, monkeypatch):
    import server.collector_selection as module
    s = selection
    before = snapshot(s)
    def fail(*args, **kwargs):
        raise ValueError('synthetic adapter initialization failure')
    monkeypatch.setattr(module.FileGatewayTransport, 'from_config', fail)
    with pytest.raises(ValueError, match='synthetic adapter'):
        s['app'].state.collector_selection.select(OWNER, {'workbench_id': 'wb-b', 'account_uid': 'account-a', 'expected_version': 1})
    assert snapshot(s) == before


def test_commit_failure_rolls_back_profile_router_and_binding(selection, monkeypatch):
    from contextlib import contextmanager
    s = selection
    before = snapshot(s)
    store = s['app'].state.repository
    original = store.write
    @contextmanager
    def failed_commit(owner):
        with original(owner) as tx:
            yield tx
            raise RuntimeError('synthetic transaction commit failure')
    monkeypatch.setattr(store, 'write', failed_commit)
    with pytest.raises(RuntimeError, match='synthetic transaction'):
        s['app'].state.collector_selection.select(OWNER, {'workbench_id': 'wb-b', 'account_uid': 'account-a', 'expected_version': 1})
    assert snapshot(s) == before


def test_memory_adapter_is_restored_even_if_disk_rollback_itself_fails(selection, monkeypatch):
    from contextlib import contextmanager
    import server.collector_selection as module
    s = selection
    before = snapshot(s)
    store = s['app'].state.repository
    original_tx, original_write = store.write, module.atomic_write
    @contextmanager
    def failed_commit(owner):
        with original_tx(owner) as tx:
            yield tx
            raise RuntimeError('synthetic transaction commit failure')
    calls = []
    def failed_rollback(path, data):
        calls.append(1)
        if len(calls) == 2:
            raise OSError('synthetic disk rollback denied')
        return original_write(path, data)
    monkeypatch.setattr(store, 'write', failed_commit)
    monkeypatch.setattr(module, 'atomic_write', failed_rollback)
    with pytest.raises((OSError, RuntimeError)):
        s['app'].state.collector_selection.select(OWNER, {'workbench_id': 'wb-b', 'account_uid': 'account-a', 'expected_version': 1})
    assert s['repository'].binding_for(OWNER) == before[0]
    assert s['app'].state.transport.adapters == before[2]
    assert len(calls) == 2


def test_select_pair_b_keeps_network_default_a_and_mq_settings_save_test_live_applied(selection, monkeypatch):
    import hashlib
    import server.mq_settings as module
    from relay.file_gateway import FileGatewayClient
    from tests.cutover.file_gateway_fakes import NOW
    s = selection
    monkeypatch.setattr(module, 'connection_client', lambda config, auth, **kwargs:
        FileGatewayClient(config['base_url'], auth, opener=s['wire'], clock=lambda: NOW))
    original = s['connection'].read_bytes()
    assert choose(s).status_code == 200
    assert s['connection'].read_bytes() == original
    response = s['api'].get('/api/im/mq-config')
    assert response.status_code == 200, response.text
    view = response.json()['data']
    assert view['editable'] and view['configured'] and view['applied']
    assert (view['workbench_id'], view['account_uid']) == ('wb-b', 'account-a')
    body = {'base_url': view['base_url'], 'config_yaml': '', 'expected_revision': view['revision']}
    result = s['api'].put('/api/im/mq-config', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['data']['saved'] and result.json()['data']['applied']
    config = json.loads(s['connection'].read_text())
    assert (config['workbench_id'], config['account_uid']) == ('wb-a', 'account-a')
    assert config['uplink_url'] == s['base'] + '/up' and config['downlink_url'] == s['base'] + '/down'
    before_calls = len(s['wire'].calls)
    result = s['api'].post('/api/im/mq-config/test', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['data']['passed'] and result.json()['data']['saved'] is False
    assert s['wire'].calls[before_calls:] == [('directory-upstream.invalid', 'pending')]


def test_switch_to_another_account_keeps_old_history_rows(selection):
    s = selection
    group, member, thread = seed_business(s)
    with s['repository'].db.transaction(write=False) as tx:
        old_group = dict(tx.execute('SELECT * FROM xj2_group WHERE group_id=?', (group,)).fetchone())
        old_thread = dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (thread,)).fetchone())
    assert choose(s, 'wb-b', 'account-b').status_code == 200
    with s['repository'].db.transaction(write=False) as tx:
        now_group = dict(tx.execute('SELECT * FROM xj2_group WHERE group_id=?', (group,)).fetchone())
        assert dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (thread,)).fetchone()) == old_thread
        for key in old_group.keys() - {'control_version', 'send_allowed'}:
            assert now_group[key] == old_group[key]
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_peer WHERE member_id=?', (member,)).fetchone()['n'] == 1
    assert s['api'].get('/api/im/contacts').json()['data']['contacts'][0]['uin'] == 'peer-wb-b-account-b'


def test_previously_unbound_owner_can_select_authenticated_pair_with_expected_version_zero(selection):
    s = selection
    with s['repository'].write(OWNER) as tx:
        tx.execute('DELETE FROM xj2_binding WHERE owner_id=?', (OWNER,))
    response = choose(s, 'wb-b', 'account-b', version=0)
    assert response.status_code == 200, response.text
    assert response.json()['data']['selected'] == {'workbench_id': 'wb-b', 'account_uid': 'account-b', 'version': 1}
    assert s['repository'].binding_for(OWNER)['workbench_id'] == 'wb-b'


def test_directory_unavailable_keeps_saved_pair_and_reports_reason(selection):
    s = selection
    before = snapshot(s)
    s['profiles'].chmod(0o644)
    response = s['api'].get('/api/im/catalog')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['workbenches'] == [] and data['note']
    assert data['selected'] == {'workbench_id': 'wb-a', 'account_uid': 'account-a', 'version': 1}
    assert snapshot(s) == before


def test_legacy_manual_binding_endpoint_cannot_bypass_authenticated_directory(selection):
    s = selection
    with s['repository'].write(OWNER) as tx:
        tx.execute('DELETE FROM xj2_binding WHERE owner_id=?', (OWNER,))
    old_profile = s['profiles'].read_bytes()
    response = s['api'].post('/api/im/binding', json={
        'workbench_id': 'forged-workbench', 'account_uid': 'forged-account', 'transport': 'oss'})
    assert response.status_code in (400, 403, 409), response.text
    assert s['repository'].binding_for(OWNER) is None
    assert s['profiles'].read_bytes() == old_profile


def test_persisted_profile_database_identity_mismatch_is_not_reported_as_recovered(selection):
    s = selection
    profiles = json.loads(s['profiles'].read_text())
    profiles[0].update(workbench_id='wrong-persisted-wb', account_uid='wrong-persisted-account')
    s['profiles'].write_text(json.dumps(profiles))
    before = snapshot(s)
    catalog = s['api'].get('/api/im/catalog').json()['data']
    assert catalog['workbenches'] == [] and catalog['note']
    assert catalog['selected']['workbench_id'] == 'wb-a'
    assert_rejected_without_change(s, choose(s), before)


def test_selected_pair_rebuilds_existing_mail_scope_and_preserves_mail_configuration(selection, monkeypatch):
    s = selection
    monkeypatch.setenv('QA_SELECTION_SMTP', 'synthetic-smtp-secret')
    monkeypatch.setenv('QA_SELECTION_IMAP', 'synthetic-imap-secret')
    mail = {'smtp_host': 'smtp.invalid', 'smtp_user': 'synthetic', 'smtp_password_env': 'QA_SELECTION_SMTP',
        'imap_host': 'imap.invalid', 'imap_user': 'synthetic', 'imap_password_env': 'QA_SELECTION_IMAP',
        'outbound_from': 'sender@synthetic.invalid', 'outbound_to': ['receiver@synthetic.invalid'],
        'inbound_from': ['receiver@synthetic.invalid'], 'inbound_to': ['sender@synthetic.invalid']}
    profiles = json.loads(s['profiles'].read_text())
    profiles[0]['mail'] = mail
    s['profiles'].write_text(json.dumps(profiles))
    app = s['make_app']()
    old = app.state.transport.adapters[(OWNER, 'account-a', 'mail')]
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + s['bearer']
        result = client.post('/api/im/binding/select', json={
            'workbench_id': 'wb-b', 'account_uid': 'account-a', 'expected_version': 1})
        assert result.status_code == 200, result.text
        selected = app.state.transport.adapters[(OWNER, 'account-a', 'mail')]
        assert selected is not old
        assert selected.binding.workbench_id == 'wb-b'
        assert selected.store.scope != old.store.scope
        assert selected.config == old.config
        assert json.loads(s['profiles'].read_text())[0]['mail'] == mail


def test_other_owners_adapter_identity_is_untouched_when_current_owner_selects(selection):
    s = selection
    sentinel = object()
    key = ('different-owner', 'different-account', 'mail')
    s['app'].state.transport.adapters[key] = sentinel
    assert choose(s).status_code == 200
    assert s['app'].state.transport.adapters[key] is sentinel


def _verified_link(s):
    response = s['api'].get('/api/im/links')
    assert response.status_code == 200, response.text
    return next(row for row in response.json()['data']['links'] if row['mode'] == 'oss')


def test_actual_selection_check_enables_current_link_and_survives_restart_without_authorizing_send(selection):
    s = selection
    assert _verified_link(s)['tested'] is False
    result = choose(s)
    assert result.status_code == 200, result.text
    current = _verified_link(s)
    assert current['tested'] is True and current['active'] is True
    assert (current['workbench_id'], current['account_uid'], current['version']) == ('wb-b', 'account-a', 2)
    assert current['catalog_stale'] is False
    assert s['api'].get('/api/im/contacts').json()['data']['contacts'][0]['uin'] == 'peer-wb-b-account-a'
    with s['repository'].db.transaction(write=False) as tx:
        assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?', (OWNER,)).fetchone()['send_allowed'] == 0
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 0
    with TestClient(s['make_app']()) as reopened:
        reopened.headers['Authorization'] = 'Bearer ' + s['bearer']
        links = reopened.get('/api/im/links').json()['data']['links']
        persisted = next(row for row in links if row['mode'] == 'oss')
        assert persisted['tested'] and persisted['active'] and persisted['workbench_id'] == 'wb-b'


@pytest.mark.parametrize('field,value', [('workbench_id', 'wb-a'), ('account_uid', 'other-account'), ('version', 1)])
def test_directory_check_evidence_cannot_be_inherited_by_other_pair_or_binding_version(selection, field, value):
    s = selection
    assert choose(s).status_code == 200
    binding = s['repository'].binding_for(OWNER)
    preferences = s['repository'].preferences(OWNER)
    preferences['collector_directory_checks']['oss'][field] = value
    with s['repository'].write(OWNER) as tx:
        tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?', (json.dumps(preferences), OWNER))
    assert _verified_link(s)['tested'] is False


def test_verified_cached_directory_keeps_current_link_readable_but_marks_stale(selection):
    s = selection
    s['publish']('wb-cached-main', 'account-a', 'catalog', expired=True)
    s['publish']('wb-cached-main', 'account-a', 'contacts', expired=True)
    assert choose(s, 'wb-cached-main').status_code == 200
    link = _verified_link(s)
    assert link['active'] and link['tested'] and link['catalog_stale']
    assert '缓存' in link['state']


def test_check_evidence_ages_without_new_network_data_being_made_current(selection):
    s = selection
    assert choose(s).status_code == 200
    binding = s['repository'].binding_for(OWNER)
    preferences = s['repository'].preferences(OWNER)
    preferences['collector_directory_checks']['oss']['updated_at'] = s['now'] - 301
    with s['repository'].write(OWNER) as tx:
        tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?', (json.dumps(preferences), OWNER))
    link = _verified_link(s)
    assert link['tested'] and link['catalog_stale']
