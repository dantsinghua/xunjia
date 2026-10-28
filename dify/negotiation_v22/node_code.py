"""Dependency-free Dify Code node: validate, never repair or erase model facts."""
import json
import re
import hashlib
from server.negotiation.slots import collect_candidates, literal_member_hints, window_reference_gaps
from dify.negotiation_v22.slot_wire import decode_slot_observation
from server.negotiation.reply_render import public_information, render_reply_plan, reply_requirements, requested_volume_signature


OUTPUT_CONTRACT_MARKER='XUNJIA_OUTPUT_CONTRACT_V1:'


class AnalysisAssociationError(ValueError):
    """Only the strict top-level analysis membership gate emits this type."""


def validate_analysis_association_feedback(feedback):
    if (not isinstance(feedback,dict) or set(feedback)!={'mode','attempt','max_attempts','reason_code'}
            or feedback.get('mode')!='OUTPUT_CONTRACT'
            or type(feedback.get('attempt')) is not int or feedback['attempt']!=1
            or type(feedback.get('max_attempts')) is not int or feedback['max_attempts']!=1
            or feedback.get('reason_code')!='DIFY_MODEL_CONTRACT_ANALYSIS_ASSOCIATION'):
        raise ValueError('Invalid analysis-association correction feedback')


def analysis_reference_maps(context):
    """Snapshot-local metadata only; never infer ownership or load a fact."""
    members=context.get('members',[]);member_ids=[row['member_id'] for row in members]
    if len(set(member_ids))!=len(member_ids):raise ValueError('Duplicate canonical member identity')
    rows=list(context.get('messages',[]))+list(context.get('correction_context',[]))
    for member in members:rows.extend(member.get('context',[]))
    for history in context.get('history_results',[]):rows.extend(history.get('messages',[]))
    records={};message_ids=[]
    def add(identity):
        if isinstance(identity,str) and identity and identity not in message_ids:message_ids.append(identity)
    for row in rows:
        identity=row['message_id'];add(identity)
        if identity in records:
            for field in ('text','direction','ingest_revision','kind','target_id','trade_day'):
                if field in row and field in records[identity] and row[field]!=records[identity][field]:
                    raise ValueError('Conflicting canonical message metadata')
        else:records[identity]=row
        add(row.get('target_id'))
    for member in members:
        for quote in member.get('quotes',{}).values():
            for refs in quote.get('evidence',{}).values():
                for identity in refs:add(identity)
        own=member.get('our_quote_json') or {}
        if isinstance(own,str):
            try:own=json.loads(own)
            except (ValueError,TypeError):own={}
        if isinstance(own,dict):add(own.get('message_id'))
    gaps=window_reference_gaps(context)
    for gap in gaps.values():
        for identity in gap.get('missing_ids',[]):add(identity)
    identity={'thread':context.get('thread_id'),'epoch':context.get('account_epoch'),
              'from':context.get('from_revision'),'through':context.get('through_revision'),
              'members':[{key:row.get(key) for key in ('member_id','bond_code','our_side','trade_day','version','control_version')} for row in members],
              'messages':[{key:row.get(key) for key in ('message_id','ingest_revision','text','direction','kind','target_id','trade_day')} for row in records.values()],
              'references':message_ids}
    namespace=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:10]
    reserved=set(message_ids)|set(member_ids)
    def assign(values,prefix):
        table={};number=1
        for value in values:
            while prefix+namespace+'_'+str(number) in reserved:number+=1
            table[value]=prefix+namespace+'_'+str(number);number+=1
        return table
    return {'m':assign(message_ids,'m'),'p':assign(member_ids,'p')}


def analysis_reference_metadata(value,context,*,encode,kind='result'):
    """Transform only enumerated analysis reference fields, never free data."""
    maps=analysis_reference_maps(context);reverse={key:{v:k for k,v in table.items()} for key,table in maps.items()}
    result=json.loads(json.dumps(value,ensure_ascii=False,allow_nan=False))
    def one(item,domain):
        if not isinstance(item,str):raise ValueError('Invalid structured analysis reference')
        if encode:return maps[domain].get(item,item)
        if item in maps[domain]:return item
        if item in reverse[domain]:return reverse[domain][item]
        if re.fullmatch(r'[mp][a-f0-9]{10}_[0-9]+|[mp][0-9]+',item):
            raise ValueError('Unknown snapshot or wrong-kind analysis reference')
        return item  # Canonical identifiers retain their original strict domain.
    def many(items,domain):
        if not isinstance(items,list):raise ValueError('Invalid structured analysis reference list')
        mapped=[one(item,domain) for item in items]
        if not encode and len(set(mapped))!=len(mapped):raise ValueError('Duplicate canonical analysis reference')
        return mapped
    def refs(row,key,domain):
        if key in row:row[key]=one(row[key],domain)
    def lists(row,key,domain):
        if key in row:row[key]=many(row[key],domain)
    def evidence(row,key):
        if key in row:
            if not isinstance(row[key],dict):raise ValueError('Invalid structured analysis evidence')
            row[key]={field:many(ids,'m') for field,ids in row[key].items()}
    def associations(items,key_domain,value_domain):
        if not isinstance(items,dict):raise ValueError('Invalid analysis association dictionary')
        mapped={}
        for key,ids in items.items():
            target=one(key,key_domain)
            if target in mapped:raise ValueError('Duplicate canonical analysis association')
            mapped[target]=many(ids,value_domain)
        return mapped
    if kind=='message_ids':return many(result,'m')
    if kind=='gaps':
        mapped={}
        for identity,row in result.items():
            lists(row,'source_ids','m');lists(row,'missing_ids','m');mapped[one(identity,'p')]=row
        return mapped
    if kind=='view':
        original_texts=[row.get('text','') for row in context.get('messages',[])+context.get('correction_context',[])]
        for member in context.get('members',[]):original_texts.extend(row.get('text','') for row in member.get('context',[]))
        mentioned={identity for identity in maps['m'] if any(identity in text for text in original_texts)}
        def message(row):
            original=row.get('message_id');refs(row,'message_id','m');refs(row,'target_id','m') if row.get('target_id') else None
            if original in mentioned:row['original_reference_id']=original
        for key in ('messages','correction_context','context'):
            for row in result.get(key,[]):message(row)
        for member in result.get('members',[]):
            refs(member,'member_id','p')
            for row in member.get('context',[]):message(row)
        for row in result.get('evidence_index',[]):
            refs(row,'member_id','p');lists(row,'loaded_message_ids','m');lists(row,'unloaded_reference_ids','m')
            if row.get('our_quote_message_id'):refs(row,'our_quote_message_id','m')
        for row in result.get('current_member_candidates',[]):refs(row,'member_id','p')
        for row in result.get('slot_candidates',[]):
            refs(row,'message_id','m');lists(row,'explicit_bond_members','p')
        if 'known_outbound_scopes' in result:result['known_outbound_scopes']=associations(result['known_outbound_scopes'],'m','p')
        if 'reference_gaps' in result:result['reference_gaps']=analysis_reference_metadata(result['reference_gaps'],context,encode=encode,kind='gaps')
        for row in result.get('history_results',[]):
            refs(row,'member_id','p');lists(row,'message_ids','m')
            for msg in row.get('messages',[]):message(msg)
        return result
    for key in ('scopes','unresolved'):
        if key in result:result[key]=associations(result[key],'m','p')
    for row in result.get('observations',[]):
        refs(row,'member_id','p');lists(row,'source_ids','m');evidence(row,'evidence')
        for segment in row.get('segments',[]):refs(segment,'message_id','m')
        for field in row.get('fields',[]):lists(field,'evidence','m')
    for row in result.get('confirmation_requests',[]):
        refs(row,'member_id','p');lists(row,'source_ids','m')
    for row in result.get('corrections',[]):
        refs(row,'message_id','m');lists(row,'member_ids','p');lists(row,'evidence','m')
    for row in result.get('history_requests',[]):
        refs(row,'member_id','p');lists(row,'message_ids','m')
    return result


