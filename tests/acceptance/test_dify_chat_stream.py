"""Complete Chatflow transport results only; chunked SSE never grants send authority."""
from copy import deepcopy
import io
import json
import urllib.error

import pytest

from server.dify_client import DifyChatClient, DifyClientError


def proposal():
    return {"schema_version": "1", "turn_id": "turn-synthetic", "conversation_epoch": 1,
            "source_message_id": "incoming-synthetic", "observations": [],
            "proposed_actions": [{"type": "WAIT"}], "public_message": None,
            "trader_view": {"reason": "等待可验证报价"}, "memory_patch": None,
            "history_request": None, "validation_ref": None}


IDS = {"conversation_id": "conversation-synthetic", "message_id": "message-synthetic",
       "task_id": "task-synthetic", "workflow_run_id": "run-synthetic"}


def event(kind, **data):
    return {"event": kind, **IDS, **data}


def stream(events):
    return b": heartbeat\n\n" + b"".join(b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n" for value in events)


def valid_events(value=None):
    text = json.dumps(proposal() if value is None else value, ensure_ascii=False)
    return [event("agent_thought", thought="PRIVATE-THOUGHT-DO-NOT-EXPOSE"),
            event("message", answer=text[:31]), event("message", answer=text[31:]),
            event("message_end"), event("workflow_finished", data={"status": "succeeded", "id": "run-synthetic"})]


class Response(io.BytesIO):
    def __init__(self, body, content_type="text/event-stream"):
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Type": content_type}

    def read1(self, size=-1):
        return super().read(7 if size < 0 else min(size, 7))


def chat(client, **kwargs):
    return client.chat(query="对手原文", user="trusted-user-and-account", inputs={"turn_ref": "opaque-turn", "context_epoch": "1", "schema_version": "1"}, **kwargs)


def test_seven_byte_stream_chunks_produce_one_complete_proposal_and_hide_thoughts():
    client = DifyChatClient("https://dify.invalid", "synthetic-app-secret", opener=lambda *_a, **_k: Response(stream(valid_events())))
    result = chat(client)
    assert result.proposal == proposal()
    assert result.conversation_id == IDS["conversation_id"]
    assert result.workflow_run_id == IDS["workflow_run_id"]
    assert "PRIVATE-THOUGHT" not in repr(result)


def test_both_success_terminals_are_required_but_their_order_is_not():
    values = valid_events()
    values[-2:] = reversed(values[-2:])
    result = chat(DifyChatClient("https://dify.invalid/v1", "synthetic-secret", opener=lambda *_a, **_k: Response(stream(values))))
    assert result.proposal == proposal()


@pytest.mark.parametrize("change", ["missing-message-end", "missing-workflow-success", "failed-workflow", "error-after-success", "partial-json"])
def test_incomplete_or_failed_stream_never_returns_an_actionable_result(change):
    values = valid_events()
    if change == "missing-message-end":
        values = [value for value in values if value["event"] != "message_end"]
    elif change == "missing-workflow-success":
        values = [value for value in values if value["event"] != "workflow_finished"]
    elif change == "failed-workflow":
        values[-1]["data"]["status"] = "failed"
    elif change == "error-after-success":
        values.append(event("error", message="PRIVATE-ERROR-SECRET"))
    else:
        values[2]["answer"] = values[2]["answer"][:-4]
    client = DifyChatClient("https://dify.invalid", "synthetic-secret", opener=lambda *_a, **_k: Response(stream(values)))
    with pytest.raises(DifyClientError) as error:
        chat(client)
    assert "PRIVATE-ERROR-SECRET" not in str(error.value)


def test_complete_json_without_successful_stream_end_is_not_success():
    values = [event("message", answer=json.dumps(proposal()))]
    client = DifyChatClient("https://dify.invalid", "synthetic-secret", opener=lambda *_a, **_k: Response(stream(values)))
    with pytest.raises(DifyClientError):
        chat(client)


def test_message_replace_discards_the_old_draft_instead_of_appending():
    values = [event("message", answer="unfinished draft"), event("message_replace", answer=json.dumps(proposal())),
              event("workflow_finished", data={"status": "succeeded", "id": "run-synthetic"}), event("message_end")]
    client = DifyChatClient("https://dify.invalid", "synthetic-secret", opener=lambda *_a, **_k: Response(stream(values)))
    assert chat(client).proposal == proposal()


def test_new_inputs_are_sent_on_each_chat_turn_with_trusted_identity():
    captured = []

    def opener(request, **kwargs):
        captured.append(request)
        return Response(stream(valid_events()))

    client = DifyChatClient("https://dify.invalid/v1", "private-app-key", opener=opener)
    for number in (1, 2):
        client.chat(query="合成原文", user="owner-a/account-a/peer-a", inputs={"turn_ref": f"ref-{number}", "context_epoch": str(number)},
                    conversation_id=IDS["conversation_id"])
    assert len(captured) == 2
    for number, request in enumerate(captured, 1):
        assert request.full_url == "https://dify.invalid/v1/chat-messages"
        assert request.get_header("Authorization") == "Bearer private-app-key"
        data = json.loads(request.data)
        assert data["inputs"]["turn_ref"] == f"ref-{number}"
        assert data["inputs"]["context_epoch"] == str(number)
        assert data["user"] == "owner-a/account-a/peer-a"
        assert data["conversation_id"] == IDS["conversation_id"]
        assert "private-app-key" not in request.data.decode()


def test_silent_new_conversation_is_rejected():
    client = DifyChatClient("https://dify.invalid", "synthetic-secret", opener=lambda *_a, **_k: Response(stream(valid_events())))
    with pytest.raises(DifyClientError):
        chat(client, conversation_id="existing-other-conversation")


@pytest.mark.parametrize("change", ["unvalidated-public", "history-and-counter", "wrong-epoch-type", "extra-top-level"])
def test_untrusted_business_envelope_is_rejected_even_with_successful_sse(change):
    value = deepcopy(proposal())
    if change == "unvalidated-public":
        value["public_message"] = "我方可报2.48"
    elif change == "history-and-counter":
        value["history_request"] = {"keywords": ["昨天"]}
        value["proposed_actions"] = [{"type": "COUNTER", "yield_pct": "2.48"}]
    elif change == "wrong-epoch-type":
        value["conversation_epoch"] = True
    else:
        value["send_now"] = True
    client = DifyChatClient("https://dify.invalid", "synthetic-secret", opener=lambda *_a, **_k: Response(stream(valid_events(value))))
    with pytest.raises(DifyClientError):
        chat(client)


def test_provider_exception_does_not_leak_app_key_or_internal_diagnostics(caplog):
    def opener(*_args, **_kwargs):
        raise urllib.error.URLError("private-app-key private-account-diagnostic")

    with pytest.raises(DifyClientError) as error:
        chat(DifyChatClient("https://dify.invalid", "private-app-key", opener=opener))
    assert "private-app-key" not in str(error.value) + caplog.text
    assert "private-account-diagnostic" not in str(error.value) + caplog.text


def test_response_size_budget_rejects_oversized_stream():
    client = DifyChatClient("https://dify.invalid", "synthetic-secret", max_response_bytes=100,
                            opener=lambda *_a, **_k: Response(stream(valid_events())))
    with pytest.raises(DifyClientError) as error:
        chat(client)
    assert error.value.code == "RESPONSE_TOO_LARGE"
