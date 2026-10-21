"""Real authenticated adapters and receiver ledger, with memory-only files MQ/SMTP/IMAP."""
import copy
import re

import pytest

from relay.protocol import Binding, Codec, ProtocolError, envelope
from tests.mq_fixtures import MQWire
from server.business import InquiryService
from server.inbox import Inbox
from server.receiver_ledger import ReceiverLedger
from server.repository import Repository
from server.transports import MailConfig, MailTransport, TransportRouter


NOW = 1_800_000_000


@pytest.fixture
def binding():
    return Binding("owner", "910001", "synthetic-wb", b"0123456789abcdef", b"s" * 32)


def command(binding, **changes):
    return {"command_id": "cmd-synthetic", "owner_user_id": binding.owner_user_id, "account_uid": binding.account_uid,
            "workbench_id": binding.workbench_id, "peer_id": "synthetic-peer", "content_hash": "a" * 64,
            "control_version": 1, "expires_at": NOW + 120, "text": "合成询价正文", "transport": "oss", **changes}


def packet(binding, value, *, kind="command", message_id=None):
    return Codec(binding, clock=lambda: NOW).pack(envelope(binding, kind, value,
        message_id=message_id or kind + "-packet", peer_id=value["peer_id"], command_id=value["command_id"],
        content_hash=value["content_hash"], control_version=value["control_version"], created_at=NOW,
        expires_at=value["expires_at"]))




class Mailbox:
    def __init__(self):
        self.sent, self.messages, self.uidvalidity = [], {}, "1"
        self.lose_response = False

    def login(self, *_):
        return "OK", []

    def send_message(self, message, **_):
        self.sent.append(copy.deepcopy(message))
        if self.lose_response:
            raise TimeoutError("Synthetic SMTP response lost")
        return {}

    def quit(self):
        pass

    def logout(self):
        pass

    def select(self, mailbox, readonly=True):
        assert readonly is True
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return "UIDVALIDITY", [self.uidvalidity.encode()]

    def uid(self, operation, *arguments):
        if operation == "search":
            start = int(re.search(r"UID (\d+):", arguments[-1])[1])
            return "OK", [" ".join(str(uid) for uid in sorted(self.messages) if uid >= start).encode()]
        uid, part = int(arguments[0]), arguments[1]
        body = self.messages[uid]
        if part == "(RFC822.SIZE)":
            return "OK", [f"{uid} (RFC822.SIZE {len(body)})".encode()]
        assert part == "(BODY.PEEK[])"
        return "OK", [(f"{uid} (BODY[] {{{len(body)}}}".encode(), body), b")"]


def mail(binding, path, mailbox):
    config = MailConfig(smtp_host="smtp.invalid", smtp_user="synthetic", smtp_password="synthetic-password",
                        imap_host="imap.invalid", imap_user="synthetic", imap_password="synthetic-password",
                        outbound_from="collector@synthetic.invalid", outbound_to=("internal@synthetic.invalid",),
                        inbound_from=("collector@synthetic.invalid",), inbound_to=("internal@synthetic.invalid",))
    return MailTransport(binding, config, path, smtp_factory=lambda _: mailbox, imap_factory=lambda _: mailbox, clock=lambda: NOW)


def native_event(**changes):
    return {"event_id": "native-event-1", "peer_id": "synthetic-peer", "text": "合成入站原文", "occurred_at": NOW,
            "ext_msg_id": "synthetic-native-message", "direction": "in", **changes}


def test_inbox_failure_is_route_scoped_redacted_and_recovery_keeps_failure_history(binding, tmp_path, caplog):
    from types import SimpleNamespace

    repo = Repository("sqlite:///" + str(tmp_path / "diagnostic.db"))
    repo.bind_account("owner", "910001", "synthetic-wb")
    service = InquiryService(repo, clock=lambda: NOW)
    failing = [True]

    def poll(limit):
        assert limit == 50
        if failing[0]:
            raise RuntimeError("synthetic-SMTP-password-private-body")
        return []

    bad = SimpleNamespace(binding=binding, route="mail", poll=poll)
    good = SimpleNamespace(binding=binding, route="oss", poll=lambda limit: [])
    inbox = Inbox(service, SimpleNamespace(adapters={"bad": bad, "good": good}))
    assert inbox.poll_once() == 0
    entries = {entry["component"]: entry for entry in service.diagnostics.snapshot("owner")}
    assert entries["inbox_mail"]["code"] == "TRANSPORT_POLL_FAILED"
    assert entries["inbox_mail"]["consecutive_failures"] == 1
    assert entries["inbox_oss"]["status"] == "ok"
    assert service.diagnostics.snapshot("another-owner") == []
    assert "synthetic-SMTP-password-private-body" not in str(entries) + caplog.text
    failing[0] = False
    inbox.poll_once()
    entry = next(entry for entry in service.diagnostics.snapshot("owner") if entry["component"] == "inbox_mail")
    assert entry["status"] == "ok" and entry["code"] is None
    assert entry["attempts"] == 2 and entry["failures"] == 1
    assert entry["consecutive_failures"] == 0


