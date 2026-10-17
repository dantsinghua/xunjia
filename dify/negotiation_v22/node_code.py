"""Dependency-free Dify Code node: validate, never repair or erase model facts."""
import json


def compact_context(context):
    """Remove mechanical repetitions, never trim text, context rows or facts."""
    def message(row):
        return {key:value for key,value in row.items() if key not in ('content_hash','thread_id','event_id')}
    result = {key:value for key,value in context.items() if key != 'basis'}
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
    return result


def _validate_result_strict(result, context, stage):
    if not isinstance(result, dict) or result.get('schema_version') != '2.2':
        raise ValueError('Expected V2.2 result')
    if not isinstance(context, dict) or context.get('schema_version') != '2.2' or context.get('stage') != stage:
        raise ValueError('Wrong stage context')
    requests = result.get('tool_requests', [])
    if not isinstance(requests, list) or len(requests) > 6:
        raise ValueError('Invalid bounded tool requests')
    for request in requests:
        if not isinstance(request, dict) or set(request) != {'name', 'arguments'}:
            raise ValueError('Invalid tool request')
        if request['name'] not in ('bond_info', 'similar_bonds', 'issuer_info', 'bond_calc') or not isinstance(request['arguments'], dict):
            raise ValueError('Only public bond tools are available')
    if requests:
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
            if interpretation['quote_owner'] != 'COUNTERPARTY' or interpretation['intent'] not in ('QUOTE', 'INDICATIVE', 'QUESTION', 'REPORTED', 'WITHDRAW'):
                raise ValueError('Invalid quote ownership or intent')
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
            if not isinstance(proposal, dict) or set(proposal) - {'kind', 'text', 'our_price'}:
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
            or result.get('tool_requests') or not isinstance(result.get('observations'), list)):
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
