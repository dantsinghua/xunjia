"""Own the existing file coordinator in ordinary deployments, without preset accounts."""
from __future__ import annotations
import base64
import fcntl
import hashlib
import json
import os
import secrets
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

from relay.file_gateway import GatewayAuth,FileGatewayClient,FileGatewayError
from relay.file_gateway_connection import connection_client,connection_identity,load_connection
from relay.file_gateway_coordinator import make_handler,CoordinatorHTTPServer
from relay.file_gateway_journal import Journal
from relay.protocol import Codec,OwnerKeys
from .mq_settings import atomic_write
from .transport_config import load_transports


class ManagedMQ:
    def __init__(self, repository, transport, state_path):
        self.repository,self.transport=repository,transport
        self.root=Path(state_path)/'mq-connections'
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True);self.root.chmod(0o700)
        self._lock=threading.RLock();self._running={};self._claims={};self.errors={}

    def paths(self,owner):
        if not isinstance(owner,str) or not owner or len(owner)>160 or not owner.isprintable():
            raise PermissionError('当前用户身份无效')
        folder=self.root/hashlib.sha256(owner.encode()).hexdigest()
        return SimpleNamespace(folder=folder,connection=folder/'network.json',profiles=folder/'transport.json',
                               state=folder/'transport',local_auth=folder/'gateway-auth.yaml')

    def configured(self,owner):return self.paths(owner).connection.is_file()

    def _keys(self,owner):
        try:
            return OwnerKeys(owner,base64.b64decode(os.environ['XUNJIA_SEAL_KEY'],validate=True),
                             base64.b64decode(os.environ['XUNJIA_SIGN_KEY'],validate=True))
        except (KeyError,ValueError,TypeError):
            raise ValueError('文件中转配置可保存；采集端报文认证密钥XUNJIA_SEAL_KEY/XUNJIA_SIGN_KEY尚未配置或无效，不能读取目录') from None

    def reserve(self,owner,client):
        identity=connection_identity(client)
        with self._lock:
            if identity in self._claims:
                if self._claims[identity][0]!=owner:raise ValueError('此文件中转连接已由其他用户接入，不能启动第二个取件器')
                return
            locks=self.root/'.locks';locks.mkdir(mode=0o700,exist_ok=True)
            handle=(locks/(identity+'.lock')).open('a+b');os.chmod(handle.name,0o600)
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                handle.close();raise ValueError('此文件中转连接已有运行实例，不能启动第二个取件器') from None
            self._claims[identity]=(owner,handle)

    def initialize(self,owner,body):
        with self._lock:
            paths=self.paths(owner)
            if paths.connection.exists():raise ValueError('文件中转配置已变化，请重新读取后再保存')
            if not isinstance(body,dict) or set(body)-{'base_url','config_yaml','expected_revision'} or body.get('expected_revision','')!='':
                raise ValueError('请提交base_url、config.yaml内容及当前配置版本')
            try:auth=GatewayAuth.from_yaml(body.get('config_yaml',''))
            except FileGatewayError:
                raise ValueError('首次连接请填写有效config.yaml中的key和16字节sm4_key') from None
            base=body.get('base_url','')
            if not isinstance(base,str):raise ValueError('文件中转地址无效')
            config={'owner_user_id':owner,'base_url':base.strip().rstrip('/'),'auth_file':'remote-auth.yaml',
                    'allow_http':os.environ.get('XUNJIA_MQ_ALLOW_HTTP','0')=='1'}
            if urlsplit(base).scheme=='http' and not config['allow_http']:
                raise ValueError('当前部署仅允许HTTPS；内网HTTP网关须在部署配置明确设置XUNJIA_MQ_ALLOW_HTTP=1')
            try:upstream=connection_client(config,auth,allow_http=config['allow_http'])
            except FileGatewayError:
                raise ValueError('文件中转地址或config.yaml配置无效') from None
            self.reserve(owner,upstream)
            paths.folder.mkdir(mode=0o700,exist_ok=True);paths.folder.chmod(0o700)
            import yaml
            raw=yaml.safe_dump({'key':auth.key,'sm4_key':auth.sm4_key.decode('utf8')}).encode()
            atomic_write(paths.folder/'remote-auth.yaml',raw)
            local=yaml.safe_dump({'key':secrets.token_urlsafe(32),'sm4_key':secrets.token_hex(8)}).encode()
            atomic_write(paths.local_auth,local)
            atomic_write(paths.profiles,b'[]')
            atomic_write(paths.connection,json.dumps(config,ensure_ascii=False).encode())

    def ensure(self,owner):
        with self._lock:
            paths=self.paths(owner)
            if not paths.connection.is_file():raise ValueError('请先在页面保存文件中转地址和config.yaml内容')
            config=json.loads(paths.connection.read_bytes())
            if config.get('owner_user_id')!=owner:raise PermissionError('文件中转连接不属于当前用户')
            if owner in self._running:return paths
            keys=self._keys(owner)
            upstream=load_connection(paths.connection,allow_http=True)
            self.reserve(owner,upstream)
            paths.state.mkdir(mode=0o700,exist_ok=True)
            journal=Journal(paths.folder/'journal.db',upstream)
            auth=GatewayAuth.from_file(paths.local_auth)
            address=urlsplit(config.get('uplink_url',''))
            if address.hostname and address.hostname!='127.0.0.1':raise ValueError('本机目录网关必须使用回环地址')
            # Rebind the saved port on restart, keeping all durable transport scopes stable.
            port=address.port or 0
            from http.server import BaseHTTPRequestHandler
            server=CoordinatorHTTPServer(('127.0.0.1',port),BaseHTTPRequestHandler)
            try:
                actual=server.server_address[1]
                wanted={'uplink_url':f'http://127.0.0.1:{actual}/up','downlink_url':f'http://127.0.0.1:{actual}/down'}
                if any(config.get(k)!=v for k,v in wanted.items()):
                    config.update(wanted);atomic_write(paths.connection,json.dumps(config,ensure_ascii=False).encode())
                server.RequestHandlerClass=make_handler(journal,auth,paths.connection,directory_codec=Codec(keys))
                server.journal=journal
                thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.1},daemon=True,name='xunjia-mq-directory')
                thread.start();self._running[owner]=(server,thread,journal)
                if json.loads(paths.profiles.read_bytes()):
                    restored=load_transports(self.repository,paths.profiles,paths.state)
                    self.transport.adapters.update(restored.adapters)
            except BaseException:
                running=self._running.pop(owner,None)
                if running:server.shutdown();running[1].join(timeout=5)
                server.server_close();raise
            return paths

    def bootstrap_profile(self,owner):
        paths=self.ensure(owner);config=json.loads(paths.connection.read_bytes())
        return {'owner_user_id':owner,'seal_key_env':'XUNJIA_SEAL_KEY','sign_key_env':'XUNJIA_SIGN_KEY',
                'file_gateway':{**{k:config[k] for k in ('uplink_url','downlink_url')},
                                'auth_file':str(paths.local_auth),'allow_http':True}}

    def gateway(self,owner):
        paths=self.ensure(owner);config=json.loads(paths.connection.read_bytes());auth=GatewayAuth.from_file(paths.local_auth)
        return SimpleNamespace(inbound=FileGatewayClient(config['uplink_url'],auth,allow_http=True),
                               outbound=FileGatewayClient(config['downlink_url'],auth,allow_http=True))

    def restore(self):
        for path in self.root.glob('*/network.json'):
            try:
                owner=json.loads(path.read_bytes())['owner_user_id'];self.ensure(owner)
            except (ValueError,OSError,KeyError) as exc:
                self.errors[path.parent.name]=type(exc).__name__

    def close(self):
        with self._lock:
            for server,thread,_ in self._running.values():
                server.shutdown();thread.join(timeout=5);server.server_close()
            self._running.clear()
            for _,handle in self._claims.values():handle.close()
            self._claims.clear()
