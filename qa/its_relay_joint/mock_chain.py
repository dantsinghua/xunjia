"""Integration harness: real project code, real loopback HTTP; synthetic native edge."""
import asyncio,hashlib,json,os,threading,time
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from relay.file_gateway import FileGatewayClient,GatewayAuth
from relay.gateway_paths import CONTRACT,InstitutionKeys,account_paths
from server.app import create_app
from server.auth import AuthConfig,AuthUser
from server.quote_control import QuoteControl
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from path_gateway_http import MockPathGateway
from mock_chatflow import MockChatflow
from server.negotiation.chatflow import Chatflows,ChatflowClient
from server.negotiation.settings import Aggregation

AUTH=GatewayAuth('mock-institution-A',b'0123456789abcdef')
AUTH_B=GatewayAuth('mock-institution-B',b'fedcba9876543210')

class NativeStore:
    def __init__(self):
        self.rows=[{'id':'qd01','self_uid':'10001','channel':'qidian','state':'running','enabled':True},
                   {'id':'qd02','self_uid':'10002','channel':'qidian','state':'running','enabled':True}]
        self.idem={};self.results={}
    def list_accounts(self,channel=None):return [dict(r) for r in self.rows if channel is None or r['channel']==channel]
    def outbox_seq_bounds(self):return (0,0)
    def idem_get(self,aid,key):return self.idem.get((aid,key))
    def get_command_result(self,trace):return self.results.get(trace)

