"""The production lifespan owns dispatch; opening a browser is not the trigger."""
import threading

from fastapi.testclient import TestClient

from server.legacy_app import create_app
from server.auth import AuthConfig


def test_production_background_dispatches_without_get_requests_and_stops_on_shutdown(tmp_path):
    seen = threading.Event()
    sent = []

    class Transport:
        def send(self, command):
            sent.append(command.copy())
            seen.set()
            return {key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "sent", "evidence": "synthetic-native-id"}

        def reconcile(self, command):
            return None

        def cancel(self, command):
            raise AssertionError("No cancellation was requested")

    app = create_app(database_url="sqlite:///" + str(tmp_path / "lifecycle.db"), enable_background=True, transport=Transport(),
                     auth_config=AuthConfig(sso_secret="synthetic", session_secret="synthetic-session-secret-over-32", clock=lambda: 1_800_000_000))
    service = app.state.inquiry
    service.repo.bind_account("owner", "910001", "synthetic-wb")
    item = {"code": "000100.IB", "name": "合成债", "peer_ids": ["synthetic-peer"], "our_side": "BUY",
            "volume_wan": "1000", "opening_yield_pct": "2.50", "limit_yield_pct": "2.40", "step_bp": "2"}
    first = service.preview("owner", "first", [item])
    service.confirm("owner", first["batch_id"], first["version"], "confirm-first")
    assert sent == []
    with TestClient(app):
        assert seen.wait(3), "The background worker must dispatch without any HTTP reads"
    assert len(sent) == 1
    seen.clear()
    second = service.preview("owner", "after-shutdown", [{**item, "peer_ids": ["second-peer"]}])
    service.confirm("owner", second["batch_id"], second["version"], "confirm-after-shutdown")
    assert not seen.wait(1), "No worker may remain alive after application shutdown"
    assert len(sent) == 1


def test_stopped_accounts_queued_messages_do_not_starve_another_accounts_live_turn(tmp_path):
    clock = [1_800_000_000]
    seen = threading.Event()
    queries = []

    class Transport:
        def send(self, command):
            return {key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "sent", "evidence": "synthetic-native-id"}

        def reconcile(self, _):
            return None

    class Agent:
        def chat(self, **kwargs):
            queries.append(kwargs["query"])
            if kwargs["query"] == "B的有效新消息":
                seen.set()
            raise RuntimeError("Synthetic model stops after proving scheduling")

    app = create_app(database_url="sqlite:///" + str(tmp_path / "fairness.db"), enable_background=True,
                     transport=Transport(), chat_client=Agent(),
                     auth_config=AuthConfig(sso_secret="synthetic", session_secret="synthetic-session-secret-over-32", clock=lambda: clock[0]))
    service = app.state.inquiry
    for owner, account in [("a", "910001"), ("b", "910002")]:
        service.repo.bind_account(owner, account, "synthetic-wb")
        draft = service.preview(owner, "first", [{"code": "000100.IB", "name": "合成债", "peer_ids": ["peer"], "our_side": "BUY",
                                                 "volume_wan": "1000", "opening_yield_pct": "2.50", "limit_yield_pct": "2.40", "step_bp": "2"}])
        service.confirm(owner, draft["batch_id"], draft["version"], "confirm")
        app.state.dispatcher.run_once()
    clock[0] += 3
    for seq in range(1, 6):
        service.ingest("a", {"event_id": f"stopped-{seq}", "account_uid": "910001", "peer_id": "peer", "kind": "message",
                             "source_seq": seq, "text": "A已停止的旧消息", "occurred_at": clock[0] - 2})
    service.set_bot("a", False)
    service.ingest("b", {"event_id": "active-b", "account_uid": "910002", "peer_id": "peer", "kind": "message",
                         "source_seq": 1, "text": "B的有效新消息", "occurred_at": clock[0] - 1})
    with TestClient(app):
        assert seen.wait(3), "Ineligible early messages must not permanently consume the batch limit"
    assert queries == ["B的有效新消息"]
