"""Assemble selected, source-bound candidates; never accept model numeric copies."""
from server.negotiation.slots import collect_candidates
from server.negotiation.text_roles import settlement_literal
import re


def decode_slot_observation(observation, context):
    supplement=isinstance(observation.get('meaning'),dict) and observation['meaning'].get('intent')=='SETTLEMENT'
    if 'meaning' in observation:
        required={'side','slots','meaning'}
        if not required<=set(observation) or set(observation)-required-{'member_id','source_ids'}:
            raise ValueError('Meaning wire cannot mix a second state or interpretation copy')
        meaning=observation['meaning']
        if (not isinstance(meaning,dict)
                or set(meaning)!=({'intent','reason'} if supplement else {'intent','validity','negotiation_space','reason'})
                or not supplement and (meaning['intent'] not in ('QUOTE','INDICATIVE','QUESTION','REPORTED','WITHDRAW')
                    or meaning['validity'] not in ('ACTIVE','WITHDRAWN','UNCONFIRMED')
                    or meaning['negotiation_space'] not in ('UNKNOWN','NEGOTIABLE','FINAL'))
                or not isinstance(meaning['reason'],str) or not meaning['reason'].strip()):
            raise ValueError('Meaning needs one explicit intent, validity and negotiation space')
        if meaning['intent']=='WITHDRAW' and (meaning['validity']!='WITHDRAWN' or meaning['negotiation_space']!='UNKNOWN'):
            raise ValueError('Withdrawal meaning cannot confirm an active or final quote')
        members=context.get('members',[])
        inbound=[m['message_id'] for m in context.get('messages',[]) if m.get('direction')=='in' and not m.get('historical')]
        mid=observation.get('member_id')
        if 'member_id' not in observation:
            if len(members)!=1:raise ValueError('Multiple members require an explicit member ID')
            mid=members[0]['member_id']
        refs=observation.get('source_ids')
        if 'source_ids' not in observation:
            if len(inbound)!=1:raise ValueError('Multiple inbound messages require explicit source IDs')
            refs=inbound
        status={} if supplement else {'validity':meaning['validity'],'finality':meaning['negotiation_space']}
        if not supplement and meaning['intent']!='WITHDRAW':status['assertion']=meaning['intent']
        observation={'member_id':mid,'side':observation['side'],'source_ids':refs,'slots':observation['slots'],
                     'status':status,'interpretation':{'quote_owner':'COUNTERPARTY','intent':meaning['intent'],
                     'negotiation_space':'UNKNOWN' if supplement else meaning['negotiation_space'],'reason':meaning['reason']}}
    required = {'member_id','side','source_ids','slots','status','interpretation'}
    if set(observation) != required:
        raise ValueError('Slot observation requires identity, sources, selections, status and interpretation')
    member = next((m for m in context.get('members',[]) if m['member_id']==observation['member_id']), None)
    if member is None or observation['side'] not in ('bid','ofr'):
        raise ValueError('Slot selection has unknown member or side')
    current = {m['message_id']:m for m in context.get('messages',[])}
    known = {m['message_id']:m for m in member.get('context',[])}
    known.update({m['message_id']:m for m in context.get('correction_context',[])})
    known.update(current)
    refs = observation['source_ids']
    if (not isinstance(refs,list) or not 1<=len(refs)<=30 or len(set(refs))!=len(refs)
            or any(ref not in known for ref in refs)
            or not any(ref in current and current[ref].get('direction')=='in' for ref in refs)):
        raise ValueError('Slot facts need loaded same-member evidence and current inbound source')
    slots, status = observation['slots'], observation['status']
    if supplement and (not isinstance(slots,dict) or set(slots)!={'settlement'}
                       or any(ref not in current or current[ref].get('direction')!='in' for ref in refs)):
        raise ValueError('Settlement supplement only selects a current settlement candidate')
    if not isinstance(slots,dict) or set(slots)-{'price','volume_wan','settlement'}:
        raise ValueError('Slot fields only select price, quantity and cash settlement')
    if not isinstance(status,dict) or set(status)-{'assertion','validity','finality'}:
        raise ValueError('Unknown selected state field')
    indexed = {s['id']:s for s in collect_candidates(context)}
    changes = dict(status)
    evidence = {field:list(refs) for field in status}
    settlement = None
    for field, selection in slots.items():
        if isinstance(selection,str):
            candidate=indexed.get(selection)
            possible=({'QUANTITY'} if field=='volume_wan' else {'DATE','SETTLEMENT'} if field=='settlement'
                      else {'YIELD','YIELD_SUFFIX','CHINABOND_BP','NET_PRICE','DIRTY_PRICE'})
            roles=possible.intersection(candidate['roles']) if candidate else set()
            if len(roles)!=1:raise ValueError('Candidate type is missing or ambiguous for the selected field')
            selection={'candidate_id':selection,'role':next(iter(roles))}
        if not isinstance(selection,dict) or set(selection)!={'candidate_id','role'}:
            raise ValueError('Select a candidate ID and role, not a copied value')
        candidate = indexed.get(selection['candidate_id'])
        role = selection['role']
        if candidate is None or candidate['message_id'] not in known or role not in candidate['roles']:
            raise ValueError('Selected candidate or role is not supported by its source')
        if field=='settlement':
            message=current.get(candidate['message_id'])
            if (role not in ('DATE','SETTLEMENT') or message is None or message.get('direction')!='in'
                    or message.get('historical') or candidate['message_id'] not in refs):
                raise ValueError('Settlement requires a selected current inbound date or term')
            marks=sorted((m.start(),other['member_id']) for other in context.get('members',[])
                         if other.get('bond_code') for m in re.finditer(re.escape(other['bond_code']),message['text'],re.I))
            prior=[mid for start,mid in marks if start<=candidate['start']]
            if marks and (not prior or prior[-1]!=observation['member_id']):
                raise ValueError('Settlement source names another member')
            settlement=(settlement_literal(candidate['token']),candidate['message_id'])
            continue
        if field=='volume_wan':
            if role!='QUANTITY':raise ValueError('Volume requires a quantity candidate')
            changes[field]=candidate['value']
        else:
            if role not in ('YIELD','YIELD_SUFFIX','CHINABOND_BP','NET_PRICE','DIRTY_PRICE'):
                raise ValueError('Price requires a typed price candidate')
            changes[field]={'kind':role if role in ('NET_PRICE','DIRTY_PRICE') else 'YIELD',
                            'notation':role if role in ('YIELD_SUFFIX','CHINABOND_BP') else 'ABSOLUTE',
                            'value':candidate['value']}
        evidence[field]=list(dict.fromkeys(refs+[candidate['message_id']]))
    if settlement is not None:
        if supplement:
            question=member.get('settlement_question')
            previous=member.get('quotes',{}).get(observation['side'],{})
            price=previous.get('price') or {}
            if (not question or question.get('side')!=observation['side']
                    or question.get('quote_revision')!=previous.get('revision')
                    or price.get('kind') not in ('NET_PRICE','DIRTY_PRICE')):
                raise ValueError('Settlement supplement needs the actual sent question for this cash version')
            changes={'price':{'kind':price['kind'],'value':price['value']},'finality':'UNKNOWN'}
            evidence={'price':list(refs),'finality':list(refs)}
        if changes.get('price',{}).get('kind') not in ('NET_PRICE','DIRTY_PRICE'):
            raise ValueError('Settlement selection needs an explicit cash price selection')
        changes['price']['settlement_date']=settlement[0]
        evidence['price']=list(dict.fromkeys(evidence['price']+[settlement[1]]))
    if not changes:raise ValueError('Slot observation has no changes')
    if 'price' in changes and changes.get('assertion')=='QUOTE' and changes.get('validity')=='ACTIVE':
        incoming=[current[ref] for ref in refs if ref in current and current[ref].get('direction')=='in'
                  and not current[ref].get('historical')]
        # Receipt state can change while A runs. The fact transaction derives
        # cross-day confirmation from the current sent question and new reply.
        if member.get('reconfirm_day'):
            incoming=[]
        latest=max(incoming,key=lambda m:m.get('ingest_revision',0)) if incoming else None
        day=latest.get('trade_day') if latest else None
        if day:
            changes['confirmed_day']=day
            evidence['confirmed_day']=[latest['message_id']]
    segments=[]
    for ref in dict.fromkeys(r for values in evidence.values() for r in values):
        text=known[ref]['text']
        marks=[]
        for other in context.get('members',[]):
            code=other.get('bond_code')
            if code:
                marks.extend((m.start(),other['member_id']) for m in re.finditer(re.escape(code),text,re.I))
        marks.sort()
        owned=[(start,next((end for end,mid in marks[i+1:] if mid!=observation['member_id']),len(text)))
               for i,(start,mid) in enumerate(marks) if mid==observation['member_id']]
        if marks and not owned:raise ValueError('Selected source explicitly names another member')
        for start,end in owned or [(0,len(text))]:
            segments.append({'message_id':ref,'text':text[start:end]})
    result={'member_id':observation['member_id'],'side':observation['side'],
            'changes':changes,'evidence':evidence,'segments':segments,'interpretation':observation['interpretation']}
    if supplement:
        result['settlement_binding']={key:member['settlement_question'][key] for key in ('command_id','quote_revision')}
    return result
