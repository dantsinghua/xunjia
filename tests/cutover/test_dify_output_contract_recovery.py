"""Independent bounded recovery of authentic Dify Code output-contract failures.

All transport is a synthetic in-process Opener. No model, gateway or IM is used.
"""
from copy import deepcopy
from functools import lru_cache
import io
import json
import logging
import re
import traceback
import urllib.error

import pytest

from server.negotiation.chatflow import ChatflowClient, FlowUnavailable
from server.negotiation.engine import Engine
from server.negotiation.reply_render import render_reply_plan
from dify.negotiation_v22.build import build
from test_dynamic_negotiation_acceptance import dynamic_context


REASONS = [
    ("Economic numbers and facts must use verified slots", "DIFY_MODEL_CONTRACT_SLOTS"),
    ("Selected quote is outside the authorized final boundary", "DIFY_MODEL_CONTRACT_BOUNDARY"),
    ("A quote reply must bind its own structured quotation", "DIFY_MODEL_CONTRACT_OWN_QUOTE"),
]
PRIVATE_MARKERS = ("SYNTHETIC_APP_SECRET", "SYNTHETIC_PRIVATE_HTTP_BODY", "SYNTHETIC_PRIVATE_PROMPT")
CONTRACT_MARKER = "XUNJIA_OUTPUT_CONTRACT_V1:"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response(io.BytesIO):
    status = 200


class ScriptedOpener:
    def __init__(self, clock, steps):
        self.clock, self.steps, self.calls = clock, list(steps), []

    def open(self, request, timeout):
        self.calls.append({
            "body": json.loads(request.data),
            "timeout": timeout,
            "at": self.clock(),
            "headers": dict(request.header_items()),
        })
        assert self.steps, "The logical operation exceeded its bounded request allowance"
        outcome, elapsed = self.steps.pop(0)
        self.clock.sleep(elapsed)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome)


@lru_cache(maxsize=2)
def generated_contract(version="1.17.1"):
    document = build("reply", version=version, provider="synthetic", model="synthetic")
    code = next(row["data"]["code"] for row in document["workflow"]["graph"]["nodes"]
                if row["id"] == "contract")
    namespace = {}
    exec(compile(code, "<string>", "exec"), namespace)
    return namespace["main"]


def failed_candidate(reason=REASONS[0][0]):
    plan = {"kind": "REPLY", "quote_value": "1.55", "text": "SYNTHETIC_PRIVATE_HTTP_BODY {own_quote}", "question": "NONE"}
    if reason == REASONS[0][0]:
        plan["text"] = "SYNTHETIC_PRIVATE_HTTP_BODY 1.54%收，{own_quote}"
    elif reason == REASONS[1][0]:
        plan["quote_value"] = "1.39"
    elif reason == REASONS[2][0]:
        plan["text"] = "SYNTHETIC_PRIVATE_HTTP_BODY 麻烦再看看？"
    else:
        raise AssertionError("This fixture only exercises the three permitted real renderer errors")
    return {"schema_version": "2.2", "reply_plan": plan}


def generated_error_carrier(context, candidate, version="1.17.1"):
    typed_context = {**deepcopy(context), "schema_version": "2.2", "stage": "reply"}
    try:
        generated_contract(version)(json.dumps(candidate, ensure_ascii=False), json.dumps(typed_context, ensure_ascii=False))
    except ValueError:
        # Model the actual Dify invalid_param wrapper around a real generated
        # Code exception, rather than inventing function names or line numbers.
        message = "Run failed: " + traceback.format_exc() + "error: exit status 255\n"
        return {"code": "invalid_param", "status": 400, "message": message}
    raise AssertionError("The intended malformed candidate unexpectedly passed real generated Code")


def carrier(reason=REASONS[0][0], **overrides):
    return {**generated_error_carrier(valid_context(), failed_candidate(reason)), **overrides}


def http_error(body, status=400):
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    return urllib.error.HTTPError(
        "https://synthetic.invalid/v1/chat-messages", status, "synthetic error", {}, io.BytesIO(raw))


def upstream_failure():
    return http_error({
        "code": "invalid_param", "status": 400,
        "message": "Run failed: [openai_api_compatible] Error: API request failed with status code 502: upstream unavailable",
    })