def test_one_slow_inbox_route_does_not_prevent_other_route_from_committing_and_acknowledging(binding, tmp_path):
    import threading
    from types import SimpleNamespace

    repo = Repository("sqlite:///" + str(tmp_path / "parallel-inbox.db"))
    repo.bind_account("owner", "910001", "synthetic-wb")
    service = InquiryService(repo, clock=lambda: NOW)
    completed = threading.Event()
    observations = []
    message = {"kind": "catalog", "owner_user_id": "owner", "account_uid": "910001", "workbench_id": "synthetic-wb", "payload": {"account_uid": "910001"}}

    def slow_poll(limit):
        observations.append(completed.wait(timeout=2))
        return []

    slow = SimpleNamespace(binding=binding, route="mail", poll=slow_poll)
    fast = SimpleNamespace(binding=binding, route="oss", poll=lambda limit: [{"source_token": "catalog-token", "envelope": message}], ack=lambda token: completed.set())
    assert Inbox(service, SimpleNamespace(adapters={"slow": slow, "fast": fast})).poll_once() == 1
    assert observations == [True], "Healthy route must finish while the other route is still waiting"


def test_receiver_deduplicates_two_authenticated_route_deliveries_and_restart(binding, tmp_path):
    path = tmp_path / "receiver.db"
    ledger = ReceiverLedger(path, binding, clock=lambda: NOW)
    codec = Codec(binding, clock=lambda: NOW)
    calls = []

    def native(value):
        calls.append(value)
        return {"status": "sent", "evidence": "native-outbound-id"}

    for route in ("oss", "mail"):
        result = ledger.receive(packet(binding, command(binding), message_id=route + "-delivery"), codec, native, lambda _: None)
        assert result["status"] == "sent"
    restarted = ReceiverLedger(path, binding, clock=lambda: NOW)
    assert restarted.receive(packet(binding, command(binding)), codec, native, lambda _: None)["status"] == "sent"
    assert len(calls) == 1


def test_cancel_barrier_precedes_original_packet_and_survives_restart(binding, tmp_path):
    path = tmp_path / "receiver.db"
    codec = Codec(binding, clock=lambda: NOW)
    ledger = ReceiverLedger(path, binding, clock=lambda: NOW)
    calls = []
    assert ledger.receive(packet(binding, command(binding), kind="cancel"), codec, calls.append, lambda _: None)["status"] == "cancelled"
    restarted = ReceiverLedger(path, binding, clock=lambda: NOW)
    for value in (command(binding), command(binding, command_id="different-old-command")):
        assert restarted.receive(packet(binding, value), codec, calls.append, lambda _: None)["status"] == "cancelled"
    assert calls == []


def test_receiver_process_crash_never_blindly_executes_pending_again(binding, tmp_path):
    path = tmp_path / "receiver.db"
    calls = []

    class ProcessCrash(BaseException):
        pass

    def crash(value):
        calls.append(value)
        raise ProcessCrash()

    ledger = ReceiverLedger(path, binding, clock=lambda: NOW)
    with pytest.raises(ProcessCrash):
        ledger.receive(packet(binding, command(binding)), Codec(binding, clock=lambda: NOW), crash, lambda _: None)
    restarted = ReceiverLedger(path, binding, clock=lambda: NOW)
    assert restarted.execute(command(binding), crash)["status"] == "unknown"
    assert len(calls) == 1
    found = restarted.reconcile(command(binding), lambda _: {"status": "sent", "evidence": "native-id-found"})
    assert found["status"] == "sent"
    assert restarted.execute(command(binding), crash)["status"] == "sent"
    assert len(calls) == 1


def test_receiver_rejects_modified_content_and_tampered_ciphertext(binding, tmp_path):
    ledger = ReceiverLedger(tmp_path / "receiver.db", binding, clock=lambda: NOW)
    codec, calls = Codec(binding, clock=lambda: NOW), []
    native = lambda value: calls.append(value) or {"status": "sent", "evidence": "native-id"}
    ledger.receive(packet(binding, command(binding)), codec, native, lambda _: None)
    with pytest.raises(ProtocolError):
        ledger.receive(packet(binding, command(binding, text="改变后的正文")), codec, native, lambda _: None)
    corrupted = bytearray(packet(binding, command(binding)))
    corrupted[-1] ^= 1
    with pytest.raises(ProtocolError):
        ledger.receive(bytes(corrupted), codec, native, lambda _: None)
    assert len(calls) == 1