def validate_output_contract_feedback(feedback):
    """Validate private rejected-model syntax only, never facts or permission."""
    if (not isinstance(feedback,dict) or set(feedback)!={'mode','attempt','max_attempts','reason_code','previous_output'}
            or feedback.get('mode')!='OUTPUT_CONTRACT'
            or type(feedback.get('attempt')) is not int or feedback['attempt']!=1
            or type(feedback.get('max_attempts')) is not int or feedback['max_attempts']!=1
            or feedback.get('reason_code') not in (
                'DIFY_MODEL_CONTRACT_SLOTS','DIFY_MODEL_CONTRACT_BOUNDARY','DIFY_MODEL_CONTRACT_OWN_QUOTE',
                'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE','DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT')):
        raise ValueError('Invalid output-contract correction feedback')
    previous=feedback['previous_output']
    if not isinstance(previous,dict) or set(previous)!={'schema_version','reply_plan'} or previous.get('schema_version')!='2.2':
        raise ValueError('Invalid previous model output')
    plan=previous['reply_plan']
    if (not isinstance(plan,dict) or not {'kind','text'}<=set(plan)
            or set(plan)-{'kind','text','quote_value','question','context_ref','acceptance'}
            or plan.get('kind') not in ('REPLY','RECONFIRM','ASK_VOLUME','CLARIFY','SOCIAL','HANDOFF_NOTICE')
            or not isinstance(plan.get('text'),str) or not 1<=len(plan['text'].strip())<=1500):
        raise ValueError('Invalid previous model output')
    if plan['kind']=='REPLY' and 'quote_value' not in plan:raise ValueError('Invalid previous model output')
    if plan['kind']=='HANDOFF_NOTICE':
        if plan.get('acceptance')!={'meaning':'ACCEPTED_CURRENT_QUOTE','source':'LATEST_INBOUND'} or 'quote_value' in plan:raise ValueError('Invalid previous model output')
    elif 'acceptance' in plan:raise ValueError('Invalid previous model output')
    if 'quote_value' in plan and (not isinstance(plan['quote_value'],str)
            or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,8})?',plan['quote_value'])):
        raise ValueError('Invalid previous model output')
    if ('question' in plan and plan['question'] not in ('NONE','NEGOTIATE','ASK_SETTLEMENT','ASK_CURRENT_QUOTE','ASK_MORE_VOLUME')
            or 'context_ref' in plan and (not isinstance(plan['context_ref'],str) or not re.fullmatch(r'[a-f0-9]{64}',plan['context_ref']))):
        raise ValueError('Invalid previous model output')
    try:size=len(json.dumps(previous,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf8'))
    except (TypeError,ValueError,UnicodeError):raise ValueError('Invalid previous model output') from None
    if size>6144:raise ValueError('Previous model output exceeds correction limit')


def compact_context(context):
    """Project model metadata; retain all business text, context rows and facts."""
    feedback=context.get('validation_feedback')
    ordinary_analysis_recovery=(context.get('stage')=='analysis' and isinstance(feedback,dict)
                                and feedback.get('mode')=='OUTPUT_CONTRACT')
    if ordinary_analysis_recovery:validate_analysis_association_feedback(feedback)
    text_only=isinstance(feedback,dict) and feedback.get('mode')=='TEXT_ONLY'
    production_reply=(context.get('stage')=='reply'
                      and context.get('context_policy_revision')=='latest_quote_confirmation_v1')
    def message(row):
        omitted={'content_hash','thread_id','event_id'}
        if production_reply:omitted.update(('message_id','native_id','target_id','source_seq'))
        return {key:value for key,value in row.items() if key not in omitted}
    def evidence_index(member, rows, our_quote):
        loaded = list(dict.fromkeys(row['message_id'] for row in rows))
        if isinstance(our_quote, str):
            try:
                our_quote = json.loads(our_quote)
            except (ValueError, TypeError):
                our_quote = None
        references = []
        our_id = our_quote.get('message_id') if isinstance(our_quote, dict) else None
        our_id = our_id if isinstance(our_id, str) and our_id else None
        if our_id is not None:
            references.append(our_id)
        for quote in member.get('quotes', {}).values():
            for refs in quote.get('evidence', {}).values():
                references.extend(refs)
        return {'member_id': member['member_id'], 'loaded_message_ids': loaded,
                'unloaded_reference_ids': [ref for ref in dict.fromkeys(references) if ref not in loaded],
                'our_quote_message_id': our_id, 'our_quote_loaded': our_id in loaded}

    # This is an index of supplied data, not new evidence or a decision. Keep
    # all source facts below so the model and the independent validator can
    # inspect the same complete raw messages and authoritative comparison.
    result = {}
    if context.get('stage') == 'reply' and not text_only:
        result['reply_requirements']=reply_requirements(context)
        result['public_information_candidates']=public_information(context)
    if context.get('stage') == 'analysis' and 'messages' in context:
        result['messages'] = context['messages']
    for key in ('tool_results', 'history_results', 'analysis_tool_results', 'analysis_tool_source'):
        if key in context:
            result[key] = context[key]
    if context.get('stage') == 'reply':
        recent = context.get('context', []) + context.get('unresolved_messages', [])
        current = None
        if recent and all(type(row.get('ingest_revision')) is int for row in recent):
            recent = sorted(recent, key=lambda row: row['ingest_revision'])
            if recent[-1].get('direction') == 'in' and not recent[-1].get('historical'):
                current = dict(recent[-1])
        result['current_inbound_message'] = current
        peer_side = {'BUY': 'ofr', 'SELL': 'bid'}.get(context.get('our_side'))
        peer_quote = context.get('quotes', {}).get(peer_side)
        result['current_counterparty_quote'] = dict(peer_quote) if isinstance(peer_quote, dict) else peer_quote
        result['current_member_lots'] = [lot for lot in context.get('group_summary', {}).get('whole_lots', [])
                                         if lot.get('member_id') == context.get('member_id')]
        if not production_reply:
            result['evidence_index'] = [evidence_index(context, context.get('context', []), context.get('our_quote'))]
    elif 'members' in context:
        common = context.get('messages', []) + context.get('correction_context', [])
        result['evidence_index'] = [evidence_index(member, common + member.get('context', []), member.get('our_quote_json'))
                                    for member in context['members']]
    result.update({key:value for key,value in context.items() if key != 'basis'})
    if (context.get('stage')=='reply' and isinstance(feedback,dict)
            and feedback.get('mode')=='OUTPUT_CONTRACT'):
        validate_output_contract_feedback(feedback)
        previous=feedback['previous_output']
        # Hide rejected prose only in this model view. Code and audit still
        # receive the full original feedback/context for every rejection.
        model_plan=dict(previous['reply_plan']);model_plan.pop('text')
        result['validation_feedback']={**feedback,'previous_output':{
            **previous,'reply_plan':model_plan}}
    if 'messages' in context:
        result['messages'] = [message(row) for row in context['messages']]
    if 'correction_context' in context:
        result['correction_context'] = [message(row) for row in context['correction_context']]
    if 'context' in context:
        result['context'] = [message(row) for row in context['context']]
    if production_reply:
        for key in ('evidence_index','member_id','thread_id','reconfirm_command_id','analysis_tool_source','handoff_evidence'):
            result.pop(key,None)
        if isinstance(result.get('current_inbound_message'),dict):
            result['current_inbound_message']=message(result['current_inbound_message'])
        if isinstance(result.get('unresolved_messages'),list):
            result['unresolved_messages']=[message(row) for row in result['unresolved_messages']]
        if isinstance(result.get('our_quote'),dict):
            result['our_quote']={key:value for key,value in result['our_quote'].items()
                                 if key not in ('command_id','message_id','native_id')}
    if 'members' in context:
        result['members'] = []
        for member in context['members']:
            copy = {key:value for key,value in member.items() if key not in ('group_id','thread_id','version','control_version')}
            copy['context'] = [message(row) for row in member.get('context',[])]
            result['members'].append(copy)
    if context.get('stage') == 'analysis' and (not context.get('validation_feedback') or ordinary_analysis_recovery):
        if context.get('context_policy_revision')=='latest_quote_confirmation_v1':
            result['reference_gaps']=window_reference_gaps(context)
        current_ids={row['message_id'] for row in context.get('messages',[])}
        numeric_roles={'YIELD','YIELD_SUFFIX','CHINABOND_BP','NET_PRICE','DIRTY_PRICE','QUANTITY'}
        # All spans are extracted and typed. Historical nonnumeric annotations
        # duplicate the intact raw history; only their numeric selections need
        # IDs in the model projection. Current semantic spans remain complete.
        candidates=collect_candidates(context)
        candidates.sort(key=lambda span:span['message_id'] not in current_ids)
        hints=literal_member_hints(context,candidates)
        result['current_member_candidates']=[{'member_id':member['member_id'], 'bond_code':member.get('bond_code'),
            'candidate_ids':[span['id'] for span in candidates if span['message_id'] in {row['message_id'] for row in context.get('messages',[]) if row.get('direction')=='in'}
                             and hints.get(span['id'])==[member['member_id']]]}
            for member in context.get('members',[])]
        result['slot_candidates'] = [{**{key:value for key,value in span.items() if key not in ('lexical','value')},
                                      'source_region':'CURRENT' if span['message_id'] in current_ids else 'HISTORY',
                                      **({'explicit_bond_members':hints[span['id']]} if span['id'] in hints else {})}
                                     for span in candidates
                                     if span['message_id'] in current_ids or numeric_roles.intersection(span['roles'])]
    if context.get('stage') == 'reply' and not text_only:
        result['reply_requirements']=reply_requirements(context)
        result['public_information_candidates']=public_information(context)
    if context.get('stage')=='analysis' and ('validation_feedback' not in context or ordinary_analysis_recovery):
        result=analysis_reference_metadata(result,context,encode=True,kind='view')
    return result


def _validate_result_strict(result, context, stage):
    if not isinstance(result, dict) or result.get('schema_version') != '2.2':
        raise ValueError('Expected V2.2 result')
    if not isinstance(context, dict) or context.get('schema_version') != '2.2' or context.get('stage') != stage:
        raise ValueError('Wrong stage context')
    history = result.get('history_requests', [])
    previous_history = context.get('history_results', [])
    previous_tools = context.get('tool_results', [])
    if not isinstance(history, list) or len(history) > 6:
        raise ValueError('Invalid bounded history requests')
    if not isinstance(previous_history, list) or not isinstance(previous_tools, list):
        raise ValueError('Invalid completed request context')
    requests = result.get('tool_requests', [])
    if not isinstance(requests, list) or len(requests) > 6:
        raise ValueError('Invalid bounded tool requests')
    for request in requests:
        if not isinstance(request, dict) or set(request) != {'name', 'arguments'}:
            raise ValueError('Invalid tool request')
        if request['name'] not in ('bond_info', 'similar_bonds', 'issuer_info', 'bond_calc') or not isinstance(request['arguments'], dict):
            raise ValueError('Only public bond tools are available')
    if history and requests:
        raise ValueError('History and public tools require separate intermediate rounds')
    if len(history) + len(requests) + len(previous_history) + len(previous_tools) > 6:
        raise ValueError('Shared history/public-tool request budget exceeded')
    completed = set()
    anchors = {}
    for previous in previous_history:
        mid, ids = previous['member_id'], previous['message_ids']
        completed.add((mid, tuple(sorted(ids))))
        anchors.setdefault(mid, set()).update(ids)
    if history:
        members = ({m['member_id'] for m in context.get('members', [])}
                   if stage == 'analysis' else {context.get('member_id')})
        for request in history:
            if not isinstance(request, dict) or set(request) != {'member_id', 'message_ids'}:
                raise ValueError('Invalid private history request fields')
            mid, ids = request['member_id'], request['message_ids']
            if not isinstance(mid, str) or not mid or mid not in members:
                raise ValueError('History member is outside the current stage')
            if not isinstance(ids, list) or not 1 <= len(ids) <= 30:
                raise ValueError('History request needs one to thirty explicit anchor IDs')
            if any(not isinstance(value, str) or not value.strip() or len(value) > 160 for value in ids):
                raise ValueError('Invalid explicit history anchor ID')
            if len(set(ids)) != len(ids):
                raise ValueError('History anchor IDs must be unique')
            signature = (mid, tuple(sorted(ids)))
            if signature in completed:
                raise ValueError('Repeated private history request')
            completed.add(signature)
            anchors.setdefault(mid, set()).update(ids)
            if len(anchors[mid]) > 30:
                raise ValueError('Per-member stage history budget exceeded')
    if requests or history:
        return result
    if stage == 'analysis':
        messages = {m['message_id']: m for m in context['messages']}
        members = {m['member_id']: m for m in context['members']}
        known = dict(messages)
        for member in members.values():
            known.update({m['message_id']: m for m in member.get('context', [])})
        known.update({m['message_id']: m for m in context.get('correction_context', [])})
        corrections = result.get('corrections', [])
        if not isinstance(corrections, list) or len(corrections) > 100:
            raise ValueError('Invalid corrections')
        for correction in corrections:
            if not isinstance(correction, dict) or set(correction) != {'message_id', 'member_ids', 'evidence'}:
                raise ValueError('Invalid correction fields')
            if correction['message_id'] not in known or not isinstance(correction['member_ids'], list) or not correction['member_ids'] or any(m not in members for m in correction['member_ids']):
                raise ValueError('Correction requires supplied same-thread anchors')
            if not isinstance(correction['evidence'], list) or not correction['evidence'] or any(r not in messages or messages[r].get('direction') != 'in' for r in correction['evidence']):
                raise ValueError('Correction requires current inbound clarification')
        scopes = result.get('scopes')
        unresolved = result.get('unresolved', {})
        observations = result.get('observations')
        if not isinstance(scopes, dict) or set(scopes) != set(messages):
            raise ValueError('Every raw message needs an explicit scope')
        if not isinstance(unresolved, dict) or set(unresolved) - set(messages):
            raise ValueError('Invalid unresolved associations')
        for mid, scope in scopes.items():
            unclear = unresolved.get(mid, [])
            if not isinstance(scope, list) or not isinstance(unclear, list) or any(m not in members for m in scope + unclear) or set(scope) & set(unclear):
                raise AnalysisAssociationError('Invalid member association')
        if not isinstance(observations, list) or len(observations) > 100:
            raise ValueError('Invalid observations')
        fields = {'price', 'volume_wan', 'assertion', 'validity', 'finality', 'confirmed_day'}
        for observation in observations:
            required = {'member_id', 'side', 'changes', 'evidence', 'segments', 'interpretation'}
            if isinstance(observation,dict) and 'settlement_binding' in observation:
                required.add('settlement_binding')
            if not isinstance(observation, dict) or set(observation) != required:
                raise ValueError('Observation needs facts, field evidence, segments and interpretation')
            if observation['member_id'] not in members or observation['side'] not in ('bid', 'ofr'):
                raise ValueError('Unknown counterparty side')
            changes, evidence = observation['changes'], observation['evidence']
            if not isinstance(changes, dict) or not changes or set(changes) - fields or not isinstance(evidence, dict) or set(evidence) != set(changes):
                raise ValueError('Every changed field needs evidence')
            for field, allowed in (('assertion', ('QUOTE','INDICATIVE','QUESTION','REPORTED')),
                                   ('validity', ('ACTIVE','WITHDRAWN','UNCONFIRMED')),
                                   ('finality', ('UNKNOWN','NEGOTIABLE','FINAL'))):
                if field in changes and changes[field] not in allowed:
                    raise ValueError('Invalid quotation '+field)
            for refs in evidence.values():
                if not isinstance(refs, list) or not 1 <= len(refs) <= 30 or any(r not in known for r in refs):
                    raise ValueError('Unknown field evidence')
                if not any(r in messages and messages[r].get('direction') == 'in' for r in refs):
                    raise ValueError('Field requires current inbound evidence')
            segments = observation['segments']
            if not isinstance(segments, list) or not segments:
                raise ValueError('Missing source segments')
            for segment in segments:
                if not isinstance(segment, dict) or set(segment) != {'message_id', 'text'} or segment['message_id'] not in known:
                    raise ValueError('Invalid source segment')
                if not isinstance(segment['text'], str) or not segment['text'] or segment['text'] not in known[segment['message_id']]['text']:
                    raise ValueError('Segment must be a literal continuous source substring')
            interpretation = observation['interpretation']
            if not isinstance(interpretation, dict) or set(interpretation) != {'quote_owner', 'intent', 'negotiation_space', 'reason'}:
                raise ValueError('Missing semantic interpretation')
            if interpretation['quote_owner'] != 'COUNTERPARTY' or interpretation['intent'] not in ('QUOTE', 'INDICATIVE', 'QUESTION', 'REPORTED', 'WITHDRAW','SETTLEMENT'):
                raise ValueError('Invalid quote ownership or intent')
            if interpretation['intent']=='SETTLEMENT' or 'settlement_binding' in observation:
                binding=observation.get('settlement_binding')
                if (interpretation['intent']!='SETTLEMENT' or not isinstance(binding,dict)
                        or set(binding)!={'command_id','quote_revision'}
                        or set(changes)!={'price','finality'} or changes['finality']!='UNKNOWN'):
                    raise ValueError('Settlement supplements cannot alter price, quantity or confirmation state')
            if interpretation['negotiation_space'] not in ('FINAL', 'NEGOTIABLE', 'UNKNOWN') or not isinstance(interpretation['reason'], str) or not interpretation['reason'].strip():
                raise ValueError('Missing contextual negotiation-space reason')
            if changes.get('finality') == 'FINAL' and interpretation['negotiation_space'] != 'FINAL':
                raise ValueError('Finality cannot bypass contextual interpretation')
    elif stage == 'reply':
        action, proposal = result.get('action'), result.get('proposal')
        if action not in ('SEND', 'WAIT', 'HANDOFF'):
            raise ValueError('Invalid reply action')
        if action != 'SEND':
            if proposal is not None:
                raise ValueError('WAIT/HANDOFF cannot contain public text')
        else:
            if not isinstance(proposal, dict) or set(proposal) - {'kind', 'text', 'our_price', 'render_plan'}:
                raise ValueError('Invalid proposal')
            if proposal.get('kind') not in ('REPLY', 'RECONFIRM', 'ASK_VOLUME', 'CLARIFY', 'SOCIAL', 'HANDOFF_NOTICE'):
                raise ValueError('Invalid proposal kind')
            if not isinstance(proposal.get('text'), str) or not 1 <= len(proposal['text']) <= 1500:
                raise ValueError('Missing bounded public text')
            if (proposal['kind'] == 'REPLY') != isinstance(proposal.get('our_price'), dict):
                raise ValueError('Only REPLY requires structured price')
    else:
        raise ValueError('Unknown stage')
    return result


def validate_result(result, context, stage):
    if (stage != 'analysis' or not isinstance(result, dict)
            or result.get('tool_requests') or result.get('history_requests') or not isinstance(result.get('observations'), list)):
        return _validate_result_strict(result, context, stage)
    # Validate the envelope once before isolating observation slots. Preserve
    # every index so Store.accepted/rejected refers to the model's actual item.
    _validate_result_strict({**result, 'observations': []}, context, stage)
    if len(result['observations']) > 100:
        raise ValueError('Invalid observations')
    checked = []
    for observation in result['observations']:
        try:
            _validate_result_strict({**result, 'observations': [observation]}, context, stage)
            checked.append(observation)
        except (ValueError, TypeError, KeyError) as exc:
            # Fixed reasons expose no raw peer text. Preserve the rejection;
            # never silently drop a bad item or claim an empty successful A.
            reason = str(exc) if isinstance(exc, ValueError) else 'Malformed observation structure'
            checked.append({'validation_error': reason})
    return {**result, 'observations': checked}


def parse_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate field')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Nonfinite JSON value')
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise ValueError('Invalid JSON length')
    raw = raw.strip()
    # Dify 0.15.3's OpenAI-compatible provider serializes reasoning_content
    # into this exact HTML prefix. Consume only that known transport wrapper;
    # arbitrary prose, multiple JSON objects and trailing text still fail.
    reasoning_prefix = '<details style="color:gray;background-color: #f8f8f8;padding: 8px;border-radius: 4px;" open> <summary> Thinking... </summary>'
    if raw.startswith(reasoning_prefix):
        end = raw.find('</details>', len(reasoning_prefix))
        if end < 0:
            raise ValueError('Incomplete provider reasoning wrapper')
        raw = raw[end + len('</details>'):].strip()
    if raw.startswith('```'):
        lines = raw.splitlines()
        if len(lines) < 3 or lines[-1] != '```':
            raise ValueError('Incomplete JSON fence')
        raw = '\n'.join(lines[1:-1])
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _repair_original(context, stage, mode):
    feedback = context.get('validation_feedback')
    if (context.get('schema_version') != '2.2' or context.get('stage') != stage
            or not isinstance(feedback, dict) or feedback.get('mode') != mode
            or type(feedback.get('attempt')) is not int or feedback['attempt'] != 1
            or type(feedback.get('max_attempts')) is not int or feedback['max_attempts'] != 1):
        raise ValueError('Patch requires the matching single trusted repair attempt')
    original = feedback.get('original_response')
    if not isinstance(original, dict) or original.get('schema_version') != '2.2':
        raise ValueError('Patch requires a trusted original response')
    if original.get('history_requests') or original.get('tool_requests'):
        raise ValueError('Repair cannot retain an intermediate request')
    return feedback, json.loads(json.dumps(original, ensure_ascii=False))


def _decode_evidence_repairs(result, context, stage):
    if stage != 'analysis' or set(result) != {'schema_version', 'evidence_repairs'}:
        raise ValueError('Evidence patch cannot contain other response fields')
    feedback, original = _repair_original(context, 'analysis', 'EVIDENCE_ONLY')
    observations, targets = original.get('observations'), feedback.get('targets')
    if not isinstance(observations, list) or len(observations) > 100 or not isinstance(targets, list) or not targets:
        raise ValueError('Missing bounded original observations or repair targets')
    allowed = {}
    for target in targets:
        if (not isinstance(target, dict) or type(target.get('index')) is not int
                or not 0 <= target['index'] < len(observations) or target['index'] in allowed):
            raise ValueError('Invalid trusted repair target')
        allowed[target['index']] = target
    patches = result['evidence_repairs']
    if not isinstance(patches, list) or not 1 <= len(patches) <= len(allowed):
        raise ValueError('Invalid bounded evidence patches')
    # Preserve every untouched observation, including an earlier rejection
    # sentinel. Revalidating it as fresh model output would rewrite its reason.
    _validate_result_strict({**original, 'observations': []}, context, 'analysis')
    seen = set()
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {'index', 'add_evidence', 'add_segments'}:
            raise ValueError('Evidence patch only permits explicit additions')
        index = patch['index']
        if type(index) is not int or index not in allowed or index in seen:
            raise ValueError('Evidence patch target is unauthorized or repeated')
        seen.add(index)
        observation, target = observations[index], allowed[index]
        if (not isinstance(observation, dict) or observation.get('member_id') != target.get('member_id')
                or observation.get('side') != target.get('side')):
            raise ValueError('Evidence patch target identity differs from the original')
        additions, segments = patch['add_evidence'], patch['add_segments']
        if not isinstance(additions, dict) or set(additions) - {'price'} or not isinstance(segments, list):
            raise ValueError('Evidence repair only adds price references and source segments')
        refs = additions.get('price', [])
        if (not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs)
                or len(refs) != len(set(refs))):
            raise ValueError('Invalid explicit added evidence IDs')
        if (not isinstance(observation.get('evidence'), dict)
                or not isinstance(observation['evidence'].get('price'), list)
                or not isinstance(observation.get('segments'), list)):
            raise ValueError('Original target is not a canonical price observation')
        added = False
        for ref in refs:
            if ref not in observation['evidence']['price']:
                observation['evidence']['price'].append(ref)
                added = True
        segment_keys = set()
        for segment in segments:
            if (not isinstance(segment, dict) or set(segment) != {'message_id', 'text'}
                    or not isinstance(segment['message_id'], str) or not isinstance(segment['text'], str)):
                raise ValueError('Invalid explicit added source segment')
            key = (segment['message_id'], segment['text'])
            if key in segment_keys:
                raise ValueError('Repeated added source segment')
            segment_keys.add(key)
            if segment not in observation['segments']:
                observation['segments'].append(segment)
                added = True
        if not added:
            raise ValueError('Evidence patch must add an explicit reference or segment')
        _validate_result_strict({**original, 'observations': [observation]}, context, 'analysis')
    return original


