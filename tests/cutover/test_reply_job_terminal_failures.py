"""Request failures are bounded per call; unsent reply jobs remain recoverable.

The earlier basis-lifetime-terminal draft is preserved in evidence and is not
the business contract. All models, native senders and SQLite data are synthetic.
"""
from copy import deepcopy
import json

import pytest

from server.negotiation.chatflow import FlowUnavailable
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from test_production_flow import OWNER, desk
from test_reply_text_repair import reply_case, replies, unchanged_facts
from test_dify_output_contract_recovery import make_client, http_error, generated_error_carrier


def safe_response(context):
    return {'schema_version':'2.2','action':'SEND','proposal':render_reply_plan(context,
        {'kind':'REPLY','quote_value':'3.19','text':'{own_quote}{peer_volume}，你再看看？'})['proposal']}


@pytest.mark.parametrize('failure', ['PUBLIC_SCOPE', 'SLOTS', 'legacy_b'])
def test_a_later_worker_may_safely_regenerate_an_unsent_candidate_from_the_same_facts(desk, failure):
    case = reply_case(desk)
    engine = case.app.state.negotiation_engine
    basis = engine.commands.basis(OWNER, case.peer['member_id'])
    calls = []
    def rejected(context):
        calls.append(context)
        if failure == 'legacy_b':
            return {'schema_version':'2.2','history_requests':[{'member_id':context['member_id'],
                'message_ids':[context['context'][0]['message_id']]}]}
        raise FlowUnavailable('DIFY_MODEL_CONTRACT_'+failure, 400)
    case.flows.decide = rejected
    first = engine.decide_member(OWNER, case.peer['member_id'])
    assert first['state'] == 'RETRY' and not replies(case)
    assert engine.decide_member(OWNER, case.peer['member_id']) is None
    case.clock.value += 6
    assert engine.commands.basis(OWNER, case.peer['member_id']) == basis
    def repaired(context):
        calls.append(context)
        return safe_response(context)
    case.flows.decide = repaired
    current = engine.decide_member(OWNER, case.peer['member_id'])
    assert current['state'] == 'QUEUED' and len(calls) == 2 and len(replies(case)) == 1
    unchanged_facts(case)
    assert len(case.native.sent) == 1


def test_two_request_cap_and_stage_deadline_do_not_become_a_basis_lifetime_cap(desk, monkeypatch):
    case = reply_case(desk)
    engine = case.app.state.negotiation_engine
    logical, holder = [], {}
    def model(context):
        logical.append(deepcopy(context))
        if not holder:
            candidate = {'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'3.19',
                'text':'{own_quote}，成交。'}}
            body = generated_error_carrier(context, candidate)
            response = json.dumps({'answer':json.dumps(safe_response(context),ensure_ascii=False)}).encode()
            client, opener, clock = make_client(monkeypatch,
                [(http_error(body), .1), (http_error(body), .1), (response, .1)])
            holder.update(client=client, opener=opener, clock=clock)
        return holder['client'].call(context)
    case.flows.decide = model
    basis = engine.commands.basis(OWNER, case.peer['member_id'])
    first = engine.decide_member(OWNER, case.peer['member_id'])
    assert first['state'] == 'RETRY' and len(holder['opener'].calls) == 2
    assert holder['client'].last_evidence['request_attempts'] == 2
    assert holder['opener'].calls[1]['timeout'] < holder['opener'].calls[0]['timeout']
    assert engine.decide_member(OWNER, case.peer['member_id']) is None
    assert len(holder['opener'].calls) == 2 and not replies(case)
    case.clock.value += 6
    holder['clock'].sleep(6)
    assert engine.commands.basis(OWNER, case.peer['member_id']) == basis
    result = engine.decide_member(OWNER, case.peer['member_id'])
    assert result['state'] == 'QUEUED' and len(logical) == 2
    assert len(holder['opener'].calls) == 3, 'The third HTTP request belongs to a later worker attempt, not the failed call'
    assert not holder['client'].last_evidence.get('retry_failures')
    assert len(replies(case)) == 1 and len(case.native.sent) == 1
    unchanged_facts(case)


def test_backend_rejection_can_be_regenerated_by_run_once_without_new_peer_facts(desk):
    case = reply_case(desk)
    engine = case.app.state.negotiation_engine
    calls = []
    def invalid(context):
        calls.append(context)
        return {'schema_version':'2.2','action':'SEND','proposal':{'kind':'REPLY',
            'text':'SYN-A 3.19%收','our_price':{'kind':'YIELD','value':'3.19'}}}
    case.flows.decide = invalid
    assert engine.decide_member(OWNER, case.peer['member_id'])['state'] == 'REJECTED'
    assert not replies(case)
    case.clock.value += 10
    def repaired(context):
        calls.append(context)
        return safe_response(context)
    case.flows.decide = repaired
    engine.run_once()
    case.app.state.dispatcher.run_once()
    assert len(calls) == 2 and len(replies(case)) == 1 and len(case.native.sent) == 2
    unchanged_facts(case)


@pytest.mark.parametrize('state', ['QUEUED', 'SENT', 'UNKNOWN'])
def test_recovery_does_not_duplicate_an_already_queued_sent_or_unknown_command(desk, state):
    case = reply_case(desk)
    engine = case.app.state.negotiation_engine
    calls = []
    def model(context):
        calls.append(context)
        return safe_response(context)
    case.flows.decide = model
    queued = engine.decide_member(OWNER, case.peer['member_id'])
    assert queued['state'] == 'QUEUED'
    if state == 'UNKNOWN':
        def uncertain(command):
            case.native.sent.append(dict(command))
            return {**{key:command[key] for key in ['command_id','account_uid','peer_id','content_hash']},
                    'status':'unknown','evidence':'synthetic native uncertainty'}
        case.native.send = uncertain
        case.native.reconcile = lambda command: {**{key:command[key] for key in
            ['command_id','account_uid','peer_id','content_hash']},'status':'unknown','evidence':'synthetic still unknown'}
    if state != 'QUEUED':
        case.app.state.dispatcher.run_once()
        case.app.state.dispatcher.run_once()
    case.clock.value += 10
    assert engine.decide_member(OWNER, case.peer['member_id']) is None
    assert len(calls) == 1 and len(replies(case)) == 1
    command = engine.commands.get(OWNER, queued['command_id'])
    assert command['state'] == state
    assert len(case.native.sent) == (1 if state == 'QUEUED' else 2)
    unchanged_facts(case)


@pytest.mark.parametrize('error', [
    FlowUnavailable('DIFY_UPSTREAM_UNAVAILABLE', 502), FlowUnavailable('DIFY_TIMEOUT'),
    FlowUnavailable('DIFY_HTTP_ERROR', 400), InvalidFact('synthetic nonpermanent input failure'),
])
def test_transient_and_other_failures_keep_the_original_backoff_retry(desk, error):
    case = reply_case(desk)
    calls = []
    def model(context):
        calls.append(context)
        raise error
    case.flows.decide = model
    first = case.app.state.negotiation_engine.decide_member(OWNER, case.peer['member_id'])
    assert case.app.state.negotiation_engine.decide_member(OWNER, case.peer['member_id']) is None
    case.clock.value += 6
    second = case.app.state.negotiation_engine.decide_member(OWNER, case.peer['member_id'])
    assert first['state'] == second['state'] == 'RETRY' and len(calls) == 2
    assert not replies(case) and len(case.native.sent) == 1
    unchanged_facts(case)
