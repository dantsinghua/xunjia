"""Transaction-level acceptance; HTTP mounting and transport remain separate gates."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import threading

import pytest

from server.business import InquiryService
from server.domain import goal
from server.repository import Repository


@pytest.fixture
def service(tmp_path):
    repo = Repository("sqlite:///" + str(tmp_path / "business.db"))
    repo.bind_account("trader-a", "910001", "synthetic-wb")
    repo.bind_account("trader-b", "910002", "synthetic-wb")
    return InquiryService(repo, clock=lambda: 1_800_000_000)


def item(**overrides):
    return {"code": "000100.IB", "name": "合成债券", "peer_ids": ["synthetic-peer"],
            "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50",
            "limit_yield_pct": "2.40", "step_bp": "2", **overrides}


def authorize(service, *, owner="trader-a", request="batch-a", items=None):
    draft = service.preview(owner, request, items or [item()])
    return service.confirm(owner, draft["batch_id"], draft["version"], "confirm-" + request)


def test_preview_is_readable_without_creating_sessions_or_commands(service):
    draft = service.preview("trader-a", "preview-only", [item()])
    assert draft["status"] == "DRAFT"
    assert draft["commands_preview"][0]["text"]
    assert service.commands("trader-a") == []
    assert service.sessions("trader-a") == []


def test_five_simultaneous_confirmations_produce_one_business_command(service):
    draft = service.preview("trader-a", "concurrent", [item()])
    barrier = threading.Barrier(5)

    def confirm():
        barrier.wait(timeout=5)
        return service.confirm("trader-a", draft["batch_id"], draft["version"], "same-confirm")

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: confirm(), range(5)))
    assert len({result["commands"][0]["command_id"] for result in results}) == 1
    assert len(service.commands("trader-a")) == 1
    assert len(service.sessions("trader-a")) == 1


def test_same_request_with_changed_amount_conflicts_without_changing_authorization(service):
    original = service.preview("trader-a", "immutable", [item()])
    with pytest.raises(ValueError):
        service.preview("trader-a", "immutable", [item(volume_wan="2000")])
    current = service.preview("trader-a", "immutable", [item()])
    assert current == original
    assert service.commands("trader-a") == []


def test_foreign_trader_cannot_confirm_or_read_someone_elses_batch(service):
    draft = service.preview("trader-a", "private", [item()])
    with pytest.raises(PermissionError):
        service.confirm("trader-b", draft["batch_id"], draft["version"], "stolen")
    assert service.commands("trader-a") == service.commands("trader-b") == []
    service.confirm("trader-a", draft["batch_id"], draft["version"], "real")
    assert service.sessions("trader-b") == []
    assert service.commands("trader-b") == []


def test_invalid_second_item_prevents_any_partial_batch_authorization(service):
    with pytest.raises(ValueError):
        service.preview("trader-a", "mixed", [item(), item(code="000200.IB", volume_wan="NaN")])
    assert service.commands("trader-a") == []
    assert service.sessions("trader-a") == []


def test_two_accounts_with_same_peer_and_request_id_stay_separate(service):
    first = authorize(service, request="same")
    second = authorize(service, owner="trader-b", request="same")
    assert first["commands"][0]["command_id"] != second["commands"][0]["command_id"]
    assert first["commands"][0]["account_uid"] == "910001"
    assert second["commands"][0]["account_uid"] == "910002"


@pytest.mark.parametrize("account,peer", [("910002", "synthetic-peer"), ("910001", "other-peer")])
def test_mismatched_receipt_identity_cannot_promote_a_session(service, account, peer):
    command = authorize(service)["commands"][0]
    before = service.sessions("trader-a")
    with pytest.raises(PermissionError):
        service.receipt(command["command_id"], account_uid=account, peer_id=peer, status="sent", evidence="fake-native-id")
    assert service.sessions("trader-a") == before
    assert service.commands("trader-a")[0]["status"] == "queued"


def test_repeated_stop_and_late_success_never_reopen_the_session(service):
    command = authorize(service)["commands"][0]
    service.set_bot("trader-a", False)
    service.set_bot("trader-a", False)
    stopped = service.sessions("trader-a")
    service.receipt(command["command_id"], account_uid="910001", peer_id="synthetic-peer", status="sent", evidence="late-native-id")
    assert service.sessions("trader-a") == stopped
    assert stopped[0]["state"] == "STOPPED"
    assert service.commands("trader-a")[0]["status"] == "unknown"


def test_duplicate_success_receipt_does_not_advance_the_session_twice(service):
    command = authorize(service)["commands"][0]
    evidence = dict(account_uid="910001", peer_id="synthetic-peer", status="sent", evidence="native-id")
    service.receipt(command["command_id"], **evidence)
    first = service.sessions("trader-a")
    service.receipt(command["command_id"], **evidence)
    assert service.sessions("trader-a") == first


def test_restart_keeps_stopped_state_and_command_identity(service, tmp_path):
    command = authorize(service)["commands"][0]
    service.set_bot("trader-a", False)
    restarted = InquiryService(Repository("sqlite:///" + str(tmp_path / "business.db")), clock=lambda: 1_800_000_001)
    assert restarted.commands("trader-a")[0]["command_id"] == command["command_id"]
    assert restarted.commands("trader-a")[0]["status"] == "cancelled"
    assert restarted.sessions("trader-a")[0]["state"] == "STOPPED"


@pytest.mark.parametrize("side,boundary,peer_side,peer,expected", [
    ("BUY", "2.40", "ofr", "2.20", "20"),
    ("SELL", "2.10", "bid", "2.40", "30"),
    ("BUY", "2.40", "ofr", "2.40", "0"),
    ("SELL", "2.10", "bid", "2.00", "-10"),
])
def test_directional_goal_uses_actual_peer_quote(side, boundary, peer_side, peer, expected):
    result = goal(side, boundary, peer_side, peer)
    assert Decimal(result["raw_gap_bp"]) == Decimal(expected)
    assert Decimal(result["remaining_gap_bp"]) == max(Decimal(0), Decimal(expected))
    assert result["target_reached"] is (Decimal(expected) <= 0)


@pytest.mark.parametrize("peer_side,valid", [("bid", True), ("ofr", False)])
def test_wrong_side_or_withdrawn_peer_quote_cannot_reach_buy_target(peer_side, valid):
    result = goal("BUY", "2.40", peer_side, "3.00", valid=valid)
    assert result["raw_gap_bp"] is None
    assert result["target_reached"] is False


def test_replayed_success_does_not_erase_a_conflict_between_execution_receipts(service):
    command = authorize(service)["commands"][0]
    identity = {"account_uid": "910001", "peer_id": "synthetic-peer"}
    service.receipt(command["command_id"], **identity, status="sent", evidence="original-sent-proof")
    service.receipt(command["command_id"], **identity, status="cancelled", evidence="contradictory-cancel-proof")
    assert service.commands("trader-a")[0]["status"] == "unknown"
    service.receipt(command["command_id"], **identity, status="sent", evidence="original-sent-proof")
    assert service.commands("trader-a")[0]["status"] == "unknown", "A replay cannot resolve contradictory execution evidence"


def test_first_explicit_authorization_can_open_a_history_only_conversation_without_replaying_history(service):
    from server.chat_runtime import ChatRunner
    from server.runtime import Dispatcher

    history = service.ingest("trader-a", {"event_id": "history-only", "account_uid": "910001", "peer_id": "synthetic-peer",
                            "kind": "message", "source_seq": 7, "text": "您好", "occurred_at": 1_799_999_000, "historical": True})
    assert service.conversations("trader-a")[0]["mode"] == "HUMAN"
    authorize(service)
    assert service.conversations("trader-a")[0]["mode"] == "AUTO"

    class Transport:
        def send(self, command):
            return {key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "sent", "evidence": "native-initial"}

        def reconcile(self, _):
            return None

    calls = []

    class Agent:
        def chat(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("Historical messages must not reach the model")

    Dispatcher(service, Transport(), clock=lambda: 1_800_000_000).run_once()
    ChatRunner(service, Agent(), clock=lambda: 1_800_000_000).process(history["message_id"])
    assert calls == []
    assert len(service.commands("trader-a")) == 1


def test_explicit_human_takeover_of_a_history_only_conversation_is_not_bypassed_by_first_authorization(service):
    service.ingest("trader-a", {"event_id": "history-only", "account_uid": "910001", "peer_id": "synthetic-peer",
                    "kind": "message", "source_seq": 7, "text": "您好", "occurred_at": 1_799_999_000, "historical": True})
    conversation = service.conversations("trader-a")[0]["conversation_id"]
    service.takeover("trader-a", conversation)
    with pytest.raises(ValueError):
        authorize(service)
    assert service.commands("trader-a") == []
    assert service.conversations("trader-a")[0]["mode"] == "HUMAN"
