"""Select an authenticated collector/account publication on the existing MQ carrier."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import urllib.error
import urllib.parse
import urllib.request

from relay.file_gateway import FileGatewayClient, GatewayAuth
from relay.protocol import Binding, token
from .file_gateway_transport import FileGatewayTransport
from .mq_settings import atomic_write
from .transport_config import profile_binding, scoped_queues, mail_adapter


class CollectorSelection:
    def __init__(self, repository, transport, config_path=None, state_path=None, connection_path=None, *, managed_runtime=None):
        self.repository = repository
        self.transport = transport
        self.path = Path(config_path) if config_path else None
        self.state = Path(state_path) if state_path else None
        self.connection_path = Path(connection_path) if connection_path else None
        self.runtime = managed_runtime
        self._lock = threading.RLock()

    @property
    def configured(self):
        return bool(self.runtime is not None or self.path and self.state and self.connection_path and self.transport is not None)

    def _owned(self, owner):
        paths=self.runtime.ensure(owner)
        return CollectorSelection(self.repository,self.transport,paths.profiles,paths.state,paths.connection,managed_runtime=self.runtime)

    def _load(self, owner):
        if not self.configured:
            raise ValueError('当前服务尚未接入采集端目录发现')
        for path in (self.path, self.connection_path):
            if path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
                raise ValueError('采集端连接配置权限或大小无效')
        raw = self.path.read_bytes()
        profiles = json.loads(raw)
        connection = json.loads(self.connection_path.read_bytes())
        if not isinstance(profiles, list) or not isinstance(connection, dict):
            raise ValueError('采集端连接配置无效')
        owned = [(i, p) for i, p in enumerate(profiles) if isinstance(p, dict) and p.get('owner_user_id') == owner]
        if not owned and not profiles and self.runtime is not None:
            profiles=[self.runtime.bootstrap_profile(owner)];owned=[(0,profiles[0])]
        if len(owned) != 1 or connection.get('owner_user_id') != owner:
            raise PermissionError('没有本人唯一的文件中转采集端连接')
        index, profile = owned[0]
        current=self.repository.binding_for(owner)
        if current and any(profile.get(key)!=current[key] for key in ('account_uid','workbench_id')):
            raise ValueError('已保存的采集端配置与当前绑定不一致，请检查上次保存是否完整')
        config = profile.get('file_gateway')
        if not isinstance(config, dict):
            raise ValueError('当前账号尚未使用its-relay文件中转')
        auth_path = Path(config['auth_file'])
        if not auth_path.is_absolute():
            auth_path = self.path.parent / auth_path
        if auth_path.stat().st_mode & 0o077:
            raise ValueError('本机网关凭据文件须为0600')
        client = FileGatewayClient(connection['uplink_url'], GatewayAuth.from_file(auth_path),
                                   allow_http=config.get('allow_http', False),
                                   timeout=config.get('timeout_seconds', 15), max_bytes=config.get('max_bytes', 4 * 1024 * 1024))
        return profiles, index, raw, connection, client

    def _directory(self, owner, *, workbench_id=None, account_uid=None, refresh=False):
        _, _, _, _, client = self._load(owner)
        query = {}
        if workbench_id is not None or account_uid is not None:
            token(workbench_id); token(account_uid)
            query.update(workbench_id=workbench_id, account_uid=account_uid)
        if refresh:
            query['refresh'] = '1'
        suffix = '?' + urllib.parse.urlencode(query) if query else ''
        request = urllib.request.Request(client.base_url + '/api/files/directory' + suffix,
                                         headers={'access_token': client.auth.token(), 'Cache-Control': 'no-store'})
        try:
            try:
                response = client.opener.open(request, timeout=client.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.status
                data = response.read(client.max_bytes + 1)
            if len(data) > client.max_bytes:
                raise ValueError('采集端目录超过大小限制')
            if status == 404:
                raise ValueError('所选企点号尚未上报通讯录，或现役网关尚未启用目录发现')
            if status != 200:
                raise ValueError('采集端目录读取失败，HTTP ' + str(status))
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            if workbench_id is None:
                if not isinstance(value.get('workbenches'), list):
                    raise ValueError()
            else:
                if value.get('workbench_id') != workbench_id or value.get('account_uid') != account_uid:
                    raise ValueError('通讯录身份与所选采集端、企点号不符')
                identity = value.get('identity', {})
                if identity.get('owner_user_id') != owner:
                    raise PermissionError('通讯录不属于当前用户')
                if not isinstance(value.get('contacts'), list) or not isinstance(value.get('org', {}), dict):
                    raise ValueError()
            return value
        except PermissionError:
            raise
        except (OSError, urllib.error.URLError):
            raise ValueError('无法连接现役采集端目录服务，原选择保持不变') from None
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ValueError('采集端目录响应无效，原选择保持不变') from None

    def catalog(self, owner, *, refresh=False):
        if self.runtime is not None and self.path is None:
            try:return self._owned(owner).catalog(owner,refresh=refresh)
            except (ValueError,OSError) as exc:
                current=self.repository.binding_for(owner)
                selected={k:current[k] for k in ('workbench_id','account_uid','version')} if current else None
                return {'workbenches':[],'selected':selected,'note':str(exc),'refresh_error':str(exc) if refresh else None}
        current = self.repository.binding_for(owner)
        selected = {k: current[k] for k in ('workbench_id', 'account_uid', 'version')} if current else None
        try:
            result = self._directory(owner, refresh=refresh)
        except PermissionError:
            raise
        except (ValueError, OSError) as exc:
            return {'workbenches': [], 'selected': selected, 'note': str(exc), 'refresh_error': str(exc) if refresh else None}
        if refresh and current:
            try:self.contacts(owner)
            except PermissionError:raise
            except (ValueError,OSError):pass
        return {**result, 'selected': selected}

    def contacts(self, owner, *, refresh=False):
        if self.runtime is not None and self.path is None:return self._owned(owner).contacts(owner,refresh=refresh)
        binding = self.repository.binding_for(owner)
        if binding is None:
            raise ValueError('请先选择采集端工作台和企点号')
        book=self._directory(owner, workbench_id=binding['workbench_id'], account_uid=binding['account_uid'], refresh=refresh)
        with self.repository.write(owner) as tx:
            current=self.repository.require_binding(tx,owner)
            if all(current[k]==binding[k] for k in ('account_uid','workbench_id','version')):
                self._record_check(tx,owner,current,book)
        return book

    def files(self, owner, *, before=None, limit=50):
        if self.runtime is not None and self.path is None:
            return self._owned(owner).files(owner,before=before,limit=limit)
        binding=self.repository.binding_for(owner)
        if binding is None:raise ValueError('请先选择采集端工作台和企点号')
        _,_,_,_,client=self._load(owner)
        query={key:binding[key] for key in ('workbench_id','account_uid')}
        query['limit']=limit
        if before is not None:query['before']=before
        request=urllib.request.Request(client.base_url+'/api/files/history?'+urllib.parse.urlencode(query),
                                       headers={'access_token':client.auth.token(),'Cache-Control':'no-store'})
        try:
            with client.opener.open(request,timeout=client.timeout) as response:
                raw=response.read(client.max_bytes+1)
                if response.status!=200 or len(raw)>client.max_bytes:raise ValueError('文件记录响应无效')
            result=json.loads(raw)
            if (not isinstance(result,dict) or result.get('owner_user_id')!=owner
                    or any(result.get(key)!=binding[key] for key in ('workbench_id','account_uid'))):
                raise PermissionError('文件记录不属于当前用户选择的采集端和企点号')
            current=self.repository.binding_for(owner)
            if not current or any(current[key]!=binding[key] for key in ('workbench_id','account_uid','version')):
                raise ValueError('采集端选择已变化，请重新读取文件记录')
            if not isinstance(result.get('items'),list):raise ValueError('文件记录响应无效')
            return {key:value for key,value in result.items() if key!='owner_user_id'}
        except (OSError,urllib.error.URLError):
            raise ValueError('无法读取文件中转保全记录，原文件与处理状态保持不变') from None
        except (json.JSONDecodeError,KeyError,TypeError):
            raise ValueError('文件记录响应无效') from None

    def _record_check(self, tx, owner, binding, book, route='oss'):
        row=tx.execute('SELECT config_json FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
        preferences=json.loads(row['config_json'])
        checks=preferences.setdefault('collector_directory_checks',{})
        checks[route]={'workbench_id':binding['workbench_id'],'account_uid':binding['account_uid'],
                       'version':binding['version'],'stale':bool(book.get('stale',True)),
                       'updated_at':book.get('updated_at'),'checked_at':self.repository.clock(),
                       'publication_id':book.get('publication_id'),'publication_hash':book.get('publication_hash')}
        tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?',
                   (json.dumps(preferences,ensure_ascii=False,separators=(',',':')),owner))

    def freshness(self, owner):
        if self.runtime is not None and self.path is None:return self._owned(owner).freshness(owner)
        binding=self.repository.binding_for(owner)
        if not binding:raise ValueError('请先选择采集端工作台和企点号')
        book=self._directory(owner,workbench_id=binding['workbench_id'],account_uid=binding['account_uid'])
        return {key:book.get(key) for key in ('updated_at','stale','publication_id','publication_hash')}

    def check(self, owner):
        if self.runtime is not None and self.path is None:return self._owned(owner).check(owner)
        binding=self.repository.binding_for(owner)
        if not binding:raise ValueError('请先选择采集端工作台和企点号')
        *_,book=self._candidate(owner,{k:binding[k] for k in ('workbench_id','account_uid')} | {'expected_version':binding['version']})
        with self.repository.write(owner) as tx:
            current=self.repository.require_binding(tx,owner)
            if any(current[k]!=binding[k] for k in ('account_uid','workbench_id','version')):
                raise ValueError('采集端选择已变化，请重新检查')
            self._record_check(tx,owner,current,book)
        return book

    def _candidate(self, owner, body):
        if not isinstance(body, dict) or set(body) != {'workbench_id', 'account_uid', 'expected_version'}:
            raise ValueError('请从目录选择工作台和企点号，并提交当前版本')
        wid, uid, version = body['workbench_id'], body['account_uid'], body['expected_version']
        token(wid); token(uid)
        if len(wid) > 160 or len(uid) > 80 or type(version) is not int or version < 0:
            raise ValueError('采集端选择或版本格式无效')
        current = self.repository.binding_for(owner)
        if (current['version'] if current else 0) != version:
            raise ValueError('当前采集端选择已变化，请重新读取后再选择')
        same = bool(current and (current['workbench_id'], current['account_uid']) == (wid, uid))
        directory = self._directory(owner)
        row = next((account for wb in directory['workbenches'] if wb.get('workbench_id') == wid
                    for account in wb.get('accounts', []) if account.get('account_uid') == uid), None)
        if row is None:
            raise ValueError('请选择该工作台实际已上报的企点号，不能跨台填写账号')
        if not same and 'negotiation-v22-watermark' not in row.get('capabilities', []):
            raise ValueError('所选企点号未支持当前协议，请更新采集端目录')
        book = self._directory(owner, workbench_id=wid, account_uid=uid)
        book['stale']=bool(book.get('stale') or row.get('stale'))
        return wid, uid, version, same, book

    def select(self, owner, body):
        if self.runtime is not None and self.path is None:
            with self._lock:return self._owned(owner).select(owner,body)
        with self._lock:
            wid, uid, version, same, book = self._candidate(owner, body)
            profiles, index, raw, connection, _ = self._load(owner)
            original = profiles[index]
            updated = copy.deepcopy(profiles)
            profile = updated[index]
            profile.update(account_uid=uid, workbench_id=wid)
            profile['file_gateway'].update(scoped_queues(connection, wid, uid))
            previous_adapters = self.transport.adapters
            staged = None
            applied = False
            try:
                with self.repository.write(owner) as tx:
                    old = tx.execute('SELECT * FROM xj2_binding WHERE owner_id=?', (owner,)).fetchone()
                    if (int(old['version']) if old else 0) != version:
                        raise ValueError('当前采集端选择已变化，请重新读取后再选择')
                    occupied = tx.execute('SELECT owner_id FROM xj2_binding WHERE account_uid=?', (uid,)).fetchone()
                    if occupied and occupied['owner_id'] != owner:
                        raise ValueError('该企点号已由其他用户连接')
                    # Re-read under the owner transaction; a concurrent selection cannot turn this into a noop.
                    same = bool(old and (old['workbench_id'], old['account_uid']) == (wid, uid))
                    if not same:
                        reset=self.repository._account_reset(tx,owner,old['account_uid']) if old else None
                        if reset and reset['state']!='complete':
                            raise ValueError('当前账号会话清空尚未完成：'+reset['state']+'；'+(reset['code'] or '等待双端确认')+'。请在当前账号查看进度并重试。')
                        control = tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?', (owner,)).fetchone()
                        group_sending = tx.execute("SELECT group_id FROM xj2_group WHERE owner_id=? AND archived=0 AND state='OPEN' AND send_allowed=1 LIMIT 1", (owner,)).fetchone()
                        member_sending = tx.execute("SELECT p.member_id FROM xj2_peer p JOIN xj2_group g ON g.group_id=p.group_id WHERE g.owner_id=? AND g.archived=0 AND g.state='OPEN' AND p.send_allowed=1 LIMIT 1", (owner,)).fetchone()
                        # A completed reset can isolate unknown historical outcomes,
                        # but never a live group, current epoch or queued command.
                        pending = tx.execute("SELECT c.command_id FROM xj2_command c JOIN xj2_thread t ON t.thread_id=c.thread_id JOIN xj2_group g ON g.group_id=c.group_id WHERE t.owner_id=? AND c.state IN ('QUEUED','PENDING','UNKNOWN') AND NOT (c.state IN ('PENDING','UNKNOWN') AND g.archived=1 AND EXISTS (SELECT 1 FROM xj2_session_reset r WHERE r.owner_id=t.owner_id AND r.account_uid=t.account_uid AND r.workbench_id=t.workbench_id AND r.state='complete' AND r.account_epoch>c.account_epoch AND r.created_at>=c.created_at)) LIMIT 1", (owner,)).fetchone()
                        if (old is not None and control['send_allowed']) or group_sending or member_sending or pending:
                            raise ValueError('请先停止机器人及券组、对手发送，并完成在途及UNKNOWN业务命令对账，再切换企点号')
                        binding = profile_binding(profile)
                        staged = FileGatewayTransport.from_config(binding, profile['file_gateway'], self.state / (uid + '.db'), role='service', config_dir=self.path.parent)
                        candidates={(owner,uid,'oss'):staged}
                        if profile.get('mail'):
                            candidates[(owner,uid,'mail')]=mail_adapter(binding,profile['mail'],self.state/(uid+'.db'))
                        if self.path.read_bytes() != raw:
                            raise ValueError('采集端连接配置已变化，请重新读取')
                        atomic_write(self.path, json.dumps(updated, ensure_ascii=False, indent=2).encode('utf-8'))
                        applied = True
                        if old:
                            tx.execute('UPDATE xj2_binding SET account_uid=?,workbench_id=?,transport=?,version=version+1 WHERE owner_id=?', (uid, wid, 'oss', owner))
                        else:
                            tx.execute("INSERT INTO xj2_binding VALUES(?,?,?,'oss',1,'{}')", (owner, uid, wid))
                        # Explicit source changes revoke prior send approvals, never erase messages or plans.
                        tx.execute('UPDATE xj2_owner SET send_allowed=0,control_version=control_version+1 WHERE owner_id=?', (owner,))
                        tx.execute('UPDATE xj2_group SET send_allowed=0,control_version=control_version+1 WHERE owner_id=?', (owner,))
                        tx.execute('UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE group_id IN (SELECT group_id FROM xj2_group WHERE owner_id=?)', (owner,))
                        self.repository.audit(tx, owner, 'collector_selected', uid, {'workbench_id': wid, 'account_uid': uid})
                        adapters = {key: value for key, value in previous_adapters.items() if key[0] != owner}
                        adapters.update(candidates)
                        self.transport.adapters = adapters
                    current=self.repository.require_binding(tx,owner)
                    self._record_check(tx,owner,current,book)
            except BaseException:
                try:
                    if applied:
                        atomic_write(self.path, raw)
                finally:
                    self.transport.adapters = previous_adapters
                raise
            binding = self.repository.binding_for(owner)
            return {'binding': binding, 'selected': {k: binding[k] for k in ('workbench_id', 'account_uid', 'version')},
                    'contacts': book, 'connected': not book.get('stale',True),
                    'note': '选择已保存；目录已过期，读取的是缓存通讯录，尚未确认实时连接' if book.get('stale') else '选择已保存并读取对应通讯录；发送仍须单独授权'}
