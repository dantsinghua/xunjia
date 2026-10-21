"""Independent receiver-to-backend fence acceptance through real envelopes."""
import copy

import pytest

from relay.collector_bridge import CollectorBridge
from server.bond_tools import BondTools
from server.business import InquiryService
from server.chat_runtime import ChatRunner
from server.inbox import Inbox
from server.receiver_ledger import ReceiverLedger
from server.repository import Repository
from server.runtime import Dispatcher
from server.transports import TransportRouter
from server.file_gateway_transport import FileGatewayTransport
from tests.cutover.file_gateway_fakes import Gateway
from test_chat_runner_safety import SignedAgent
from test_dual_transport import NOW, Mailbox, binding, command, mail, native_event


def stack(binding, tmp_path):
    objects, mailbox = Gateway(), Mailbox()
    source_path = tmp_path / "source.db"
    oss = FileGatewayTransport(binding, objects.client('down'), objects.client('up'), source_path, role='collector', clock=lambda: NOW)
    smtp = mail(binding, source_path, mailbox)
    receiver = ReceiverLedger(tmp_path / "receiver.db", binding, clock=lambda: NOW)
    bridge = CollectorBridge(ledger=receiver, adapters={"oss": oss, "mail": smtp}, native_send=lambda _: None, native_reconcile=lambda _: None)
    subscriber = FileGatewayTransport(binding, objects.client('up'), objects.client('down'), tmp_path / 'subscriber.db', role='service', clock=lambda: NOW)
    imap = mail(binding, tmp_path / "subscriber.db", mailbox)
    return bridge, receiver, subscriber, imap, mailbox


def deliver_mail(mailbox):
    mailbox.messages = {index + 1: message.as_bytes() for index, message in enumerate(mailbox.sent)}


def test_manual_fence_is_identical_on_both_signed_routes_and_replay_never_changes_business_hash(binding, tmp_path):
    bridge, ledger, oss, imap, mailbox = stack(binding, tmp_path)
    event = native_event(direction="out", role="human")
    assert bridge.publish_native(event) == 1
    control = ledger.event_control(event["event_id"])
    assert control["manual_fence_id"] and control["manual_fence_version"] == 1
    deliver_mail(mailbox)
    first_oss, first_mail = oss.poll()[0], imap.poll()[0]
    for item in (first_oss, first_mail):
        envelope = item["envelope"]
        assert {key: envelope[key] for key in control} == control
        assert not any(key in envelope["payload"] for key in control)
    assert first_oss["envelope"]["payload_hash"] == first_mail["envelope"]["payload_hash"]
    assert first_oss["envelope"]["payload"] == first_mail["envelope"]["payload"]
    original = copy.deepcopy(first_oss["envelope"])
    oss.ack(first_oss["source_token"])
    imap.ack(first_mail["source_token"])
    bridge.publish_native(event)
    assert ledger.event_control(event["event_id"]) == control
    bridge.replay_events(1, 1)
    deliver_mail(mailbox)
    # Already-ACKed subscribers must not replay business consumption. Inspect
    # the real newly signed publication bytes to prove the immutable envelope.
    oss_bytes = bridge.adapters["oss"].outbound.opener.queues["up.invalid"][-1][1]
    mail_bytes = list(mailbox.sent[-1].iter_attachments())[0].get_payload(decode=True)
    for blob in (oss_bytes, mail_bytes):
        replay = bridge.codec.unpack(blob)
        assert replay["transport_replay"] is True
        assert replay["payload"] == original["payload"] and replay["payload_hash"] == original["payload_hash"]
        assert {key: replay[key] for key in control} == control
    assert oss.poll() == [] and imap.poll() == []


