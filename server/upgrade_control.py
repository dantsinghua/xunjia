"""Durable operator-triggered upgrades. Downloads are consumed exactly once per job."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid
from fastapi import APIRouter, Body, Request, HTTPException

ACTIVE = ('queued', 'downloading', 'verifying', 'installing')
MESSAGES = {
    'queued': '升级任务已提交', 'downloading': '正在同步升级包',
    'verifying': '正在校验签名和版本', 'installing': '正在安装，服务将短暂重启',
    'succeeded': '升级完成，服务版本及健康检查通过', 'empty': '暂无待同步的升级包',
    'failed': '升级未完成，请查看错误码',
    'unknown': '任务结果待核对，已暂停再次同步，避免重复消费或安装',
}


def private_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError('UPGRADE_STORAGE_SYMLINK')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


class UpgradeStore:
    def __init__(self, root):
        self.root = private_directory(root)
        self.path = self.root / 'upgrades.db'
        if self.path.is_symlink():
            raise ValueError('UPGRADE_STORAGE_SYMLINK')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS job(
                  id TEXT PRIMARY KEY, state TEXT NOT NULL, owner TEXT NOT NULL,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  target_build TEXT NOT NULL DEFAULT '', code TEXT NOT NULL DEFAULT '',
                  package_sha256 TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS setting(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact(
                  path TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at REAL NOT NULL,
                  fingerprint TEXT NOT NULL, removed_at REAL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA synchronous=FULL')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def lock(self, *, blocking=False):
        fd = os.open(self.root / 'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
        finally:
            os.close(fd)

    def latest(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM job ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def update(self, job_id, state, **fields):
        if state not in MESSAGES or set(fields) - {'target_build', 'code', 'package_sha256', 'result'}:
            raise ValueError('INVALID_UPGRADE_STATE')
        with self.db() as db:
            db.execute('UPDATE job SET state=?,updated_at=?' + ''.join(',' + k + '=?' for k in fields) + ' WHERE id=?',
                       [state, time.time(), *fields.values(), job_id])

    def detail(self, job_id, **fields):
        allowed = {'phase', 'rollback', 'health', 'previous_build', 'code'}
        if set(fields) - allowed:raise ValueError('INVALID_UPGRADE_DETAIL')
        with self.db() as db:
            row = db.execute('SELECT result FROM job WHERE id=?', (job_id,)).fetchone()
            if row is None:raise ValueError('UPGRADE_JOB_NOT_FOUND')
            result = json.loads(row[0])
            if not isinstance(result, dict):result = {}
            result.update(fields)
            db.execute('UPDATE job SET result=?,updated_at=? WHERE id=?',
                       (json.dumps(result), time.time(), job_id))

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM setting WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute('INSERT INTO setting VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value)))


def load_channel(prefix):
    path = Path(prefix) / 'config/upgrade-channel.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 32768:
        raise ValueError('UPGRADE_CHANNEL_NOT_CONFIGURED')
    return validate_channel(json.loads(path.read_text()))


def validate_channel(config):
    if not isinstance(config, dict):
        raise ValueError('UPGRADE_CHANNEL_INVALID')
    from relay.gateway_paths import validate_path
    queue = validate_path(config['queue'])
    if not queue.startswith('xjup_') or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', config['site_id']):
        raise ValueError('UPGRADE_CHANNEL_INVALID')
    if not isinstance(config.get('operators'), list) or not all(isinstance(v, str) and v for v in config['operators']):
        raise ValueError('UPGRADE_OPERATORS_INVALID')
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(load_pem_public_key(config['public_key'].encode('ascii')), Ed25519PublicKey):
        raise ValueError('UPGRADE_PUBLIC_KEY_INVALID')
    return config


class UpgradeControl:
    def __init__(self):
        self.app = Path(__file__).resolve().parents[1]
        metadata = self.app / '.deployment.json'
        self.prefix = None
        if metadata.is_file():
            candidate = Path(json.loads(metadata.read_text())['prefix'])
            if candidate.is_absolute() and candidate.resolve() == candidate:
                self.prefix = candidate

    def configured(self):
        if self.prefix is None:
            raise ValueError('MANAGED_INSTALL_REQUIRED')
        return load_channel(self.prefix)

    def store(self):
        return UpgradeStore(self.prefix / 'state/upgrade')

    def status(self, owner):
        build = ''
        version = self.app / 'VERSION.json'
        if version.is_file():
            build = json.loads(version.read_text()).get('build_id', '')
        result = {'enabled': False, 'can_upgrade': False, 'current_build': build,
                  'retention_days': 30, 'job': None}
        try:
            config = self.configured()
        except (ValueError, OSError, KeyError, TypeError):
            result['hint'] = '尚未安装升级通道配置'
            return result
        result.update(enabled=True, can_upgrade='*' in config['operators'] or owner in config['operators'])
        store = self.store()
        job = store.latest()
        # A killed worker must never leave an apparently running job forever or
        # silently re-consume the remote queue. Only the operator can reconcile it.
        if job and job['state'] in ACTIVE and time.time() - job['updated_at'] > 90:
            try:
                with store.lock():
                    fresh = store.latest()
                    if fresh and fresh['id'] == job['id'] and fresh['state'] in ACTIVE:
                        store.update(job['id'], 'unknown', code='UPGRADE_WORKER_INTERRUPTED')
                        job = store.latest()
            except BlockingIOError:
                pass
        if job:
            result['job'] = {k: job[k] for k in ('id', 'state', 'created_at', 'updated_at', 'target_build')}
            detail = json.loads(job['result'])
            if isinstance(detail, dict) and ('phase' in detail or 'rollback' in detail):
                result['job']['detail'] = {key: detail[key] for key in
                    ('phase', 'rollback', 'health', 'previous_build', 'code') if key in detail}
            message = ('目标版本已安装，本次未重复安装或执行健康检查' if job['code'] == 'ALREADY_INSTALLED'
                       else MESSAGES[job['state']])
            if job['code'] in ('NO_INTACT_SUPPORTED_BASE_BUNDLE', 'NO_INTACT_CURRENT_BASE_BUNDLE'):
                message = '自动检查后未找到完整的升级基线，已保留下载包；服务未执行本次升级'
            elif job['code'] == 'INSTALLED_BUILD_NOT_SUPPORTED':
                message = '升级包不支持当前运行版本，服务未执行本次升级'
            elif job['code'] == 'UPGRADE_ROLLBACK_SUCCEEDED':
                message = '升级未成功，已自动恢复旧版并通过健康检查，可继续使用'
            elif job['code'] == 'UPGRADE_FAILED_OLD_VERSION_HEALTHY':
                message = '升级未完成，旧版仍正常运行，可继续使用'
            elif job['code'] == 'UPGRADE_ROLLBACK_FAILED':
                message = '升级未成功，自动恢复未通过检查，请下载诊断日志联系技术支持'
            result['job']['message'] = message + ((' · ' + job['code']) if job['code'] else '')
        result['retention'] = store.get('retention', {'days': 30, 'hint': '等待首次清理'})
        from .upgrade_automation import schedule
        result['schedule'] = schedule(store)
        if job:result['log_delivery'] = store.get('log_delivery:'+job['id'], {'state':'pending'})
        return result

    def start(self, owner, *, scheduled_day=None):
        config = self.configured()
        if '*' not in config['operators'] and owner not in config['operators']:
            raise PermissionError('当前登录账号没有升级权限')
        store = self.store()
        try:
            with store.lock():
                if scheduled_day is not None and store.get('scheduled_day')==scheduled_day:
                    return self.status(owner)
                previous = store.latest()
                if previous and previous['state'] in (*ACTIVE, 'unknown'):
                    raise ValueError('已有升级任务或待核对结果，请等待并刷新状态')
                identity = uuid.uuid4().hex
                with store.db() as db:
                    now = time.time()
                    db.execute('INSERT INTO job(id,state,owner,created_at,updated_at) VALUES(?,?,?,?,?)',
                               (identity, 'queued', owner, now, now))
                    if scheduled_day is not None:
                        db.execute("INSERT INTO setting VALUES('scheduled_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(scheduled_day),))
                from .upgrade_diagnostics import event
                event(store, identity, 'queued', 'UPGRADE_QUEUED')
                python = self.app.parent / 'venv/bin/python'
                worker = self.app / 'deploy/remote_upgrade.py'
                command = ['/usr/bin/systemd-run', '--unit=xunjia-upgrade-' + identity,
                           '--property=Type=simple', '--property=UMask=0077',
                           '--property=Restart=on-failure', '--property=RestartSec=3',
                           str(python), '-I', '-B', str(worker), '--prefix', str(self.prefix), '--job', identity]
                # Submission is durable first. A timeout can still mean systemd
                # started the worker; never mark it retryable on an ambiguous result.
                try:
                    completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                    if completed.returncode:
                        store.update(identity, 'unknown', code='UPGRADE_START_NOT_CONFIRMED')
                except (OSError, subprocess.TimeoutExpired):
                    store.update(identity, 'unknown', code='UPGRADE_START_NOT_CONFIRMED')
        except BlockingIOError:
            raise ValueError('升级或文件清理正在进行，请稍后重试') from None
        return self.status(owner)

    def tick(self):
        from .upgrade_automation import tick
        tick(self)

    def resume(self):
        """Recover an interrupted owned task after app restart; never re-download."""
        if self.prefix is None or not (self.prefix / 'config/upgrade-channel.json').is_file():return
        self.configured()
        store = self.store()
        try:
            with store.lock():
                job = store.latest()
                if not job or job['state'] not in ('downloading', 'verifying', 'installing'):return
                command = ['/usr/bin/systemd-run', '--unit=xunjia-upgrade-' + job['id'],
                           '--property=Type=simple', '--property=UMask=0077',
                           '--property=Restart=on-failure', '--property=RestartSec=3',
                           str(self.app.parent / 'venv/bin/python'), '-I', '-B',
                           str(self.app / 'deploy/remote_upgrade.py'), '--prefix', str(self.prefix), '--job', job['id']]
                result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                if result.returncode:
                    from .upgrade_diagnostics import event
                    event(store, job['id'], 'recovery', 'UPGRADE_RECOVERY_START_PENDING')
        except BlockingIOError:
            return

    def maintain(self):
        if self.prefix is None:
            return
        if not (self.prefix / 'config/upgrade-channel.json').is_file():
            return
        store = None
        try:
            self.configured()
            from .upgrade_retention import prune_artifacts
            store = self.store()
            with store.lock():
                job = store.latest()
                if job and job['state'] in (*ACTIVE, 'unknown'):
                    return
                prune_artifacts(store, self.prefix)
        except BlockingIOError:
            return
        except Exception as exc:
            if store is not None:
                store.set('retention', {'days': 30, 'last_run': time.time(), 'removed': 0,
                    'blocked': 1, 'code': type(exc).__name__, 'hint': '文件清理未完成，请查看运行诊断'})
            raise


def upgrade_router(control):
    router = APIRouter(prefix='/api/system/upgrade')

    @router.get('')
    def status(request: Request):
        data = control.status(request.state.user.user_id)
        institution = getattr(request.app.state, 'institution', None)
        archive = getattr(institution, 'download_retention', None)
        if archive:
            retention = data.setdefault('retention', {'days': 30})
            retention['removed'] = retention.get('removed', 0) + archive.get('payloads_removed', 0) + archive.get('media_removed', 0)
            retention['blocked'] = retention.get('blocked', 0) + archive.get('protected', 0) + len(archive.get('errors', []))
            note = '下载缓存保留30天；消息、附件正文和去重记录已入库保留，未完成或待核对数据受保护'
            if archive.get('state') == 'error' or archive.get('errors'):
                note = '附件或原包清理未完成；' + note
            retention['hint'] = (retention.get('hint', '') + '；' + note).lstrip('；')
        return {'ok': True, 'data': data}

    @router.post('')
    def start(request: Request, body: dict = Body(default_factory=dict)):
        if body:
            raise ValueError('升级使用服务端固定通道，无需提交路径或命令')
        return {'ok': True, 'data': control.start(request.state.user.user_id)}

    @router.put('/schedule')
    def change_schedule(request: Request, body: dict = Body(default_factory=dict)):
        from .upgrade_automation import save_schedule
        try:return {'ok':True,'data':save_schedule(control,request.state.user.user_id,body)}
        except BlockingIOError:raise HTTPException(409,'升级正在处理，请稍后保存调度配置') from None

    @router.get('/{job_id}/diagnostics')
    def diagnostics(request: Request, job_id: str):
        from .upgrade_diagnostics import snapshot
        control.configured()
        return {'ok': True, 'data': snapshot(control.store(), job_id)}

    @router.get('/{job_id}/log')
    def download_log(request: Request, job_id: str):
        from .upgrade_diagnostics import log_text
        from fastapi.responses import Response
        control.configured()
        body = log_text(control.store(), job_id)
        return Response(body, media_type='text/plain', headers={
            'Content-Disposition': 'attachment; filename="xunjia-upgrade-' + job_id + '.txt"',
            'Cache-Control': 'no-store'})
    return router
