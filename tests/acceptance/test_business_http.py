"""Production API acceptance: successful authorized paths plus isolation and GET safety."""
import hashlib
import base64
import json

from fastapi.testclient import TestClient
import pytest

from server.legacy_app import create_app
from server.auth import AuthConfig
from server.business import InquiryService


NOW = 1_800_000_000


@pytest.fixture
def http_business(tmp_path):
    sends = []
    app = create_app(
        database_url="sqlite:///" + str(tmp_path / "http-business.db"),
        auth_config=AuthConfig(sso_secret="synthetic-sso", session_secret="synthetic-session-secret-32-bytes-minimum", clock=lambda: NOW),
        enable_background=False, transport=lambda *args, **kwargs: sends.append((args, kwargs)),
    )
    for user, uid in [("trader-a", "910001"), ("trader-b", "910002")]:
        app.state.repository.bind_account(user, uid, "synthetic-wb")
    with TestClient(app) as client:
        headers = {}
        for user in ("trader-a", "trader-b"):
            sign = hashlib.md5(f"{user}|{user}|{NOW}|synthetic-sso".encode()).hexdigest()
            response = client.post("/api/auth/sso", json={"userName": user, "nickName": user, "ts": str(NOW), "sign": sign})
            assert response.status_code == 200
            headers[user] = {"Authorization": "Bearer " + response.json()["token"]}
        yield app, client, headers, sends


def item():
    return {"code": "000100.IB", "name": "合成债券", "peer_ids": ["shared-peer"],
            "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50",
            "limit_yield_pct": "2.40", "step_bp": "2"}


def seed_history(rig, owner, count):
    service = InquiryService(rig[0].state.repository, clock=lambda: NOW)
    uid = "910001" if owner == "trader-a" else "910002"
    for index in range(count):
        service.ingest(owner, {"event_id": f"history-{owner}-{index}", "account_uid": uid, "peer_id": "shared-peer",
                              "source_seq": index + 1, "kind": "message", "text": f"{owner}-history-{index}",
                              "occurred_at": NOW, "historical": True, "direction": "in"})


@pytest.mark.parametrize("path", ["/api/health", "/api/auth/me", "/api/missing-endpoint"])
def test_security_headers_cover_success_unauthenticated_and_error_responses(http_business, path):
    _, client, _, _ = http_business
    response = client.get(path)
    policy = response.headers.get("content-security-policy", "")
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy and "'unsafe-eval'" not in policy
    assert "object-src 'none'" in policy
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("referrer-policy") in ("no-referrer", "same-origin", "strict-origin-when-cross-origin")



def test_chat_cursor_visits_105_same_timestamp_messages_without_duplicates_or_new_arrivals(http_business):
    app, client, headers, sends = http_business
    seed_history(http_business, "trader-a", 105)
    seen, cursor, sizes = [], None, []
    for page in range(5):
        params = {"cp": "shared-peer", "limit": 50}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/monitor/chat", headers=headers["trader-a"], params=params)
        assert response.status_code == 200
        data = response.json()["data"]
        sizes.append(len(data["messages"]))
        seen.extend(message["text"] for message in data["messages"])
        if page == 0:
            InquiryService(app.state.repository, clock=lambda: NOW + 1).ingest("trader-a", {
                "event_id": "newest-after-first-page", "account_uid": "910001", "peer_id": "shared-peer", "source_seq": 106,
                "kind": "message", "text": "NEW-ARRIVAL", "occurred_at": NOW + 1, "historical": True, "direction": "in"})
        if not data["hasMore"]:
            assert data["nextCursor"] is None
            break
        assert data["nextCursor"] and data["nextCursor"] != cursor
        cursor = data["nextCursor"]
    assert sizes == [50, 50, 5]
    assert len(seen) == len(set(seen)) == 105
    assert set(seen) == {f"trader-a-history-{i}" for i in range(105)}
    latest = client.get("/api/monitor/chat", headers=headers["trader-a"], params={"cp": "shared-peer", "limit": 1}).json()["data"]
    assert latest["messages"][0]["text"] == "NEW-ARRIVAL"
    assert sends == []


