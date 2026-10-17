"""Release-only regression: no Node, no esbuild, no customer DB, no Dify/IM calls."""
from __future__ import annotations
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy'))
from common import DeployError, atomic_json, config_fingerprint, read_env, sha256, snapshot_payload, verify_bundle
import manager as dm
from manager import Manager
CONFIG="""XUNJIA_DATABASE_URL='oceanbase://synthetic:local-only@127.0.0.1:39999/xunjia_qa'
XUNJIA_HOST='127.0.0.1'
XUNJIA_PORT='19997'
XUNJIA_SSO_SECRET='synthetic-portal'
XUNJIA_AUTH_SECRET='synthetic-32-byte-session-key-for-qa-only'
XUNJIA_RUN_BACKGROUND='0'
XUNJIA_ENABLE_LIVE_TRANSPORT='0'
XUNJIA_DIFY_URL=''
XUNJIA_DIFY_APP_KEY=''
"""
def write(path,content,mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content,encoding='utf8');path.chmod(mode)
def manifest(bundle):
    lines=[]
    for p in sorted(bundle.rglob('*')):
        if p.is_file() and p.name!='SHA256SUMS.txt' and p!=bundle/'config/xunjia.env':
            lines.append(sha256(p)+'  '+p.relative_to(bundle).as_posix())
    write(bundle/'SHA256SUMS.txt','\n'.join(lines)+'\n')
def bundle(root,label='one'):
    b=root/('bundle-'+label);b.mkdir()
    files={
      'VERSION.json':json.dumps({'deployment_layout':2,'build_id':label}),
      'config/xunjia.env.example':CONFIG,
      'config/xunjia.env':CONFIG,
      'app/server/app.py':"# XUNJIA_DATABASE_URL\nBUILD="+repr(label)+'\n',
      'app/server/__init__.py':'',
      'deploy/manager.py':'# synthetic fixture, never executed\n',
      'deploy/runtime.py':'# synthetic fixture, never executed\n',
      'scripts/export_oceanbase_schema.py':'# never executed\n',
      'sql/oceanbase/001_xunjia_schema_mysql.sql':'-- synthetic; no DDL executed\n',
      'requirements.lock':'# no installs inside this unit test\n',
    }
    for name,text in files.items():write(b/name,text,0o600 if name=='config/xunjia.env' else 0o644)
    (b/'wheels'/dm.platform.machine()).mkdir(parents=True)
    manifest(b);return b
class FakeService:
    def __init__(self):
        self.running=False;self.calls=[];self.boot=uuid.uuid4().hex;self.fail_start=0
    def validate(self):self.calls.append('validate')
    def active(self):return self.running
    def stop(self):self.calls.append('stop');self.running=False
    def start(self):
        self.calls.append('start')
        if self.fail_start:
            self.fail_start-=1;raise DeployError('SERVICE_START_FAILED')
        self.running=True;self.boot=uuid.uuid4().hex
class FastManager(Manager):
    """Use fixture code/venv instead of invoking pip; pointer/config logic is unmodified."""
    def prepare_release(self,txn,version):
        release=Path(txn['target']);release.mkdir(parents=True)
        shutil.copytree(self.bundle/'app',release/'app')
        (release/'app/config').symlink_to(self.prefix/'config')
        write(release/'venv/bin/python','# synthetic interpreter marker\n',0o755)
        self.phase(txn,'STAGED');return release
    def validate_release(self,release):
        if not release.is_relative_to(self.prefix):raise DeployError('ROLLBACK_TARGET_INVALID')
        meta=dm.load_json(release/'RELEASE.json')
        dm.verify_payload(release/'app',meta['payload'])
        if not (release/'venv/bin/python').is_file():raise DeployError('ROLLBACK_VENV_MISSING')
        return meta
@pytest.fixture
def setup(tmp_path,monkeypatch):
    prefix=tmp_path/'site';service=FakeService();b=bundle(tmp_path)
    m=FastManager(b,prefix,service,timeout=0,poll=0)
    def healthy(url):
        if not service.running:raise DeployError('HEALTH_UNAVAILABLE')
        assert ':19997/' in url, 'Health must use the actual config port'
        current=m.current();meta=dm.load_json(current/'RELEASE.json')
        return {'service':'xunjia','ready':True,'build_id':meta['build_id'],
                'release_id':meta['release_id'],'boot_id':service.boot}
    monkeypatch.setattr(dm,'read_health',healthy)
    return m,service,b,healthy
