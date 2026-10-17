"""V2 source intake and execution host using the shared authenticated OSS/mail wire.
The old xj_command/xj_session dispatch and ingestion modules are not imported.
"""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from .prices import InvalidFact
from .store import encoded, identity, new_id
class Inbox:
    def __init__(self,service,transport):self.service,self.transport=service,transport
    def apply(self,binding,envelope):
        store=self.service.store;owner=binding.owner_user_id
        current=store.binding_for(owner)
        if not current or any(current[k]!=getattr(binding,k) or envelope.get(k)!=getattr(binding,k) for k in ('account_uid','workbench_id')):
            raise PermissionError('Collector/account binding does not match V2 business state')
        if envelope.get('owner_user_id')!=owner:raise PermissionError('Wrong event owner')
        payload=envelope.get('payload')
        if not isinstance(payload,dict):raise InvalidFact('Invalid input envelope')
        kind=envelope.get('kind')
        if kind=='receipt':
            row=self.service.commands_core.get(owner,payload.get('command_id'))
            with store.db.transaction(write=False) as tx:thread=store._thread(tx,owner,row['thread_id'])
            for key,value in (('command_id',row['command_id']),('content_hash',row['content_hash']),('peer_id',thread['peer_uid']),('account_uid',thread['account_uid'])):
                if payload.get(key)!=value or envelope.get(key)!=value:raise InvalidFact('Receipt identity/content mismatch')
            return self.service.commands_core.receipt(owner,row['command_id'],account_uid=thread['account_uid'],peer_uid=thread['peer_uid'],content_hash=row['content_hash'],status=str(payload.get('status','')).upper(),evidence=payload.get('evidence'),native_id=payload.get('native_id'),occurred_at=payload.get('executed_at'))
        if kind in ('contacts','catalog'):return None
        if kind!='event':raise InvalidFact('Unsupported business input envelope')
        peer=payload.get('peer_id')
        if peer!=envelope.get('peer_id'):raise InvalidFact('Counterparty envelope mismatch')
        identity(peer,'counterparty')
        direction=payload.get('direction',payload.get('dir','in'))
        direction={'incoming':'in','inbound':'in','outgoing':'out','outbound':'out'}.get(direction,direction)
        occurred=payload.get('occurred_at',payload.get('ts'))
        if type(occurred) in (int,float) and occurred>100000000000:occurred/=1000
        native=payload.get('ext_msg_id') or payload.get('native_id') or payload.get('source_message_id')
        source_command=payload.get('source_command_id')
        if direction=='out' and source_command:
            row=self.service.commands_core.get(owner,source_command)
            if json.loads(row['body_json'])['text']!=payload.get('text'):raise InvalidFact('Native echo differs from immutable command text')
            return self.service.commands_core.receipt(owner,source_command,account_uid=binding.account_uid,peer_uid=peer,content_hash=row['content_hash'],status='SENT',evidence='native:'+str(native),native_id=native,occurred_at=occurred)
        with store.write(owner) as tx:
            thread=tx.execute('SELECT * FROM xj2_thread WHERE owner_id=? AND account_uid=? AND peer_uid=?',(owner,binding.account_uid,peer)).fetchone()
            if thread:tid=thread['thread_id']
            else:
                tid=new_id();tx.execute('INSERT INTO xj2_thread(thread_id,owner_id,account_uid,peer_uid,ingest_revision,analyzed_revision) VALUES(?,?,?,?,0,0)',(tid,owner,binding.account_uid,peer))
            fence=envelope.get('manual_fence_id');version=envelope.get('manual_fence_version',0)
            if fence:
                if type(version) is not int or version<1:raise InvalidFact('Invalid receiver manual fence')
                tx.execute('UPDATE xj2_thread SET receiver_fence_id=?,receiver_fence_version=? WHERE thread_id=? AND receiver_fence_version<=?',(fence,version,tid,version))
            if direction=='out' and not payload.get('historical') and not envelope.get('transport_replay'):
                for p in tx.execute('SELECT member_id FROM xj2_peer WHERE thread_id=?',(tid,)).fetchall():
                    tx.execute('UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE member_id=?',(p['member_id'],))
                    store._cancel_commands(tx,member_id=p['member_id'],reason='NATIVE_HUMAN_MESSAGE')
        kind=payload.get('event_kind',payload.get('kind','message'))
        if kind=='private':kind='message'
        day=payload.get('trade_day')
        if not day and type(occurred) in (int,float):day=datetime.fromtimestamp(occurred,ZoneInfo('Asia/Shanghai')).date().isoformat()
        return store.ingest(owner,tid,event_id=payload.get('event_id'),source_seq=payload.get('source_seq'),text=payload.get('text'),
             trade_day=day,occurred_at=occurred,direction=direction,historical=bool(payload.get('historical') or envelope.get('transport_replay')),
             native_id=native,kind=kind,target_event_id=payload.get('target_event_id'))
    def poll_once(self):
        if self.transport is None:return 0
        adapters=list({id(a):a for a in getattr(self.transport,'adapters',{}).values()}.values())
        done=0
        for adapter in adapters:
            owner=adapter.binding.owner_user_id
            try:items=adapter.poll(limit=50)
            except Exception as exc:
                self.service.diagnostics.record('inbox',owner=owner,code='TRANSPORT_POLL_FAILED',error=exc);continue
            for item in items:
                try:self.apply(adapter.binding,item['envelope'])
                except (ValueError,PermissionError,KeyError,TypeError) as exc:
                    with self.service.store.write(owner) as tx:
                        self.service.store.audit(tx,owner,'inbound_quarantine',new_id(),{'error_type':type(exc).__name__})
                    self.service.diagnostics.record('inbox',owner=owner,code='INBOUND_QUARANTINED',error=exc)
                except Exception as exc:
                    self.service.diagnostics.record('inbox',owner=owner,code='BUSINESS_INGEST_RETRY',error=exc);continue
                try:adapter.ack(item['source_token']);done+=1
                except Exception as exc:self.service.diagnostics.record('inbox',owner=owner,code='CURSOR_ACK_RETRY',error=exc)
        return done
