"""Build a clean, content-addressed application package. Never copy filled site config."""
from __future__ import annotations
import argparse
import ast
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from email.parser import BytesParser
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
           'XUNJIA_INSTITUTION_CONFIG','XUNJIA_EGRESS_GATEWAY','XUNJIA_MQ_ALLOW_HTTP','XUNJIA_DIFY_URL','XUNJIA_ALLOWED_ORIGINS',
           'XUNJIA_AGGREGATION_PROFILE','XUNJIA_REFERENCE_CONFIG',
           'XUNJIA_BOND_CONTEXT_MESSAGES','XUNJIA_DIFY_STAGE_BUDGET_SECONDS','XUNJIA_DIFY_TIMEOUT_SECONDS',
           'XUNJIA_VALUATION_REFRESH_SECONDS','XUNJIA_AUTH_DEV_LOGIN','XUNJIA_DEV_USER_NAME','XUNJIA_DEV_NICK_NAME',
           'QB_CONFIG_DIR','QB_MYSQL_HOST','QB_MYSQL_PORT','QB_MYSQL_USER','QB_MYSQL_DB',
           'QB_DICTIONARY_SOURCE','QB_DICTIONARY_CONFIG','QB_FE_VER',
           'XUNJIA_QUOTE_CACHE','XUNJIA_QUOTE_AUTOSTART','XUNJIA_QT_CONFIG',*secrets}
    if set(values)-known:raise DeployError('UNKNOWN_TEMPLATE_FIELD')
    if values.get('XUNJIA_RUN_BACKGROUND')!='1':
        raise DeployError('TEMPLATE_BACKGROUND_DISABLED')
    if any(values.get(k,'0')!='0' for k in ('XUNJIA_AUTH_DEV_LOGIN','XUNJIA_QUOTE_AUTOSTART')):
        raise DeployError('TEMPLATE_AUTOMATION_ENABLED')
    return values
def validate_qt_template(path):
    try:data=json.loads(path.read_text(encoding='utf8'))
    except (ValueError,OSError):raise DeployError('QT_TEMPLATE_INVALID') from None
    if not isinstance(data,dict) or data.get('mode')!='fix' or data.get('enabled') is not False:
        raise DeployError('QT_TEMPLATE_INVALID')
    private=('host','username','password','sender_comp_id','target_comp_id','server_name','ca_file','client_cert_file','client_key_file')
    if any(data.get(key) for key in private) or data.get('account_owners')!={}:
        raise DeployError('QT_TEMPLATE_CONTAINS_SITE_CONFIG')

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
    # The mount entrypoint requires these exact templates. Do not package arbitrary
    # operator/site .conf files from the checkout.
    if (root/'deploy/mount_xunjia.py').is_file():
        for name in ('nginx-xunjia-map.conf','nginx-xunjia-locations.conf'):
            source=root/'deploy'/name
            if source.is_symlink() or not source.is_file():
                raise DeployError('XUNJIA_MOUNT_CONFIG_MISSING','deploy/'+name)
            files['deploy/'+name]=source
    collector_source_manifest=root/'deploy/collector-qtrade-v22/manifest.json'
    if collector_source_manifest.is_file():
        files['deploy/collector-qtrade-v22/manifest.json']=collector_source_manifest
    for name in ('requirements.lock','requirements-dev.lock','config/xunjia.env.example',
                 'config/README.md','config/institution-connection.example.json'):
        files[name]=root/name
    auth_template=root/'config/institution-auth.example.yaml'
    if auth_template.is_file():files['config/institution-auth.example.yaml']=auth_template
    for name in ('bond-dictionary.mysql.example.json','bond-dictionary.file.example.json','qt-connection.example.json'):
        path=root/'config'/name
        if path.is_file():
            if name=='qt-connection.example.json':validate_qt_template(path)
            files['config/'+name]=path
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
    for name in ('conftest.py','institution_test_support.py','tls_material.py'):
        files['tests/'+name]=root/'tests'/name
    for source in sorted((root/'tests/release').glob('*.py')):
        files['tests/release/'+source.name]=source
    for folder in ('cutover','negotiation','benchmarks'):
        for source in sorted((root/'tests'/folder).glob('*.py')):
            files['tests/'+folder+'/'+source.name]=source
    for name in ('test_oceanbase_schema_sql_20261015.py',
                 'test_public_market_tools_20261016.py'):
        files['tests/acceptance/'+name]=root/'tests/acceptance'/name
    return files
def validate_quote_runtime(files):
    # Validate the payload itself; imports from the build checkout can hide omissions.
    required=('__init__.py','managed_session.py','qb_gateway.py','qb_sign.py',
              'qb_ws.py','qb_apollo.py','pb_raw.py','store.py')
    for name in required:
        path=files.get('app/collector/'+name)
        if path is None or not path.is_file() or path.is_symlink():
            raise DeployError('QUOTE_RUNTIME_MISSING','app/collector/'+name)
    if 'app/server/qt_control.py' in files:
        for name in ('app/collector/qtrade_fix.py','app/server/qt_credentials.py','app/server/qt_quotes.py','config/qt-connection.example.json'):
            path=files.get(name)
            if path is None or not path.is_file() or path.is_symlink():raise DeployError('QT_RUNTIME_MISSING',name)
