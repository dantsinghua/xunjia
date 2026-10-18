"""Dependency-free Dify Code node: validate, never repair or erase model facts."""
import json
import re
from server.negotiation.slots import collect_candidates, literal_member_hints, window_reference_gaps
from dify.negotiation_v22.slot_wire import decode_slot_observation
from server.negotiation.reply_render import public_information, render_reply_plan, reply_requirements, quote_grid


def compact_context(context):
    """Remove mechanical repetitions, never trim text, context rows or facts."""
    def message(row):
        return {key:value for key,value in row.items() if key not in ('content_hash','thread_id','event_id')}
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
    if context.get('stage') == 'reply' and not context.get('validation_feedback'):
        result['reply_requirements']=reply_requirements(context)
        result['quote_grid']=quote_grid(context)
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
        result['evidence_index'] = [evidence_index(context, context.get('context', []), context.get('our_quote'))]
    elif 'members' in context:
        common = context.get('messages', []) + context.get('correction_context', [])
        result['evidence_index'] = [evidence_index(member, common + member.get('context', []), member.get('our_quote_json'))
                                    for member in context['members']]
    result.update({key:value for key,value in context.items() if key != 'basis'})
    if 'messages' in context:
        result['messages'] = [message(row) for row in context['messages']]
    if 'correction_context' in context:
        result['correction_context'] = [message(row) for row in context['correction_context']]
    if 'context' in context:
        result['context'] = [message(row) for row in context['context']]
    if 'members' in context:
        result['members'] = []
        for member in context['members']:
            copy = {key:value for key,value in member.items() if key not in ('group_id','thread_id','version','control_version')}
            copy['context'] = [message(row) for row in member.get('context',[])]
            result['members'].append(copy)
    if context.get('stage') == 'analysis' and not context.get('validation_feedback'):
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
            'candidate_ids':[span['id'] for span in candidates if span['message_id'] in current_ids
                             and hints.get(span['id'])==[member['member_id']]]}
            for member in context.get('members',[])]
        result['slot_candidates'] = [{**{key:value for key,value in span.items() if key not in ('lexical','value')},
                                      'source_region':'CURRENT' if span['message_id'] in current_ids else 'HISTORY',
                                      **({'explicit_bond_members':hints[span['id']]} if span['id'] in hints else {})}
                                     for span in candidates
                                     if span['message_id'] in current_ids or numeric_roles.intersection(span['roles'])]
    if context.get('stage') == 'reply' and not context.get('validation_feedback'):
        result['reply_requirements']=reply_requirements(context)
        result['quote_grid']=quote_grid(context)
        result['public_information_candidates']=public_information(context)
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
                raise ValueError('Invalid member association')
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
            if proposal.get('kind') not in ('REPLY', 'RECONFIRM', 'ASK_VOLUME', 'CLARIFY', 'SOCIAL'):
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
                                      'Reply direction differs from our authorized side'):
        raise ValueError('Text patch requires a permitted public-text rejection')
    proposal, patch = original.get('proposal'), result['text_repair']
    if original.get('action') != 'SEND' or not isinstance(proposal, dict) or proposal.get('kind') != 'REPLY':
        raise ValueError('Text patch requires the original SEND/REPLY candidate')
    if not isinstance(patch, dict) or set(patch) != {'text'}:
        raise ValueError('Text patch only permits text')
    proposal['text'] = patch['text']
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


def _reply_parts(result):
    proposal = result['proposal']
    if (result.get('action') != 'SEND' or set(proposal) - {'kind', 'information_text', 'negotiation_text', 'our_price'}
            or not {'information_text', 'negotiation_text'} <= set(proposal)):
        raise ValueError('Reply parts require an ordinary SEND proposal without mixed text')
    parts = [proposal['information_text'], proposal['negotiation_text']]
    if any(not isinstance(part, str) for part in parts):
        raise ValueError('Reply parts must be strings')
    decoded = {key: value for key, value in proposal.items() if key not in ('information_text', 'negotiation_text')}
    decoded['text'] = '\n'.join(part for part in parts if part != '')
    return {**result, 'proposal': decoded}


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
    if (stage=='analysis' and 'validation_feedback' not in context
            and not result.get('tool_requests') and not result.get('history_requests')):
        result=_complete_known_outbound_scopes(result,context)
    confirmations=result.get('confirmation_requests',[])
    if confirmations:
        if stage!='analysis' or 'validation_feedback' in context or result.get('tool_requests') or result.get('history_requests'):
            raise ValueError('Reference confirmation requires an ordinary final analysis')
        if not isinstance(confirmations,list) or len(confirmations)>100:raise ValueError('Unbounded reference confirmations')
        _validate_result_strict({**result,'observations':[]},context,stage)
        members={m['member_id'] for m in context['members']}
        messages={m['message_id']:m for m in context['messages']}
        pending=[]
        for item in confirmations:
            if isinstance(item,dict):
                item=dict(item)
                if 'member_id' not in item and len(members)==1:item['member_id']=next(iter(members))
                inbound=[ref for ref,m in messages.items() if m.get('direction')=='in' and not m.get('historical')]
                if 'source_ids' not in item and len(inbound)==1:item['source_ids']=inbound
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
        if (stage!='reply' or 'validation_feedback' in context or result.get('action') not in (None,'SEND')
                or set(result)-{'schema_version','action','reply_plan','trader_view'}):
            raise ValueError('Reply plan requires an ordinary SEND response without mixed fields')
        proposal=render_reply_plan(context,result['reply_plan'])['proposal']
        return validate_result({**{k:v for k,v in result.items() if k!='reply_plan'},'action':'SEND','proposal':proposal},context,stage)
    if (stage=='reply' and context.get('context_policy_revision')=='latest_quote_confirmation_v1'
            and not context.get('validation_feedback') and result.get('action')=='SEND'):
        raise ValueError('Current reply flow requires a source-bound reply_plan')
    if _has_price_expression(result) and (stage != 'analysis' or 'validation_feedback' in context
            or result.get('tool_requests') or result.get('history_requests')):
        raise ValueError('Price expressions require an ordinary final analysis response')
    proposal = result.get('proposal')
    if isinstance(proposal, dict) and {'information_text', 'negotiation_text'} & set(proposal):
        if (stage != 'reply' or 'validation_feedback' in context
                or result.get('tool_requests') or result.get('history_requests')):
            raise ValueError('Reply parts require an ordinary final reply response')
        return validate_result(_reply_parts(result), context, stage)
    # Full repair responses retain their old path, including backend frozen-
    # field rejection. Never extract a benign patch from an overreaching reply.
    if (context.get('validation_feedback') or stage != 'analysis' or result.get('tool_requests')
            or result.get('history_requests') or not isinstance(result.get('observations'), list)):
        return validate_result(result, context, stage)
    _validate_result_strict({**result, 'observations': []}, context, 'analysis')
    if len(result['observations']) > 100:
        raise ValueError('Invalid observations')
    checked = []
    for index, observation in enumerate(result['observations']):
        try:
            slot_wire = isinstance(observation,dict) and 'slots' in observation
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