def test_oss_response_loss_keeps_one_object_identity_and_only_sends_queries_afterward(binding, tmp_path):
    objects = MQWire()
    objects.lose_upload = True
    adapter = objects.transport(binding, tmp_path / "transport.db", role="service", clock=lambda: NOW)
    value = command(binding)
    assert adapter.send(value)["status"] == "unknown"
    objects.lose_upload = False
    assert adapter.send(value)["status"] == "unknown"
    adapter.reconcile(value)
    business_puts = [(name, blob) for _, name, blob in objects.uploaded if name.startswith("xunjia-v22-command-")]
    assert len(business_puts) == 1
    assert Codec(binding, clock=lambda: NOW).unpack(business_puts[0][1])["payload"]["command_id"] == "cmd-synthetic"


def test_smtp_acceptance_does_not_claim_sent_or_expose_plain_business_text(binding, tmp_path):
    mailbox = Mailbox()
    adapter = mail(binding, tmp_path / "mail.db", mailbox)
    result = adapter.send(command(binding, transport="mail"))
    assert result["status"] == "accepted"
    assert len(mailbox.sent) == 1
    message = mailbox.sent[0]
    assert "合成询价正文" not in message.get_body(preferencelist=("plain",)).get_content()
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_filename() == "xunjia-v2.bin"
    assert Codec(binding, clock=lambda: NOW).unpack(attachments[0].get_payload(decode=True))["payload"]["text"] == "合成询价正文"


def test_bad_mail_is_quarantined_and_uidvalidity_change_does_not_reprocess_acked_event(binding, tmp_path):
    mailbox = Mailbox()
    adapter = mail(binding, tmp_path / "mail.db", mailbox)
    adapter.publish_event(native_event())
    wire = mailbox.sent[0].as_bytes()
    mailbox.messages = {1: b"bad unauthenticated email", 2: wire}
    pending = adapter.poll()
    assert len(pending) == 1
    assert pending[0]["envelope"]["message_id"] == "native-event-1"
    assert len(adapter.quarantined()) == 1
    assert len(adapter.poll()) == 1, "Polling alone must not acknowledge the event"
    adapter.ack(pending[0]["source_token"])
    assert adapter.poll() == []
    mailbox.uidvalidity = "2"
    mailbox.messages = {1: wire}
    assert adapter.poll() == [], "UID reset must retain stable authenticated event deduplication"


def test_same_native_event_published_over_both_routes_has_one_stable_source_sequence(binding, tmp_path):
    objects, mailbox = MQWire(), Mailbox()
    shared = tmp_path / "publisher.db"
    oss = objects.transport(binding, shared, role="collector", clock=lambda: NOW)
    smtp = mail(binding, shared, mailbox)
    first = oss.publish_event(native_event(transport="oss", object_key="ignored-route-data"))
    second = smtp.publish_event(native_event(transport="mail", mail_uid=777, uidvalidity=9))
    assert first == second == 1
    raw = list(mailbox.sent[-1].iter_attachments())[0].get_payload(decode=True)
    event = Codec(binding, clock=lambda: NOW).unpack(raw)["payload"]
    assert event["source_seq"] == 1
    assert not {"transport", "object_key", "mail_uid", "uidvalidity"} & event.keys()


def test_inbox_does_not_ack_before_business_commit_and_replays_after_failure(binding, tmp_path, monkeypatch):
    objects = MQWire()
    publisher = objects.transport(binding, tmp_path / "publisher.db", role="collector", clock=lambda: NOW)
    subscriber = objects.transport(binding, tmp_path / "subscriber.db", role="service", clock=lambda: NOW)
    publisher.publish_event(native_event())
    repo = Repository("sqlite:///" + str(tmp_path / "business.db"))
    repo.bind_account(binding.owner_user_id, binding.account_uid, binding.workbench_id)
    service = InquiryService(repo, clock=lambda: NOW)
    inbox = Inbox(service, TransportRouter({("owner", "910001", "oss"): subscriber}))
    original = service.ingest

    def fail(*_args, **_kwargs):
        raise OSError("synthetic database failure before commit")

    monkeypatch.setattr(service, "ingest", fail)
    assert inbox.poll_once() == 0
    assert len(subscriber.poll()) == 1
    monkeypatch.setattr(service, "ingest", original)
    assert inbox.poll_once() == 1
    assert subscriber.poll() == []
    assert inbox.poll_once() == 0


