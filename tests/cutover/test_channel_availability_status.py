"""Heartbeat freshness is not native login; unavailable direct requests never queue."""
import pytest
from server.channel_readiness import ChannelAdmission, ChannelUnavailable
from tests.institution_test_support import selection, OWNER


@pytest.fixture(autouse=True)
def synchronous_clock(selection):
    selection["runtime"].clock = lambda: selection["clock"][0]
    selection["runtime"].directory.clock = lambda: selection["clock"][0]


def admission(s):
    workers = {name: {"running": True} for name in ("dispatch", "inbox")}
    return ChannelAdmission(s["app"].state.inquiry, s["runtime"], s["app"].state.transport,
                            True, {"workers": workers, "background_errors": {}})


def test_expired_heartbeat_is_unknown_transport_not_native_logout(selection):
    s = selection
    assert admission(s).report(OWNER)["ready"]
    s["advance"](61)
    row = s["runtime"].directory.account("wb-a", "account-a")
    assert row["online"] is True and row["available"] is False
    state = admission(s).report(OWNER)
    assert state["code"] == "COLLECTOR_HEARTBEAT_STALE"
    assert "心跳" in state["hint"] and "已退出登录" not in state["hint"]
    with pytest.raises(ChannelUnavailable) as caught:
        admission(s)(OWNER)
    assert caught.value.submitted is False


def test_new_directory_heartbeat_recovers_same_context_without_rebind(selection):
    s = selection
    before = s["repository"].binding_for(OWNER)
    context = s["repository"].preferences(OWNER)["context_id"]
    s["advance"](61)
    assert not admission(s).report(OWNER)["ready"]
    s["publish"]("wb-a", "account-a", "catalog")
    s["refresh"]()
    assert admission(s).report(OWNER)["ready"]
    assert s["repository"].binding_for(OWNER) == before
    assert s["repository"].preferences(OWNER)["context_id"] == context


def test_fresh_collector_not_ready_does_not_assume_logout(selection):
    s = selection
    s["publish"]("wb-a", "account-a", "catalog", online=False)
    s["refresh"]()
    state = admission(s).report(OWNER)
    assert state["code"] == "COLLECTOR_NOT_READY"
    assert not state["ready"] and "已离线" not in state["hint"]


def test_duplicate_online_identity_has_its_own_error(selection):
    s = selection
    s["publish"]("wb-b", "account-a", "catalog")
    s["refresh"]()
    assert admission(s).report(OWNER)["code"] == "COLLECTOR_IDENTITY_CONFLICT"


def test_stale_contact_refresh_has_actionable_error_without_false_permission_denial(selection):
    s = selection
    s["advance"](61)
    read = s["api"].get("/api/im/contacts")
    assert read.status_code == 200 and read.json()["data"]["contacts"]
    refresh = s["api"].get("/api/im/contacts", params={"refresh": 1})
    assert refresh.status_code == 400
    assert "心跳" in refresh.text and "恢复登录" not in refresh.text
    assert s["api"].get("/api/im/links").json()["data"]["links"][0]["catalog_stale"]


def test_real_direct_endpoint_rejects_stale_heartbeat_without_creating_work(selection):
    s = selection
    s["app"].state.inquiry.submit_guard = admission(s)
    s["advance"](61)
    def counts():
        with s["repository"].db.transaction(write=False) as tx:
            return {t: tx.execute("SELECT COUNT(*) AS n FROM " + t).fetchone()["n"]
                    for t in ("xj2_batch", "xj2_command", "xj2_group", "xj2_peer")}
    before = counts()
    response = s["api"].post("/api/im/rfq/send-direct", json={
        "request_id": "heartbeat-outage-direct", "items": [{
            "code": "SYN-HEARTBEAT", "name": "Synthetic only", "our_side": "BUY",
            "volume_wan": "1", "opening_yield_pct": "2.20", "limit_yield_pct": "2.08",
            "step_bp": "2", "pricing_basis": "absolute", "peer_ids": ["peer-wb-a-account-a"]}]})
    assert response.status_code == 503, response.text
    assert response.json()["submitted"] is False
    assert response.json()["code"] == "COLLECTOR_HEARTBEAT_STALE"
    assert counts() == before
