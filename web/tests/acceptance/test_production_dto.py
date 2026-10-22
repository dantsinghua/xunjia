"""Use responses from the actual production API in the real browser renderer."""
import hashlib
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from playwright.sync_api import expect

from server.app import create_app
from server.auth import AuthConfig


def test_negotiating_session_from_production_api_is_displayed_as_inquiring(ui, tmp_path):
    app = create_app(submission_guard=lambda _owner: None, database_url="sqlite:///" + str(tmp_path / "dto.db"), enable_background=False,
                     auth_config=AuthConfig(sso_secret="sso", session_secret="test-session-secret-longer-than-32", clock=lambda: 1_800_000_000))
    app.state.repository.bind_account("dto-trader", "910001", "test-wb")
    service = app.state.inquiry
    assert service.__class__.__module__ == "server.negotiation.service"
    sign = hashlib.md5(b"dto-trader|DTO Trader|1800000000|sso").hexdigest()
    with TestClient(app) as client:
        token = client.post("/api/auth/sso", json={"userName": "dto-trader", "nickName": "DTO Trader", "ts": "1800000000", "sign": sign}).json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        preview = client.post("/api/im/rfq/preview", json={"request_id": "draft", "items": [{
            "code": "000100.IB", "name": "生产DTO合成券", "peer_ids": ["dto-peer"],
            "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50",
            "limit_yield_pct": "2.40", "step_bp": "2",
        }]})
        assert preview.status_code == 200, preview.text
        draft = preview.json()["data"]
        confirmed = client.post("/api/im/rfq/send", json={"batch_id": draft["batch_id"],
            "expected_version": draft["version"], "request_id": "confirm"})
        assert confirmed.status_code == 200, confirmed.text
        command = confirmed.json()["data"]["commands"][0]
        commands = service.commands_core
        claimed = commands.claim("dto-trader", command["command_id"])
        assert claimed is not None
        commands.receipt("dto-trader", command["command_id"], account_uid="910001", peer_uid="dto-peer",
            content_hash=claimed["content_hash"], status="SENT", evidence="synthetic native test receipt",
            native_id="synthetic-native-id")
        assert service.sessions("dto-trader")[0]["state"] == "NEGOTIATING"
        response = client.get("/api/monitor/counterparties", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        ui[2]["counterparties_data"] = response.json()["data"]
    page, url, _, _, _ = ui
    page.clock.install(time=datetime.fromtimestamp(1_800_000_000,timezone.utc))
    page.goto(url + "/#/monitor")
    expect(page.locator(".cp-card")).to_contain_text("生产DTO合成券")
    expect(page.locator(".cp-card .stage")).to_have_text("询价中")
    expect(page.locator(".rb-card .stage")).to_have_text("询价中")