def test_chat_cursor_cannot_select_another_accounts_messages(http_business):
    _, client, headers, sends = http_business
    seed_history(http_business, "trader-a", 3)
    seed_history(http_business, "trader-b", 3)
    first = client.get("/api/monitor/chat", headers=headers["trader-a"], params={"cp": "shared-peer", "limit": 1}).json()["data"]
    response = client.get("/api/monitor/chat", headers=headers["trader-b"], params={"cp": "shared-peer", "cursor": first["nextCursor"], "user_id": "trader-a", "account_uid": "910001"})
    assert response.status_code == 200
    assert all(message["text"].startswith("trader-b-history-") for message in response.json()["data"]["messages"])
    assert "trader-a-history-" not in response.text
    assert sends == []


@pytest.mark.parametrize("value", ["not-base64!", {}, [True, "id"], [float("inf"), "id"], [NOW, ""], [NOW, "x" * 65]])
def test_invalid_chat_cursor_is_a_client_error_without_sends(http_business, value):
    _, client, headers, sends = http_business
    cursor = value if isinstance(value, str) else base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    response = client.get("/api/monitor/chat", headers=headers["trader-a"], params={"cp": "shared-peer", "cursor": cursor})
    assert response.status_code in (400, 422)
    assert sends == []


def test_chat_cursor_and_legacy_before_cannot_be_combined(http_business):
    _, client, headers, sends = http_business
    cursor = base64.urlsafe_b64encode(json.dumps([NOW, "id"]).encode()).decode()
    response = client.get("/api/monitor/chat", headers=headers["trader-a"], params={"cp": "shared-peer", "cursor": cursor, "before": NOW})
    assert response.status_code in (400, 422)
    assert sends == []


def test_runtime_diagnostics_are_owner_filtered_and_never_return_exception_text(http_business, caplog):
    app, client, headers, sends = http_business
    diagnostics = app.state.inquiry.diagnostics
    secret = "synthetic-secret-url-password-private-message"
    diagnostics.record("chat", owner="trader-a", code="MODEL_FAILED", error=RuntimeError(secret), entity="private-turn-a")
    diagnostics.record("chat", owner="trader-b", code="TOOL_FAILED", error=ValueError(secret), entity="private-turn-b")
    diagnostics.record("market", code="MARKET_POLL_FAILED", error=RuntimeError(secret))
    for owner, own, foreign in [("trader-a", "private-turn-a", "private-turn-b"), ("trader-b", "private-turn-b", "private-turn-a")]:
        response = client.get("/api/runtime/status?owner=trader-a&user_id=trader-a", headers=headers[owner])
        assert response.status_code == 200
        assert own in response.text and foreign not in response.text
        assert secret not in response.text
        entries = response.json()["data"]["components"]
        assert {entry["component"] for entry in entries} == {"chat", "market"}
        assert all(entry["status"] == "degraded" for entry in entries)
    assert secret not in caplog.text
    assert "MODEL_FAILED" in caplog.text
    assert client.get("/api/runtime/status").status_code == 401
    assert sends == []


def test_public_health_distinguishes_api_readiness_from_automation_capability(http_business):
    _, client, _, sends = http_business
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ready"] is True and data["database_ready"] is True
    assert data["background_enabled"] is False
    assert data["automation_configured"] is False
    assert "components" not in data
    assert sends == []


def test_public_health_reports_database_failure_without_exposing_error(http_business, monkeypatch):
    app, client, _, sends = http_business

    def broken_database(*args, **kwargs):
        raise RuntimeError("synthetic-database-password-and-dsn")

    monkeypatch.setattr(app.state.repository.db, "transaction", broken_database)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["data"]["database_ready"] is False
    assert response.json()["data"]["ready"] is False
    assert "synthetic-database-password-and-dsn" not in response.text
    assert sends == []


