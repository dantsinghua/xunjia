"""Injectable collector loop; does not start accounts, pollers or network clients.

Disable the legacy xunjia poller before installing this bridge on the same OSS
prefix. Both routes must share the new receiver ledger and source publication
state. Run run_once() in the collector's existing scheduler, not a second agent.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import PurePosixPath

from relay.protocol import Codec, ProtocolError, digest, token
from server.receiver_ledger import ReceiverLedger
from server.transports import TransportUnavailable


class CollectorBridge:
    def __init__(self, *, ledger: ReceiverLedger, adapters: dict, native_send,
                 native_reconcile, primary_transport: str | None = None, object_store=None,
                 echo_wait_seconds=5, native_verification_budget=5):
        if not (0 < echo_wait_seconds <= 5 and echo_wait_seconds <= native_verification_budget):
            raise ValueError('echo等待须为正数且不超过5秒及native验证预算')
        self.echo_wait_seconds=echo_wait_seconds
        if primary_transport is not None and (primary_transport not in adapters or primary_transport not in ('oss','mail')):
            raise TransportUnavailable('NOT_CONFIGURED: COLLECTOR_PRIMARY_TRANSPORT')
        self.ledger, self.binding, self.adapters = ledger, ledger.binding, adapters
        self.native_send, self.native_reconcile = native_send, native_reconcile
        # The construction hint is accepted for existing schedulers only. Native
        # events always use every configured uplink; command.transport selects replies.
        self.object_store = object_store
        self.last_publication_errors = []
        self.codec = Codec(self.binding, clock=ledger.clock)
        for route,adapter in adapters.items():
            if route not in ('oss','mail') or adapter.binding != self.binding:
                raise ProtocolError('采集桥通道与固定绑定不匹配')
        paths={adapter.store.path.resolve() for adapter in adapters.values()}
        if not adapters or len(paths)!=1:raise ProtocolError('两路采集上行必须共享同一持久事件账本')
        self.prefix=f'wb/{token(self.binding.workbench_id)}/in/xunjia/'

    def _oss_inputs(self, limit, errors):
        if 'oss' not in self.adapters:return []
        if self.object_store is None:
            errors.append('NOT_CONFIGURED: COLLECTOR_OSS_LIST_GET');return []
        try:names=self.object_store.list(self.prefix)
        except Exception:
            errors.append('OSS_INBOX_LIST_UNAVAILABLE');return []
        def priority(name):
            leaf=PurePosixPath(name).name
            return (0 if leaf.startswith('cancel-') else 1 if leaf.startswith('query-') else 2,name)
        pending=[]
        for name in sorted(names,key=priority):
            if not isinstance(name,str) or not name.startswith(self.prefix) or not name.endswith('.bin'):
                continue
            leaf=name[len(self.prefix):]
            if '/' in leaf or '..' in leaf:continue
            try:blob=self.object_store.get(name)
            except Exception:
                errors.append('OSS_INBOX_GET_UNAVAILABLE');continue
            if blob is None:continue
            fingerprint=hashlib.sha256(blob).hexdigest();source='oss:'+name
            if self.ledger.source_done(source,fingerprint):continue
            try:env=self.codec.unpack(blob)
            except ProtocolError:
                self.ledger.mark_source(source,fingerprint,reason='protocol-rejected')
                errors.append('OSS_INBOX_PROTOCOL_REJECTED');continue
            pending.append((env,source,fingerprint,None,None))
            if len(pending)>=limit:break
        return pending

    def _mail_inputs(self, limit, errors):
        adapter=self.adapters.get('mail')
        if adapter is None:return []
        try:rows=adapter.poll(limit=limit)
        except Exception:
            errors.append('MAIL_INBOX_UNAVAILABLE');return []
        return [(row['envelope'],'mail:'+row['source_token'],digest(row['envelope']),adapter,row['source_token']) for row in rows]

    def run_once(self, limit=50):
        if not 1<=limit<=200:raise ValueError('采集批量须为1至200')
        processed=0;errors=[]
        try:self.flush_deferred()
        except Exception:errors.append('DEFERRED_EVENT_PUBLICATION_PENDING')
        inputs=self._oss_inputs(limit,errors)+self._mail_inputs(limit,errors)
        order={'cancel':0,'query':1,'command':2}
        for env,source,fingerprint,adapter,source_token in sorted(inputs,key=lambda item:order.get(item[0].get('kind'),3)):
            try:
                if self.ledger.source_done(source,fingerprint):
                    if adapter:adapter.ack(source_token)
                    continue
                # Mail poll has authenticated the envelope; revalidate current
                # identity/expiry immediately before entering the execution ledger.
                self.codec._validate(env)
                kind=env['kind'];command=env['payload']
                if kind not in order:
                    self.ledger.mark_source(source,fingerprint,reason='unsupported-downlink-kind')
                    if adapter:adapter.ack(source_token)
                    errors.append('UNSUPPORTED_DOWNLINK_KIND');continue
                reply=self.adapters.get(command.get('transport'))
                if reply is None:raise TransportUnavailable('NOT_CONFIGURED: ORIGINAL_REPLY_TRANSPORT')
                if kind=='cancel':outcome=self.ledger.cancel(command)
                elif kind=='query':outcome=self.ledger.reconcile(command,self.native_reconcile)
                else:outcome=self.ledger.execute(command,self.native_send)
                self.flush_deferred()
                # A held command has never entered native execution. Keep the
                # original source unacked so clearing an echo hold can retry it.
                if outcome.get('evidence')=='native-echo-classification-held':continue
                # Persist the outcome and receipt queue before acknowledging input.
                # Network failure later does not require repeating the native action.
                reply.queue_receipt(command,outcome)
                self.ledger.mark_source(source,fingerprint)
                if adapter:adapter.ack(source_token)
                processed+=1
            except ProtocolError:
                self.ledger.mark_source(source,fingerprint,reason='command-validation-rejected')
                if adapter:adapter.ack(source_token)
                errors.append('COMMAND_VALIDATION_REJECTED')
            except Exception:
                errors.append('COMMAND_OR_RECEIPT_PERSISTENCE_UNAVAILABLE')
        for adapter in self.adapters.values():
            try:adapter.flush_publications(limit=limit)
            except Exception:errors.append('RECEIPT_PUBLICATION_PENDING')
        for route,adapter in self.adapters.items():
            try:adapter.flush_events(limit=limit)
            except Exception:errors.append(route+':EVENT_PUBLICATION_PENDING')
        return {'processed':processed,'errors':errors}

    def publish_capabilities(self,*,updated_at=None):
        payload={'account_uid':self.binding.account_uid,'workbench_id':self.binding.workbench_id,
                 'updated_at':self.ledger.clock() if updated_at is None else updated_at,
                 'capabilities':['negotiation-v22-watermark'],'accounts':[{'account_uid':self.binding.account_uid}]}
        return [adapter.publish_catalog(payload) for adapter in self.adapters.values()]
    def replay_events(self,first_seq,last_seq):
        """Bounded administrator recovery; immutable native facts, read-only replay.

        Call only after inspecting the subscriber gap. This is not an HTTP route
        and never executes native_send or renews a send command's authorization.
        """
        adapter=next(iter(self.adapters.values()))
        sequences=adapter.queue_replay(first_seq,last_seq,routes=tuple(self.adapters))
        errors=[]
        for route,channel in self.adapters.items():
            try:channel.flush_events(limit=200)
            except Exception:errors.append(route+':EVENT_REPLAY_PENDING')
        return {'source_sequences':sequences,'state':'queued','errors':errors}

    def publish_native(self, event: dict, *, historical=False):
        """Publish one real native record. Never infer command ownership from text."""
        if not isinstance(event,dict) or type(historical) is not bool:
            raise ProtocolError('原生事件格式无效')
        data=dict(event)
        # Native facts cannot supply control authorization; only the durable
        # receiver ledger assigns these authenticated top-level metadata fields.
        data.pop('manual_fence_id',None);data.pop('manual_fence_version',None)
        occurred=data.get('occurred_at')
        if type(occurred) not in (int,float) or not math.isfinite(occurred):
            raise ProtocolError('原生事件必须提供真实秒级occurred_at')
        direction=data.get('direction',data.get('dir'))
        direction={'incoming':'in','inbound':'in','outgoing':'out','outbound':'out'}.get(direction,direction)
        if direction not in ('in','out'):raise ProtocolError('原生事件方向无效')
        peer=token(data.get('peer_id'))
        native_id=data.get('ext_msg_id') or data.get('source_message_id')
        if not isinstance(native_id,str) or not native_id:
            raise ProtocolError('原生事件缺少稳定消息ID')
        if not data.get('event_id'):
            data['event_id']='native-'+digest([self.binding.account_uid,peer,direction,native_id])
        data['direction']=direction;data['historical']=historical or data.get('historical') is True
        if direction=='in' and not data['historical']:
            self.ledger.begin_inbound(data['event_id'],peer)
        data.pop('source_command_id',None)
        if direction=='out':
            command_id=self.ledger.command_for_native(native_id,peer)
            if command_id:data['source_command_id']=command_id
            state=self.ledger.stage_out(data,self.echo_wait_seconds)
            if state=='waiting':return None
            return self.flush_deferred().get(data['event_id'])
        return self._publish_event(data)

    def flush_deferred(self):
        """Resolve exact evidence or fail closed; persist publication before release."""
        published={}
        for data in self.ledger.deferred_ready():
            published[data['event_id']]=self._publish_event(data)
            self.ledger.deferred_published(data['event_id'])
        return published

    def _publish_event(self,data):
        errors=[];sequence=None
        # Persist all route intents first. A failed OSS call cannot prevent SMTP
        # publication or erase its independent retry state (and vice versa).
        for route,adapter in self.adapters.items():
            try:
                sequence=adapter.queue_event(data,routes=tuple(self.adapters),control=self.ledger.event_control(data['event_id']))
                break
            except Exception:errors.append(route+':EVENT_JOURNAL_UNAVAILABLE')
        if sequence is None:
            self.last_publication_errors=errors
            raise TransportUnavailable('EVENT_JOURNAL_UNAVAILABLE')
        if data.get('direction')=='in' and not data.get('historical'):
            self.ledger.finish_inbound_journal(data['event_id'],data['peer_id'],sequence)
        for route,adapter in self.adapters.items():
            try:adapter.flush_events(limit=200)
            except Exception:errors.append(route+':EVENT_PUBLICATION_PENDING')
        self.last_publication_errors=errors
        return sequence