def validate_arm_runtime(path):
    expected='c2083943c86dbb21ca0211238362fd922de7b0475688f26c135cf5d20a1c2f48'
    if path is None or path.is_symlink() or not path.is_file():raise DeployError('ARM_RUNTIME_MISSING')
    if sha256(path)!=expected:raise DeployError('ARM_RUNTIME_UNREVIEWED')

def validate_auth_template(path):
    if not path.is_file():return
    import yaml
    try:
        node=yaml.compose(path.read_text(encoding='utf8'))
        if not isinstance(node,yaml.nodes.MappingNode) or len(node.value)!=2:
            raise ValueError()
        if any(not isinstance(part,yaml.nodes.ScalarNode) or part.tag!='tag:yaml.org,2002:str'
               for pair in node.value for part in pair):raise ValueError()
        values={key.value:value.value for key,value in node.value}
    except (OSError,ValueError,yaml.YAMLError):raise DeployError('AUTH_TEMPLATE_INVALID') from None
    if values!={'key':'REPLACE_INSTITUTION_KEY','sm4_key':'REPLACE_16_BYTES'}:
        raise DeployError('AUTH_TEMPLATE_CONTAINS_SECRET_OR_UNKNOWN_FIELD')

def bootstrap_tool_files(directory):
    directory=directory.resolve()
    manifest=json.loads((directory/'redistribution-files.json').read_text(encoding='utf8'))
    files={}
    for item in manifest['files']:
        name=item['package_path']
        source=directory/Path(item['source']).name
        if name=='runtime/busybox-aarch64':name='runtime/tools/busybox-aarch64'
        elif not name.startswith('third-party/busybox-static/'):raise DeployError('BOOTSTRAP_TOOL_PATH_INVALID')
        if (Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name or
                source.is_symlink() or not source.is_file() or sha256(source)!=item['sha256'] or name in files):
            raise DeployError('BOOTSTRAP_TOOL_INVALID')
        files[name]=source
    binary=files.get('runtime/tools/busybox-aarch64')
    if binary is None or sha256(binary)!='7d93682be37cf6ed46699f6fff546b80bf133cf8ec342c2d04bc23387f78bc34':
        raise DeployError('BOOTSTRAP_TOOL_UNREVIEWED')
    for name in ('COPYRIGHT.debian','LICENSE.GPL-2.0','SOURCE-AND-LICENSE.txt'):
        if 'third-party/busybox-static/'+name not in files:raise DeployError('BOOTSTRAP_TOOL_LICENSE_MISSING')
    return files
def production_wheels(directory:Path, lock:Path):
    """Select the exact CPython 3.12 ARM64 runtime closure, never development wheels."""
    normalize=lambda name:re.sub(r'[-_.]+','-',name).lower()
    required={}
    for line in lock.read_text(encoding='utf8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)',line)
        if not match or normalize(match[1]) in required:raise DeployError('PRODUCTION_LOCK_INVALID')
        required[normalize(match[1])]=match[2]
    if not required:raise DeployError('PRODUCTION_LOCK_EMPTY')
    found={name:[] for name in required}
    for path in sorted(directory.glob('*.whl')):
        if path.is_symlink():raise DeployError('INPUT_FILE_INVALID',path.name)
        parts=path.stem.split('-')
        if len(parts) not in (5,6):raise DeployError('WHEEL_NAME_INVALID',path.name)
        name,version=normalize(parts[0]),parts[1]
        if name not in required:continue
        python,abi,platform=parts[-3:]
        pure=python in ('py3','py2.py3') and abi=='none' and platform=='any'
        arm=all(re.fullmatch(r'manylinux(?:_2_(?:17|28)|2014)_aarch64',p) for p in platform.split('.'))
        compatible=python=='cp312' and abi in ('cp312','abi3') or (
            re.fullmatch(r'cp3[0-9]{1,2}',python) and 7<=int(python[3:])<=12 and abi=='abi3')
        if version!=required[name] or not (pure or arm and compatible):
            raise DeployError('PRODUCTION_WHEEL_INCOMPATIBLE',path.name)
        with zipfile.ZipFile(path) as archive:
            metadata=[p for p in archive.namelist() if p.endswith('.dist-info/METADATA')]
            if len(metadata)!=1:raise DeployError('WHEEL_METADATA_INVALID',path.name)
            info=BytesParser().parsebytes(archive.read(metadata[0]))
            if normalize(info.get('Name',''))!=name or info.get('Version')!=version:
                raise DeployError('WHEEL_METADATA_INVALID',path.name)
        found[name].append(path)
    if any(len(paths)!=1 for paths in found.values()):raise DeployError('PRODUCTION_WHEEL_MISSING_OR_DUPLICATE')
    return [found[name][0] for name in sorted(found)]