def test_first_personal_strategy_save_persists_only_for_owner_without_authorizing_inquiry(http_business):
    app, client, headers, sends = http_business
    initial = client.get("/api/strategy", headers=headers["trader-a"])
    assert initial.status_code == 200
    assert initial.json()["data"]["configured"] is False
    assert initial.json()["data"]["templates"] == []
    response = client.post("/api/strategy", headers=headers["trader-a"], json={
        "template_id": "personal", "start_offset_bp": 10, "boundary_bp": 2, "step_bp": 1,
        "strict_match": True, "system": {"s1_round_lot_wan": 1000, "s2_crossday_bp": 0}})
    assert response.status_code == 200 and response.json()["data"]["configured"] is True
    saved = client.get("/api/strategy", headers=headers["trader-a"]).json()["data"]
    assert saved["active_template_id"] == "personal"
    assert saved["templates"][0]["step_bp"] == 1
    other = client.get("/api/strategy", headers=headers["trader-b"]).json()["data"]
    assert other["configured"] is False and other["templates"] == []
    assert app.state.inquiry.commands("trader-a") == []
    assert sends == []


def preview(rig, owner="trader-a", request="preview-a"):
    _, client, headers, _ = rig
    response = client.post("/api/im/rfq/preview", headers=headers[owner], json={"request_id": request, "items": [item()]})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    return response.json()["data"]


def confirm(rig, draft, owner="trader-a", request="confirm-a"):
    _, client, headers, _ = rig
    return client.post("/api/im/rfq/send", headers=headers[owner], json={
        "batch_id": draft["batch_id"], "expected_version": draft["version"], "request_id": request,
    })


def test_preview_and_explicit_confirmation_use_the_production_route(http_business):
    app, _, _, sends = http_business
    service = InquiryService(app.state.repository)
    draft = preview(http_business)
    assert service.commands("trader-a") == []
    assert sends == []
    response = confirm(http_business, draft)
    assert response.status_code == 200, response.text
    commands = service.commands("trader-a")
    assert len(commands) == 1
    assert commands[0]["status"] == "queued"
    assert commands[0]["account_uid"] == "910001"
    assert sends == []


def test_legacy_freeform_items_cannot_bypass_authorization_in_production(http_business):
    app, client, headers, sends = http_business
    response = client.post("/api/im/rfq/send", headers=headers["trader-a"], json={
        "items": [{"code": "000100.IB", "text": "请发这个没有确认方向和量的价格", "uins": ["shared-peer"]}],
        "request_id": "legacy-attempt",
    })
    assert response.status_code in (400, 409, 422)
    assert response.json().get("ok") is not True
    assert InquiryService(app.state.repository).commands("trader-a") == []
    assert sends == []


def test_other_trader_cannot_confirm_a_stolen_batch_id(http_business):
    app, _, _, sends = http_business
    draft = preview(http_business)
    response = confirm(http_business, draft, owner="trader-b")
    assert response.status_code in (403, 404)
    service = InquiryService(app.state.repository)
    assert service.commands("trader-a") == service.commands("trader-b") == []
    assert sends == []


def test_repeated_http_confirmation_has_one_command_identity(http_business):
    app, _, _, _ = http_business
    draft = preview(http_business)
    for _ in range(2):
        response = confirm(http_business, draft)
        assert response.status_code == 200, response.text
    assert len(InquiryService(app.state.repository).commands("trader-a")) == 1


@pytest.mark.parametrize("path", [
    "/api/im/rfq/status", "/api/monitor/summary", "/api/monitor/counterparties", "/api/monitor/chat?cp=shared-peer",
])
def test_authorized_get_is_functional_and_never_dispatches_or_changes_business_state(http_business, path):
    app, client, headers, sends = http_business
    draft = preview(http_business)
    assert confirm(http_business, draft).status_code == 200
    service = InquiryService(app.state.repository)
    before = (service.commands("trader-a"), service.sessions("trader-a"))
    for _ in range(3):
        response = client.get(path, headers=headers["trader-a"])
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
    assert (service.commands("trader-a"), service.sessions("trader-a")) == before
    assert sends == []


def test_status_query_cannot_read_foreign_sessions_via_query_parameters(http_business):
    app, client, headers, _ = http_business
    draft = preview(http_business)
    assert confirm(http_business, draft).status_code == 200
    own = client.get("/api/im/rfq/status", headers=headers["trader-a"])
    assert own.status_code == 200
    assert len(own.json()["data"]["items"]) == 1
    foreign = client.get("/api/im/rfq/status?user_id=trader-a&account_uid=910001", headers=headers["trader-b"])
    assert foreign.status_code in (200, 403)
    if foreign.status_code == 200:
        assert foreign.json()["data"]["items"] == []
    for session in InquiryService(app.state.repository).sessions("trader-a"):
        assert session["session_id"] not in foreign.text


