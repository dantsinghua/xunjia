"""Account-wide reset through the existing authenticated file transport.

Reset retires an authorization round, not native history or unknown outcomes.
All source baselines are separate from the application's local message revisions.
"""
from __future__ import annotations
import json
from .negotiation.prices import InvalidFact
from .negotiation.store import encoded,identity,new_id
from relay.protocol import session_reset_hash,validate_session_reset


class SessionReset:
    def __init__(self,service,transport):
        self.service,self.store,self.transport=service,service.store,transport
        self.clock=self.store.clock
        self._next_attempt={}
        self._scan_after=(0,'')

    @staticmethod
    def public(row):
        if row is None:return None
        value=dict(row)
        return {key:value[key] for key in ('reset_id','binding_version','account_epoch','state','code',
                'snapshot_hash','created_at','updated_at')} | {
            'scope':{'workbench_id':value['workbench_id'],'account_uid':value['account_uid']},
            'snapshot':json.loads(value['snapshot_json'])}

    def _adapter(self,binding):
        key=(binding['owner_id'],binding['account_uid'],binding['transport'])
        adapter=getattr(self.transport,'adapters',{}).get(key)
        if adapter is None or not callable(getattr(adapter,'queue_session_reset',None)):
            raise InvalidFact('SESSION_RESET_TRANSPORT_NOT_CONFIGURED')
        if adapter.binding.workbench_id!=binding['workbench_id']:
            raise InvalidFact('SESSION_RESET_TRANSPORT_BINDING_CHANGED')
        return adapter

    def get(self,owner,reset_id=None):
        with self.store.db.transaction(write=False) as tx:
            binding=self.store.require_binding(tx,owner)
            if reset_id is None:
                row=self.store._account_reset(tx,owner,binding['account_uid'])
            else:
                identity(reset_id,'reset id',64)
                row=tx.execute('SELECT * FROM xj2_session_reset WHERE reset_id=? AND owner_id=? AND workbench_id=? AND account_uid=?',
                    (reset_id,owner,binding['workbench_id'],binding['account_uid'])).fetchone()
                if row is None:raise PermissionError('Session reset does not belong to the selected account')
        return self.public(row)

    @staticmethod
    def _find_request(tx,owner,request_id):
        row=tx.execute('SELECT * FROM xj2_session_reset WHERE owner_id=? AND request_id=?',(owner,request_id)).fetchone()
        if row:return row
        for candidate in tx.execute('SELECT * FROM xj2_session_reset WHERE owner_id=? ORDER BY created_at DESC',(owner,)).fetchall():
            if request_id in json.loads(candidate['local_cut_json']).get('request_ids',[]):return candidate
        return None

    def _retry(self,tx,row,request_id):
        cut=json.loads(row['local_cut_json'])
        aliases=list(cut.get('request_ids',[]))
        if request_id!=row['request_id'] and request_id not in aliases:aliases.append(request_id)
        cut['request_ids']=aliases
        state='requested' if row['state']=='blocked' else row['state']
        code='' if row['state']=='blocked' else row['code']
        tx.execute('UPDATE xj2_session_reset SET state=?,code=?,local_cut_json=?,updated_at=? WHERE reset_id=?',
            (state,code,encoded(cut),self.clock(),row['reset_id']))
        self._next_attempt.pop(row['reset_id'],None)
        return self.public(tx.execute('SELECT * FROM xj2_session_reset WHERE reset_id=?',(row['reset_id'],)).fetchone())

    def request(self,owner,body):
        if not isinstance(body,dict) or set(body)!={'request_id','expected_binding_version','confirmed'}:
            raise InvalidFact('Specify request_id, expected_binding_version and confirmed')
        request_id=identity(body['request_id'],'reset request')
        if body['confirmed'] is not True or type(body['expected_binding_version']) is not int:
            raise InvalidFact('Explicit account-wide session reset confirmation is required')
        # Signed collector capability is a prerequisite for a NEW operation.
        # Never hold the owner write lock across a catalog/network read, and do
        # not make retries or idempotent results depend on a fresh catalog.
        with self.store.db.transaction(write=False) as tx:
            before=self.store.require_binding(tx,owner)
            if before['version']!=body['expected_binding_version']:
                raise InvalidFact('Account binding changed; review the reset scope')
            previous=self._find_request(tx,owner,request_id)
            latest=self.store._account_reset(tx,owner,before['account_uid'])
            needs_capability=previous is None and (latest is None or latest['state']=='complete')
        if needs_capability:
            adapter=self._adapter(before)
            try:catalog=adapter.catalog()
            except Exception:raise InvalidFact('SESSION_RESET_CAPABILITY_UNAVAILABLE') from None
            if (not isinstance(catalog,dict) or catalog.get('account_uid')!=before['account_uid']
                    or not isinstance(catalog.get('capabilities'),list)
                    or 'negotiation-v22-session-reset' not in catalog['capabilities']):
                raise InvalidFact('SESSION_RESET_COLLECTOR_UNSUPPORTED')
        with self.store.write(owner) as tx:
            binding=self.store.require_binding(tx,owner)
            if binding['version']!=body['expected_binding_version'] or any(binding[key]!=before[key] for key in ('workbench_id','account_uid','transport')):
                raise InvalidFact('Account binding changed; review the reset scope')
            old=self._find_request(tx,owner,request_id)
            if old:
                if any(old[k]!=binding[k] for k in ('workbench_id','account_uid')) or old['binding_version']!=binding['version']:
                    raise InvalidFact('Reset request id belongs to another binding')
                return self._retry(tx,old,request_id) if old['state']!='complete' else self.public(old)
            latest=self.store._account_reset(tx,owner,binding['account_uid'])
            if latest and latest['state']!='complete':return self._retry(tx,latest,request_id)
            self._adapter(binding)
            epoch=latest['account_epoch'] if latest else 0
            payload={'reset_id':new_id(),'owner_user_id':owner,'workbench_id':binding['workbench_id'],
                'account_uid':binding['account_uid'],'binding_version':binding['version'],
                'expected_account_epoch':epoch,'account_epoch':epoch+1,'transport':binding['transport']}
            context=json.loads(binding['config_json']).get('context_id')
            if context:payload['context_id']=context
            payload['request_hash']=session_reset_hash(payload)
            groups=self._groups(tx,owner,binding['account_uid'],binding['workbench_id'])
            for group in groups:
                tx.execute('UPDATE xj2_group SET send_allowed=0,control_version=control_version+1 WHERE group_id=?',(group['group_id'],))
                for peer in tx.execute('SELECT member_id FROM xj2_peer WHERE group_id=?',(group['group_id'],)).fetchall():
                    tx.execute('UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE member_id=?',(peer['member_id'],))
                    tx.execute("UPDATE xj2_reply_job SET state='CANCELLED',lease_until=0 WHERE member_id=? AND state IN ('RUNNING','RETRY')",(peer['member_id'],))
                self.store._cancel_commands(tx,group_id=group['group_id'],reason='ACCOUNT_SESSION_RESET')
            threads=tx.execute('SELECT thread_id FROM xj2_thread WHERE owner_id=? AND account_uid=? AND workbench_id=?',(owner,binding['account_uid'],binding['workbench_id'])).fetchall()
            for thread in threads:
                tx.execute("UPDATE xj2_analysis SET state='CANCELLED',lease_until=0 WHERE thread_id=? AND state IN ('RUNNING','RETRY')",(thread['thread_id'],))
            now=self.clock()
            tx.execute("INSERT INTO xj2_session_reset(reset_id,owner_id,workbench_id,account_uid,binding_version,request_id,request_hash,account_epoch,state,code,request_json,snapshot_hash,snapshot_json,local_cut_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'requested','',?,'','null','{}',?,?)",
                (payload['reset_id'],owner,binding['workbench_id'],binding['account_uid'],binding['version'],request_id,payload['request_hash'],epoch+1,encoded(payload),now,now))
            self.store.audit(tx,owner,'session_reset_requested',payload['reset_id'],{'account_uid':binding['account_uid'],'workbench_id':binding['workbench_id'],'account_epoch':epoch+1})
            row=tx.execute('SELECT * FROM xj2_session_reset WHERE reset_id=?',(payload['reset_id'],)).fetchone()
            return self.public(row)

    @staticmethod
    def _groups(tx,owner,account,workbench):
        groups=tx.execute('SELECT DISTINCT g.group_id FROM xj2_group g JOIN xj2_peer p ON p.group_id=g.group_id JOIN xj2_thread t ON t.thread_id=p.thread_id WHERE g.owner_id=? AND t.owner_id=? AND t.account_uid=? AND t.workbench_id=? AND g.archived=0',(owner,owner,account,workbench)).fetchall()
        for group in groups:
            foreign=tx.execute('SELECT t.thread_id FROM xj2_peer p JOIN xj2_thread t ON t.thread_id=p.thread_id WHERE p.group_id=? AND (t.owner_id<>? OR t.account_uid<>? OR t.workbench_id IS NULL OR t.workbench_id<>?) LIMIT 1',(group['group_id'],owner,account,workbench)).fetchone()
            if foreign:raise InvalidFact('Cannot reset a group containing another collector/account')
        return groups

    def accept_ack(self,binding,payload):
        validate_session_reset(payload,binding,ack=True)
        owner=binding.owner_user_id
        with self.store.write(owner) as tx:
            current=self.store.require_binding(tx,owner)
            row=tx.execute('SELECT * FROM xj2_session_reset WHERE reset_id=? AND owner_id=?',(payload['reset_id'],owner)).fetchone()
            if row is None:raise InvalidFact('Unknown account reset acknowledgement')
            if any(current[k]!=getattr(binding,k) or current[k]!=row[k] for k in ('workbench_id','account_uid')) or current['version']!=row['binding_version']:
                raise InvalidFact('Reset acknowledgement belongs to a changed binding')
            request=json.loads(row['request_json'])
            if any(payload.get(key)!=value for key,value in request.items()):
                raise InvalidFact('Reset acknowledgement does not match the original request')
            latest=self.store._account_reset(tx,owner,current['account_uid'])
            if latest['reset_id']!=row['reset_id']:return self.public(row) # Old exact ACK cannot replace a newer reset.
            phase=payload['phase'];snapshot=payload.get('snapshot')
            if row['state']=='complete':
                if phase=='complete' and payload['snapshot_hash']!=row['snapshot_hash']:
                    raise InvalidFact('Completed reset snapshot changed')
                return self.public(row)
            if phase in ('waiting_native','blocked'):
                if row['state']=='local_applied':
                    if phase=='blocked':
                        tx.execute('UPDATE xj2_session_reset SET code=?,updated_at=? WHERE reset_id=?',(payload['code'],self.clock(),row['reset_id']))
                        return self.public(tx.execute('SELECT * FROM xj2_session_reset WHERE reset_id=?',(row['reset_id'],)).fetchone())
                    return self.public(row)
                tx.execute('UPDATE xj2_session_reset SET state=?,code=?,snapshot_hash=?,snapshot_json=?,updated_at=? WHERE reset_id=?',
                    (phase,payload.get('code',''),payload['snapshot_hash'],encoded(snapshot),self.clock(),row['reset_id']))
            elif phase=='barrier_ready':
                if row['state']=='local_applied':
                    if payload['snapshot_hash']!=row['snapshot_hash']:raise InvalidFact('Reset snapshot changed after application')
                    return self.public(row)
                if snapshot['old_native_tasks_running'] or snapshot['pending_inbound_count'] or snapshot['pending_echo_count']:
                    raise InvalidFact('Native reset is not quiescent')
                cuts={};source_cut=snapshot['published_source_highwater']
                for group in self._groups(tx,owner,current['account_uid'],current['workbench_id']):
                    tx.execute('UPDATE xj2_group SET archived=1,send_allowed=0,version=version+1,control_version=control_version+1 WHERE group_id=?',(group['group_id'],))
                threads=tx.execute('SELECT * FROM xj2_thread WHERE owner_id=? AND account_uid=? AND workbench_id=?',(owner,current['account_uid'],current['workbench_id'])).fetchall()
                for thread in threads:
                    peer=snapshot['peers'].get(thread['peer_uid'],{})
                    watermark=peer.get('source_watermark',0)
                    # Snapshot omission cannot assert receipt of messages never observed there.
                    if watermark>source_cut:raise InvalidFact('Peer source watermark exceeds the publication snapshot')
                    cuts[thread['thread_id']]=0
                    for message in tx.execute('SELECT ingest_revision,source_seq FROM xj2_message WHERE thread_id=? ORDER BY ingest_revision',(thread['thread_id'],)).fetchall():
                        if message['source_seq']>source_cut:break
                        cuts[thread['thread_id']]=message['ingest_revision']
                    fence_id=thread['receiver_fence_id'];fence_version=thread['receiver_fence_version']
                    if peer.get('receiver_fence_version',0)>fence_version:
                        fence_id=peer['receiver_fence_id'];fence_version=peer['receiver_fence_version']
                    elif peer.get('receiver_fence_version',0)==fence_version and fence_version and peer.get('receiver_fence_id')!=fence_id:
                        raise InvalidFact('Native reset fence conflicts with the same observed version')
                    pending=tx.execute('SELECT MIN(received_at) AS first_pending FROM xj2_message WHERE thread_id=? AND ingest_revision>? AND historical=0 AND (source_seq<=0 OR source_seq>?)',
                        (thread['thread_id'],max(cuts[thread['thread_id']],thread['analyzed_revision']),source_cut)).fetchone()['first_pending']
                    tx.execute('UPDATE xj2_thread SET source_watermark=CASE WHEN source_watermark<? THEN ? ELSE source_watermark END,dispatch_version=CASE WHEN dispatch_version<? THEN ? ELSE dispatch_version END,receiver_fence_id=?,receiver_fence_version=?,pending_since=? WHERE thread_id=?',
                        (watermark,watermark,max(0,peer.get('min_control_version',1)-1),max(0,peer.get('min_control_version',1)-1),fence_id,fence_version,pending,thread['thread_id']))
                local_cut={**json.loads(row['local_cut_json']),'source_highwater':source_cut,'local_revisions':cuts}
                tx.execute("UPDATE xj2_session_reset SET state='local_applied',code='',snapshot_hash=?,snapshot_json=?,local_cut_json=?,updated_at=? WHERE reset_id=?",
                    (payload['snapshot_hash'],encoded(snapshot),encoded(local_cut),self.clock(),row['reset_id']))
                self.store.audit(tx,owner,'session_reset_applied',row['reset_id'],{'snapshot_hash':payload['snapshot_hash'],'account_epoch':row['account_epoch'],'local_cut':local_cut})
            elif phase=='complete':
                if row['state']!='local_applied' or payload['snapshot_hash']!=row['snapshot_hash']:
                    raise InvalidFact('Reset completion has no matching applied snapshot')
                tx.execute("UPDATE xj2_session_reset SET state='complete',code='',updated_at=? WHERE reset_id=?",(self.clock(),row['reset_id']))
                self._next_attempt.pop(row['reset_id'],None)
                self.store.audit(tx,owner,'session_reset_complete',row['reset_id'],{'account_epoch':row['account_epoch'],'snapshot_hash':row['snapshot_hash']})
            return self.public(tx.execute('SELECT * FROM xj2_session_reset WHERE reset_id=?',(row['reset_id'],)).fetchone())

    def run_once(self):
        with self.store.db.transaction(write=False) as tx:
            stamp,identity_after=self._scan_after
            rows=[dict(r) for r in tx.execute("SELECT * FROM xj2_session_reset WHERE state<>'complete' AND (created_at>? OR (created_at=? AND reset_id>?)) ORDER BY created_at,reset_id LIMIT 20",(stamp,stamp,identity_after)).fetchall()]
            if len(rows)<20 and identity_after:
                rows.extend(dict(r) for r in tx.execute("SELECT * FROM xj2_session_reset WHERE state<>'complete' AND (created_at<? OR (created_at=? AND reset_id<=?)) ORDER BY created_at,reset_id LIMIT ?",(stamp,stamp,identity_after,20-len(rows))).fetchall())
            if rows:self._scan_after=(rows[-1]['created_at'],rows[-1]['reset_id'])
        processed=0
        for row in rows:
            if self._next_attempt.get(row['reset_id'],0)>self.clock():continue
            self._next_attempt[row['reset_id']]=self.clock()+5
            try:
                with self.store.db.transaction(write=False) as tx:
                    binding=self.store.require_binding(tx,row['owner_id'])
                    if any(binding[k]!=row[k] for k in ('workbench_id','account_uid')) or binding['version']!=row['binding_version']:
                        raise InvalidFact('SESSION_RESET_BINDING_CHANGED')
                payload=json.loads(row['request_json'])
                payload['phase']='applied' if row['state']=='local_applied' else 'request'
                if payload['phase']=='applied':payload['snapshot_hash']=row['snapshot_hash']
                adapter=self._adapter(binding)
                adapter.queue_session_reset(payload)
                adapter.flush_publications()
                self.service.diagnostics.record('session_reset',owner=row['owner_id'],entity=row['reset_id'])
                processed+=1
            except Exception as exc:
                with self.store.write(row['owner_id']) as tx:
                    tx.execute("UPDATE xj2_session_reset SET code=?,updated_at=? WHERE reset_id=? AND state<>'complete'",
                        ('SESSION_RESET_BINDING_CHANGED' if str(exc)=='SESSION_RESET_BINDING_CHANGED' else 'SESSION_RESET_DELIVERY_RETRY',self.clock(),row['reset_id']))
                self.service.diagnostics.record('session_reset',owner=row['owner_id'],entity=row['reset_id'],code='SESSION_RESET_DELIVERY_RETRY',error=exc)
        return processed