def valid_context():
    context = dynamic_context()
    context["_stage_budget_seconds"] = 5.0
    context["diagnostic_probe"] = {"private": "SYNTHETIC_PRIVATE_PROMPT", "nested": [1, {"basis_note": "frozen"}]}
    return context


def successful_stage(context):
    plan = {"kind": "REPLY", "quote_value": "1.55", "text": "我再靠近些，{own_quote}，你再看看？", "question": "NONE"}
    proposal = render_reply_plan(context, plan)["proposal"]
    stage = {"schema_version": "2.2", "action": "SEND", "proposal": proposal, "trader_view": {}}
    return stage, json.dumps({
        "answer": json.dumps(stage, ensure_ascii=False), "message_id": "synthetic-result",
        "workflow_run_id": "synthetic-workflow",
    }, ensure_ascii=False).encode()


def make_client(monkeypatch, steps, *, stage="reply", clock=None):
    clock = clock or Clock()
    monkeypatch.setattr("server.negotiation.chatflow.time.sleep", clock.sleep)
    opener = ScriptedOpener(clock, steps)
    client = ChatflowClient(
        "https://synthetic.invalid", "SYNTHETIC_APP_SECRET",
        stage=stage, timeout=90, opener=opener, clock=clock)
    return client, opener, clock


def sent_context(call):
    return json.loads(call["body"]["inputs"]["context_json"])


def assert_private_evidence(client, caplog, error=None):
    public = json.dumps(client.last_evidence) + caplog.text + ((str(error) + repr(error)) if error else "")
    assert not any(marker in public for marker in PRIVATE_MARKERS)


@pytest.mark.parametrize("reason,code", REASONS)
def test_exact_code_failure_recovers_once_with_bound_result_and_immutable_inputs(monkeypatch, caplog, reason, code):
    caplog.set_level(logging.INFO)
    context = valid_context()
    original = deepcopy(context)
    expected, success = successful_stage(context)
    client, opener, _clock = make_client(monkeypatch, [(http_error(carrier(reason)), 1.25), (success, 0.1)])

    result = client.call(context)

    assert result == expected and isinstance(result, dict)
    assert context == original, "Correction must not mutate the caller's nested frozen input"
    assert len(opener.calls) == 2
    before, after = map(sent_context, opener.calls)
    assert "_stage_budget_seconds" not in before and "_stage_budget_seconds" not in after
    feedback = after.pop("validation_feedback")
    assert feedback == {"mode": "OUTPUT_CONTRACT", "attempt": 1, "max_attempts": 1,
                        "reason_code": code, "previous_output": failed_candidate(reason)}
    assert before == after, "Only the additional correction feedback may change between requests"
    for key in ("basis", "context", "quotes", "our_quote", "boundary"):
        assert before[key] == original[key]
    outer_before, outer_after = (deepcopy(call["body"]) for call in opener.calls)
    outer_before["inputs"].pop("context_json")
    outer_after["inputs"].pop("context_json")
    assert outer_before == outer_after and "conversation_id" not in outer_after
    assert opener.calls[0]["headers"] == opener.calls[1]["headers"]
    assert opener.calls[1]["timeout"] == pytest.approx(
        5.0 - (opener.calls[1]["at"] - opener.calls[0]["at"]))
    assert 0 < opener.calls[1]["timeout"] < opener.calls[0]["timeout"] == 5.0
    assert client.last_evidence["request_attempts"] == 2
    assert client.last_evidence["retry_failures"][0]["error_code"] == code
    assert client.last_evidence["retry_failures"][0]["http_status"] == 400
    assert client.last_evidence["workflow_run_id"] == "synthetic-workflow"
    # Recovery does not grant economic authority: the existing hard validator still owns it.
    validated = Engine.__new__(Engine)._validate_reply(original, deepcopy(result["proposal"]))
    assert validated["our_price"] == {
        "kind": "YIELD", "value": "1.55", "settlement_date": None,
        "computed_yield": None, "calculation_ref": None,
    }
    assert_private_evidence(client, caplog)


