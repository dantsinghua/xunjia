"""Read-only retained-file metadata through real local coordinator and ASGI API.

Only the existing in-memory vendor files HTTP is used; never consumes real MQ.
"""
import hashlib
import json
from urllib.parse import urlencode

import pytest

from server.auth import AuthUser
from relay.protocol import Codec, envelope
from tests.cutover.test_im_catalog_selection import selection, choose
from tests.cutover.test_file_gateway_pair_isolation import directory_http, OWNER, binding


def history(s, wid='wb-a', uid='account-a', **query):
    status, _, raw = s['client']()._request('history?' + urlencode({
        'workbench_id': wid, 'account_uid': uid, **query}))
    assert status == 200
    return json.loads(raw)


def test_real_http_history_lists_only_requested_authenticated_pair_without_wire_operations(selection):
    s = selection
    calls = list(s['wire'].calls)
    with s['journal'].db() as db:
        before = db.execute('SELECT id,state,h,body FROM receive ORDER BY id').fetchall()
    value = history(s)
    assert value['scope'] == 'retained_authenticated_files'
    assert (value['workbench_id'], value['account_uid']) == ('wb-a', 'account-a')
    assert len(value['items']) == 2
    assert {item['kind'] for item in value['items']} == {'catalog', 'contacts'}
    assert all('wb-a-account-a' in item['name'] for item in value['items'])
    assert all(item['lane'] == 'up' and item['source_created_at'] <= s['now'] for item in value['items'])
    assert all(item['receive_state'] == 'STORED' for item in value['items'])
    assert all(item['received_at'] is not None for item in value['items'])
    assert all(item['extension'] in ['bin', '.bin'] for item in value['items'])
    assert all(not any(secret in item for secret in ['body', 'blob', 'access_token', 'auth_key', 'sm4_key']) for item in value['items'])
    assert s['wire'].calls == calls
    with s['journal'].db() as db:
        assert db.execute('SELECT id,state,h,body FROM receive ORDER BY id').fetchall() == before


def test_real_http_history_pagination_is_stable_disjoint_and_pair_scoped(selection):
    s = selection;calls = list(s['wire'].calls)
    first = history(s, limit=1)
    assert len(first['items']) == 1 and type(first['next_before']) is int
    second = history(s, limit=1, before=first['next_before'])
    assert len(second['items']) == 1
    assert first['items'][0]['id'] > second['items'][0]['id']
    assert first['items'][0]['id'] != second['items'][0]['id']
    assert all('wb-a-account-a' in item['name'] for page in [first, second] for item in page['items'])
    assert history(s, before=second['items'][0]['id'])['items'] == []
    assert s['wire'].calls == calls


def test_current_binding_controls_api_scope_and_client_parameters_cannot_select_another_pair(selection):
    s = selection
    response = s['api'].get('/api/im/files')
    assert response.status_code == 200
    original = response.json()['data']
    assert (original['workbench_id'], original['account_uid']) == ('wb-a', 'account-a')
    polluted = s['api'].get('/api/im/files', params={'workbench_id':'wb-b','account_uid':'account-b','owner_user_id':'other-owner'})
    assert polluted.status_code in [200, 400, 422]
    if polluted.status_code == 200:
        assert polluted.json()['data'] == original
    assert choose(s, 'wb-b', 'account-a').status_code == 200
    calls = list(s['wire'].calls)
    changed = s['api'].get('/api/im/files').json()['data']
    assert (changed['workbench_id'], changed['account_uid']) == ('wb-b', 'account-a')
    assert len(changed['items']) == 2
    assert all('wb-b-account-a' in item['name'] for item in changed['items'])
    assert {item['id'] for item in changed['items']}.isdisjoint(item['id'] for item in original['items'])
    assert s['wire'].calls == calls


