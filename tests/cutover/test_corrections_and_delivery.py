"""Independent corrections, actual-entry delivery and lifecycle acceptance."""
import json
from types import SimpleNamespace

import pytest

from test_production_flow import NOW, OWNER, confirm, desk, draft, item, raw
from test_acceptance_contract import current, observation, parser, setup_groups


def test_association_correction_revokes_wrong_contribution_without_inventing_new_price(desk):
    app, client, flows, _, _ = desk
    groups = setup_groups(desk, two_bonds=True)
    a, b = groups["SYN-A"]["peers"][0], groups["SYN-B"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(a, msg, segment=msg["text"])])
    message_id = raw(app, a["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    assert current(app, a).price.value == "3.10"
    revision = app.state.repository.snapshot(OWNER)["revision"]
    body = {"thread_id": a["thread_id"], "member_ids": [b["member_id"]],
            "expected_revision": revision, "reason": "交易员核对原消息实际属于B券"}
    path = "/api/negotiation/messages/" + message_id + "/association"
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert current(app, a).price is None
    assert app.state.repository.group(OWNER, groups["SYN-A"]["group_id"])["summary"]["reported_volume_wan"] == "0"
    assert current(app, b).price is None, "Association correction is not permission to fabricate a new bond quote"
    history = client.get("/api/negotiation/messages/" + a["thread_id"]).json()["data"]["messages"]
    original = next(row for row in history if row["message_id"] == message_id)
    assert original["text"] == "SYN-A 3.10出500万"
    assert {ctx["member_id"] for ctx in original["contexts"] if ctx["relation"] == "MENTION"} == {b["member_id"]}
    stale = client.post(path, json={**body, "member_ids": [a["member_id"]]})
    assert stale.status_code in (400, 409), stale.text


def test_clarification_resolves_only_uncertain_fragment_and_recovers_quoted_anchor(desk):
    app, _, flows, _, _ = desk
    groups = setup_groups(desk, two_bonds=True)
    a, b = groups["SYN-A"]["peers"][0], groups["SYN-B"]["peers"][0]
    text = "SYN-A 3.10出500万；另一只3.15出800万"
    message_id = raw(app, a["thread_id"], text)
    def first(snapshot):
        msg = next(m for m in snapshot["messages"] if m["message_id"] == message_id)
        return {"schema_version": "2.2", "scopes": {message_id: [a["member_id"]]},
                "unresolved": {message_id: [b["member_id"]]},
                "observations": [observation(a, msg, segment="SYN-A 3.10出500万")]}
    flows.analyze = first
    app.state.negotiation_engine.run_once()
    assert current(app, a).price.value == "3.10"
    assert current(app, b).price is None
    clarification = raw(app, a["thread_id"], "另一只是SYN-B", seq=2)
    def second(snapshot):
        msg = next(m for m in snapshot["messages"] if m["message_id"] == clarification)
        value = observation(b, msg, price="3.15", volume="800", segment=msg["text"])
        value["segments"].insert(0, {"message_id": message_id, "text": "另一只3.15出800万"})
        value["evidence"] = {key: [message_id, clarification] for key in value["changes"]}
        return {"schema_version": "2.2", "scopes": {clarification: [b["member_id"]]},
                "observations": [value], "unresolved": {}, "corrections": [
                    {"message_id": message_id, "member_ids": [a["member_id"], b["member_id"]],
                     "evidence": [clarification]},
                ]}
    flows.analyze = second
    app.state.negotiation_engine.run_once()
    assert current(app, a).price.value == "3.10"
    assert current(app, b).price is not None
    assert current(app, b).price.value == "3.15" and current(app, b).volume_wan == "800"
    with app.state.repository.db.transaction(write=False) as tx:
        rows = tx.execute("SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?", (message_id, b["member_id"])).fetchall()
    assert rows and all(row["relation"] != "UNRESOLVED" for row in rows)


def test_retracting_quantity_update_replays_other_still_valid_field_evidence(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg)])
    price_message = raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    def quantity(snapshot, msg):
        return [{"member_id": peer["member_id"], "side": "ofr",
                 "changes": {"volume_wan": "300"}, "evidence": {"volume_wan": [msg["message_id"]]}}]
    flows.analyze = parser(quantity)
    raw(app, peer["thread_id"], "SYN-A 出量改300万", seq=2)
    app.state.negotiation_engine.run_once()
    assert current(app, peer).volume_wan == "300"
    raw(app, peer["thread_id"], "撤回刚才的改量", seq=3, kind="retract", target_event_id="event-2")
    quote = current(app, peer)
    assert quote.price.value == "3.10" and quote.evidence["price"] == [price_message]
    assert quote.volume_wan == "500", "Withdrawn 300 update must replay the still-valid earlier 500 field"
    assert quote.validity == "ACTIVE"