def test_mutable_env_edit_does_not_break_package_integrity(setup):
    m,_,b,_=setup
    write(b/'config/xunjia.env',CONFIG.replace('local-only','operator-filled-value'),0o600)
    assert verify_bundle(b)['build_id']=='one'
    write(b/'config/transport.json','{"value":"local-test-only"}',0o600)
    verify_bundle(b)
def test_immutable_code_edit_is_still_rejected(setup):
    _,_,b,_=setup
    write(b/'app/server/app.py','# corrupted')
    with pytest.raises(DeployError,match='INTEGRITY_MISMATCH'):verify_bundle(b)
def test_literal_config_cannot_execute_shell(tmp_path):
    marker=tmp_path/'MUST_NOT_EXIST'
    p=tmp_path/'xunjia.env'
    write(p,"A='$(touch "+str(marker)+")'\nB='text#literal'\n",0o600)
    values=read_env(p)
    assert '$(touch ' in values['A'] and values['B']=='text#literal'
    assert not marker.exists()
def test_duplicate_config_keys_rejected_without_values(tmp_path):
    p=tmp_path/'xunjia.env';write(p,"A='secret-one'\nA='secret-two'\n",0o600)
    with pytest.raises(DeployError) as err:read_env(p)
    assert err.value.code=='CONFIG_DUPLICATE_KEY'
    assert 'secret' not in str(err.value)
def test_first_install_does_not_start_a_stopped_service(setup):
    m,svc,_,_=setup
    result=m.install()
    assert result['status']=='installed_stopped'
    assert 'start' not in svc.calls and 'stop' not in svc.calls
    assert os.readlink(m.prefix/'app')=='current/app'
    assert os.readlink(m.prefix/'venv')=='current/venv'
    assert (m.prefix/'app/config').resolve()==(m.prefix/'config').resolve()
    assert (m.prefix/'config/xunjia.env').read_text()==CONFIG
def test_update_and_rollback_keep_latest_full_configuration_and_state(setup,tmp_path):
    m,svc,_,_=setup
    first=m.install();old=m.current()
    cfg=m.prefix/'config'
    write(cfg/'transport.json','{"object":"v1"}',0o600)
    write(cfg/'certs/custom.crt','TEST CERTIFICATE NOT REAL',0o600)
    write(m.prefix/'state/marker','DO NOT CHANGE')
    m.bundle=bundle(tmp_path,'two');m.install()
    assert m.current()!=old
    write(cfg/'transport.json','{"object":"after-upgrade"}',0o600)
    write(cfg/'xunjia.env',CONFIG+'LOCAL_SITE_EDIT=latest\n',0o600)
    before=config_fingerprint(cfg)
    m.rollback()
    assert m.current()==old
    assert config_fingerprint(cfg)==before
    assert (m.prefix/'state/marker').read_text()=='DO NOT CHANGE'
    assert 'start' not in svc.calls
def test_missing_previous_release_refuses_before_stopping(setup):
    m,svc,_,_=setup;m.install();svc.running=True
    before=list(svc.calls)
    with pytest.raises(DeployError,match='NO_PREVIOUS_RELEASE'):m.rollback()
    assert svc.calls.count('stop')==before.count('stop') and svc.running
@pytest.mark.parametrize('damage',['app','venv'])
def test_damaged_rollback_target_refused_before_stopping(setup,tmp_path,damage):
    m,svc,_,_=setup;m.install();old=m.current()
    m.bundle=bundle(tmp_path,'two');m.install();svc.running=True
    if damage=='app':write(old/'app/server/app.py','# changed')
    else:(old/'venv/bin/python').unlink()
    stops=svc.calls.count('stop');current=m.current()
    with pytest.raises(DeployError):m.rollback()
    assert m.current()==current and svc.calls.count('stop')==stops
def test_install_and_rollback_share_process_lock(setup):
    m,svc,b,_=setup;m.install()
    other=FastManager(b,m.prefix,svc)
    with m.lock():
        for action in (other.install,other.rollback,other.recover):
            with pytest.raises(DeployError,match='DEPLOY_BUSY'):action()
@pytest.mark.parametrize('fault_phase',['STAGED','LAYOUT_READY','SWITCHING','STARTING'])
def test_install_fault_restores_original_version_and_service(setup,tmp_path,fault_phase):
    m,svc,_,_=setup;m.install();original=m.current();svc.running=True
    m.bundle=bundle(tmp_path,'two');raised=[]
    def fail(phase,*_):
        if phase==fault_phase and not raised:
            raised.append(phase);raise DeployError('SYNTHETIC_FAILURE')
    m.phase_hook=fail
    with pytest.raises(DeployError,match='SYNTHETIC_FAILURE'):m.install()
    assert m.current()==original and svc.running
    assert not m.unfinished()
