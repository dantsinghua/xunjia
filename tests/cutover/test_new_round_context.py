"""New explicit rounds own current facts but share actual bond/peer history."""
import json

import pytest

from server.negotiation.prices import LimitPolicy
from test_production_flow import NOW, OWNER, confirm, desk, draft, item, raw
from test_acceptance_contract import current, observation, setup_groups
from test_private_history_requests import request, scope_only


def completed_round(desk, *, archived=False):
    app, client, flows, _, clock = desk
    group = setup_groups(desk, stopped=False)["SYN-A"]
    peer = group["peers"][0]
    anchor = next(m for m in app.state.repository.context(OWNER, peer["member_id"]) if m["direction"] == "out")
    flows.analyze = scope_only
    old_ids = [anchor["message_id"]]
    for index in range(1, 22):
        old_ids.append(raw(app, peer["thread_id"], "SYN-A 本轮实际历史交流", seq=index))
    app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    final = raw(app, peer["thread_id"], "SYN-A 3.15出1000万，已是最终实价，不能再让", seq=22)
    old_ids.append(final)
    flows.analyze = lambda snapshot: {"schema_version": "2.2", "scopes": {final: [peer["member_id"]]},
        "observations": [observation(peer, snapshot["messages"][-1], price="3.15", volume="1000",
                                     finality="FINAL", segment=snapshot["messages"][-1]["text"])], "unresolved": {}}
    saved = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert saved["facts_committed"] and not saved["rejected"]
    assert app.state.repository.group(OWNER, group["group_id"])["state"] == "HANDOFF"
    if archived:
        response = client.post("/api/sessions/" + peer["member_id"] + "/archive")
        assert response.status_code == 200, response.text
    clock.value += 100
    return group, peer, anchor, old_ids


def new_round(desk):
    app, client, _, transport, _ = desk
    plan = {**item("SYN-A", ["peer-a"]), "opening_yield_pct": "3.30"}
    confirmed = confirm(client, draft(client, [plan], key="new-same-bond-round"), key="confirm-new-same-bond-round")
    group = next(g for g in app.state.repository.group_rows(OWNER) if g["group_id"] == confirmed["group_ids"][0])
    app.state.dispatcher.run_once()
    assert len(transport.sent) == 2
    peer = group["peers"][0]
    initial = next(m for m in app.state.repository.messages(OWNER, peer["thread_id"])
                   if m["event_id"] == "command:" + confirmed["commands"][0]["command_id"])
    return group, peer, initial


@pytest.mark.parametrize("archived", [False, True])
def test_current_quote_goes_only_to_new_round_without_mutating_old_handoff(desk, archived):
    app, _, flows, transport, _ = desk
    old_group, old_peer, _, _ = completed_round(desk, archived=archived)
    old_quote = current(app, old_peer).dump()
    old_plan = app.state.repository.group(OWNER, old_group["group_id"])["handoff_json"]
    new_group, new_peer, _ = new_round(desk)
    incoming = raw(app, new_peer["thread_id"], "SYN-A 3.10出500万", seq=100)
    def analyze(snapshot):
        # A semantically ordinary model may associate every offered same-code
        # member; the production round contract must not offer/update an old round.
        scopes, observations = {}, []
        for msg in snapshot["messages"]:
            hits = [member for member in snapshot["members"] if member["bond_code"] in msg["text"]]
            scopes[msg["message_id"]] = [member["member_id"] for member in hits]
            if msg["direction"] == "in":
                observations.extend(observation(member, msg) for member in hits)
        return {"schema_version": "2.2", "scopes": scopes, "observations": observations, "unresolved": {}}
    flows.analyze = analyze
    saved = app.state.negotiation_engine.analyze_thread(OWNER, new_peer["thread_id"])
    assert saved["facts_committed"]
    assert current(app, new_peer).price.value == "3.10" and current(app, new_peer).volume_wan == "500"
    assert current(app, old_peer).dump() == old_quote, "Current new-round input mutated the old handoff quote"
    old = app.state.repository.group(OWNER, old_group["group_id"])
    assert old["state"] == "HANDOFF" and old["send_allowed"] == 0 and old["handoff_json"] == old_plan
    message = next(m for m in app.state.repository.raw_page(OWNER, new_peer["thread_id"])["messages"] if m["message_id"] == incoming)
    assert {ctx["member_id"] for ctx in message["contexts"] if ctx["relation"] == "MENTION"} == {new_peer["member_id"]}
    app.state.dispatcher.run_once()
    assert len(transport.sent) == 2


