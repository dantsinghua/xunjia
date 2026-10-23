"""One institutional directory consumer; one selected account per signed-in workspace."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
from relay.file_gateway import GatewayAuth,FileGatewayError
from relay.file_gateway_connection import connection_client,protected_auth_path,TLS_FIELDS,resolve_tls_files
from relay.gateway_paths import CONTRACT,DIRECTORY_PATH,ORGANIZATION_PATH,OFFLINE_SECONDS,InstitutionKeys,account_paths
from relay.institution_archive import InstitutionArchive
from relay.institution_directory import InstitutionDirectory
from relay.protocol import token
from .institution_transport import InstitutionTransport
from .receiver_ledger import account_lock
from .institution_settings import atomic_write

class InstitutionRuntime:
    def __init__(self, repository, transport, config_path, state_path, *, clock=time.time, client_factory=None):
        self.repository,self.transport=repository,transport;self.path=Path(config_path)
        self.root=Path(state_path)/"institution-path-v1";self.clock=clock
        self._lock=threading.RLock();self._stop=threading.Event();self._thread=None;self._lease=None
        self.background_enabled=True
        self._factory=client_factory;self.config=None;self.auth=None;self.keys=None;self.directory=None;self.archive=None
        self.errors={};self._last_friends={};self._last_org_request=0;self.applied_revision="";self._last_archive_prune=0
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_file():self.load()

    @property
    def configured(self):return self.config is not None

    def load(self):
        if self.path.stat().st_mode & 0o077 or self.path.stat().st_size>65536:raise ValueError("机构连接配置权限或大小无效")
        raw=self.path.read_bytes();config=resolve_tls_files(json.loads(raw),self.path.parent)
        if not isinstance(config,dict) or config.get("contract")!=CONTRACT:raise ValueError("机构文件通道合同不匹配")
        if set(config)-{"contract","base_url","auth_file","hostname","addresses","directory_state_dir",*TLS_FIELDS}:raise ValueError("机构配置含不允许字段；路径由系统内置")
        auth=GatewayAuth.from_file(protected_auth_path(self.path,config["auth_file"]))
        keys=InstitutionKeys.from_auth(auth)
        if self.keys is not None and keys.institution_id!=self.keys.institution_id:raise ValueError("机构凭据已变化，请保存配置清空旧业务后重新选择账号")
        connection_client(config,auth,path=DIRECTORY_PATH)
        self.config,self.auth,self.keys=config,auth,keys
        self.state=self.root/keys.institution_id;self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        if 'directory_state_dir' in config and (not isinstance(config['directory_state_dir'],str) or not config['directory_state_dir'].strip()):
            raise ValueError('共享机构目录状态路径无效')
        directory_root=Path(config['directory_state_dir']).expanduser() if config.get('directory_state_dir') else self.root
        if not directory_root.is_absolute():directory_root=self.path.parent/directory_root
        self.directory=InstitutionDirectory(directory_root/keys.institution_id/"directory.db",keys,clock=self.clock)
        self.archive=InstitutionArchive(self.state/"archive",clock=self.clock)
        self.applied_revision=hashlib.sha256(raw).hexdigest()

    def client(self, path):
        if not self.configured:raise ValueError("请先配置机构文件中转服务")
        return self._factory(path) if self._factory else connection_client(self.config,self.auth,path=path)

    def availability(self, wb, uid):
        """A missing heartbeat cannot establish that the native app logged out."""
        row=self.directory.account(wb,uid) if self.directory else None
        if row is not None and row.get('channel')!='qidian':
            return {"ready":False,"code":"COLLECTOR_CHANNEL_UNSUPPORTED","hint":"询价工作台只允许选择企点账号"}
        if row is None:
            return {"ready":False,"code":"COLLECTOR_NOT_DISCOVERED","hint":"尚未收到当前企点号的采集目录"}
        if row.get("state")=="conflict":
            return {"ready":False,"code":"COLLECTOR_IDENTITY_CONFLICT","hint":"同一企点号被多个采集工作台同时上报，请核对当前采集端"}
        stamp=row.get("updated_at")
        if type(stamp) not in (int,float) or not 0<=self.clock()-stamp<OFFLINE_SECONDS:
            return {"ready":False,"code":"COLLECTOR_HEARTBEAT_STALE","hint":"采集通道心跳已过期，无法确认收发状态，请检查文件中转连接"}
        if not row["available"]:
            return {"ready":False,"code":"COLLECTOR_NOT_READY","hint":"采集端报告收发未就绪，请检查采集端通道状态"}
        return {"ready":True,"code":"READY","hint":"采集通道心跳正常"}

    def online(self, wb, uid):
        return self.availability(wb,uid)["ready"]

    def adapter(self, owner, wb, uid, *, preferences=None):
        with self._lock:
            account=self.directory.account(wb,uid)
            if account is not None and account.get('channel')!='qidian':raise ValueError('询价工作台只允许选择企点账号')
            preferences=self.repository.preferences(owner) if preferences is None else preferences
            context=preferences.get("context_id")
            key=(owner,uid,"oss");old=self.transport.adapters.get(key)
            if isinstance(old,InstitutionTransport) and getattr(old.binding,'channel','qidian')=='qidian' and old.binding.workbench_id==wb and old.context_id==context:return old
            binding=self.keys.binding(wb,uid,owner);paths=account_paths(wb,uid)
            ident=hashlib.sha256(json.dumps([owner,wb,uid,context]).encode()).hexdigest()
            adapter=InstitutionTransport(binding,self.client(paths["uplink_path"]),self.client(paths["downlink_path"]),self.state/"accounts"/(ident+".db"),role="service",institution_owner=self.keys.owner_id,clock=self.clock)
            adapter.online_guard=lambda:self.online(wb,uid)
            adapter.context_id=context;adapter.context_version=preferences.get("context_version",0)
            adapter.context_ready=bool(preferences.get("context_ready",False))
            adapter.active_guard=lambda command:self.current_adapter(owner) is adapter and command.get("context_id")==adapter.context_id and adapter.context_ready
            adapter.manifest_handler=lambda data:self.accept_manifest(owner,adapter,data)
            adapter.catalog_provider=lambda:{"account_uid":uid,"workbench_id":wb,"capabilities":self.directory.account(wb,uid).get("capabilities",[]) if self.online(wb,uid) else []}
            adapter.contacts_provider=lambda:self.directory.contacts(wb,uid,owner)
            return adapter

    def current_adapter(self, owner):
        binding=self.repository.binding_for(owner)
        if not binding:return None
        prefs=self.repository.preferences(owner)
        if prefs.get("relay_contract")!=CONTRACT or prefs.get("relay_institution_id")!=self.keys.institution_id:return None
        adapter=self.transport.adapters.get((owner,binding["account_uid"],"oss"))
        if not isinstance(adapter,InstitutionTransport) or getattr(adapter.binding,'channel','qidian')!='qidian' or adapter.binding.workbench_id!=binding["workbench_id"]:return None
        if adapter.context_id!=prefs.get("context_id"):return None
        return adapter

    def request_context(self, adapter):
        if not adapter.context_id or adapter.context_ready:return
        if self.clock()-getattr(adapter,"_context_last",0)<2:return
        adapter._context_last=self.clock()
        try:
            adapter.publish_manifest({"contract":CONTRACT,"op":"activate_context","context_id":adapter.context_id,
                "context_version":adapter.context_version})
            self.errors.pop("context:"+adapter.binding.account_uid,None)
        except Exception as exc:self.errors["context:"+adapter.binding.account_uid]=getattr(exc,"code",type(exc).__name__)

    def retire_context(self, adapter, *, notify=True):
        adapter.context_ready=False
        try:
            if notify:
                adapter.publish_manifest({"contract":CONTRACT,"op":"activate_context","context_id":uuid.uuid4().hex,
                    "context_version":max(int(self.clock()*1000000),adapter.context_version+1)})
        except Exception as exc:
            self.errors["retired_context"]=getattr(exc,"code",type(exc).__name__)
        # Retained journals are evidence, including ambiguous upload/download
        # outcomes. Detaching the adapter and context guard retire execution;
        # neither scope contents nor UNKNOWN may be erased as cleanup.

    def accept_manifest(self, owner, adapter, data):
        payload=data["payload"]
        if payload.get("op")!="context_ready":return self.archive.accept(data)
        if payload.get("context_id")!=adapter.context_id or payload.get("context_version")!=adapter.context_version:return
        peers=payload.get("peers");epoch=payload.get("account_epoch",0)
        if not isinstance(peers,dict) or type(epoch) is not int or epoch<0:raise ValueError("CONTEXT_ACK_INVALID")
        with self.repository.write(owner) as tx:
            binding=tx.execute('SELECT * FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
            if not binding:return
            prefs=json.loads(binding['config_json'])
            if prefs.get('context_id')!=adapter.context_id:return
            if binding['workbench_id']!=adapter.binding.workbench_id or binding['account_uid']!=adapter.binding.account_uid:return
            prefs.update(context_ready=True,context_epoch=epoch,context_peers=peers)
            tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?',(json.dumps(prefs,ensure_ascii=False),owner))
        adapter.context_ready=True
        self.errors.pop("context:"+adapter.binding.account_uid,None)

    def install(self, owner, wb, uid):
        adapter=self.adapter(owner,wb,uid)
        self.transport.adapters={k:v for k,v in self.transport.adapters.items() if k[0]!=owner or k[2]!="oss"} | {(owner,uid,"oss"):adapter}
        return adapter

    def restore(self):
        if not self.configured:return
        with self.repository.db.transaction(write=False) as tx:
            bindings=[dict(r) for r in tx.execute("SELECT owner_id,workbench_id,account_uid,config_json FROM xj2_binding").fetchall()]
        for row in bindings:
            prefs=json.loads(row["config_json"])
            if prefs.get("relay_contract")==CONTRACT:
                if prefs.get("relay_institution_id")!=self.keys.institution_id:raise ValueError("已选账号不属于当前机构连接，请保存配置清空旧业务后重新选择账号")
                self.install(row["owner_id"],row["workbench_id"],row["account_uid"])

    def _reset_changed_native_accounts(self):
        """A signed fresh catalog proves a physical slot now uses another login.

        Expiry, missing heartbeats and an empty catalog are NOT account changes.
        Archive the old execution scope and require explicitly selecting the new UID.
        """
        from .fresh_state import clear_workspace
        books={row["workbench_id"]:row for row in self.directory.catalog()["workbenches"]}
        with self.repository.db.transaction(write=False) as tx:
            bindings=[dict(row) for row in tx.execute("SELECT * FROM xj2_binding").fetchall()]
        for binding in bindings:
            prefs=json.loads(binding['config_json']);slot=prefs.get('collector_account_id')
            book=books.get(binding['workbench_id'])
            if not slot or not book or book.get('stale'):continue
            rows=[row for row in book['accounts'] if row.get('channel')=='qidian' and row['account_id']==slot and row.get('available')]
            if len(rows)!=1 or rows[0]['account_uid']==binding['account_uid']:continue
            owner=binding['owner_id'];old_adapter=self.current_adapter(owner)
            with self.repository.write(owner) as tx:
                current=tx.execute("SELECT * FROM xj2_binding WHERE owner_id=?",(owner,)).fetchone()
                if not current or any(current[key]!=binding[key] for key in ('account_uid','workbench_id','version','config_json')):continue
                retained=clear_workspace(tx,owner,clock=self.repository.clock)
                tx.execute("DELETE FROM xj2_binding WHERE owner_id=?",(owner,))
                tx.execute("UPDATE xj2_owner SET projection_version=projection_version+1 WHERE owner_id=?",(owner,))
                self.repository.audit(tx,owner,'native_account_changed',rows[0]['account_uid'],
                    {'workbench_id':binding['workbench_id'],'previous_account_uid':binding['account_uid'],
                     'collector_account_id':slot,'retained_counts':retained})
            self.transport.adapters={key:value for key,value in self.transport.adapters.items() if key[0]!=owner}
            if old_adapter is not None:self.retire_context(old_adapter,notify=False)

    def tick(self):
        with self._lock:return self._tick()

    def _tick(self):
        if not self.configured:return
        if self.archive is not None and self.clock()-self._last_archive_prune>=3600:
            cleaned=self.archive.prune();self._last_archive_prune=self.clock()-(3600 if cleaned.get("more_pending") else 0)
        # Institution lock covers consumption; per-account business adapters retain
        # their independent source journals and are consumed by the existing Inbox.
        try:
            with account_lock(str(self.directory.path)+'.consume',self.keys.binding('directory','directory'),blocking=False):
                for path in (DIRECTORY_PATH,ORGANIZATION_PATH):
                    try:self.directory.consume(self.client(path),limit=100);self.errors.pop(path,None)
                    except Exception as exc:self.errors[path]=getattr(exc,"code",type(exc).__name__)
        except BlockingIOError:pass # Another application owns this shared intake round.
        self._reset_changed_native_accounts()
        adapters=list(self.transport.adapters.values())
        for adapter in adapters:
            if not isinstance(adapter,InstitutionTransport) or getattr(adapter.binding,'channel','qidian')!='qidian':continue
            self.request_context(adapter)
            wb,uid=adapter.binding.workbench_id,adapter.binding.account_uid;pair=(wb,uid)
            if self.clock()-self._last_friends.get(pair,0)>=10:
                self._last_friends[pair]=self.clock()
                try:self.directory.consume(self.client(account_paths(wb,uid)["friends_path"]),limit=50);self.errors.pop(str(pair),None)
                except Exception as exc:self.errors[str(pair)]=getattr(exc,"code",type(exc).__name__)

    def start(self):
        if not self.background_enabled:return
        if self._thread and self._thread.is_alive():return
        if not self.configured:return
        lease=account_lock(self.state/"directory-consumer",self.keys.binding("institution-directory","directory"),blocking=False)
        lease.__enter__();self._lease=lease
        try:
            self.restore();self._stop.clear()
            def run():
                while not self._stop.is_set():
                    try:self.tick()
                    except Exception as exc:self.errors["runtime"]=type(exc).__name__
                    self._stop.wait(1)
            self._thread=threading.Thread(target=run,name="institution-file-directory",daemon=True);self._thread.start()
        except BaseException:self._lease.__exit__(None,None,None);self._lease=None;raise

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
            if self._thread.is_alive():raise RuntimeError("机构目录取件正在完成持久化，请稍后停止")
        if self._lease:self._lease.__exit__(None,None,None);self._lease=None

    def settings_status(self, owner):
        binding=self.repository.binding_for(owner) or {}
        revision=hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.is_file() else ""
        applied=bool(self.configured and self._thread and self._thread.is_alive()
            and revision==self.applied_revision and not self.errors.get("configuration"))
        return {**{k:self.config[k] for k in TLS_FIELDS if self.config and k in self.config},"configured":self.configured,"backend":"file_gateway","contract":CONTRACT,"base_url":self.config.get("base_url","") if self.config else "","credentials_set":self.auth is not None,"background_enabled":self.background_enabled,"revision":revision,"applied":applied,"account_uid":binding.get("account_uid"),"workbench_id":binding.get("workbench_id"),"note":"配置已保存，但运行加载失败；请检查状态目录并重新保存重试。" if self.errors.get("configuration") else "机构目录共享；路径由系统内置，当前工作台一次选择一个企点号。"}

    def _candidate(self, body):
        if not isinstance(body,dict) or set(body)-{"base_url","config_yaml","expected_revision",*TLS_FIELDS}:raise ValueError("只允许服务地址和机构凭据，不允许编辑路径")
        config=dict(self.config or {"contract":CONTRACT,"base_url":"https://its-relay.yingmi.cn"})
        base=body.get("base_url",config.get("base_url",""));text=body.get("config_yaml","")
        if not isinstance(base,str) or len(base)>2048 or not isinstance(text,str):raise ValueError("机构连接配置格式无效")
        config["base_url"]=base.strip().rstrip("/")
        for field in TLS_FIELDS:
            if field in body:config[field]=body[field]
        config=resolve_tls_files(config,self.path.parent)
        from urllib.parse import urlsplit
        if urlsplit(base).hostname!=config.get("hostname"):config.pop("addresses",None);config.pop("hostname",None)
        try:
            auth=GatewayAuth.from_yaml(text) if text.strip() else self.auth
            if auth is None:raise ValueError("首次配置需要有效key和16字节sm4_key")
            client=connection_client(config,auth,path=DIRECTORY_PATH)
        except FileGatewayError as exc:
            raise ValueError("文件中转配置无效："+exc.code) from None
        return config,auth,client

    def settings_test(self, owner, body):
        config,auth,client=self._candidate(body)
        try:
            n=client.pending()
            return {"passed":True,"connected":True,"saved":False,"steps":[{"name":"机构目录连接","ok":True,"detail":"认证通过，待取文件数量 "+str(n)+"；未上传、未下载"}]}
        except FileGatewayError as exc:return {"passed":False,"connected":False,"saved":False,"steps":[{"name":"机构目录连接","ok":False,"detail":exc.code}]}

    def _apply_saved_settings(self, owner):
        try:
            revision=hashlib.sha256(self.path.read_bytes()).hexdigest()
            if self.applied_revision!=revision or self.errors.get("configuration"):
                self.load()
            self.start()
        except Exception:
            # Configuration is committed; do not report an unsaved draft or expose
            # database/filesystem exception text that may contain private values.
            self.errors["configuration"]="INSTITUTION_CONFIG_LOAD_FAILED"
            return {**self.settings_status(owner),"saved":True,"applied":False,
                    "error_code":"INSTITUTION_CONFIG_LOAD_FAILED"}
        self.errors.pop("configuration",None)
        return {**self.settings_status(owner),"saved":True}

    def settings_save(self, owner, body):
        from .fresh_state import clear_workspace
        import yaml
        with self._lock:
            raw=self.path.read_bytes() if self.path.exists() else b""
            revision=hashlib.sha256(raw).hexdigest() if raw else ""
            if body.get("expected_revision","")!=revision:raise ValueError("机构配置已经变化，请刷新后重试")
            config,auth,client=self._candidate(body)
            same=bool(self.configured and config["base_url"]==self.config["base_url"] and auth==self.auth and all(config.get(k)==self.config.get(k) for k in TLS_FIELDS))
            if same:return self._apply_saved_settings(owner)
            # Validate a replacement without altering the active configuration.
            try:
                if self.configured:client.pending()
            except FileGatewayError as exc:raise ValueError("新通道不可用，原配置未改变："+exc.code) from None
            self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            auth_path=self.path.parent/("institution-auth-"+uuid.uuid4().hex+".yaml")
            atomic_write(auth_path,yaml.safe_dump({"key":auth.key,"sm4_key":auth.sm4_key.decode("utf8")}).encode())
            config["auth_file"]=auth_path.name
            candidate_path=self.path.parent/("candidate-"+uuid.uuid4().hex+".json")
            atomic_write(candidate_path,json.dumps(config,ensure_ascii=False,indent=2).encode())
            try:candidate=InstitutionRuntime(self.repository,self.transport,candidate_path,self.root.parent,clock=self.clock,client_factory=self._factory)
            except Exception:
                candidate_path.unlink(missing_ok=True);auth_path.unlink(missing_ok=True);raise ValueError("新配置加载失败，原配置未改变") from None
        # The directory worker must finish its current read before changing credentials.
        self.changing=True
        try:self.close()
        except BaseException:
            self.changing=False
            raise
        with self._lock:
            if (self.path.read_bytes() if self.path.exists() else b"")!=raw:
                candidate_path.unlink(missing_ok=True);auth_path.unlink(missing_ok=True);self.changing=False;self.start();raise ValueError("机构配置已经变化，请刷新后重试")
            previous_adapters=dict(self.transport.adapters);removed={};committed=False
            try:
                # One DB transaction for all workspaces using this shared gateway.
                with self.repository.db.transaction() as tx:
                    owners=[r['owner_id'] for r in tx.execute('SELECT owner_id FROM xj2_binding').fetchall()]
                    for who in owners:
                        removed[who]=clear_workspace(tx,who,clock=self.repository.clock)
                        tx.execute('DELETE FROM xj2_binding WHERE owner_id=?',(who,))
                        tx.execute('UPDATE xj2_owner SET projection_version=projection_version+1 WHERE owner_id=?',(who,))
                    atomic_write(self.path,candidate_path.read_bytes())
                committed=True
                for adapter in previous_adapters.values():
                    if isinstance(adapter,InstitutionTransport):
                        try:self.retire_context(adapter)
                        except Exception:self.errors["retired_context"]="OLD_CONTEXT_RETIREMENT_FAILED"
                self.transport.adapters={}
                self.config,self.auth,self.keys=candidate.config,candidate.auth,candidate.keys
                self.state,self.directory,self.archive=candidate.state,candidate.directory,candidate.archive
                self.applied_revision=hashlib.sha256(self.path.read_bytes()).hexdigest()
                self.errors={};self._last_friends={}
            except BaseException as exc:
                if not committed:
                    if raw:atomic_write(self.path,raw)
                    else:self.path.unlink(missing_ok=True)
                    auth_path.unlink(missing_ok=True)
                    self.transport.adapters=previous_adapters;self.start()
                    raise ValueError("文件中转配置保存失败，原配置与业务未改变") from None
                self.transport.adapters={}
                self.errors['configuration']='INSTITUTION_CONFIG_LOAD_FAILED'
                raise ValueError("新配置已保存且旧工作区已归档，运行加载失败，请重新读取配置后重试") from None
            finally:
                self.changing=False
                candidate_path.unlink(missing_ok=True)
        result=self._apply_saved_settings(owner)
        return {**result,"reset":bool(removed),"note":"已更新配置并归档旧工作区，请选择新工作台与企点号；历史事实和待核对记录保留，不会重发旧任务。" if removed else result.get("note","")}

class InstitutionSettings:
    def __init__(self, runtime):self.runtime=runtime
    def _configured_runtime(self):
        if self.runtime is None:raise ValueError('机构文件通道未配置状态目录；请配置XUNJIA_STATE_DIR后重启服务')
        return self.runtime
    def status(self, owner):
        if self.runtime is None:
            return {'configured':False,'backend':'file_gateway','contract':CONTRACT,
                    'base_url':'','credentials_set':False,'background_enabled':False,'revision':'','applied':False,
                    'account_uid':None,'workbench_id':None,
                    'note':'机构文件通道未配置状态目录；请配置XUNJIA_STATE_DIR后重启服务'}
        return self.runtime.settings_status(owner)
    def save(self, owner, body):return self._configured_runtime().settings_save(owner,body)
    def test(self, owner, body):return self._configured_runtime().settings_test(owner,body)
