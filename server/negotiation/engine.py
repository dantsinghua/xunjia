"""Two-stage runtime: receive -> facts transaction -> independently validated reply.
Network/model calls never run under database locks. Analysis scheduling is
independent of sending controls and outstanding native acknowledgements.
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
import math
import re
import time
from .prices import InvalidFact, Price, StaleDecision, number, resolve_yield_suffix, valuation_quote, volume_wan
from .store import encoded, new_id
from .validation import economics, token_side, validate_public_text, validate_observation
from .reply_render import render_reply_plan
from .slots import collect_candidates, window_reference_gaps
from .text_roles import numeric_spelling, settlement_literal
class Engine:
    def __init__(self,service,chatflows,tools,*,aggregation=None,stage_budget_seconds=150,
                 context_policy_revision='latest_quote_confirmation_v1'):
        if (type(stage_budget_seconds) not in (int,float) or not math.isfinite(stage_budget_seconds)
                or not 1<=stage_budget_seconds<=540):
            raise InvalidFact('Chatflow stage budget must be finite and between 1 and 540 seconds')
        self.service=service;self.store=service.store;self.commands=service.commands_core
        self.client=chatflows;self.tools=tools;self.aggregation=aggregation
        self.stage_budget_seconds=float(stage_budget_seconds)
        self.lease_seconds=math.ceil(stage_budget_seconds)+30
        if context_policy_revision not in ('latest_quote_confirmation_v1','explicit_anchors_v0'):
            raise InvalidFact('Unknown context policy revision')
        self.context_policy_revision=context_policy_revision
        self._reply_cursor=''  # Fair bounded pages even when 50 peers each discuss several bonds.
    @staticmethod
    def _merge_history(context, additions):
        targets={p['member_id']:p for p in context.get('members',[])}
        if 'member_id' in context:targets[context['member_id']]=context
        for mid,messages in additions.items():
            if mid not in targets:raise InvalidFact('Private history cannot change the stage member scope')
            target=targets[mid];current={m['message_id']:m for m in target.get('context',[])}
            current.update({m['message_id']:deepcopy(m) for m in messages})
            target['context']=sorted(current.values(),key=lambda m:(m['occurred_at'],m['ingest_revision']))

    @staticmethod
    def _analysis_response(result):
        fields=('schema_version','scopes','unresolved','observations','corrections','tool_requests','history_requests')
        return {key:deepcopy(result[key]) for key in fields if key in result}

    @staticmethod
    def _reply_response(result):
        fields=('schema_version','action','proposal','trader_view','tool_requests','history_requests')
        return {key:deepcopy(result[key]) for key in fields if key in result}

    @staticmethod
    def _reply_audit_value(value):
        # A malformed model value must remain inspectable without making its
        # ordinary validation rejection fail a second time during audit writes.
        try:
            text=encoded(value);text.encode('utf8')
            return json.loads(text)
        except (TypeError,ValueError,UnicodeError,OverflowError):
            try:representation=ascii(value)
            except Exception:representation='<unrepresentable '+type(value).__name__+'>'
            return {'json_safe':False,'response_type':type(value).__name__,
                    'representation':representation[:65536],'representation_truncated':len(representation)>65536}

    @staticmethod
    def _reply_without_text(result):
        frozen=deepcopy(result)
        if isinstance(frozen.get('proposal'),dict):frozen['proposal'].pop('text',None)
        return encoded(frozen)

    def _tool_call(self,method,context,*,owner=None,on_final=None,repair_audit=None):
        working=deepcopy(context);working['tool_results']=[];working['history_results']=[]
        working['context_policy_revision']=self.context_policy_revision
        executed_tools=[]
        def finished(value):
            if method=='analyze':
                value=dict(value)
                # This record comes only from this worker's actual invocations,
                # never from a field supplied by the model or a repair reply.
                value['_public_tool_results']=deepcopy(executed_tools)
            return value
        histories={};history_requests=set()
        used=0;started=time.monotonic();deadline=started+self.stage_budget_seconds
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise InvalidFact('CHATFLOW_STAGE_DEADLINE_EXCEEDED')
            working['_stage_budget_seconds']=remaining
            try:result=getattr(self.client,method)(working)
            except Exception:
                if method=='decide' and repair_audit is not None:
                    repair_audit['initial_elapsed_ms']=round((time.monotonic()-started)*1000,3)
                raise
            if repair_audit is not None:
                capture=self._analysis_response if method=='analyze' else self._reply_response
                value=capture(result) if isinstance(result,dict) else deepcopy(result)
                repair_audit['initial_response']=value if method=='analyze' else self._reply_audit_value(value)
                repair_audit['initial_elapsed_ms']=round((time.monotonic()-started)*1000,3)
            if time.monotonic()>=deadline:raise InvalidFact('CHATFLOW_STAGE_DEADLINE_EXCEEDED')
            if not isinstance(result,dict) or result.get('schema_version')!='2.2':raise InvalidFact('Expected V2.2 Chatflow stage result')
            requests=result.get('tool_requests',[])
            private=result.get('history_requests',[])
            if requests and private:raise InvalidFact('Use separate public-tool and private-history rounds')
            if private and self.context_policy_revision=='latest_quote_confirmation_v1':
                members={p['member_id']:p for p in context.get('members',[])} if method=='analyze' else {context['member_id']:context}
                if not isinstance(private,list) or not 1<=len(private)<=6:raise InvalidFact('Invalid bounded reference request')
                for request in private:
                    if (owner is None or not isinstance(request,dict) or set(request)!={'member_id','message_ids'}
                            or request['member_id'] not in members or not isinstance(request['message_ids'],list)
                            or not 1<=len(request['message_ids'])<=30
                            or any(not isinstance(ref,str) or not 1<=len(ref)<=160 for ref in request['message_ids'])):
                        raise InvalidFact('Invalid scoped reference request')
                # A legacy model may still ask to fetch. The current policy
                # records the missing reference without reading older history
                # or spending another remote model round.
                if method=='analyze':
                    mids={request['member_id'] for request in private}
                    scopes={message['message_id']:[mid for mid,p in members.items()
                            if p.get('bond_code','') and p['bond_code'] in message['text']]
                            for message in context['messages']}
                    for message in context['messages']:
                        if message.get('direction')=='in' and not scopes[message['message_id']]:
                            scopes[message['message_id']]=sorted(mids)
                    result={'schema_version':'2.2','scopes':scopes,'unresolved':{},'observations':[
                        {'member_id':mid,'side':'ofr' if members[mid]['our_side']=='BUY' else 'bid',
                         'validation_error':'REFERENCE_CONFIRMATION_REQUIRED'} for mid in sorted(mids)]}
                else:
                    plan={'kind':'CLARIFY','question':'ASK_CURRENT_QUOTE'}
                    result={'schema_version':'2.2','action':'SEND','proposal':render_reply_plan(context,plan)['proposal']}
                requests=[];private=[]
            if not requests and not private:
                result=dict(result);result['_history_context']=deepcopy(histories)
                result['_public_tool_results']=deepcopy(executed_tools)
                if on_final is None:return finished(result)
                baseline,feedback=on_final(result)
                repair_audit['initial_elapsed_ms']=round((time.monotonic()-started)*1000,3)
                # No baseline may be committed unless its first validation
                # completed inside the same model-stage budget.
                if time.monotonic()>=deadline:raise InvalidFact('CHATFLOW_STAGE_DEADLINE_EXCEEDED')
                if feedback is None:return finished(baseline)
                repair_started=time.monotonic()
                try:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise InvalidFact('CHATFLOW_STAGE_DEADLINE_EXCEEDED')
                    working['_stage_budget_seconds']=remaining
                    working['validation_feedback']=deepcopy(feedback)
                    repair_audit['attempted']=True
                    candidate=getattr(self.client,method)(deepcopy(working))
                    repair_audit['repair_response']=deepcopy(candidate) if method=='analyze' else self._reply_audit_value(candidate)
                    if time.monotonic()>=deadline:raise InvalidFact('CHATFLOW_STAGE_DEADLINE_EXCEEDED')
                    if not isinstance(candidate,dict) or candidate.get('schema_version')!='2.2':
                        raise InvalidFact('Expected V2.2 evidence repair result')
                    if candidate.get('tool_requests') or candidate.get('history_requests'):
                        raise InvalidFact('Evidence repair cannot request tools or private history')
                    repaired,_=on_final(candidate,repair=True)
                    if time.monotonic()>=deadline:raise InvalidFact('CHATFLOW_STAGE_DEADLINE_EXCEEDED')
                    repair_audit['succeeded']=True
                    return finished(repaired)
                except Exception as exc:
                    repair_audit['fallback']=True
                    repair_audit['repair_error']=type(exc).__name__+': '+str(exc)
                    return finished(baseline)
                finally:
                    repair_audit['repair_elapsed_ms']=round((time.monotonic()-repair_started)*1000,3)
            batch=private or requests
            if not isinstance(batch,list) or not 1<=len(batch)<=6 or used+len(batch)>6:raise InvalidFact('Chatflow exceeded the shared six-request budget')
            for request in private:
                if owner is None or not isinstance(request,dict) or set(request)!={'member_id','message_ids'}:
                    raise InvalidFact('Invalid private history request')
                mid=request['member_id'];ids=request['message_ids']
                members={p['member_id'] for p in context.get('members',[])} if method=='analyze' else {context['member_id']}
                if mid not in members:raise InvalidFact('History member is outside the current stage')
                if (not isinstance(ids,list) or not 1<=len(ids)<=30 or
                    any(not isinstance(ref,str) or not 1<=len(ref)<=160 for ref in ids) or len(set(ids))!=len(ids)):
                    raise InvalidFact('Private history requires one to thirty unique explicit message IDs')
                key=(mid,tuple(sorted(ids)))
                if key in history_requests:raise InvalidFact('Repeated private history request')
                old={m['message_id'] for m in histories.get(mid,[])}
                if len(old|set(ids))>30:raise InvalidFact('Private history exceeds thirty anchors for one member')
                through=context['through_revision'] if method=='analyze' else context['basis']['observed_watermark']
                rows=self.store.context(owner,mid,anchors=ids,through_revision=through)
                indexed={m['message_id']:m for m in rows}
                messages=[indexed[ref] for ref in ids]
                merged={m['message_id']:m for m in histories.get(mid,[])};merged.update({m['message_id']:m for m in messages})
                histories[mid]=list(merged.values());history_requests.add(key)
                self._merge_history(working,{mid:messages})
                working['history_results'].append({'member_id':mid,'message_ids':list(ids),'messages':messages})
                used+=1
            for request in requests:
                if not isinstance(request,dict) or set(request)!={'name','arguments'}:raise InvalidFact('Invalid public tool request')
                response=self.tools.invoke(request['name'],request['arguments'])
                recorded={'request':deepcopy(request),'result':deepcopy(response)}
                executed_tools.append(recorded)
                working['tool_results'].append(deepcopy(recorded))
                used+=1
    @staticmethod
    def _cash_sources(snapshot,observation,member,aliases):
        refs=set(observation.get('evidence',{}).get('price',[]))
        current={m['message_id']:m for m in snapshot['messages'] if m.get('direction')=='in'
                 and not m.get('historical') and m['message_id'] in refs}
        regions={}
        for ref,message in current.items():
            marks=[]
            for alias,mids in aliases.items():
                if len(mids)==1:
                    marks.extend((m.start(),next(iter(mids))) for m in re.finditer(
                        r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',message['text'],re.I))
            marks.sort()
            regions[ref]=[(start,next((end for end,other in marks[index+1:] if other!=mid),len(message['text'])))
                          for index,(start,mid) in enumerate(marks) if mid==member['member_id']] if marks else [(0,len(message['text']))]
        return current,regions

    def _cash_settlement(self,snapshot,observation,member,requested,aliases):
        """Bind a cash settlement to this member's current inbound evidence.

        T+1 is resolved only by the configured trading calendar. This runs
        before Price construction; unresolved terms never enter saved facts.
        """
        if requested is None:return None
        if not isinstance(requested,str):raise InvalidFact('Invalid cash settlement')
        requested=settlement_literal(requested)
        current,regions=self._cash_sources(snapshot,observation,member,aliases)
        dates=set()
        for candidate in collect_candidates(snapshot):
            message=current.get(candidate['message_id'])
            if (message is None
                    or not {'DATE','SETTLEMENT'}.intersection(candidate['roles'])):continue
            if not any(left<=candidate['start'] and candidate['end']<=right
                       for left,right in regions[candidate['message_id']]):continue
            text=message['text']
            prefix=re.split(r'[,，;；。\n]',text[:candidate['start']])[-1]
            suffix=re.split(r'[,，;；。\n]',text[candidate['end']:],maxsplit=1)[0]
            # A selected date is not sufficient: explicit denial, a question,
            # or a differently labelled business date is not confirmation.
            if (re.search(r'[?？]',prefix+suffix) or re.search(r'(?:吗|么)\s*$',suffix)
                    or re.search(r'(?:不是|不按|不要|不能|不是按|是不是|是否|不确定|未确定)\s*$',prefix)
                    or re.search(r'(?:评级|到期|起息|兑付|发行|上市)(?:日期|日)?\s*(?:为|是|[:：])?\s*$',prefix)):
                continue
            literal=settlement_literal(candidate['token'])
            if requested.startswith('T+') and literal!=requested:continue
            if literal=='T+0':resolved=message.get('trade_day')
            elif literal=='T+1':
                resolve=getattr(self.service.calendar,'settlement_date',None)
                try:resolved=resolve(message['trade_day'],'T+1') if callable(resolve) else None
                except Exception:resolved=None  # Missing calendar retains an incomparable cash quote.
            else:resolved=literal
            if resolved and (requested.startswith('T+') or requested==resolved):dates.add(resolved)
        return next(iter(dates)) if len(dates)==1 else None

    def _cash_conversion(self,snapshot,observation,member):
        price=observation['changes'].get('price')
        if not price or price['kind']=='YIELD' or not price.get('settlement_date'):return None
        # _normalize resolved and validated current counterpart evidence before
        # Price construction. Calculation cannot supply a missing settlement.
        settlement=price['settlement_date']
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
    @staticmethod
    def _uncovered_quotes(snapshot,observations,aliases):
        """Reject an unprocessed explicit fragment; never synthesize its facts.

        This deliberately recognizes direct quote syntax only. A preceding
        question, third-party attribution or reference to our price remains
        A's semantic task. A trailing question does not erase a direct quote
        fragment; any valid current classification can cover it, including
        QUESTION/REPORTED and an update of only the fields that changed.
        """
        current={message['message_id']:message['ingest_revision'] for message in snapshot['messages']
                 if message.get('direction')=='in' and not message.get('historical')}
        covered={};invalid=set()
        for observation in observations:
            mid,side=observation.get('member_id'),observation.get('side')
            if 'validation_error' in observation:
                invalid.add((mid,side));continue
            for refs in observation.get('evidence',{}).values():
                for ref in refs:
                    if ref in current:covered[mid,side]=max(covered.get((mid,side),-1),current[ref])
        direct_lead=re.compile(r'(?:[\s:：，,]|我(?:们|这边)?|本方(?:这边)?|现在|目前|'
                               r'报价|报|净价|全价|收益率|最终(?:价)?|可(?:以)?|'
                               r'收|出|买|卖|bid|ofr|offer)*',re.I)
        missing={}
        for message in snapshot['messages']:
            if message.get('direction')!='in' or message.get('historical') or message.get('kind')=='retract':continue
            text=message['text'];anchors=[]
            for alias,mids in aliases.items():
                if len(mids)!=1:continue
                for match in re.finditer(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',text,re.I):
                    anchors.append((match.start(),match.end(),next(iter(mids))))
            # Prefer the longest matching spelling at an overlapping anchor.
            unique=[]
            for start,end,mid in sorted(set(anchors),key=lambda hit:(hit[0],-hit[1])):
                if unique and start<unique[-1][1]:continue
                unique.append((start,end,mid))
            for index,(start,end,mid) in enumerate(unique):
                stop=unique[index+1][0] if index+1<len(unique) else len(text)
                clause=max((text.rfind(sep,0,start) for sep in ';；。\n'),default=-1)+1
                # Keep attribution before the bond anchor, but not a prior
                # bond's clause. Never erase '听说别家有' into a direct quote.
                prefix=text[clause:start] if index==0 or unique[index-1][0]<clause else ''
                fragment=prefix+text[end:stop]
                prices,_=economics(fragment,bond_codes=tuple(aliases))
                # A comma cannot discard attribution before the first price.
                if not prices or not direct_lead.fullmatch(fragment[:prices[0][1]]):continue
                for _value,left,right in prices:
                    side=token_side(fragment,left,right)
                    if side is None:continue
                    lead=re.split(r'[，,;；。\n/]',fragment[:left])[-1]
                    if not direct_lead.fullmatch(lead):continue
                    # A later classified update can supersede an earlier quote
                    # in the same aggregation batch; older evidence cannot
                    # cover a newly arrived, still unprocessed quote.
                    if (covered.get((mid,side),-1)>=message['ingest_revision'] or (mid,side) in invalid
                            or (mid,None) in invalid):continue
                    missing.setdefault((mid,side),[]).append(message['message_id'])
        return [{'member_id':mid,'side':side,'affected_members':[mid],
                 'validation_error':'Explicit current quote lacks a corresponding observation',
                 'message_ids':list(dict.fromkeys(refs))} for (mid,side),refs in missing.items()]

    def _normalize(self,snapshot,result,check_coverage=True):
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
        missing_references=set()
        if getattr(self,'context_policy_revision',None)=='latest_quote_confirmation_v1':
            missing_references=set(window_reference_gaps(snapshot,scopes))
        for observation in observations:
            try:
                if not isinstance(observation,dict):raise InvalidFact("Invalid observation")
                if observation.get('member_id') in missing_references:raise InvalidFact('REFERENCE_CONFIRMATION_REQUIRED')
                if 'validation_error' in observation:raise InvalidFact(observation['validation_error'])
                member=members.get(observation.get('member_id'))
                if not member:raise InvalidFact("Unknown member")
                changes=observation.get('changes',{})
                if not isinstance(changes,dict):raise InvalidFact("Invalid changes")
                interpretation=observation.get('interpretation')
                if isinstance(interpretation,dict) and interpretation.get('intent')=='SETTLEMENT':
                    question=member.get('settlement_question')
                    self.store._settlement_sources(snapshot,observation,question)
                    previous=member['quotes'][observation['side']]['price']
                    raw=changes.get('price')
                    if (not isinstance(raw,dict) or raw.get('kind') not in ('NET_PRICE','DIRTY_PRICE')
                            or (raw.get('kind'),raw.get('value'))!=(previous['kind'],previous['value'])
                            or set(raw)-{'kind','value','settlement_date'}):
                        raise InvalidFact('Settlement answer must retain the bound cash price')
                    settlement=self._cash_settlement(snapshot,observation,member,raw.get('settlement_date'),aliases)
                    if settlement is None:raise InvalidFact('Settlement date or published calendar is unavailable')
                    changes['price']=Price(raw['kind'],raw['value'],settlement_date=settlement).dump()
                    validated={key:observation[key] for key in ('member_id','side','changes','evidence','settlement_binding')}
                    calculation=self._cash_conversion(snapshot,validated,member)
                    if calculation:result['_verified_calculations'].append(calculation)
                    checked.append(validated)
                    continue
                if 'settlement_binding' in observation:
                    raise InvalidFact('Settlement binding requires the explicit supplement intent')
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
                    if raw.get('kind') in ('NET_PRICE','DIRTY_PRICE'):
                        current,regions=self._cash_sources(snapshot,observation,member,aliases)
                        candidates=[c for c in collect_candidates(snapshot) if c['message_id'] in current
                                    and any(left<=c['start'] and c['end']<=right for left,right in regions[c['message_id']])]
                        current_text=[current[ref]['text'][left:right] for ref,ranges in regions.items() for left,right in ranges]
                        if (any({'DATE','SETTLEMENT'}.intersection(c['roles']) for c in candidates)
                                and not any({'NET_PRICE','DIRTY_PRICE'}.intersection(c['roles']) for c in candidates)
                                and not any(re.search(r'(?:按|照|沿用|采用).{0,12}(?:价|报价|条件)',text) for text in current_text)):
                            raise InvalidFact('Settlement-only reply cannot replay or reconfirm a historical cash price')
                    if raw.get('kind') in ('NET_PRICE','DIRTY_PRICE') and raw.get('settlement_date') is not None:
                        raw['settlement_date']=self._cash_settlement(snapshot,observation,member,raw['settlement_date'],aliases)
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
                                **({'validation_path':observation['validation_path']} if isinstance(observation,dict) and 'validation_path' in observation else {}),
                                'affected_members':sorted(affected),'validation_error':str(exc)})
        for mid in missing_references-{row.get('member_id') for row in checked}:
            checked.append({'member_id':mid,'side':'ofr' if members[mid]['our_side']=='BUY' else 'bid',
                            'affected_members':[mid],'validation_error':'REFERENCE_CONFIRMATION_REQUIRED'})
        if check_coverage:checked.extend(self._uncovered_quotes(snapshot,checked,aliases))
        observations=checked
        return scopes,observations,unresolved

    @staticmethod
    def _evidence_repair_targets(snapshot,original,errors):
        """Only a well-formed, locatable price-source rejection is repairable."""
        members={p['member_id'] for p in snapshot['members']}
        raw_ids={m['message_id'] for m in snapshot['messages']}
        scopes=original.get('scopes');unresolved=original.get('unresolved',{})
        observations=original.get('observations');corrections=original.get('corrections',[])
        if (not isinstance(scopes,dict) or set(scopes)!=raw_ids or not isinstance(unresolved,dict)
                or set(unresolved)-raw_ids or not isinstance(observations,list) or len(observations)>100
                or not isinstance(corrections,list) or len(corrections)>100):return []
        for ref in raw_ids:
            assigned=scopes[ref];unclear=unresolved.get(ref,[])
            if (not isinstance(assigned,list) or not isinstance(unclear,list)
                    or len(assigned)+len(unclear)>100
                    or any(not isinstance(mid,str) or mid not in members for mid in assigned+unclear)
                    or set(assigned)&set(unclear)):return []
        for correction in corrections:
            if (not isinstance(correction,dict) or set(correction)!={'message_id','member_ids','evidence'}
                    or not isinstance(correction['member_ids'],list) or not isinstance(correction['evidence'],list)
                    or not correction['evidence']
                    or any(not isinstance(ref,str) or ref not in raw_ids for ref in correction['evidence'])
                    or any(not isinstance(mid,str) or mid not in members for mid in correction['member_ids'])):return []
        return [deepcopy(error) for error in errors
                if error['reason']=='Price is not supported by its classified raw evidence'
                and error['member_id'] in members and error['side'] in ('bid','ofr')
                and isinstance(observations[error['index']].get('changes',{}).get('price'),dict)]

    @staticmethod
    def _check_evidence_repair(snapshot,original,candidate,targets):
        if set(candidate)-set(original)-{'observations'}:
            raise InvalidFact('Evidence repair added fields outside the original A envelope')
        frozen=lambda value:{key:item for key,item in value.items() if key!='observations'}
        if encoded(frozen(candidate))!=encoded(frozen(original)):
            raise InvalidFact('Evidence repair changed the original A envelope')
        observations=candidate.get('observations')
        if not isinstance(observations,list) or len(observations)!=len(original['observations']):
            raise InvalidFact('Evidence repair changed observation count or order')
        indexes={target['index'] for target in targets}
        loaded={m['message_id'] for p in snapshot['members'] for m in p['context']}
        loaded.update(m['message_id'] for m in snapshot['messages'])
        for index,(old,new) in enumerate(zip(original['observations'],observations)):
            if index not in indexes:
                if encoded(new)!=encoded(old):raise InvalidFact('Evidence repair changed a frozen sibling')
                continue
            if not isinstance(new,dict):raise InvalidFact('Invalid repaired observation')
            economic=lambda value:{key:item for key,item in value.items() if key not in ('evidence','segments')}
            if encoded(economic(new))!=encoded(economic(old)):
                raise InvalidFact('Evidence repair changed observation identity, facts or interpretation')
            evidence=new.get('evidence')
            if not isinstance(evidence,dict):raise InvalidFact('Invalid repaired field evidence')
            for refs in evidence.values():
                if not isinstance(refs,list) or any(not isinstance(ref,str) or ref not in loaded for ref in refs):
                    raise InvalidFact('Evidence repair requires already loaded actual message IDs')
            for field,refs in old['evidence'].items():
                if not set(refs)<=set(evidence.get(field,[])):
                    raise InvalidFact('Evidence repair removed an original field reference')
            segments=new.get('segments',[])
            if not isinstance(segments,list) or any(not isinstance(segment,dict)
                    or not isinstance(segment.get('message_id'),str) or segment['message_id'] not in loaded
                    for segment in segments):
                raise InvalidFact('Evidence repair segments require already loaded actual message IDs')

    def analyze_thread(self,owner,thread_id,*,force=False):
        if self.client is None:return None
        job=self.store.claim_analysis(owner,thread_id,aggregation=self.aggregation,force=force,lease_seconds=self.lease_seconds,
            include_known_outbound_scopes=self.context_policy_revision=='latest_quote_confirmation_v1')
        if job is None:return None
        start=time.monotonic()
        snapshot=deepcopy(job['snapshot'])
        # Parser gets actual message context, never internal limit prices.
        for member in snapshot['members']:
            member['plan']={k:v for k,v in member.get('plan',{}).items() if k in ('name','code','our_side','trade_day')}
        repair_audit={'mode':'EVIDENCE_ONLY','claim_attempt':job['attempt'],
            'attempted':False,'succeeded':False,'fallback':False,'targets':[],
            'initial_response':None,'initial_errors':[],'repair_response':None,'repair_error':None,
            'initial_elapsed_ms':0,'repair_elapsed_ms':0}
        frozen={}
        def final_analysis(result,*,repair=False):
            if repair:
                self._check_evidence_repair(job['snapshot'],frozen['original'],result,repair_audit['targets'])
                subset=deepcopy(frozen['original'])
                subset['observations']=[deepcopy(result['observations'][t['index']]) for t in repair_audit['targets']]
                scopes,observations,unresolved=self._normalize(job['snapshot'],subset,False)
                if encoded([scopes,unresolved])!=encoded([frozen['baseline']['scopes'],frozen['baseline']['unresolved']]):
                    raise InvalidFact('Evidence repair changed normalized associations')
                for observation in observations:
                    if 'validation_error' in observation:raise InvalidFact(observation['validation_error'])
                prepared=deepcopy(frozen['baseline'])
                for target,observation in zip(repair_audit['targets'],observations):
                    prepared['observations'][target['index']]=observation
                prepared['_verified_calculations'].extend(subset['_verified_calculations'])
                return prepared,None
            original=self._analysis_response(result)
            self._merge_history(job['snapshot'],result['_history_context'])
            # Hydrate explicit correction anchors in the fixed snapshot only;
            # they are not an extra model/tool round or a new intake watermark.
            correction_context=[]
            with self.store.db.transaction(write=False) as tx:
                for correction in original.get('corrections',[]):
                    if not isinstance(correction,dict):raise InvalidFact('Invalid correction')
                    row=tx.execute('SELECT * FROM xj2_message WHERE message_id=? AND thread_id=? AND ingest_revision<=?',
                        (correction.get('message_id'),thread_id,job['snapshot']['through_revision'])).fetchone()
                    if not row:raise InvalidFact('Correction anchor is outside this snapshot')
                    correction_context.append(dict(row))
            job['snapshot']['correction_context']=correction_context
            normalized=deepcopy(original)
            scopes,observations,unresolved=self._normalize(job['snapshot'],normalized)
            repair_audit['initial_errors']=[{'index':index,'member_id':observation.get('member_id'),
                'side':observation.get('side'),'reason':observation['validation_error']}
                for index,observation in enumerate(observations) if 'validation_error' in observation]
            repair_audit['targets']=self._evidence_repair_targets(job['snapshot'],original,repair_audit['initial_errors'])
            prepared={'scopes':scopes,'observations':observations,'unresolved':unresolved,
                'corrections':original.get('corrections'),'_verified_calculations':normalized['_verified_calculations']}
            frozen.update(original=deepcopy(original),baseline=deepcopy(prepared))
            feedback={'mode':'EVIDENCE_ONLY','attempt':1,'max_attempts':1,
                'targets':deepcopy(repair_audit['targets']),'original_response':deepcopy(original)} if repair_audit['targets'] else None
            return prepared,feedback
        try:
            result=self._tool_call('analyze',snapshot,owner=owner,on_final=final_analysis,repair_audit=repair_audit)
            saved=self.store.commit_analysis(owner,job['analysis_id'],job['lease_token'],
                scopes=result['scopes'],observations=result['observations'],unresolved=result['unresolved'],
                corrections=result.get('corrections'),public_tool_results=result['_public_tool_results'],
                reconfirm_current_quotes=self.context_policy_revision=='latest_quote_confirmation_v1')
            if repair_audit['succeeded']:
                rejected={entry['index'] for entry in saved['rejected']}
                if rejected & {target['index'] for target in repair_audit['targets']}:
                    repair_audit['succeeded']=False
                    repair_audit['repair_error']='Repaired evidence was rejected by the final Store contract'
            saved['repair']=deepcopy(repair_audit)
            with self.store.write(owner) as tx:
                tx.execute("UPDATE xj2_analysis SET result_json=? WHERE analysis_id=? AND lease_token=? AND state='COMMITTED'",
                    (encoded(saved),job['analysis_id'],job['lease_token']))
                if result.get('_verified_calculations'):
                    self.store.audit(tx,owner,'cash_conversion_evidence',job['analysis_id'],{'calculations':result['_verified_calculations']})
            self.service.diagnostics.record('analysis',owner=owner,entity=job['analysis_id'])
            return saved
        except Exception as exc:
            repair_audit['succeeded']=False
            repair_audit['stage_error']=type(exc).__name__+': '+str(exc)
            try:self.store.fail_analysis(owner,job['analysis_id'],job['lease_token'],reason=type(exc).__name__,retry_after_seconds=min(60,2**min(job['attempt'],5)))
            except Exception:pass
            self.service.diagnostics.record('analysis',owner=owner,code='ANALYSIS_RETRY',error=exc,entity=job['analysis_id'])
            return {'facts_committed':False,'analysis_id':job['analysis_id'],'error':type(exc).__name__,'repair':deepcopy(repair_audit)}
        finally:
            with self.store.write(owner) as tx:
                self.store.audit(tx,owner,'analysis_evidence_repair',job['analysis_id'],repair_audit)
                self.store.audit(tx,owner,'analysis_duration',job['analysis_id'],{'elapsed_ms':round((time.monotonic()-start)*1000,3)})
    @staticmethod
    def _reply_boundary_yield(boundary):
        if not boundary or boundary['kind']!='YIELD':
            raise InvalidFact('Reply boundary requires a verified comparable yield')
        return number(boundary['value'])

    @staticmethod
    def _final_peer_yield(context,limit):
        peer=context['quotes'].get('ofr' if context['our_side']=='BUY' else 'bid',{})
        if (peer.get('assertion')=='QUOTE' and peer.get('validity')=='ACTIVE'
                and peer.get('finality')=='FINAL' and peer.get('confirmed_day')==context['trade_day']
                and peer.get('price')):
            value=Price.load(peer['price']).as_yield()
            if value is not None and (context['our_side']=='BUY' and value>=limit
                    or context['our_side']=='SELL' and value<=limit):return value
        return None

    @staticmethod
    def _reply_step_inputs(context):
        last=context['our_quote'].get('price')
        previous=Price.load(last).as_yield() if last else context['plan'].get('opening_yield_pct')
        step=context['plan'].get('step_bp')
        if previous is None or step is None:raise InvalidFact('Authorized initial price/step is unavailable')
        return number(previous),number(step),'LAST_SENT' if last else 'AUTHORIZED_OPENING'

    @classmethod
    def _quote_constraints(cls,context):
        """Project existing economic guards onto the 0.01 yield-percent grid.

        This pure view chooses neither a price nor an action and grants no send
        authority. Validation always uses the original server-owned inputs.
        """
        result={'status':'UNAVAILABLE','price_kind':'YIELD','tick_size':'0.01',
            'min_yield_pct':None,'max_yield_pct':None,'base_yield_pct':None,
            'base_source':None,'max_step_bp':None,'reason':None}
        try:
            side=context['our_side']
            if side not in ('BUY','SELL'):raise InvalidFact('Explicit BUY/SELL is required')
            limit=cls._reply_boundary_yield(context.get('boundary'))
            final=cls._final_peer_yield(context,limit)
            previous,step,source=cls._reply_step_inputs(context)
            result.update(base_yield_pct=format(previous,'f'),base_source=source,max_step_bp=format(step,'f'))
            delta=step/Decimal(100)
            lower,upper=previous-delta,previous+delta
            if side=='BUY':lower=max(lower,limit,final if final is not None else limit)
            else:upper=min(upper,limit,final if final is not None else limit)
            lower=lower.quantize(Decimal('0.01'),rounding=ROUND_CEILING)
            upper=upper.quantize(Decimal('0.01'),rounding=ROUND_FLOOR)
            if lower>upper:
                result.update(status='EMPTY',reason='No legal REPLY yield satisfies the current price constraints')
            else:
                result.update(status='AVAILABLE',min_yield_pct=format(lower,'.2f'),max_yield_pct=format(upper,'.2f'))
        except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError) as exc:
            result['reason']=str(exc) or 'Comparable reply pricing inputs are unavailable'
        return result

    def _reply_context(self,tx,owner,mid,basis):
        member=self.store._member(tx,owner,mid)
        group=self.store._group(tx,owner,member['group_id'])
        thread=self.store._thread(tx,owner,member['thread_id'])
        unresolved=[dict(r) for r in tx.execute("SELECT m.* FROM xj2_message m JOIN xj2_scope s ON s.message_id=m.message_id WHERE s.member_id=? AND s.relation IN ('UNRESOLVED','INVALID') AND m.ingest_revision<=? ORDER BY m.ingest_revision DESC LIMIT ?",(mid,thread['analyzed_revision'],self.store.settings.context_messages)).fetchall()]
        context={'member_id':mid,'thread_id':member['thread_id'],'basis':basis,
                'bond_code':group['bond_code'],'our_side':group['our_side'],'trade_day':group['trade_day'],
                'policy':json.loads(group['policy_json']),'boundary':json.loads(group['boundary_json']),
                'plan':json.loads(group['plan_json']),'group_summary':self.store._summary(tx,group),
                'quotes':{s:self.store._quote(tx,mid,s).dump() for s in ('bid','ofr')},
                'our_quote':json.loads(member['our_quote_json']),'reconfirm_day':member['reconfirm_day'],
                'reconfirm_command_id':member['reconfirm_command_id'],'unresolved_messages':unresolved,
                'response_state':member['response_state'],
                'context':self.store._context(tx,mid,thread['analyzed_revision'])}
        context['quote_constraints']=self._quote_constraints(context)
        context['analysis_tool_results']=[]
        context['analysis_tool_source']=None
        context['context_policy_revision']=self.context_policy_revision
        context['reference_confirmation_required']=False
        analysis=tx.execute("SELECT analysis_id,through_revision,snapshot_json,result_json FROM xj2_analysis WHERE thread_id=? AND through_revision=? AND state='COMMITTED'",
            (thread['thread_id'],thread['analyzed_revision'])).fetchone()
        if analysis:
            # The owned thread and exact watermark are already checked above.
            # A new member must not borrow a result from before it was present.
            snapshot=json.loads(analysis['snapshot_json'])
            if any(p['member_id']==mid for p in snapshot.get('members',[])):
                saved=json.loads(analysis['result_json'])
                context['reference_confirmation_required']=any(row.get('member_id')==mid and row.get('reason')=='REFERENCE_CONFIRMATION_REQUIRED'
                                                              for row in saved.get('rejected',[]))
                public_results=saved.get('public_tool_results',[])
                if public_results:
                    context['analysis_tool_results']=public_results
                    context['analysis_tool_source']={'analysis_id':analysis['analysis_id'],
                        'through_revision':analysis['through_revision']}
        return context
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
        if not isinstance(proposal,dict) or set(proposal)-{'kind','text','our_price','render_plan'}:raise InvalidFact('Unexpected reply fields')
        economic_text=proposal.get('text','')
        if 'render_plan' in proposal:
            try:rendered=render_reply_plan(context,proposal['render_plan'])
            except (ValueError,KeyError,TypeError,ArithmeticError) as exc:raise InvalidFact(str(exc)) from exc
            if rendered['proposal']!=proposal:raise InvalidFact('Public text or price differs from its source-bound plan')
            economic_text=rendered['economic_text']
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
            quoted,limit=price.as_yield(),self._reply_boundary_yield(boundary)
            if context['our_side']=='BUY' and quoted<limit or context['our_side']=='SELL' and quoted>limit:raise InvalidFact('Reply exceeds internal limit')
            peer_yield=self._final_peer_yield(context,limit)
            if peer_yield is not None and (context['our_side']=='BUY' and quoted<peer_yield
                    or context['our_side']=='SELL' and quoted>peer_yield):
                raise InvalidFact('Reply worsens this counterparty current final eligible price')
            previous,step,_source=self._reply_step_inputs(context)
            if abs(quoted-previous)*100>step:raise InvalidFact('Reply exceeds maximum step from actual sent price')
            text=economic_text
            advertised=re.findall(r'(?<![\d.+-])([+-]?\d+(?:\.\d+)?)\s*%',numeric_spelling(text))
            if price.kind=='YIELD':
                if not advertised or any(number(v.removeprefix('+'))!=quoted for v in advertised):raise InvalidFact('Public yield text differs from validated quote')
            elif advertised or ('净价' if price.kind=='NET_PRICE' else '全价') not in text:
                raise InvalidFact('Cash quote must explicitly state its price kind, not an unvalidated yield')
            body['our_price']=price.dump()
        validate_public_text(context,body,economic_text=economic_text)
        # The original response and its plan remain in the decision audit;
        # only validated native message fields cross the command boundary.
        body.pop('render_plan',None)
        if proposal.get('render_plan',{}).get('question')=='ASK_SETTLEMENT':
            side='ofr' if context['our_side']=='BUY' else 'bid'
            body['settlement_question']={'side':side,'quote_revision':context['quotes'][side]['revision']}
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
                tx.execute("UPDATE xj2_reply_job SET state='RUNNING',lease_token=?,lease_until=?,attempts=attempts+1 WHERE job_id=?",(token,self.store.clock()+self.lease_seconds,job_id))
            else:
                job_id=new_id()
                tx.execute("INSERT INTO xj2_reply_job(job_id,member_id,request_id,basis_json,state,lease_token,lease_until,attempts,result_json,created_at) VALUES(?,?,?,?,'RUNNING',?,?,1,'{}',?)",(job_id,mid,request,encoded(basis),token,self.store.clock()+self.lease_seconds,self.store.clock()))
        start=time.monotonic()
        repair_audit={'mode':'TEXT_ONLY','claim_attempt':old['attempts']+1 if old else 1,
            'attempted':False,'succeeded':False,'fallback':False,'initial_response':None,
            'initial_errors':[],'repair_response':None,'repair_error':None,
            'initial_elapsed_ms':0,'repair_elapsed_ms':0}
        frozen={}
        def final_reply(value,*,repair=False):
            if repair:
                if self._reply_without_text(value)!=frozen['key']:
                    raise InvalidFact('Text repair changed frozen nontext response fields')
                prepared=deepcopy(value)
                prepared['_history_context']=deepcopy(frozen['history'])
            else:
                self._merge_history(context,value['_history_context'])
                context['tool_results']=deepcopy(value.get('_public_tool_results',[]))
                original=self._reply_response(value)
                frozen.update(original=deepcopy(original),history=deepcopy(value['_history_context']))
                prepared=dict(value)
            if prepared.get('action')!='SEND':return prepared,None
            proposal=prepared.get('proposal');body=error=None
            try:body=self._validate_reply(context,proposal)
            except Exception as exc:error=exc
            try:
                proposal_key=encoded(proposal);proposal_key.encode('utf8')
                public_key=self._reply_without_text(self._reply_response(prepared));public_key.encode('utf8')
            except (TypeError,ValueError,UnicodeError,OverflowError):
                proposal_key=public_key=None
                error=error or InvalidFact('Reply response must be JSON serializable')
                body=None
            if repair and error is not None:raise error
            prepared['_checked_proposal']={'key':proposal_key,'body':deepcopy(body),'error':error}
            if repair:return prepared,None
            frozen['key']=public_key
            if error is not None:repair_audit['initial_errors']=[{'reason':str(error)}]
            eligible=(isinstance(error,InvalidFact) and str(error) in (
                'Public yield text differs from validated quote','Reply direction differs from our authorized side')
                and isinstance(proposal,dict) and proposal.get('kind')=='REPLY'
                and proposal.get('our_price') is not None and public_key is not None)
            feedback={'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,'reason':str(error),
                'original_response':deepcopy(frozen['original'])} if eligible else None
            return prepared,feedback
        try:
            returned=self._tool_call('decide',context,owner=owner,on_final=final_reply,repair_audit=repair_audit)
            proposal=returned.get('proposal');action=returned.get('action')
            with self.store.write(owner) as tx:
                lease=tx.execute('SELECT state,lease_token,lease_until FROM xj2_reply_job WHERE job_id=?',(job_id,)).fetchone()
                if lease['state']!='RUNNING' or lease['lease_token']!=token or lease['lease_until']<=self.store.clock():
                    repair_audit['succeeded']=False;repair_audit['stage_error']='STALE_LEASE'
                    return {'state':'STALE_LEASE','repair':deepcopy(repair_audit)}
            if action in ('WAIT','HANDOFF'):
                if proposal is not None:raise InvalidFact('WAIT/HANDOFF must not carry a message')
                with self.store.write(owner) as tx:
                    self.commands._require_reply_lease(tx,owner,mid,request,job_id,token)
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
                checked=returned['_checked_proposal']
                def validated(p):
                    if checked['key'] is None or encoded(p)!=checked['key']:
                        raise InvalidFact('Reply proposal changed after complete validation')
                    if checked['error'] is not None:raise checked['error']
                    return deepcopy(checked['body'])
                result=self.commands.propose(owner,mid,basis=basis,request_id=request,proposal=proposal,
                       validator=validated,expires_at=self.store.clock()+120,
                       reply_job_id=job_id,reply_lease_token=token)
            if repair_audit['succeeded'] and result['state']!='QUEUED':
                repair_audit['succeeded']=False
                repair_audit['repair_error']='Validated text was rejected by the final command gate'
            result['repair']=deepcopy(repair_audit)
            with self.store.write(owner) as tx:
                tx.execute("UPDATE xj2_reply_job SET state=?,lease_until=?,result_json=? WHERE job_id=? AND lease_token=?",('REJECTED' if result['state']=='REJECTED' else 'DONE',self.store.clock()+5,encoded(result),job_id,token))
                self.store.audit(tx,owner,'reply_stage_result',job_id,{'result':result,'trader_view':self._reply_audit_value(returned.get('trader_view',{})),'elapsed_ms':round((time.monotonic()-start)*1000,3)})
            return result
        except StaleDecision:
            repair_audit['succeeded']=False;repair_audit['stage_error']='STALE_LEASE'
            return {'state':'STALE_LEASE','repair':deepcopy(repair_audit)}
        except Exception as exc:
            repair_audit['succeeded']=False
            repair_audit['stage_error']=type(exc).__name__+': '+str(exc)
            result={'state':'RETRY','facts_preserved':True,'repair':deepcopy(repair_audit)}
            with self.store.write(owner) as tx:
                tx.execute("UPDATE xj2_reply_job SET state='RETRY',lease_until=?,result_json=? WHERE job_id=? AND lease_token=? AND state='RUNNING' AND lease_until>?",(self.store.clock()+5,encoded({'error_type':type(exc).__name__,'repair':repair_audit}),job_id,token,self.store.clock()))
            self.service.diagnostics.record('reply',owner=owner,code='REPLY_RETRY',error=exc,entity=job_id)
            return result
        finally:
            with self.store.write(owner) as tx:
                self.store.audit(tx,owner,'reply_text_repair',job_id,repair_audit)
    def run_once(self,*,force=False):
        if self.client is None:return {'analyzed':0,'decided':0}
        if self.aggregation is None and not force:raise InvalidFact('AGGREGATION_MEASUREMENT_REQUIRED')
        with self.store.db.transaction(write=False) as tx:
            candidates=tx.execute('SELECT * FROM xj2_thread WHERE analyzed_revision<ingest_revision ORDER BY pending_since').fetchall()
            threads=[]
            for candidate in candidates:
                if self.store._analysis_pending(tx,dict(candidate)):threads.append(candidate)
                if len(threads)==10:break
        analyzed=0
        for t in threads:
            if self.analyze_thread(t['owner_id'],t['thread_id'],force=force) is not None:analyzed+=1
        with self.store.db.transaction(write=False) as tx:
            base=("SELECT p.member_id,g.owner_id FROM xj2_peer p JOIN xj2_group g ON g.group_id=p.group_id "
                  "JOIN xj2_owner o ON o.owner_id=g.owner_id WHERE g.state='OPEN' AND g.archived=0 "
                  "AND g.send_allowed=1 AND p.send_allowed=1 AND o.send_allowed=1 ")
            cursor=getattr(self,'_reply_cursor','')
            members=list(tx.execute(base+"AND p.member_id>? ORDER BY p.member_id LIMIT 100",(cursor,)).fetchall())
            if len(members)<100 and cursor:
                members.extend(tx.execute(base+"AND p.member_id<=? ORDER BY p.member_id LIMIT ?",(cursor,100-len(members))).fetchall())
            if members:self._reply_cursor=members[-1]['member_id']
        decided=0
        for member in members:
            if self.decide_member(member['owner_id'],member['member_id']) is not None:decided+=1
        with self.store.db.transaction(write=False) as tx:
            owners=[r['owner_id'] for r in tx.execute('SELECT owner_id FROM xj2_owner').fetchall()]
        for owner in owners:self.commands.mark_unresponsive(owner)
        return {'analyzed':analyzed,'decided':decided}
