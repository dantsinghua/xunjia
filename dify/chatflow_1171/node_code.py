"""Pure functions embedded in Dify Code nodes; no network or secret access."""
import json


def loads(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate key')
            value[key] = item
        return value
    def invalid(value):
        raise ValueError('nonfinite JSON')
    if not isinstance(text, str) or len(text) > 100000:
        raise ValueError('invalid or oversized JSON')
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('object required')
    return value


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def check_turn(value, trusted):
    keys = {'schema_version', 'turn_id', 'conversation_epoch', 'source_message_id', 'observations',
            'proposed_actions', 'public_message', 'trader_view', 'memory_patch', 'history_request', 'validation_ref'}
    if set(value) != keys or value['schema_version'] != '1':
        raise ValueError('invalid AgentTurn fields')
    if any(value[k] != trusted[k] for k in ('turn_id', 'conversation_epoch', 'source_message_id')):
        raise ValueError('untrusted turn identity')
    if type(value['conversation_epoch']) is not int:
        raise ValueError('invalid epoch')
    for key in ('observations', 'proposed_actions'):
        if not isinstance(value[key], list) or len(value[key]) > 50 or not all(isinstance(v, dict) for v in value[key]):
            raise ValueError('invalid array')
    if any(a.get('type') not in ('SOCIAL_REPLY', 'CLARIFY_BOND', 'ASK_QUOTE', 'COUNTER', 'WAIT', 'REQUEST_HANDOFF') for a in value['proposed_actions']):
        raise ValueError('invalid action')
    if not isinstance(value['trader_view'], dict) or any(value[k] is not None and not isinstance(value[k], dict) for k in ('memory_patch', 'history_request')):
        raise ValueError('invalid proposal object')
    return value


def context_request(turn_ref, schema_version):
    if schema_version != '1' or not isinstance(turn_ref, str) or not turn_ref or len(turn_ref) > 2048:
        raise ValueError('invalid context reference')
    return {'body': dumps({'turn_ref': turn_ref})}


def prepare_context(body, status_code, context_epoch, schema_version):
    response = loads(body)
    if status_code != 200 or response.get('ok') is not True or schema_version != '1':
        raise ValueError('context unavailable')
    context = response['context']
    if set(context) != {'turn_id', 'conversation_epoch', 'source_message_id', 'decision_context'}:
        raise ValueError('invalid context envelope')
    if type(context['conversation_epoch']) is not int or str(context['conversation_epoch']) != context_epoch:
        raise ValueError('stale context')
    if not all(isinstance(context[k], str) and context[k] for k in ('turn_id', 'source_message_id')):
        raise ValueError('invalid context identity')
    if not isinstance(context['decision_context'], dict):
        raise ValueError('invalid decision context')
    budget_ref = context['decision_context'].get('tool_budget_ref')
    if not isinstance(budget_ref, str) or not budget_ref or len(budget_ref) > 2048:
        raise ValueError('missing readonly tool budget reference')
    def reject_secrets(value):
        if isinstance(value, dict):
            if set(value) & {'turn_ref', 'service_capability', 'authorization', 'api_key', 'access_secret', 'security_token'}:
                raise ValueError('capability in model context')
            for child in value.values():
                reject_secrets(child)
        elif isinstance(value, list):
            for child in value:
                reject_secrets(child)
    reject_secrets(context)
    trusted = {k: context[k] for k in ('turn_id', 'conversation_epoch', 'source_message_id')}
    if len(dumps(context)) > 50000:
        raise ValueError('decision context budget exceeded')
    return {'agent_context': dumps(context), 'trusted': dumps(trusted), 'tool_budget_ref': budget_ref}


def parse_strict(text, trusted_json, second_pass):
    trusted = loads(trusted_json)
    proposal = check_turn(loads(text), trusted)
    if proposal['history_request'] is not None:
        if second_pass:
            raise ValueError('history budget exhausted')
        request = proposal['history_request']
        if set(request) - {'message_refs', 'keywords', 'date_range', 'reason'}:
            raise ValueError('invalid history request')
        for key in ('message_refs', 'keywords'):
            if key in request and (not isinstance(request[key], list) or len(request[key]) > 50 or not all(isinstance(v, str) and 0 < len(v) <= 200 for v in request[key])):
                raise ValueError('history bounds exceeded')
        proposal['proposed_actions'] = [{'type': 'WAIT'}]
    # Private model output never supplies final public prose or a validation ticket.
    proposal['public_message'] = None
    proposal['validation_ref'] = None
    return {'proposal': dumps(proposal)}


def parse_first(text, trusted_json):
    try:
        parsed = parse_strict(text, trusted_json, False)
        return {'valid': True, 'proposal': parsed['proposal']}
    except (ValueError, TypeError, KeyError):
        return {'valid': False, 'proposal': ''}


def parse_repaired(text, trusted_json):
    return parse_strict(text, trusted_json, False)


def parse_history(text, trusted_json):
    return parse_strict(text, trusted_json, True)


def history_check(proposal_json):
    proposal = loads(proposal_json)
    return {'needs_history': proposal['history_request'] is not None, 'proposal': proposal_json}


def passthrough(proposal_json):
    return {'proposal': proposal_json}


def history_request(turn_ref, proposal_json):
    proposal = loads(proposal_json)
    if not isinstance(proposal['history_request'], dict):
        raise ValueError('history request missing')
    return {'body': dumps({'turn_ref': turn_ref, 'request': proposal['history_request']})}


def prepare_history(body, status_code, agent_context):
    response = loads(body)
    if status_code != 200 or response.get('ok') is not True:
        raise ValueError('history unavailable')
    messages = response.get('messages')
    if not isinstance(messages, list) or len(messages) > 50:
        raise ValueError('history bounds exceeded')
    context = loads(agent_context)
    context['history_addendum'] = messages
    context['history_budget_remaining'] = 0
    encoded = dumps(context)
    if len(encoded) > 50000:
        raise ValueError('history context budget exceeded')
    return {'agent_context': encoded}


def proposal_request(turn_ref, proposal_json):
    return {'body': dumps({'turn_ref': turn_ref, 'proposal': loads(proposal_json)})}


def validation_response(body, status_code, proposal_json):
    response = loads(body)
    if status_code != 200:
        raise ValueError('validation unavailable')
    allowed = response.get('ok') is True and response.get('needs_public_reply') is True
    public = response.get('public_context', {}) if allowed else {}
    if not isinstance(public, dict) or set(public) - {'allowed_actions', 'public_facts', 'peer_message', 'relationship_context'}:
        raise ValueError('private field in public context')
    if allowed and not public:
        raise ValueError('empty public context')
    return {'needs_public_reply': allowed, 'public_context': dumps(public), 'proposal': proposal_json}


def public_proposal(text, proposal_json, turn_ref):
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('invalid public candidate')
    proposal = loads(proposal_json)
    proposal['public_message'] = text.strip()
    proposal['validation_ref'] = None
    return {'proposal': dumps(proposal), 'body': dumps({'turn_ref': turn_ref, 'proposal': proposal})}


def final_response(body, status_code, proposal_json):
    response = loads(body)
    if status_code != 200 or response.get('ok') is not True:
        raise ValueError('final text rejected')
    ref = response.get('validation_ref')
    if not isinstance(ref, str) or not ref or len(ref) > 2048:
        raise ValueError('missing validation receipt')
    proposal = loads(proposal_json)
    if not isinstance(proposal.get('public_message'), str) or not proposal['public_message']:
        raise ValueError('missing public candidate')
    proposal['validation_ref'] = ref
    return {'result': dumps(proposal)}


def internal_result(proposal_json):
    proposal = loads(proposal_json)
    proposal['public_message'] = None
    proposal['validation_ref'] = None
    proposal['proposed_actions'] = [{'type': 'WAIT'}]
    return {'result': dumps(proposal)}