class Dispatcher:
    def __init__(self,service,transport,*,live_enabled=False):
        self.service,self.store,self.commands=service,service.store,service.commands_core
        self.transport=transport;self.live_enabled=live_enabled
        self._last_check={}
    def _wire(self,owner,row):
        with self.store.write(owner) as tx:
            original=self.commands._command(tx,owner,row['command_id'])
            result=json.loads(original['result_json'])
            if result.get('wire'):return result['wire']
            binding=self.store.require_binding(tx,owner)
            thread=self.store._thread(tx,owner,original['thread_id'])
            body=json.loads(original['body_json'])
            wire={'business_version':'2.2','command_id':original['command_id'],'owner_user_id':owner,
                  'account_uid':thread['account_uid'],'peer_id':thread['peer_uid'],'workbench_id':binding['workbench_id'],
                  'kind':'manual_send' if body['kind']=='MANUAL' else 'send_text','op':'send_text','text':body['text'],
                  'content_hash':original['content_hash'],'control_version':result['dispatch_version'],
                  'expires_at':original['expires_at'],'transport':binding['transport'],
                  'source_watermark':result['source_watermark'],'dispatch_watermark':result['dispatch_watermark']}
            if thread['receiver_fence_id']:
                wire.update(receiver_fence_id=thread['receiver_fence_id'],receiver_fence_version=thread['receiver_fence_version'])
            result['wire']=wire
            tx.execute('UPDATE xj2_command SET result_json=? WHERE command_id=?',(encoded(result),original['command_id']))
            return wire
    def _result(self,owner,wire,result):
        if not isinstance(result,dict) or any(result.get(k)!=wire[k] for k in ('command_id','account_uid','peer_id','content_hash')):
            raise InvalidFact('Transport result does not match command')
        state=result.get('status')
        if state in ('accepted','pending'):return
        self.commands.receipt(owner,wire['command_id'],account_uid=wire['account_uid'],peer_uid=wire['peer_id'],content_hash=wire['content_hash'],status=str(state).upper(),evidence=result.get('evidence'),native_id=result.get('native_id'),occurred_at=result.get('executed_at'))
    def _uncertain(self,owner,wire,exc):
        self.commands.receipt(owner,wire['command_id'],account_uid=wire['account_uid'],peer_uid=wire['peer_id'],content_hash=wire['content_hash'],status='UNKNOWN',evidence='transport-uncertain:'+type(exc).__name__)
        self.service.diagnostics.record('dispatch',owner=owner,code='DELIVERY_UNCERTAIN',error=exc,entity=wire['command_id'])
    def _compatible(self,wire):
        # A newly wired host must explicitly advertise that it checks source
        # watermarks. Old collectors may read V2 envelopes yet lack this check.
        if getattr(self.transport,'supports_v22',False):return True
        adapters=getattr(self.transport,'adapters',{})
        adapter=adapters.get((wire['owner_user_id'],wire['account_uid'],wire['transport']))
        if adapter is None:return False
        catalog=adapter.catalog()
        return (catalog.get('account_uid')==wire['account_uid'] and
                'negotiation-v22-watermark' in catalog.get('capabilities',[]))
    def run_once(self):
        if self.transport is None:return 0
        processed=0
        with self.store.db.transaction(write=False) as tx:
            pending=tx.execute("SELECT c.*,g.owner_id FROM xj2_command c JOIN xj2_group g ON g.group_id=c.group_id WHERE c.state IN ('PENDING','UNKNOWN') ORDER BY c.created_at LIMIT 20").fetchall()
        for row in pending:
            owner=row['owner_id'];cid=row['command_id']
            if self._last_check.get(cid,0)>self.store.clock():continue
            self._last_check[cid]=self.store.clock()+5
            wire=self._wire(owner,row)
            with self.store.write(owner) as tx:
                current=self.commands._command(tx,owner,cid)
                body=json.loads(current['body_json'])
                reason=self.commands._scope_gate(tx,owner,current['member_id'],json.loads(current['basis_json']),body,command_id=cid)
                cancel=bool(current['cancel_requested'] or reason or current['expires_at']<=self.store.clock())
            try:self._result(owner,wire,self.transport.cancel(wire) if cancel else self.transport.reconcile(wire))
            except Exception as exc:self._uncertain(owner,wire,exc)
            processed+=1
        if not self.live_enabled:return processed
        with self.store.db.transaction(write=False) as tx:
            queued=tx.execute("SELECT c.command_id,g.owner_id FROM xj2_command c JOIN xj2_group g ON g.group_id=c.group_id WHERE c.state='QUEUED' ORDER BY c.created_at,c.command_id LIMIT 20").fetchall()
        for row in queued:
            owner=row['owner_id'];cid=row['command_id']
            claim=self.commands.claim(owner,cid)
            if claim is None:continue
            wire=self._wire(owner,claim)
            try:
                if not self._compatible(wire):
                    self.commands.receipt(owner,cid,account_uid=wire['account_uid'],peer_uid=wire['peer_id'],content_hash=wire['content_hash'],status='FAILED',evidence='collector-v22-capability-not-confirmed-before-submit')
                    continue
                result=self.transport.send(wire) if self.commands.execution_permission(owner,cid) else self.transport.cancel(wire)
                self._result(owner,wire,result)
            except Exception as exc:self._uncertain(owner,wire,exc)
            processed+=1
        return processed