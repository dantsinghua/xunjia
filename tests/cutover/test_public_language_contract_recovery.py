"""Public-language checks must fail early without weakening final authority."""
from copy import deepcopy
import builtins
import json
import re
import sys

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from server.negotiation.chatflow import FlowUnavailable
from dify.negotiation_v22.build import build
from test_dify_output_contract_recovery import (
    valid_context, generated_error_carrier, structured_payload, replace_structured_payload,
    make_client, http_error, sent_context, successful_stage,
)


PUBLIC_CODE = 'DIFY_MODEL_CONTRACT_PUBLIC_SCOPE'


PUBLIC_REASONS = [
    ('{own_quote}，内部策略不能公开。', 'Public reply exposes private policy or internal context'),
    ('{own_quote}，{peer_volume}确认。', 'Quantity confirmation must explicitly refer to a quotation, not execution'),
    ('{own_quote}，这笔成交。', 'Reply promises execution or reserves a counterparty lot'),
]


def language_plan(text):
    return {'kind': 'REPLY', 'quote_value': '1.55', 'text': text, 'question': 'NONE'}


@pytest.mark.parametrize('text,reason', PUBLIC_REASONS)
def test_existing_backend_still_rejects_the_original_exact_language_reason(text, reason):
    context = valid_context()
    body = {'kind': 'REPLY', 'our_price': {'kind': 'YIELD', 'value': '1.55'},
            'text': text.replace('{own_quote}', '1.55%收').replace('{peer_volume}', '500万')}
    before = deepcopy((context, body))
    with pytest.raises(InvalidFact, match='^'+re.escape(reason)+'$'):
        Engine.__new__(Engine)._validate_reply(context, body)
    assert (context, body) == before


@pytest.mark.parametrize('text,reason', PUBLIC_REASONS)
def test_renderer_rejects_the_same_known_language_error_before_code_can_return_a_candidate(text, reason):
    context, plan = valid_context(), language_plan(text)
    before = deepcopy((context, plan))
    with pytest.raises(ValueError, match='^'+re.escape(reason)+'$'):
        render_reply_plan(context, plan)
    assert (context, plan) == before


def test_generated_code_rejects_a_complete_can_we_trade_candidate_with_controlled_public_scope():
    candidate = {'schema_version': '2.2', 'reply_plan': language_plan('{own_quote}，可以成交吗？')}
    body = generated_error_carrier(valid_context(), candidate)
    payload = structured_payload(body)
    assert payload['reason_code'] == PUBLIC_CODE
    assert payload['origin'] == 'render_reply_plan' and payload['previous_output'] == candidate


@pytest.mark.parametrize('text,reason', PUBLIC_REASONS)
def test_each_exact_public_reason_can_receive_only_one_frozen_context_correction(monkeypatch, text, reason):
    context = valid_context()
    before = deepcopy(context)
    candidate = {'schema_version': '2.2', 'reply_plan': language_plan(text)}
    body = generated_error_carrier(context, candidate)
    assert 'ValueError: '+reason+'\n' in body['message']
    assert structured_payload(body)['reason_code'] == PUBLIC_CODE
    expected, success = successful_stage(context)
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), .1), (success, .1)])
    assert client.call(context) == expected and context == before
    first, second = map(sent_context, opener.calls)
    feedback = second.pop('validation_feedback')
    assert feedback == {'mode': 'OUTPUT_CONTRACT', 'attempt': 1, 'max_attempts': 1,
                        'reason_code': PUBLIC_CODE, 'previous_output': candidate}
    assert first == second and len(opener.calls) == 2
    assert client.last_evidence['retry_failures'][0]['error_code'] == PUBLIC_CODE
    assert opener.calls[1]['timeout'] < opener.calls[0]['timeout']
    assert Engine.__new__(Engine)._validate_reply(context, expected['proposal'])['our_price']['value'] == '1.55'


@pytest.mark.parametrize('text', ['我再靠近些，{own_quote}，你再看看？',
    '不自动成交、不锁量，{own_quote}，先确认报价。',
    '{own_quote}，这只是报价，不涉及成交。'])