def rejected_carriers():
    reason = REASONS[0][0]
    proper = carrier()
    return [
        {"message": reason},
        {"code": "invalid_param", "status": 400, "message": "ValueError: " + reason},
        carrier(code="other_error"),
        carrier(status=401),
        carrier(message=proper["message"].replace("in render_reply_plan", "in unrelated_function")),
        carrier(message=proper["message"].replace("Run failed: Traceback (most recent call last):", "ordinary client error")),
        carrier(message=proper["message"].replace("ValueError:", "json.decoder.JSONDecodeError:")),
        carrier(message=proper["message"].replace(reason, "Unknown natural reply plan fields")),
        carrier(message=proper["message"] + "untrusted extra trailer"),
        carrier(message=proper["message"].replace("ValueError: " + reason, 'ValueError: quoted "' + reason + '"')),
        b"{not-valid-json",
        b"\xff",
        json.dumps(carrier()).encode() + b" " * 17000,
    ]


@pytest.mark.parametrize("body", rejected_carriers())
def test_arbitrary_or_spoofed_400_carrier_never_becomes_a_contract_retry(monkeypatch, caplog, body):
    caplog.set_level(logging.INFO)
    context = valid_context()
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), 0.2)])
    with pytest.raises(FlowUnavailable) as caught:
        client.call(context)
    assert len(opener.calls) == 1
    assert not client.last_evidence.get("output_contract_recovery", {}).get("attempted")
    assert_private_evidence(client, caplog, caught.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
def test_non_400_contract_text_never_grants_contract_recovery(monkeypatch, status):
    client, opener, _clock = make_client(monkeypatch, [(http_error(carrier(), status), 0.1)])
    with pytest.raises(FlowUnavailable):
        client.call(valid_context())
    assert len(opener.calls) == 1


@pytest.mark.parametrize("failure", [
    TimeoutError("SYNTHETIC_PRIVATE_HTTP_BODY"),
    urllib.error.URLError(TimeoutError("SYNTHETIC_PRIVATE_HTTP_BODY")),
])
def test_timeout_never_retries_or_discloses_raw_failure(monkeypatch, caplog, failure):
    client, opener, _clock = make_client(monkeypatch, [(failure, 0.1)])
    with pytest.raises(FlowUnavailable, match="DIFY_TIMEOUT") as caught:
        client.call(valid_context())
    assert len(opener.calls) == 1
    assert_private_evidence(client, caplog, caught.value)


@pytest.mark.parametrize("raw", [b"{not-json", b"\xff", b'{"answer":"not-json"}'])
def test_stage_json_failure_is_not_an_http_contract_recovery(monkeypatch, raw):
    client, opener, _clock = make_client(monkeypatch, [(raw, 0.1)])
    with pytest.raises(FlowUnavailable, match="DIFY_INVALID_STAGE_JSON"):
        client.call(valid_context())
    assert len(opener.calls) == 1


@pytest.mark.parametrize("reason,code", REASONS)
def test_second_contract_failure_is_terminal_with_first_failure_retained(monkeypatch, caplog, reason, code):
    client, opener, _clock = make_client(monkeypatch, [
        (http_error(carrier(reason)), 0.2), (http_error(carrier(reason)), 0.2)])
    with pytest.raises(FlowUnavailable) as caught:
        client.call(valid_context())
    assert len(opener.calls) == 2
    assert client.last_evidence["request_attempts"] == 2
    assert client.last_evidence["retry_failures"][0]["error_code"] == code
    assert_private_evidence(client, caplog, caught.value)


@pytest.mark.parametrize("contract_first", [False, True])
def test_upstream_and_contract_failures_share_one_total_two_request_cap(monkeypatch, caplog, contract_first):
    contract = http_error(carrier())
    failures = [contract, upstream_failure()] if contract_first else [upstream_failure(), contract]
    client, opener, _clock = make_client(monkeypatch, [(failure, 0.4) for failure in failures])
    with pytest.raises(FlowUnavailable) as caught:
        client.call(valid_context())
    assert len(opener.calls) == 2 and client.last_evidence["request_attempts"] == 2
    first, second = map(sent_context, opener.calls)
    assert client.last_evidence["retry_failures"][0]["error_code"] == (
        "DIFY_MODEL_CONTRACT_SLOTS" if contract_first else "DIFY_UPSTREAM_UNAVAILABLE")
    if contract_first:
        assert second.pop("validation_feedback")["mode"] == "OUTPUT_CONTRACT"
        assert second == first
    else:
        assert second == first and "validation_feedback" not in second
    assert opener.calls[1]["timeout"] == pytest.approx(
        5.0 - (opener.calls[1]["at"] - opener.calls[0]["at"]))
    assert_private_evidence(client, caplog, caught.value)


@pytest.mark.parametrize("stage,feedback", [
    ("analysis", None),
    ("reply", {"mode": "TEXT_ONLY", "attempt": 1, "max_attempts": 1}),
    ("reply", {"mode": "EVIDENCE_ONLY", "attempt": 1, "max_attempts": 1}),
    ("reply", {"mode": "OUTPUT_CONTRACT", "attempt": 1, "max_attempts": 1}),
])
def test_other_stage_or_existing_feedback_cannot_open_another_correction(monkeypatch, stage, feedback):
    context = valid_context()
    if feedback is not None:
        context["validation_feedback"] = feedback
    original = deepcopy(context)
    client, opener, _clock = make_client(monkeypatch, [(http_error(carrier()), 0.1)], stage=stage)
    with pytest.raises(FlowUnavailable):
        client.call(context)
    assert len(opener.calls) == 1 and context == original
    assert sent_context(opener.calls[0]).get("validation_feedback") == feedback


def test_exhausted_absolute_deadline_cannot_start_a_correction(monkeypatch):
    context = valid_context()
    context["_stage_budget_seconds"] = 1.0
    client, opener, _clock = make_client(monkeypatch, [(http_error(carrier()), 1.1)])
    with pytest.raises(FlowUnavailable):
        client.call(context)
    assert len(opener.calls) == 1
    assert not client.last_evidence.get("output_contract_recovery", {}).get("attempted")


def test_ordinary_valid_response_does_not_gain_a_second_call(monkeypatch):
    context = valid_context()
    expected, success = successful_stage(context)
    client, opener, _clock = make_client(monkeypatch, [(success, 0.1)])
    assert client.call(context) == expected
    assert len(opener.calls) == 1 and "validation_feedback" not in sent_context(opener.calls[0])


def test_valid_text_only_patch_stays_a_frozen_text_only_operation(monkeypatch):
    context = valid_context()
    context["validation_feedback"] = {"mode": "TEXT_ONLY", "attempt": 1, "max_attempts": 1,
                                      "reason": "Public yield text differs from validated quote"}
    context["original_response"] = {"schema_version": "2.2", "action": "SEND",
                                    "proposal": {"kind": "REPLY", "our_price": {"kind": "YIELD", "value": "1.55"}}}
    original = deepcopy(context)
    expected = {"schema_version": "2.2", "text_repair": {"text": "synthetic frozen text patch"}}
    raw = json.dumps({"answer": json.dumps(expected)}).encode()
    client, opener, _clock = make_client(monkeypatch, [(raw, 0.1)])
    assert client.call(context) == expected and context == original
    assert len(opener.calls) == 1
    assert sent_context(opener.calls[0])["validation_feedback"] == original["validation_feedback"]


def test_later_normal_operation_cannot_inherit_prior_recovery_evidence(monkeypatch):
    context = valid_context()
    expected, success = successful_stage(context)
    client, opener, _clock = make_client(monkeypatch, [
        (http_error(carrier()), 0.1), (success, 0.1), (success, 0.1)])
    assert client.call(context) == expected
    assert client.last_evidence["retry_failures"][0]["error_code"] == "DIFY_MODEL_CONTRACT_SLOTS"
    assert client.call(context) == expected
    assert len(opener.calls) == 3
    assert not client.last_evidence.get("retry_failures")
    assert not client.last_evidence.get("output_contract_recovery", {}).get("attempted")


def structured_payload(body):
    lines = body["message"].splitlines()
    terminal = next(line for line in reversed(lines)
                    if line.startswith("ValueError: " + CONTRACT_MARKER))
    return json.loads(terminal.removeprefix("ValueError: " + CONTRACT_MARKER))


def replace_structured_payload(body, payload, *, raw_json=None):
    body = deepcopy(body)
    encoded = raw_json if raw_json is not None else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    message, count = re.subn(r"(?m)^ValueError: " + re.escape(CONTRACT_MARKER) + r"[^\n]*$",
                            lambda _match: "ValueError: " + CONTRACT_MARKER + encoded,
                            body["message"])
    assert count == 1, "Only the generated terminal exception is a structured carrier"
    body["message"] = message
    return body


@pytest.mark.parametrize("version", ["0.15.3", "1.17.1"])
@pytest.mark.parametrize("reason,code", REASONS)
def test_real_generated_wrapper_carries_only_the_exact_failed_model_plan(version, reason, code):
    context = valid_context()
    candidate = {**failed_candidate(reason), "action": "SEND", "trader_view": {"reason": "SYNTHETIC_PRIVATE_PROMPT"}}
    before = deepcopy((context, candidate))
    actual = generated_error_carrier(context, candidate, version)
    payload = structured_payload(actual)
    assert payload == {"version": 1, "origin": "render_reply_plan", "reason_code": code,
                       "previous_output": failed_candidate(reason)}
    assert (context, candidate) == before
    assert "SYNTHETIC_PRIVATE_PROMPT" not in json.dumps(payload)
    assert "in decode_model_output" in actual["message"] and "in render_reply_plan" in actual["message"]


@pytest.mark.parametrize("mutation", [
    "extra_carrier", "version_bool", "wrong_origin", "reason_mismatch", "candidate_extra",
    "candidate_list", "candidate_schema", "plan_extra", "plan_nan", "plan_nan_string",
    "plan_text_not_string", "plan_context_ref", "plan_too_long", "json_nan", "json_duplicate",
])
def test_malformed_structured_candidate_cannot_grant_a_second_request(monkeypatch, caplog, mutation):
    caplog.set_level(logging.DEBUG)
    body = carrier()
    payload = structured_payload(body)
    plan = payload["previous_output"]["reply_plan"]
    raw_json = None
    if mutation == "extra_carrier": payload["server_context"] = {"private": "SYNTHETIC_PRIVATE_PROMPT"}
    elif mutation == "version_bool": payload["version"] = True
    elif mutation == "wrong_origin": payload["origin"] = "unrelated_tool"
    elif mutation == "reason_mismatch": payload["reason_code"] = REASONS[1][1]
    elif mutation == "candidate_extra": payload["previous_output"]["context"] = valid_context()
    elif mutation == "candidate_list": payload["previous_output"] = []
    elif mutation == "candidate_schema": payload["previous_output"]["schema_version"] = "3"
    elif mutation == "plan_extra": plan["command_id"] = "server-owned-id"
    elif mutation == "plan_nan": plan["quote_value"] = float("nan")
    elif mutation == "plan_nan_string": plan["quote_value"] = "NaN"
    elif mutation == "plan_text_not_string": plan["text"] = {"template": "{own_quote}"}
    elif mutation == "plan_context_ref": plan["context_ref"] = "not-a-context-hash"
    elif mutation == "plan_too_long": plan["text"] = "x"*1501
    elif mutation == "json_nan": raw_json = json.dumps(payload).replace('"version": 1', '"version": NaN')
    else: raw_json = json.dumps(payload).replace('"version": 1', '"version": 1, "version": 1')
    body = replace_structured_payload(body, payload, raw_json=raw_json)
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), 0.1)])
    with pytest.raises(FlowUnavailable) as caught:
        client.call(valid_context())
    assert len(opener.calls) == 1
    assert not client.last_evidence.get("output_contract_recovery", {}).get("attempted")
    assert_private_evidence(client, caplog, caught.value)


