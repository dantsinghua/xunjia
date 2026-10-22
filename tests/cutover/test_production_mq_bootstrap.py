"""Current institution bootstrap; real app/runtime/codec with synthetic vendor HTTP."""
import base64
import os
import hashlib
import json
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from relay.file_gateway import FileGatewayClient,GatewayAuth
from relay.gateway_paths import CONTRACT,DIRECTORY_PATH,InstitutionKeys
from relay.protocol import Codec,envelope,ProtocolError
from server.app import create_app
from server.auth import AuthConfig,AuthUser
from server.quote_control import QuoteControl
from tests.institution_test_support import build_harness,BASE,YAML,AUTH,OWNER
from tests.tls_material import tls_config

@pytest.fixture
def bootstrap(tmp_path,monkeypatch):
    s=build_harness(tmp_path,monkeypatch,configured=False,selected=False)
    try:yield s
    finally:s['stack'].close()

def status(s):
    response=s['client'].get('/api/im/mq-config');assert response.status_code==200,response.text
    return response.json()['data']

def save(s,**changes):
    return s['client'].put('/api/im/mq-config',json={'base_url':BASE,'config_yaml':YAML,'expected_revision':status(s)['revision'],**tls_config(),**changes})

def discover(s):
    response=s['client'].post('/api/im/catalog/refresh');assert response.status_code==200,response.text
    return response.json()['data']

def select(s,wb='wb-a',uid='account-a',version=0):
    response=s['client'].post('/api/im/binding/select',json={'workbench_id':wb,'account_uid':uid,'expected_version':version})
    assert response.status_code==200,response.text
    return response.json()['data']

def wait_for(predicate,timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.02)
    pytest.fail('Synthetic institution runtime did not observe publication within bound')

def test_initial_unbound_owner_can_edit_two_fields_without_private_serve_configuration(bootstrap):
    s=bootstrap;view=status(s)
    assert not view['configured'] and not view['credentials_set'] and view['backend']=='file_gateway'
    assert view['revision']=='' and view['account_uid'] is None and view['workbench_id'] is None
    assert s['app'].state.repository.binding_for(OWNER) is None and s['vendor'].calls==[]

def test_valid_save_and_pending_test_do_not_dequeue_or_fabricate_an_initial_binding(bootstrap):
    s=bootstrap;s['seed']('wb-a','account-a');s['vendor'].calls.clear()
    assert save(s).status_code==200
    assert s['app'].state.repository.binding_for(OWNER) is None
    response=s['client'].post('/api/im/mq-config/test',json={'base_url':BASE,'config_yaml':''})
    assert response.status_code==200 and response.json()['data']['passed']
    assert s['vendor'].calls==[(DIRECTORY_PATH,'pending')]
    assert sum(len(q) for q in s['vendor'].queues.values())==2
    catalog=s['client'].get('/api/im/catalog').json()['data']
    assert catalog['workbenches']==[] and catalog['selected'] is None
    assert AUTH.key not in json.dumps(status(s)) and AUTH.sm4_key.decode() not in response.text

def test_explicit_discovery_real_signed_four_pairs_first_selection_and_restart_persist(bootstrap):
    s=bootstrap
    for wb,uids in [('wb-a',['account-a','account-b']),('wb-b',['account-c','account-d'])]:
        for uid in uids:s['seed'](wb,uid)
    assert save(s).status_code==200
    data=discover(s);assert data['selected'] is None
    assert {(w['workbench_id'],a['account_uid']) for w in data['workbenches'] for a in w['accounts']}=={
        ('wb-a','account-a'),('wb-a','account-b'),('wb-b','account-c'),('wb-b','account-d')}
    chosen=select(s,'wb-b','account-c');s['runtime'].tick()
    assert chosen['selected']=={'workbench_id':'wb-b','account_uid':'account-c','version':1}
    assert s['client'].get('/api/im/contacts').json()['data']['contacts'][0]['uin']=='peer-wb-b-account-c'
    with TestClient(s['make_app']()) as reopened:
        reopened.headers['Authorization']='Bearer '+s['token']
        assert reopened.get('/api/im/catalog').json()['data']['selected']==chosen['selected']
        assert reopened.get('/api/im/contacts').json()['data']['contacts'][0]['uin']=='peer-wb-b-account-c'
        assert reopened.get('/api/im/mq-config').json()['data']['configured']

def test_initial_blank_credentials_are_rejected_without_a_profile_or_directory(bootstrap):
    s=bootstrap;response=save(s,config_yaml='')
    assert response.status_code in (400,422) and not s['connection'].exists()
    assert s['repository'].binding_for(OWNER) is None and s['vendor'].calls==[]
    assert s['client'].get('/api/im/catalog').json()['data']['workbenches']==[]