def render_package(output:Path,wheels:Path,arm_runtime:Path|None=None,collector_wheel:Path|None=None,*,minimal_aarch64=False,bootstrap_tools:Path|None=None):
    if output.exists() or output.with_name(output.name+'.sha256').exists():raise DeployError('OUTPUT_ALREADY_EXISTS')
    template_is_clean(ROOT/'config/xunjia.env.example')
    validate_auth_template(ROOT/'config/institution-auth.example.yaml')
    if not (ROOT/'web/dist/index.html').is_file():raise DeployError('FRONTEND_BUILD_MISSING')
    sys.path.insert(0,str(ROOT))
    from deploy.frontend_build import verify_frontend_build
    from scripts.export_oceanbase_schema import render,SQL_FILE
    if SQL_FILE.read_text(encoding='utf8')!=render():raise DeployError('SQL_SOURCE_DRIFT')
    files=input_files(ROOT)
    if minimal_aarch64:
        validate_arm_runtime(arm_runtime)
        files={name:path for name,path in files.items() if 'tests' not in Path(name).parts
               and name not in ('requirements-dev.lock','deploy/05-test-package.sh','deploy/package_tests.py')}
        files['app/requirements.lock']=ROOT/'requirements.lock'
    if bootstrap_tools is not None:files.update(bootstrap_tool_files(bootstrap_tools))
    validate_quote_runtime(files)
    frontend=verify_frontend_build(ROOT/'web')
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
    for arch in (('aarch64','common') if minimal_aarch64 else ('x86_64','aarch64','common')):
        candidates=(production_wheels(wheels/arch,ROOT/'requirements.lock') if minimal_aarch64 and arch=='aarch64'
                    else sorted((wheels/arch).glob('pip-*-py3-none-any.whl' if minimal_aarch64 else '*.whl')))
        if not candidates:raise DeployError('WHEELHOUSE_MISSING',arch)
        if minimal_aarch64 and arch=='common' and len(candidates)!=1:raise DeployError('PIP_BOOTSTRAP_AMBIGUOUS')
        for f in candidates:files['wheels/'+arch+'/'+f.name]=f
    if arm_runtime:
        if not arm_runtime.is_file():raise DeployError('ARM_RUNTIME_MISSING')
        files['runtime/python-runtime-aarch64.tar.gz']=arm_runtime
    snapshot={}
    for rel,source in files.items():
        if source.is_symlink() or not source.is_file():raise DeployError('INPUT_FILE_INVALID',rel)
        if rel.endswith('.py'):ast.parse(source.read_text(encoding='utf-8-sig'),filename=rel)
        snapshot[rel]=sha256(source)
    build_id=hashlib.sha256(canonical(snapshot)).hexdigest()
    git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    name='xunjia-agent-batch1-'+build_id[:16]
    version={'deployment_layout':2,'build_id':build_id,'source_commit':git_commit,
             'frontend_build':{'version':frontend['version'],'source_hash':frontend['source_hash']},
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
    if minimal_aarch64:
        version.update(package_profile='minimal-aarch64',architectures=['aarch64'],
                       archive_tests='Not included; deploy/06-check-runtime.sh verifies the installed runtime offline',
                       database_deployment='Explicit fresh-install clears only xunjia_business and recreates current schema',
                       customer_database_ddl_executed_by_installer='only with explicit fresh-install --apply')
    version['system_tar_required_for_bootstrap']=bootstrap_tools is None
    with tempfile.TemporaryDirectory(prefix='xunjia-build-batch1-') as tmp:
        stage=Path(tmp)/name;stage.mkdir()
        for rel,source in files.items():
            target=stage/rel;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            target.chmod(0o755 if source.suffix=='.sh' or rel=='runtime/tools/busybox-aarch64' else 0o644)
            if sha256(target)!=snapshot[rel]:raise DeployError('SOURCE_CHANGED_DURING_BUILD',rel)
        if verify_frontend_build(ROOT/'web',stage/'frontend/dist')!=frontend:
            raise DeployError('FRONTEND_CHANGED_DURING_BUILD')
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
        if verify_frontend_build(ROOT/'web')!=frontend:raise DeployError('FRONTEND_CHANGED_DURING_BUILD')
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
    p.add_argument('--minimal-aarch64',action='store_true',help='ARM runtime and production dependencies only; no development tests')
    p.add_argument('--bootstrap-tools',type=Path,help='Verified static ARM extraction tool and corresponding source/license directory')
    args=p.parse_args();render_package(args.output,args.wheels,args.arm_runtime,args.collector_wheel,minimal_aarch64=args.minimal_aarch64,bootstrap_tools=args.bootstrap_tools)
if __name__=='__main__':main()