@pytest.mark.parametrize("mutation", ["marker_only", "no_handler", "no_renderer", "different_exception", "oversized_marker"])
def test_marker_and_origin_spoofs_are_not_authentic_generated_errors(monkeypatch, mutation):
    body = carrier()
    payload = structured_payload(body)
    if mutation == "marker_only": body["message"] = "Run failed: ValueError: " + CONTRACT_MARKER + json.dumps(payload)
    elif mutation == "no_handler": body["message"] = body["message"].replace("in decode_model_output", "in unrelated_handler")
    elif mutation == "no_renderer": body["message"] = body["message"].replace("in render_reply_plan", "in unrelated_renderer")
    elif mutation == "different_exception": body["message"] = body["message"].replace("ValueError: " + CONTRACT_MARKER, "RuntimeError: " + CONTRACT_MARKER)
    else: body = replace_structured_payload(body, payload, raw_json=json.dumps(payload)+" "*8193)
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), 0.1)])
    with pytest.raises(FlowUnavailable): client.call(valid_context())
    assert len(opener.calls) == 1


def test_unstructured_old_traceback_without_original_candidate_is_not_recoverable(monkeypatch):
    reason = REASONS[0][0]
    message = ('Run failed: Traceback (most recent call last):\n'
               '  File "<string>", line 1322, in decode_stage_result\n'
               '  File "<string>", line 616, in render_reply_plan\n'
               'ValueError: '+reason+'\nerror: exit status 255\n')
    client, opener, _clock = make_client(monkeypatch, [(http_error({"code":"invalid_param", "status":400, "message":message}), 0.1)])
    with pytest.raises(FlowUnavailable): client.call(valid_context())
    assert len(opener.calls) == 1, "An old traceback lacks the exact model candidate; do not guess a repair"