def test_unknown_receipt_keeps_analysis_active_and_never_resubmits_command(desk):
    app, _, flows, transport, clock = desk
    groups = setup_groups(desk, stopped=False)
    peer = groups["SYN-A"]["peers"][0]
    original_send = transport.send
    def uncertain(command):
        result = original_send(command)
        return {**result, "status": "unknown", "native_id": None, "evidence": "synthetic uncertain"}
    transport.send = uncertain
    raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    app.state.dispatcher.run_once()
    cid = transport.sent[-1]["command_id"]
    assert app.state.inquiry.commands_core.get(OWNER, cid)["state"] == "UNKNOWN"
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, price="3.11", volume="600")])
    raw(app, peer["thread_id"], "SYN-A 改3.11出600万", seq=2)
    app.state.negotiation_engine.run_once()
    assert current(app, peer).price.value == "3.11" and current(app, peer).volume_wan == "600"
    assert app.state.inquiry.commands_core.get(OWNER, cid)["state"] == "UNKNOWN"
    assert len(transport.sent) == 2
    assert app.state.inquiry.commands_core.claim(OWNER, cid) is None


def test_new_intake_after_dispatch_claim_fails_native_execution_permission(desk):
    app, _, flows, transport, _ = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    queued = next(row for row in app.state.inquiry.commands(OWNER) if row["state"] == "QUEUED")
    commands = app.state.inquiry.commands_core
    assert commands.claim(OWNER, queued["command_id"]) is not None
    raw(app, peer["thread_id"], "SYN-A撤价", seq=2)
    assert commands.execution_permission(OWNER, queued["command_id"]) is False
    row = commands.get(OWNER, queued["command_id"])
    assert row["state"] == "PENDING" and row["cancel_requested"] == 1
    assert len(transport.sent) == 1


def test_unresponsive_state_is_updated_even_when_all_sending_is_stopped(desk):
    app, client, flows, _, clock = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    # The sent initial question is parsed before advancing the deterministic clock.
    app.state.negotiation_engine.run_once()
    assert client.post("/api/bot/toggle", json={"status": "停止"}).status_code == 200
    clock.value += 3600
    app.state.negotiation_engine.run_once()
    state = app.state.repository.group_rows(OWNER)[0]["peers"][0]
    assert state["response_state"] == "UNRESPONSIVE"
    assert current(app, peer).finality == "UNKNOWN"


def test_native_inbox_event_and_receipt_use_same_v22_persistent_state(desk):
    app, client, flows, transport, _ = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    binding = SimpleNamespace(owner_user_id=OWNER, account_uid="synthetic-account", workbench_id="synthetic-wb")
    envelope = {"kind": "event", "owner_user_id": OWNER, "account_uid": binding.account_uid,
                "workbench_id": binding.workbench_id, "peer_id": "peer-a",
                "payload": {"event_id": "native-event-1", "source_seq": 1, "peer_id": "peer-a",
                            "text": "SYN-A 3.10出500万", "direction": "in", "occurred_at": NOW,
                            "trade_day": "2026-10-17", "ext_msg_id": "synthetic-in-native"}}
    message_id = app.state.inbox.apply(binding, envelope)
    before = client.get("/api/negotiation/messages/" + peer["thread_id"]).json()["data"]
    assert next(m for m in before["messages"] if m["message_id"] == message_id)["analysis_state"] == "QUEUED"
    assert app.state.inbox.apply(binding, envelope) == message_id
    app.state.negotiation_engine.run_once()
    assert current(app, peer).price.value == "3.10"
    queued = next(c for c in app.state.inquiry.commands(OWNER) if c["state"] == "QUEUED")
    commands = app.state.inquiry.commands_core
    assert commands.claim(OWNER, queued["command_id"])
    receipt = {"command_id": queued["command_id"], "account_uid": binding.account_uid,
               "peer_id": "peer-a", "content_hash": queued["content_hash"], "status": "sent",
               "evidence": "synthetic collector native ack", "native_id": "synthetic-native-ack"}
    result = app.state.inbox.apply(binding, {**envelope, **receipt, "kind": "receipt", "payload": receipt})
    assert result == "SENT"
    assert commands.get(OWNER, queued["command_id"])["state"] == "SENT"
    state = app.state.repository.group_rows(OWNER)[0]["peers"][0]
    assert state["pending_command_id"] is None
    assert state["our_quote"]["native_id"] == "synthetic-native-ack"
    assert state["our_quote"]["price"]["value"] == "3.19"


