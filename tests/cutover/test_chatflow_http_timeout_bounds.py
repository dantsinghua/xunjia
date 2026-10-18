"""Measured slow stages may configure HTTP budgets without unbounded waits."""
import json

import pytest

from server.negotiation.chatflow import ChatflowClient


RESULT = {"schema_version": "2.2", "scopes": {}, "observations": [], "unresolved": {}}


class Response:
    status = 200
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size):
        return json.dumps({"answer": json.dumps(RESULT), "message_id": "synthetic-budget-response"}).encode()


class RecordingOpener:
    def __init__(self):
        self.calls = []
    def open(self, request, *, timeout):
        self.calls.append({"url": request.full_url, "timeout": timeout, "body": json.loads(request.data)})
        return Response()


@pytest.mark.parametrize("configured,stage_budget", [(240, 540), (300, None)])
def test_explicit_supported_http_budget_is_passed_to_actual_opener(configured, stage_budget):
    opener = RecordingOpener()
    client = ChatflowClient("https://synthetic.invalid/v1", "synthetic-budget-key",
                            stage="analysis", timeout=configured, opener=opener)
    context = {"thread_id": "synthetic-budget-thread"}
    if stage_budget is not None:
        context["_stage_budget_seconds"] = stage_budget
    assert client.call(context) == RESULT
    assert len(opener.calls) == 1
    assert opener.calls[0]["timeout"] == configured
    assert client.last_evidence["message_id"] == "synthetic-budget-response"


def test_remaining_stage_budget_still_clamps_a_240_second_http_budget():
    opener = RecordingOpener()
    client = ChatflowClient("https://synthetic.invalid/v1", "synthetic-budget-key",
                            stage="analysis", timeout=240, opener=opener)
    assert client.call({"_stage_budget_seconds": 17.5}) == RESULT
    assert opener.calls[0]["timeout"] == 17.5


def test_default_http_timeout_remains_ninety_seconds():
    opener = RecordingOpener()
    client = ChatflowClient("https://synthetic.invalid/v1", "synthetic-budget-key", stage="analysis", opener=opener)
    assert client.call({}) == RESULT
    assert client.timeout == 90 and opener.calls[0]["timeout"] == 90


@pytest.mark.parametrize("invalid", [300.001, float("inf"), float("nan"), True])
def test_http_timeout_rejects_over_maximum_nonfinite_and_bool(invalid):
    opener = RecordingOpener()
    with pytest.raises(ValueError, match="timeout"):
        ChatflowClient("https://synthetic.invalid/v1", "synthetic-budget-key",
                       stage="analysis", timeout=invalid, opener=opener)
    assert opener.calls == []
