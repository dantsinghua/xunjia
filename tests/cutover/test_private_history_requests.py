"""Private history retrieval must stay scoped, bounded and evidence backed."""
from copy import deepcopy

import pytest

from server.negotiation.prices import LimitPolicy
from test_production_flow import OWNER, desk, raw
from test_acceptance_contract import current, observation, setup_groups


def scope_only(snapshot):
    return {"schema_version": "2.2", "scopes": {
        m["message_id"]: [p["member_id"] for p in snapshot["members"] if p["bond_code"] in m["text"]]
        for m in snapshot["messages"]}, "observations": [], "unresolved": {}}


def history_desk(desk, count=22):
    app, client, flows, _, _ = desk
    group = setup_groups(desk, stopped=False)["SYN-A"]
    peer = group["peers"][0]
    anchor = next(m for m in app.state.repository.context(OWNER, peer["member_id"]) if m["direction"] == "out")
    assert anchor["native_id"]
    assert client.post("/api/bot/toggle", json={"status": "停止"}).status_code == 200
    flows.analyze = scope_only
    ids = [raw(app, peer["thread_id"], "SYN-A 历史交流", seq=n) for n in range(1, count + 1)]
    app.state.negotiation_engine.run_once()
    default = app.state.repository.context(OWNER, peer["member_id"])
    assert len(default) == 20 and anchor["message_id"] not in {m["message_id"] for m in default}
    return group, peer, anchor, ids


def request(mid, ids):
    return {"schema_version": "2.2", "history_requests": [{"member_id": mid, "message_ids": ids}]}


def test_analysis_fetches_real_old_native_anchor_without_evicting_default_twenty(desk):
    app, _, flows, _, _ = desk
    _, peer, anchor, _ = history_desk(desk)
    incoming = raw(app, peer["thread_id"], "SYN-A 按你那条历史报价出500万", seq=23)
    seen = []
    def analyze(snapshot):
        seen.append(deepcopy(snapshot))
        if len(seen) == 1:
            return request(peer["member_id"], [anchor["message_id"]])
        member = next(p for p in snapshot["members"] if p["member_id"] == peer["member_id"])
        assert {m["message_id"] for m in seen[0]["members"][0]["context"]} <= {m["message_id"] for m in member["context"]}
        assert len(member["context"]) == 21
        history = snapshot["history_results"]
        assert len(history) == 1 and history[0]["member_id"] == peer["member_id"]
        assert history[0]["message_ids"] == [anchor["message_id"]]
        assert [m["message_id"] for m in history[0]["messages"]] == [anchor["message_id"]]
        msg = next(m for m in snapshot["messages"] if m["message_id"] == incoming)
        value = observation(peer, msg, price="3.20", volume="500", segment=msg["text"])
        value["evidence"]["price"] = [anchor["message_id"], incoming]
        value["segments"].append({"message_id": anchor["message_id"], "text": anchor["text"]})
        return {"schema_version": "2.2", "scopes": {incoming: [peer["member_id"]]}, "observations": [value], "unresolved": {}}
    flows.analyze = analyze
    saved = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert saved["facts_committed"] and not saved["rejected"], saved
    assert len(seen) == 2
    assert current(app, peer).price.value == "3.20" and current(app, peer).volume_wan == "500"
    assert anchor["message_id"] in current(app, peer).evidence["price"]


