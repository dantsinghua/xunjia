"""Positive quotation and per-counterparty cross-day production contracts."""
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.auth import AuthConfig
from server.negotiation.settings import Aggregation
from server.reference_data import ReferenceSnapshot
from test_production_flow import Clock, FakeFlows, FakeNative, OWNER, User, confirm, desk, draft, item, raw
from test_acceptance_contract import current, observation, parser, setup_groups


@pytest.mark.parametrize("mode", ["absolute", "bp"])
def test_rollover_asks_every_counterparty_and_updates_only_bp_boundary(tmp_path, mode):
    class Reference:
        day = "2026-10-17"
        value = "2.9818"
        def trading_day(self, stamp):
            return self.day
        def latest_valuation_day(self, stamp):
            return self.day
        def snapshot(self):
            return ReferenceSnapshot("synthetic-" + self.day, self.day,
                bonds=[{"code": "SYN-A", "bond_id": "SYN-A", "valuation_yield_pct": self.value,
                        "valuation_date": self.day, "aliases": []}])
    reference, clock, flows, transport = Reference(), Clock(), FakeFlows(), FakeNative()
    app = create_app(database_url="sqlite:///" + str(tmp_path / "daily.db"),
        auth_config=AuthConfig(sso_secret="synthetic-sso", session_secret="synthetic-session-at-least-32-characters", clock=clock),
        enable_background=False, chatflows=flows, transport=transport,
        aggregation=Aggregation(0, 0, 1000, "synthetic-fixture-only"),
        reference_provider=reference, calendar=reference, enable_live_transport=True)
    app.state.repository.bind_account(OWNER, "synthetic-account", "synthetic-wb")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.auth.issue_token(User())
        target = item("SYN-A", ["peer-a", "peer-b"])
        if mode == "bp":
            app.state.repository.save_preferences(OWNER, {"floors": {"SYN-A": {"kind": "bp", "value": "5"}}})
            target.update(pricing_basis="bp", limit_yield_pct="3.03")
        confirm(client, draft(client, [target]))
        app.state.dispatcher.run_once()
        assert len(transport.sent) == 2
        flows.decide = lambda context: {"schema_version": "2.2", "action": "WAIT", "proposal": None}
        peers = app.state.repository.group_rows(OWNER)[0]["peers"]
        for peer in peers:
            raw(app, peer["thread_id"], "SYN-A 3.10出500万")
        app.state.negotiation_engine.run_once()
        assert all(current(app, peer).confirmed_day == "2026-10-17" for peer in peers)
        reference.day, reference.value = "2026-10-19", "2.9872"
        clock.value += 2 * 86400
        app.state.rollover_once()
        group = app.state.repository.group_rows(OWNER)[0]
        assert group["boundary"]["value"] == ("3.04" if mode == "bp" else "3.12")
        assert group["summary"]["reported_volume_wan"] == "0"
        assert all(p["reconfirm_day"] == "2026-10-19" for p in group["peers"])
        assert all(current(app, p).confirmed_day is None for p in peers)
        contexts = []
        def reconfirm(context):
            contexts.append(context)
            return {"schema_version": "2.2", "action": "SEND", "proposal": {
                "kind": "RECONFIRM", "text": "SYN-A 请确认今天的报价和可做数量。"}}
        flows.decide = reconfirm
        app.state.negotiation_engine.run_once()
        app.state.dispatcher.run_once()
        assert len(transport.sent) == 4
        assert {wire["peer_id"] for wire in transport.sent[2:]} == {"peer-a", "peer-b"}
        assert all(ctx["reconfirm_day"] == "2026-10-19" for ctx in contexts)
        assert all(current(app, peer).confirmed_day is None for peer in peers), "Asking is not a fresh peer confirmation"


def test_reference_to_actual_sent_price_and_fresh_volume_is_accepted(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    app.state.negotiation_engine.run_once()
    anchor = next(m for m in app.state.repository.context(OWNER, peer["member_id"]) if m["direction"] == "out")
    def quote(snapshot, msg):
        value = observation(peer, msg, price="3.20", volume="500", finality="FINAL", segment=msg["text"])
        value["evidence"]["price"] = [anchor["message_id"], msg["message_id"]]
        value["segments"].append({"message_id": anchor["message_id"], "text": anchor["text"]})
        value["interpretation"]["reason"] = "明确引用同券同对手真实原生已发价，并表示不能继续让价；数量为本次对手自报。"
        return [value]
    flows.analyze = parser(quote)
    raw(app, peer["thread_id"], "SYN-A 就按你刚才那个价出500万，这是最后条件，没有再让空间")
    app.state.negotiation_engine.run_once()
    fact = current(app, peer)
    assert fact.price is not None
    assert fact.price.value == "3.20" and fact.volume_wan == "500"
    assert fact.finality == "FINAL"
    assert anchor["message_id"] in fact.evidence["price"]


def test_decimal_quantity_and_contextual_suffix_remain_usable_positive_quotes(desk):
    app, _, flows, _, _ = desk
    peer = setup_groups(desk)["SYN-A"]["peers"][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, price="2.20", volume="0.5亿")])
    raw(app, peer["thread_id"], "SYN-A 2.20，0.5亿出")
    app.state.negotiation_engine.run_once()
    assert current(app, peer).price.value == "2.20"
    assert Decimal(current(app, peer).volume_wan) == Decimal("5000")
    def suffix(snapshot, msg):
        value = observation(peer, msg, volume="5000", segment=msg["text"])
        value["changes"]["price"] = {"kind": "YIELD", "value": "20", "notation": "YIELD_SUFFIX"}
        return [value]
    flows.analyze = parser(suffix)
    raw(app, peer["thread_id"], "SYN-A 20 5000", seq=2)
    app.state.negotiation_engine.run_once()
    assert current(app, peer).revision == 2
    assert current(app, peer).price.value == "2.20"
    assert Decimal(current(app, peer).volume_wan) == Decimal("5000")
