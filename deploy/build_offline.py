"""Build a clean, content-addressed application package. Never copy filled site config."""
from __future__ import annotations
import argparse
import ast
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
from common import DeployError, read_env, sha256
def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')
def template_is_clean(path):
    values=read_env(path,require_private=False)
    # Whitelist allowed initialized values, never reuse local xunjia.env.
    if 'REPLACE_DATABASE_PASSWORD' not in values.get('XUNJIA_DATABASE_URL',''):
        raise DeployError('TEMPLATE_DATABASE_SECRET_PRESENT')
    secrets=('XUNJIA_DIFY_APP_KEY','XUNJIA_DIFY_ANALYSIS_APP_KEY','XUNJIA_DIFY_REPLY_APP_KEY','XUNJIA_SSO_SECRET','XUNJIA_AUTH_SECRET',
             'XUNJIA_AGENT_SERVICE_TOKEN','QB_MYSQL_PASSWORD',
             'XUNJIA_SEAL_KEY','XUNJIA_SIGN_KEY')
    if any(values.get(key) for key in secrets): raise DeployError('TEMPLATE_CONTAINS_SECRET')
    known={'XUNJIA_DATABASE_URL','XUNJIA_HOST','XUNJIA_PORT','XUNJIA_WEB_DIST','XUNJIA_STATE_DIR',
           'XUNJIA_RUN_BACKGROUND','XUNJIA_QUOTE_SOURCE',
           'XUNJIA_INSTITUTION_CONFIG','XUNJIA_MQ_ALLOW_HTTP','XUNJIA_DIFY_URL','XUNJIA_ALLOWED_ORIGINS',
           'XUNJIA_AGGREGATION_PROFILE','XUNJIA_REFERENCE_CONFIG',
           'XUNJIA_BOND_CONTEXT_MESSAGES','XUNJIA_DIFY_STAGE_BUDGET_SECONDS','XUNJIA_DIFY_TIMEOUT_SECONDS',
           'XUNJIA_VALUATION_REFRESH_SECONDS','XUNJIA_AUTH_DEV_LOGIN','XUNJIA_DEV_USER_NAME','XUNJIA_DEV_NICK_NAME',
           'QB_CONFIG_DIR','QB_MYSQL_HOST','QB_MYSQL_PORT','QB_MYSQL_USER','QB_MYSQL_DB',
           'XUNJIA_QUOTE_CACHE','XUNJIA_QUOTE_AUTOSTART',*secrets}
    if set(values)-known:raise DeployError('UNKNOWN_TEMPLATE_FIELD')
    if values.get('XUNJIA_RUN_BACKGROUND')!='1':
        raise DeployError('TEMPLATE_BACKGROUND_DISABLED')
    if any(values.get(k,'0')!='0' for k in ('XUNJIA_AUTH_DEV_LOGIN','XUNJIA_QUOTE_AUTOSTART')):
        raise DeployError('TEMPLATE_AUTOMATION_ENABLED')
    return values
