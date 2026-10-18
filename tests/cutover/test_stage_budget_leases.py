"""Configured model stages keep bounded leases and fence old workers."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import server.negotiation.engine as engine_module
from server.negotiation.engine import Engine
from test_production_flow import OWNER, desk, raw
from test_acceptance_contract import current, observation
from test_private_history_requests import history_desk, request, scope_only
from test_persistent_retry_http import latest_row


def configured(app, flows, seconds):
    return Engine(app.state.inquiry, flows, app.state.bond_tools,
                  aggregation=app.state.negotiation_engine.aggregation,
                  stage_budget_seconds=seconds)


@pytest.mark.parametrize("stage", ["analysis", "reply"])
def test_360_second_budget_preserves_lease_after_181_seconds_and_history_round(desk, monkeypatch, stage):
    app, client, flows, _, clock = desk
    _, peer, anchor, _ = history_desk(desk)
    incoming = raw(app, peer["thread_id"], "SYN-A 3.10出500万", seq=23)
    def final_analysis(snapshot):
        msg = next(m for m in snapshot["messages"] if m["message_id"] == incoming)
        return {"schema_version": "2.2", "scopes": {incoming: [peer["member_id"]]},
                "observations": [observation(peer, msg)], "unresolved": {}}
    flows.analyze = final_analysis
    if stage == "reply":
        assert app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])["facts_committed"]
        assert client.post("/api/bot/toggle", json={"status": "正常"}).status_code == 200
    monkeypatch.setattr(engine_module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    seen = []
    def call(context):
        seen.append(deepcopy(context))
        if len(seen) == 1:
            clock.value += 181
            return request(peer["member_id"], [anchor["message_id"]])
        assert 0 < context["_stage_budget_seconds"] <= 179.001
        if stage == "analysis":
            return final_analysis(context)
        return {"schema_version": "2.2", "action": "SEND", "proposal": {
            "kind": "REPLY", "text": "3.19%收500万", "our_price": {"kind": "YIELD", "value": "3.19"}}}
    setattr(flows, "analyze" if stage == "analysis" else "decide", call)
    engine = configured(app, flows, 360)
    # This two-round lease regression explicitly replays the old fetch policy.
    engine.context_policy_revision = 'explicit_anchors_v0'
    result = engine.analyze_thread(OWNER, peer["thread_id"]) if stage == "analysis" else engine.decide_member(OWNER, peer["member_id"])
    assert len(seen) == 2
    if stage == "analysis":
        assert result["facts_committed"] and not result["rejected"], result
        row = latest_row(app.state.repository, "xj2_analysis")
        assert row["state"] == "COMMITTED" and row["lease_until"] > clock.value
    else:
        assert result["state"] == "QUEUED", result
        row = latest_row(app.state.repository, "xj2_reply_job")
        assert row["state"] == "DONE" and row["attempts"] == 1
    assert current(app, peer).revision == 1 and current(app, peer).price.value == "3.10"


@pytest.mark.parametrize("with_history_request", [False, True])
def test_exceeded_stage_budget_cannot_commit_late_model_result(desk, monkeypatch, with_history_request):
    app, _, flows, _, clock = desk
    _, peer, anchor, _ = history_desk(desk)
    raw(app, peer["thread_id"], "SYN-A 3.10出500万", seq=23)
    monkeypatch.setattr(engine_module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    calls = []
    def analyze(snapshot):
        calls.append(snapshot)
        clock.value += 361
        if with_history_request:
            return request(peer["member_id"], [anchor["message_id"]])
        return {"schema_version": "2.2", "scopes": {snapshot["messages"][-1]["message_id"]: [peer["member_id"]]},
                "observations": [observation(peer, snapshot["messages"][-1])], "unresolved": {}}
    flows.analyze = analyze
    result = configured(app, flows, 360).analyze_thread(OWNER, peer["thread_id"])
    assert result["facts_committed"] is False and len(calls) == 1
    assert current(app, peer).revision == 0
    assert app.state.repository.snapshot(OWNER)["threads"][0]["analysis_pending"]


def test_expired_analysis_lease_reclaimed_by_another_worker_fences_original_model(desk):
    app, _, flows, _, clock = desk
    _, peer, _, _ = history_desk(desk)
    raw(app, peer["thread_id"], "SYN-A 3.10出500万", seq=23)
    lease = {}
    def analyze(snapshot):
        before = latest_row(app.state.repository, "xj2_analysis")
        clock.value = before["lease_until"] + 0.01
        newer = app.state.repository.claim_analysis(OWNER, peer["thread_id"], force=True, lease_seconds=390)
        assert newer["analysis_id"] == before["analysis_id"]
        assert newer["lease_token"] != before["lease_token"]
        lease.update(newer)
        return scope_only(snapshot)
    flows.analyze = analyze
    result = configured(app, flows, 360).analyze_thread(OWNER, peer["thread_id"])
    assert result["facts_committed"] is False
    after = latest_row(app.state.repository, "xj2_analysis")
    assert after["state"] == "RUNNING" and after["lease_token"] == lease["lease_token"]
    assert after["attempts"] == 2
    assert current(app, peer).revision == 0


@pytest.mark.parametrize("invalid", [0, 541, float("nan"), True])
def test_stage_budget_rejects_unbounded_or_non_numeric_configuration(desk, invalid):
    app, _, flows, _, _ = desk
    with pytest.raises(ValueError):
        configured(app, flows, invalid)