def seed_other_scope(store, member, thread, event_id, text, stamp):
    mid = store.ingest(OWNER, thread, event_id=event_id, source_seq=70, text=text, trade_day="2026-10-17",
                       direction="out", native_id="native-" + event_id, occurred_at=stamp)
    job = store.claim_analysis(OWNER, thread, force=True)
    store.commit_analysis(OWNER, job["analysis_id"], job["lease_token"],
        scopes={m["message_id"]: [member] for m in job["snapshot"]["messages"]}, observations=[])
    return mid


def test_new_round_default_twenty_is_global_same_bond_same_peer_not_member_id(desk):
    app, _, _, _, _ = desk
    old_group, old_peer, _, old_ids = completed_round(desk, archived=True)
    store = app.state.repository
    other_bond = store.create_group(OWNER, bond_code="SYN-B", our_side="BUY", target_volume="1000",
        trade_day="2026-10-17", policy=LimitPolicy("ABSOLUTE", "3.12"))
    other_bond_member, same_thread = store.add_member(OWNER, other_bond, account_uid="synthetic-account", peer_uid="peer-a")
    excluded_bond = seed_other_scope(store, other_bond_member, same_thread, "other-bond", "SYN-B PRIVATE_OTHER_BOND", NOW + 90)
    other_peer_member, other_thread = store.add_member(OWNER, old_group["group_id"], account_uid="synthetic-account", peer_uid="peer-b")
    excluded_peer = seed_other_scope(store, other_peer_member, other_thread, "other-peer", "SYN-A PRIVATE_OTHER_PEER", NOW + 95)
    _, new_peer, initial = new_round(desk)
    context = store.context(OWNER, new_peer["member_id"])
    assert len(context) == 20
    assert [row["message_id"] for row in context] == (old_ids + [initial["message_id"]])[-20:]
    assert not {excluded_bond, excluded_peer} & {row["message_id"] for row in context}
    assert any(row["direction"] == "out" and row["native_id"] for row in context)
    assert any(row["direction"] == "in" for row in context)


@pytest.mark.parametrize("archived", [False, True])
def test_new_round_can_fetch_old_native_anchor_and_confirm_it_without_old_round_write(desk, archived):
    app, _, flows, _, _ = desk
    _, old_peer, anchor, _ = completed_round(desk, archived=archived)
    old_quote = current(app, old_peer).dump()
    _, new_peer, _ = new_round(desk)
    incoming = raw(app, new_peer["thread_id"], "SYN-A 按上一轮你那条报价出500万", seq=100)
    calls = []
    def analyze(snapshot):
        calls.append(snapshot)
        if len(calls) == 1:
            return request(new_peer["member_id"], [anchor["message_id"]])
        assert [m["message_id"] for m in snapshot["history_results"][0]["messages"]] == [anchor["message_id"]]
        msg = next(m for m in snapshot["messages"] if m["message_id"] == incoming)
        value = observation(new_peer, msg, price="3.20", segment=msg["text"])
        value["evidence"]["price"] = [anchor["message_id"], incoming]
        value["segments"].append({"message_id": anchor["message_id"], "text": anchor["text"]})
        return {"schema_version": "2.2", "scopes": {m["message_id"]: [new_peer["member_id"]] for m in snapshot["messages"]},
                "observations": [value], "unresolved": {}}
    flows.analyze = analyze
    saved = app.state.negotiation_engine.analyze_thread(OWNER, new_peer["thread_id"])
    assert saved["facts_committed"] and not saved["rejected"], saved
    assert len(calls) == 2
    assert current(app, new_peer).price.value == "3.20" and current(app, new_peer).volume_wan == "500"
    assert current(app, old_peer).dump() == old_quote


