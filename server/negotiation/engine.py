"""Two-stage runtime: receive -> facts transaction -> independently validated reply.
Network/model calls never run under database locks. Analysis scheduling is
independent of sending controls and outstanding native acknowledgements.
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
import time
from .prices import InvalidFact, Price, number, resolve_yield_suffix, valuation_quote, volume_wan
from .store import encoded, new_id
from .validation import validate_public_text, validate_observation
class Engine:
    def __init__(self,service,chatflows,tools,*,aggregation=None):
        self.service=service;self.store=service.store;self.commands=service.commands_core
        self.client=chatflows;self.tools=tools;self.aggregation=aggregation
    def _tool_call(self,method,context):
        working=deepcopy(context);working['tool_results']=[]
        used=0;deadline=time.monotonic()+150
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise InvalidFact('CHATFLOW_STAGE_DEADLINE_EXCEEDED')
            working['_stage_budget_seconds']=remaining
            result=getattr(self.client,method)(working)
            if not isinstance(result,dict) or result.get('schema_version')!='2.2':raise InvalidFact('Expected V2.2 Chatflow stage result')
            requests=result.get('tool_requests',[])
            if not requests:return result
            if not isinstance(requests,list) or not 1<=len(requests)<=6 or used+len(requests)>6:raise InvalidFact('Chatflow exceeded bounded public tool calls')
            for request in requests:
                if not isinstance(request,dict) or set(request)!={'name','arguments'}:raise InvalidFact('Invalid public tool request')
                response=self.tools.invoke(request['name'],request['arguments'])
                working['tool_results'].append({'request':request,'result':response})
                used+=1
    def _cash_conversion(self,snapshot,observation,member):
        price=observation['changes'].get('price')
        if not price or price['kind']=='YIELD' or not price.get('settlement_date'):return None
        known={m['message_id']:m for p in snapshot['members'] for m in p['context']}
        known.update({m['message_id']:m for m in snapshot['messages']})
        evidence=[known[x] for x in observation['evidence']['price'] if x in known and known[x]['direction']=='in']
        settlement=price['settlement_date'];confirmed=False
        for message in evidence:
            if settlement in message['text']:confirmed=True
            if re.search(r'T\+0|T＋0',message['text'],re.I) and settlement==message['trade_day']:confirmed=True
            if re.search(r'T\+1|T＋1',message['text'],re.I) and self.service.calendar is not None:
                resolve=getattr(self.service.calendar,'settlement_date',None)
                if callable(resolve) and resolve(message['trade_day'],'T+1')==settlement:confirmed=True
        if not confirmed:
            # Preserve the actual cash price; ignore a model-invented date. This
            # leaves the quote incomparable and B must ask the actual settlement.
            price['settlement_date']=None
            return {'status':'SETTLEMENT_CONFIRMATION_REQUIRED','member_id':member['member_id']}
        if self.service.reference_provider is None:return {'status':'TERMS_NOT_CONFIGURED','member_id':member['member_id']}
        try:
            snapshot_data=self.service.reference_provider.snapshot()
            matches=[b for b in snapshot_data.bonds if member['bond_code'] in (b.get('bond_id'),b.get('code'))]
            if len(matches)!=1:return {'status':'BOND_TERMS_UNRESOLVED','member_id':member['member_id']}
            bond_id=matches[0].get('bond_id') or member['bond_code']
            terms=snapshot_data.terms.get(bond_id,{})
            if not terms.get('terms_snapshot_id'):return {'status':'VERIFIED_TERMS_MISSING','member_id':member['member_id']}
            request={'schema_version':'1','operation':'yield_from_price','inputs':{
                'bond_id':bond_id,'terms_snapshot_id':terms['terms_snapshot_id'],
                'settlement_date':settlement,'price_type':'clean' if price['kind']=='NET_PRICE' else 'dirty',
                'price_per_100':price['value']}}
            response=self.tools.invoke('bond_calc',request)
            data=response.get('data') or {};out=data.get('result') or {}
            accepted=(response.get('status') in ('ok','partial') and not data.get('hypothetical')
                      and out.get('yield_pct') is not None and out.get('solver_status')=='converged')
            record={'member_id':member['member_id'],'request':request,'response':response,'status':'VERIFIED' if accepted else 'CALCULATION_UNAVAILABLE'}
            if accepted:
                ref='calc-'+hashlib.sha256(encoded(record).encode()).hexdigest()
                normalized=Price(price['kind'],price['value'],settlement_date=settlement,
                                 computed_yield=str(Decimal(out['yield_pct']).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)),calculation_ref=ref)
                observation['changes']['price']=normalized.dump();record['calculation_ref']=ref
            return record
        except Exception as exc:
            return {'status':'CALCULATION_UNAVAILABLE','error_type':type(exc).__name__,'member_id':member['member_id']}
    def _normalize(self,snapshot,result):
        result['_verified_calculations']=[]
        snapshot=deepcopy(snapshot)
        snapshot['corrections']=result.get('corrections',[])
        scopes=deepcopy(result.get('scopes'));observations=deepcopy(result.get('observations'))
        unresolved=deepcopy(result.get('unresolved',{}))
        if not isinstance(scopes,dict) or not isinstance(observations,list):raise InvalidFact('Analysis needs scopes and observations')
        members={p['member_id']:p for p in snapshot['members']}
        aliases={}
        for mid,member in members.items():
            for alias in (member['bond_code'],member.get('plan',{}).get('name')):
                if alias:aliases.setdefault(str(alias).casefold(),set()).add(mid)
        if self.service.reference_provider:
            try:
                public=self.service.reference_provider.snapshot()
                for bond in public.bonds:
                    mids={m for m,p in members.items() if p['bond_code'] in (bond.get('code'),bond.get('bond_id'))}
                    for alias in bond.get('aliases',[]):
                        if isinstance(alias,str) and mids:aliases.setdefault(alias.casefold(),set()).update(mids)
            except Exception:
                pass  # Missing reference facts do not erase raw messages or known explicit codes.
        # Explicit unique codes/aliases beat a model's obsolete focus. No quoted
        # price is reassigned: incompatible observations fail field evidence checks.
        for message in snapshot['messages']:
            explicit=set()
            text=message['text'].casefold()
            for alias,mids in aliases.items():
                if len(mids)==1 and re.search(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',text):explicit.update(mids)
            if explicit:
                scopes[message['message_id']]=sorted(explicit)
                unclear=unresolved.get(message['message_id'],[])
                unresolved[message['message_id']]=[m for m in unclear if m not in explicit]
        checked=[]
        for observation in observations:
            try:
                if not isinstance(observation,dict):raise InvalidFact("Invalid observation")
                member=members.get(observation.get('member_id'))
                if not member:raise InvalidFact("Unknown member")
                changes=observation.get('changes',{})
                if not isinstance(changes,dict):raise InvalidFact("Invalid changes")
                interpretation=observation.get('interpretation')
                if isinstance(interpretation,dict) and interpretation.get('intent') in ('QUOTE','INDICATIVE','QUESTION','REPORTED') and 'assertion' not in changes:
                    changes['assertion']=interpretation['intent']
                    evidence=observation.get('evidence',{})
                    evidence['assertion']=list(dict.fromkeys(ref for refs in evidence.values() for ref in refs))
                price=changes.get('price')
                raw_price=deepcopy(price)
                if isinstance(price,dict):
                    raw=dict(price);notation=raw.pop('notation','ABSOLUTE')
                    if raw.get('computed_yield') is not None:
                        raise InvalidFact('Model may not invent calculator evidence or derived yields; use verified calculator integration')
                    if notation=='YIELD_SUFFIX':
                        current=member['quotes'].get(observation.get('side'),{}).get('price')
                        if current and current.get('kind')=='YIELD':anchor=current['value']
                        else:
                            previous=json.loads(member.get('our_quote_json','{}')).get('price',{})
                            if previous.get('kind')=='YIELD':anchor=previous['value']
                            else:
                                value,_day=self.service.valuation(member['bond_code']);anchor=value.yield_pct
                        raw['kind']='YIELD';raw['value']=str(resolve_yield_suffix(raw['value'],contextual_anchor=anchor))
                    elif notation=='CHINABOND_BP':
                        value,_day=self.service.valuation(member['bond_code'])
                        raw['kind']='YIELD';raw['value']=str(valuation_quote(value.yield_pct,raw['value']))
                    elif notation!='ABSOLUTE':raise InvalidFact('Unsupported price notation')
                    changes['price']=Price.load(raw).dump()
                if changes.get('volume_wan') is not None:
                    # All target quantities and quoted-amount fields explicitly use wan.
                    # LLM supplies the raw classified QUANTITY, never a bare price token.
                    changes['volume_wan']=str(volume_wan(str(changes['volume_wan']),contextual_unit='万'))
                validated=validate_observation(snapshot,observation,aliases,raw_price=raw_price)
                calculation=self._cash_conversion(snapshot,validated,member)
                if calculation:result['_verified_calculations'].append(calculation)
                checked.append(validated)
            except (InvalidFact,ValueError,KeyError,TypeError,StopIteration) as exc:
                affected={observation['member_id']} if isinstance(observation,dict) and observation.get('member_id') in members else set()
                checked.append({'member_id':observation.get('member_id') if isinstance(observation,dict) else None,
                                'side':observation.get('side') if isinstance(observation,dict) else None,
                                'affected_members':sorted(affected),'validation_error':str(exc)})
        observations=checked
        return scopes,observations,unresolved
    def analyze_thread(self,owner,thread_id,*,force=False):
        if self.client is None:return None
        job=self.store.claim_analysis(owner,thread_id,aggregation=self.aggregation,force=force,lease_seconds=180)
        if job is None:return None
        start=time.monotonic()
        snapshot=deepcopy(job['snapshot'])
        # Parser gets actual message context, never internal limit prices.
        for member in snapshot['members']:
            member['plan']={k:v for k,v in member.get('plan',{}).items() if k in ('name','code','our_side','trade_day')}
        try:
            result=self._tool_call('analyze',snapshot)
            # A may resolve an older unresolved fragment. Hydrate only explicit
            # anchors in the same frozen thread; never trust model-supplied text.
            correction_context=[]
            with self.store.db.transaction(write=False) as tx:
                for correction in result.get('corrections',[]):
                    if not isinstance(correction,dict):raise InvalidFact('Invalid correction')
                    row=tx.execute('SELECT * FROM xj2_message WHERE message_id=? AND thread_id=? AND ingest_revision<=?',
                        (correction.get('message_id'),thread_id,job['snapshot']['through_revision'])).fetchone()
                    if not row:raise InvalidFact('Correction anchor is outside this snapshot')
                    correction_context.append(dict(row))
            job['snapshot']['correction_context']=correction_context
            scopes,observations,unresolved=self._normalize(job['snapshot'],result)
            saved=self.store.commit_analysis(owner,job['analysis_id'],job['lease_token'],scopes=scopes,observations=observations,unresolved=unresolved,corrections=result.get('corrections'))
            if result.get('_verified_calculations'):
                with self.store.write(owner) as tx:
                    self.store.audit(tx,owner,'cash_conversion_evidence',job['analysis_id'],{'calculations':result['_verified_calculations']})
            self.service.diagnostics.record('analysis',owner=owner,entity=job['analysis_id'])
            return saved
        except Exception as exc:
            try:self.store.fail_analysis(owner,job['analysis_id'],job['lease_token'],reason=type(exc).__name__,retry_after_seconds=min(60,2**min(job['attempt'],5)))
            except Exception:pass
            self.service.diagnostics.record('analysis',owner=owner,code='ANALYSIS_RETRY',error=exc,entity=job['analysis_id'])
            return {'facts_committed':False,'analysis_id':job['analysis_id'],'error':type(exc).__name__}
        finally:
            with self.store.write(owner) as tx:
                self.store.audit(tx,owner,'analysis_duration',job['analysis_id'],{'elapsed_ms':round((time.monotonic()-start)*1000,3)})
    def _reply_context(self,tx,owner,mid,basis):
        member=self.store._member(tx,owner,mid)
        group=self.store._group(tx,owner,member['group_id'])
        thread=self.store._thread(tx,owner,member['thread_id'])
        unresolved=[dict(r) for r in tx.execute("SELECT m.* FROM xj2_message m JOIN xj2_scope s ON s.message_id=m.message_id WHERE s.member_id=? AND s.relation IN ('UNRESOLVED','INVALID') AND m.ingest_revision<=? ORDER BY m.ingest_revision DESC LIMIT ?",(mid,thread['analyzed_revision'],self.store.settings.context_messages)).fetchall()]
        return {'member_id':mid,'thread_id':member['thread_id'],'basis':basis,
                'bond_code':group['bond_code'],'our_side':group['our_side'],'trade_day':group['trade_day'],
                'policy':json.loads(group['policy_json']),'boundary':json.loads(group['boundary_json']),
                'plan':json.loads(group['plan_json']),'group_summary':self.store._summary(tx,group),
                'quotes':{s:self.store._quote(tx,mid,s).dump() for s in ('bid','ofr')},
                'our_quote':json.loads(member['our_quote_json']),'reconfirm_day':member['reconfirm_day'],
                'reconfirm_command_id':member['reconfirm_command_id'],'unresolved_messages':unresolved,
                'response_state':member['response_state'],
                'context':self.store._context(tx,mid,thread['analyzed_revision'])}
    def _verified_reply_cash(self,context,price):
        peer=context['quotes']['ofr' if context['our_side']=='BUY' else 'bid'].get('price') or {}
        if (not price.settlement_date or price.settlement_date!=peer.get('settlement_date')
                or not peer.get('calculation_ref') or self.service.reference_provider is None):
            raise InvalidFact('Cash reply needs counterpart-confirmed settlement and comparable verified terms')
        snapshot=self.service.reference_provider.snapshot()
        matches=[b for b in snapshot.bonds if context['bond_code'] in (b.get('bond_id'),b.get('code'))]
        if len(matches)!=1:raise InvalidFact('Bond terms are not unique')
        bond_id=matches[0].get('bond_id') or context['bond_code']
        terms=snapshot.terms.get(bond_id,{})
        request={'schema_version':'1','operation':'yield_from_price','inputs':{'bond_id':bond_id,
            'terms_snapshot_id':terms.get('terms_snapshot_id'),'settlement_date':price.settlement_date,
            'price_type':'clean' if price.kind=='NET_PRICE' else 'dirty','price_per_100':price.value}}
        response=self.tools.invoke('bond_calc',request);data=response.get('data') or {};result=data.get('result') or {}
        if (response.get('status') not in ('ok','partial') or data.get('hypothetical')
                or result.get('yield_pct') is None or result.get('solver_status')!='converged'):
            raise InvalidFact('Verified cash-to-yield calculation unavailable')
        reference='calc-'+hashlib.sha256(encoded({'request':request,'response':response}).encode()).hexdigest()
        return Price(price.kind,price.value,settlement_date=price.settlement_date,
                     computed_yield=str(result['yield_pct']),calculation_ref=reference)
    def _validate_reply(self,context,proposal):
        if not isinstance(proposal,dict) or set(proposal)-{'kind','text','our_price'}:raise InvalidFact('Unexpected reply fields')
        kind=proposal.get('kind')
        if kind not in ('REPLY','RECONFIRM','ASK_VOLUME','CLARIFY','SOCIAL'):raise InvalidFact('Unsupported reply action')
        if context.get('unresolved_messages') and kind!='CLARIFY':raise InvalidFact('Resolve the uncertain bond fragment before an economic reply')
        if context['reconfirm_day'] and kind not in ('RECONFIRM','CLARIFY'):raise InvalidFact('Must ask this peer for today\'s quote first')
        body=dict(proposal)
        price=body.get('our_price')
        if kind=='REPLY' and price is None:raise InvalidFact('A quote reply needs a structured our_price')
        if kind!='REPLY' and price is not None:raise InvalidFact('Nonquote actions must not silently insert a price')
        if price is not None:
            price=Price.load(price)
            boundary=context['boundary']
            if price.kind!='YIELD':
                # Always recalculate cash proposals; any model-supplied derived
                # yield/reference is ignored, not used to authorize execution.
                price=self._verified_reply_cash(context,price)
            if not boundary or boundary['kind']!='YIELD':raise InvalidFact('Reply boundary requires a verified comparable yield')
            quoted,limit=price.as_yield(),number(boundary['value'])
            if context['our_side']=='BUY' and quoted<limit or context['our_side']=='SELL' and quoted>limit:raise InvalidFact('Reply exceeds internal limit')
            last=context['our_quote'].get('price')
            previous=Price.load(last).as_yield() if last else context['plan'].get('opening_yield_pct')
            step=context['plan'].get('step_bp')
            if previous is None or step is None:raise InvalidFact('Authorized initial price/step is unavailable')
            if abs(quoted-number(previous))*100>number(step):raise InvalidFact('Reply exceeds maximum step from actual sent price')
            text=body.get('text','')
            advertised=re.findall(r'(?<![\d.])(-?\d+(?:\.\d+)?)\s*%',text)
            if price.kind=='YIELD':
                if not advertised or any(number(v)!=quoted for v in advertised):raise InvalidFact('Public yield text differs from validated quote')
            elif advertised or ('净价' if price.kind=='NET_PRICE' else '全价') not in text:
                raise InvalidFact('Cash quote must explicitly state its price kind, not an unvalidated yield')
            body['our_price']=price.dump()
        validate_public_text(context,body)
        return body
    def decide_member(self,owner,mid):
        if self.client is None:return None
        with self.store.write(owner) as tx:
            basis=self.commands._basis(tx,owner,mid,True)
            context=self._reply_context(tx,owner,mid,basis)
            gate_kind='CLARIFY' if context['unresolved_messages'] else 'RECONFIRM' if context['reconfirm_day'] else 'REPLY'
            if self.commands._gate(tx,owner,mid,basis,kind=gate_kind):
                return None
            recent=sorted(context['context']+context['unresolved_messages'],key=lambda m:m['ingest_revision'])
            first_reconfirmation=context['reconfirm_day'] and not context['reconfirm_command_id']
            if not first_reconfirmation and (not recent or recent[-1]['direction']!='in' or recent[-1]['historical']):return None
            request='decision:'+hashlib.sha256(encoded(basis).encode()).hexdigest()
            old=tx.execute('SELECT * FROM xj2_reply_job WHERE member_id=? AND request_id=?',(mid,request)).fetchone()
            if old and (old['state']=='DONE' or old['lease_until']>self.store.clock()):return None
            token=new_id()
            if old:
                job_id=old['job_id']
                tx.execute("UPDATE xj2_reply_job SET state='RUNNING',lease_token=?,lease_until=?,attempts=attempts+1 WHERE job_id=?",(token,self.store.clock()+180,job_id))
            else:
                job_id=new_id()
                tx.execute("INSERT INTO xj2_reply_job(job_id,member_id,request_id,basis_json,state,lease_token,lease_until,attempts,result_json,created_at) VALUES(?,?,?,?,'RUNNING',?,?,1,'{}',?)",(job_id,mid,request,encoded(basis),token,self.store.clock()+180,self.store.clock()))
        start=time.monotonic()
        try:
            returned=self._tool_call('decide',context)
            proposal=returned.get('proposal');action=returned.get('action')
            with self.store.write(owner) as tx:
                lease=tx.execute('SELECT state,lease_token,lease_until FROM xj2_reply_job WHERE job_id=?',(job_id,)).fetchone()
                if lease['state']!='RUNNING' or lease['lease_token']!=token or lease['lease_until']<=self.store.clock():return {'state':'STALE_LEASE'}
            if action in ('WAIT','HANDOFF'):
                if proposal is not None:raise InvalidFact('WAIT/HANDOFF must not carry a message')
                with self.store.write(owner) as tx:
                    reason=self.commands._gate(tx,owner,mid,basis,kind=gate_kind)
                    if reason:result={'state':'REJECTED','error':{'reason':reason}}
                    else:
                        result={'state':'WAIT' if action=='WAIT' else 'HANDOFF','trader_view':returned.get('trader_view',{})}
                        if action=='HANDOFF':
                            tx.execute('UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE member_id=?',(mid,))
                            self.store._cancel_commands(tx,member_id=mid,reason='CHATFLOW_HANDOFF')
                    tx.execute('INSERT INTO xj2_decision(decision_id,member_id,basis_json,state,result_json,created_at) VALUES(?,?,?,?,?,?)',(new_id(),mid,encoded(basis),result['state'],encoded(result),self.store.clock()))
            else:
                if action!='SEND':raise InvalidFact('Reply stage must choose SEND, WAIT, or HANDOFF')
                result=self.commands.propose(owner,mid,basis=basis,request_id=request,proposal=proposal,
                       validator=lambda p:self._validate_reply(context,p),expires_at=self.store.clock()+120)
            with self.store.write(owner) as tx:
                tx.execute("UPDATE xj2_reply_job SET state=?,lease_until=?,result_json=? WHERE job_id=? AND lease_token=?",('REJECTED' if result['state']=='REJECTED' else 'DONE',self.store.clock()+5,encoded(result),job_id,token))
                self.store.audit(tx,owner,'reply_stage_result',job_id,{'result':result,'trader_view':returned.get('trader_view',{}),'elapsed_ms':round((time.monotonic()-start)*1000,3)})
            return result
        except Exception as exc:
            with self.store.write(owner) as tx:
                tx.execute("UPDATE xj2_reply_job SET state='RETRY',lease_until=?,result_json=? WHERE job_id=? AND lease_token=?",(self.store.clock()+5,encoded({'error_type':type(exc).__name__}),job_id,token))
            self.service.diagnostics.record('reply',owner=owner,code='REPLY_RETRY',error=exc,entity=job_id)
            return {'state':'RETRY','facts_preserved':True}
    def run_once(self,*,force=False):
        if self.client is None:return {'analyzed':0,'decided':0}
        if self.aggregation is None and not force:raise InvalidFact('AGGREGATION_MEASUREMENT_REQUIRED')
        with self.store.db.transaction(write=False) as tx:
            threads=tx.execute('SELECT thread_id,owner_id FROM xj2_thread WHERE analyzed_revision<ingest_revision ORDER BY pending_since LIMIT 10').fetchall()
        analyzed=0
        for t in threads:
            if self.analyze_thread(t['owner_id'],t['thread_id'],force=force) is not None:analyzed+=1
        with self.store.db.transaction(write=False) as tx:
            members=tx.execute("SELECT p.member_id,g.owner_id FROM xj2_peer p JOIN xj2_group g ON g.group_id=p.group_id JOIN xj2_owner o ON o.owner_id=g.owner_id WHERE g.state='OPEN' AND g.send_allowed=1 AND p.send_allowed=1 AND o.send_allowed=1 ORDER BY p.member_id LIMIT 100").fetchall()
        decided=0
        for member in members:
            if self.decide_member(member['owner_id'],member['member_id']) is not None:decided+=1
        with self.store.db.transaction(write=False) as tx:
            owners=[r['owner_id'] for r in tx.execute('SELECT owner_id FROM xj2_owner').fetchall()]
        for owner in owners:self.commands.mark_unresponsive(owner)
        return {'analyzed':analyzed,'decided':decided}
