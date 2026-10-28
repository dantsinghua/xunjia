"""Add /xunjia/ to the existing South Bank HTTP site without replacing its routes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

class MountError(ValueError):pass

def digest(data):return hashlib.sha256(data).hexdigest()

def atomic_write(path, data, original=None):
    temp=path.with_name(path.name+'.xunjia-new')
    if temp.exists()or temp.is_symlink():raise MountError('STAGING_FILE_EXISTS')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
    try:
        with os.fdopen(fd,'wb')as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        if original is not None:
            shutil.copystat(original,temp)
            st=original.stat();os.chown(temp,st.st_uid,st.st_gid)
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()

def plan(config_dir, snippets):
    config_dir=Path(config_dir);site=config_dir/'conf.d/njcb.conf';main=config_dir/'nginx.conf'
    for p in (config_dir,config_dir/'conf.d',site,main):
        if p.is_symlink():raise MountError('CONFIG_SYMLINK_UNREVIEWED')
    before=site.read_bytes();text=before.decode('utf-8')
    if not re.search(r'^\s*include\s+'+re.escape(str(config_dir/'conf.d/*.conf'))+r'\s*;',main.read_text(),re.M):
        raise MountError('HTTP_CONFIG_INCLUDE_NOT_CONFIRMED')
    masked=re.sub(r'''"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|#[^\r\n]*''',
                  lambda m:''.join(c if c in '\r\n' else ' ' for c in m[0]),text)
    starts=list(re.finditer(r'\bserver\s*\{',masked))
    if len(starts)!=1 or not re.search(r'^\s*listen\s+(?:80|0\.0\.0\.0:80|\[::\]:80)(?:\s|;)',masked,re.M):
        raise MountError('UNIQUE_EXISTING_PORT80_SITE_REQUIRED')
    if not re.search(r'proxy_pass\s+http://127\.0\.0\.1:8000(?:/)?\s*;',text):
        raise MountError('EXISTING_SOUTH_BANK_UPSTREAM_NOT_CONFIRMED')
    location=config_dir/'xunjia-locations.conf';mapping=config_dir/'conf.d/xunjia-map.conf'
    include='include '+str(location)+';'
    newline='\r\n'if b'\r\n'in before else'\n'
    if include in text:
        if text.count(include)!=1:raise MountError('DUPLICATE_XUNJIA_INCLUDE')
        after=before
    else:
        if '/xunjia'in text:raise MountError('EXISTING_XUNJIA_ROUTE_UNREVIEWED')
        position=starts[0].end();after=(text[:position]+newline+'    '+include+text[position:]).encode('utf-8')
    files={site:after,mapping:Path(snippets,'nginx-xunjia-map.conf').read_bytes(),location:Path(snippets,'nginx-xunjia-locations.conf').read_bytes()}
    for p,data in files.items():
        if p.is_symlink():raise MountError('TARGET_SYMLINK_UNREVIEWED')
        if p!=site and p.exists()and p.read_bytes()!=data:raise MountError('EXISTING_XUNJIA_SNIPPET_DIFFERS')
    return site,before,files

def nginx_run(binary, main, operation):
    args=[str(binary),'-c',str(main),'-t']if operation=='test'else[str(binary),'-c',str(main),'-s','reload']
    try:r=subprocess.run(args,capture_output=True,text=True,timeout=30)
    except (OSError,subprocess.TimeoutExpired):raise MountError('NGINX_'+operation.upper()+'_FAILED')from None
    if r.returncode:raise MountError('NGINX_'+operation.upper()+'_FAILED')

def http_read(path, host):
    request=urllib.request.Request('http://127.0.0.1'+path,headers={'Host':host})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=8)as response:
        data=response.read(2097153)
        if len(data)>2097152:raise MountError('HTTP_CHECK_TOO_LARGE')
        return response.status,data

def apply(config_dir, snippets, backup_root, runner, check):
    site,before,files=plan(config_dir,snippets)
    runner('test')
    # Check the real backend and preserved root before touching the shared nginx config.
    check('before')
    backup=Path(backup_root)/('mount-'+str(time.time_ns()));backup.mkdir(parents=True,mode=0o700)
    snapshots={p:p.read_bytes()if p.exists()else None for p in files}
    for p,data in snapshots.items():
        if data is not None:shutil.copy2(p,backup/p.name)
    record={'site':str(site),'before_sha256':digest(before),'backup':str(backup),'changed_files':[]}
    try:
        for p,data in files.items():
            if (p.read_bytes()if p.exists()else None)!=snapshots[p]:raise MountError('CONFIG_CHANGED_DURING_PREPARATION')
            if snapshots[p]!=data:
                atomic_write(p,data,p if p.exists()else None);record['changed_files'].append(str(p))
        runner('test');runner('reload');check('after')
    except Exception:
        for p in reversed(list(files)):
            if str(p)not in record['changed_files']:continue
            if not p.is_file()or p.read_bytes()!=files[p]:raise MountError('CONFIG_CHANGED_DURING_APPLY_NO_OVERWRITE')
            if snapshots[p]is None:p.unlink()
            else:atomic_write(p,snapshots[p],backup/p.name)
        runner('test');runner('reload')
        record['status']='ROLLED_BACK';(backup/'RESULT.json').write_text(json.dumps(record,indent=2))
        raise
    record.update(status='XUNJIA_MOUNT_READY',after_sha256=digest(site.read_bytes()))
    (backup/'RESULT.json').write_text(json.dumps(record,indent=2));return record

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    config=Path('/etc/nginx');snippets=Path(__file__).resolve().parent
    site,before,files=plan(config,snippets)
    if not args.apply:
        print(json.dumps({'status':'CHECK_ONLY','site':str(site),'changes':[str(p)for p,d in files.items()if not p.exists()or p.read_bytes()!=d]}));return
    if os.geteuid()!=0:raise MountError('ROOT_REQUIRED_FOR_NGINX_CONFIG')
    sys.path.insert(0,str(snippets))
    from common import verify_bundle
    expected=verify_bundle(snippets.parent)['frontend_build']
    binary=Path(shutil.which('nginx')or'/usr/sbin/nginx').resolve()
    pid=int(Path('/run/nginx.pid').read_text().strip())
    proc=Path('/proc')/str(pid)
    if Path(os.readlink(proc/'exe')).resolve()!=binary:raise MountError('RUNNING_NGINX_BINARY_MISMATCH')
    command=(proc/'cmdline').read_bytes().decode(errors='replace').replace('\0',' ')
    if re.search(r'nginx:\s*(?:worker|cache|helper)',command):raise MountError('NGINX_MASTER_REQUIRED')
    words=shlex.split(command)
    configs=[]
    for i,word in enumerate(words):
        if word=='-p' or word.startswith('-p') and len(word)>2:raise MountError('RUNNING_NGINX_PREFIX_UNREVIEWED')
        if word=='-c':
            if i+1>=len(words):raise MountError('RUNNING_NGINX_CONFIG_MISMATCH')
            configs.append(words[i+1])
        elif word.startswith('-c') and len(word)>2:configs.append(word[2:])
    if not configs:
        info=subprocess.run([str(binary),'-V'],capture_output=True,text=True,timeout=15)
        match=re.search(r'--conf-path=([^\s]+)',info.stdout+' '+info.stderr)
        if info.returncode or not match:raise MountError('RUNNING_NGINX_CONFIG_UNVERIFIED')
        configs=[match[1].strip('\'"')]
    if len(configs)!=1 or Path(configs[0])!=config/'nginx.conf':raise MountError('RUNNING_NGINX_CONFIG_MISMATCH')
    local=Path('/home/xunjia/app/frontend/dist/assets/build-version.json')
    current=json.loads(local.read_text())
    if any(current.get(k)!=expected.get(k) for k in ('version','source_hash')):
        raise MountError('INSTALL_THIS_FRONTEND_BEFORE_MOUNTING')
    version=current['version'];root_hash=None
    def check(phase):
        nonlocal root_hash
        if phase=='before':
            root_hash=digest(http_read('/','158.188.1.200')[1])
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:18770/assets/build-version.json',timeout=8)as r:
                remote=json.load(r)
                if any(remote.get(k)!=expected.get(k) for k in ('version','source_hash')):
                    raise MountError('BACKEND_FRONTEND_VERSION_MISMATCH')
            return
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            try:
                if digest(http_read('/','158.188.1.200')[1])!=root_hash:raise MountError('SOUTH_BANK_ROOT_CHANGED')
                data=json.loads(http_read('/xunjia/assets/build-version.json','158.188.1.200')[1])
                if all(data.get(k)==expected.get(k) for k in ('version','source_hash')):return
            except (OSError,ValueError):pass
            time.sleep(.5)
        raise MountError('PREFIX_HTTP_CHECK_FAILED')
    print(json.dumps(apply(config,snippets,Path('/home/xunjia/state/nginx-mount'),lambda op:nginx_run(binary,config/'nginx.conf',op),check)))

if __name__=='__main__':
    os.umask(0o077)
    try:main()
    except Exception as exc:
        code=str(exc)if isinstance(exc,MountError)and re.fullmatch('[A-Z0-9_]+',str(exc))else'CHECK_FAILED_'+type(exc).__name__
        print(code);raise SystemExit(2)