def seed_cross_round_dependency(desk, *, final=False):
    app, _, flows, _, _ = desk
    _, old_peer, anchor, _ = completed_round(desk, archived=True)
    group, peer, _ = new_round(desk)
    volume = "1000" if final else "500"
    text = "SYN-A 按上一轮你那条报价出" + volume + "万"
    if final:
        text += "，已是最终实价，不能再让"
    incoming = raw(app, peer["thread_id"], text, seq=100)
    def analyze(snapshot):
        if not snapshot.get("history_results"):
            return request(peer["member_id"], [anchor["message_id"]])
        msg = next(m for m in snapshot["messages"] if m["message_id"] == incoming)
        value = observation(peer, msg, price="3.20", volume=volume,
            finality="FINAL" if final else "NEGOTIABLE", segment=msg["text"])
        value["evidence"]["price"] = [anchor["message_id"], incoming]
        value["segments"].append({"message_id": anchor["message_id"], "text": anchor["text"]})
        return {"schema_version": "2.2", "scopes": {m["message_id"]: [peer["member_id"]] for m in snapshot["messages"]},
                "observations": [value], "unresolved": {}}
    flows.analyze = analyze
    saved = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert saved["facts_committed"] and not saved["rejected"], saved
    assert current(app, peer).price.value == "3.20"
    # The anchor remains scoped to its old round. This is a real logical-history
    # dependency, not a test-created exact scope on the new member.
    with app.state.repository.db.transaction(write=False) as tx:
        assert tx.execute("SELECT 1 FROM xj2_scope WHERE message_id=? AND member_id=?",
                          (anchor["message_id"], peer["member_id"])).fetchone() is None
    return group, peer, old_peer, anchor, incoming


@pytest.mark.parametrize("change", ["retract", "edit", "association"])
def test_changing_old_round_anchor_recomputes_new_round_dependents_and_coverage(desk, change):
    app, client, _, _, _ = desk
    group, peer, _, anchor, _ = seed_cross_round_dependency(desk, final=True)
    store = app.state.repository
    before = current(app, peer)
    before_group = store.group(OWNER, group["group_id"])
    assert before_group["state"] == "HANDOFF" and before_group["summary"]["final_confirmed_volume_wan"] == "1000"
    if change == "association":
        other = store.create_group(OWNER, bond_code="SYN-B", our_side="BUY", target_volume="1000",
            trade_day="2026-10-17", policy=LimitPolicy("ABSOLUTE", "3.12"))
        other_member, _ = store.add_member(OWNER, other, account_uid="synthetic-account", peer_uid="peer-a")
        response = client.post("/api/negotiation/messages/" + anchor["message_id"] + "/association", json={
            "thread_id": peer["thread_id"], "member_ids": [other_member],
            "expected_revision": store.snapshot(OWNER)["revision"], "reason": "synthetic old anchor belongs to another bond"})
        assert response.status_code == 200, response.text
    else:
        store.ingest(OWNER, peer["thread_id"], event_id="old-anchor-" + change, source_seq=101,
            text="撤回历史报价" if change == "retract" else "SYN-A 更正为3.18%收",
            trade_day="2026-10-17", direction="out", kind=change, target_event_id=anchor["event_id"])
    quote = current(app, peer)
    assert quote.price is None, "Current quote still trusts a changed anchor from the previous round"
    assert quote.revision > before.revision and quote.finality == "UNKNOWN"
    after = store.group(OWNER, group["group_id"])
    assert after["summary"]["price_eligible_volume_wan"] == "0"
    assert after["summary"]["final_confirmed_volume_wan"] == "0"
    assert after["state"] == "HANDOFF" and after["send_allowed"] == 0 and after["review_needed"] == 1
    assert after["handoff_json"] == before_group["handoff_json"], "The original handoff evidence must remain auditable"


def test_unrelated_quantity_recompute_preserves_valid_cross_round_price_anchor(desk):
    app, _, flows, _, _ = desk
    group, peer, old_peer, anchor, _ = seed_cross_round_dependency(desk)
    original = current(app, peer)
    old_quote = current(app, old_peer).dump()
    update = raw(app, peer["thread_id"], "SYN-A 出量改800万", seq=101)
    flows.analyze = lambda snapshot: {"schema_version": "2.2", "scopes": {update: [peer["member_id"]]},
        "observations": [{"member_id": peer["member_id"], "side": "ofr", "changes": {"volume_wan": "800"},
                          "evidence": {"volume_wan": [update]}}], "unresolved": {}}
    saved = app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])
    assert saved["facts_committed"] and not saved["rejected"]
    assert current(app, peer).volume_wan == "800"
    raw(app, peer["thread_id"], "撤回刚才的改量", seq=102, kind="retract", target_event_id="event-101")
    quote = current(app, peer)
    assert quote.price is not None, "Unrelated amount retraction erased still-valid logical-history price evidence"
    assert quote.price.value == "3.20" and quote.evidence["price"] == original.evidence["price"]
    assert anchor["message_id"] in quote.evidence["price"]
    assert quote.volume_wan == "500" and quote.validity == "ACTIVE"
    assert app.state.repository.group(OWNER, group["group_id"])["summary"]["price_eligible_volume_wan"] == "500"
    assert current(app, old_peer).dump() == old_quote
