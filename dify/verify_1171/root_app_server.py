"""Real server.app in a fresh synthetic SQLite instance, with no real transport."""
import hmac
import json
from pathlib import Path
import threading
import time

from fastapi import Request
import uvicorn

from server.app import create_app
from server.auth import AuthConfig
from server.dify_client import DifyChatClient
from server.reference_data import ReferenceSnapshot

ROOT=Path('/verify')
SECRET=json.loads((ROOT/'credentials.json').read_text())
OWNER='integration-owner'
ACCOUNT='91000999'
PEER='synthetic-peer'
DATABASE=ROOT/('root-instance-'+str(time.time_ns())+'.sqlite')
EVENTS=[]


class Transport:
    def send(self,command):
        EVENTS.append({'kind':'fake_send','command_id':command['command_id']})
        return {k:command[k] for k in ('command_id','account_uid','peer_id','content_hash')}|{'status':'sent','evidence':'synthetic-local-ack-'+command['command_id']}
    def reconcile(self,command):return self.send(command)
    def cancel(self,command):
        return {k:command[k] for k in ('command_id','account_uid','peer_id','content_hash')}|{'status':'cancelled','evidence':'synthetic-local-cancel'}


class Reference:
    def snapshot(self):
        return ReferenceSnapshot(snapshot_id='integration-synthetic',as_of=time.strftime('%Y-%m-%d'),sources=[{'id':'fixture','origin':'synthetic','transport':'local','as_of':time.strftime('%Y-%m-%d')}],
            bonds=[{'bond_id':'synthetic-bond','code':'000100.IB','name':'合成债','market':'IB','master':{},'valuation':{},'quotes':[]}])


key=json.loads((ROOT/'root-app-key.json').read_text())['token']
app=create_app(database_url='sqlite:///'+str(DATABASE),auth_config=AuthConfig(sso_secret=SECRET['app'],session_secret=SECRET['app']),
    enable_background=False,transport=Transport(),chat_client=DifyChatClient('http://api:5001',key,timeout=90,total_timeout=180),
    reference_provider=Reference(),agent_service_token=SECRET['service'],agent_tool_token=SECRET['tool'])
service=app.state.inquiry
service.repo.bind_account(OWNER,ACCOUNT,'synthetic-workbench')
draft=service.preview(OWNER,'integration-preview',[{'code':'000100.IB','name':'合成债','peer_ids':[PEER],'our_side':'SELL','volume_wan':'1000','opening_yield_pct':'2.00','limit_yield_pct':'2.10','step_bp':'2'}])
service.confirm(OWNER,draft['batch_id'],draft['version'],'integration-confirm')
app.state.dispatcher.run_once()
assert service.sessions(OWNER)[0]['state']=='NEGOTIATING'
(ROOT/'root-instance.json').write_text(json.dumps({'database':str(DATABASE),'owner':OWNER,'account':ACCOUNT,'peer':PEER}))


def auth(request):
    if not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+SECRET['service']):raise PermissionError('synthetic admin denied')


def incoming(sequence):
    return service.ingest(OWNER,{'event_id':'integration-event-'+str(sequence),'account_uid':ACCOUNT,'peer_id':PEER,
        'kind':'private','source_seq':sequence,'text':'000100.IB 想了解一下这只券的可做量','occurred_at':time.time(),'historical':False})['message_id']


@app.post('/_verify/run')
def run(request:Request):
    auth(request)
    message=incoming(1)
    result=app.state.chat_runner.process(message)
    before=service.commands(OWNER)
    queued=[c for c in before if c['status']=='queued']
    with service.repo.db.transaction(write=False) as tx:
        turn=dict(tx.execute('SELECT * FROM xj_turn WHERE message_id=?',(message,)).fetchone())
    data=json.loads(turn['data_json'])
    proof={'result':result,'queued_before_dispatch':len(queued),'turn_state':turn['state'],'tool_calls':data.get('tool_calls',0),
        'validation_bound':bool(data.get('validation',{}).get('ref')),'source_message_id':message}
    app.state.dispatcher.run_once()
    proof['sent_after_dispatch']=sum(c['status']=='sent' for c in service.commands(OWNER))
    proof['transport_events']=EVENTS[:]
    (ROOT/'root-chain-result.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
    return proof


@app.post('/_verify/budget/start')
def budget_start(request:Request):
    auth(request)
    message=incoming(2)
    ref=app.state.chat_runner._start(message)
    if ref is None:raise ValueError('budget fixture could not start')
    context=app.state.chat_runner.context(ref)
    return {'turn_ref':ref,'tool_budget_ref':context['tool_budget_ref'],'turn_id':context['turn_id']}


@app.get('/_verify/health')
def health():return {'ok':True,'database':DATABASE.name,'external_transport':False}


if __name__=='__main__':
    tls=uvicorn.Config(app,host='0.0.0.0',port=8443,ssl_certfile='/verify/tls/root-backend.crt',ssl_keyfile='/verify/tls/root-backend.key',lifespan='off',access_log=False,log_level='warning')
    plain=uvicorn.Config(app,host='0.0.0.0',port=8000,lifespan='off',access_log=False,log_level='warning')
    threading.Thread(target=uvicorn.Server(plain).run,daemon=True).start();uvicorn.Server(tls).run()
