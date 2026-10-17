"""Independent V2.2 acceptance gates through the production app and engine.

Only model responses and native transport are synthetic: the production
evidence validator, persistence, controls and command gates all execute.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from test_production_flow import OWNER, Clock, FakeFlows, FakeNative, User, confirm, desk, draft, item, raw
from fastapi.testclient import TestClient
from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.settings import Aggregation
from server.negotiation.prices import Price
from server.reference_data import ReferenceSnapshot


def setup_groups(desk, *, two_bonds=False, peers=None, stopped=True):
    app, client, flows, transport, clock = desk
    rows = [item("SYN-A", peers or ["peer-a"])]
    if two_bonds:
        rows.append(item("SYN-B", ["peer-a"]))
    confirm(client, draft(client, rows))
    if stopped:
        assert client.post("/api/bot/toggle", json={"status": "停止"}).status_code == 200
    else:
        app.state.dispatcher.run_once()
    return {g["bond_code"]: g for g in app.state.repository.group_rows(OWNER)}


def observation(member, message, *, price="3.10", volume="500", side="ofr",
                finality="NEGOTIABLE", segment=None, **extra):
    changes = {"price": Price("YIELD", price).dump(), "volume_wan": volume,
               "confirmed_day": message["trade_day"], "validity": "ACTIVE",
               "finality": finality}
    changes.update(extra)
    value = {"member_id": member["member_id"], "side": side, "changes": changes,
             "evidence": {key: [message["message_id"]] for key in changes}}
    if segment is not None:
        value["segments"] = [{"message_id": message["message_id"], "text": segment}]
        value["interpretation"] = {
            "quote_owner": "COUNTERPARTY", "intent": "QUOTE",
            "negotiation_space": finality,
            "reason": "该对手对本券直接报价；最终性依据此片段及同券实际上下文。",
        }
    return value


def parser(factory):
    def analyze(snapshot):
        scopes = {m["message_id"]: [p["member_id"] for p in snapshot["members"]
                  if p["bond_code"] in m["text"]] for m in snapshot["messages"]}
        observations = []
        for message in snapshot["messages"]:
            if message["direction"] == "in" and message["kind"] == "message":
                observations.extend(factory(snapshot, message))
        return {"schema_version": "2.2", "scopes": scopes,
                "observations": observations, "unresolved": {}}
    return analyze


def current(app, member, side="ofr"):
    return app.state.repository.quote(OWNER, member["member_id"], side)


def test_production_rejects_price_not_supported_by_any_raw_evidence(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, price="3.99")])
    raw(app, peer["thread_id"], "SYN-A 3.10出，500万")
    app.state.negotiation_engine.run_once()
    quote = current(app, peer)
    assert quote.price is None or quote.price.value != "3.99"


def test_our_authorized_quantity_cannot_supply_missing_counterparty_quantity(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, volume="1000")])
    raw(app, peer["thread_id"], "SYN-A 3.10出")
    app.state.negotiation_engine.run_once()
    assert current(app, peer).volume_wan is None


def test_ofr_evidence_cannot_create_bid_fact(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, side="bid")])
    raw(app, peer["thread_id"], "SYN-A 3.10出，500万")
    app.state.negotiation_engine.run_once()
    assert current(app, peer, "bid").price is None


def test_final_label_without_contextual_interpretation_is_not_final_coverage(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, price="3.15", volume="1000", finality="FINAL")])
    raw(app, peer["thread_id"], "SYN-A 3.15出1000万还能再谈，先报个意向")
    app.state.negotiation_engine.run_once()
    group = app.state.repository.group_rows(OWNER)[0]
    assert group["state"] == "OPEN"
    assert group["summary"]["final_confirmed_volume_wan"] == "0"


@pytest.mark.parametrize("text", [
    "SYN-A 3.15出1000万是你说的最终价吗？",
    "客户说SYN-A 3.15出1000万是最终价，我这边暂时没有报价",
])
def test_final_interpretation_cannot_override_explicit_question_or_third_party_quote(desk, text):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, price="3.15", volume="1000", finality="FINAL", segment=msg["text"])])
    raw(app, peer["thread_id"], text)
    app.state.negotiation_engine.run_once()
    group = app.state.repository.group_rows(OWNER)[0]
    assert group["state"] == "OPEN"
    assert group["summary"]["final_confirmed_volume_wan"] == "0"


def test_bilateral_quantity_is_bound_to_its_direction_within_one_bond(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    text = "SYN-A 3.05收1000万，3.15出500万"
    flows.analyze = parser(lambda snap, msg: [
        observation(peer, msg, price="3.05", volume="1000", side="bid", segment=text),
        observation(peer, msg, price="3.15", volume="1000", side="ofr", segment=text),
    ])
    raw(app, peer["thread_id"], text)
    app.state.negotiation_engine.run_once()
    assert current(app, peer, "bid").price.value == "3.05"
    assert current(app, peer, "bid").volume_wan == "1000"
    assert current(app, peer, "ofr").volume_wan != "1000", "Counterparty bid quantity cannot populate the ofr side"


def test_multibond_fragments_cannot_swap_economic_fields(desk):
    app, _, flows, _, _ = desk
    groups = setup_groups(desk, two_bonds=True)
    a, b = groups["SYN-A"]["peers"][0], groups["SYN-B"]["peers"][0]
    text = "SYN-A 3.10出500万；SYN-B 3.15出800万"
    flows.analyze = parser(lambda snap, msg: [
        observation(a, msg, price="3.15", volume="800", segment="SYN-A 3.10出500万"),
        observation(b, msg, price="3.15", volume="800", segment="SYN-B 3.15出800万"),
    ])
    raw(app, a["thread_id"], text)
    app.state.negotiation_engine.run_once()
    assert current(app, a).price is None or current(app, a).price.value != "3.15"
    assert current(app, b).price is not None, "Correct sibling fragment must survive invalid A fields"
    assert current(app, b).price.value == "3.15"
    assert current(app, b).volume_wan == "800"


def test_unique_current_alias_beats_old_focus_without_reassigning_a_price(desk):
    app, _, flows, _, _ = desk
    groups = setup_groups(desk, two_bonds=True)
    a, b = groups["SYN-A"]["peers"][0], groups["SYN-B"]["peers"][0]
    app.state.inquiry.reference_provider = SimpleNamespace(snapshot=lambda: ReferenceSnapshot(
        "synthetic-aliases", "2026-10-17", bonds=[{"code": "SYN-B", "bond_id": "SYN-B", "aliases": ["独立乙券"]}]))
    flows.analyze = parser(lambda snap, msg: [observation(a, msg)])
    raw(app, a["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    def old_focus(snapshot):
        msg = snapshot["messages"][-1]
        return {"schema_version": "2.2", "scopes": {msg["message_id"]: [a["member_id"]]}, "unresolved": {},
                "observations": [observation(a, msg, price="3.15", volume="300", segment=msg["text"]),
                                 observation(b, msg, price="3.15", volume="300", segment=msg["text"])]}
    flows.analyze = old_focus
    message_id = raw(app, a["thread_id"], "独立乙券 3.15出300万", seq=2)
    app.state.negotiation_engine.run_once()
    assert current(app, a).price.value == "3.10" and current(app, a).volume_wan == "500"
    assert current(app, b).price.value == "3.15" and current(app, b).volume_wan == "300"
    linked = next(m for m in app.state.repository.raw_page(OWNER, a["thread_id"])["messages"] if m["message_id"] == message_id)
    assert {c["member_id"] for c in linked["contexts"] if c["relation"] == "MENTION"} == {b["member_id"]}


def test_malformed_one_bond_does_not_abort_correct_other_bond_facts(desk):
    app, _, flows, _, _ = desk
    groups = setup_groups(desk, two_bonds=True)
    a, b = groups["SYN-A"]["peers"][0], groups["SYN-B"]["peers"][0]
    def output(snapshot, message):
        broken = observation(a, message, segment="SYN-A 3.10出500万")
        broken["changes"]["price"] = {"kind": "YIELD", "value": "garbled"}
        return [broken, observation(b, message, price="3.15", volume="800", segment="SYN-B 3.15出800万")]
    flows.analyze = parser(output)
    raw(app, a["thread_id"], "SYN-A 3.10出500万；SYN-B 3.15出800万")
    app.state.negotiation_engine.run_once()
    assert current(app, b).price is not None
    assert current(app, b).price.value == "3.15"


@pytest.mark.parametrize("proposal", [
    {"kind": "SOCIAL", "text": "那我就3.00%收1000万，成交。"},
    {"kind": "CLARIFY", "text": "3.00%收1000万，确认一下"},
    {"kind": "REPLY", "text": "3.19%收，100000万", "our_price": Price("YIELD", "3.19").dump()},
    {"kind": "REPLY", "text": "3.19%收，500万，成交锁量", "our_price": Price("YIELD", "3.19").dump()},
    {"kind": "REPLY", "text": "3.19%收，另按3.00收，500万", "our_price": Price("YIELD", "3.19").dump()},
])
def test_reply_full_body_cannot_bypass_economic_or_commitment_gate(desk, proposal):
    app, _, flows, transport, _ = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    flows.decide = lambda context: {"schema_version": "2.2", "action": "SEND", "proposal": deepcopy(proposal)}
    raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    assert current(app, peer).price.value == "3.10", "Rejecting a reply must preserve accepted counterparty facts"
    app.state.dispatcher.run_once()
    assert len(transport.sent) == 1, "Only the initial authorized inquiry may reach the synthetic native sender"


def test_rejected_reply_can_be_regenerated_without_rewriting_peer_facts(desk):
    app, _, flows, transport, clock = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    flows.bad_reply = True
    raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    app.state.negotiation_engine.run_once()
    revision = current(app, peer).revision
    flows.bad_reply = False
    clock.value += 10
    app.state.negotiation_engine.run_once()
    app.state.dispatcher.run_once()
    assert current(app, peer).revision == revision
    assert len(transport.sent) == 2, "A rejected reply must be regenerable from already committed facts"


def test_two_counterparties_concurrently_reach_exact_shared_target_once(desk):
    app, _, flows, _, _ = desk
    group = setup_groups(desk, peers=["peer-a", "peer-b"])["SYN-A"]
    barrier = Barrier(2)
    def output(snapshot, msg):
        member = snapshot["members"][0]
        barrier.wait(timeout=5)
        return [observation(member, msg, price="3.15", volume="500", finality="FINAL", segment=msg["text"])]
    flows.analyze = parser(output)
    for peer in group["peers"]:
        raw(app, peer["thread_id"], "SYN-A 3.15出500万，这是最终实价，不能再让")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: app.state.negotiation_engine.analyze_thread(OWNER, p["thread_id"]), group["peers"]))
    assert all(r["facts_committed"] for r in results)
    final = app.state.repository.group(OWNER, group["group_id"])
    assert final["summary"]["final_confirmed_volume_wan"] == "1000"
    assert final["state"] == "HANDOFF" and final["send_allowed"] == 0
    assert sorted(lot["volume_wan"] for lot in final["summary"]["whole_lots"]) == ["500", "500"]
    assert all(current(app, peer).revision == 1 for peer in group["peers"])


def test_model_snapshot_does_not_swallow_message_arriving_during_analysis(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    entered, finish = Event(), Event()
    def output(snapshot, msg):
        entered.set()
        assert finish.wait(timeout=5)
        return [observation(peer, msg)]
    flows.analyze = parser(output)
    raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(app.state.negotiation_engine.analyze_thread, OWNER, peer["thread_id"])
        try:
            assert entered.wait(timeout=5), "Model did not reach synchronization point"
            raw(app, peer["thread_id"], "SYN-A 改为3.15出500万", seq=2)
        finally:
            finish.set()
        assert future.result(timeout=5)["facts_committed"]
    state = app.state.repository.snapshot(OWNER)
    assert state["threads"][0]["analyzed_revision"] < state["threads"][0]["ingest_revision"]
    assert state["groups"][0]["peers"][0]["analysis_pending"]
    assert current(app, peer).price.value == "3.10"


def test_default_live_dispatch_stays_off_even_with_a_capable_transport(tmp_path):
    clock, flows, transport = Clock(), FakeFlows(), FakeNative()
    app = create_app(database_url="sqlite:///" + str(tmp_path / "default-off.db"),
        auth_config=AuthConfig(sso_secret="synthetic-sso", session_secret="synthetic-session-at-least-32-characters", clock=clock),
        enable_background=False, chatflows=flows, transport=transport,
        aggregation=Aggregation(0, 0, 1000, "synthetic-fixture-only"))
    app.state.repository.bind_account(OWNER, "synthetic-account", "synthetic-wb")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.auth.issue_token(User())
        confirmed = confirm(client, draft(client))
        assert app.state.dispatcher.live_enabled is False
        app.state.dispatcher.run_once()
        assert transport.sent == []
        assert app.state.inquiry.commands_core.get(OWNER, confirmed["commands"][0]["command_id"])["state"] == "QUEUED"