def test_ordinary_natural_wording_and_previously_legal_local_negation_remain_legal(text):
    context = valid_context()
    rendered = render_reply_plan(context, language_plan(text))
    body = Engine.__new__(Engine)._validate_reply(context, rendered['proposal'])
    assert body['text'] == text.replace('{own_quote}', '1.55%收')


def test_another_public_language_violation_on_correction_stops_without_a_third_attempt(monkeypatch):
    context = valid_context()
    first = generated_error_carrier(context, {'schema_version':'2.2', 'reply_plan':language_plan(PUBLIC_REASONS[0][0])})
    second = generated_error_carrier(context, {'schema_version':'2.2', 'reply_plan':language_plan(PUBLIC_REASONS[2][0])})
    client, opener, _clock = make_client(monkeypatch, [(http_error(first), .1), (http_error(second), .1)])
    with pytest.raises(FlowUnavailable): client.call(context)
    assert len(opener.calls) == 2 and client.last_evidence['request_attempts'] == 2


def test_public_carrier_does_not_cascade_into_output_recovery_during_text_only(monkeypatch):
    context = valid_context()
    body = generated_error_carrier(context, {'schema_version':'2.2', 'reply_plan':language_plan(PUBLIC_REASONS[2][0])})
    context['validation_feedback'] = {'mode':'TEXT_ONLY', 'attempt':1, 'max_attempts':1,
        'reason':'Reply direction differs from our authorized side', 'original_response':successful_stage(context)[0]}
    before = deepcopy(context)
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), .1)])
    with pytest.raises(FlowUnavailable): client.call(context)
    assert len(opener.calls) == 1 and context == before
    assert sent_context(opener.calls[0])['validation_feedback'] == before['validation_feedback']


@pytest.mark.parametrize('bad_cause', [None, 'Unknown public language rule'])
def test_public_scope_code_without_one_exact_permitted_cause_is_not_a_recovery(monkeypatch, bad_cause):
    context = valid_context()
    body = generated_error_carrier(context, {'schema_version':'2.2', 'reply_plan':language_plan(PUBLIC_REASONS[0][0])})
    line = 'ValueError: '+PUBLIC_REASONS[0][1]+'\n'
    body['message'] = body['message'].replace(line, '' if bad_cause is None else 'ValueError: '+bad_cause+'\n')
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), .1)])
    with pytest.raises(FlowUnavailable): client.call(context)
    assert len(opener.calls) == 1


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_real_generated_closures_execute_public_language_helper_without_application_imports(version):
    graph = build('reply', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    nodes = {row['id']:row['data'] for row in graph['nodes']}
    import_module = builtins.__import__
    def stdlib_only(name, *args, **kwargs):
        assert name.split('.')[0] in sys.stdlib_module_names, 'Generated Code cannot import application or external SDK modules'
        return import_module(name, *args, **kwargs)
    scope = {'__builtins__':{**vars(builtins), '__import__':stdlib_only}}
    exec(compile(nodes['contract']['code'], '<string>', 'exec'), scope)
    for text, reason in PUBLIC_REASONS:
        with pytest.raises(ValueError) as caught:
            scope['main'](json.dumps({'schema_version':'2.2','reply_plan':language_plan(text)}),
                          json.dumps({**valid_context(),'schema_version':'2.2','stage':'reply'}))
        assert str(caught.value).startswith('XUNJIA_OUTPUT_CONTRACT_V1:')
        assert json.loads(str(caught.value).split(':',1)[1])['reason_code'] == PUBLIC_CODE
    projection = {'__builtins__':{**vars(builtins), '__import__':stdlib_only}}
    exec(compile(nodes['context']['code'], '<string>', 'exec'), projection)
    candidate = {'schema_version':'2.2', 'reply_plan':language_plan(PUBLIC_REASONS[0][0])}
    corrected = {**valid_context(),'schema_version':'2.2','stage':'reply','validation_feedback':{
        'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,'reason_code':PUBLIC_CODE,'previous_output':candidate}}
    result = projection['main'](json.dumps(corrected))
    model_candidate=deepcopy(candidate)
    del model_candidate['reply_plan']['text']
    assert json.loads(result['context'])['validation_feedback']['previous_output'] == model_candidate
    assert corrected['validation_feedback']['previous_output'] == candidate
