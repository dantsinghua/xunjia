from fastapi.testclient import TestClient
from server.legacy_app import create_app
from server.auth import AuthConfig,AuthUser
from types import SimpleNamespace


def test_failed_inbox_is_visible_only_to_owner_without_exception_secrets(tmp_path,caplog):
    class Broken:
        route='oss'
        binding=SimpleNamespace(owner_user_id='a',account_uid='a1',workbench_id='wb')
        def poll(self,**kwargs):raise RuntimeError('SHOULD-NOT-LEAK-TOKEN')
    transport=SimpleNamespace(adapters={('a','a1','oss'):Broken()})
    app=create_app(database_url='sqlite:///'+str(tmp_path/'x.db'),auth_config=AuthConfig(sso_secret='fake',session_secret='fake-session-key-with-more-than-32-bytes'),enable_background=False,transport=transport)
    for owner in ('a','b'):app.state.repository.bind_account(owner,owner+'1','wb')
    assert app.state.inbox.poll_once()==0
    with TestClient(app) as client:
        a=client.get('/api/runtime/status',headers={'Authorization':'Bearer '+app.state.auth.issue_token(AuthUser('a','A'))}).json()
        b=client.get('/api/runtime/status',headers={'Authorization':'Bearer '+app.state.auth.issue_token(AuthUser('b','B'))}).json()
        assert a['data']['components'][0]['code']=='TRANSPORT_POLL_FAILED'
        assert a['data']['components'][0]['failures']==1
        assert b['data']['components']==[]
        assert 'SHOULD-NOT-LEAK' not in str(a)+str(b)+caplog.text
        health=client.get('/api/health').json()['data']
        assert health['database_ready'] and not health['automation_configured']
