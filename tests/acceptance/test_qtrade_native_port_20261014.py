"""Controlled v2 CollectorBridge-to-QTrade Bus adapter verification.

Tests use only an in-memory fake Bus/Store and ephemeral receiver journal.
No running QTrade Agent, real IM account or live OSS transport is accessed.
"""
import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from relay.protocol import Binding, ProtocolError
from relay.qtrade_native_port import QTradeNativePort
from server.receiver_ledger import ReceiverLedger

NOW = 1_800_000_000


@pytest.fixture
def binding():
    return Binding("controlled-owner", "990001", "controlled-wb",
                   b"0123456789abcdef", b"s" * 32)


@pytest.fixture
def loop():
    current = asyncio.new_event_loop()
    ready = threading.Event()
    thread = threading.Thread(
        target=lambda: (asyncio.set_event_loop(current), ready.set(), current.run_forever()),
        daemon=True,
    )
    thread.start()
    assert ready.wait(timeout=5)
    yield current
    current.call_soon_threadsafe(current.stop)
    thread.join(timeout=5)
    assert not thread.is_alive()
    current.close()


class Store:
    def __init__(self):
        self.accounts = [{"id": "local-acct-01", "self_uid": "990001",
                          "channel": "qidian", "state": "running"}]
        self.idem = {}
        self.results = {}

    def list_accounts(self, channel=None):
        return [v for v in self.accounts if v["channel"] == channel]

    def idem_get(self, account_id, key):
        return self.idem.get((account_id, key))

    def get_command_result(self, trace_id):
        return self.results.get(trace_id)


class Bus:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or SimpleNamespace(
            ok=True, code="DELIVERED",
            data={"ext_msg_id": "qd:verified-001", "confirmed_by": "ingest_merge"})

    async def submit(self, command):
        self.calls.append(command)
        return self.response


def command(binding, **updates):
    return {
        "command_id": "cmd-controlled",
        "owner_user_id": binding.owner_user_id,
        "account_uid": binding.account_uid,
        "workbench_id": binding.workbench_id,
        "peer_id": "native-peer-77",
        "kind": "send_text",
        "text": "合成询价消息，不进行任何真实发送",
        "content_hash": "e" * 64,
        "control_version": 1,
        "expires_at": NOW + 120,
        "transport": "oss",
        **updates,
    }


def factory(account_id, body):
    return SimpleNamespace(
        account_id=account_id,
        op="send_text",
        args={"session": body["peer_id"], "text": body["text"]},
        idempotency_key=body["command_id"],
    )


def port(binding, loop, *, store=None, bus=None, legacy_disabled=lambda: True, enabled=False):
    return QTradeNativePort(binding=binding, store=store or Store(), bus=bus or Bus(),
                            loop=loop, legacy_inbox_disabled=legacy_disabled,
                            allow_native_send=enabled, command_factory=factory)


def test_disabled_by_default_even_when_qtrade_account_is_running(binding, loop):
    bus = Bus()
    item = port(binding, loop, bus=bus)
    assert item.ready()["native_send_enabled"] is False
    outcome = item.native_send(command(binding))
    assert outcome["status"] == "failed"
    assert bus.calls == []


def test_enabled_port_refuses_live_legacy_inbox(binding, loop):
    with pytest.raises(RuntimeError, match="legacy"):
        port(binding, loop, legacy_disabled=lambda: False, enabled=True)
    is_disabled = [True]
    bus = Bus()
    item = port(binding, loop, bus=bus, legacy_disabled=lambda: is_disabled[0], enabled=True)
    is_disabled[0] = False
    assert item.native_send(command(binding))["status"] == "failed"
    assert bus.calls == []


def test_authenticated_v2_receiver_can_send_once_through_bound_bus(binding, loop, tmp_path):
    bus = Bus()
    item = port(binding, loop, bus=bus, enabled=True)
    ledger = ReceiverLedger(tmp_path / "rx.db", binding, clock=lambda: NOW)
    original = command(binding)
    first = ledger.execute(original, item.native_send)
    assert first["status"] == "sent"
    assert len(bus.calls) == 1
    assert bus.calls[0].idempotency_key == original["command_id"]
    assert bus.calls[0].account_id == "local-acct-01"
    assert bus.calls[0].args["session"] == original["peer_id"]
    assert first["native_id"] == "qd:verified-001"
    assert ledger.execute(original, item.native_send)["status"] == "sent"
    assert len(bus.calls) == 1
    assert ReceiverLedger(tmp_path / "rx.db", binding, clock=lambda: NOW).execute(
        original, item.native_send)["status"] == "sent"
    assert len(bus.calls) == 1