def test_stopping_through_api_cancels_only_the_authenticated_traders_queued_work(http_business):
    app, client, headers, sends = http_business
    for owner in ("trader-a", "trader-b"):
        draft = preview(http_business, owner, "preview-" + owner)
        assert confirm(http_business, draft, owner, "confirm-" + owner).status_code == 200
    for _ in range(2):
        response = client.post("/api/bot/toggle", headers=headers["trader-a"], json={"status": "停止", "user_id": "trader-b"})
        assert response.status_code == 200, response.text
    service = InquiryService(app.state.repository)
    assert service.commands("trader-a")[0]["status"] == "cancelled"
    assert service.commands("trader-b")[0]["status"] == "queued"
    assert sends == []


def test_receipt_cannot_be_read_by_another_authenticated_trader(http_business):
    app, client, headers, _ = http_business
    draft = preview(http_business)
    assert confirm(http_business, draft).status_code == 200
    command = InquiryService(app.state.repository).commands("trader-a")[0]
    path = "/api/im/receipts/" + command["command_id"]
    own = client.get(path, headers=headers["trader-a"])
    assert own.status_code == 200
    assert own.json()["data"]["status"] == "queued"
    assert own.json()["data"]["ok"] is False
    foreign = client.get(path, headers=headers["trader-b"])
    assert foreign.status_code in (403, 404)
    assert command["text"] not in foreign.text


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_wrong_or_ill_typed_preview_version_does_not_create_commands(http_business, version):
    app, client, headers, sends = http_business
    draft = preview(http_business)
    response = client.post("/api/im/rfq/send", headers=headers["trader-a"], json={
        "batch_id": draft["batch_id"], "expected_version": version, "request_id": "bad-version",
    })
    assert response.status_code in (400, 409, 422)
    assert InquiryService(app.state.repository).commands("trader-a") == []
    assert sends == []


@pytest.mark.parametrize("field,value", [("volume_wan", "0"), ("volume_wan", "-1"),
                                          ("step_bp", "NaN"), ("limit_yield_pct", "Infinity"),
                                          ("opening_yield_pct", ""), ("our_side", "ofr")])
def test_invalid_explicit_authorization_is_rejected_before_any_command(http_business, field, value):
    app, client, headers, sends = http_business
    invalid = {**item(), field: value}
    response = client.post("/api/im/rfq/preview", headers=headers["trader-a"], json={"request_id": "invalid", "items": [invalid]})
    assert response.status_code in (400, 422)
    assert InquiryService(app.state.repository).commands("trader-a") == []
    assert sends == []


def test_merged_unicode_text_over_limit_rejects_the_whole_preview(http_business):
    app, client, headers, sends = http_business
    items = [{**item(), "code": f"{index:06d}.IB", "name": "合成" * 40} for index in range(20)]
    response = client.post("/api/im/rfq/preview", headers=headers["trader-a"], json={"request_id": "too-long", "items": items})
    assert response.status_code == 400
    assert "1500" in response.text
    assert InquiryService(app.state.repository).commands("trader-a") == []
    assert sends == []


def test_bot_requires_an_explicit_target_and_never_toggles_on_bad_body(http_business):
    _, client, headers, _ = http_business
    before = client.get("/api/monitor/summary", headers=headers["trader-a"]).json()["data"]["bot"]
    response = client.post("/api/bot/toggle", headers=headers["trader-a"], json={"on": False})
    assert response.status_code == 400
    after = client.get("/api/monitor/summary", headers=headers["trader-a"]).json()["data"]["bot"]
    assert after == before


def test_unbound_authenticated_user_cannot_read_or_create_business_state(http_business):
    _, client, _, sends = http_business
    user = "unbound-trader"
    sign = hashlib.md5(f"{user}|{user}|{NOW}|synthetic-sso".encode()).hexdigest()
    response = client.post("/api/auth/sso", json={"userName": user, "nickName": user, "ts": str(NOW), "sign": sign})
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json()["token"]}
    assert client.get("/api/im/rfq/status", headers=headers).status_code == 403
    assert client.post("/api/im/rfq/preview", headers=headers, json={"request_id": "unbound", "items": [item()]}).status_code == 403
    assert sends == []