def test_other_authenticated_owner_cannot_read_connection_history(selection):
    s = selection;calls = list(s['wire'].calls)
    token = s['app'].state.auth.issue_token(AuthUser('other-owner','other-owner'))
    response = s['api'].get('/api/im/files', headers={'Authorization':'Bearer '+token})
    assert response.status_code == 403
    assert 'wb-a-account-a' not in response.text
    assert s['wire'].calls == calls


def test_history_excludes_unauthenticated_and_foreign_owner_bytes_and_retains_unknown(selection):
    s = selection
    foreign = binding('foreign-wb', 'foreign-uid', owner='unrelated-owner')
    blob = Codec(foreign, clock=lambda:s['now']).pack(envelope(foreign, 'catalog',
        {'accounts':[{'account_uid':'foreign-uid'}]}, message_id='foreign-catalog',
        created_at=s['now']-10, expires_at=s['now']+600))
    with s['journal'].db() as db:
        # Exact historical retained-byte preimages; current authenticated upload
        # correctly rejects these, so they must not be fabricated as successful uploads.
        for name, body in [('foreign-wb.bin',blob),('not-an-authenticated-envelope.bin',b'not-a-signed-packet')]:
            db.execute('INSERT INTO receive(state,h,name,body) VALUES(?,?,?,?)',
                ('STORED',hashlib.sha256(body).hexdigest(),name,body))
        db.execute('INSERT INTO receive(state,h,name,body) VALUES(?,?,?,?)',
            ('UNKNOWN','d'*64,'old-outcome.bin',None))
        original_unknown = db.execute("SELECT * FROM receive WHERE h=?",('d'*64,)).fetchone()
    calls = list(s['wire'].calls)
    result = history(s)
    assert len(result['items']) == 2
    assert 'foreign-wb' not in json.dumps(result) and 'not-an-authenticated' not in json.dumps(result)
    with s['journal'].db() as db:
        assert db.execute("SELECT * FROM receive WHERE h=?",('d'*64,)).fetchone() == original_unknown
    assert s['wire'].calls == calls


def test_legacy_received_time_stays_null_not_filled_from_source_or_today(selection):
    s = selection
    with s['journal'].db() as db:
        assert 'received_at' in [row[1] for row in db.execute('PRAGMA table_info(receive)')]
        ids = [row[0] for row in db.execute('SELECT id FROM receive ORDER BY id')]
        db.execute('UPDATE receive SET received_at=NULL WHERE id=?', (ids[0],))
    calls = list(s['wire'].calls)
    values = history(s)['items']
    assert any(item['received_at'] is None for item in values)
    assert all(item['source_created_at'] is not None for item in values)
    with s['journal'].db() as db:
        assert db.execute('SELECT received_at FROM receive WHERE id=?',(ids[0],)).fetchone()[0] is None
    assert s['wire'].calls == calls


@pytest.mark.parametrize('query', [{'before':0},{'before':-1},{'before':'not-int'},{'limit':0},{'limit':101}])
def test_invalid_metadata_pagination_rejected_without_fetching_remote(selection, query):
    s = selection;calls = list(s['wire'].calls)
    response = s['api'].get('/api/im/files',params=query)
    assert response.status_code in [400,422]
    assert s['wire'].calls == calls


def test_unbound_owner_cannot_get_file_history_or_trigger_remote_consumer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig
    monkeypatch.delenv('XUNJIA_TRANSPORT_CONFIG',raising=False)
    monkeypatch.delenv('XUNJIA_MQ_CONNECTION_CONFIG',raising=False)
    monkeypatch.setenv('XUNJIA_STATE_DIR',str(tmp_path/'unbound-state'))
    app=create_app(database_url='sqlite:///'+str(tmp_path/'unbound.db'),enable_background=False,
        auth_config=AuthConfig(sso_secret='synthetic-only',session_secret='z'*40))
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+app.state.auth.issue_token(AuthUser(OWNER,OWNER))
        response=client.get('/api/im/files')
        assert response.status_code==400
        assert app.state.repository.binding_for(OWNER) is None
