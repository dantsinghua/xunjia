"""Ask the missing settlement condition, preserving source and bounded recovery."""
from copy import deepcopy
import json

import pytest

from server.negotiation.chatflow import FlowUnavailable, _output_contract_failure
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from test_verified_literal_fact_binding import cash_context, settlement, validated
from test_prompt_mode_responsibilities import generated
from test_dify_output_contract_recovery import (
    generated_error_carrier, make_client, http_error, sent_context)


CAUSE = 'Settlement clarification must actually ask for settlement'
ACTUAL_BAD = '您好，请问SYN-A 净价34.19出{peer_volume}万，是基于现金交割吗？'


def context():
    value=cash_context();value.update(schema_version='2.2',stage='reply')
    value['quotes']['ofr']['price']['value']='34.19'
    value['context'][0]['text']='SYN-A 净价34.19出500万'
    assert value['quotes']['ofr']['price']['settlement_date'] is None
    return value


@pytest.mark.parametrize('text,allowed', [
    ('请问哪天交割？',True),
    ('请问何时交收？',True),
    ('结算怎么安排？',True),
    ('您看{settlement_options}，选哪个？',True),
    (ACTUAL_BAD,False),
    ('请问是现金结算还是实物结算？',False),
])
def test_settlement_question_supports_date_synonyms_and_options_but_not_payment_method_only(text,allowed):
    value=context();plan=settlement(text);before=deepcopy((value,plan))
    if allowed:
        rendered,body=validated(value,plan)
        assert body['kind']=='CLARIFY' and body.get('our_price') is None
        assert body['settlement_question']['side']=='ofr'
        assert rendered['proposal']['render_plan']['text']==text
        assert body['text']==text.replace('{settlement_options}','T+0、T+1或具体日期')
    else:
        with pytest.raises(ValueError,match='^'+CAUSE+'$'):render_reply_plan(value,plan)
    assert (value,plan)==before and value['quotes']['ofr']['price']['settlement_date'] is None


def test_real_semantic_code_failure_can_correct_to_a_date_question_once_with_the_original_client_cap(monkeypatch):
    value=context();candidate={'schema_version':'2.2','reply_plan':settlement(ACTUAL_BAD)}
    before=deepcopy((value,candidate))
    for version in ('0.15.3','1.17.1'):
        carrier=generated_error_carrier(value,candidate,version=version)
        assert _output_contract_failure(400,json.dumps(carrier).encode())==('DIFY_MODEL_CONTRACT_SLOTS',candidate)
        project,contract=generated('reply',version)
        good={'schema_version':'2.2','reply_plan':settlement('请问哪天交割？')}
        answer=json.loads(contract(json.dumps(good),json.dumps(value))['result'])
        client,opener,_clock=make_client(monkeypatch,[(http_error(carrier),.1),
            (json.dumps({'answer':json.dumps(answer)}).encode(),.1)])
        assert client.call(value)==answer and len(opener.calls)==2
        corrected=sent_context(opener.calls[1]);feedback=corrected.pop('validation_feedback')
        assert corrected==value and feedback['previous_output']==candidate
        system=project(json.dumps(sent_context(opener.calls[1])))['system_prompt']
        assert any('方式' in line and any(word in line for word in ('日期','哪天','何时','交割日','交收日'))
                   and any(word in line for word in ('问','询','确认')) for line in system.splitlines())
        assert (value,candidate)==before
        client,opener,_clock=make_client(monkeypatch,[(http_error(carrier),.1),(http_error(carrier),.1)])
        with pytest.raises(FlowUnavailable) as exc:client.call(value)
        assert exc.value.code=='DIFY_MODEL_CONTRACT_SLOTS' and len(opener.calls)==2
        assert opener.calls[1]['timeout']<opener.calls[0]['timeout']
    assert (value,candidate)==before


def test_provider_auth_or_generic_400_with_the_same_reason_words_never_gain_code_recovery(monkeypatch):
    value=context();before=deepcopy(value)
    for status,message in [
        (401,CAUSE),
        (400,'Run failed: [openai_api_compatible] Error: API request failed with status code 403: '+CAUSE),
        (400,'ValueError: '+CAUSE),
    ]:
        carrier={'code':'invalid_param','status':status,'message':message}
        assert _output_contract_failure(status,json.dumps(carrier).encode()) is None
        client,opener,_clock=make_client(monkeypatch,[(http_error(carrier,status=status),.1)])
        with pytest.raises(FlowUnavailable):client.call(value)
        assert len(opener.calls)==1 and 'output_contract_recovery' not in client.last_evidence
    assert value==before