def _decode_text_repair(result, context, stage):
    if stage != 'reply' or set(result) != {'schema_version', 'text_repair'}:
        raise ValueError('Text patch cannot contain other response fields')
    feedback, original = _repair_original(context, 'reply', 'TEXT_ONLY')
    if feedback.get('reason') not in ('Public yield text differs from validated quote',
                                      'Reply direction differs from our authorized side',
                                      'Reply repeats the last verified native quotation'):
        raise ValueError('Text patch requires a permitted public-text rejection')
    proposal, patch = original.get('proposal'), result['text_repair']
    if original.get('action') != 'SEND' or not isinstance(proposal, dict) or proposal.get('kind') != 'REPLY':
        raise ValueError('Text patch requires the original SEND/REPLY candidate')
    if not isinstance(patch, dict) or set(patch) != {'text'}:
        raise ValueError('Text patch only permits text')
    if not isinstance(proposal.get('render_plan'),dict):
        raise ValueError('Text patch requires a source-bound natural reply plan')
    # Only the literal-slot template changes. Rebind against the frozen
    # application context; no naked-text compatibility branch remains.
    rebuilt=render_reply_plan(context,{**proposal['render_plan'],'text':patch['text']})['proposal']
    def nontext(value):
        value=dict(value);value.pop('text',None)
        if isinstance(value.get('render_plan'),dict):
            value['render_plan']={k:v for k,v in value['render_plan'].items() if k!='text'}
        return value
    if (nontext(rebuilt)!=nontext(proposal)
            or requested_volume_signature(rebuilt)!=requested_volume_signature(proposal)):
        raise ValueError('Text patch changed frozen economic fields')
    original['proposal']=rebuilt
    return validate_result(original, context, 'reply')


