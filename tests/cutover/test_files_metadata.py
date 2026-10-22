"""Read-only retained-file metadata through real local coordinator and ASGI API.

Only the existing in-memory vendor files HTTP is used; never consumes real MQ.
"""
import hashlib
import json
from urllib.parse import urlencode

import pytest

from server.auth import AuthUser
from relay.protocol import Codec, envelope
from tests.cutover.test_im_catalog_selection import choose
from tests.institution_test_support import selection as institution_selection, collector, OWNER
from relay.gateway_paths import CONTRACT



@pytest.fixture
def selection(institution_selection):
    s=institution_selection
    s['file_adapters']={}
    for wb,uid in [('wb-a','account-a'),('wb-b','account-c')]:
        native=collector(s,wb,uid,filename='files-'+wb+'.db')
        for i in range(2):
            native.publish_event({'event_id':'files-'+wb+'-'+str(i),'peer_id':'peer-'+wb,
                'direction':'in','text':'synthetic retained metadata '+str(i),
                'occurred_at':s['now'],'ext_msg_id':'native-'+wb+'-'+str(i)})
        adapter=s['runtime'].adapter(OWNER,wb,uid)
        assert len(adapter.poll())==2
        s['file_adapters'][(wb,uid)]=adapter
    return s


def history(s,**query):
    response=s['api'].get('/api/im/files',params=query)
    assert response.status_code==200,response.text
    return response.json()['data']


def test_real_http_history_lists_only_requested_authenticated_pair_without_wire_operations(selection):
    s=selection;calls=list(s['wire'].calls);adapter=s['file_adapters'][('wb-a','account-a')]
    with adapter.store.db() as db:before=[tuple(x) for x in db.execute('SELECT * FROM fg_download')]
    value=history(s)
    assert value['scope']=='retained_authenticated_files'
    assert (value['workbench_id'],value['account_uid'])==('wb-a','account-a')
    assert len(value['items'])==2 and {i['kind'] for i in value['items']}=={'event'}
    assert all(i['lane']=='up' and i['source_created_at']<=s['now'] and i['received_at'] is not None for i in value['items'])
    assert all(i['extension']=='.bin' and i['receive_state']=='STAGED' for i in value['items'])
    assert all(not {'body','blob','access_token','auth_key','sm4_key'} & i.keys() for i in value['items'])
    with adapter.store.db() as db:assert [tuple(x) for x in db.execute('SELECT * FROM fg_download')]==before
    assert s['wire'].calls==calls


def test_real_http_history_pagination_is_stable_disjoint_and_pair_scoped(selection):
    s = selection;calls = list(s['wire'].calls)
    first = history(s, limit=1)
    assert len(first['items']) == 1 and type(first['next_before']) is int
    second = history(s, limit=1, before=first['next_before'])
    assert len(second['items']) == 1
    assert first['items'][0]['id'] > second['items'][0]['id']
    assert first['items'][0]['id'] != second['items'][0]['id']
    assert all(page['workbench_id']=='wb-a' and page['account_uid']=='account-a' for page in [first,second])
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
    assert choose(s, 'wb-b', 'account-c').status_code == 200
    # Switching starts a new retained journal, not a migration of old files.
    assert s['api'].get('/api/im/files').json()['data']['items']==[]
    native=collector(s,'wb-b','account-c',filename='files-new-context.db')
    native.poll();s['app'].state.inbox.poll_once()
    for i in range(2):
        native.publish_event({'event_id':'new-context-'+str(i),'peer_id':'peer-wb-b','direction':'in',
            'text':'current account newly received '+str(i),'occurred_at':s['now'],'ext_msg_id':'current-native-'+str(i)})
    s['runtime'].current_adapter(OWNER).poll()
    calls = list(s['wire'].calls)
    changed = s['api'].get('/api/im/files').json()['data']
    assert (changed['workbench_id'], changed['account_uid']) == ('wb-b', 'account-c')
    assert len(changed['items']) == 2
    assert all(item['kind']=='event' for item in changed['items'])
    assert {(changed['workbench_id'],changed['account_uid'],i['id']) for i in changed['items']}.isdisjoint((original['workbench_id'],original['account_uid'],i['id']) for i in original['items'])
    assert s['wire'].calls == calls