def test_unknown_oss_submission_cannot_be_silently_reissued_via_mail(binding, tmp_path):
    objects, mailbox = MQWire(), Mailbox()
    objects.lose_upload = True
    shared = tmp_path / "transport.db"
    oss = objects.transport(binding, shared, role="service", clock=lambda: NOW)
    smtp = mail(binding, shared, mailbox)
    assert oss.send(command(binding))["status"] == "unknown"
    with pytest.raises(ProtocolError):
        smtp.send(command(binding, transport="mail"))
    assert mailbox.sent == []




@pytest.mark.parametrize("replay", [False, True])
def test_administrator_replay_is_display_only_while_fresh_inbound_remains_runnable(binding, tmp_path, monkeypatch, replay):
    from server.runtime import Dispatcher

    repo = Repository("sqlite:///" + str(tmp_path / "business.db"))
    repo.bind_account("owner", "910001", "synthetic-wb")
    service = InquiryService(repo, clock=lambda: NOW)
    draft = service.preview("owner", "initial", [{"code": "000100.IB", "name": "合成债", "peer_ids": ["synthetic-peer"],
                           "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50", "limit_yield_pct": "2.40", "step_bp": "2"}])
    service.confirm("owner", draft["batch_id"], draft["version"], "confirm-initial")

    class Native:
        def send(self, value):
            return {key: value[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "sent", "evidence": "native-initial"}

        def reconcile(self, _):
            return None

    Dispatcher(service, Native(), clock=lambda: NOW).run_once()
    objects = MQWire()
    publisher = objects.transport(binding, tmp_path / "publisher.db", role="collector", clock=lambda: NOW)
    subscriber = objects.transport(binding, tmp_path / "subscriber.db", role="service", clock=lambda: NOW)
    publisher.publish_event(native_event(text="谢谢", occurred_at=NOW + 1))
    if replay:
        publisher.queue_replay(1, 1)
        publisher.flush_events()
    observed = []
    original = service.ingest

    def capture(owner, event, **kwargs):
        result = original(owner, event, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(service, "ingest", capture)
    inbox = Inbox(service, TransportRouter({("owner", "910001", "oss"): subscriber}))
    assert inbox.poll_once() == 1
    assert len(observed) == 1
    assert observed[0]["processing"] == ("ignored" if replay else "queued")


@pytest.mark.parametrize("exact_native_id", [True, False])
def test_native_out_waits_for_exact_receipt_and_holds_auto_send_until_classified(binding, tmp_path, exact_native_id):
    from relay.collector_bridge import CollectorBridge

    objects = MQWire()
    publisher = objects.transport(binding, tmp_path / "publisher.db", role="collector", clock=lambda: NOW)
    subscriber = objects.transport(binding, tmp_path / "subscriber.db", role="service", clock=lambda: NOW)
    ledger = ReceiverLedger(tmp_path / "receiver.db", binding, clock=lambda: NOW)
    bridge = CollectorBridge(ledger=ledger, adapters={"oss": publisher}, native_send=lambda _: None, native_reconcile=lambda _: None)
    first = command(binding)
    second = command(binding, command_id="next-authorized-command")
    event = native_event(direction="out", ext_msg_id="echo-native", text=first["text"])

    def execute_first(value):
        assert bridge.publish_native(event) is None
        assert subscriber.poll() == []
        held = ledger.execute(second, lambda _: pytest.fail("Unclassified native out must hold later automatic sends"))
        assert held["status"] == "pending"
        assert ledger.reconcile(second, lambda _: pytest.fail("A held command has not executed and cannot be queried natively"))["status"] == "pending"
        return {"status": "sent", "evidence": "native-first", "native_id": "echo-native" if exact_native_id else "different-native"}

    assert ledger.execute(first, execute_first)["status"] == "sent"
    bridge.flush_deferred()
    received = subscriber.poll()
    assert len(received) == 1
    payload = received[0]["envelope"]["payload"]
    assert payload["event_id"] == event["event_id"] and payload["text"] == first["text"]
    assert payload.get("source_command_id") == (first["command_id"] if exact_native_id else None)
    calls = []
    outcome = ledger.execute(second, lambda value: calls.append(value) or {"status": "sent", "evidence": "native-second"})
    assert outcome["status"] == ("sent" if exact_native_id else "cancelled")
    assert len(calls) == (1 if exact_native_id else 0)


@pytest.mark.parametrize("recovery", ["timeout", "restart"])
def test_unresolved_native_echo_fails_closed_and_late_mapping_cannot_reclassify_published_fact(binding, tmp_path, recovery):
    from relay.collector_bridge import CollectorBridge

    clock = [NOW]
    objects = MQWire()
    publisher = objects.transport(binding, tmp_path / "publisher.db", role="collector", clock=lambda: clock[0])
    subscriber = objects.transport(binding, tmp_path / "subscriber.db", role="service", clock=lambda: clock[0])
    path = tmp_path / "receiver.db"
    ledger = ReceiverLedger(path, binding, clock=lambda: clock[0])
    bridge = CollectorBridge(ledger=ledger, adapters={"oss": publisher}, native_send=lambda _: None, native_reconcile=lambda _: None)
    value = command(binding)
    event = native_event(direction="out", ext_msg_id="unresolved-native", text=value["text"])

    class SimulatedCrash(BaseException):
        pass

    def crash_after_echo(_):
        assert bridge.publish_native(event) is None
        raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        ledger.execute(value, crash_after_echo)
    if recovery == "timeout":
        clock[0] += 5
    else:
        ledger = ReceiverLedger(path, binding, clock=lambda: clock[0])
        bridge = CollectorBridge(ledger=ledger, adapters={"oss": publisher}, native_send=lambda _: None, native_reconcile=lambda _: None)
    bridge.flush_deferred()
    messages = subscriber.poll()
    assert len(messages) == 1
    original = messages[0]["envelope"]["payload"]
    assert original.get("source_command_id") is None
    rejected = ledger.execute(command(binding, command_id="after-unresolved-echo"), lambda _: pytest.fail("Manual classification must bar old control versions"))
    assert rejected["status"] == "cancelled"
    ledger.reconcile(value, lambda _: {"status": "sent", "evidence": "late-native-evidence", "native_id": "unresolved-native"})
    bridge.publish_native(event)
    assert subscriber.poll()[0]["envelope"]["payload"] == original


def test_trusted_human_out_is_published_immediately_even_while_native_send_is_pending(binding, tmp_path):
    from relay.collector_bridge import CollectorBridge

    objects = MQWire()
    publisher = objects.transport(binding, tmp_path / "publisher.db", role="collector", clock=lambda: NOW)
    subscriber = objects.transport(binding, tmp_path / "subscriber.db", role="service", clock=lambda: NOW)
    ledger = ReceiverLedger(tmp_path / "receiver.db", binding, clock=lambda: NOW)
    bridge = CollectorBridge(ledger=ledger, adapters={"oss": publisher}, native_send=lambda _: None, native_reconcile=lambda _: None)

    def native(_):
        assert bridge.publish_native(native_event(direction="out", role="human")) == 1
        observed = subscriber.poll()
        assert len(observed) == 1
        assert observed[0]["envelope"]["payload"].get("source_command_id") is None
        return {"status": "sent", "evidence": "original-native", "native_id": "different-native"}

    assert ledger.execute(command(binding), native)["status"] == "sent"
    result = ledger.execute(command(binding, command_id="must-not-follow-human"), lambda _: pytest.fail("Known human takeover must stop the old authorization"))
    assert result["status"] == "cancelled"


def test_expired_held_command_reaches_failed_without_ever_calling_native(binding, tmp_path):
    from relay.collector_bridge import CollectorBridge

    clock = [NOW]
    publisher = MQWire().transport(binding, tmp_path / "publisher.db", role="collector", clock=lambda: clock[0])
    ledger = ReceiverLedger(tmp_path / "receiver.db", binding, clock=lambda: clock[0])
    bridge = CollectorBridge(ledger=ledger, adapters={"oss": publisher}, native_send=lambda _: None, native_reconcile=lambda _: None)
    second = command(binding, command_id="held-short-authorization", expires_at=NOW + 1)

    def native(_):
        assert bridge.publish_native(native_event(direction="out", ext_msg_id="first-native")) is None
        assert ledger.execute(second, lambda _: pytest.fail("Held command cannot execute"))["status"] == "pending"
        return {"status": "sent", "evidence": "first-native-evidence", "native_id": "first-native"}

    ledger.execute(command(binding), native)
    bridge.flush_deferred()
    clock[0] += 2
    result = ledger.reconcile(second, lambda _: pytest.fail("Expired held command was never submitted; no native reconciliation is valid"))
    assert result["status"] == "failed", "A provably unexecuted expired authorization must not remain pending forever"