def test_duplicate_save_keeps_same_runtime_and_no_duplicate_remote_consumer(bootstrap):
    s=bootstrap;assert save(s).status_code==200
    runtime=s['runtime'];directory=runtime.directory;revision=status(s)['revision'];before=list(s['vendor'].calls)
    assert save(s,config_yaml='').status_code==200
    assert runtime is s['app'].state.institution and runtime.directory is directory
    assert status(s)['revision']==revision and s['vendor'].calls==before
    s['seed']('wb-a','account-a');s['vendor'].calls.clear()
    assert len(discover(s)['workbenches'])==1
    assert s['vendor'].calls.count((DIRECTORY_PATH,'download'))==1
    assert s['client'].get('/api/im/catalog').json()['data']['selected'] is None

def test_other_login_shares_one_institution_runtime_without_binding_or_second_consumer(bootstrap):
    s=bootstrap;assert save(s).status_code==200;runtime=s['runtime'];before=list(s['vendor'].calls)
    token=s['app'].state.auth.issue_token(AuthUser('other-owner','other-owner'))
    s['client'].headers['Authorization']='Bearer '+token
    assert save(s,config_yaml='').status_code==200
    assert s['app'].state.institution is runtime and runtime._thread is None
    assert s['vendor'].calls==before and s['repository'].binding_for('other-owner') is None

def test_protocol_keys_are_derived_from_institution_auth_without_private_protocol_environment(bootstrap):
    s=bootstrap
    for key in ('XUNJIA_SEAL_KEY','XUNJIA_SIGN_KEY'):s['monkeypatch'].delenv(key,raising=False)
    s['seed']('wb-real','account-real');assert save(s).status_code==200
    assert discover(s)['workbenches'][0]['workbench_id']=='wb-real'
    runtime=s['runtime'];expected=InstitutionKeys.from_auth(AUTH)
    assert runtime.keys==expected and runtime.auth==AUTH
    assert not any('seal_key' in x or 'sign_key' in x for x in runtime.config)

def test_real_new_publication_updates_selected_contacts_and_stale_badge_without_fake_cache_time(bootstrap):
    s=bootstrap;s['seed']('wb-a','account-a');assert save(s).status_code==200;discover(s);select(s);s['runtime'].tick()
    before=s['client'].get('/api/im/contacts').json()['data'];selected=s['repository'].binding_for(OWNER)
    s['advance'](61);assert not s['runtime'].online('wb-a','account-a')
    s['seed']('wb-a','account-a');s['runtime'].tick()
    fresh=s['client'].get('/api/im/contacts').json()['data']
    assert fresh['updated_at']>before['updated_at'] and fresh['friends_updated_at']>before['friends_updated_at']
    assert not fresh['stale'] and s['repository'].binding_for(OWNER)['version']==selected['version']
    with s['repository'].db.transaction(write=False) as tx:assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']==0

def test_foreign_institution_publications_never_become_unbound_users_candidates(bootstrap):
    s=bootstrap;assert save(s).status_code==200
    foreign=InstitutionKeys.from_auth(GatewayAuth('foreign-synthetic',b'fedcba9876543210'))
    binding=foreign.binding('foreign-workbench','directory');now=s['now']
    payload={'contract':CONTRACT,'op':'directory','revision':1,'updated_at':now,'accounts':[]}
    raw=Codec(binding,clock=lambda:now).pack(envelope(binding,'manifest',payload,message_id='foreign-directory',created_at=now,expires_at=now+120))
    s['vendor'].client(DIRECTORY_PATH).upload('foreign.bin',raw)
    assert discover(s)['workbenches']==[] and s['repository'].binding_for(OWNER) is None
    with s['runtime'].directory.db() as db:assert db.execute("SELECT COUNT(*) FROM ip_input WHERE state='QUARANTINED'").fetchone()[0]==1

def test_saved_credentials_and_profile_references_are_private_and_unchanged_after_reload_reads(bootstrap):
    s=bootstrap;assert save(s).status_code==200
    secrets=[p for p in s['tmp'].glob('institution*') if p.is_file()]
    assert secrets and all(p.stat().st_mode & 0o077==0 for p in secrets)
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in secrets}
    for _ in range(2):status(s);s['client'].get('/api/im/catalog')
    assert {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in secrets}==before
    assert not any(OWNER in p.parts for p in s['runtime'].root.rglob('*'))

def test_unbound_links_projection_is_empty_and_never_a_fixed_placeholder_account(bootstrap):
    s=bootstrap;response=s['client'].get('/api/im/links');assert response.status_code==200,response.text
    data=response.json()['data'];assert data['links']==[] and not data.get('active_id')
    assert s['repository'].binding_for(OWNER) is None

def test_http_is_rejected_even_with_retired_opt_in(bootstrap):
    s=bootstrap;response=save(s,base_url=BASE.replace('https:','http:'))
    assert response.status_code in (400,422) and not status(s)['configured'] and s['vendor'].calls==[]
    s['monkeypatch'].setenv('XUNJIA_MQ_ALLOW_HTTP','1')
    response=save(s,base_url=BASE.replace('https:','http:'))
    assert response.status_code==400 and 'FILE_TLS_HTTPS_REQUIRED' in response.text
    assert not status(s)['configured'] and s['vendor'].calls==[]