def test_only_known_render_errors_are_wrapped_by_real_generated_code(monkeypatch):
    candidate = failed_candidate()
    candidate["reply_plan"]["unsupported_field"] = "SYNTHETIC_PRIVATE_HTTP_BODY"
    body = generated_error_carrier(valid_context(), candidate)
    assert CONTRACT_MARKER not in body["message"]
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), 0.1)])
    with pytest.raises(FlowUnavailable): client.call(valid_context())
    assert len(opener.calls) == 1


def test_marker_inside_failed_model_text_cannot_replace_the_outer_original_candidate(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    candidate = failed_candidate()
    # Literal untrusted marker text must not be mistaken for the generated
    # terminal carrier. JSON braces here would instead be an unknown fact slot
    # and correctly produce a different, deliberately nonrecoverable error.
    candidate["reply_plan"]["text"] += ' '+CONTRACT_MARKER+'version=999,origin=fake'
    body = generated_error_carrier(valid_context(), candidate)
    _stage, success = successful_stage(valid_context())
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), 0.1), (success, 0.1)])
    client.call(valid_context())
    assert sent_context(opener.calls[1])["validation_feedback"]["previous_output"] == candidate
    assert_private_evidence(client, caplog)


def test_candidate_utf8_limit_is_not_a_character_count_or_http_body_limit(monkeypatch):
    context = valid_context()
    context_ref = successful_stage(context)[0]["proposal"]["render_plan"]["context_ref"]
    candidates = []
    for size in range(1400, 1501):
        value = {"schema_version":"2.2", "reply_plan":{"kind":"REPLY", "quote_value":"-123456789012.12",
            "question":"NEGOTIATE", "context_ref":context_ref, "text":"😀"*size}}
        length = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        candidates.append((length, value))
    below = max((item for item in candidates if item[0] <= 6144), key=lambda item:item[0])
    above = min((item for item in candidates if item[0] > 6144), key=lambda item:item[0])
    assert len(above[1]["reply_plan"]["text"]) <= 1500
    _stage, success = successful_stage(context)
    body = generated_error_carrier(context, below[1])
    assert structured_payload(body)["previous_output"] == below[1]
    client, opener, _clock = make_client(monkeypatch, [(http_error(body), 0.1), (success, 0.1)])
    client.call(context)
    assert len(opener.calls) == 2
    too_large = generated_error_carrier(context, above[1])
    assert CONTRACT_MARKER not in too_large["message"], "The real wrapper must not emit an oversized candidate"
    client, opener, _clock = make_client(monkeypatch, [(http_error(too_large), 0.1)])
    with pytest.raises(FlowUnavailable):client.call(context)
    assert len(opener.calls) == 1


