"""Production RFQ and human operations backed exclusively by the V2 fact core."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
from ..diagnostics import Diagnostics
from .application_store import ApplicationStore
from .commands import Commands
from .prices import InvalidFact, LimitPolicy, Price, Valuation, iso_day, number
from .store import encoded, identity, new_id
def digest(value):return hashlib.sha256(encoded(value).encode()).hexdigest()
class Inquiry:
    engine='v22'
    def __init__(self,store:ApplicationStore,*,reference_provider=None,calendar=None):
        self.repo=self.store=store
        self.clock=store.clock
        self.commands_core=Commands(store,current_day=self.trade_day)
        self.reference_provider=reference_provider
        self.calendar=calendar
        self.diagnostics=Diagnostics(clock=self.clock)
    def trade_day(self):
        # Counterparty quotes require confirmation on EVERY new local day.
        # The publication calendar is separate and is used only for valuation.
        return datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).date().isoformat()
    def valuation(self,code):
        if self.reference_provider is None:raise InvalidFact('CHINABOND_SOURCE_NOT_CONFIGURED')
        if self.calendar is None or not callable(getattr(self.calendar,'latest_valuation_day',None)):
            raise InvalidFact('CHINABOND_PUBLICATION_CALENDAR_NOT_CONFIGURED')
        snapshot=self.reference_provider.snapshot()
        candidates=[b for b in snapshot.bonds if b.get('code',b.get('bond_id'))==code]
        if len(candidates)!=1:raise InvalidFact('No unique ChinaBond valuation for this bond')
        row=candidates[0]
        day=row.get('valuation_date',snapshot.as_of)
        expected=self.calendar.latest_valuation_day(self.clock())
        valuation=Valuation(code,iso_day(day),str(row.get('valuation_yield_pct',row.get('val_yield',''))),snapshot.snapshot_id)
        if day!=expected:raise InvalidFact('Valuation snapshot is not the latest required trading date')
        return valuation,expected
    def normalize_item(self,owner,item,*,preferences=None):
        if not isinstance(item,dict):raise InvalidFact('RFQ item must be an object')
        code=identity(item.get('code'),'bond code',32)
        name=identity(item.get('name',code),'bond name',160)
        side=item.get('our_side')
        peers=item.get('peer_ids')
        if side not in ('BUY','SELL'):raise InvalidFact('Explicit BUY/SELL is required')
        if not isinstance(peers,list) or not peers or len(peers)>100:raise InvalidFact('Select 1..100 counterparties')
        peers=list(dict.fromkeys(identity(p,'counterparty') for p in peers))
        volume=number(item.get('volume_wan'),'volume');step=number(item.get('step_bp'),'step')
        if volume<=0 or step<=0:raise InvalidFact('Target amount and step must be positive')
        opening=Price('YIELD',str(item.get('opening_yield_pct','')))
        policy=LimitPolicy('ABSOLUTE',str(item.get('limit_yield_pct','')))
        valuation=None;valuation_date=None
        if item.get('pricing_basis','absolute')=='bp':
            prefs=preferences if preferences is not None else self.repo.preferences(owner)
            floor=prefs.get('floors',{}).get(code,{})
            if floor.get('kind')!='bp':raise InvalidFact('BP pricing requires a saved per-bond BP offset')
            offset=number(str(floor['value']))*(1 if side=='BUY' else -1)
            policy=LimitPolicy('CHINABOND_BP',str(offset))
            valuation,valuation_date=self.valuation(code)
        elif item.get('pricing_basis','absolute')!='absolute':raise InvalidFact('Unknown limit pricing basis')
        boundary=policy.resolve(bond_code=code,valuation=valuation,expected_valuation_date=valuation_date)
        if boundary is None:raise InvalidFact('Limit valuation is unavailable')
        limit=number(boundary['value'])
        if (side=='BUY' and number(opening.value)<limit) or (side=='SELL' and number(opening.value)>limit):
            raise InvalidFact('Opening price is outside the explicitly authorized boundary')
        if policy.mode=='CHINABOND_BP' and number(item['limit_yield_pct'])!=limit:
            raise InvalidFact('Browser BP conversion differs from the trusted rounded server valuation')
        day=iso_day(item.get('trade_day',self.trade_day()))
        plan={'code':code,'name':name,'peer_ids':peers,'our_side':side,
              'volume_wan':str(volume),'opening_yield_pct':opening.value,
              'limit_yield_pct':boundary['value'],'step_bp':str(step),
              'pricing_basis':item.get('pricing_basis','absolute'),'trade_day':day}
        direction='收' if side=='BUY' else '出'
        plan['text']=f'{name} {code}，{opening.value}%{direction}。请报可做价格和数量。'
        return {'plan':plan,'policy':asdict(policy),'boundary':boundary,
                'valuation':asdict(valuation) if valuation else None,'valuation_date':valuation_date}
    def preview(self,owner,request_id,items):
        identity(request_id,'preview request')
        if not isinstance(items,list) or not 1<=len(items)<=500:raise InvalidFact('Preview needs 1..500 explicit bonds')
        prefs=self.repo.preferences(owner)
        normalized=[self.normalize_item(owner,item,preferences=prefs) for item in items]
        codes=[r['plan']['code'] for r in normalized]
        if len(codes)!=len(set(codes)):raise InvalidFact('A batch may have only one target group per bond')
        content_hash=digest(items)
        lines=[];per_peer=defaultdict(list)
        for r in normalized:
            for peer in r['plan']['peer_ids']:
                lines.append({**r['plan'],'peer_id':peer})
                per_peer[peer].append(r['plan']['text'])
        previews=[{'peer_id':p,'text':'\n'.join(v)} for p,v in per_peer.items()]
        if any(len(v['text'])>1500 for v in previews):raise InvalidFact('Initial combined message exceeds collector limit; reduce this batch')
        with self.repo.write(owner) as tx:
            binding=self.repo.require_binding(tx,owner)
            old=tx.execute('SELECT * FROM xj2_batch WHERE owner_id=? AND request_id=?',(owner,request_id)).fetchone()
            if old:
                if old['content_hash']!=content_hash:raise InvalidFact('Preview idempotency key reused with different inputs')
                return json.loads(old['preview_json'])['public']
            bid=new_id()
            public={'batch_id':bid,'version':1,'status':'preview','engine':'v22','items':lines,'commands_preview':previews}
            payload={'public':public,'items':normalized,'preferences_hash':digest(prefs)}
            tx.execute("INSERT INTO xj2_batch(batch_id,owner_id,request_id,content_hash,version,state,binding_version,preview_json,result_json,created_at) VALUES(?,?,?,?,1,'PREVIEW',?,?,'{}',?)",(bid,owner,request_id,content_hash,binding['version'],encoded(payload),self.clock()))
            self.repo.audit(tx,owner,'rfq_preview',bid,{'bond_count':len(codes),'peers':len(per_peer)})
        return public
    def confirm(self,owner,batch_id,expected_version,request_id):
        identity(request_id,'confirm request');identity(batch_id,'batch')
        # Refresh external valuation before acquiring a database transaction.
        with self.repo.db.transaction(write=False) as tx:
            found=tx.execute('SELECT preview_json FROM xj2_batch WHERE batch_id=? AND owner_id=?',(batch_id,owner)).fetchone()
        if not found:raise PermissionError('Preview does not exist for this trader')
        preview=json.loads(found['preview_json'])
        fresh={r['plan']['code']:self.valuation(r['plan']['code']) for r in preview['items'] if r['policy']['mode']=='CHINABOND_BP'}
        with self.repo.write(owner) as tx:
            b=self.repo.require_binding(tx,owner)
            row=tx.execute('SELECT * FROM xj2_batch WHERE batch_id=? AND owner_id=?',(batch_id,owner)).fetchone()
            if row['state']=='CONFIRMED':return json.loads(row['result_json'])
            if type(expected_version) is not int or expected_version!=row['version']:raise InvalidFact('Preview version changed')
            if row['binding_version']!=b['version']:raise InvalidFact('Account binding changed since preview')
            if self.clock()-row['created_at']>900:raise InvalidFact('Preview expired; review a fresh preview')
            if digest(json.loads(b['config_json']))!=preview['preferences_hash']:raise InvalidFact('Saved pricing settings changed; regenerate preview')
            control=tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?',(owner,)).fetchone()
            if not control['send_allowed']:
                # Confirm authorizes only this batch. Preserve the existing
                # global pause on old groups before opening the new groups.
                paused=tx.execute("SELECT group_id FROM xj2_group WHERE owner_id=? AND state='OPEN' AND send_allowed=1",(owner,)).fetchall()
                for group in paused:
                    tx.execute('UPDATE xj2_group SET send_allowed=0,control_version=control_version+1 WHERE group_id=?',(group['group_id'],))
                    self.repo._cancel_commands(tx,group_id=group['group_id'],reason='PRESERVE_PREVIOUS_BATCH_PAUSE')
            grouped=defaultdict(list);group_ids=[]
            for item in preview['items']:
                plan=item['plan'];code=plan['code']
                existing=tx.execute("SELECT group_id FROM xj2_group WHERE owner_id=? AND bond_code=? AND archived=0 AND state='OPEN' LIMIT 1",(owner,code)).fetchone()
                if existing:raise InvalidFact('An active bond group already exists; update or explicitly close it first')
                valuation=Valuation(**item['valuation']) if item['valuation'] else None
                if code in fresh:
                    actual,date=fresh[code]
                    if asdict(actual)!=item['valuation'] or date!=item['valuation_date']:raise InvalidFact('ChinaBond snapshot changed after preview')
                policy=LimitPolicy(**item['policy'])
                gid=self.repo._create_group(tx,owner,bond_code=code,our_side=plan['our_side'],target_volume=plan['volume_wan'],trade_day=plan['trade_day'],policy=policy,boundary=item['boundary'],valuation=valuation,send_allowed=True,plan=plan)
                group_ids.append(gid)
                for peer in plan['peer_ids']:
                    mid,tid=self.repo._add_member(tx,owner,gid,account_uid=b['account_uid'],peer_uid=peer)
                    grouped[peer].append((mid,tid,plan))
            # Confirmation explicitly authorizes these new groups, never clears an existing member's pause.
            tx.execute('UPDATE xj2_owner SET send_allowed=1 WHERE owner_id=?',(owner,))
            commands=[]
            for peer,values in grouped.items():
                primary=values[0][0];mids=[x[0] for x in values]
                body={'kind':'INITIAL','text':'\n'.join(x[2]['text'] for x in values),
                      'scope_members':mids,
                      'scope_prices':{m:Price('YIELD',p['opening_yield_pct']).dump() for m,_t,p in values},
                      'scope_bases':{m:self.commands_core._basis(tx,owner,m,False) for m in mids}}
                body=self.commands_core._body(body)
                basis=self.commands_core._basis(tx,owner,primary,False)
                cid=self.commands_core._insert(tx,owner,primary,basis,'initial:'+batch_id+':'+peer,body,self.clock()+120)
                commands.append({'command_id':cid,'cmd_id':cid,'status':'queued','peer_id':peer,'text':body['text'],'session_ids':mids})
            result={'batch_id':batch_id,'engine':'v22','group_ids':group_ids,'commands':commands}
            tx.execute("UPDATE xj2_batch SET state='CONFIRMED',result_json=? WHERE batch_id=?",(encoded(result),batch_id))
            self.repo.audit(tx,owner,'rfq_confirm',batch_id,{'group_ids':group_ids,'command_ids':[c['command_id'] for c in commands]})
            return result
    def set_bot(self,owner,enabled):
        if not self.repo.binding_for(owner):raise PermissionError('Bind an account first')
        self.repo.set_sending(owner,enabled)
        return {'botStatus':'正常' if enabled else '停止','analysis_enabled':True}
    def stop_sessions(self,owner,codes):
        snapshot=self.repo.snapshot(owner);changed=[]
        for group in snapshot['groups']:
            if group['bond_code'] in codes and not group['archived']:
                self.repo.set_sending(owner,False,group_id=group['group_id']);changed.append(group['group_id'])
        return {'stopped_groups':changed,'stopped':codes,'analysis_enabled':True}
    def manual_send(self,owner,peer,text,request_id):
        identity(peer,'counterparty');identity(request_id,'manual request')
        if not isinstance(text,str) or not text.strip() or len(text)>1500:raise InvalidFact('Manual message must have 1..1500 characters')
        with self.repo.write(owner) as tx:
            b=self.repo.require_binding(tx,owner)
            rows=tx.execute('SELECT p.member_id,p.thread_id FROM xj2_peer p JOIN xj2_thread t ON t.thread_id=p.thread_id WHERE t.owner_id=? AND t.account_uid=? AND t.peer_uid=? ORDER BY p.member_id',(owner,b['account_uid'],peer)).fetchall()
            if not rows:raise InvalidFact('Select an existing bond/counterparty conversation before human sending')
            old=tx.execute('SELECT * FROM xj2_command WHERE member_id=? AND request_id=?',(rows[0]['member_id'],request_id)).fetchone()
            if old:
                if json.loads(old['body_json'])['text']!=text:raise InvalidFact('Manual request key conflicts with previous text')
                return {'cmd_id':old['command_id'],'status':old['state'].lower()}
            for p in rows:
                tx.execute('UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE member_id=?',(p['member_id'],))
                self.repo._cancel_commands(tx,member_id=p['member_id'],reason='HUMAN_SEND')
            mids=[r['member_id'] for r in rows];mid=mids[0]
            body={'kind':'MANUAL','text':text,'scope_members':mids,'scope_prices':{},
                  'scope_bases':{m:self.commands_core._basis(tx,owner,m,False) for m in mids}}
            cid=self.commands_core._insert(tx,owner,mid,self.commands_core._basis(tx,owner,mid,False),request_id,body,self.clock()+120)
            return {'cmd_id':cid,'command_id':cid,'status':'queued','analysis_enabled':True}
    def sessions(self,owner):
        result=[]
        for group in self.repo.group_rows(owner):
            for peer in group['peers']:
                q=peer['quotes']['ofr' if group['our_side']=='BUY' else 'bid'];price=q.get('price') or {}
                state='HANDOFF' if group['state']=='HANDOFF' else 'STOPPED' if not group['send_allowed'] or not peer['send_allowed'] else 'WAITING_SEND' if peer['pending_command_id'] else 'NEGOTIATING'
                result.append({'session_id':peer['member_id'],'group_id':group['group_id'],'batch_id':group['group_id'],
                    'conversation_id':peer['thread_id'],'peer_id':peer['peer_uid'],'bond_code':group['bond_code'],
                    'state':state,'revision':peer['version'],'pending_command_id':peer['pending_command_id'],
                    'policy':group['plan'],'peer_quote':{**q,'yield_pct':price.get('value') if price.get('kind')=='YIELD' else price.get('computed_yield')},
                    'quotes':peer['quotes'],'our_quote':peer['our_quote'],'created_at':group['created_at'],
                    'archived':group['archived'],'summary':group['summary'],'analysis_pending':peer['analysis_pending']})
        return result
    def conversations(self,owner):
        return [{'conversation_id':t['thread_id'],'peer_id':t['peer_uid'],**t} for t in self.repo.snapshot(owner)['threads']]
    def commands(self,owner):
        with self.repo.db.transaction(write=False) as tx:
            rows=tx.execute('SELECT c.*,t.peer_uid FROM xj2_command c JOIN xj2_thread t ON t.thread_id=c.thread_id WHERE t.owner_id=? ORDER BY c.created_at,c.command_id',(owner,)).fetchall()
        return [{**dict(r),'status':r['state'].lower(),'peer_id':r['peer_uid'],'text':json.loads(r['body_json'])['text']} for r in rows]
    status_commands=commands