def test_direct_institution_has_private_auth_derived_keys_and_no_local_gateway(bootstrap):
    s=bootstrap;assert save(s).status_code==200;runtime=s['runtime']
    config=json.loads(s['connection'].read_text());auth=s['connection'].parent/config['auth_file']
    assert GatewayAuth.from_file(auth)==AUTH and auth.stat().st_mode & 0o077==0
    assert runtime.keys==InstitutionKeys.from_auth(AUTH) and not hasattr(s['app'].state,'managed_mq')
    assert not {'uplink_url','downlink_url','path','owner_user_id','account_uid'} & config.keys()
    assert AUTH.key not in json.dumps(status(s)) and AUTH.sm4_key.decode() not in json.dumps(status(s))

def test_one_institution_worker_follows_new_packets_without_manual_book_read(bootstrap):
    s=bootstrap;assert save(s).status_code==200;runtime=s['runtime']
    runtime.background_enabled=True;runtime.start();worker=runtime._thread
    try:
        runtime.start();assert runtime._thread is worker and worker.is_alive()
        s['seed']('wb-a','account-a')
        wait_for(lambda:runtime.directory.account('wb-a','account-a') is not None)
        assert s['client'].get('/api/im/catalog').json()['data']['workbenches'][0]['workbench_id']=='wb-a'
        assert s['repository'].binding_for(OWNER) is None
    finally:runtime.close()

def test_institution_worker_restores_directory_after_restart_without_refresh(bootstrap):
    s=bootstrap;assert save(s).status_code==200;s['seed']('wb-a','account-a');discover(s)
    s['runtime'].close();app=s['make_app']();runtime=app.state.institution;runtime.background_enabled=True
    try:
        runtime.start();s['seed']('wb-b','account-b')
        wait_for(lambda:runtime.directory.account('wb-b','account-b') is not None)
        with TestClient(app) as api:
            api.headers['Authorization']='Bearer '+s['token']
            data=api.get('/api/im/catalog').json()['data']
            assert {w['workbench_id'] for w in data['workbenches']}=={'wb-a','wb-b'} and data['selected'] is None
    finally:runtime.close()

def test_subscription_preserves_old_unknown_and_continues_new_authenticated_directory(bootstrap):
    s=bootstrap;assert save(s).status_code==200;directory=s['runtime'].directory
    with directory.db() as db:
        seq=db.execute("INSERT INTO ip_input(path,state,name,at) VALUES(?,'UNKNOWN','old-uncertain',?)",(DIRECTORY_PATH,s['now'])).lastrowid
        before=tuple(db.execute('SELECT * FROM ip_input WHERE seq=?',(seq,)).fetchone())
    s['seed']('wb-a','account-a');data=discover(s)
    assert not data['refresh_error'] and data['workbenches'][0]['workbench_id']=='wb-a'
    calls=list(s['vendor'].calls)
    for _ in range(2):assert s['client'].get('/api/im/catalog').json()['data']['workbenches'][0]['workbench_id']=='wb-a'
    assert s['vendor'].calls==calls
    with directory.db() as db:assert tuple(db.execute('SELECT * FROM ip_input WHERE seq=?',(seq,)).fetchone())==before


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
        'XUNJIA_STATE_DIR': '', 'XUNJIA_INSTITUTION_CONFIG': '',
        'XUNJIA_RUN_BACKGROUND': '0',
        'XUNJIA_SEAL_KEY': base64.b64encode(b'b'*32).decode(), 'XUNJIA_SIGN_KEY': base64.b64encode(b'd'*32).decode()}
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
    assert not env.get('XUNJIA_TRANSPORT_CONFIG') and not env.get('XUNJIA_MQ_CONNECTION_CONFIG')
    assert captured['args'][1:5] == ['-m', 'uvicorn', 'server.app:create_app', '--factory']
    for key in list(os.environ):
        if key.startswith('XUNJIA_') and key not in env:monkeypatch.delenv(key,raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Same real application factory that the standard launcher would exec;
    # only the explicitly supported DB DI uses temporary SQLite instead of a real vendor DB.
    app = create_app(database_url='sqlite:///' + str(prefix / 'business.db'),
        quote_control=QuoteControl(directory=prefix/'quote'),
        auth_config=AuthConfig(sso_secret='synthetic-bootstrap-sso', session_secret='q' * 40),
        enable_background=False)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + app.state.auth.issue_token(AuthUser(OWNER, OWNER))
        response = client.get('/api/im/mq-config')
        assert response.status_code == 200, response.text
        assert not response.json()['data']['configured']
        assert app.state.institution is not None and app.state.institution.path==prefix/'state'/'institution-connection.json'
        assert not hasattr(app.state,'managed_mq')

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