def input_files(root):
    files={}
    for module in ('server','relay','collector'):
        for source in sorted((root/module).rglob('*')):
            if source.suffix=='.py' or module=='collector' and source.suffix=='.sql':
                if source.is_file() and '__pycache__' not in source.parts:
                    files['app/'+source.relative_to(root).as_posix()]=source
    for folder,exts in [('deploy',{'.py','.sh','.md','.example'}),('scripts',{'.py'}),
                        ('sql',{'.sql','.md'}),('dify',{'.py','.yml','.yaml','.json','.md','.txt','.lock'})]:
        for source in sorted((root/folder).rglob('*')):
            if source.is_file() and source.suffix in exts and '__pycache__' not in source.parts:
                # Preserve the concurrent, unreviewed local candidate on disk;
                # the measured production A/B package is negotiation_v22.
                files[source.relative_to(root).as_posix()]=source
    collector_source_manifest=root/'deploy/collector-qtrade-v22/manifest.json'
    if collector_source_manifest.is_file():
        files['deploy/collector-qtrade-v22/manifest.json']=collector_source_manifest
    for name in ('requirements.lock','requirements-dev.lock','config/xunjia.env.example',
                 'config/README.md','config/institution-connection.example.json'):
        files[name]=root/name
    measured=root/'config/aggregation-profile.measured.json'
    provenance=root/'config/aggregation-measurement.json'
    if measured.is_file()!=provenance.is_file():
        raise DeployError('AGGREGATION_EVIDENCE_MISSING')
    if measured.is_file():
        from server.negotiation.settings import Aggregation
        try:
            Aggregation(**json.loads(measured.read_text(encoding='utf8')))
            evidence=json.loads(provenance.read_text(encoding='utf8'))
            if not isinstance(evidence,dict) or evidence.get('profile_sha256')!=sha256(measured):
                raise ValueError('Measured profile bytes differ from their provenance')
        except (TypeError,ValueError) as exc:
            raise DeployError('INVALID_MEASURED_AGGREGATION_PROFILE',str(exc)) from exc
        for source in (measured,provenance):files[source.relative_to(root).as_posix()]=source
    for source in sorted((root/'web/dist').rglob('*')):
        if source.is_file():files['frontend/dist/'+source.relative_to(root/'web/dist').as_posix()]=source
    # The deployable regression suite intentionally does NOT need web/src, Node or esbuild.
    files['tests/conftest.py']=root/'tests/conftest.py'
    for source in sorted((root/'tests/release').glob('*.py')):
        files['tests/release/'+source.name]=source
    for folder in ('cutover','negotiation','benchmarks'):
        for source in sorted((root/'tests'/folder).glob('*.py')):
            files['tests/'+folder+'/'+source.name]=source
    for name in ('test_oceanbase_schema_sql_20261015.py',
                 'test_public_market_tools_20261016.py','test_sts_bucket_decoupling_20261015.py'):
        files['tests/acceptance/'+name]=root/'tests/acceptance'/name
    return files