def _price_expression(value):
    """Map an explicit model-selected spelling; never infer from raw evidence."""
    if not isinstance(value, dict) or not {'basis', 'operation', 'bp'} & set(value):
        return value
    basis = value.get('basis')
    if basis == '中债估值':
        if (set(value) != {'basis', 'operation', 'bp'} or value['operation'] not in ('加', '减')
                or not isinstance(value['bp'], str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value['bp'])):
            raise ValueError('BP expression needs an explicit operation and unsigned decimal string')
        return {'kind': 'YIELD', 'value': ('-' if value['operation'] == '减' else '') + value['bp'],
                'notation': 'CHINABOND_BP'}
    kinds = {'收益率': 'YIELD', '净价': 'NET_PRICE', '全价': 'DIRTY_PRICE', '收益率尾数': 'YIELD',
             'NET_PRICE': 'NET_PRICE', 'DIRTY_PRICE': 'DIRTY_PRICE'}
    if not isinstance(basis, str) or basis not in kinds:
        raise ValueError('Unknown explicit price basis')
    allowed = {'basis', 'value'} | ({'settlement_date'} if kinds[basis] in ('NET_PRICE', 'DIRTY_PRICE') else set())
    if (set(value) - allowed or not isinstance(value.get('value'), str) or not value['value'].strip()
            or ('settlement_date' in value and (not isinstance(value['settlement_date'], str) or not value['settlement_date'].strip()))):
        raise ValueError('Price expression has mixed, missing or unsupported fields')
    if basis == '收益率尾数' and not re.fullmatch(r'[0-9]{2}', value['value']):
        raise ValueError('Yield suffix expression must preserve two digits')
    result = {'kind': kinds[basis], 'value': value['value'],
              'notation': 'YIELD_SUFFIX' if basis == '收益率尾数' else 'ABSOLUTE'}
    if 'settlement_date' in value:
        result['settlement_date'] = value['settlement_date']
    return result