def test_high_control_version_cannot_bypass_unobserved_or_obsolete_manual_fence(binding, tmp_path):
    bridge, ledger, _, _, _ = stack(binding, tmp_path)
    event = native_event(direction="out", role="human")
    bridge.publish_native(event)
    first = ledger.event_control(event["event_id"])
    calls = []

    def native(value):
        calls.append(value["command_id"])
        return {"status": "sent", "evidence": "synthetic-native-proof"}

    missing = command(binding, command_id="high-but-unaware", control_version=1000)
    assert ledger.execute(missing, native)["status"] == "cancelled"
    wrong = command(binding, command_id="wrong-fence", control_version=1001, receiver_fence_id="invented-fence", receiver_fence_version=first["manual_fence_version"])
    assert ledger.execute(wrong, native)["status"] == "cancelled"
    authorized = command(binding, command_id="aware-command", control_version=1002, receiver_fence_id=first["manual_fence_id"], receiver_fence_version=first["manual_fence_version"])
    for _ in range(2):
        assert ledger.execute(authorized, native)["status"] == "sent"
    assert calls == ["aware-command"]
    bridge.publish_native(native_event(event_id="second-manual", ext_msg_id="second-native", direction="out", role="human"))
    stale = {**authorized, "command_id": "old-fence-high-version", "control_version": 9999}
    assert ledger.execute(stale, native)["status"] == "cancelled"
    assert calls == ["aware-command"]


def authorize(service, request="first", code="000100.IB"):
    draft = service.preview("owner", request, [{"code": code, "name": "合成债", "peer_ids": ["synthetic-peer"],
        "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50", "limit_yield_pct": "2.40", "step_bp": "2"}])
    return service.confirm("owner", draft["batch_id"], draft["version"], request + "-confirm")


def test_received_manual_fence_requires_explicit_resume_then_flows_into_agent_and_manual_commands(binding, tmp_path):
    bridge, ledger, subscriber, _, _ = stack(binding, tmp_path)
    repo = Repository("sqlite:///" + str(tmp_path / "business.db"))
    repo.bind_account("owner", "910001", "synthetic-wb")
    service = InquiryService(repo, clock=lambda: NOW)
    calls, wires = [], []

    class Transport:
        def send(self, value):
            wires.append(copy.deepcopy(value))
            def native(command):
                calls.append(command["command_id"])
                return {"status": "sent", "evidence": "native:" + command["command_id"], "native_id": "native-" + command["command_id"]}
            return ledger.execute(value, native)

        def reconcile(self, value):
            return ledger.reconcile(value, lambda _: pytest.fail("No command in this case is uncertain"))

    dispatcher = Dispatcher(service, Transport(), clock=lambda: NOW)
    authorize(service)
    dispatcher.run_once()
    assert len(calls) == 1
    event = native_event(direction="out", role="human")
    bridge.publish_native(event)
    control = ledger.event_control(event["event_id"])
    inbox = Inbox(service, TransportRouter({("owner", "910001", "oss"): subscriber}))
    assert inbox.poll_once() == 1
    conversation = service.conversations("owner")[0]
    assert conversation["mode"] == "HUMAN"
    dispatcher.run_once()
    assert len(calls) == 1
    service.resume("owner", conversation["conversation_id"])
    received = service.ingest("owner", {"event_id": "quote-after-resume", "account_uid": "910001", "peer_id": "synthetic-peer",
        "source_seq": 2, "kind": "message", "text": "000100.IB 我卖2.20%，1000万，实价", "occurred_at": NOW + 1, "direction": "in"})
    agent = SignedAgent()
    runner = ChatRunner(service, agent, BondTools(), clock=lambda: NOW)
    agent.runner = runner
    runner.process(received["message_id"])
    queued = [row for row in service.commands("owner") if row["status"] == "queued"]
    assert len(queued) == 1, "Explicitly resumed, valid agent proposals must still be executable"
    dispatcher.run_once()
    assert len(calls) == 2
    service.manual_send("owner", "synthetic-peer", "人工明确询问，请稍后回复", "new-explicit-manual")
    dispatcher.run_once()
    dispatcher.run_once()
    assert len(calls) == len(set(calls)) == 3
    for wire in wires[1:]:
        assert wire["receiver_fence_id"] == control["manual_fence_id"]
        assert wire["receiver_fence_version"] == control["manual_fence_version"]


