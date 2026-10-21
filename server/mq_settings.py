"""Owner editor for the existing MQ file connection; no STS exchange or queue reads."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import tempfile
from urllib.parse import urlsplit
import uuid

from fastapi import APIRouter, Body, Request

from relay.file_gateway import GatewayAuth, FileGatewayError
from relay.file_gateway_connection import connection_client, connection_identity, protected_auth_path
from .file_gateway_transport import FileGatewayTransport
from .transport_config import scoped_queues


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class MQSettings:
    def __init__(self, repository, transport, config_path=None, *, managed_runtime=None):
        self.repository = repository
        self.transport = transport
        self.path = Path(config_path) if config_path else None
        self.runtime = managed_runtime
        self._lock = threading.RLock()

    def _binding(self, owner):
        binding = self.repository.binding_for(owner)
        if binding is None:
            if self.runtime is not None:
                return {'owner_user_id':owner,'account_uid':None,'workbench_id':None}
            raise PermissionError('尚未绑定本人采集账号')
        return binding

    def _load(self, owner):
        binding = self._binding(owner)
        if not self.path or self.transport is None:
            raise ValueError('部署尚未接入现有文件中转配置')
        if self.path.stat().st_mode & 0o077 or self.path.stat().st_size > 65536:
            raise ValueError('文件中转连接配置文件权限或大小无效')
        raw = self.path.read_bytes()
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError('文件中转连接配置须为JSON对象')
        if config.get('owner_user_id') != owner:
            raise PermissionError('当前用户不拥有此文件中转连接')
        adapters = getattr(self.transport, 'adapters', {})
        adapter = adapters.get((owner, binding['account_uid'], 'oss'))
        if self.runtime is not None and binding['account_uid'] is None:
            adapter = self.runtime.gateway(owner)
            self._active(adapter)
            return binding,config,raw,adapter
        if not isinstance(adapter, FileGatewayTransport):
            raise ValueError('当前绑定账号尚未使用文件网关')
        expected=scoped_queues(config,binding['workbench_id'],binding['account_uid'])
        if expected['uplink_url'] != adapter.inbound.base_url or expected['downlink_url'] != adapter.outbound.base_url:
            raise ValueError('文件中转配置未接入当前账号的现役文件网关')
        self._active(adapter)
        return binding, config, raw, adapter

    def _active(self, adapter):
        try:
            views = []
            for client in (adapter.inbound, adapter.outbound):
                _, _, body = client._request('pending')
                active = json.loads(body)
                if not isinstance(active, dict) or active.get('connection_config') != str(self.path.resolve()):
                    raise ValueError()
                if not all(isinstance(active.get(k), str) and len(active[k]) == 64
                           for k in ('connection_revision', 'connection_identity')) or type(active.get('switch_safe')) is not bool:
                    raise ValueError()
                views.append(active)
            if any(views[0][k] != views[1][k] for k in ('connection_revision', 'connection_identity')):
                raise ValueError()
            return {**views[0], 'switch_safe': views[0]['switch_safe'] and views[1]['switch_safe']}
        except (FileGatewayError, ValueError, KeyError, TypeError):
            raise ValueError('现役文件网关尚未确认此配置，配置未应用') from None

    def status(self, owner):
        if self.runtime is not None and self.path is None:
            paths=self.runtime.paths(owner)
            if not paths.connection.is_file():
                return dict(editable=True,configured=False,backend='file_gateway',base_url='',credentials_set=False,
                            applied=False,account_uid=None,workbench_id=None,revision='',note='先填写文件中转地址和config.yaml，再选择采集端与台内企点号')
            try:
                self.runtime.ensure(owner)
            except (ValueError,OSError) as exc:
                raw=paths.connection.read_bytes();config=json.loads(raw)
                if config.get('owner_user_id')!=owner:raise PermissionError('文件中转连接不属于当前用户')
                binding=self.repository.binding_for(owner) or {}
                return dict(editable=True,configured=True,backend='file_gateway',base_url=config.get('base_url',''),
                            credentials_set=True,applied=False,account_uid=binding.get('account_uid'),workbench_id=binding.get('workbench_id'),
                            revision=hashlib.sha256(raw).hexdigest(),note=str(exc))
            return MQSettings(self.repository,self.transport,paths.connection,managed_runtime=self.runtime).status(owner)
        with self._lock:
            binding = self._binding(owner)
            view = dict(editable=False, configured=False, backend='file_gateway', base_url='',
                        credentials_set=False, applied=False, account_uid=binding['account_uid'],
                        workbench_id=binding['workbench_id'], revision='', note='')
            try:
                _, config, raw, adapter = self._load(owner)
            except OSError:
                return {**view, 'note': '文件中转连接配置文件不存在或不可读'}
            except ValueError as exc:
                return {**view, 'note': str(exc)}
            revision = hashlib.sha256(raw).hexdigest()
            applied = self._active(adapter)['connection_revision'] == revision
            valid = False
            try:
                GatewayAuth.from_file(protected_auth_path(self.path, config['auth_file']))
                valid = True
            except (ValueError, OSError, KeyError, FileGatewayError):
                pass
            return {**view, 'editable': True, 'configured': valid and bool(config.get('base_url')),
                    'credentials_set': valid, 'base_url': config.get('base_url', ''),
                    'revision': revision, 'applied': applied,
                    'note': ('现役文件网关已加载此配置。' if applied else '配置已保存，现役文件网关尚未加载。') +
                            'config.yaml内容留空保留原凭据；测试仅查询待收文件数量'}

    def _candidate(self, body, config, adapter):
        if not isinstance(body, dict) or set(body) - {'base_url', 'config_yaml', 'expected_revision'}:
            raise ValueError('仅支持base_url和config.yaml内容')
        base = body.get('base_url', config.get('base_url', ''))
        text = body.get('config_yaml', '')
        if not isinstance(base, str) or len(base) > 2048 or not isinstance(text, str):
            raise ValueError('文件中转连接配置格式无效')
        candidate = dict(config, base_url=base.strip().rstrip('/'))
        if urlsplit(candidate['base_url']).hostname != config.get('hostname'):
            candidate.pop('addresses', None)
            candidate.pop('hostname', None)
        try:
            auth = GatewayAuth.from_yaml(text) if text.strip() else GatewayAuth.from_file(protected_auth_path(self.path, config['auth_file']))
            client = connection_client(candidate, auth, allow_http=config.get('allow_http', False))
        except (FileGatewayError, OSError, KeyError, ValueError):
            raise ValueError('请核对base_url及config.yaml中的key和16字节sm4_key') from None
        return candidate, auth, client, bool(text.strip())

    def test(self, owner, body):
        if self.runtime is not None and self.path is None:
            paths=self.runtime.paths(owner)
            if paths.connection.is_file():
                config=json.loads(paths.connection.read_bytes())
                if config.get('owner_user_id')!=owner:raise PermissionError('文件中转连接不属于当前用户')
            else:
                base=body.get('base_url','') if isinstance(body,dict) else ''
                config={'base_url':base,'allow_http':os.environ.get('XUNJIA_MQ_ALLOW_HTTP','0')=='1'}
            local=MQSettings(self.repository,self.transport,paths.connection)
            _,_,client,_=local._candidate(body,config,None)
            try:
                pending=client.pending()
                return dict(passed=True,connected=True,saved=False,steps=[dict(name='文件中转',ok=True,detail=f'认证成功，待收文件 {pending} 个；未取件、未上传')])
            except FileGatewayError as exc:
                return dict(passed=False,connected=False,saved=False,steps=[dict(name='文件中转',ok=False,detail=exc.code)])
        with self._lock:
            _, config, _, adapter = self._load(owner)
            _, _, client, _ = self._candidate(body, config, adapter)
        try:
            pending = client.pending()
            return dict(passed=True, connected=True, saved=False,
                        steps=[dict(name='文件中转', ok=True, detail=f'认证成功，待收文件 {pending} 个；未取件、未上传')])
        except FileGatewayError as exc:
            return dict(passed=False, connected=False, saved=False,
                        steps=[dict(name='文件中转', ok=False, detail=exc.code)])

    def save(self, owner, body):
        if self.runtime is not None and self.path is None:
            if not self.runtime.configured(owner):
                self.runtime.initialize(owner,body)
                return {**self.status(owner),'saved':True}
            paths=self.runtime.ensure(owner)
            return MQSettings(self.repository,self.transport,paths.connection,managed_runtime=self.runtime).save(owner,body)
        with self._lock:
            _, config, raw, adapter = self._load(owner)
            if not isinstance(body, dict) or body.get('expected_revision') != hashlib.sha256(raw).hexdigest():
                raise ValueError('文件中转配置已变化，请重新读取后再保存')
            candidate, auth, _, replace_auth = self._candidate(body, config, adapter)
            active = self._active(adapter)
            proposed = connection_client(candidate, auth, allow_http=config.get('allow_http', False))
            if connection_identity(proposed) != active['connection_identity'] and not active['switch_safe']:
                raise ValueError('当前队列仍有历史账本或未决文件，请先完成队列迁移；可更新同一key的SM4凭据，原数据与UNKNOWN保留')
            if self.runtime is not None:self.runtime.reserve(owner,proposed)
            # Prepare credentials separately, then atomically switch the single config pointer.
            # In-flight coordinator operations retain their previous complete client snapshot.
            path = None
            if replace_auth:
                folder = self.path.parent / '.mq-secrets'
                folder.mkdir(mode=0o700, exist_ok=True)
                folder.chmod(0o700)
                path = folder / (uuid.uuid4().hex + '.yaml')
                import yaml
                atomic_write(path, yaml.safe_dump({'key': auth.key, 'sm4_key': auth.sm4_key.decode('utf-8')}, allow_unicode=True).encode('utf-8'))
                candidate['auth_file'] = str(path.relative_to(self.path.parent))
            try:
                if self.path.read_bytes() != raw:
                    raise ValueError('文件中转配置已变化，请重新读取后再保存')
                atomic_write(self.path, json.dumps(candidate, ensure_ascii=False, indent=2).encode('utf-8'))
            except BaseException:
                if path is not None:
                    path.unlink(missing_ok=True)
                raise
            # A local pending request loads the snapshot; it never dequeues remote data.
            view = self.status(owner)
            if not view['applied']:
                view['note'] = '配置已保存，但尚未应用。' + view['note']
            return {**view, 'saved': True}


def mq_settings_router(settings):
    router = APIRouter(prefix='/api/im/mq-config')

    @router.get('')
    def status(request: Request):
        return {'ok': True, 'data': settings.status(request.state.user.user_id)}

    @router.put('')
    def save(request: Request, body: dict = Body(default_factory=dict)):
        return {'ok': True, 'data': settings.save(request.state.user.user_id, body)}

    @router.post('/test')
    def test(request: Request, body: dict = Body(default_factory=dict)):
        return {'ok': True, 'data': settings.test(request.state.user.user_id, body)}
    return router
