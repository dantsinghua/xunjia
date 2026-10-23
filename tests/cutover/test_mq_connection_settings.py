"""Current institution settings via real HTTP; synthetic FileGatewayClient HTTP only."""
import hashlib
import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.tls_material import tls_config
from relay.file_gateway import GatewayAuth, FileGatewayError
from relay.file_gateway_connection import connection_client, load_connection
from relay.gateway_paths import CONTRACT, DIRECTORY_PATH
from server.auth import AuthUser
from tests.institution_test_support import selection, build_harness, AUTH, BASE, YAML, OWNER

KEY=AUTH.key
SM4=AUTH.sm4_key.decode()

@pytest.fixture
def mq(selection):
    return selection

def status(s):
    response=s['api'].get('/api/im/mq-config');assert response.status_code==200,response.text
    return response.json()['data']

def body(s,**changes):
    return {'base_url':BASE,'config_yaml':'','expected_revision':status(s)['revision'],**changes}

def files(s):
    return {str(p):p.read_bytes() for p in s['connection'].parent.glob('institution*') if p.is_file()}

@pytest.mark.parametrize('method,suffix',[('GET',''),('PUT',''),('POST','/test')])
def test_api_requires_authentication(mq,method,suffix):
    before=files(mq);response=mq['api'].request(method,'/api/im/mq-config'+suffix,headers={'Authorization':''},json=body(mq) if method!='GET' else None)
    assert response.status_code==401 and files(mq)==before

def test_two_field_status_hides_credential_and_identity_is_fixed(mq):
    view=status(mq)
    assert view['configured'] and view['backend']=='file_gateway' and view['contract']==CONTRACT
    assert view['base_url']==BASE and view['credentials_set']
    assert (view['account_uid'],view['workbench_id'])==('account-a','wb-a')
    assert view['revision']==hashlib.sha256(mq['connection'].read_bytes()).hexdigest()
    assert not {'config_yaml','auth_key','sm4_key','token','bucket','sts_url'} & view.keys()
    assert KEY not in json.dumps(view) and SM4 not in json.dumps(view)

def test_identical_save_keeps_shared_config_router_and_identity(mq):
    before=files(mq);binding=mq['repository'].binding_for(OWNER);adapters=dict(mq['app'].state.transport.adapters)
    response=mq['api'].put('/api/im/mq-config',json=body(mq));assert response.status_code==200,response.text
    assert response.json()['data']['saved'] and not response.json()['data']['applied']
    assert files(mq)==before and mq['repository'].binding_for(OWNER)==binding
    assert mq['app'].state.transport.adapters==adapters

def test_empty_yaml_retains_existing_credentials_and_same_host_dns_mapping(mq):
    cfg=json.loads(mq['connection'].read_text());cfg.update(hostname='relay.invalid',addresses=['192.0.2.11'])
    mq['connection'].write_text(json.dumps(cfg));mq['runtime'].load();before=files(mq)
    response=mq['api'].put('/api/im/mq-config',json=body(mq));assert response.status_code==200,response.text
    assert files(mq)==before and mq['runtime'].auth==AUTH
    assert mq['runtime'].config['addresses']==['192.0.2.11']

@pytest.mark.parametrize('change',[
 {'config_yaml':'auth: [broken'},
 {'config_yaml':'key: synthetic-secret\nkey: duplicate\nsm4_key: 0123456789abcdef\n'},
 {'config_yaml':'key: synthetic-secret\nsm4_key: short\n'},
 {'config_yaml':'key: synthetic-secret\nsm4_key: '+'短'*16+'\n'},
 {'config_yaml':True},{'base_url':'https://user:private@relay.invalid'},
 {'base_url':'http://relay.invalid/its-relay/v1'}, {'base_url':'https://relay.invalid/api/files'},
 {'expected_revision':'stale'},{'account_uid':'stolen'},{'owner_user_id':'other'}, {'workbench_id':'other'}])
def test_invalid_config_or_revision_is_rejected_without_partial_writes(mq,change):
    before=files(mq);response=mq['api'].put('/api/im/mq-config',json=body(mq,**change))
    assert response.status_code in (400,403,409,422),response.text
    assert files(mq)==before and 'synthetic-secret' not in response.text and 'private@' not in response.text

def test_signed_in_workspace_can_replace_shared_connection_with_explicit_reset(mq):
    token=mq['app'].state.auth.issue_token(AuthUser('other-owner','other-owner'));headers={'Authorization':'Bearer '+token}
    response=mq['api'].put('/api/im/mq-config',headers=headers,json=body(mq,base_url='https://other.invalid'))
    assert response.status_code==200,response.text
    assert response.json()['data']['reset'] and response.json()['data']['base_url']=='https://other.invalid'
    assert mq['repository'].binding_for(OWNER) is None
    assert mq['app'].state.transport.adapters=={}


