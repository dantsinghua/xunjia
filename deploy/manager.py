"""Transactional, locked, recoverable offline releases. Does not execute database DDL.
Mutable config/state are shared. A single current symlink selects app AND venv.
The only managed service is an explicitly named xunjia-agent*.service.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (DeployError, atomic_json, config_fingerprint, effective_env,
                    fsync_dir, health_url, load_json, read_env, safe_prefix,
                    safe_service, sha256, snapshot_payload, validate_config,
                    verify_bundle, verify_payload)
TERMINAL = {'COMMITTED', 'ABORTED', 'RECOVERED'}
def run(command: list[str], *, env=None, timeout=240) -> str:
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError('SUBPROCESS_UNAVAILABLE', Path(command[0]).name) from None
    if result.returncode:
        # Commands may include application import failures with DSNs. Never echo stderr.
        raise DeployError('SUBPROCESS_FAILED', Path(command[0]).name + ' exit=' + str(result.returncode))
    return result.stdout.strip()
class SystemService:
    def __init__(self, name: str, prefix: Path):
        self.name = safe_service(name)
        self.prefix = prefix
    def exists(self):
        result = subprocess.run(['systemctl', 'show', self.name, '--property=LoadState', '--value'],
                                capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() == 'loaded'
    def active(self):
        # systemctl may return zero after ignoring a request inside a chroot.
        # Only an explicitly loaded unit can be considered active.
        if not shutil.which('systemctl') or not self.exists(): return False
        return subprocess.run(['systemctl','is-active','--quiet',self.name],
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0
    def validate(self):
        if not shutil.which('systemctl'): return
        if not self.exists(): return
        state=run(['systemctl','show',self.name,'--property=ActiveState','--value'],timeout=10)
        if state not in ('active','inactive','failed'):
            raise DeployError('SERVICE_TRANSITION_IN_PROGRESS')
        result = run(['systemctl','show',self.name,'--property=ExecStart','--value'], timeout=15)
        # The stable app path, not an obsolete release path, must own the entrypoint.
        script = str(self.prefix/'app/deploy/start.sh')
        old_entry = str(self.prefix/'venv/bin')
        if script not in result and not (old_entry in result and 'server.app:create_app' in result):
            raise DeployError('SERVICE_ENTRYPOINT_MISMATCH', 'review the dedicated xunjia unit before switching')
        if 'njcb' in result.lower() or 'nanyin' in result.lower():
            raise DeployError('OTHER_SERVICE_FORBIDDEN')
    def main_pid(self):
        text=run(['systemctl','show',self.name,'--property=MainPID','--value'],timeout=10)
        return int(text) if text.isdigit() else 0
    def owns_listener(self,port):
        pid=self.main_pid()
        if not pid:return False
        sockets=set()
        try:
            for fd in (Path('/proc')/str(pid)/'fd').iterdir():
                try:
                    link=os.readlink(fd)
                    if link.startswith('socket:['):sockets.add(link[8:-1])
                except FileNotFoundError:continue
            for name in ('tcp','tcp6'):
                for line in (Path('/proc/net')/name).read_text().splitlines()[1:]:
                    fields=line.split()
                    if len(fields)>9 and fields[3]=='0A' and int(fields[1].split(':')[-1],16)==int(port) and fields[9] in sockets:
                        return True
        except (OSError,ValueError):return False
        return False
    def stop(self): run(['systemctl','stop',self.name], timeout=120)
    def start(self): run(['systemctl','start',self.name], timeout=120)
def read_health(url: str) -> dict:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(url, timeout=3) as response:
            body = response.read(65537)
            if len(body)>65536: raise DeployError('HEALTH_RESPONSE_TOO_LARGE')
            parsed = json.loads(body)
            if response.status != 200: raise DeployError('HEALTH_NOT_READY')
            if not isinstance(parsed,dict) or not isinstance(parsed.get('data'),dict):
                raise DeployError('HEALTH_CONTRACT_INVALID')
            return parsed['data']
    except urllib.error.HTTPError as exc:
        exc.close()
        raise DeployError('HEALTH_NOT_READY', 'HTTP '+str(exc.code)) from None
    except (OSError, ValueError, urllib.error.URLError):
        raise DeployError('HEALTH_UNAVAILABLE') from None
class Manager:
    def __init__(self, bundle: Path, prefix: Path, service=None, *, timeout=45,
                 poll=1.0, phase_hook=None):
        self.bundle = bundle.resolve()
        self.prefix = safe_prefix(prefix)
        self.service = service or SystemService(os.getenv('XUNJIA_SERVICE','xunjia-agent.service'),self.prefix)
        self.timeout, self.poll = timeout, poll
        self.phase_hook = phase_hook  # Injection for synthetic fault tests only; never read from env/CLI.
    def phase(self, txn, value):
        txn['phase'] = value
        atomic_json(self.txn_path(txn['id']), txn)
        if self.phase_hook: self.phase_hook(value, self, txn)
    def txn_path(self, rid): return self.prefix/'deployments'/(rid+'.json')
    @contextlib.contextmanager
    def lock(self):
        self.prefix.mkdir(parents=True, exist_ok=True)
        path = self.prefix/'.xunjia-deploy.lock'
        fd = os.open(path, os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0), 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise DeployError('DEPLOY_BUSY', 'another install/rollback/recovery holds the lock') from None
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
    def unfinished(self):
        records = []
        for path in (self.prefix/'deployments').glob('*.json'):
            record = load_json(path)
            if record.get('phase') not in TERMINAL: records.append(record)
        return records
    def ensure_idle(self):
        if self.unfinished(): raise DeployError('RECOVERY_REQUIRED', 'run 03-recover.sh --apply before another update')
    def link_target(self, path):
        if not path.is_symlink(): return None
        return str(path.resolve())
    def current(self):
        p = self.prefix/'current'
        if not p.is_symlink():
            if p.exists():raise DeployError('CURRENT_NOT_A_SYMLINK')
            return None
        resolved = p.resolve()
        if not resolved.is_relative_to(self.prefix) or not resolved.is_dir():
            raise DeployError('CURRENT_RELEASE_INVALID')
        for name in ('app','venv'):
            alias = self.prefix/name
            if not alias.is_symlink() or os.readlink(alias) != 'current/'+name:
                raise DeployError('LAYOUT_ALIAS_CHANGED', name)
        return resolved
    def validate_release(self, release: Path):
        if not release.resolve().is_relative_to(self.prefix) or not release.is_dir():
            raise DeployError('ROLLBACK_TARGET_INVALID')
        meta = load_json(release/'RELEASE.json')
        verify_payload(release/'app', meta['payload'])
        py = release/'venv/bin/python'
        if not py.is_file(): raise DeployError('ROLLBACK_VENV_MISSING')
        # Interpreter and import smoke, without constructing app or touching a DB.
        env = {k:v for k,v in os.environ.items() if not k.startswith(('XUNJIA_','QB_'))}
        env.update(PYTHONPATH=str(release/'app'), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        run([str(py),'-c','import server.app, relay.file_gateway; print("ok")'],env=env)
        run([str(py),'-m','pip','check'], env=env)
        return meta
    def config_sources(self):
        candidates = [self.prefix/'config']
        old = self.prefix/'app/config'
        if old.is_dir() and old.resolve() != candidates[0].resolve(): candidates.append(old)
        return [p for p in candidates if p.is_dir()]
    def candidate_config(self):
        paths = self.config_sources()
        values = {}
        for folder in paths:
            for name, content in config_fingerprint(folder).items():
                if name in values and values[name] != content:
                    raise DeployError('CONFIG_CONFLICT', name)
                values[name] = content
        envfile = next((p/'xunjia.env' for p in paths if (p/'xunjia.env').is_file()), self.bundle/'config/xunjia.env')
        cfg = read_env(envfile)
        validate_config(cfg)
        return envfile, cfg
    def platform_check(self):
        arch = platform.machine()
        if arch not in ('x86_64','aarch64'): raise DeployError('CPU_UNSUPPORTED', arch)
        try: libc = tuple(map(int, os.confstr('CS_GNU_LIBC_VERSION').split()[-1].split('.')[:2]))
        except (ValueError, OSError, AttributeError): raise DeployError('GLIBC_UNVERIFIED') from None
        if libc < (2,28): raise DeployError('GLIBC_TOO_OLD', 'requires glibc 2.28 or newer')
        if not (self.bundle/'wheels'/arch).is_dir(): raise DeployError('WHEELHOUSE_MISSING', arch)
        return arch
    def preflight(self):
        version = verify_bundle(self.bundle)
        arch = self.platform_check()
        self.service.validate()
        self.ensure_idle()
        envfile, cfg = self.candidate_config()
        existing = self.current()
        if existing: self.validate_release(existing)
        elif (self.prefix/'app').exists():
            self.validate_legacy()
        return {'integrity':'verified','build_id':version['build_id'],'architecture':arch,
                'config_source':str(envfile),'configuration_missing':validate_config(cfg),
                'service_active':self.service.active(),'database_changes':False}
    def validate_legacy(self):
        app, venv = self.prefix/'app', self.prefix/'venv'
        if not app.is_dir() or not (app/'server/app.py').is_file() or not (venv/'bin/python').is_file():
            raise DeployError('LEGACY_LAYOUT_INCOMPLETE', 'both previous app and venv are required')
        if not app.resolve().is_relative_to(self.prefix) or not venv.resolve().is_relative_to(self.prefix):
            raise DeployError('LEGACY_LAYOUT_OUTSIDE_PREFIX')
        text = (app/'server/app.py').read_text(encoding='utf8')
        if 'XUNJIA_DATABASE_URL' not in text: raise DeployError('NOT_XUNJIA_APPLICATION')
        snapshot_payload(app)
    def commit_config(self, txn):
        """Migrate the WHOLE config directory once; never overwrite a real file."""
        sources = self.config_sources()
        fingerprints = {str(p):config_fingerprint(p) for p in sources}
        dest = self.prefix/'config'
        dest.mkdir(mode=0o700, exist_ok=True)
        if dest.is_symlink(): raise DeployError('SHARED_CONFIG_SYMLINK_FORBIDDEN')
        for folder in sources:
            if folder.resolve() == dest.resolve(): continue
            if config_fingerprint(folder) != fingerprints[str(folder)]: raise DeployError('CONFIG_CHANGED_DURING_PREPARATION')
            for p in sorted(folder.rglob('*')):
                rel = p.relative_to(folder); q = dest/rel
                if q.exists() or q.is_symlink():
                    if p.is_dir() and not p.is_symlink() and q.is_dir(): continue
                    a = config_fingerprint(folder).get(str(rel))
                    b = config_fingerprint(dest).get(str(rel))
                    if a != b: raise DeployError('CONFIG_CONFLICT', str(rel))
                    continue
                q.parent.mkdir(parents=True, exist_ok=True)
                if p.is_symlink(): q.symlink_to(os.readlink(p))
                elif p.is_dir(): q.mkdir(exist_ok=True)
                elif p.is_file(): shutil.copy2(p,q)
                else: raise DeployError('CONFIG_SPECIAL_FILE', str(rel))
        if not (dest/'xunjia.env').exists():
            # On first installation, only this editable operator config is imported.
            shutil.copy2(self.bundle/'config/xunjia.env', dest/'xunjia.env')
            for p in sorted((self.bundle/'config').rglob('*')):
                rel = p.relative_to(self.bundle/'config'); q = dest/rel
                if p.is_file() and not q.exists() and not p.is_symlink():
                    q.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p,q)
        os.chmod(dest,0o700)
        read_env(dest/'xunjia.env')
        txn['config_path']=str(dest/'xunjia.env')
    def python_base(self):
        for candidate in (self.prefix/'python/bin/python3.12', Path(shutil.which('python3.12') or '/nonexistent')):
            if candidate.is_file():
                run([str(candidate),'-c','import sys; assert sys.version_info[:2]==(3,12)'])
                return candidate
        # Runtime extraction is explicit, confined, and never overlays a system Python.
        if platform.machine()!='aarch64': raise DeployError('PYTHON312_MISSING')
        archive=self.bundle/'runtime/python-runtime-aarch64.tar.gz'
        if not archive.is_file() or (self.prefix/'python').exists(): raise DeployError('PYTHON312_MISSING')
        import tarfile
        temporary=self.prefix/('.python-'+uuid.uuid4().hex)
        temporary.mkdir(mode=0o700)
        try:
            import posixpath
            with tarfile.open(archive) as tar:
                members=tar.getmembers();links=[]
                for item in members:
                    rel=Path(item.name)
                    if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0]!='python':
                        raise DeployError('RUNTIME_ARCHIVE_UNSAFE')
                    dest=temporary/rel
                    if item.isdir():dest.mkdir(parents=True,exist_ok=True)
                    elif item.isfile():
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        with tar.extractfile(item) as source, dest.open('xb') as target:shutil.copyfileobj(source,target)
                        dest.chmod(item.mode & 0o777)
                    elif item.issym() or item.islnk():links.append(item)
                    else:raise DeployError('RUNTIME_ARCHIVE_SPECIAL_FILE')
                # Materialize confined links only after regular files are extracted.
                for item in links:
                    dest=temporary/item.name
                    base=posixpath.dirname(item.name) if item.issym() else ''
                    link=Path(posixpath.normpath(posixpath.join(base,item.linkname)))
                    if link.is_absolute() or '..' in link.parts or not link.parts or link.parts[0]!='python':
                        raise DeployError('RUNTIME_LINK_OUTSIDE_PREFIX')
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    if item.issym():dest.symlink_to(item.linkname)
                    else:os.link(temporary/link,dest)
            py=temporary/'python/bin/python3.12'
            run([str(py),'-c','import sys; assert sys.version_info[:2]==(3,12)'])
            os.replace(temporary/'python',self.prefix/'python')
            return self.prefix/'python/bin/python3.12'
        finally: shutil.rmtree(temporary,ignore_errors=True)
    def prepare_release(self, txn, version):
        release=Path(txn['target']); release.mkdir(parents=True, mode=0o700)
        app=release/'app'
        shutil.copytree(self.bundle/'app',app)
        for name in ('deploy','scripts','sql'):
            shutil.copytree(self.bundle/name,app/name)
        if (self.bundle/'dify').is_dir():
            shutil.copytree(self.bundle/'dify',app/'dify')
        shutil.copytree(self.bundle/'frontend',app/'frontend')
        (app/'config').symlink_to(self.prefix/'config',target_is_directory=True)
        shutil.copy2(self.bundle/'VERSION.json',app/'VERSION.json')
        atomic_json(app/'.deployment.json',{'prefix':str(self.prefix),'release_id':txn['id'],'build_id':version['build_id']})
        base=self.python_base()
        venv=release/'venv'
        run([str(base),'-m','venv',str(venv)])
        env={k:v for k,v in os.environ.items() if not k.startswith(('XUNJIA_','QB_','PIP_'))}
        env.update(PIP_NO_INDEX='1',PIP_DISABLE_PIP_VERSION_CHECK='1',PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
        py=venv/'bin/python'
        try: run([str(py),'-m','pip','--version'],env=env)
        except DeployError:
            wheels=list((self.bundle/'wheels/common').glob('pip-*.whl'))
            if len(wheels)!=1: raise DeployError('PIP_BOOTSTRAP_MISSING')
            env['PYTHONPATH']=str(wheels[0])
            run([str(py),'-m','pip','install','--no-index','--no-deps',str(wheels[0])],env=env)
            env.pop('PYTHONPATH',None)
        run([str(py),'-m','pip','install','--no-index','--no-deps',
             '--find-links='+str(self.bundle/'wheels'/platform.machine()),
             '--find-links='+str(self.bundle/'wheels/common'),'-r',str(self.bundle/'requirements.lock')],env=env)
        run([str(py),'-m','pip','check'],env=env)
        env['PYTHONPATH']=str(app)
        run([str(py),'-c','import server.app, relay.file_gateway, relay.protocol; print("ok")'],env=env)
        self.phase(txn,'STAGED')
        return release
    def set_current(self, target: Path | None):
        p=self.prefix/'current'
        if target is None:
            if p.is_symlink(): p.unlink()
            fsync_dir(self.prefix); return
        temporary=self.prefix/('.current-'+uuid.uuid4().hex)
        temporary.symlink_to(target,target_is_directory=True)
        os.replace(temporary,p); fsync_dir(self.prefix)
    def migrate_layout(self, txn):
        """One-time old-layout migration, with a durable record for each move."""
        if self.current():
            self.phase(txn,'LAYOUT_READY');return
        legacy_app=self.prefix/'app'
        if legacy_app.exists():
            self.validate_legacy()
            holder=self.prefix/'backups'/txn['id']
            holder.mkdir(parents=True,mode=0o700)
            # Record destinations before moves so interrupted migration can be recovered.
            txn['legacy_backup']=str(holder)
            txn['previous']=str(holder/'legacy-release')
            self.phase(txn,'MIGRATING')
            old_fingerprint=snapshot_payload(legacy_app)
            for name in ('app','venv'):
                os.replace(self.prefix/name,holder/('previous-'+name))
            previous_app=holder/'previous-app'
            cfg=previous_app/'config'
            if cfg.exists() or cfg.is_symlink():
                os.replace(cfg,holder/'original-config')
            cfg.symlink_to(self.prefix/'config',target_is_directory=True)
            legacy=holder/'legacy-release';legacy.mkdir()
            (legacy/'app').symlink_to(previous_app,target_is_directory=True)
            (legacy/'venv').symlink_to(holder/'previous-venv',target_is_directory=True)
            atomic_json(legacy/'RELEASE.json',{'legacy':True,'build_id':None,'release_id':None,
                        'previous':None,'payload':old_fingerprint})
            self.set_current(legacy)
        for name in ('app','venv'):
            p=self.prefix/name
            if p.exists() or p.is_symlink(): raise DeployError('LAYOUT_ALIAS_OCCUPIED',name)
            p.symlink_to('current/'+name,target_is_directory=True)
        self.phase(txn,'LAYOUT_READY')
    def check_health(self, release: Path, cfg: dict, *, old_boot=None):
        meta=load_json(release/'RELEASE.json')
        legacy=meta.get('legacy',False)
        url=health_url(cfg,'/api/health' if legacy else '/api/health/ready')
        end=time.monotonic()+self.timeout
        last='HEALTH_UNAVAILABLE'
        while True:
            try:
                data=read_health(url)
                if data.get('service')!='xunjia' or data.get('ready') is not True:
                    raise DeployError('HEALTH_NOT_READY')
                if isinstance(self.service,SystemService):
                    if legacy:
                        if not self.service.owns_listener(cfg.get('XUNJIA_PORT','18770')):
                            raise DeployError('HEALTH_WRONG_PROCESS')
                    elif data.get('pid')!=self.service.main_pid():raise DeployError('HEALTH_WRONG_PROCESS')
                if not legacy:
                    if data.get('build_id')!=meta['build_id'] or data.get('release_id')!=meta['release_id']:
                        raise DeployError('HEALTH_WRONG_RELEASE')
                    if not data.get('boot_id') or old_boot and data['boot_id']==old_boot:
                        raise DeployError('HEALTH_OLD_PROCESS')
                return data
            except DeployError as exc: last=exc.code
            if time.monotonic()>=end: raise DeployError(last)
            time.sleep(self.poll)
    def old_boot(self,cfg):
        try: return read_health(health_url(cfg,'/api/health')).get('boot_id')
        except DeployError: return None
    def install(self):
        with self.lock():
            plan=self.preflight()
            version=load_json(self.bundle/'VERSION.json')
            previous=self.current()
            rid=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:10]
            txn={'id':rid,'operation':'install','previous':str(previous) if previous else None,
                 'target':str(self.prefix/'releases'/rid),'active_before':self.service.active(),
                 'service_touched':False,'switched':False,'phase':'PREPARING'}
            self.phase(txn,'PREPARING')
            try:
                sources={str(p):config_fingerprint(p) for p in self.config_sources()}
                _,cfg=self.candidate_config()
                if txn['active_before']: validate_config(cfg,ready=True)
                release=self.prepare_release(txn,version)
                if sources!={str(p):config_fingerprint(p) for p in self.config_sources()}:
                    raise DeployError('CONFIG_CHANGED_DURING_PREPARATION')
                if self.current()!=previous:raise DeployError('CURRENT_CHANGED_DURING_PREPARATION')
                self.service.validate()
                if self.service.active()!=txn['active_before']:raise DeployError('SERVICE_STATE_CHANGED_DURING_PREPARATION')
                self.commit_config(txn)
                (self.prefix/'state').mkdir(mode=0o700,exist_ok=True)
                cfg=effective_env(read_env(self.prefix/'config/xunjia.env'),self.prefix,release/'app')
                oldboot=self.old_boot(cfg) if txn['active_before'] else None
                if txn['active_before']:
                    txn['service_touched']=True; self.phase(txn,'STOPPING')
                    self.service.stop()
                self.migrate_layout(txn)
                atomic_json(release/'RELEASE.json',{'legacy':False,'build_id':version['build_id'],
                    'release_id':rid,'previous':txn['previous'],'payload':snapshot_payload(release/'app')})
                # Journal is durable BEFORE the single atomic current-pointer operation.
                txn['switched']=True; self.phase(txn,'SWITCHING')
                self.set_current(release)
                self.phase(txn,'STARTING')
                if txn['active_before']:
                    self.service.start(); self.check_health(release,cfg,old_boot=oldboot)
                self.phase(txn,'COMMITTED')
                return {'status':'running_verified' if txn['active_before'] else 'installed_stopped',
                        'release_id':rid,'build_id':version['build_id'],'config':str(self.prefix/'config/xunjia.env')}
            except BaseException as exc:
                self.restore(txn)
                raise exc
    def discard_failed_target(self, txn):
        if txn['operation']!='install':return
        candidate=Path(txn['target'])
        current=self.prefix/'current'
        if candidate.parent!=self.prefix/'releases' or candidate.name!=txn['id']:
            raise DeployError('INVALID_FAILED_RELEASE_PATH')
        if current.is_symlink() and current.resolve()==candidate.resolve():return
        if candidate.is_dir() and not candidate.is_symlink():shutil.rmtree(candidate)
    def restore(self, txn):
        """Used by failed install, failed rollback, and explicit crash recovery."""
        try:
            # Preparation failure has not touched the running service/current pointer.
            # Do not silently overwrite an externally changed pointer during abort.
            if not txn.get('service_touched') and not txn.get('switched') and txn['phase'] not in ('MIGRATING','LAYOUT_READY'):
                self.phase(txn,'ABORTED');self.discard_failed_target(txn);return
            if txn.get('service_touched') and self.service.active(): self.service.stop()
            previous=Path(txn['previous']) if txn.get('previous') else None
            holder=Path(txn['legacy_backup']) if txn.get('legacy_backup') else None
            if holder and not (holder/'legacy-release/RELEASE.json').is_file():
                # Migration interrupted before both prior artifacts became a managed release.
                for name in ('app','venv'):
                    saved=holder/('previous-'+name); live=self.prefix/name
                    if saved.exists() or saved.is_symlink():
                        if live.is_symlink() and os.readlink(live)=='current/'+name: live.unlink()
                        if live.exists() or live.is_symlink(): raise DeployError('RECOVERY_PATH_CONFLICT',name)
                        os.replace(saved,live)
                cfg=self.prefix/'app/config'
                original=holder/'original-config'
                if original.exists() and not cfg.exists() and not cfg.is_symlink():
                    os.replace(original,cfg)
                previous=None
                self.set_current(None)
            elif previous:
                self.validate_release(previous)
                for name in ('app','venv'):
                    p=self.prefix/name
                    if not p.exists() and not p.is_symlink():p.symlink_to('current/'+name,target_is_directory=True)
                self.set_current(previous)
            elif txn.get('switched') or txn['phase'] in ('LAYOUT_READY','SWITCHING','STARTING'):
                self.set_current(None)
                for name in ('app','venv'):
                    p=self.prefix/name
                    if p.is_symlink() and os.readlink(p)=='current/'+name:p.unlink()
            if txn.get('service_touched') and txn.get('active_before'):
                self.service.start()
                if previous:
                    cfg=effective_env(read_env(self.prefix/'config/xunjia.env'),self.prefix,previous/'app')
                    self.check_health(previous,cfg)
                elif not self.service.active(): raise DeployError('OLD_SERVICE_RESTORE_FAILED')
            self.phase(txn,'ABORTED')
            self.discard_failed_target(txn)
        except BaseException:
            txn['phase']='RECOVERY_REQUIRED';atomic_json(self.txn_path(txn['id']),txn)
            raise DeployError('RECOVERY_REQUIRED','automatic restoration did not complete; keep the journal') from None
    def rollback(self):
        with self.lock():
            self.ensure_idle(); self.service.validate()
            current=self.current()
            if not current: raise DeployError('NO_MANAGED_RELEASE')
            current_meta=self.validate_release(current)
            if not current_meta.get('previous'): raise DeployError('NO_PREVIOUS_RELEASE')
            previous=Path(current_meta['previous'])
            self.validate_release(previous)  # Must pass BEFORE stopping anything.
            cfg=read_env(self.prefix/'config/xunjia.env')
            active=self.service.active()
            if active: validate_config(cfg,ready=True)
            rid=time.strftime('%Y%m%d-%H%M%S')+'-rollback-'+uuid.uuid4().hex[:8]
            txn={'id':rid,'operation':'rollback','previous':str(current),'target':str(previous),
                 'active_before':active,'service_touched':False,'switched':False,'phase':'PREPARING'}
            self.phase(txn,'PREPARING')
            try:
                oldboot=self.old_boot(cfg) if active else None
                if active:
                    txn['service_touched']=True;self.phase(txn,'STOPPING');self.service.stop()
                txn['switched']=True;self.phase(txn,'SWITCHING');self.set_current(previous)
                self.phase(txn,'STARTING')
                if active:
                    self.service.start();self.check_health(previous,cfg,old_boot=oldboot)
                self.phase(txn,'COMMITTED')
                return {'status':'rollback_verified' if active else 'rollback_stopped',
                        'release_id':load_json(previous/'RELEASE.json').get('release_id'),
                        'config_preserved':True}
            except BaseException as exc:
                self.restore(txn);raise exc
    def recover(self):
        with self.lock():
            records=self.unfinished()
            if not records: return {'status':'no_recovery_needed'}
            if len(records)!=1: raise DeployError('MULTIPLE_INCOMPLETE_DEPLOYMENTS')
            self.service.validate()
            txn=records[0];self.restore(txn);self.phase(txn,'RECOVERED')
            return {'status':'recovered','transaction':txn['id']}
    def verify_installed(self, *, liveness=False):
        self.ensure_idle()
        current=self.current()
        if not current: raise DeployError('NO_MANAGED_RELEASE')
        meta=self.validate_release(current)
        cfg=effective_env(read_env(self.prefix/'config/xunjia.env'),self.prefix,current/'app')
        if liveness:
            value=read_health(health_url(cfg,'/api/health/live'))
            if value.get('service')!='xunjia' or value.get('build_id')!=meta['build_id'] or value.get('release_id')!=meta['release_id']:
                raise DeployError('HEALTH_WRONG_RELEASE')
        else:
            validate_config(cfg,ready=True);value=self.check_health(current,cfg)
        return {'status':'liveness_only' if liveness else 'business_ready',
                'build_id':meta['build_id'],'release_id':meta['release_id'],
                'automation_ready':value.get('automation_ready',False)}
def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('preflight','install','rollback','recover','verify'))
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--package-only',action='store_true')
    parser.add_argument('--liveness',action='store_true')
    parser.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parents[1])
    local_meta=Path(__file__).resolve().parents[1]/'.deployment.json'
    default_prefix=load_json(local_meta).get('prefix','/home/xunjia') if local_meta.is_file() else '/home/xunjia'
    parser.add_argument('--prefix',type=Path,default=Path(os.getenv('XUNJIA_PREFIX',default_prefix)))
    args=parser.parse_args()
    manager=Manager(args.bundle,args.prefix)
    def interrupted(signum, frame): raise DeployError('DEPLOY_INTERRUPTED',str(signum))
    signal.signal(signal.SIGTERM,interrupted)
    try:
        if args.operation=='verify':
            value=({'status':'package_integrity_only',**verify_bundle(args.bundle)}
                   if args.package_only else manager.verify_installed(liveness=args.liveness))
        elif args.operation=='preflight' or args.operation=='install' and not args.apply:
            value=manager.preflight();value['mode']='plan_only'
        elif args.operation=='install':value=manager.install()
        elif args.operation=='rollback':
            if not args.apply: raise DeployError('EXPLICIT_APPLY_REQUIRED')
            value=manager.rollback()
        else:
            if not args.apply: raise DeployError('EXPLICIT_APPLY_REQUIRED')
            value=manager.recover()
        print(json.dumps(value,ensure_ascii=False,sort_keys=True))
    except DeployError as exc:
        print(str(exc),file=sys.stderr);raise SystemExit(75 if exc.code=='DEPLOY_BUSY' else 2)
    except Exception as exc:
        print('DEPLOY_UNEXPECTED_FAILURE: '+type(exc).__name__,file=sys.stderr);raise SystemExit(3)
if __name__=='__main__': main()
