"""An adjacent temporal subject is a quotation claim even without a free verb."""
from copy import deepcopy
import json

import pytest

from server.negotiation.prices import InvalidFact
from test_dynamic_negotiation_acceptance import dynamic_context,natural_plan,validate_plan


@pytest.mark.parametrize('text',[
    '你之前{own_quote}，现在再看看？','您刚才{own_quote}，现在再看看？',
    '贵方上次{own_quote}，现在再看看？','我之前{own_quote}，现在再看看？',
    '我方刚才{own_quote}，现在再看看？','我们上次{own_quote}，现在再看看？',
    '你之前{our_last_quote}，现在{own_quote}。',
    '并非不是你之前{own_quote}，本轮{own_quote}。',
    '并非不是说你之前{own_quote}，本轮{own_quote}。',
    '我不再让价。你之前{own_quote}，现在再看看？',
    '我看到消息了。你之前{own_quote}，现在再看看？',
    '我看到你之前{own_quote}，现在再看看？'])
def test_implicit_temporal_ownership_or_current_as_history_is_not_authorized(text):
    context=dynamic_context();before=deepcopy(context)
    with pytest.raises((ValueError,InvalidFact)):validate_plan(context,natural_plan(text=text))
    assert context==before


@pytest.mark.parametrize('text',[
    '你看看{own_quote}这个位置，我们再聊？',
    '我看到你报价{peer_quote}，这次我给{own_quote}。',
    '我刚才看到你报价{peer_quote}，这次我给{own_quote}。',
    '不是你之前{own_quote}，是我现在{own_quote}，请再看看？',
    '并非我之前{own_quote}，这是我本轮{own_quote}，请再看看？',
    '不是说你之前{own_quote}，我现在{own_quote}。',
    '我不是说你之前{own_quote}，我现在{own_quote}。'])
def test_perception_polite_request_and_explicit_local_negation_do_not_create_a_false_quote_claim(text):
    context=dynamic_context();before=deepcopy(context)
    body=validate_plan(context,natural_plan(text=text))
    assert body['kind']=='REPLY' and body['our_price']['value']=='1.55'
    assert context==before


def test_actual_synthetic_store_sent_proof_allows_own_historical_quote_only_as_history(tmp_path):
    from test_verified_reply_repetition_closure import sent_case
    from test_reference_window_policy import OWNER
    case,cid=sent_case(tmp_path);context=case.reply_context();before=deepcopy(context)
    assert case.service.commands_core.get(OWNER,cid)['state']=='SENT'
    assert context['our_quote']['verified_sent'] is True
    body=validate_plan(context,natural_plan('3.18','我之前{our_last_quote}，现在{own_quote}，请再看看？'))
    assert '3.19%收' in body['text'] and '3.18%收' in body['text']
    assert context==before


def test_real_generated_actor_error_maps_to_one_existing_slots_correction(monkeypatch):
    from test_dify_output_contract_recovery import valid_context,generated_error_carrier,http_error
    from test_dify_output_contract_recovery import make_client,sent_context,structured_payload,successful_stage
    from test_reply_output_repair_projection import generated
    context=valid_context();candidate={'schema_version':'2.2','reply_plan':{
        'kind':'REPLY','quote_value':'1.55','text':'你之前{own_quote}，请再看看？','question':'NONE'}}
    before=deepcopy(context);body=generated_error_carrier(context,candidate)
    assert structured_payload(body)['reason_code']=='DIFY_MODEL_CONTRACT_SLOTS'
    expected,success=successful_stage(context)
    client,opener,_clock=make_client(monkeypatch,[(http_error(body),.1),(success,.1)])
    assert client.call(context)==expected and len(opener.calls)==2
    feedback=sent_context(opener.calls[1])['validation_feedback']
    assert feedback['previous_output']==candidate and feedback['reason_code']=='DIFY_MODEL_CONTRACT_SLOTS'
    for version in ['0.15.3','1.17.1']:
        _project,decode=generated(version)
        corrected=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':{
            'kind':'REPLY','quote_value':'1.55','question':'NONE','text':'我再靠近些，{own_quote}，你再看看？'},'trader_view':{}}),
            json.dumps(sent_context(opener.calls[1])))['result'])
        assert corrected==expected
    assert context==before


def test_second_actor_violation_does_not_get_a_third_request(monkeypatch):
    from test_dify_output_contract_recovery import valid_context,generated_error_carrier,http_error,make_client
    from server.negotiation.chatflow import FlowUnavailable
    context=valid_context();candidate={'schema_version':'2.2','reply_plan':{
        'kind':'REPLY','quote_value':'1.55','text':'我之前{own_quote}，请再看看？','question':'NONE'}}
    body=generated_error_carrier(context,candidate)
    client,opener,_clock=make_client(monkeypatch,[(http_error(body),.1),(http_error(body),.1)])
    with pytest.raises(FlowUnavailable):client.call(context)
    assert len(opener.calls)==2 and client.last_evidence['request_attempts']==2