def test_historical_first_contact_with_fence_still_allows_first_explicit_batch_authorization(binding, tmp_path):
    bridge, ledger, subscriber, _, _ = stack(binding, tmp_path)
    repo = Repository("sqlite:///" + str(tmp_path / "first-contact.db"))
    repo.bind_account("owner", "910001", "synthetic-wb")
    service = InquiryService(repo, clock=lambda: NOW)
    event = native_event(direction="out", role="human")
    bridge.publish_native(event)
    bridge.replay_events(1, 1)
    inbox = Inbox(service, TransportRouter({("owner", "910001", "oss"): subscriber}))
    assert inbox.poll_once() == 1
    result = authorize(service)
    assert len(result["commands"]) == 1
    control = ledger.event_control(event["event_id"])
    command_value = service.commands("owner")[0]
    assert command_value["receiver_fence_id"] == control["manual_fence_id"]
    assert command_value["receiver_fence_version"] == control["manual_fence_version"]
    calls = []

    class Transport:
        def send(self, value):
            return ledger.execute(value, lambda current: calls.append(current["command_id"]) or {"status": "sent", "evidence": "first-authorized-native"})

        def reconcile(self, value):
            return None

    dispatcher = Dispatcher(service, Transport(), clock=lambda: NOW)
    dispatcher.run_once()
    dispatcher.run_once()
    assert calls == [command_value["command_id"]]
    assert service.commands("owner")[0]["status"] == "sent"


def test_historical_new_fence_applies_but_old_replay_cannot_undo_resume_and_same_version_fork_is_rejected(binding, tmp_path):
    bridge, ledger, subscriber, _, _ = stack(binding, tmp_path)
    repo = Repository("sqlite:///" + str(tmp_path / "fence-order.db"))
    repo.bind_account("owner", "910001", "synthetic-wb")
    service = InquiryService(repo, clock=lambda: NOW)
    authorize(service)
    inbox = Inbox(service, TransportRouter({("owner", "910001", "oss"): subscriber}))
    first_event = native_event(direction="out", role="human")
    bridge.publish_native(first_event)
    first_envelope = copy.deepcopy(subscriber.poll()[0]["envelope"])
    inbox.poll_once()
    cid = service.conversations("owner")[0]["conversation_id"]
    service.resume("owner", cid)
    second_event = native_event(event_id="new-manual-fence", ext_msg_id="new-human-native", direction="out", role="human")
    bridge.publish_native(second_event)
    second = subscriber.poll()[0]["envelope"]
    historical = {**second, "transport_replay": True}
    inbox.apply(binding, bridge.codec.unpack(bridge.codec.pack(historical)))
    current = service.conversations("owner")[0]
    assert current["mode"] == "HUMAN"
    assert current["receiver_fence_version"] == 2
    assert current["receiver_fence_id"] == ledger.event_control(second_event["event_id"])["manual_fence_id"]
    fork = {**second, "manual_fence_id": "different-valid-signed-fence"}
    with pytest.raises(ValueError, match="同版"):
        inbox.apply(binding, bridge.codec.unpack(bridge.codec.pack(fork)))
    assert service.conversations("owner")[0] == current
    service.resume("owner", cid)
    resumed = service.conversations("owner")[0]
    inbox.apply(binding, first_envelope)
    assert service.conversations("owner")[0] == resumed
    assert resumed["mode"] == "AUTO" and resumed["receiver_fence_version"] == 2


def test_manual_event_inside_native_execution_blocks_even_an_unseen_higher_control_version(binding, tmp_path):
    bridge, ledger, _, _, _ = stack(binding, tmp_path)
    nested = []

    def in_flight_native(_):
        bridge.publish_native(native_event(direction="out", role="human"))
        value = command(binding, command_id="in-flight-unseen-high-version", control_version=9999)
        outcome = ledger.execute(value, lambda _: nested.append("must-not-execute") or {"status": "sent", "evidence": "wrong"})
        assert outcome["status"] == "cancelled"
        return {"status": "sent", "evidence": "original-physical-send"}

    assert ledger.execute(command(binding), in_flight_native)["status"] == "sent"
    assert nested == []