def test_invalid_identity_and_wrong_account_cannot_cross_bus(binding, loop):
    bus, store = Bus(), Store()
    item = port(binding, loop, bus=bus, store=store, enabled=True)
    with pytest.raises(ProtocolError):
        item.native_send(command(binding, account_uid="foreign-acct"))
    store.accounts = [{"id": "foreign-local", "self_uid": "other-uid",
                       "channel": "qidian", "state": "running"}]
    assert item.native_send(command(binding))["status"] == "failed"
    assert bus.calls == []


def test_no_native_identity_is_not_a_success_or_automatic_retry(binding, loop, tmp_path):
    bus = Bus(SimpleNamespace(ok=True, code="DELIVERED", data={"message_id": "no-native-id"}))
    item = port(binding, loop, bus=bus, enabled=True)
    result = ReceiverLedger(tmp_path / "rx.db", binding, clock=lambda: NOW).execute(
        command(binding), item.native_send)
    assert result["status"] == "unknown"
    result = ReceiverLedger(tmp_path / "rx.db", binding, clock=lambda: NOW).execute(
        command(binding), item.native_send)
    assert result["status"] == "unknown"
    assert len(bus.calls) == 1


def test_native_reconcile_uses_only_qtrade_idempotency_store(binding, loop):
    store, bus = Store(), Bus()
    port_obj = port(binding, loop, store=store, bus=bus, enabled=True)
    record = command(binding)
    assert port_obj.native_reconcile(record)["status"] == "unknown"
    store.idem[("local-acct-01", record["command_id"])] = {
        "status": "DONE", "trace_id": "synthetic-trace-01",
        "op": "send_text", "args_hash": port_obj._args_hash(record)}
    store.results["synthetic-trace-01"] = {
        "ok": 1, "code": "DELIVERED",
        "data": {"ext_msg_id": "qd:readback-123", "confirmed_by": "ingest_merge"}}
    result = port_obj.native_reconcile(record)
    assert result["status"] == "sent"
    assert result["native_id"] == "qd:readback-123"
    assert bus.calls == []


def test_pre_execution_denial_maps_to_definite_failure(binding, loop):
    bus = Bus(SimpleNamespace(ok=False, code="LOGIN_REQUIRED", data={}))
    item = port(binding, loop, bus=bus, enabled=True)
    assert item.native_send(command(binding))["status"] == "failed"
    assert len(bus.calls) == 1


def test_native_message_mapping_preserves_origin_and_historical_default(binding, loop):
    item = port(binding, loop)
    sample = {
        "account_id": "local-acct-01",
        "session_id": "local-acct-01:native-peer-77",
        "ext_msg_id": "qd:real-stable-id",
        "dir": "in", "text": "仅测试的对手文本", "ts_ms": 1_800_000_000_000,
    }
    initial = item.normalize_native_message(sample)
    assert initial["historical"] is True
    assert initial["occurred_at"] == NOW
    assert initial["peer_id"] == "native-peer-77"
    current = item.normalize_native_message(sample, live=True)
    assert current["historical"] is False
    assert current["event_id"] == initial["event_id"]
    with pytest.raises(ProtocolError):
        item.normalize_native_message({**sample, "account_id": "someone-else"}, live=True)


def test_qtrade_bus_adapter_requires_worker_not_event_loop_thread(binding, loop):
    bus = Bus()
    item = port(binding, loop, bus=bus, enabled=True)
    async def call_same_loop():
        return item.native_send(command(binding))
    result = asyncio.run_coroutine_threadsafe(call_same_loop(), loop).result(timeout=5)
    assert result["status"] == "unknown"
    assert bus.calls == []

def test_reconcile_rejects_idempotency_key_bound_to_other_text(binding, loop):
    store, bus = Store(), Bus()
    adapter = port(binding, loop, store=store, bus=bus, enabled=True)
    original = command(binding)
    store.idem[("local-acct-01", original["command_id"])] = {
        "status": "DONE", "trace_id": "wrong-trace",
        "op": "send_text", "args_hash": "f" * 64}
    store.results["wrong-trace"] = {
        "ok": 1, "code": "DELIVERED",
        "data": {"ext_msg_id": "qd:other-message", "confirmed_by": "ingest_merge"}}
    result = adapter.native_reconcile(original)
    assert result["status"] == "unknown"
    assert result["evidence"] == "qtrade-idempotency-identity-mismatch"
    assert bus.calls == []