def test_reply_stage_fetches_old_anchor_and_preserves_its_default_context(desk):
    app, client, flows, transport, _ = desk
    _, peer, anchor, _ = history_desk(desk)
    incoming = raw(app, peer["thread_id"], "SYN-A 3.10出500万", seq=23)
    flows.analyze = lambda snap: {"schema_version": "2.2", "scopes": {incoming: [peer["member_id"]]},
        "observations": [observation(peer, snap["messages"][-1])], "unresolved": {}}
    assert app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])["facts_committed"]
    assert client.post("/api/bot/toggle", json={"status": "正常"}).status_code == 200
    seen = []
    def decide(context):
        seen.append(deepcopy(context))
        if len(seen) == 1:
            assert len(context["context"]) == 20
            return request(peer["member_id"], [anchor["message_id"]])
        assert {m["message_id"] for m in seen[0]["context"]} <= {m["message_id"] for m in context["context"]}
        assert len(context["context"]) == 21
        assert [m["message_id"] for m in context["history_results"][0]["messages"]] == [anchor["message_id"]]
        return {"schema_version": "2.2", "action": "SEND", "proposal": {
            "kind": "REPLY", "text": "3.19%收500万", "our_price": {"kind": "YIELD", "value": "3.19"}}}
    flows.decide = decide
    result = app.state.negotiation_engine.decide_member(OWNER, peer["member_id"])
    assert result["state"] == "QUEUED" and len(seen) == 2, result
    app.state.dispatcher.run_once()
    assert len(transport.sent) == 2


def scoped_message(store, owner, mid, tid, *, native=True):
    value = store.ingest(owner, tid, event_id="private-anchor", source_seq=99,
        text="PRIVATE_HISTORY_MUST_NOT_LEAK", trade_day="2026-10-17", direction="out",
        native_id="synthetic-private-native" if native else None)
    job = store.claim_analysis(owner, tid, force=True)
    scopes = {m["message_id"]: [mid] for m in job["snapshot"]["messages"]}
    store.commit_analysis(owner, job["analysis_id"], job["lease_token"], scopes=scopes, observations=[])
    return value


@pytest.mark.parametrize("scope", ["owner", "thread", "member", "draft"])
def test_analysis_rejects_private_anchor_outside_member_or_without_native_evidence(desk, scope):
    app, _, flows, _, _ = desk
    group, peer, _, _ = history_desk(desk)
    store = app.state.repository
    owner, mid, tid = OWNER, peer["member_id"], peer["thread_id"]
    if scope == "owner":
        owner = "another-synthetic-owner"
        gid = store.create_group(owner, bond_code="SYN-A", our_side="BUY", target_volume="1000", trade_day="2026-10-17", policy=LimitPolicy("ABSOLUTE", "3.12"))
        mid, tid = store.add_member(owner, gid, account_uid="another-synthetic-account", peer_uid="peer-a")
    elif scope == "thread":
        mid, tid = store.add_member(OWNER, group["group_id"], account_uid="synthetic-account", peer_uid="peer-b")
    elif scope == "member":
        gid = store.create_group(OWNER, bond_code="SYN-B", our_side="BUY", target_volume="1000", trade_day="2026-10-17", policy=LimitPolicy("ABSOLUTE", "3.12"))
        mid, tid = store.add_member(OWNER, gid, account_uid="synthetic-account", peer_uid="peer-a")
    denied = scoped_message(store, owner, mid, tid, native=scope != "draft")
    raw(app, peer["thread_id"], "SYN-A 请看历史", seq=100)
    calls = []
    def analyze(snapshot):
        calls.append(deepcopy(snapshot))
        return request(peer["member_id"], [denied])
    flows.analyze = analyze
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert result["facts_committed"] is False
    assert len(calls) == 1, "Rejected private history must not be delivered in another model call"
    assert not calls[0].get("history_results")


def test_future_anchor_after_fixed_analysis_snapshot_is_not_retrieved(desk):
    app, _, flows, _, _ = desk
    _, peer, _, _ = history_desk(desk)
    raw(app, peer["thread_id"], "SYN-A 当前请求", seq=23)
    seen = []
    def analyze(snapshot):
        seen.append(deepcopy(snapshot))
        if len(seen) > 1:
            return scope_only(snapshot)
        future = raw(app, peer["thread_id"], "SYN-A 新到的未来锚点", seq=24)
        store = app.state.repository
        store.correct_association(OWNER, peer["thread_id"], future, [peer["member_id"]],
            store.snapshot(OWNER)["revision"], "synthetic concurrent human association")
        return request(peer["member_id"], [future])
    flows.analyze = analyze
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert result["facts_committed"] is False
    assert len(seen) == 1