def render_package(output:Path,wheels:Path,arm_runtime:Path|None=None,collector_wheel:Path|None=None):
    if output.exists() or output.with_name(output.name+'.sha256').exists():raise DeployError('OUTPUT_ALREADY_EXISTS')
    template_is_clean(ROOT/'config/xunjia.env.example')
    if not (ROOT/'web/dist/index.html').is_file():raise DeployError('FRONTEND_BUILD_MISSING')
    sys.path.insert(0,str(ROOT))
    from scripts.export_oceanbase_schema import render,SQL_FILE
    if SQL_FILE.read_text(encoding='utf8')!=render():raise DeployError('SQL_SOURCE_DRIFT')
    files=input_files(ROOT)
    if collector_wheel is None or not collector_wheel.is_file():raise DeployError('COLLECTOR_RUNTIME_WHEEL_MISSING')
    collector_manifest=collector_wheel.parent/'collector-runtime-manifest.json'
    evidence=json.loads(collector_manifest.read_text(encoding='utf8'))
    if evidence.get('sha256')!=sha256(collector_wheel):raise DeployError('COLLECTOR_RUNTIME_HASH_MISMATCH')
    with zipfile.ZipFile(collector_wheel) as wheel:
        for name,digest in evidence['source_hashes'].items():
            if name.startswith('/') or '..' in Path(name).parts or sha256(ROOT/name)!=digest:
                raise DeployError('COLLECTOR_RUNTIME_SOURCE_DRIFT')
            if hashlib.sha256(wheel.read(name)).hexdigest()!=digest:
                raise DeployError('COLLECTOR_RUNTIME_PAYLOAD_DRIFT')
    files['collector-runtime/'+collector_wheel.name]=collector_wheel
    files['collector-runtime/collector-runtime-manifest.json']=collector_manifest
    for arch in ('x86_64','aarch64','common'):
        candidates=sorted((wheels/arch).glob('*.whl'))
        if not candidates:raise DeployError('WHEELHOUSE_MISSING',arch)
        for f in candidates:files['wheels/'+arch+'/'+f.name]=f
    if arm_runtime:
        if not arm_runtime.is_file():raise DeployError('ARM_RUNTIME_MISSING')
        files['runtime/python-runtime-aarch64.tar.gz']=arm_runtime
    snapshot={}
    for rel,source in files.items():
        if source.is_symlink() or not source.is_file():raise DeployError('INPUT_FILE_INVALID',rel)
        if rel.endswith('.py'):ast.parse(source.read_text(encoding='utf8'),filename=rel)
        snapshot[rel]=sha256(source)
    build_id=hashlib.sha256(canonical(snapshot)).hexdigest()
    git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    name='xunjia-agent-batch1-'+build_id[:16]
    version={'deployment_layout':2,'build_id':build_id,'source_commit':git_commit,
             'source_worktree_included':True,'created_utc':datetime.now(timezone.utc).isoformat(),
             'mutable_config':'config/xunjia.env (excluded from immutable checksum)',
             'shared_site_config':'PREFIX/config','shared_state':'PREFIX/state',
             'release_switch':'single current symlink with durable journal and process lock',
             'archive_tests':'tests/release, tests/cutover, tests/negotiation, tests/benchmarks and selected acceptance; no Node required',
             'python':'3.12','minimum_glibc':'2.28','architectures':['x86_64','aarch64'],
             'public_tools_require_token':False,'dify_admin_required':False,
             'collector_runtime':'collector-runtime/'+collector_wheel.name,
             'customer_database_ddl_executed_by_installer':False,
             'live_chain_e2e':'not performed / not claimed'}
    with tempfile.TemporaryDirectory(prefix='xunjia-build-batch1-') as tmp:
        stage=Path(tmp)/name;stage.mkdir()
        for rel,source in files.items():
            target=stage/rel;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            target.chmod(0o755 if source.suffix=='.sh' else 0o644)
            if sha256(target)!=snapshot[rel]:raise DeployError('SOURCE_CHANGED_DURING_BUILD',rel)
        # Never read, change, or upload root/config/xunjia.env. It may contain real secrets.
        shutil.copyfile(stage/'config/xunjia.env.example',stage/'config/xunjia.env')
        (stage/'config/xunjia.env').chmod(0o600)
        (stage/'VERSION.json').write_text(json.dumps(version,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        (stage/'SOURCE-HASHES.json').write_bytes(canonical(snapshot)+b'\n')
        with (stage/'SHA256SUMS.txt').open('w',encoding='utf8') as stream:
            for p in sorted(stage.rglob('*')):
                if p.is_file() and p.name!='SHA256SUMS.txt' and p!=stage/'config/xunjia.env':
                    stream.write(sha256(p)+'  '+p.relative_to(stage).as_posix()+'\n')
        for rel,source in files.items():
            if sha256(source)!=snapshot[rel]:raise DeployError('SOURCE_CHANGED_DURING_BUILD',rel)
        for p in stage.rglob('*'):
            if p.is_file() and ('__pycache__' in p.parts or p.suffix in ('.pyc','.pyo','.db','.sqlite','.log','.key','.pem')):
                raise DeployError('DIRTY_BUILD_PAYLOAD',p.name)
        output.parent.mkdir(parents=True,exist_ok=True)
        temp=output.with_name(output.name+'.incomplete')
        if temp.exists():raise DeployError('INCOMPLETE_OUTPUT_EXISTS')
        try:
            with temp.open('xb') as raw, gzip.GzipFile(fileobj=raw,mode='wb',filename='',mtime=0) as zipped:
                with tarfile.open(fileobj=zipped,mode='w',format=tarfile.PAX_FORMAT) as archive:
                    for p in [stage]+sorted(stage.rglob('*')):
                        info=archive.gettarinfo(str(p),arcname=p.relative_to(stage.parent).as_posix())
                        info.uid=info.gid=0;info.uname=info.gname='root';info.mtime=0
                        if info.isfile():
                            with p.open('rb') as data:archive.addfile(info,data)
                        elif info.isdir():archive.addfile(info)
                        else:raise DeployError('BUILD_LINK_FORBIDDEN')
            temp.replace(output)
        except BaseException:
            temp.unlink(missing_ok=True);raise
    digest=sha256(output)
    output.with_name(output.name+'.sha256').write_text(digest+'  '+output.name+'\n',encoding='ascii')
    print(json.dumps({'archive':str(output),'sha256':digest,'bytes':output.stat().st_size,
                      'build_id':build_id,'source_files':len(snapshot)},ensure_ascii=False))
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--wheels',type=Path,required=True)
    p.add_argument('--arm-runtime',type=Path)
    p.add_argument('--collector-wheel',type=Path,required=True)
    args=p.parse_args();render_package(args.output,args.wheels,args.arm_runtime,args.collector_wheel)
if __name__=='__main__':main()