def test_test_pending_uses_unsaved_form_but_never_persists_or_swaps_router(mq):
    before=files(mq);adapters=dict(mq['app'].state.transport.adapters);calls=len(mq['wire'].calls)
    response=mq['api'].post('/api/im/mq-config/test',json=body(mq,base_url='https://draft.invalid',config_yaml=YAML))
    assert response.status_code==200,response.text
    data=response.json()['data'];assert data['passed'] and data['connected'] and not data['saved']
    assert mq['wire'].calls[calls:]==[(DIRECTORY_PATH,'pending')]
    assert files(mq)==before and mq['app'].state.transport.adapters==adapters
    assert KEY not in response.text and SM4 not in response.text

def test_failed_config_commit_does_not_leave_unreferenced_new_credentials(tmp_path,monkeypatch):
    from server import institution_settings as module
    s=build_harness(tmp_path/'first-save',monkeypatch,configured=False,selected=False)
    try:
        before=files(s);original=module.atomic_write
        def write(path,data):
            if path==s['connection']:raise OSError('synthetic config commit failure')
            return original(path,data)
        monkeypatch.setattr(module,'atomic_write',write)
        # Runtime imports may bind atomic_write directly; inject the write dependency there too.
        import server.institution_runtime as runtime_module
        if hasattr(runtime_module,'atomic_write'):monkeypatch.setattr(runtime_module,'atomic_write',write)
        response=s['api'].put('/api/im/mq-config',json={'base_url':BASE,'config_yaml':YAML,'expected_revision':'',**tls_config()})
        assert response.status_code==400 and 'synthetic config commit failure' not in response.text
        assert files(s)==before
        assert not status(s)['configured']
    finally:s['stack'].close()

@pytest.mark.parametrize('allowed',[False,True])
def test_retired_http_flags_are_rejected_even_with_certificates(mq,allowed):
    cfg={'base_url':'http://relay.invalid','allow_http':allowed,'auth_file':mq['auth_file'].name,**tls_config()}
    path=mq['tmp']/'http-policy.json';path.write_text(json.dumps(cfg));path.chmod(0o600)
    with pytest.raises(FileGatewayError):load_connection(path)

def test_direct_path_clients_use_institution_authentication_without_local_coordinator(mq):
    runtime=mq['runtime'];up=runtime.client('qa_up');down=runtime.client('qa_down')
    assert up.base_url==down.base_url==BASE and up.auth==down.auth==AUTH
    assert up.queue_identity!=down.queue_identity
    assert not hasattr(mq['app'].state,'managed_mq')
    assert set(runtime.config)<= {'contract','base_url','auth_file','hostname','addresses','client_cert_file','client_key_file','ca_file'}