def _has_price_expression(result):
    observations = result.get('observations')
    if not isinstance(observations, list):
        return False
    for observation in observations:
        fields = observation.get('fields') if isinstance(observation, dict) else None
        if not isinstance(fields, list):
            continue
        for field in fields:
            if (isinstance(field, dict) and field.get('field') == 'price' and isinstance(field.get('value'), dict)
                    and {'basis', 'operation', 'bp'} & set(field['value'])):
                return True
    return False


class WireFieldError(ValueError):
    def __init__(self, reason, path):
        super().__init__(reason)
        self.path = path


def _paired_observation(observation):
    if not isinstance(observation, dict) or 'fields' not in observation:
        return observation
    if 'changes' in observation or 'evidence' in observation:
        raise ValueError('Paired fields cannot mix with canonical changes or evidence')
    fields = observation['fields']
    if not isinstance(fields, list) or not 1 <= len(fields) <= 6:
        raise ValueError('Expected a bounded nonempty paired field list')
    changes, evidence = {}, {}
    for index, item in enumerate(fields):
        if not isinstance(item, dict) or set(item) != {'field', 'value', 'evidence'}:
            raise ValueError('Each paired field requires its own value and evidence')
        field = item['field']
        if not isinstance(field, str) or field not in ('price', 'volume_wan', 'assertion', 'validity', 'finality', 'confirmed_day') or field in changes:
            raise ValueError('Paired field name is unknown or repeated')
        try:
            changes[field] = _price_expression(item['value']) if field == 'price' else item['value']
        except ValueError as exc:
            raise WireFieldError(str(exc), 'fields['+str(index)+'].value') from exc
        evidence[field] = item['evidence']
    return {**{key: value for key, value in observation.items() if key != 'fields'},
            'changes': changes, 'evidence': evidence}


