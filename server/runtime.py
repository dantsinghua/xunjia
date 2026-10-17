"""Durable dispatcher: acceptance is pending, uncertain delivery is reconciled.

A command is claimed in a short transaction before network I/O. The transport
must persist command IDs and cancellation tombstones at the execution endpoint;
local state alone cannot promise exactly-once execution across response loss.
"""
from __future__ import annotations
import json
import time

from .business import encoded


class Dispatcher:
    def __init__(self,service,transport,*,clock=time.time):
        self.service=service;self.transport=transport;self.clock=clock

    @staticmethod
    def envelope(row):
        return {**json.loads(row['body_json']),**{key:row[key] for key in ('command_id','owner_user_id','account_uid','peer_id','conversation_id','kind','content_hash','control_version','expires_at','status')}}

    def _receipt(self,command,result):
        if not isinstance(result,dict):return False
        if any(result.get(key)!=command[key] for key in ('command_id','account_uid','peer_id','content_hash')):return False
        status=result.get('status')
        if status in ('accepted','pending'):return True
        if status not in ('sent','failed','cancelled','unknown') or not isinstance(result.get('evidence'),str) or not result['evidence']:return False
        self.service.receipt(command['command_id'],account_uid=command['account_uid'],peer_id=command['peer_id'],status=status,evidence=result['evidence'],native_id=result.get('native_id'))
        return True

    def _unknown(self,command,reason,error=None):
        with self.service.repo.db.transaction() as tx:
            self.service._binding(tx,command['owner_user_id'])
            tx.execute("UPDATE xj_command SET status='unknown',result_json=? WHERE command_id=? AND status='pending'",(encoded({'reason':reason}),command['command_id']))
            self.service._audit(tx,command['owner_user_id'],'delivery_uncertain',command['command_id'],{'reason':reason,'error_type':type(error).__name__ if error else None})
        self.service.diagnostics.record('dispatch',owner=command['owner_user_id'],code='DELIVERY_UNCERTAIN',error=error,entity=command['command_id'])

    def run_once(self):
        if self.transport is None:return 0
        with self.service.repo.db.transaction(write=False) as tx:
            candidates=tx.execute("SELECT command_id,owner_user_id FROM xj_command WHERE status IN ('pending','unknown') AND next_check_at<=? ORDER BY next_check_at,created_at,command_id LIMIT 5",(self.clock(),)).fetchall()
        unresolved=[]
        for candidate in candidates:
            with self.service.repo.db.transaction() as tx:
                self.service._binding(tx,candidate['owner_user_id'])
                row=tx.execute("SELECT * FROM xj_command WHERE command_id=? AND status IN ('pending','unknown') AND next_check_at<=?",(candidate['command_id'],self.clock())).fetchone()
                if not row:continue
                tx.execute('UPDATE xj_command SET next_check_at=? WHERE command_id=?',(self.clock()+5,row['command_id']))
                unresolved.append(dict(row))
        if unresolved:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=5,thread_name_prefix='xunjia-reconcile') as pool:
                list(pool.map(self._reconcile,unresolved))
        with self.service.repo.db.transaction() as tx:
            # Lock the account first everywhere, then re-read the candidate. This
            # serializes state transitions without holding a database lock over I/O.
            candidate=tx.execute("SELECT command_id,owner_user_id FROM xj_command WHERE status='queued' ORDER BY created_at,command_id LIMIT 1").fetchone()
            if not candidate:return len(unresolved)
            binding=self.service._binding(tx,candidate['owner_user_id'])
            row=tx.execute('SELECT * FROM xj_command WHERE command_id=?',(candidate['command_id'],)).fetchone()
            if row['status']!='queued':return len(unresolved)
            body=json.loads(row['body_json'])
            conv=tx.execute('SELECT mode,control_version FROM xj_conversation WHERE conversation_id=?',(row['conversation_id'],)).fetchone()
            required_mode='HUMAN' if row['kind']=='manual_send' else 'AUTO'
            valid=((binding['bot_enabled'] or row['kind']=='manual_send') and conv and conv['mode']==required_mode and conv['control_version']==row['control_version']
                   and binding['version']==body.get('binding_version') and binding['account_uid']==row['account_uid']
                   and binding['transport']==body.get('transport') and row['expires_at']>self.clock())
            for sid,revision in body.get('session_revisions',{}).items():
                session=tx.execute('SELECT revision,pending_command_id FROM xj_session WHERE session_id=? AND owner_user_id=?',(sid,row['owner_user_id'])).fetchone()
                valid=valid and session and session['revision']==revision and session['pending_command_id']==row['command_id']
            if not valid:
                tx.execute("UPDATE xj_command SET status='cancelled',result_json=? WHERE command_id=? AND status='queued'",(encoded({'reason':'发送授权过期或已变更'}),row['command_id']))
                return len(unresolved)+1
            tx.execute("UPDATE xj_command SET status='pending' WHERE command_id=? AND status='queued'",(row['command_id'],))
            self.service._audit(tx,row['owner_user_id'],'dispatch_claim',row['command_id'],{'content_hash':row['content_hash']})
            command=self.envelope(row);command['status']='pending'
        # A concurrent stop may have installed a cancellation barrier after the
        # claim. Recheck locally and carry the original version to the receiver.
        with self.service.repo.db.transaction(write=False) as tx:
            current=tx.execute('SELECT mode,control_version FROM xj_conversation WHERE conversation_id=?',(command['conversation_id'],)).fetchone()
        try:
            if not current or current['mode']!=('HUMAN' if command['kind']=='manual_send' else 'AUTO') or current['control_version']!=command['control_version']:
                result=self.transport.cancel(command)
            else:result=self.transport.send(command)
            if not self._receipt(command,result):self._unknown(command,'发送返回缺少匹配的执行证据')
        except Exception as exc:
            self._unknown(command,'提交后结果未知，等待对账',exc)
        return len(unresolved)+1

    def _reconcile(self,row):
        command=self.envelope(row)
        with self.service.repo.db.transaction(write=False) as tx:
            binding=tx.execute('SELECT bot_enabled,version FROM xj_binding WHERE owner_user_id=?',(row['owner_user_id'],)).fetchone()
            conv=tx.execute('SELECT mode,control_version FROM xj_conversation WHERE conversation_id=?',(row['conversation_id'],)).fetchone()
        required_mode='HUMAN' if row['kind']=='manual_send' else 'AUTO'
        stale=not binding or not conv or (row['kind']!='manual_send' and not binding['bot_enabled']) or conv['control_version']!=row['control_version'] or conv['mode']!=required_mode
        try:
            result=self.transport.cancel(command) if stale else self.transport.reconcile(command)
            if result is not None and not self._receipt(command,result):self._unknown(command,'回执身份或证据未通过校验')
        except Exception as exc:self._unknown(command,'对账或取消结果未知',exc)
        if stale:self._settle_stop(row['owner_user_id'])

    def _settle_stop(self,owner):
        with self.service.repo.db.transaction() as tx:
            binding=self.service._binding(tx,owner)
            pending=tx.execute("SELECT command_id FROM xj_command WHERE owner_user_id=? AND status IN ('pending','unknown') LIMIT 1",(owner,)).fetchone()
            if not pending and not binding['bot_enabled']:
                tx.execute("UPDATE xj_conversation SET mode='STOPPED' WHERE owner_user_id=? AND mode='STOPPING'",(owner,))
