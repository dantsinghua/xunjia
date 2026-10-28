"""Offline capsule and upgrade transaction tests: synthetic files, no real service."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import zlib

import pytest
from deploy import build_incremental as builder

ROOT = Path(__file__).resolve().parents[2]

def seal(root, build):
    (root / 'VERSION.json').write_text(json.dumps({'build_id': build}))
    rows = []
    for p in sorted(root.rglob('*')):
        if p.is_file() and p.relative_to(root).as_posix() not in ('SHA256SUMS.txt', 'config/xunjia.env', 'config/private.yaml'):
            rows.append(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(root).as_posix())
    (root / 'SHA256SUMS.txt').write_text('\n'.join(rows) + '\n')

def write(root, name, data):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return p

def sample(root, build, code):
    write(root, 'app/server/schema.py', 'SCHEMA = "unchanged"\n')
    write(root, 'sql/oceanbase/001_xunjia_schema_mysql.sql', '-- unchanged schema\n')
    write(root, 'app/server/app.py', code)
    write(root, 'config/xunjia.env.example', 'EXAMPLE_ONLY=1\n')
    write(root, 'config/xunjia.env', 'SITE_SECRET=synthetic_private_never_ship\n')
    write(root, 'config/private.yaml', 'synthetic-secret-material')
    write(root, 'runtime/python-runtime-aarch64.tar.gz', b'synthetic_unchanged_runtime' * 4000)
    write(root, 'deploy/start.sh', '#!/bin/sh\nexit 0\n')
    for name in ('nginx-xunjia-map.conf','nginx-xunjia-locations.conf'):
        write(root, 'deploy/' + name, (ROOT / 'deploy' / name).read_bytes())
    seal(root, build)
    return root

def load(script):
    spec = importlib.util.spec_from_file_location('capsule_' + uuid.uuid4().hex, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def case(tmp_path):
    old = sample(tmp_path/'old', 'base-one', 'old source\n')
    other = sample(tmp_path/'other', 'base-two', 'other old source\n')
    target = sample(tmp_path/'new', 'target-release', 'new source\n')
    out = tmp_path/'upgrade.py'
    result = builder.build([old, other], target, out)
    return SimpleNamespace(old=old, other=other, target=target, out=out, result=result, module=load(out), root=tmp_path)

@pytest.mark.parametrize('base_name',['old','other'])
def test_all_reviewed_baselines_build_identical_new_bundle_without_private_config(case, base_name):
    m = case.module
    base = getattr(case, base_name)
    before = {p.relative_to(base).as_posix():p.read_bytes() for p in base.rglob('*') if p.is_file()}
    output = m.materialize(base)
    assert m.verify_tree(output,'target-release',m.CAPSULE['manifest_sha256'])
    assert (output/'config/xunjia.env').read_bytes() == (output/'config/xunjia.env.example').read_bytes()
    assert not (output/'config/private.yaml').exists()
    assert 'synthetic_private_never_ship' not in case.out.read_text()
    assert before == {p.relative_to(base).as_posix():p.read_bytes() for p in base.rglob('*') if p.is_file()}
    assert m.materialize(base) == output
    assert (output/'deploy/start.sh').stat().st_mode & 0o111
    assert case.result['reused_files'] > 0
    assert 'runtime/python-runtime-aarch64.tar.gz' not in m.PAYLOAD

@pytest.mark.parametrize('damage',['source','manifest','version','unlisted','link'])
def test_damaged_or_ambiguous_base_refuses_before_destination(case,damage):
    if damage=='source':(case.old/'app/server/app.py').write_text('changed')
    elif damage=='manifest':(case.old/'SHA256SUMS.txt').write_text('bad')
    elif damage=='version':(case.old/'VERSION.json').write_text('{}')
    elif damage=='unlisted':(case.old/'unexpected.py').write_text('not allowed')
    else:(case.old/'config/link').symlink_to(case.root/'outside')
    with pytest.raises(case.module.IncrementalError):case.module.materialize(case.old)
    assert not (case.root/'xunjia-update-target-release').exists()

@pytest.mark.parametrize('name',['../escape','/absolute','a\\b','a/../escape','a\nline'])
def test_capsule_paths_cannot_escape_owned_stage(case,name):
    m=case.module;m.CAPSULE['files'][name]='a'*64;m.CAPSULE['sizes'][name]=1
    with pytest.raises(m.IncrementalError):m.materialize(case.old)

@pytest.mark.parametrize('damage',['corrupt','oversized','trailing_stream'])
def test_payload_has_exact_size_hash_and_single_compression_stream(case,damage):
    m=case.module;name='app/server/app.py'
    raw=b'new source\n'
    if damage=='corrupt':raw=b'bad source\n'
    if damage=='oversized':raw=b'A'*1000000
    blob=zlib.compress(raw)
    if damage=='trailing_stream':blob+=zlib.compress(raw)
    m.PAYLOAD[name]=base64.b64encode(blob).decode()
    with pytest.raises(m.IncrementalError):m.materialize(case.old)

@pytest.mark.parametrize('name',builder.SCHEMA_FILES)
def test_no_ddl_incremental_refuses_any_schema_change(case,name):
    (case.target/name).write_text('changed schema')
    seal(case.target,'target-release')
    with pytest.raises(builder.runtime.IncrementalError,match='SCHEMA_CHANGE'):
        builder.build([case.old],case.target,case.root/'bad.py')
    assert not (case.root/'bad.py').exists()

def test_output_is_never_overwritten(case):
    before=case.out.read_bytes()
    with pytest.raises(builder.runtime.IncrementalError,match='OUTPUT_ALREADY_EXISTS'):
        builder.build([case.old],case.target,case.out)
    assert case.out.read_bytes()==before

def installed(case):
    bundle=case.module.materialize(case.old)
    prefix=case.root/'installed'
    write(prefix,'app/server/app.py','# dedicated app')
    write(prefix,'app/VERSION.json',json.dumps({'build_id':'base-one'}))
    write(prefix,'config/xunjia.env','SITE_SECRET=synthetic_keep_exactly\n')
    write(prefix,'state/journal','UNKNOWN must survive')
    config=case.root/'nginx'
    write(config,'nginx.conf',f'events {{}}\nhttp {{\n include {config}/conf.d/*.conf;\n}}\n')
    write(config,'conf.d/njcb.conf','server {\n listen 80;\n location /api/ { proxy_pass http://127.0.0.1:8000; }\n}\n')
    return bundle,prefix,config

class FakeManager:
    def __init__(self,bundle,prefix):
        self.prefix=prefix;self.service=SimpleNamespace(active=lambda:True);self.calls=[]
    def preflight(self):self.calls.append('preflight');return {'integrity':'verified'}
    def install(self):self.calls.append('install');return {'release_id':'synthetic-new'}
    def current(self):return self.prefix/'releases/synthetic-new'
    def rollback(self):self.calls.append('rollback');return {'status':'rollback_verified'}
    def verify_installed(self):self.calls.append('verify');return {'status':'business_ready'}

@pytest.mark.parametrize('apply',[False,True])
def test_one_click_uses_transactional_install_only_and_preserves_config_state_and_south_bank(case,apply):
    bundle,prefix,config=installed(case);ctl=FakeManager(bundle,prefix)
    before={p:p.read_bytes() for base in (prefix,config) for p in base.rglob('*') if p.is_file()}
    mounts=[]
    result=case.module.deploy(bundle,prefix,apply=apply,manager_factory=lambda *_:ctl,
        mount_function=lambda:mounts.append('mount') or {'status':'synthetic'},nginx_config=config)
    assert ctl.calls==(['preflight','install'] if apply else ['preflight'])
    assert mounts==(['mount'] if apply else [])
    assert all(p.read_bytes()==data for p,data in before.items())
    assert result['database_ddl_executed'] is False and result['dify_publish_required'] is True
    assert result['external_live_acceptance']=='NOT_PERFORMED'

def test_nginx_failure_rolls_back_only_this_app_release(case):
    bundle,prefix,config=installed(case);ctl=FakeManager(bundle,prefix)
    def fail():raise RuntimeError('synthetic nginx failure, its own rollback already completed')
    with pytest.raises(RuntimeError):case.module.deploy(bundle,prefix,apply=True,manager_factory=lambda *_:ctl,mount_function=fail,nginx_config=config)
    assert ctl.calls==['preflight','install','rollback']

def test_rerun_does_not_restart_same_release(case):
    bundle,prefix,config=installed(case);ctl=FakeManager(bundle,prefix)
    (prefix/'app/VERSION.json').write_text(json.dumps({'build_id':'target-release'}))
    result=case.module.deploy(bundle,prefix,apply=True,manager_factory=lambda *_:ctl,mount_function=lambda:{},nginx_config=config)
    assert result['already_installed'] and ctl.calls==['preflight','verify']

def test_other_or_unproven_install_is_not_replaced(case):
    bundle,prefix,config=installed(case)
    (prefix/'app/VERSION.json').write_text(json.dumps({'build_id':'newer-unreviewed-release'}))
    with pytest.raises(case.module.IncrementalError,match='INSTALLED_BUILD_NOT_SUPPORTED'):
        case.module.deploy(bundle,prefix,apply=True,nginx_config=config)