@pytest.mark.parametrize("configured", [90, 240, 300, 17.5, 1])
def test_first_opener_timeout_is_exact_and_second_request_uses_the_original_deadline(monkeypatch, configured):
    class AdvancingClock(Clock):
        def __call__(self):
            self.now += .001
            return self.now
    clock = AdvancingClock()
    monkeypatch.setattr("server.negotiation.chatflow.time.sleep", clock.sleep)
    context = valid_context(); context["_stage_budget_seconds"] = configured
    _stage, success = successful_stage(context)
    opener = ScriptedOpener(clock, [(http_error(carrier()), configured*.1), (success, .01)])
    client = ChatflowClient("https://synthetic.invalid", "SYNTHETIC_APP_SECRET", stage="reply",
                            timeout=configured, opener=opener, clock=clock)
    client.call(context)
    assert opener.calls[0]["timeout"] == configured
    assert 0 < opener.calls[1]["timeout"] < configured-configured*.1
    assert len(opener.calls) == 2


@pytest.mark.parametrize("version", ["0.15.3", "1.17.1"])
def test_precise_feedback_remains_valid_in_real_generated_projection_and_correction_code(monkeypatch, version):
    context = valid_context()
    candidate = failed_candidate()
    expected, success = successful_stage(context)
    client, opener, _clock = make_client(monkeypatch,
        [(http_error(generated_error_carrier(context, candidate, version)), 0.1), (success, 0.1)])
    assert client.call(context) == expected
    corrected = sent_context(opener.calls[1])
    graph = build("reply", version=version, provider="synthetic", model="synthetic")["workflow"]["graph"]
    nodes = {row["id"]: row["data"] for row in graph["nodes"]}
    projection, contract = {}, {}
    exec(nodes["context"]["code"], projection)
    exec(nodes["contract"]["code"], contract)
    projected = projection["main"](json.dumps(corrected, ensure_ascii=False))
    assert corrected["validation_feedback"] == {"mode":"OUTPUT_CONTRACT","attempt":1,"max_attempts":1,
        "reason_code":"DIFY_MODEL_CONTRACT_SLOTS","previous_output":candidate}
    model_candidate = deepcopy(candidate)
    del model_candidate["reply_plan"]["text"]
    assert json.loads(projected["context"])["validation_feedback"]["previous_output"] == model_candidate
    assert corrected["validation_feedback"]["previous_output"] == candidate
    good_model_output = {"schema_version": "2.2", "reply_plan": {
        "kind": "REPLY", "quote_value": "1.55", "text": "我再靠近些，{own_quote}，你再看看？", "question": "NONE"},
        "trader_view": {}}
    decoded = json.loads(contract["main"](json.dumps(good_model_output, ensure_ascii=False),
                                         json.dumps(corrected, ensure_ascii=False))["result"])
    assert decoded == expected
    assert Engine.__new__(Engine)._validate_reply(context, decoded["proposal"])["our_price"]["value"] == "1.55"


