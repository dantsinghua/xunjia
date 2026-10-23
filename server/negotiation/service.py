"""Production RFQ and human operations backed exclusively by the V2 fact core."""
from __future__ import annotations
from collections import defaultdict
from contextlib import contextmanager
import threading
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
def pricing_preferences(value):
    # Directory freshness is runtime evidence, not a price authorization input.
    # Keep every other preference in the fence, including future policy keys.
    return {key: item for key, item in value.items() if key not in ('collector_directory_checks','context_ready','context_epoch','context_peers')}

class SubmitRejected(InvalidFact):
    """A definitive rejection of this submission before new commands committed."""
    submitted = False
    def __init__(self, code, hint, *, field=None, bond_code=None):
        super().__init__(hint)
        self.submitted = False
        self.code, self.hint = code, hint
        self.field, self.bond_code = field, bond_code

class Inquiry:
    engine='v22'
    def __init__(self,store:ApplicationStore,*,reference_provider=None,calendar=None,valuation_source=None,submit_guard=None):
        self.repo=self.store=store
        self.clock=store.clock
        self.commands_core=Commands(store,current_day=self.trade_day)
        self.reference_provider=reference_provider
        self.calendar=calendar
        self.valuation_source=valuation_source  # Explicitly labelled ChinaBond values carried by Inno rows.
        self.diagnostics=Diagnostics(clock=self.clock)
        self.submit_guard=submit_guard
        self._submit_locks={}
        self._submit_lock_guard=threading.Lock()
    @contextmanager
    def _submission(self, owner):
        # Serialize retries within this service without holding a DB transaction
        # during guard/source I/O. The existing owner row lock still fences commits
        # from other service instances/processes.
        identity(owner,'owner')
        with self._submit_lock_guard:
            lock=self._submit_locks.setdefault(owner,threading.RLock())
        with lock:
            try:yield
            except SubmitRejected:raise
            except InvalidFact as exc:
                raise SubmitRejected('SUBMIT_REJECTED',str(exc)) from exc
    def _guard_submit(self, owner):
        if self.submit_guard is None:return
        try:
            allowed=self.submit_guard(owner)
        except SubmitRejected:raise
        except (ValueError,PermissionError) as exc:
            raise SubmitRejected(getattr(exc,'code','SUBMIT_UNAVAILABLE'),getattr(exc,'hint',str(exc)),
                                 field=getattr(exc,'field',None),bond_code=getattr(exc,'bond_code',None)) from exc
        except Exception as exc:
            raise SubmitRejected('SUBMIT_CHECK_FAILED','提交条件暂不可核验，未创建发送指令') from exc
        if allowed is False:
            raise SubmitRejected('SUBMIT_UNAVAILABLE','当前通道未允许提交，未创建发送指令')
    def trade_day(self):
        # Counterparty quotes require confirmation on EVERY new local day.
        # Valuation publication dates never change this counterparty guard.
        return datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).date().isoformat()
    def valuation(self,code):
        return self._resolve_valuation(code,*self._valuation_inputs())
    def _valuation_inputs(self,*,refresh_reference=False):
        # No publication-calendar lookup: source dates are display metadata.
        # Load a formal snapshot at most once, only as a fallback to Innodealing.
        if self.reference_provider is None and self.valuation_source is None:
            raise InvalidFact('CHINABOND_SOURCE_NOT_CONFIGURED')
        loaded=False;snapshot=None;failure=None
        def reference():
            nonlocal loaded,snapshot,failure
            if not loaded:
                loaded=True
                if self.reference_provider is not None:
                    if refresh_reference:
                        invalidate=getattr(self.reference_provider,'invalidate_valuation_cache',None)
                        if callable(invalidate):
                            try:invalidate()
                            except Exception:pass  # Still attempt the existing source read.
                    try:snapshot=self.reference_provider.snapshot()
                    except Exception as exc:failure=type(exc).__name__
            return snapshot,failure
        return None,reference,[]
    def _resolve_valuation(self,code,expected,reference,warnings):
        failures=[]
        if self.valuation_source is not None:
            try:
                value=self.valuation_source(code)
                if value is not None:
                    if not isinstance(value,Valuation) or value.bond_code!=code:
                        raise InvalidFact('Inno valuation adapter returned a different bond')
                    return value,expected
            except Exception as exc:failures.append(type(exc).__name__)
        snapshot,failure=reference()
        if failure:failures.append(failure)
        if snapshot is not None:
            try:
                rows=[b for b in snapshot.bonds if b.get('code',b.get('bond_id'))==code]
                if len(rows)!=1:raise InvalidFact('No unique ChinaBond valuation for this bond')
                row=rows[0]
                value=Valuation(code,row.get('valuation_date',snapshot.as_of),
                    str(row.get('valuation_yield_pct',row.get('val_yield',''))),snapshot.snapshot_id)
                return value,expected
            except Exception as exc:failures.append(type(exc).__name__)
        raise InvalidFact('CHINABOND_VALUATION_UNAVAILABLE:'+code+':'+','.join(failures))
    def _valuation_details(self,code,*,inputs=None):
        inputs=self._valuation_inputs() if inputs is None else inputs
        value,expected=self._resolve_valuation(code,*inputs)
        warnings=list(inputs[2])
        if value.trading_date is None:
            warnings.append('来源未提供有效估值日期；按所示中债估值计算。')
        return value,expected,warnings
    def refresh_valuations(self,*,refresh_reference=True):
        """Retry recovery even AFTER the group day advanced; no model/native I/O.
        The same entry is called after a successful source polling tick and
        immediately after newly ingested Inno rows. One resolution per bond is
        shared across owners. Transactions below recheck each policy version.
        """
        today=self.trade_day()
        with self.store.db.transaction(write=False) as tx:
            groups=[dict(g) for g in tx.execute('SELECT * FROM xj2_group WHERE archived=0').fetchall()]
        bp_codes={g['bond_code'] for g in groups if json.loads(g['policy_json'])['mode']=='CHINABOND_BP'}
        resolved={};errors={};inputs=None;input_error=None
        if bp_codes:
            try:
                inputs=self._valuation_inputs(refresh_reference=refresh_reference)
            except Exception as exc:input_error=str(exc)[:300]
        for code in sorted(bp_codes):
            if input_error is not None:
                errors[code]=input_error
                continue
            try:resolved[code]=self._resolve_valuation(code,*inputs)
            except Exception as exc:errors[code]=str(exc)[:300]
        result={'rolled':0,'revalued':0,'pending':[]}
        for g in groups:
            owner=g['owner_id'];gid=g['group_id'];is_bp=json.loads(g['policy_json'])['mode']=='CHINABOND_BP'
            valuation,expected=resolved.get(g['bond_code'],(None,None))
            try:
                if g['trade_day']<today:
                    self.store.rollover(owner,gid,trade_day=today,valuation=valuation,expected_valuation_date=expected)
                    result['rolled']+=1
                elif is_bp and valuation is not None:
                    if self.store.refresh_valuation(owner,gid,valuation=valuation,expected_valuation_date=expected,
                                                    expected_policy_version=g['policy_version']):
                        result['revalued']+=1
                if is_bp and g['bond_code'] in errors:
                    result['pending'].append({'group_id':gid,'bond_code':g['bond_code'],'reason':errors[g['bond_code']]})
                    self.diagnostics.record('valuation',owner=owner,entity=gid,code='VALUATION_REFRESH_PENDING')
                else:self.diagnostics.record('valuation',owner=owner,entity=gid)
            except Exception as exc:
                # One recovering group must not prevent the others from rolling.
                result['pending'].append({'group_id':gid,'bond_code':g['bond_code'],'reason':type(exc).__name__})
                self.diagnostics.record('valuation',owner=owner,entity=gid,code='VALUATION_APPLY_RETRY',error=exc)
        return result
    def normalize_item(self,owner,item,*,preferences=None,valuation_inputs=None):
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
        valuation=None;valuation_date=None;valuation_warnings=[]
        if item.get('pricing_basis','absolute')=='bp':
            prefs=preferences if preferences is not None else self.repo.preferences(owner)
            floor=prefs.get('floors',{}).get(code,{})
            if floor.get('kind')!='bp':raise InvalidFact('BP pricing requires a saved per-bond BP offset')
            offset=number(str(floor['value']))*(1 if side=='BUY' else -1)
            policy=LimitPolicy('CHINABOND_BP',str(offset))
            valuation,valuation_date,valuation_warnings=self._valuation_details(code,inputs=valuation_inputs)
        elif item.get('pricing_basis','absolute')!='absolute':raise InvalidFact('Unknown limit pricing basis')
        boundary=policy.resolve(bond_code=code,valuation=valuation,expected_valuation_date=valuation_date)
        if boundary is None:raise InvalidFact('Limit valuation is unavailable')
        # The trader explicitly supplies the initial inquiry yield. Its position
        # relative to the negotiation boundary is not a submission restriction.
        # In BP mode, the boundary is resolved from the current sourced valuation;
        # the browser's displayed conversion is advisory, never a second authority.
        day=iso_day(item.get('trade_day',self.trade_day()))
        plan={'code':code,'name':name,'peer_ids':peers,'our_side':side,
              'volume_wan':str(volume),'opening_yield_pct':opening.value,
              'limit_yield_pct':boundary['value'],'step_bp':str(step),
              'pricing_basis':item.get('pricing_basis','absolute'),'trade_day':day}
        if valuation is not None:
            plan['valuation_snapshot']={'snapshot_id':valuation.snapshot_id,
                'valuation_date':valuation.trading_date,'yield_pct':valuation.yield_pct,
                'provider':valuation.provider,'source':valuation.source}
            plan['valuation_warnings']=valuation_warnings
        direction='收' if side=='BUY' else '出'
        plan['text']=f'{name} {code}，{opening.value}%{direction}。请报可做价格和数量。'
        return {'plan':plan,'policy':asdict(policy),'boundary':boundary,
                'valuation':asdict(valuation) if valuation else None,'valuation_date':valuation_date}
    def _prepare_batch(self,owner,items):
        if not isinstance(items,list) or not 1<=len(items)<=500:raise InvalidFact('Preview needs 1..500 explicit bonds')
        prefs=self.repo.preferences(owner)
        # One reference snapshot per preparation; each bond still resolves its
        # own explicit price/boundary using the existing validation core.
        inputs=self._valuation_inputs() if any(isinstance(i,dict) and i.get('pricing_basis')=='bp' for i in items) else None
        normalized=[self.normalize_item(owner,item,preferences=prefs,valuation_inputs=inputs) for item in items]
        codes=[r['plan']['code'] for r in normalized]
        if len(codes)!=len(set(codes)):raise InvalidFact('A batch may have only one target group per bond')
        lines=[];per_peer=defaultdict(list)
        for r in normalized:
            for peer in r['plan']['peer_ids']:
                lines.append({**r['plan'],'peer_id':peer})
                per_peer[peer].append(r['plan']['text'])
        previews=[{'peer_id':p,'text':'\n'.join(v)} for p,v in per_peer.items()]
        if any(len(v['text'])>1500 for v in previews):raise InvalidFact('Initial combined message exceeds collector limit; reduce this batch')
        return prefs,normalized,lines,previews
    def preview(self,owner,request_id,items):
        identity(request_id,'preview request')
        prefs,normalized,lines,previews=self._prepare_batch(owner,items)
        codes=[r['plan']['code'] for r in normalized]
        per_peer={p['peer_id']:p for p in previews}
        content_hash=digest(items)
        with self.repo.write(owner) as tx:
            binding=self.repo.require_binding(tx,owner)
            epoch=self.repo.account_epoch(tx,owner,binding['account_uid'])
            old=tx.execute('SELECT * FROM xj2_batch WHERE owner_id=? AND request_id=?',(owner,request_id)).fetchone()
            if old:
                self._require_batch_scope(tx,owner,old)
                if old['content_hash']!=content_hash:raise InvalidFact('Preview idempotency key reused with different inputs')
                return json.loads(old['preview_json'])['public']
            self.repo._ensure_peer_capacity(tx,owner,binding['account_uid'],per_peer)
            bid=new_id()
            public={'batch_id':bid,'version':1,'status':'preview','engine':'v22','items':lines,'commands_preview':previews}
            payload={'public':public,'items':normalized,'preferences_hash':digest(pricing_preferences(prefs)),'preferences_scope':'pricing-v1','account_epoch':epoch}
            tx.execute("INSERT INTO xj2_batch(batch_id,owner_id,request_id,content_hash,version,state,binding_version,preview_json,result_json,created_at) VALUES(?,?,?,?,1,'PREVIEW',?,?,'{}',?)",(bid,owner,request_id,content_hash,binding['version'],encoded(payload),self.clock()))
            self.repo.audit(tx,owner,'rfq_preview',bid,{'bond_count':len(codes),'peers':len(per_peer)})
        return public
    def _require_batch_scope(self,tx,owner,row):
        binding=self.repo.require_binding(tx,owner)
        preview=json.loads(row['preview_json'])
        epoch=self.repo.account_epoch(tx,owner,binding['account_uid'])
        if row['binding_version']!=binding['version'] or preview.get('account_epoch',0)!=epoch:
            raise InvalidFact('Stored request belongs to a retired account context; submit a new request id')
        result=json.loads(row['result_json'])
        if any(self.repo.group_is_retired(tx,owner,gid) for gid in result.get('group_ids',[])):
            raise InvalidFact('Stored request belongs to a retired workspace')
    def confirm(self,owner,batch_id,expected_version,request_id):
        with self._submission(owner):
            identity(request_id,'confirm request');identity(batch_id,'batch')
            with self.repo.db.transaction(write=False) as tx:
                found=tx.execute('SELECT * FROM xj2_batch WHERE batch_id=? AND owner_id=?',(batch_id,owner)).fetchone()
                if found:self._require_batch_scope(tx,owner,found)
            if not found:raise PermissionError('Preview does not exist for this trader')
            if found['state']=='CONFIRMED':return json.loads(found['result_json'])
            self._guard_submit(owner)
            preview=json.loads(found['preview_json'])
            # Explicit preview confirmation deliberately revalidates valuation.
            fresh={r['plan']['code']:self.valuation(r['plan']['code']) for r in preview['items'] if r['policy']['mode']=='CHINABOND_BP'}
            with self.repo.write(owner) as tx:
                row=tx.execute('SELECT * FROM xj2_batch WHERE batch_id=? AND owner_id=?',(batch_id,owner)).fetchone()
                self._require_batch_scope(tx,owner,row)
                if row['state']=='CONFIRMED':return json.loads(row['result_json'])
                b=self.repo.require_binding(tx,owner)
                return self._commit_batch(tx,owner,row,b,preview,expected_version,fresh)
    def _commit_batch(self,tx,owner,row,b,preview,expected_version,fresh):
        batch_id=row['batch_id']
        epoch=self.repo.account_epoch(tx,owner,b['account_uid'],require_complete=True)
        if preview.get('account_epoch',0)!=epoch:raise InvalidFact('Preview belongs to an earlier account reset; regenerate preview')
        if type(expected_version) is not int or expected_version!=row['version']:raise InvalidFact('Preview version changed')
        if row['binding_version']!=b['version']:raise InvalidFact('Account binding changed since preview')
        if self.clock()-row['created_at']>900:raise InvalidFact('Preview expired; review a fresh preview')
        prefs=json.loads(b['config_json'])
        checked=pricing_preferences(prefs) if preview.get('preferences_scope')=='pricing-v1' else prefs
        if digest(checked)!=preview['preferences_hash']:raise InvalidFact('Saved pricing settings changed; regenerate preview')
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
                if actual.authorization_key()!=valuation.authorization_key():
                    raise InvalidFact('ChinaBond valuation changed after preview; regenerate preview')
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

    def send_direct(self,owner,request_id,items):
        """Prepare once, then atomically commit the batch and its commands."""
        with self._submission(owner):
            identity(request_id,'direct send request')
            if not isinstance(items,list) or not 1<=len(items)<=500:
                raise InvalidFact('直接发送需要1到500只明确债券')
            request_key='direct:'+digest(request_id)
            try:content_hash=digest(items)
            except (ValueError,TypeError) as exc:raise InvalidFact('直接发送参数格式无效') from exc
            with self.repo.db.transaction(write=False) as tx:
                old=tx.execute('SELECT * FROM xj2_batch WHERE owner_id=? AND request_id=?',(owner,request_key)).fetchone()
                if old:
                    self._require_batch_scope(tx,owner,old)
                    if old['content_hash']!=content_hash:raise InvalidFact('同一发送请求的参数已变化，请勿重复使用请求编号')
                    if old['state']=='CONFIRMED':return json.loads(old['result_json'])
                before=self.repo.require_binding(tx,owner)
                epoch=self.repo.account_epoch(tx,owner,before['account_uid'],require_complete=True)
                if old:
                    previous=json.loads(old['preview_json'])
                    if old['binding_version']!=before['version'] or previous.get('account_epoch',0)!=epoch:
                        raise InvalidFact('原发送请求的账号版本已变化，请重新核对后提交')
            self._guard_submit(owner)
            prefs,normalized,lines,previews=self._prepare_batch(owner,items)
            preferences_hash=digest(pricing_preferences(prefs))
            with self.repo.write(owner) as tx:
                row=tx.execute('SELECT * FROM xj2_batch WHERE owner_id=? AND request_id=?',(owner,request_key)).fetchone()
                if row:
                    self._require_batch_scope(tx,owner,row)
                    if row['content_hash']!=content_hash:raise InvalidFact('同一发送请求的参数已变化，请勿重复使用请求编号')
                    if row['state']=='CONFIRMED':return json.loads(row['result_json'])
                b=self.repo.require_binding(tx,owner)
                if any(b[k]!=before[k] for k in ('account_uid','workbench_id','version')):
                    raise InvalidFact('发送准备期间账号已变化，请重新核对后提交')
                if self.repo.account_epoch(tx,owner,b['account_uid'],require_complete=True)!=epoch:
                    raise InvalidFact('发送准备期间账号会话版本已变化，请重新核对后提交')
                if row:
                    previous=json.loads(row['preview_json'])
                    old_prefs=pricing_preferences(prefs) if previous.get('preferences_scope')=='pricing-v1' else prefs
                    if row['binding_version']!=b['version'] or previous.get('account_epoch',0)!=epoch or digest(old_prefs)!=previous['preferences_hash']:
                        raise InvalidFact('原发送请求的账号或价格授权已变化，请重新核对后提交')
                self.repo._ensure_peer_capacity(tx,owner,b['account_uid'],{p['peer_id']:p for p in previews})
                bid=row['batch_id'] if row else new_id()
                public={'batch_id':bid,'version':1,'status':'preview','engine':'v22','items':lines,'commands_preview':previews}
                prepared={'public':public,'items':normalized,'preferences_hash':preferences_hash,'preferences_scope':'pricing-v1','account_epoch':epoch}
                if row:
                    tx.execute('UPDATE xj2_batch SET preview_json=?,created_at=? WHERE batch_id=?',(encoded(prepared),self.clock(),bid))
                else:
                    tx.execute("INSERT INTO xj2_batch(batch_id,owner_id,request_id,content_hash,version,state,binding_version,preview_json,result_json,created_at) VALUES(?,?,?,?,1,'PREVIEW',?,?,'{}',?)",(bid,owner,request_key,content_hash,b['version'],encoded(prepared),self.clock()))
                row=tx.execute('SELECT * FROM xj2_batch WHERE batch_id=? AND owner_id=?',(bid,owner)).fetchone()
                # There is no second live valuation fetch for direct submission.
                return self._commit_batch(tx,owner,row,b,prepared,row['version'],{})

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
    def _manual_result(self,tx,owner,peer,text,request_id):
        old=tx.execute('SELECT c.*,t.peer_uid FROM xj2_command c JOIN xj2_thread t ON t.thread_id=c.thread_id WHERE t.owner_id=? AND c.request_id=?',(owner,request_id)).fetchone()
        if not old:return None
        binding=self.repo.require_binding(tx,owner)
        thread=self.repo._thread(tx,owner,old['thread_id'])
        basis=json.loads(old['basis_json'])
        if (not self.repo.thread_is_current(tx,thread) or self.repo.group_is_retired(tx,owner,old['group_id'])
                or basis.get('binding_version',0)!=binding['version']):
            raise InvalidFact('Manual request belongs to a retired account context; submit a new request id')
        body=json.loads(old['body_json'])
        if old['peer_uid']!=peer or body.get('kind')!='MANUAL' or body.get('text')!=text:
            raise InvalidFact('Manual request key conflicts with previous recipient or text')
        return {'cmd_id':old['command_id'],'command_id':old['command_id'],'status':old['state'].lower(),'analysis_enabled':True}
    def manual_send(self,owner,peer,text,request_id):
        with self._submission(owner):
            identity(peer,'counterparty');identity(request_id,'manual request')
            if not isinstance(text,str) or not text.strip() or len(text)>1500:raise InvalidFact('Manual message must have 1..1500 characters')
            with self.repo.db.transaction(write=False) as tx:
                old=self._manual_result(tx,owner,peer,text,request_id)
                before=None if old else self.repo.require_binding(tx,owner)
            if old:return old
            self._guard_submit(owner)
            with self.repo.write(owner) as tx:
                old=self._manual_result(tx,owner,peer,text,request_id)
                if old:return old
                b=self.repo.require_binding(tx,owner)
                if any(b[k]!=before[k] for k in ('account_uid','workbench_id','version')):
                    raise InvalidFact('发送准备期间账号已变化，请重新核对后提交')
                self.repo.account_epoch(tx,owner,b['account_uid'],require_complete=True)
                rows=tx.execute('SELECT p.member_id,p.thread_id FROM xj2_peer p JOIN xj2_thread t ON t.thread_id=p.thread_id JOIN xj2_group g ON g.group_id=p.group_id WHERE t.owner_id=? AND t.workbench_id=? AND t.account_uid=? AND t.peer_uid=? AND g.archived=0 ORDER BY p.member_id',(owner,b['workbench_id'],b['account_uid'],peer)).fetchall()
                if not rows:raise InvalidFact('Select an existing bond/counterparty conversation before human sending')
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
                    'policy':{**group['plan'],'trade_day':group['trade_day'],
                              'limit_yield_pct':group['boundary']['value'] if group['boundary'] else None},'peer_quote':{**q,'yield_pct':price.get('value') if price.get('kind')=='YIELD' else price.get('computed_yield')},
                    'quotes':peer['quotes'],'our_quote':peer['our_quote'],'created_at':group['created_at'],
                    'archived':group['archived'],'summary':group['summary'],'analysis_pending':peer['analysis_pending']})
        return result
    def conversations(self,owner):
        return [{'conversation_id':t['thread_id'],'peer_id':t['peer_uid'],**t} for t in self.repo.snapshot(owner)['threads']]
    def commands(self,owner):
        rows=self.repo.command_rows(owner)
        return [{**dict(r),'status':r['state'].lower(),'peer_id':r['peer_uid'],'text':json.loads(r['body_json'])['text']} for r in rows]
    status_commands=commands