def _complete_known_outbound_scopes(result, context):
    """Fill only trusted native-send associations; missing inbound stays an error."""
    known=context.get('known_outbound_scopes',{})
    if not isinstance(known,dict):raise ValueError('Invalid application-owned outbound associations')
    messages={m['message_id']:m for m in context.get('messages',[])}
    members={m['member_id'] for m in context.get('members',[])}
    scopes=result.get('scopes')
    if not isinstance(scopes,dict):return result
    scopes=dict(scopes)
    for ref,expected in known.items():
        message=messages.get(ref)
        if (not message or message.get('direction')!='out' or message.get('historical')
                or not message.get('native_id') or not isinstance(expected,list) or not expected
                or any(not isinstance(mid,str) or mid not in members for mid in expected)
                or len(set(expected))!=len(expected)):
            raise ValueError('Only current known native outbound messages may have application-owned associations')
        if ref in scopes and (not isinstance(scopes[ref],list) or len(scopes[ref])!=len(expected)
                              or set(scopes[ref])!=set(expected)):
            raise ValueError('Model changed an application-owned outbound association')
        scopes[ref]=list(expected)
    return {**result,'scopes':scopes}


def decode_stage_result(result, context, stage):
    """Decode model representations, then retain the original contract gates."""
    if not isinstance(result, dict) or result.get('schema_version') != '2.2':
        raise ValueError('Expected V2.2 result')
    if not isinstance(context, dict) or context.get('schema_version') != '2.2' or context.get('stage') != stage:
        raise ValueError('Wrong stage context')
    feedback=context.get('validation_feedback')
    output_correction=isinstance(feedback,dict) and feedback.get('mode')=='OUTPUT_CONTRACT'
    ordinary_analysis_recovery=stage=='analysis' and output_correction
    if output_correction:
        if ordinary_analysis_recovery:validate_analysis_association_feedback(feedback)
        elif stage=='reply':validate_output_contract_feedback(feedback)
        else:raise ValueError('Invalid output-contract correction stage')
    ordinary_reply=stage=='reply' and ('validation_feedback' not in context or output_correction)
    if ordinary_analysis_recovery and not result.get('tool_requests') and not result.get('history_requests'):
        _validate_result_strict({**result,'observations':[]},context,stage)
        # Complete coverage is already required. Validate known native scopes
        # without adopting the helper's filled/reordered copy as a repair.
        _complete_known_outbound_scopes(result,context)
    if (stage=='analysis' and 'validation_feedback' not in context
            and not result.get('tool_requests') and not result.get('history_requests')):
        result=_complete_known_outbound_scopes(result,context)
    confirmations=result.get('confirmation_requests',[])
    if confirmations:
        if stage!='analysis' or ('validation_feedback' in context and not ordinary_analysis_recovery) or result.get('tool_requests') or result.get('history_requests'):
            raise ValueError('Reference confirmation requires an ordinary final analysis')
        if not isinstance(confirmations,list) or len(confirmations)>100:raise ValueError('Unbounded reference confirmations')
        _validate_result_strict({**result,'observations':[]},context,stage)
        members={m['member_id'] for m in context['members']}
        messages={m['message_id']:m for m in context['messages']}
        pending=[]
        for item in confirmations:
            if isinstance(item,dict):
                item=dict(item)
                if not ordinary_analysis_recovery and 'member_id' not in item and len(members)==1:item['member_id']=next(iter(members))
                inbound=[ref for ref,m in messages.items() if m.get('direction')=='in' and not m.get('historical')]
                if not ordinary_analysis_recovery and 'source_ids' not in item and len(inbound)==1:item['source_ids']=inbound
            if (not isinstance(item,dict) or set(item)!={'member_id','side','source_ids'}
                    or item['member_id'] not in members or item['side'] not in ('bid','ofr')
                    or not isinstance(item['source_ids'],list) or not item['source_ids']
                    or any(ref not in messages or messages[ref].get('direction')!='in'
                           or item['member_id'] not in result['scopes'].get(ref,[]) for ref in item['source_ids'])):
                raise ValueError('Reference confirmation needs current same-member inbound sources')
            pending.append({'member_id':item['member_id'],'side':item['side'],
                            'validation_error':'REFERENCE_CONFIRMATION_REQUIRED','validation_path':'confirmation_requests'})
        remaining={k:v for k,v in result.items() if k!='confirmation_requests'}
        decoded=decode_stage_result(remaining,context,stage)
        blocked={row['member_id'] for row in pending}
        if any(row.get('member_id') in blocked for row in decoded.get('observations',[])):
            raise ValueError('Unresolved reference cannot also commit price or confirmation for this member')
        return {**decoded,'observations':decoded.get('observations',[])+pending}
    if 'evidence_repairs' in result:
        return _decode_evidence_repairs(result, context, stage)
    if 'text_repair' in result:
        return _decode_text_repair(result, context, stage)
    if 'reply_plan' in result:
        if (not ordinary_reply or result.get('action') not in (None,'SEND')
                or set(result)-{'schema_version','action','reply_plan','trader_view'}):
            raise ValueError('Reply plan requires an ordinary SEND response without mixed fields')
        if output_correction:
            previous=feedback['previous_output']['reply_plan']
            current=result['reply_plan']
            if (isinstance(current,dict) and (current.get('kind')=='HANDOFF_NOTICE' or previous.get('kind')=='HANDOFF_NOTICE')
                    and {key:value for key,value in current.items() if key!='text'}!={key:value for key,value in previous.items() if key!='text'}):
                raise ValueError('Output-contract correction changed frozen reply fields')
            if (not isinstance(current,dict)
                    or ('context_ref' in current)!=('context_ref' in previous)
                    or current.get('context_ref')!=previous.get('context_ref')
                    or current.get('kind')=='REPLY' and (previous['kind']!='REPLY'
                       or current.get('question','NONE')!=previous.get('question','NONE')
                       or feedback['reason_code']!='DIFY_MODEL_CONTRACT_BOUNDARY'
                          and current.get('quote_value')!=previous['quote_value'])):
                raise ValueError('Output-contract correction changed frozen reply fields')
        proposal=render_reply_plan(context,result['reply_plan'])['proposal']
        return validate_result({**{k:v for k,v in result.items() if k!='reply_plan'},'action':'SEND','proposal':proposal},context,stage)
    if ordinary_reply and result.get('action')=='SEND':
        raise ValueError('Current reply flow requires a source-bound reply_plan')
    if _has_price_expression(result) and (stage != 'analysis' or ('validation_feedback' in context and not ordinary_analysis_recovery)
            or result.get('tool_requests') or result.get('history_requests')):
        raise ValueError('Price expressions require an ordinary final analysis response')
    # Full repair responses retain their old path, including backend frozen-
    # field rejection. Never extract a benign patch from an overreaching reply.
    if (context.get('validation_feedback') and not ordinary_analysis_recovery or stage != 'analysis' or result.get('tool_requests')
            or result.get('history_requests') or not isinstance(result.get('observations'), list)):
        return validate_result(result, context, stage)
    _validate_result_strict({**result, 'observations': []}, context, 'analysis')
    if len(result['observations']) > 100:
        raise ValueError('Invalid observations')
    checked = []
    for index, observation in enumerate(result['observations']):
        try:
            slot_wire = isinstance(observation,dict) and 'slots' in observation
            if ordinary_analysis_recovery and slot_wire and not {'member_id','source_ids'}<=set(observation):
                raise ValueError('Association correction needs explicit member and source IDs')
            decoded = decode_slot_observation(observation,context) if slot_wire else _paired_observation(observation)
            if slot_wire:
                _validate_result_strict({**result, 'observations':[decoded]}, context, stage)
                checked.append(decoded)
            else:
                checked.append(validate_result({**result, 'observations': [decoded]}, context, stage)['observations'][0])
        except (ValueError, TypeError, KeyError) as exc:
            rejection = {'validation_error': str(exc) if isinstance(exc, ValueError) else 'Malformed observation structure'}
            if isinstance(observation, dict) and (isinstance(exc, WireFieldError) or 'slots' in observation):
                mid=observation.get('member_id')
                if 'member_id' not in observation and 'meaning' in observation and len(context.get('members',[]))==1:
                    mid=context['members'][0]['member_id']
                rejection.update(member_id=mid, side=observation.get('side'),
                    validation_path='observations['+str(index)+'].'+(exc.path if isinstance(exc,WireFieldError) else 'slots'))
            checked.append(rejection)
    return {**result, 'observations': checked}