def test_identical_history_request_stops_after_second_model_response(desk):
    app, _, flows, _, _ = desk
    _, peer, anchor, _ = history_desk(desk)
    raw(app, peer["thread_id"], "SYN-A 重复补取", seq=23)
    seen = []
    def analyze(snapshot):
        seen.append(deepcopy(snapshot))
        assert len(seen) <= 2, "Identical history requests looped beyond the declared bound"
        return request(peer["member_id"], [anchor["message_id"]])
    flows.analyze = analyze
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert result["facts_committed"] is False and len(seen) == 2
    assert len(seen[1]["history_results"]) == 1


@pytest.mark.parametrize("amount", [0, 31])
def test_history_request_requires_one_to_thirty_explicit_ids(desk, amount):
    app, _, flows, _, _ = desk
    _, peer, anchor, _ = history_desk(desk)
    raw(app, peer["thread_id"], "SYN-A 有界补取", seq=23)
    seen = []
    def analyze(snapshot):
        seen.append(snapshot)
        return request(peer["member_id"], [anchor["message_id"]] * amount)
    flows.analyze = analyze
    assert app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])["facts_committed"] is False
    assert len(seen) == 1


def test_private_history_and_public_tools_share_six_request_budget(desk):
    app, _, flows, _, _ = desk
    _, peer, anchor, ids = history_desk(desk)
    raw(app, peer["thread_id"], "SYN-A 总预算", seq=23)
    calls = []
    def analyze(snapshot):
        calls.append(deepcopy(snapshot))
        turn = len(calls)
        assert turn <= 7
        if turn == 1:
            return request(peer["member_id"], [anchor["message_id"]])
        if turn <= 6:
            return {"schema_version": "2.2", "tool_requests": [{"name": "bond_info", "arguments": {
                "schema_version": "1", "mode": "search", "query": "synthetic", "limit": 1}}]}
        return request(peer["member_id"], [ids[0]])
    flows.analyze = analyze
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert result["facts_committed"] is False and len(calls) == 7
    assert len(calls[-1]["tool_results"]) == 5


def test_one_member_cannot_accumulate_more_than_thirty_unique_anchors(desk):
    app, _, flows, _, _ = desk
    _, peer, _, ids = history_desk(desk, count=60)
    raw(app, peer["thread_id"], "SYN-A 分批补取", seq=61)
    seen = []
    def analyze(snapshot):
        seen.append(deepcopy(snapshot))
        assert len(seen) <= 2
        return request(peer["member_id"], ids[:20] if len(seen) == 1 else ids[20:31])
    flows.analyze = analyze
    result = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert result["facts_committed"] is False and len(seen) == 2


def test_reply_cannot_switch_history_request_to_another_member_in_same_thread(desk):
    app, client, flows, _, _ = desk
    _, peer, _, _ = history_desk(desk)
    store = app.state.repository
    gid = store.create_group(OWNER, bond_code="SYN-B", our_side="BUY", target_volume="1000", trade_day="2026-10-17", policy=LimitPolicy("ABSOLUTE", "3.12"))
    other, thread = store.add_member(OWNER, gid, account_uid="synthetic-account", peer_uid="peer-a")
    private_id = scoped_message(store, OWNER, other, thread)
    incoming = raw(app, peer["thread_id"], "SYN-A 3.10出500万", seq=100)
    flows.analyze = lambda snapshot: {"schema_version": "2.2", "scopes": {incoming: [peer["member_id"]]},
        "observations": [observation(peer, snapshot["messages"][-1])], "unresolved": {}}
    assert app.state.negotiation_engine.analyze_thread(OWNER, thread)["facts_committed"]
    assert client.post("/api/bot/toggle", json={"status": "正常"}).status_code == 200
    calls = []
    def decide(context):
        calls.append(deepcopy(context))
        return request(other, [private_id])
    flows.decide = decide
    result = app.state.negotiation_engine.decide_member(OWNER, peer["member_id"])
    assert result["state"] in ("RETRY", "REJECTED") and len(calls) == 1
    assert not calls[0].get("history_results")
