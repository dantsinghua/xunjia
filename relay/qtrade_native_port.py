"""Fail-closed QTrade Bus boundary for a future v2 CollectorBridge host integration.

This adapter is NOT a background poller and NEVER starts a QTrade account.
Valid authorized commands use the bound native account. The legacy
XunjiaInbox must remain disabled so only one receiver executes each command.

Contract:
  - invoke native_send/native_reconcile only from the synchronous Bridge worker,
    not from the QTrade asyncio event-loop thread;
  - the ReceiverLedger is the first authority for v2 envelope, stop, fence,
    duplicate and cancellation checks; this adapter adds account and native
    evidence checks before crossing into the pre-existing QTrade Bus;
  - native confirmation requires a precise confirmed ext_msg_id returned by
    QTrade's BUS/DB merge; transport submission alone is not a sent receipt;
  - reconciliation is read-only against QTrade's idempotency store, and must
    never invoke bus.submit again.
"""
from __future__ import annotations

import asyncio
import copy
from concurrent.futures import TimeoutError as FutureTimeout
import hashlib
import json
import math
import threading
from typing import Any, Callable

from relay.protocol import Binding, ProtocolError, validate_command

_PRE_EXECUTION = frozenset({
    "TARGET_NOT_FOUND", "LOGIN_REQUIRED", "INVALID_ARGS", "UNSUPPORTED", "GATE_BLOCKED",
})


def _qtrade_command(account_id: str, command: dict[str, Any]):
    """Resolve the QTrade-owned model only in the collector host environment."""
    from qtrade_agent.models import Command, CommandOrigin
    return Command(
        account_id, "send_text",
        {"session": command["peer_id"], "text": command["text"]},
        idempotency_key=command["command_id"],
        origin=CommandOrigin(transport="http", actor="xunjia-v2-collector"),
    )


