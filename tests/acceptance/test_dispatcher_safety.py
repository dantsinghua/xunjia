"""Independent dispatcher gates; synthetic transports and durable SQLite state."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from server.business import InquiryService
from server.repository import Repository


NOW = 1_800_000_000


class Transport:
    def __init__(self):
        self.sends = []
        self.reconciliations = []
        self.cancellations = []
        self.send_result = "sent"
        self.reconcile_result = None

    @staticmethod
    def receipt(command, status):
        return {"command_id": command["command_id"], "account_uid": command["account_uid"],
                "peer_id": command["peer_id"], "content_hash": command["content_hash"],
                "status": status, "evidence": "synthetic-native-evidence-" + status}

    def send(self, command):
        self.sends.append(command.copy())
        if isinstance(self.send_result, Exception):
            raise self.send_result
        return self.receipt(command, self.send_result)

    def reconcile(self, command):
        self.reconciliations.append(command.copy())
        return None if self.reconcile_result is None else self.receipt(command, self.reconcile_result)

    def cancel(self, command):
        self.cancellations.append(command.copy())
        return self.receipt(command, "cancelled")


@pytest.fixture
def dispatcher_case(tmp_path):
    from server.runtime import Dispatcher

    url = "sqlite:///" + str(tmp_path / "dispatch.db")
    service = InquiryService(Repository(url), clock=lambda: NOW)
    service.repo.bind_account("trader-a", "910001", "synthetic-wb")
    draft = service.preview("trader-a", "initial", [{
        "code": "000100.IB", "name": "合成债", "peer_ids": ["synthetic-peer"],
        "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50",
        "limit_yield_pct": "2.40", "step_bp": "2",
    }])
    service.confirm("trader-a", draft["batch_id"], draft["version"], "confirm-initial")
    transport = Transport()
    return service, transport, Dispatcher(service, transport, clock=lambda: NOW), url


def test_five_dispatchers_claim_one_queued_command_only_once(dispatcher_case):
    from server.runtime import Dispatcher

    service, transport, _, _ = dispatcher_case
    ready = threading.Barrier(5)

    def dispatch():
        ready.wait(timeout=5)
        Dispatcher(service, transport, clock=lambda: NOW).run_once()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: dispatch(), range(5)))
    assert len(transport.sends) == 1
    assert service.commands("trader-a")[0]["status"] == "sent"


def test_transport_acceptance_is_not_an_execution_receipt(dispatcher_case):
    service, transport, dispatcher, _ = dispatcher_case
    transport.send_result = "accepted"
    dispatcher.run_once()
    assert service.commands("trader-a")[0]["status"] == "pending"
    assert service.sessions("trader-a")[0]["state"] == "WAITING_SEND"


def test_response_loss_becomes_unknown_and_restart_reconciles_without_resending(dispatcher_case):
    from server.runtime import Dispatcher

    service, transport, dispatcher, url = dispatcher_case
    transport.send_result = TimeoutError("synthetic response lost after acceptance")
    dispatcher.run_once()
    command_id = service.commands("trader-a")[0]["command_id"]
    assert service.commands("trader-a")[0]["status"] == "unknown"
    restarted = InquiryService(Repository(url), clock=lambda: NOW + 1)
    again = Dispatcher(restarted, transport, clock=lambda: NOW + 1)
    for _ in range(3):
        again.run_once()
    assert len(transport.sends) == 1
    assert transport.reconciliations
    assert all(command["command_id"] == command_id for command in transport.reconciliations)
    assert restarted.commands("trader-a")[0]["status"] == "unknown"


def test_late_reconciliation_promotes_only_the_original_command(dispatcher_case):
    service, transport, dispatcher, _ = dispatcher_case
    transport.send_result = "pending"
    dispatcher.run_once()
    command_id = service.commands("trader-a")[0]["command_id"]
    transport.reconcile_result = "sent"
    dispatcher.run_once()
    assert len(transport.sends) == 1
    assert service.commands("trader-a")[0]["command_id"] == command_id
    assert service.commands("trader-a")[0]["status"] == "sent"
    assert service.sessions("trader-a")[0]["state"] == "NEGOTIATING"


def test_stop_before_dispatch_prevents_any_transport_send(dispatcher_case):
    service, transport, dispatcher, _ = dispatcher_case
    service.set_bot("trader-a", False)
    dispatcher.run_once()
    assert transport.sends == []
    assert service.commands("trader-a")[0]["status"] == "cancelled"


def test_stop_pending_uses_cancel_barrier_and_never_reopens_session(dispatcher_case):
    service, transport, dispatcher, _ = dispatcher_case
    transport.send_result = "pending"
    dispatcher.run_once()
    service.set_bot("trader-a", False)
    dispatcher.run_once()
    assert len(transport.sends) == 1
    assert transport.cancellations
    assert service.commands("trader-a")[0]["status"] == "cancelled"
    assert service.sessions("trader-a")[0]["state"] == "STOPPED"


@pytest.mark.parametrize("field,value", [("account_uid", "other-account"), ("peer_id", "other-peer"),
                                        ("command_id", "other-command"), ("content_hash", "wrong-content-hash")])
def test_wrong_execution_receipt_cannot_mark_a_command_sent(dispatcher_case, field, value):
    service, transport, dispatcher, _ = dispatcher_case
    original = transport.send

    def mismatched(command):
        return {**original(command), field: value}

    transport.send = mismatched
    dispatcher.run_once()
    assert service.commands("trader-a")[0]["status"] != "sent"
    assert service.sessions("trader-a")[0]["state"] == "WAITING_SEND"


def test_expired_authorization_is_never_given_to_transport(dispatcher_case):
    from server.runtime import Dispatcher

    service, transport, _, _ = dispatcher_case
    Dispatcher(service, transport, clock=lambda: NOW + 300).run_once()
    assert transport.sends == []
    assert service.commands("trader-a")[0]["status"] in ("cancelled", "failed")


def test_stop_during_network_io_is_not_blocked_and_late_ack_only_updates_history(dispatcher_case):
    service, transport, dispatcher, _ = dispatcher_case
    entered, release = threading.Event(), threading.Event()
    original = transport.send

    def delayed(command):
        entered.set()
        assert release.wait(5)
        return original(command)

    transport.send = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(dispatcher.run_once)
        try:
            assert entered.wait(3)
            stop = pool.submit(service.set_bot, "trader-a", False)
            stop.result(timeout=2)
            stopped = service.sessions("trader-a")
            assert stopped[0]["state"] == "STOPPED"
        finally:
            release.set()
        dispatch.result(timeout=3)
    assert service.sessions("trader-a") == stopped
    assert service.commands("trader-a")[0]["status"] == "sent"
    assert len(transport.sends) == 1


def test_process_crash_after_claim_does_not_requeue_or_resend_after_restart(dispatcher_case):
    from server.runtime import Dispatcher

    service, transport, dispatcher, url = dispatcher_case

    class SimulatedCrash(BaseException):
        pass

    def crash_after_external_submission(command):
        transport.sends.append(command.copy())
        raise SimulatedCrash()

    transport.send = crash_after_external_submission
    with pytest.raises(SimulatedCrash):
        dispatcher.run_once()
    restarted = InquiryService(Repository(url), clock=lambda: NOW + 1)
    assert restarted.commands("trader-a")[0]["status"] == "pending"
    Dispatcher(restarted, transport, clock=lambda: NOW + 1).run_once()
    assert len(transport.sends) == 1
    assert len(transport.reconciliations) == 1
    assert restarted.commands("trader-a")[0]["status"] in ("pending", "unknown")