def test_conflicting_receipts_remain_visible_as_unknown_in_the_rfq_status(http_business):
    app, client, headers, _ = http_business
    draft = preview(http_business)
    assert confirm(http_business, draft).status_code == 200
    service = app.state.inquiry
    command = service.commands("trader-a")[0]
    identity = {"account_uid": "910001", "peer_id": "shared-peer"}
    service.receipt(command["command_id"], **identity, status="sent", evidence="sent-proof")
    service.receipt(command["command_id"], **identity, status="cancelled", evidence="conflicting-cancel-proof")
    assert service.commands("trader-a")[0]["status"] == "unknown"
    response = client.get("/api/im/rfq/status", headers=headers["trader-a"])
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["status"] == "待核对"


def test_nested_recipient_json_is_a_clean_client_error_not_http_500(http_business):
    app, _, headers, sends = http_business
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/im/rfq/preview", headers=headers["trader-a"], json={
            "request_id": "invalid-recipient", "items": [{**item(), "peer_ids": [["shared-peer"]]}],
        })
    assert response.status_code in (400, 422)
    assert InquiryService(app.state.repository).commands("trader-a") == []
    assert sends == []


def test_bond_name_cannot_inject_trade_acceptance_into_the_initial_outbound_message(http_business):
    from server.runtime import Dispatcher

    app, client, headers, _ = http_business
    response = client.post("/api/im/rfq/preview", headers=headers["trader-a"], json={
        "request_id": "name-injection", "items": [{**item(), "name": "合成债，成交，已确认交易"}],
    })
    if response.status_code in (400, 422):
        assert app.state.inquiry.commands("trader-a") == []
        return
    assert response.status_code == 200
    confirmed = confirm(http_business, response.json()["data"])
    if confirmed.status_code in (400, 409, 422):
        return
    assert confirmed.status_code == 200
    sent = []

    class Transport:
        def send(self, command):
            sent.append(command)
            return {key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "sent", "evidence": "synthetic-native-id"}

        def reconcile(self, _):
            return None

    Dispatcher(app.state.inquiry, Transport(), clock=lambda: NOW).run_once()
    assert sent == [], "The initial-message path must enforce the same no-trade-acceptance boundary"


def adjudicate_conflicting_command(rig):
    app, client, headers, _ = rig
    draft = preview(rig)
    assert confirm(rig, draft).status_code == 200
    service = app.state.inquiry
    command = service.commands("trader-a")[0]
    identity = {"account_uid": "910001", "peer_id": "shared-peer"}
    service.receipt(command["command_id"], **identity, status="sent", evidence="reviewed-sent-e1")
    service.receipt(command["command_id"], **identity, status="cancelled", evidence="reviewed-cancel-e2")
    path = "/api/im/receipts/" + command["command_id"]
    current = client.get(path, headers=headers["trader-a"]).json()["data"]
    resolved = client.post(path + "/reconcile", headers=headers["trader-a"], json={
        "status": "failed", "evidence": "synthetic-reviewed-native-record", "confirmed": True,
        "expected_result_hash": current["expected_result_hash"],
    })
    assert resolved.status_code == 200, resolved.text
    return service, command, identity, path


def test_inconclusive_poll_cannot_erase_an_explicit_human_receipt_verdict(http_business):
    _, client, headers, _ = http_business
    service, command, identity, path = adjudicate_conflicting_command(http_business)
    service.receipt(command["command_id"], **identity, status="unknown", evidence="query-timeout-e3")
    result = client.get(path, headers=headers["trader-a"]).json()["data"]
    assert result["status"] == "failed"
    assert result["adjudicated"] is True


def test_old_reviewed_evidence_replay_after_an_unknown_poll_remains_idempotent(http_business):
    _, client, headers, _ = http_business
    service, command, identity, path = adjudicate_conflicting_command(http_business)
    service.receipt(command["command_id"], **identity, status="unknown", evidence="query-timeout-e3")
    service.receipt(command["command_id"], **identity, status="sent", evidence="reviewed-sent-e1")
    result = client.get(path, headers=headers["trader-a"]).json()["data"]
    assert result["status"] == "failed"
    assert result["adjudicated"] is True