class QTradeNativePort:
    # Host replacement in the same native Bus must still see older timed-out
    # calls. A different Bus/account has an independent execution boundary.
    _future_lock = threading.Lock()
    _account_tasks = {}

    def __init__(
        self,
        *,
        binding: Binding,
        store: Any,
        bus: Any,
        loop: asyncio.AbstractEventLoop,
        legacy_inbox_disabled: Callable[[], bool],
        timeout_seconds: float = 45.0,
        command_factory: Callable[[str, dict[str, Any]], Any] = _qtrade_command,
        execution_permission: Callable[[dict[str, Any]], bool] | None = None,
    ):
        if not isinstance(binding, Binding):
            raise ValueError("v2 collector binding is required")
        if not callable(legacy_inbox_disabled) or not callable(command_factory):
            raise ValueError("collector isolation and command factory are required")
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 120:
            raise ValueError("QTrade native result timeout is invalid")
        if not legacy_inbox_disabled():
            raise RuntimeError("Refuse native activation while legacy XunjiaInbox is enabled")
        self.binding, self.store, self.bus, self.loop = binding, store, bus, loop
        self.legacy_inbox_disabled = legacy_inbox_disabled
        self.timeout_seconds = float(timeout_seconds)
        self.command_factory = command_factory
        self.execution_permission = execution_permission
        self._task_scope = (id(bus), binding.owner_user_id, binding.workbench_id, binding.account_uid)

    def quiescent(self):
        """Queued and timed-out submissions remain active until their coroutine exits."""
        with self._future_lock:
            active = self._account_tasks.get(self._task_scope)
            return active is None or not active[1]

    def _native_finished(self, task):
        with self._future_lock:
            active = self._account_tasks.get(self._task_scope)
            if active is not None:
                active[1].discard(task)
                if not active[1]:
                    del self._account_tasks[self._task_scope]

    def _account(self):
        matches = [
            row for row in self.store.list_accounts(channel="qidian")
            if isinstance(row, dict) and str(row.get("self_uid") or "") == self.binding.account_uid
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def ready(self) -> dict[str, bool]:
        account = self._account()
        return {
            "legacy_inbox_disabled": bool(self.legacy_inbox_disabled()),
            "native_send_enabled": bool(self.legacy_inbox_disabled() and callable(self.execution_permission)),
            "single_bound_account": account is not None,
            "account_online": bool(account and account.get("state") in ("running", "degraded")),
            "qtrade_loop_running": self.loop.is_running(),
        }

    def _preflight(self, command: dict[str, Any]):
        validate_command(command, self.binding)
        if not self.legacy_inbox_disabled():
            return None, {"status": "failed", "evidence": "qtrade-v2-native-disabled"}
        account = self._account()
        if not account or account.get("state") not in ("running", "degraded"):
            return None, {"status": "failed", "evidence": "qtrade-v2-account-offline-or-ambiguous"}
        if not self.loop.is_running():
            return None, {"status": "unknown", "evidence": "qtrade-v2-loop-not-ready"}
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return None, {"status": "unknown", "evidence": "qtrade-v2-wrong-execution-thread"}
        return account, None

    @staticmethod
    def _result(value: Any):
        code = getattr(value, "code", None)
        ok = getattr(value, "ok", None) is True
        data = getattr(value, "data", None)
        data = data if isinstance(data, dict) else {}
        native_id = data.get("ext_msg_id")
        confirmation = data.get("confirmed_by")
        if not ok and code == 'GATE_BLOCKED' and getattr(getattr(value,'error',None),'reason',None)=='xunjia_native_permission_changed':
            return {'status':'cancelled','evidence':'qtrade-native-permission-changed-before-send'}
        if (ok and code in ("DELIVERED", "IDEMPOTENT_REPLAY")
                and isinstance(native_id, str) and native_id
                and isinstance(confirmation, str) and confirmation):
            return {"status": "sent", "evidence": "qtrade-confirmed-native:" + native_id, "native_id": native_id}
        if (not ok and code == "SEND_FAILED" and data.get("native_action_attempted") is False
                and isinstance(data.get("reason"), str) and data["reason"]):
            return {"status": "failed", "evidence": "qtrade-rejected-before-native:" + data["reason"][:160]}
        if not ok and code in _PRE_EXECUTION:
            return {"status": "failed", "evidence": "qtrade-rejected-before-native:" + code}
        return {"status": "unknown", "evidence": "qtrade-result-needs-native-reconciliation"}

    def native_send(self, command: dict[str, Any]):
        command = copy.deepcopy(command)
        account, reason = self._preflight(command)
        if reason is not None:
            return reason
        qcommand = self.command_factory(str(account["id"]), command)
        if command.get('business_version') == '2.2' or 'account_epoch' in command or callable(self.execution_permission):
            if not callable(self.execution_permission) or getattr(self.bus,'supports_xunjia_v22_guard',False) is not True:
                return {'status':'failed','evidence':'qtrade-v22-native-guard-not-installed'}
            fixed = copy.deepcopy(command)
            expected_args = {'session':fixed['peer_id'],'text':fixed['text']}
            def permitted():
                current = self._account()
                return bool(self.legacy_inbox_disabled()
                    and current and current['id']==account['id'] and current.get('state') in ('running','degraded')
                    and getattr(qcommand,'account_id',None)==str(account['id'])
                    and getattr(qcommand,'op',None)=='send_text' and getattr(qcommand,'args',None)==expected_args
                    and getattr(qcommand,'idempotency_key',None)==fixed['command_id']
                    and self.execution_permission(copy.deepcopy(fixed)) is True)
            qcommand._xunjia_native_guard = permitted
        # This call must not share the event loop thread. No resend on timeout:
        # the QTrade Bus may still finish the already claimed native action.
        task = object()
        with self._future_lock:
            # Hold the actual Bus while any call runs, so its id cannot be
            # recycled. Release the registry entry after the last coroutine.
            self._account_tasks.setdefault(self._task_scope, (self.bus, set()))[1].add(task)
        async def tracked_submit():
            try:
                return await self.bus.submit(qcommand)
            finally:
                self._native_finished(task)
        submitted = tracked_submit()
        try:
            try:
                future = asyncio.run_coroutine_threadsafe(submitted, self.loop)
            except Exception:
                submitted.close()
                self._native_finished(task)
                raise
            result = future.result(timeout=self.timeout_seconds)
        except FutureTimeout:
            return {"status": "unknown", "evidence": "qtrade-native-result-timeout"}
        except Exception:
            return {"status": "unknown", "evidence": "qtrade-native-result-exception"}
        return self._result(result)

    @staticmethod
    def _args_hash(command: dict[str, Any]) -> str:
        # Match QTrade Bus.canonical_args_hash exactly. Reconciliation must
        # not accept a historical idempotency key bound to different text.
        args = {"session": command["peer_id"], "text": command["text"]}
        return hashlib.sha256(json.dumps(
            args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    def native_reconcile(self, command: dict[str, Any]):
        validate_command(command, self.binding)
        account = self._account()
        if not account:
            return {"status": "unknown", "evidence": "qtrade-account-identity-not-resolved"}
        # Read-only: never use bus.submit (even with the same idempotency key).
        row = self.store.idem_get(str(account["id"]), command["command_id"])
        if not isinstance(row, dict):
            return {"status": "unknown", "evidence": "qtrade-no-native-idempotency-record"}
        if row.get("op") != "send_text" or row.get("args_hash") != self._args_hash(command):
            return {"status": "unknown", "evidence": "qtrade-idempotency-identity-mismatch"}
        if row.get("status") != "DONE":
            return {"status": "unknown", "evidence": "qtrade-native-command-still-unresolved"}
        result = self.store.get_command_result(row["trace_id"])
        if not isinstance(result, dict):
            return {"status": "unknown", "evidence": "qtrade-native-result-unavailable"}

        class Confirmed:
            pass

        value = Confirmed()
        value.ok = result.get("ok") in (True, 1)
        value.code = result.get("code")
        value.data = result.get("data")
        outcome = self._result(value)
        if outcome["status"] == "unknown":
            readback = getattr(self.store, "confirmed_outbound_for_trace", None)
            message = readback(row["trace_id"]) if callable(readback) else None
            if (isinstance(message, dict) and message.get("trace_id") == row["trace_id"]
                    and message.get("account_id") == account["id"]
                    and message.get("session_id") == str(account["id"]) + ":" + command["peer_id"]
                    and message.get("dir") == "out" and message.get("type") == "text"
                    and message.get("text") == command["text"] and message.get("state") == "DELIVERED"
                    and isinstance(message.get("ext_msg_id"), str) and message["ext_msg_id"]
                    and isinstance(message.get("confirmed_by"), str) and message["confirmed_by"]):
                outcome = {"status": "sent", "native_id": message["ext_msg_id"],
                    "evidence": "qtrade-native-readback:" + row["trace_id"]}
                stamp = message.get("ts_ms")
                if type(stamp) in (int, float) and math.isfinite(stamp) and stamp > 0:
                    outcome["executed_at"] = stamp / 1000
        return outcome

    def normalize_native_message(self, row: dict[str, Any], *, live: bool = False):
        """Convert one verified QTrade journal row for Bridge.publish_native.

        Historical backfill is the default and must never trigger a new turn.
        """
        if not isinstance(row, dict) or type(live) is not bool:
            raise ProtocolError("QTrade native record is invalid")
        account = self._account()
        if not account or row.get("account_id") != account["id"]:
            raise ProtocolError("QTrade record belongs to another account")
        session = row.get("session_id")
        prefix = str(account["id"]) + ":"
        if not isinstance(session, str) or not session.startswith(prefix):
            raise ProtocolError("QTrade record lacks a bound peer session")
        peer = session[len(prefix):]
        native = row.get("ext_msg_id")
        direction = row.get("dir")
        text = row.get("text")
        stamp = row.get("ts_ms")
        if (not peer or not isinstance(native, str) or not native
                or direction not in ("in", "out") or not isinstance(text, str)
                or isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or stamp <= 0):
            raise ProtocolError("QTrade record is missing verified native facts")
        event_id = "qtrade-" + hashlib.sha256(
            (self.binding.account_uid + "|" + peer + "|" + direction + "|" + native).encode("utf-8")
        ).hexdigest()
        return {
            "event_id": event_id,
            "peer_id": peer,
            "direction": direction,
            "text": text,
            "kind": "message",
            "ext_msg_id": native,
            "occurred_at": stamp / 1000,
            "historical": not live,
        }
