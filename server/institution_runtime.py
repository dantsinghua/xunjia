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
from relay.file_gateway_connection import connection_client,protected_auth_path
from relay.gateway_paths import CONTRACT,DIRECTORY_PATH,ORGANIZATION_PATH,InstitutionKeys,account_paths
from relay.institution_archive import InstitutionArchive
from relay.institution_directory import InstitutionDirectory
from relay.protocol import token
from .institution_transport import InstitutionTransport
from .receiver_ledger import account_lock
from .mq_settings import atomic_write

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
        raw=self.path.read_bytes();config=json.loads(raw)
        if not isinstance(config,dict) or config.get("contract")!=CONTRACT:raise ValueError("机构文件通道合同不匹配")
        if set(config)-{"contract","base_url","auth_file","allow_http","hostname","addresses"}:raise ValueError("机构配置含不允许字段；路径由系统内置")
        auth=GatewayAuth.from_file(protected_auth_path(self.path,config["auth_file"]))
        keys=InstitutionKeys.from_auth(auth)
        if self.keys is not None and keys.institution_id!=self.keys.institution_id:raise ValueError("机构身份变更需要停止接入并完成迁移")
        connection_client(config,auth,allow_http=config.get("allow_http",False),path=DIRECTORY_PATH)
        self.config,self.auth,self.keys=config,auth,keys
        self.state=self.root/keys.institution_id;self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.directory=InstitutionDirectory(self.state/"directory.db",keys,clock=self.clock)
        self.archive=InstitutionArchive(self.state/"archive",clock=self.clock)
        self.applied_revision=hashlib.sha256(raw).hexdigest()

    def client(self, path):
        if not self.configured:raise ValueError("请先配置机构文件中转服务")
        return self._factory(path) if self._factory else connection_client(self.config,self.auth,allow_http=self.config.get("allow_http",False),path=path)

    def online(self, wb, uid):
        row=self.directory.account(wb,uid) if self.directory else None
        return bool(row and row["available"])

    def adapter(self, owner, wb, uid):
        with self._lock:
            key=(owner,uid,"oss");old=self.transport.adapters.get(key)
            if isinstance(old,InstitutionTransport) and old.binding.workbench_id==wb:return old
            binding=self.keys.binding(wb,uid,owner);paths=account_paths(wb,uid)
            ident=hashlib.sha256(json.dumps([owner,wb,uid]).encode()).hexdigest()
            adapter=InstitutionTransport(binding,self.client(paths["uplink_path"]),self.client(paths["downlink_path"]),self.state/"accounts"/(ident+".db"),role="service",institution_owner=self.keys.owner_id,clock=self.clock)
            adapter.online_guard=lambda:self.online(wb,uid)
            adapter.manifest_handler=self.archive.accept
            adapter.catalog_provider=lambda:{"account_uid":uid,"workbench_id":wb,"capabilities":self.directory.account(wb,uid).get("capabilities",[]) if self.online(wb,uid) else []}
            adapter.contacts_provider=lambda:self.directory.contacts(wb,uid,owner)
            return adapter

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
                if prefs.get("relay_institution_id")!=self.keys.institution_id:raise ValueError("已选账号不属于当前机构连接；旧状态保留，需显式迁移")
                self.install(row["owner_id"],row["workbench_id"],row["account_uid"])

    def tick(self):
        if not self.configured:return
        if self.archive is not None and self.clock()-self._last_archive_prune>=3600:
            cleaned=self.archive.prune();self._last_archive_prune=self.clock()-(3600 if cleaned.get("more_pending") else 0)
        # Institution lock covers consumption; per-account business adapters retain
        # their independent source journals and are consumed by the existing Inbox.
        for path in (DIRECTORY_PATH,ORGANIZATION_PATH):
            try:self.directory.consume(self.client(path),limit=100);self.errors.pop(path,None)
            except Exception as exc:self.errors[path]=getattr(exc,"code",type(exc).__name__)
        adapters=list(self.transport.adapters.values())
        for adapter in adapters:
            if not isinstance(adapter,InstitutionTransport):continue
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
        return {"editable":True,"configured":self.configured,"backend":"file_gateway","contract":CONTRACT,"base_url":self.config.get("base_url","") if self.config else "","credentials_set":self.auth is not None,"background_enabled":self.background_enabled,"revision":self.applied_revision,"applied":bool(self.configured and self._thread and self._thread.is_alive()),"account_uid":binding.get("account_uid"),"workbench_id":binding.get("workbench_id"),"note":"机构目录共享；路径由系统内置，当前工作台一次选择一个企点号。"}

    def _candidate(self, body):
        if not isinstance(body,dict) or set(body)-{"base_url","config_yaml","expected_revision"}:raise ValueError("只允许服务地址和机构凭据，不允许编辑路径")
        config=dict(self.config or {"contract":CONTRACT,"allow_http":os.environ.get("XUNJIA_MQ_ALLOW_HTTP")=="1"})
        base=body.get("base_url",config.get("base_url",""));text=body.get("config_yaml","")
        if not isinstance(base,str) or len(base)>2048 or not isinstance(text,str):raise ValueError("机构连接配置格式无效")
        config["base_url"]=base.strip().rstrip("/")
        from urllib.parse import urlsplit
        if urlsplit(base).hostname!=config.get("hostname"):config.pop("addresses",None);config.pop("hostname",None)
        auth=GatewayAuth.from_yaml(text) if text.strip() else self.auth
        if auth is None:raise ValueError("首次配置需要有效key和16字节sm4_key")
        client=connection_client(config,auth,allow_http=config.get("allow_http",False),path=DIRECTORY_PATH)
        return config,auth,client

    def settings_test(self, owner, body):
        config,auth,client=self._candidate(body)
        try:
            n=client.pending()
            return {"passed":True,"connected":True,"saved":False,"steps":[{"name":"机构目录连接","ok":True,"detail":"认证通过，待取文件数量 "+str(n)+"；未上传、未下载"}]}
        except FileGatewayError as exc:return {"passed":False,"connected":False,"saved":False,"steps":[{"name":"机构目录连接","ok":False,"detail":exc.code}]}

    def settings_save(self, owner, body):
        with self._lock:
            raw=self.path.read_bytes() if self.path.exists() else b""
            revision=hashlib.sha256(raw).hexdigest() if raw else ""
            if body.get("expected_revision","")!=revision:raise ValueError("机构配置已经变化，请刷新后重试")
            config,auth,_=self._candidate(body)
            if self.configured and (config["base_url"]!=self.config["base_url"] or auth!=self.auth):
                raise ValueError("机构地址或密钥变更需停机迁移；原配置与UNKNOWN均保留，未切换")
            if self.configured:return {**self.settings_status(owner),"saved":True}
            self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            import yaml
            name="institution-auth-"+uuid.uuid4().hex+".yaml";auth_path=self.path.parent/name
            atomic_write(auth_path,yaml.safe_dump({"key":auth.key,"sm4_key":auth.sm4_key.decode("utf8")}).encode())
            config["auth_file"]=name
            atomic_write(self.path,json.dumps(config,ensure_ascii=False,indent=2).encode())
            self.load();self.start()
            return {**self.settings_status(owner),"saved":True}

class InstitutionSettings:
    def __init__(self, runtime):self.runtime=runtime
    def status(self, owner):return self.runtime.settings_status(owner)
    def save(self, owner, body):return self.runtime.settings_save(owner,body)
    def test(self, owner, body):return self.runtime.settings_test(owner,body)
