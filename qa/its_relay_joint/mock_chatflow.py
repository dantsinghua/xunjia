"""Synthetic Dify HTTP edge. Exercises the production ChatflowClient, not a model."""
import json,threading,uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class MockChatflow:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.bad_reply=False
    def start(self):
        edge=self
        class H(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                assert self.path=='/v1/chat-messages'
                n=int(self.headers['Content-Length']);assert 0<n<=2*1024*1024
                req=json.loads(self.rfile.read(n));ctx=json.loads(req['inputs']['context_json']);stage=ctx['stage']
                assert self.headers['Authorization']=='Bearer mock-'+stage
                assert req['response_mode']=='blocking' and req['user'].startswith('xunjia-')
                edge.calls.append({'stage':stage,'context':ctx})
                if stage=='analysis':
                    scopes={};observations=[]
                    for msg in ctx['messages']:
                        hits=[p for p in ctx['members'] if p['bond_code'] in msg['text']]
                        scopes[msg['message_id']]=[p['member_id'] for p in hits]
                        if msg['direction']!='in' or msg['historical'] or msg['kind']!='message':continue
                        for p in hits:
                            data={'price':{'kind':'YIELD','value':'3.10'},'volume_wan':'500','confirmed_day':datetime.fromtimestamp(edge.clock(),ZoneInfo('Asia/Shanghai')).date().isoformat(),'validity':'ACTIVE','finality':'NEGOTIABLE'}
                            observations.append({'member_id':p['member_id'],'side':'ofr','changes':data,'evidence':{k:[msg['message_id']] for k in data}})
                    value={'schema_version':'2.2','scopes':scopes,'observations':observations,'unresolved':{}}
                else:
                    text='3.99%\u6536\uff0c500\u4e07' if edge.bad_reply else '3.19%\u6536\uff0c500\u4e07'
                    value={'schema_version':'2.2','action':'SEND','proposal':{'kind':'REPLY','text':text,'our_price':{'kind':'YIELD','value':'3.19'}},'trader_view':{'reason':'synthetic HTTP model response'}}
                data=json.dumps({'answer':json.dumps(value,ensure_ascii=False),'message_id':str(uuid.uuid4()),'conversation_id':'mock-conversation','workflow_run_id':'mock-run'},ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),H);self.server.daemon_threads=True
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True);self.thread.start();return self
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(3)