def test_save_waits_for_institution_lock_and_uses_one_current_snapshot(mq):
    runtime=mq['runtime'];started=threading.Event();request=body(mq)
    def save():started.set();return mq['api'].put('/api/im/mq-config',json=request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with runtime._lock:
            result=executor.submit(save);assert started.wait(2);assert not result.done()
        response=result.result(timeout=10)
    assert response.status_code==200,response.text
    assert response.json()['data']['revision']==status(mq)['revision']

@pytest.mark.parametrize('invalid',['identity','path','auth-reference'])
def test_invalid_persisted_config_refuses_restart_without_mutating_journals(mq,invalid):
    config=json.loads(mq['connection'].read_text())
    if invalid=='identity':config['contract']='retired'
    elif invalid=='path':config['uplink_path']='caller-chosen'
    else:config['auth_file']='../outside.yaml'
    mq['connection'].write_text(json.dumps(config));before=mq['connection'].read_bytes()
    with pytest.raises((ValueError,FileGatewayError,OSError)):mq['make_app']()
    assert mq['connection'].read_bytes()==before

def test_config_pointer_is_explicit_and_not_client_editable(mq):
    before=files(mq)
    response=mq['api'].put('/api/im/mq-config',json=body(mq,config_path='/outside'))
    assert response.status_code==400 and files(mq)==before

def test_disabled_runtime_never_claims_editable_coordinator_or_applied_worker(mq):
    view=status(mq);assert view['configured'] and not view['background_enabled'] and not view['applied']
    assert mq['runtime']._thread is None and mq['runtime']._lease is None

def test_paths_cannot_be_overridden_by_connection_form(mq):
    before=files(mq)
    for field in ('uplink_url','downlink_url','path'):
        response=mq['api'].put('/api/im/mq-config',json=body(mq,**{field:'forged'}))
        assert response.status_code==400 and files(mq)==before

@pytest.mark.parametrize('change_key',[False,True])
def test_existing_unknown_is_retained_and_adapter_retired_on_explicit_connection_replacement(mq,change_key):
    adapter=mq['app'].state.transport.adapters[(OWNER,'account-a','oss')]
    old_context=adapter.context_id
    blob=b'\x00synthetic-ambiguous-native-evidence\xff'
    with adapter.store.db() as db:
        seq=db.execute("INSERT INTO fg_download(scope,state,name,blob,code) VALUES(?,'UNKNOWN','original.bin',?,'FILE_GATEWAY_HTTP_ERROR')",(adapter.store.scope,blob)).lastrowid
        before=tuple(db.execute('SELECT * FROM fg_download WHERE seq=?',(seq,)).fetchone())
    request=body(mq,base_url='https://new.invalid')
    if change_key:
        new=GatewayAuth('changed-synthetic-key',AUTH.sm4_key);mq['wire'].accepted_auth.append(new)
        request=body(mq,config_yaml='key: '+new.key+'\nsm4_key: '+SM4+'\n')
    response=mq['api'].put('/api/im/mq-config',json=request)
    assert response.status_code==200,response.text
    assert response.json()['data']['reset'] and mq['repository'].binding_for(OWNER) is None
    assert mq['app'].state.transport.adapters=={}
    assert not adapter.context_ready and not adapter.active_guard({'context_id':old_context})
    with adapter.store.db() as db:
        retained=db.execute('SELECT * FROM fg_download WHERE seq=?',(seq,)).fetchone()
        assert tuple(retained)==before and bytes(retained['blob'])==blob
    assert mq['runtime'].auth==(new if change_key else AUTH)
    assert mq['runtime'].config['base_url']==(BASE if change_key else 'https://new.invalid')


def test_sm4_rotation_starts_fresh_without_old_binding_or_credential_disclosure(mq):
    revision=status(mq)['revision'];new=GatewayAuth(KEY,b'fedcba9876543210');mq['wire'].accepted_auth.append(new)
    response=mq['api'].put('/api/im/mq-config',json=body(mq,config_yaml='key: '+KEY+'\nsm4_key: fedcba9876543210\n'))
    assert response.status_code==200,response.text
    assert response.json()['data']['reset'] and status(mq)['revision']!=revision
    assert mq['runtime'].auth==new and mq['repository'].binding_for(OWNER) is None
    assert 'fedcba9876543210' not in response.text and KEY not in response.text


def test_first_save_load_failure_reports_saved_but_unapplied_without_secret_leak(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    s=build_harness(tmp_path/'load-failure',monkeypatch,configured=False,selected=False)
    try:
        import server.institution_runtime as module
        def fail(*args,**kwargs):raise OSError('synthetic-sensitive-hotload-error')
        monkeypatch.setattr(module,'InstitutionDirectory',fail)
        with TestClient(s['app'],raise_server_exceptions=False) as api:
            api.headers.update(s['api'].headers)
            response=api.put('/api/im/mq-config',json={'base_url':BASE,'config_yaml':YAML,'expected_revision':'',**tls_config()})
        assert response.status_code in (200,400,503),response.text
        value=response.json()
        assert 'synthetic-sensitive-hotload-error' not in response.text and AUTH.key not in response.text
        persisted=s['connection'].exists()
        if response.status_code==200:
            assert value['data']['saved']==persisted and value['data']['applied'] is False
        else:
            assert value['ok'] is False and value['hint']
        assert not status(s)['applied']
    finally:s['stack'].close()


def test_old_oss_settings_api_is_retired(mq):
    assert not any(route.path.startswith('/api/im/oss-config') for route in mq['app'].routes if hasattr(route,'path'))
    assert mq['api'].get('/api/im/oss-config').status_code==404

def test_receive_unknown_does_not_hide_config_or_block_identical_save(mq):
    directory=mq['runtime'].directory
    with directory.db() as db:
        seq=db.execute("INSERT INTO ip_input(path,state,at) VALUES(?,'UNKNOWN',?)",(DIRECTORY_PATH,mq['now'])).lastrowid
        before=tuple(db.execute('SELECT * FROM ip_input WHERE seq=?',(seq,)).fetchone())
    assert status(mq)['configured']
    response=mq['api'].put('/api/im/mq-config',json=body(mq));assert response.status_code==200
    with directory.db() as db:assert tuple(db.execute('SELECT * FROM ip_input WHERE seq=?',(seq,)).fetchone())==before
    assert mq['api'].post('/api/im/mq-config/test',json=body(mq)).json()['data']['passed']

def test_existing_directory_identity_refuses_automatic_foreign_adoption(mq):
    directory=mq['runtime'].directory
    with directory.db() as db:
        db.execute("INSERT INTO ip_input(path,state,at) VALUES(?,'UNKNOWN',?)",(DIRECTORY_PATH,mq['now']))
        db.execute("UPDATE ip_directory_identity SET identity='foreign-institution'")
        original=[tuple(x) for x in db.execute('SELECT * FROM ip_input')]
    with pytest.raises(Exception,match='DIRECTORY_INSTITUTION_CHANGED'):mq['make_app']()
    with directory.db() as db:assert [tuple(x) for x in db.execute('SELECT * FROM ip_input')]==original

def test_revision_and_runtime_application_are_not_fabricated_from_lane_metadata(mq):
    view=status(mq)
    assert view['revision']==hashlib.sha256(mq['connection'].read_bytes()).hexdigest()
    assert not view['applied'] and mq['runtime']._thread is None
    assert 'connection_revision' not in view


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