def test_private_candidate_never_appears_in_terminal_exception_or_debug_evidence(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client, opener, _clock = make_client(monkeypatch,
        [(http_error(carrier()), 0.1), (http_error(carrier()), 0.1)])
    with pytest.raises(FlowUnavailable) as caught:
        client.call(valid_context())
    assert len(opener.calls) == 2
    assert getattr(caught.value, "_previous_output", None) == failed_candidate()
    assert_private_evidence(client, caplog, caught.value)
    assert "previous_output" not in json.dumps(client.last_evidence)


@pytest.mark.parametrize("second", [
    http_error({"code":"invalid_param", "status":400, "message":"ordinary second failure"}),
    http_error({"message":"SYNTHETIC_PRIVATE_HTTP_BODY"}, 401),
    TimeoutError("SYNTHETIC_PRIVATE_HTTP_BODY"),
    b'{"answer":"not-json"}',
])
def test_second_failure_is_not_hidden_by_an_automatic_wait_or_third_attempt(monkeypatch, caplog, second):
    caplog.set_level(logging.DEBUG)
    client, opener, _clock = make_client(monkeypatch,
        [(http_error(carrier()), 0.1), (second, 0.1)])
    with pytest.raises(FlowUnavailable) as caught:
        client.call(valid_context())
    assert len(opener.calls) == 2
    assert client.last_evidence["request_attempts"] == 2
    assert client.last_evidence["retry_failures"][0]["error_code"] == "DIFY_MODEL_CONTRACT_SLOTS"
    assert_private_evidence(client, caplog, caught.value)