def test_start_failure_restores_previous_version(setup,tmp_path):
    m,svc,_,_=setup;m.install();original=m.current();svc.running=True
    m.bundle=bundle(tmp_path,'two');svc.fail_start=1
    with pytest.raises(DeployError,match='SERVICE_START_FAILED'):m.install()
    assert m.current()==original and svc.running
    assert not m.unfinished()
def test_same_port_serving_old_process_is_rejected(setup,tmp_path,monkeypatch):
    m,svc,_,healthy=setup;m.install();old=m.current();svc.running=True
    oldmeta=dm.load_json(old/'RELEASE.json')
    m.bundle=bundle(tmp_path,'two')
    def stale(url):
        data=healthy(url)
        if m.current()!=old:
            data.update(build_id=oldmeta['build_id'],release_id=oldmeta['release_id'])
        return data
    monkeypatch.setattr(dm,'read_health',stale)
    with pytest.raises(DeployError,match='HEALTH_WRONG_RELEASE'):m.install()
    assert m.current()==old and svc.running
def test_rollback_failure_restores_the_newer_running_version(setup,tmp_path):
    m,svc,_,_=setup;m.install();m.bundle=bundle(tmp_path,'two');m.install()
    newer=m.current();svc.running=True;svc.fail_start=1
    before=config_fingerprint(m.prefix/'config')
    with pytest.raises(DeployError,match='SERVICE_START_FAILED'):m.rollback()
    assert m.current()==newer and svc.running
    assert config_fingerprint(m.prefix/'config')==before
def test_incomplete_journal_blocks_new_update_and_can_be_recovered(setup,tmp_path):
    m,svc,_,_=setup;m.install();old=m.current()
    m.bundle=bundle(tmp_path,'two');m.install();new=m.current()
    txn={'id':new.name,'operation':'install','previous':str(old),'target':str(new),
         'active_before':False,'service_touched':False,'switched':True,'phase':'STARTING'}
    atomic_json(m.txn_path(txn['id']),txn)
    with pytest.raises(DeployError,match='RECOVERY_REQUIRED'):m.install()
    result=m.recover()
    assert result['status']=='recovered' and m.current()==old and not m.unfinished()
def test_legacy_full_config_migrates_and_old_version_uses_same_shared_config(setup,tmp_path):
    m,svc,_,_=setup
    write(m.prefix/'app/server/app.py',"# XUNJIA_DATABASE_URL\n# legacy\n")
    write(m.prefix/'venv/bin/python','# synthetic',0o755)
    write(m.prefix/'app/config/xunjia.env',CONFIG,0o600)
    write(m.prefix/'app/config/transport.json','{"legacy":true}',0o600)
    write(m.prefix/'app/config/cert.crt','synthetic-cert',0o600)
    m.install()
    assert (m.prefix/'config/transport.json').read_text()=='{"legacy":true}'
    write(m.prefix/'config/transport.json','{"new-edit":true}',0o600)
    m.rollback()
    assert (m.prefix/'app/config/transport.json').read_text()=='{"new-edit":true}'
def test_legacy_partial_move_is_restored(setup,monkeypatch):
    m,svc,_,_=setup
    write(m.prefix/'app/server/app.py',"# XUNJIA_DATABASE_URL\n# legacy\n")
    write(m.prefix/'venv/bin/python','# synthetic',0o755)
    write(m.prefix/'app/config/xunjia.env',CONFIG,0o600)
    original=dm.os.replace;once=[]
    def fail_second(source,dest):
        if Path(source)==m.prefix/'venv' and not once:
            once.append(True);raise OSError('synthetic rename failure')
        return original(source,dest)
    monkeypatch.setattr(dm.os,'replace',fail_second)
    with pytest.raises(OSError):m.install()
    assert (m.prefix/'app/server/app.py').is_file() and (m.prefix/'venv/bin/python').is_file()
    assert (m.prefix/'app/config/xunjia.env').read_text()==CONFIG
    assert not m.unfinished()
def test_public_tool_template_requires_no_token():
    import yaml
    path=ROOT/'dify/chatflow_1171/public-tools.openapi.yml'
    spec=yaml.safe_load(path.read_text(encoding='utf8'))
    assert len(spec['paths'])==4 and not spec.get('security')
def test_other_services_and_unsafe_prefix_rejected(tmp_path):
    from common import safe_service,safe_prefix
    for name in ('njcb-all.service','nginx.service','nanyin_deposit_agent.service'):
        with pytest.raises(DeployError):safe_service(name)
    for prefix in ('/','/home','/tmp','relative','/home/nanyin-agent'):
        with pytest.raises(DeployError):safe_prefix(prefix)