class NativeSink:
    supports_xunjia_v22_guard=True
    def __init__(self,store):self.calls=[];self.store=store;self.mode='sent'
    async def submit(self,command):
        assert callable(command._xunjia_native_guard) and command._xunjia_native_guard(),'final production native guard rejected'
        self.calls.append(command)
        if self.mode=='unknown':return SimpleNamespace(ok=False,code='TIMEOUT',data={})
        trace='mock-'+command.idempotency_key
        h=hashlib.sha256(json.dumps(command.args,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        data={'ext_msg_id':'native-'+str(len(self.calls)),'confirmed_by':'synthetic-native-readback'}
        self.store.idem[command.account_id,command.idempotency_key]={'op':'send_text','args_hash':h,'status':'DONE','trace_id':trace}
        self.store.results[trace]={'ok':True,'code':'DELIVERED','data':data}
        return SimpleNamespace(ok=True,code='DELIVERED',data=data)

class Chain:
    def __init__(self,root,monkeypatch):
        self.root=Path(root);self.now=[time.time()];self.clock=lambda:self.now[0]
        for k in list(os.environ):
            if k.startswith(('XUNJIA_','QB_')):monkeypatch.delenv(k,raising=False)
        self.gateway=MockPathGateway(self.root/'gateway',[AUTH,AUTH_B],self.clock).start()
        self.client=lambda path:FileGatewayClient(self.gateway.base,AUTH,path=path,allow_http=True,clock=self.clock,timeout=3)
        auth=self.root/'synthetic.yaml';auth.write_text(json.dumps({'key':AUTH.key,'sm4_key':AUTH.sm4_key.decode()}));auth.chmod(0o600)
        self.cfg=self.root/'institution.json';self.cfg.write_text(json.dumps({'contract':CONTRACT,'base_url':self.gateway.base,'auth_file':auth.name,'allow_http':True}));self.cfg.chmod(0o600)
        monkeypatch.setenv('XUNJIA_INSTITUTION_CONFIG',str(self.cfg));monkeypatch.setenv('XUNJIA_STATE_DIR',str(self.root/'service-state'))
        self.model=MockChatflow(self.clock).start()
        self.flows=Chatflows(ChatflowClient(self.model.base,"mock-analysis",stage="analysis",timeout=3),ChatflowClient(self.model.base,"mock-reply",stage="reply",timeout=3))
        self.app=self.new_app();self.api=TestClient(self.app);self.api.__enter__()
        self.loop=asyncio.new_event_loop();self.thread=threading.Thread(target=self.loop.run_forever,daemon=True);self.thread.start()
        self.native=NativeStore();self.bus=NativeSink(self.native)
        settings=SimpleNamespace(workbench_id='wb-a',view=lambda:{'workbench_name':'mock desk'},saved=lambda:None)
        self.collector=InstitutionCollectorHost(store=self.native,bus=self.bus,loop=self.loop,keys=InstitutionKeys.from_auth(AUTH),config={'workbench_name':'mock desk','contract':CONTRACT},settings=settings,data_dir=self.root/'collector-state',legacy_inbox_disabled=lambda:True,
            friends_provider=lambda row:[{'uin':'peer-'+row['self_uid'],'name':'synthetic contact','remark':''}],
            organization_provider=lambda row:{'org_state':'ready','org':{'departments':[{'id':'d1','parent_id':'','name':'synthetic organization','order':0}],'members':[{'uin':'org-peer','name':'synthetic staff','department_id':'d1','account':'','order':0}]}},
            clock=self.clock,client_factory=self.client)
        # The ordinary scheduler is deliberately not started. Run its same tick methods
        # deterministically; account/native side effects only reach NativeSink.
        self.collector.active=True
        for row in self.native.rows:
            host=self.collector._make_host(row);host._active=True
            self.collector.hosts[row['id'],row['self_uid']]=host
        self.advertise();self.collector.metadata_once();self.runtime.tick()
    def new_app(self):
        app=create_app(database_url='sqlite:///'+str(self.root/'business.sqlite3'),enable_background=False,chatflows=self.flows,aggregation=Aggregation(0,0,1000,"mock-only-not-production"),auth_config=AuthConfig(sso_secret='s'*64,session_secret='t'*64,clock=self.clock),quote_control=QuoteControl(directory=self.root/'quote',managers=[]))
        app.state.institution_runtime._factory=self.client
        return app
    @property
    def runtime(self):return self.app.state.institution_runtime
    def advertise(self):
        self.collector.publisher.directory(self.collector.directory_rows());self.runtime.tick()
    def login(self,owner='owner-a'):
        self.api.headers['Authorization']='Bearer '+self.app.state.auth.issue_token(AuthUser(owner,owner))
    def select(self,owner='owner-a',uid='10001',version=0):
        self.login(owner)
        r=self.api.post('/api/im/binding/select',json={'workbench_id':'wb-a','account_uid':uid,'expected_version':version});assert r.status_code==200,r.text
        for h in self.collector.hosts.values():h.bridge.publish_capabilities()
        self.runtime.tick();self.app.state.inbox.poll_once()
        return r.json()['data']
    def pump(self,rounds=1):
        for _ in range(rounds):
            self.runtime.tick();self.app.state.dispatcher.run_once();self.app.state.session_reset.run_once()
            for h in self.collector.hosts.values():
                result=asyncio.run_coroutine_threadsafe(h.run_once(),self.loop).result(10)
                assert not result.get('errors'),result
            self.app.state.inbox.poll_once()
    def preview(self,owner='owner-a',peer='peer-10001',key='mock-rfq'):
        self.login(owner)
        r=self.api.post('/api/im/rfq/preview',json={'request_id':key,'items':[{'code':'SYN-MOCK','name':'synthetic','peer_ids':[peer],'our_side':'BUY','volume_wan':'1000','opening_yield_pct':'3.20','limit_yield_pct':'3.10','step_bp':'2'}]})
        assert r.status_code==200,r.text
        return r.json()['data']
    def confirm(self,owner='owner-a',peer='peer-10001',key='mock-rfq'):
        draft=self.preview(owner,peer,key)
        r=self.api.post('/api/im/rfq/send',json={'batch_id':draft['batch_id'],'expected_version':draft['version'],'request_id':key+'-confirm'})
        assert r.status_code==200,r.text
        return r.json()['data']
    def messages(self):
        with self.app.state.repository.db.transaction(write=False) as tx:return [dict(r) for r in tx.execute('SELECT * FROM xj2_message').fetchall()]
    def restart_service(self):
        self.api.__exit__(None,None,None);self.app=self.new_app();self.runtime.restore()
        self.api=TestClient(self.app);self.api.__enter__()
    def close(self):
        for h in self.collector.hosts.values():h._active=False
        self.collector.active=False
        self.api.__exit__(None,None,None)
        self.loop.call_soon_threadsafe(self.loop.stop);self.thread.join(3);assert not self.thread.is_alive();self.loop.close()
        self.gateway.close();self.model.close()