def test_unresolved_fragment_allows_one_actual_clarification_without_changing_known_bond(desk):
    app, _, flows, transport, _ = desk
    groups = setup_groups(desk, two_bonds=True, stopped=False)
    a, b = groups["SYN-A"]["peers"][0], groups["SYN-B"]["peers"][0]
    raw(app, a["thread_id"], "SYN-A 3.10出500万；另一只也可以谈")
    def analyze(snapshot):
        scopes, observations, unresolved = {}, [], {}
        for msg in snapshot["messages"]:
            scopes[msg["message_id"]] = [p["member_id"] for p in snapshot["members"] if p["bond_code"] in msg["text"]]
            if msg["direction"] == "in":
                observations.append(observation(a, msg, segment="SYN-A 3.10出500万"))
                unresolved[msg["message_id"]] = [b["member_id"]]
        return {"schema_version": "2.2", "scopes": scopes, "observations": observations, "unresolved": unresolved}
    decisions = []
    def decide(context):
        decisions.append(context["member_id"])
        if context["member_id"] == b["member_id"]:
            return {"schema_version": "2.2", "action": "SEND", "proposal": {
                "kind": "CLARIFY", "text": "请确认另一只是哪个券？"}}
        return {"schema_version": "2.2", "action": "WAIT", "proposal": None}
    flows.analyze, flows.decide = analyze, decide
    app.state.negotiation_engine.run_once()
    assert decisions.count(b["member_id"]) == 1, "Unresolved member must reach B only for a permitted clarification"
    app.state.dispatcher.run_once()
    assert len(transport.sent) == 2
    assert transport.sent[-1]["text"] == "请确认另一只是哪个券？"
    assert current(app, a).price.value == "3.10" and current(app, a).volume_wan == "500"
    assert current(app, b).price is None
    assert app.state.repository.group(OWNER, groups["SYN-A"]["group_id"])["state"] == "OPEN"
    app.state.negotiation_engine.run_once()
    app.state.dispatcher.run_once()
    assert len(transport.sent) == 2, "No new peer evidence means no repeated clarification"


def test_removed_association_is_not_reexposed_as_a_current_message_context(desk):
    app, client, flows, _, _ = desk
    groups = setup_groups(desk, two_bonds=True)
    a, b = groups["SYN-A"]["peers"][0], groups["SYN-B"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(a, msg)])
    message_id = raw(app, a["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    response = client.post("/api/negotiation/messages/" + message_id + "/association", json={
        "thread_id": a["thread_id"], "member_ids": [b["member_id"]],
        "expected_revision": app.state.repository.snapshot(OWNER)["revision"], "reason": "合成来源归属纠正"})
    assert response.status_code == 200, response.text
    raw_messages = client.get("/api/negotiation/messages/" + a["thread_id"]).json()["data"]["messages"]
    row = next(m for m in raw_messages if m["message_id"] == message_id)
    assert {c["member_id"] for c in row["contexts"]} == {b["member_id"]}
    projected = client.get("/api/monitor/chat?cp=peer-a").json()["data"]["messages"]
    row = next(m for m in projected if m["id"] == message_id)
    assert {c["code"] for c in row["contexts"]} == {"SYN-B"}


def test_initial_inquiry_does_not_repeat_shared_group_total_to_each_counterparty(desk):
    app, client, _, _, _ = desk
    preview = draft(client, [item("SYN-A", ["peer-a", "peer-b"])])
    initial = confirm(client, preview)
    assert len(initial["commands"]) == 2
    for command in initial["commands"]:
        assert "3.20%收" in command["text"]
        assert "1000" not in command["text"], "Shared target must not become an independent full-size request to every peer"
        assert "数量" in command["text"]
    group = app.state.repository.group_rows(OWNER)[0]
    assert group["target_volume"] == "1000"
    assert group["summary"]["target_volume_wan"] == "1000"


def test_archiving_whole_group_checks_other_counterpartys_unknown_command(desk):
    app, client, _, _, _ = desk
    group = setup_groups(desk, peers=["peer-a", "peer-b"], stopped=False)["SYN-A"]
    peers = {p["peer_uid"]: p for p in group["peers"]}
    raw(app, peers["peer-b"]["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    queued = next(row for row in app.state.inquiry.commands(OWNER) if row["state"] == "QUEUED")
    commands = app.state.inquiry.commands_core
    claimed = commands.claim(OWNER, queued["command_id"])
    assert claimed is not None
    assert commands.receipt(OWNER, queued["command_id"], account_uid="synthetic-account", peer_uid="peer-b",
        content_hash=queued["content_hash"], status="UNKNOWN", evidence="synthetic unknown native result") == "UNKNOWN"
    assert client.post("/api/negotiation/groups/" + group["group_id"] + "/sending", json={"allowed": False}).status_code == 200
    response = client.post("/api/sessions/" + peers["peer-a"]["member_id"] + "/archive")
    assert response.status_code in (400, 409), "Whole-group archival must reject another member's unresolved native command"
    assert app.state.repository.group(OWNER, group["group_id"])["archived"] == 0
    assert commands.get(OWNER, queued["command_id"])["state"] == "UNKNOWN"