def decode_model_output(result,context,stage):
    """Carry authentic bounded Code rejections to the matching stage client."""
    feedback=context.get('validation_feedback') if isinstance(context,dict) else None
    ordinary_analysis_recovery=(stage=='analysis' and isinstance(feedback,dict)
                                and feedback.get('mode')=='OUTPUT_CONTRACT')
    if ordinary_analysis_recovery:validate_analysis_association_feedback(feedback)
    if stage=='analysis' and isinstance(context,dict) and ('validation_feedback' not in context or ordinary_analysis_recovery):
        result=analysis_reference_metadata(result,context,encode=False)
    previous=None
    if isinstance(result,dict) and result.get('schema_version')=='2.2' and isinstance(result.get('reply_plan'),dict):
        try:
            candidate=json.dumps({'schema_version':'2.2','reply_plan':result['reply_plan']},
                ensure_ascii=False,allow_nan=False,separators=(',',':'))
            if len(candidate.encode('utf8'))<=6144:previous=json.loads(candidate)
        except (ValueError,TypeError,UnicodeError):pass
    try:return decode_stage_result(result,context,stage)
    except AnalysisAssociationError as exc:
        if stage!='analysis' or feedback is not None and not ordinary_analysis_recovery:raise
        payload={'version':1,'origin':'analysis_associations',
                 'reason_code':'DIFY_MODEL_CONTRACT_ANALYSIS_ASSOCIATION'}
        raise ValueError(OUTPUT_CONTRACT_MARKER+json.dumps(payload,separators=(',',':'))) from exc
    except ValueError as exc:
        reasons={
            'Economic numbers and facts must use verified slots':'DIFY_MODEL_CONTRACT_SLOTS',
            'Settlement clarification must actually ask for settlement':'DIFY_MODEL_CONTRACT_SLOTS',
            'Cash quote basis prefix conflicts with verified field':'DIFY_MODEL_CONTRACT_SLOTS',
            'Ambiguous adjacent quote direction word':'DIFY_MODEL_CONTRACT_SLOTS',
            'Quote percent suffix conflicts with verified price kind':'DIFY_MODEL_CONTRACT_SLOTS',
            'Own quote slot repeats or conflicts with an adjacent direction':'DIFY_MODEL_CONTRACT_SLOTS',
            'Quote direction suffix conflicts with verified field':'DIFY_MODEL_CONTRACT_SLOTS',
            'Ambiguous adjacent quote percent component':'DIFY_MODEL_CONTRACT_SLOTS',
            'Ambiguous adjacent quantity unit word':'DIFY_MODEL_CONTRACT_SLOTS',
            'Quantity unit suffix conflicts with verified whole lot':'DIFY_MODEL_CONTRACT_SLOTS',
            'Equivalent fact-macro text exceeds 1500 characters':'DIFY_MODEL_CONTRACT_SLOTS',
            'Public reference is attributed to the wrong speaker':'DIFY_MODEL_CONTRACT_SLOTS',
            'Current quotation cannot be described as historical':'DIFY_MODEL_CONTRACT_SLOTS',
            'Selected quote is outside the authorized final boundary':'DIFY_MODEL_CONTRACT_BOUNDARY',
            'A quote reply must bind its own structured quotation':'DIFY_MODEL_CONTRACT_OWN_QUOTE',
            'Nonquote plan cannot insert an own price':'DIFY_MODEL_CONTRACT_OWN_QUOTE',
            'Public reply exposes private policy or internal context':'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE',
            'Quantity confirmation must explicitly refer to a quotation, not execution':'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE',
            'Reply promises execution or reserves a counterparty lot':'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE',
            'Counterparty reference is not an actual quotation':'DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT',
            'Unknown or unavailable public fact slot':'DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT',
        }
        feedback=context.get('validation_feedback') if isinstance(context,dict) else None
        if (stage!='reply' or str(exc) not in reasons or previous is None
                or feedback is not None and (not isinstance(feedback,dict) or feedback.get('mode')!='OUTPUT_CONTRACT')):
            raise
        correction={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,
                    'reason_code':reasons[str(exc)],'previous_output':previous}
        try:validate_output_contract_feedback(correction)
        except ValueError:raise exc
        payload={'version':1,'origin':'render_reply_plan','reason_code':correction['reason_code'],
                 'previous_output':previous}
        encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        if len(encoded.encode('utf8'))>8192:raise exc
        raise ValueError(OUTPUT_CONTRACT_MARKER+encoded) from exc