def test_other_authenticated_workspace_cannot_read_selected_account_file_history(selection):
    s=selection;calls=list(s['wire'].calls)
    token=s['app'].state.auth.issue_token(AuthUser('other-owner','other-owner'));headers={'Authorization':'Bearer '+token}
    response=s['api'].get('/api/im/files',headers=headers)
    assert response.status_code==400
    assert 'items' not in response.json().get('data',{})
    assert s['wire'].calls==calls


def test_history_excludes_unauthenticated_and_foreign_account_bytes_and_retains_unknown(selection):
    s=selection;adapter=s['file_adapters'][('wb-a','account-a')]
    foreign=s['keys'].binding('foreign-wb','foreign-uid')
    blob=Codec(foreign,clock=lambda:s['now']).pack(envelope(foreign,'event',{'event_id':'foreign-event','peer_id':'foreign-peer','direction':'in','text':'foreign synthetic'},message_id='foreign-event',created_at=s['now']-10,expires_at=s['now']+600))
    with adapter.store.db() as db:
        for name,body in [('foreign.bin',blob),('bad.bin',b'not-signed')]:
            db.execute('INSERT INTO fg_download(scope,state,name,blob) VALUES(?,?,?,?)',(adapter.store.scope,'STORED',name,body))
        seq=db.execute("INSERT INTO fg_download(scope,state,name) VALUES(?,'UNKNOWN','old-outcome.bin')",(adapter.store.scope,)).lastrowid
        original=tuple(db.execute('SELECT * FROM fg_download WHERE seq=?',(seq,)).fetchone())
    calls=list(s['wire'].calls);result=history(s)
    assert len(result['items'])==2 and 'foreign' not in json.dumps(result) and 'bad.bin' not in json.dumps(result)
    with adapter.store.db() as db:assert tuple(db.execute('SELECT * FROM fg_download WHERE seq=?',(seq,)).fetchone())==original
    assert s['wire'].calls==calls


def test_legacy_received_time_stays_null_not_filled_from_source_or_today(selection):
    s=selection;adapter=s['file_adapters'][('wb-a','account-a')]
    event=history(s)['items'][0]
    assert event['kind']=='event' and event['lane']=='up'
    with adapter.store.db() as db:
        seq=event['id']//2
        db.execute('UPDATE fg_download SET received_at=NULL WHERE seq=?',(seq,))
    calls=list(s['wire'].calls);values=history(s)['items']
    assert any(i['received_at'] is None for i in values) and all(i['source_created_at'] is not None for i in values)
    with adapter.store.db() as db:assert db.execute('SELECT received_at FROM fg_download WHERE seq=?',(seq,)).fetchone()[0] is None
    assert s['wire'].calls==calls


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
    from server.quote_control import QuoteControl
    monkeypatch.delenv('XUNJIA_TRANSPORT_CONFIG',raising=False)
    monkeypatch.delenv('XUNJIA_MQ_CONNECTION_CONFIG',raising=False)
    monkeypatch.setenv('XUNJIA_STATE_DIR',str(tmp_path/'unbound-state'))
    app=create_app(database_url='sqlite:///'+str(tmp_path/'unbound.db'),enable_background=False,
        quote_control=QuoteControl(directory=tmp_path/'quote'),
        auth_config=AuthConfig(sso_secret='synthetic-only',session_secret='z'*40))
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+app.state.auth.issue_token(AuthUser(OWNER,OWNER))
        response=client.get('/api/im/files')
        assert response.status_code==400
        assert app.state.repository.binding_for(OWNER) is None
