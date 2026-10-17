"""Apply authenticated channel envelopes, then acknowledge transport cursors."""
from .business import digest,identity


class Inbox:
    def __init__(self,service,transport):self.service=service;self.transport=transport

    def adapters(self):
        values=getattr(self.transport,'adapters',{}).values()
        return list({id(adapter):adapter for adapter in values}.values())

    def poll_once(self):
        from concurrent.futures import ThreadPoolExecutor
        adapters=self.adapters()
        if not adapters:return 0
        with ThreadPoolExecutor(max_workers=min(5,len(adapters)),thread_name_prefix='xunjia-inbox') as pool:
            return sum(pool.map(self._poll_adapter,adapters))

    def _poll_adapter(self,adapter):
        processed=0
        owner=adapter.binding.owner_user_id
        component='inbox_'+getattr(adapter,'route','unknown')
        try:items=adapter.poll(limit=50)
        except Exception as exc:
            self.service.diagnostics.record(component,owner=owner,code='TRANSPORT_POLL_FAILED',error=exc)
            return 0
        failed=False
        for item in items:
            try:self.apply(adapter.binding,item['envelope'])
            except (ValueError,PermissionError,KeyError,TypeError) as exc:
                failed=True
                try:
                    with self.service.repo.db.transaction() as tx:
                        self.service._binding(tx,owner)
                        self.service._audit(tx,owner,'inbound_quarantine',identity('bad_'),{'envelope_hash':digest(item.get('envelope')),'reason':'invalid_business_event','error_type':type(exc).__name__})
                except Exception as persistence_error:
                    self.service.diagnostics.record(component,owner=owner,code='QUARANTINE_PERSIST_FAILED',error=persistence_error)
                    continue
                self.service.diagnostics.record(component,owner=owner,code='INBOUND_QUARANTINED',error=exc)
            except Exception as exc:
                failed=True
                self.service.diagnostics.record(component,owner=owner,code='BUSINESS_INGEST_RETRY',error=exc)
                continue
            try:adapter.ack(item['source_token']);processed+=1
            except Exception as exc:
                failed=True
                self.service.diagnostics.record(component,owner=owner,code='CURSOR_ACK_RETRY',error=exc)
        if not failed:self.service.diagnostics.record(component,owner=owner)
        return processed

    def apply(self,binding,envelope):
        stored=self.service.repo.binding_for(binding.owner_user_id)
        if not stored or any(stored[k]!=getattr(binding,k) for k in ('account_uid','workbench_id')):raise PermissionError('传输绑定已变更')
        if any(envelope.get(k)!=getattr(binding,k) for k in ('owner_user_id','account_uid','workbench_id')):raise PermissionError('信封归属不匹配')
        payload=envelope.get('payload')
        if not isinstance(payload,dict):raise ValueError('事件正文无效')
        kind=envelope.get('kind')
        if kind=='receipt':
            with self.service.repo.db.transaction(write=False) as tx:
                original=tx.execute('SELECT * FROM xj_command WHERE command_id=? AND owner_user_id=?',(payload.get('command_id'),binding.owner_user_id)).fetchone()
            if not original:raise ValueError('未知命令回执')
            for key in ('command_id','account_uid','peer_id','content_hash'):
                if payload.get(key)!=original[key] or envelope.get(key)!=original[key]:raise PermissionError('回执不匹配原命令')
            self.service.receipt(original['command_id'],account_uid=binding.account_uid,peer_id=original['peer_id'],status=payload.get('status'),evidence=payload.get('evidence'),native_id=payload.get('native_id'))
        elif kind=='event':
            direction=payload.get('direction',payload.get('dir','in'))
            direction={'inbound':'in','incoming':'in','outbound':'out','outgoing':'out'}.get(direction,direction)
            occurred=payload.get('occurred_at',payload.get('ts'))
            if isinstance(occurred,(int,float)) and occurred>100000000000:occurred/=1000
            peer=payload.get('peer_id')
            if peer!=envelope.get('peer_id'):raise PermissionError('事件对手与信封不匹配')
            message_kind=payload.get('event_kind',payload.get('kind','message'))
            if message_kind=='private':message_kind='message'
            source_command=payload.get('source_command_id')
            if source_command and direction=='out':
                with self.service.repo.db.transaction(write=False) as tx:
                    original=tx.execute('SELECT * FROM xj_command WHERE command_id=? AND owner_user_id=?',(source_command,binding.owner_user_id)).fetchone()
                if not original:raise ValueError('出站关联命令不存在')
                import json
                body=json.loads(original['body_json'])
                if original['account_uid']!=binding.account_uid or original['peer_id']!=peer or body.get('text')!=payload.get('text'):raise PermissionError('出站事件不匹配原命令')
                native_id=payload.get('source_message_id') or payload.get('ext_msg_id') or payload.get('native_id')
                if not isinstance(native_id,str) or not native_id:raise ValueError('出站关联缺少原生消息ID')
                self.service.receipt(source_command,account_uid=binding.account_uid,peer_id=peer,status='sent',evidence='native:'+native_id,native_id=native_id)
                return
            event={'event_id':payload.get('event_id'),'account_uid':binding.account_uid,'peer_id':peer,'kind':message_kind,
                   'source_seq':payload.get('source_seq'),'text':payload.get('text'),'occurred_at':occurred,
                   'historical':payload.get('historical',False),'direction':direction}
            native=payload.get('source_message_id') or payload.get('ext_msg_id') or payload.get('native_id')
            if native:event['native_message_id']=native
            if message_kind in ('edit','retract'):event['target_event_id']=payload.get('target_event_id')
            self.service.ingest(binding.owner_user_id,event,force_historical=envelope.get('transport_replay') is True,receiver_fence_id=envelope.get('manual_fence_id'),receiver_fence_version=envelope.get('manual_fence_version',0))
        elif kind in ('catalog','contacts'):
            if payload.get('account_uid')!=binding.account_uid:raise PermissionError('目录账号不匹配')
        else:raise ValueError('未知入站事件')
