"""Isolated synthetic backend and OpenAI-compatible model; never calls upstream."""
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(os.environ.get('VERIFY_DATA','/verify'))
LOCK=threading.Lock()
COUNTS={}


def event(name,**facts):
    with LOCK:
        with (ROOT/'fixture-events.jsonl').open('a') as f:
            f.write(json.dumps({'event':name,'time':time.time(),**facts},ensure_ascii=False)+'\n')


def proposal(turn='verify-turn-1',epoch=1,source='verify-message-1'):
    return dict(schema_version='1',turn_id=turn,conversation_epoch=epoch,source_message_id=source,
        observations=[],proposed_actions=[{'type':'ASK_QUOTE','rfq_session_id':'verify-session-1'}],
        public_message=None,trader_view={'reason':'合成数据与工具调用已核对'},memory_patch=None,history_request=None,validation_ref=None)


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def send(self,value,status=200):
        encoded=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
    def do_GET(self):
        if self.path in ('/health','/v1/models','/models'):
            return self.send({'object':'list','data':[{'id':'xunjia-fixture','object':'model','owned_by':'synthetic'}]})
        return self.send({'error':'not_found'},404)
    def do_POST(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=1048576:return self.send({'error':'size'},400)
        try:body=json.loads(self.rfile.read(size))
        except ValueError:return self.send({'error':'json'},400)
        if self.path in ('/v1/chat/completions','/chat/completions'):return self.model(body)
        if self.path.startswith('/internal/agent/'):
            if self.headers.get('Authorization')!='Bearer '+os.environ['VERIFY_SERVICE_TOKEN']:return self.send({'ok':False},403)
            if not re.fullmatch(r'verify-turn-ref-[12]',str(body.get('turn_ref',''))):return self.send({'ok':False},403)
            index=body['turn_ref'].rsplit('-',1)[-1]
            if self.path.endswith('/context'):
                event('context',turn=index)
                return self.send({'ok':True,'context':{'turn_id':'verify-turn-'+index,'conversation_epoch':int(index),'source_message_id':'verify-message-'+index,
                    'decision_context':{'tool_budget_ref':'verify-budget-'+index,'our_side':'SELL','limit_yield_pct':'2.10','peer_yield_pct':'2.40',
                        'history_budget_remaining':1,'market':[],'actual_journal':[{'id':'verify-message-'+index,'direction':'in','text':'请问这只券目前可做多少量？'}]}}})
            if self.path.endswith('/history'):
                event('history',turn=index);return self.send({'ok':True,'messages':[{'id':'verify-history-1','direction':'in','text':'合成历史'}]})
            if self.path.endswith('/validate'):
                candidate=body.get('proposal',{})
                if candidate.get('turn_id')!='verify-turn-'+index or candidate.get('conversation_epoch')!=int(index):return self.send({'ok':False},409)
                if candidate.get('public_message') is None:
                    event('validate_action',turn=index)
                    return self.send({'ok':True,'needs_public_reply':True,'public_context':{'allowed_actions':['ASK_QUOTE'],'public_facts':[],
                        'peer_message':'请问这只券目前可做多少量？','relationship_context':[]},'validation_ref':None})
                if candidate.get('public_message')!='请问这只券目前可做多少量？':return self.send({'ok':False},400)
                event('validate_text',turn=index)
                return self.send({'ok':True,'validation_ref':'fixture-validated-'+index})
        if self.path.startswith('/api/agent/tools/'):
            if self.headers.get('Authorization')!='Bearer '+os.environ['VERIFY_TOOL_TOKEN']:return self.send({'error':'auth'},403)
            ref=body.get('tool_budget_ref')
            if ref not in ('verify-budget-1','verify-budget-2'):return self.send({'error':'scope'},403)
            with LOCK:COUNTS[ref]=COUNTS.get(ref,0)+1;count=COUNTS[ref]
            if count>6:return self.send({'error':'budget'},429)
            from server.bond_tools import BondTools
            name=self.path.rsplit('/',1)[-1]
            try:arguments=json.loads(body.get('arguments_json',''))
            except ValueError:return self.send({'error':'json'},400)
            event('tool',tool=name,turn=ref[-1],count=count)
            return self.send(BondTools().invoke(name,arguments))
        return self.send({'error':'not_found'},404)
    def model(self,body):
        if body.get('model')!='xunjia-fixture':return self.send({'error':{'message':'fixture model only'}},400)
        messages=body.get('messages',[])
        text=json.dumps(messages,ensure_ascii=False)
        joined='\n'.join(str(m.get('content','')) for m in messages)
        context={}
        marker='可信本轮上下文:\n'
        if marker in joined:
            try:context=json.JSONDecoder().raw_decode(joined.split(marker,1)[1])[0]
            except ValueError:pass
        calls=None
        if body.get('tools') and not any(m.get('role')=='tool' for m in messages):
            names=[t['function']['name'] for t in body['tools']]
            target=next((n for n in names if 'bond_calc' in n),None)
            if not target:return self.send({'error':{'message':'bond_calc absent'}},400)
            ref=context.get('decision_context',{}).get('tool_budget_ref','missing-budget')
            arguments={'arguments_json':json.dumps({'schema_version':'1','operation':'yield_gap','inputs':{'our_side':'SELL','peer_side':'bid','limit_yield_pct':'2.10','peer_yield_pct':'2.40','same_basis':True}}),'tool_budget_ref':ref}
            calls=[{'id':'call-fixture-1','type':'function','function':{'name':target,'arguments':json.dumps(arguments)}}]
            content=None;event('model_tool_request',tool=target)
        elif body.get('tools'):
            tool_text='\n'.join(str(m.get('content','')) for m in messages if m.get('role')=='tool')
            if 'raw_gap_bp' not in tool_text or 'analysis_only' not in tool_text or 'tool invoke error' in tool_text:
                event('fixture_tool_failed')
                return self.send({'error':{'message':'synthetic golden tool result missing'}},400)
            # Agent query holds a JSON context inside a message's content string.
            match=re.search(r'verify-turn-([12])',joined);index=match.group(1) if match else '1'
            candidate=proposal(context.get('turn_id','verify-turn-'+index),context.get('conversation_epoch',int(index)),context.get('source_message_id','verify-message-'+index))
            sessions=context.get('decision_context',{}).get('sessions',[])
            if sessions:candidate['proposed_actions'][0]['rfq_session_id']=sessions[0]['session_id']
            content=json.dumps(candidate,ensure_ascii=False)
            event('model_private_proposal',turn=candidate['turn_id'])
        elif '受限的公开消息措辞节点' in text:
            if any(token in text for token in ('limit_yield_pct','verify-turn-ref','verify-budget','PRIVATE-')):
                event('public_context_leak');return self.send({'error':{'message':'private context leaked'}},400)
            content='请问这只券目前可做多少量？';event('model_public_reply')
        else:content='fixture credential validation only';event('model_credential_probe')
        message={'role':'assistant','content':content}
        if calls:message['tool_calls']=calls
        finish='tool_calls' if calls else 'stop'
        if not body.get('stream'):
            return self.send({'id':'chatcmpl-fixture','object':'chat.completion','created':int(time.time()),'model':'xunjia-fixture',
                              'choices':[{'index':0,'message':message,'finish_reason':finish}],'usage':{'prompt_tokens':20,'completion_tokens':20,'total_tokens':40}})
        chunks=[]
        delta={'role':'assistant','content':content or ''}
        if calls:delta['tool_calls']=[dict(index=0,**calls[0])]
        for d,f in ((delta,None),({},finish)):
            chunks.append({'id':'chatcmpl-fixture','object':'chat.completion.chunk','created':int(time.time()),'model':'xunjia-fixture',
                           'choices':[{'index':0,'delta':d,'finish_reason':f}]})
        payload=(''.join('data: '+json.dumps(c,ensure_ascii=False)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)


if __name__=='__main__':
    ROOT.mkdir(exist_ok=True)
    plain=ThreadingHTTPServer(('0.0.0.0',8000),Handler)
    secure=ThreadingHTTPServer(('0.0.0.0',8443),Handler)
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain('/verify/tls/server.crt','/verify/tls/server.key')
    secure.socket=tls.wrap_socket(secure.socket,server_side=True)
    threading.Thread(target=plain.serve_forever,daemon=True).start();secure.serve_forever()