def test_genuinely_new_conflicting_execution_evidence_can_reopen_manual_verdict(http_business):
    _, client, headers, _ = http_business
    service, command, identity, path = adjudicate_conflicting_command(http_business)
    service.receipt(command["command_id"], **identity, status="sent", evidence="genuinely-new-native-sent-evidence")
    result = client.get(path, headers=headers["trader-a"]).json()["data"]
    assert result["status"] == "unknown"
    status = client.get("/api/im/rfq/status", headers=headers["trader-a"]).json()["data"]
    assert status["items"][0]["status"] == "待核对"


def test_manual_reconciliation_does_not_implicitly_resume_queued_social_replies(http_business):
    from server.dify_client import ChatResult
    from server.runtime import Dispatcher

    app, client, headers, _ = http_business
    draft = preview(http_business)
    assert confirm(http_business, draft).status_code == 200
    service, runner = app.state.inquiry, app.state.chat_runner
    command = service.commands("trader-a")[0]
    class UncertainTransport:
        def send(self, current):
            return {key: current[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "unknown", "evidence": "transport-outcome-unknown"}

    Dispatcher(service, UncertainTransport(), clock=lambda: NOW).run_once()
    assert service.commands("trader-a")[0]["status"] == "unknown"
    incoming = service.ingest("trader-a", {"event_id": "waiting-social", "account_uid": "910001", "peer_id": "shared-peer", "kind": "message",
                             "source_seq": 1, "text": "谢谢", "occurred_at": NOW + 1, "historical": False})
    path = "/api/im/receipts/" + command["command_id"]
    old = client.get(path, headers=headers["trader-a"]).json()["data"]
    response = client.post(path + "/reconcile", headers=headers["trader-a"], json={"status": "failed", "evidence": "synthetic-native-ledger-check",
        "confirmed": True, "expected_result_hash": old["expected_result_hash"]})
    assert response.status_code == 200
    calls = []

    class Agent:
        def chat(self, **kwargs):
            calls.append(kwargs)
            ref = kwargs["inputs"]["turn_ref"]
            context = runner.context(ref)
            value = {"schema_version": "1", "turn_id": context["turn_id"], "conversation_epoch": context["conversation_epoch"],
                     "source_message_id": context["source_message_id"], "observations": [], "proposed_actions": [{"type": "SOCIAL_REPLY"}],
                     "public_message": "不客气。", "trader_view": {}, "memory_patch": None, "history_request": None, "validation_ref": None}
            value["validation_ref"] = runner.validate(ref, value)["validation_ref"]
            return ChatResult(value, "synthetic-conversation", "synthetic-message", "synthetic-task", "synthetic-run")

    runner.client = Agent()
    runner.process(incoming["message_id"])
    assert calls == [], "Recording a manual verdict must not release old automatic replies"
    assert len(service.commands("trader-a")) == 1


@pytest.mark.parametrize("change", [{"confirmed": False}, {"confirmed": "true"}, {"evidence": "short"}, {"expected_result_hash": "stale"}])
def test_manual_reconciliation_rejects_missing_consent_short_evidence_or_stale_state(http_business, change):
    app, client, headers, _ = http_business
    draft = preview(http_business)
    assert confirm(http_business, draft).status_code == 200
    command = app.state.inquiry.commands("trader-a")[0]
    app.state.inquiry.receipt(command["command_id"], account_uid="910001", peer_id="shared-peer", status="unknown", evidence="unknown-outcome")
    path = "/api/im/receipts/" + command["command_id"]
    current = client.get(path, headers=headers["trader-a"]).json()["data"]
    body = {"status": "failed", "confirmed": True, "evidence": "synthetic-reviewed-native-record", "expected_result_hash": current["expected_result_hash"], **change}
    response = client.post(path + "/reconcile", headers=headers["trader-a"], json=body)
    assert response.status_code == 400
    assert app.state.inquiry.commands("trader-a")[0]["status"] == "unknown"
