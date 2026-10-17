"""Cross-layer controlled acceptance: real FastAPI -> DB -> dispatcher -> receiver ledger.
All identities, valuations, and native execution evidence are synthetic.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from relay.protocol import Binding
from server.legacy_app import create_app
from server.auth import AuthConfig
from server.reference_data import ReferenceSnapshot
from server.receiver_ledger import ReceiverLedger

NOW = 1_800_000_000
OWNER, ACCOUNT, PEER = "controlled-trader", "990123", "controlled-peer"


class VerifiedSource:
    def __init__(self):
        self.version = "controlled-valuation-v1"
        self.value = "2.00"

    def snapshot(self):
        date = datetime.fromtimestamp(NOW, ZoneInfo("Asia/Shanghai")).date().isoformat()
        def fact(value):
            return {"value": value, "quality": "verified", "source_ref": "fixture-chinabond"}
        return ReferenceSnapshot(
            snapshot_id=self.version, as_of=date,
            sources=[{"id": "fixture-chinabond"}],
            bonds=[{"code": "000100.IB", "valuation": {
                "provider": fact("中债"), "basis": fact("maturity"),
                "yield_pct": fact(self.value), "valuation_date": fact(date)}}],
        )


class SyntheticExecutor:
    def __init__(self, path):
        self.native_calls = []
        self.last_envelope = None
        self.ledger = ReceiverLedger(path, Binding(
            OWNER, ACCOUNT, "controlled-workbench", b"0123456789abcdef", b"s" * 32),
            clock=lambda: NOW)

    def _native(self, command):
        self.native_calls.append(command["command_id"])
        return {"status": "sent", "evidence": "controlled-native-success",
                "native_id": "controlled-native-id"}

    def send(self, command):
        self.last_envelope = command
        return self.ledger.execute(command, self._native)

    def cancel(self, command):
        return self.ledger.cancel(command)

    def reconcile(self, command):
        return self.ledger.reconcile(command, lambda _: None)


@pytest.fixture
def rig(tmp_path):
    ref, transport = VerifiedSource(), SyntheticExecutor(tmp_path / "receiver.db")
    app = create_app(
        database_url="sqlite:///" + str(tmp_path / "controlled.sqlite"),
        auth_config=AuthConfig(
            sso_secret="synthetic-integration-sso",
            session_secret="synthetic-integration-session-secret-at-least-32-bytes",
            clock=lambda: NOW),
        reference_provider=ref, transport=transport, enable_background=False)
    app.state.repository.bind_account(OWNER, ACCOUNT, "controlled-workbench")
    prefs = {
        "floors": {"000100.IB": {"kind": "bp", "value": 5, "revision": 1}},
        "strategy": {"template_id": "controlled", "start_offset_bp": 10,
                     "boundary_bp": 5, "step_bp": 2, "strict_match": True}}
    with app.state.repository.db.transaction() as tx:
        tx.execute("UPDATE xj_binding SET config_json=? WHERE owner_user_id=?",
                   (json.dumps(prefs), OWNER))
    with TestClient(app) as client:
        token_body = {"userName": OWNER, "nickName": OWNER, "ts": str(NOW)}
        token_body["sign"] = hashlib.md5(
            f"{OWNER}|{OWNER}|{NOW}|synthetic-integration-sso".encode()).hexdigest()
        auth = client.post("/api/auth/sso", json=token_body)
        assert auth.status_code == 200, auth.text
        header = {"Authorization": "Bearer " + auth.json()["token"]}
        yield app, client, header, ref, transport


def item(code="000100.IB", *, basis="bp"):
    if basis == "bp":
        opening, boundary, name = "2.10", "2.05", "合成债券"
    else:
        opening, boundary, name = "2.50", "2.40", "另一合成债券"
    return {"code": code, "name": name, "peer_ids": [PEER],
            "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": opening,
            "limit_yield_pct": boundary, "step_bp": "2", "strict_match": True,
            "pricing_basis": basis}


def preview(client, header, suffix, records):
    res = client.post("/api/im/rfq/preview", json={
        "request_id": "preview-" + suffix, "items": records}, headers=header)
    assert res.status_code == 200, res.text
    return res.json()["data"]


def confirm(client, header, draft, suffix):
    return client.post("/api/im/rfq/send", json={
        "batch_id": draft["batch_id"], "expected_version": draft["version"],
        "request_id": "confirm-" + suffix}, headers=header)


def test_api_bp_confirmation_to_receiver_and_explicit_takeover_stay_scoped(rig):
    app, client, header, ref, transport = rig
    draft = preview(client, header, "bp", [item()])
    assert draft["items"][0]["valuation_snapshot"]["snapshot_id"] == "controlled-valuation-v1"
    result = confirm(client, header, draft, "bp")
    assert result.status_code == 200, result.text
    first = result.json()["data"]["commands"][0]
    assert first["status"] == "queued"
    assert app.state.dispatcher.run_once() == 1
    assert transport.native_calls == [first["command_id"]]
    assert transport.last_envelope["text"].find("2.10%") >= 0
    assert app.state.dispatcher.run_once() == 0
    assert transport.native_calls == [first["command_id"]]
    assert transport.ledger.execute(transport.last_envelope, transport._native)["status"] == "sent"
    assert len(transport.native_calls) == 1, "transport replay must not re-send"

    session = app.state.inquiry.sessions(OWNER)[0]
    assert session["our_quote"]["yield_pct"] == "2.10"
    assert session["policy"]["valuation_snapshot"]["valuation_date"]
    receipt = client.get("/api/im/receipts/" + first["command_id"], headers=header)
    assert receipt.status_code == 200 and receipt.json()["data"]["status"] == "sent"

    stop = client.post("/api/session/" + session["session_id"] + "/takeover",
                       json={}, headers=header)
    assert stop.status_code == 200, stop.text
    toggle = client.post("/api/bot/toggle", json={"status": "停止"}, headers=header)
    assert toggle.status_code == 200
    second = preview(client, header, "other", [item("000200.IB", basis="absolute")])
    blocked = confirm(client, header, second, "other")
    assert blocked.status_code == 400, blocked.text
    assert len(app.state.inquiry.commands(OWNER)) == 1
    assert len(transport.native_calls) == 1


def test_api_rejects_untrusted_bp_change_after_preview_and_foreign_scope(rig):
    app, client, header, reference, transport = rig
    initial = preview(client, header, "reference-change", [item()])
    reference.version = "controlled-valuation-v2"
    denied = confirm(client, header, initial, "reference-change")
    assert denied.status_code == 400 and "估值" in denied.text
    assert app.state.inquiry.commands(OWNER) == []
    noauth = client.post("/api/im/rfq/send", json={
        "batch_id": initial["batch_id"], "expected_version": initial["version"],
        "request_id": "different"}, headers={})
    assert noauth.status_code == 401
    assert transport.native_calls == []


def test_absolute_authorization_requires_explicit_numeric_boundaries_but_no_reference(rig):
    app, client, header, reference, transport = rig
    app.state.inquiry.reference_provider = None
    draft = preview(client, header, "absolute", [item("000200.IB", basis="absolute")])
    assert draft["items"][0]["pricing_basis"] == "absolute"
    assert "valuation_snapshot" not in draft["items"][0]
    result = confirm(client, header, draft, "absolute")
    assert result.status_code == 200
    assert transport.native_calls == []
