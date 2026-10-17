"""Bindings, previews and read projections for the V2 production core only."""
from __future__ import annotations
import json
from .store import Store, encoded, identity, new_id
from .prices import InvalidFact
class ApplicationStore(Store):
    def binding_for(self, owner):
        with self.db.transaction(write=False) as tx:
            row=tx.execute('SELECT b.*,o.send_allowed FROM xj2_binding b JOIN xj2_owner o ON o.owner_id=b.owner_id WHERE b.owner_id=?',(owner,)).fetchone()
        if not row:return None
        return {'owner_user_id':row['owner_id'],'account_uid':row['account_uid'],
                'workbench_id':row['workbench_id'],'transport':row['transport'],
                'version':row['version'],'bot_enabled':row['send_allowed']}
    def bind_account(self,owner_user_id,account_uid,workbench_id,transport='oss'):
        for value,label,size in ((owner_user_id,'owner',160),(account_uid,'account',80),(workbench_id,'workbench',160)):
            identity(value,label,size)
        if transport not in ('oss','mail'):raise InvalidFact('Unsupported route')
        with self.write(owner_user_id) as tx:
            occupied=tx.execute('SELECT owner_id FROM xj2_binding WHERE account_uid=?',(account_uid,)).fetchone()
            if occupied and occupied['owner_id']!=owner_user_id:raise InvalidFact('Account already belongs to another trader')
            old=tx.execute('SELECT * FROM xj2_binding WHERE owner_id=?',(owner_user_id,)).fetchone()
            if old:
                if (old['account_uid'],old['workbench_id'],old['transport'])!=(account_uid,workbench_id,transport):
                    raise InvalidFact('Existing identity/route cannot be replaced by binding; stop and reconcile first')
            else:
                tx.execute('INSERT INTO xj2_binding(owner_id,account_uid,workbench_id,transport,version,config_json) VALUES(?,?,?,?,1,?)',
                           (owner_user_id,account_uid,workbench_id,transport,'{}'))
                tx.execute('UPDATE xj2_owner SET send_allowed=0 WHERE owner_id=?',(owner_user_id,))
        return self.binding_for(owner_user_id)
    def preferences(self,owner):
        with self.db.transaction(write=False) as tx:
            row=tx.execute('SELECT config_json FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
        if row is None:raise PermissionError('Account binding is not configured')
        return json.loads(row['config_json'])
    def save_preferences(self,owner,change):
        if not isinstance(change,dict):raise InvalidFact('Preferences must be an object')
        with self.write(owner) as tx:
            binding=self.require_binding(tx,owner)
            current=json.loads(binding['config_json']);current.update(change)
            tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?',(encoded(current),owner))
        return current
    def require_binding(self,tx,owner):
        row=tx.execute('SELECT * FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
        if row is None:raise PermissionError('Account binding is not configured')
        return dict(row)
    def audit(self,tx,owner,kind,entity,data):
        tx.execute('INSERT INTO xj2_audit(audit_id,owner_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)',
                   (new_id(),owner,kind,entity,self.clock(),encoded(data)))
    def group_rows(self,owner,*,tx=None):
        if tx is None:
            with self.db.transaction(write=False) as session:return self.group_rows(owner,tx=session)
        rows=tx.execute('SELECT * FROM xj2_group WHERE owner_id=? ORDER BY created_at,group_id',(owner,)).fetchall()
        result=[]
        for row in rows:
            g=dict(row)
            for field in ('policy','boundary','valuation','plan','handoff'):
                g[field]=json.loads(g.pop(field+'_json'))
            g['summary']=self._summary(tx,dict(row))
            peers=tx.execute('SELECT p.*,t.peer_uid,t.account_uid,t.ingest_revision,t.analyzed_revision FROM xj2_peer p JOIN xj2_thread t ON t.thread_id=p.thread_id WHERE p.group_id=? ORDER BY p.member_id',(g['group_id'],)).fetchall()
            g['peers']=[]
            for peer in peers:
                item=dict(peer)
                item['our_quote']=json.loads(item.pop('our_quote_json'))
                item['quotes']={side:self._quote(tx,item['member_id'],side).dump() for side in ('bid','ofr')}
                item['analysis_pending']=item['ingest_revision']>item['analyzed_revision']
                pending=tx.execute('SELECT state FROM xj2_command WHERE command_id=?',(item['pending_command_id'],)).fetchone() if item['pending_command_id'] else None
                item['command_state']=pending['state'] if pending else None
                g['peers'].append(item)
            result.append(g)
        return result
    def snapshot(self,owner):
        with self.db.transaction(write=False) as tx:
            # Stable owner snapshot; no event mutation or read-triggered negotiation.
            lock=' FOR UPDATE' if tx.mysql else ''
            owner_row=tx.execute('SELECT * FROM xj2_owner WHERE owner_id=?'+lock,(owner,)).fetchone()
            if not owner_row:return {'engine':'v22','revision':0,'groups':[],'threads':[],'send_allowed':False}
            groups=self.group_rows(owner,tx=tx)
            threads=[dict(row) for row in tx.execute('SELECT * FROM xj2_thread WHERE owner_id=? ORDER BY thread_id',(owner,)).fetchall()]
            for t in threads:
                t['analysis_pending']=t['ingest_revision']>t['analyzed_revision']
                latest=tx.execute('SELECT state,result_json FROM xj2_analysis WHERE thread_id=? ORDER BY through_revision DESC LIMIT 1',(t['thread_id'],)).fetchone()
                t['analysis_state']=latest['state'] if latest else 'IDLE'
                t['analysis_result']=json.loads(latest['result_json']) if latest else None
            return {'engine':'v22','revision':owner_row['projection_version'],'groups':groups,
                    'threads':threads,'send_allowed':bool(owner_row['send_allowed'])}
    def raw_page(self,owner,thread_id,*,after_revision=0,limit=200):
        with self.db.transaction(write=False) as tx:
            t=self._thread(tx,owner,thread_id)
            rows=tx.execute('SELECT * FROM xj2_message WHERE thread_id=? AND ingest_revision>? ORDER BY ingest_revision LIMIT ?',
                            (thread_id,after_revision,limit)).fetchall()
            messages=[]
            for row in rows:
                item=dict(row)
                item['contexts']=[dict(c) for c in tx.execute("SELECT s.member_id,s.relation,g.group_id,g.bond_code,g.plan_json FROM xj2_scope s JOIN xj2_peer p ON p.member_id=s.member_id JOIN xj2_group g ON g.group_id=p.group_id WHERE s.message_id=? AND s.relation<>'REMOVED'",(row['message_id'],)).fetchall()]
                for c in item['contexts']:c['name']=json.loads(c.pop('plan_json')).get('name',c['bond_code'])
                item['analysis_state']='ANALYZED' if row['ingest_revision']<=t['analyzed_revision'] else 'QUEUED'
                if any(c['relation'] in ('INVALID','UNRESOLVED') for c in item['contexts']):item['analysis_state']='NEEDS_REVIEW'
                item['received']=True
                messages.append(item)
            version=tx.execute('SELECT projection_version FROM xj2_owner WHERE owner_id=?',(owner,)).fetchone()['projection_version']
        return {'revision':version,'thread':t,'messages':messages,'next_revision':rows[-1]['ingest_revision'] if rows else after_revision}
    def market_overlays(self,owner):
        groups=self.group_rows(owner)
        labels={g['bond_code']:('待人工确认' if g['state']=='HANDOFF' else '已暂停' if not g['send_allowed'] else '询价中') for g in groups if not g['archived']}
        try:prefs=self.preferences(owner)
        except PermissionError:prefs={}
        return labels,prefs.get('floors',{})
    def switch_transport(self,owner,route,expected_version):
        if route not in ('oss','mail'):raise InvalidFact('Unsupported transport')
        with self.write(owner) as tx:
            b=self.require_binding(tx,owner)
            if b['version']!=expected_version:raise InvalidFact('Binding version changed')
            if route==b['transport']:return
            control=tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?',(owner,)).fetchone()
            pending=tx.execute("SELECT c.command_id FROM xj2_command c JOIN xj2_group g ON g.group_id=c.group_id WHERE g.owner_id=? AND c.state IN ('QUEUED','PENDING','UNKNOWN') LIMIT 1",(owner,)).fetchone()
            if control['send_allowed'] or pending:raise InvalidFact('Stop sending and reconcile pending commands before route switch')
            tx.execute('UPDATE xj2_binding SET transport=?,version=version+1 WHERE owner_id=?',(route,owner))
            tx.execute('UPDATE xj2_owner SET control_version=control_version+1 WHERE owner_id=?',(owner,))
            self.audit(tx,owner,'transport_switched',b['account_uid'],{'from':b['transport'],'to':route})
