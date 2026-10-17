"""询价规则。底价、会话接管、消息状态、无回执的询价中降级。不调用 Dify，不发消息。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

RECEIPT_WAIT_S = 60


def message_delivery(ext_msg_id: str, ts: Any, now: float) -> str:
    """有读库消息号才是已发出。没有回执时，1 分钟内待回执，超过为发送失败。"""
    if str(ext_msg_id or "").strip():
        return "已发出"
    age = None
    if isinstance(ts, (int, float)) and ts > 0:
        seconds = float(ts) / 1000.0 if ts > 10_000_000_000 else float(ts)
        age = now - seconds
    if age is not None and age < RECEIPT_WAIT_S:
        return "待回执"
    return "发送失败"


def bonds_from_dify_outputs(outputs: dict | None) -> list[dict[str, str]]:
    """Dify 出参。多只债各带简称，前端只显示简称。"""
    if not isinstance(outputs, dict):
        return []
    rows = outputs.get("bonds")
    if not isinstance(rows, list):
        if outputs.get("bond_code") or outputs.get("bond_name"):
            rows = [outputs]
        else:
            rows = []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("bond_code") or row.get("code") or "").strip()
        name = str(row.get("bond_name") or row.get("name") or "").strip()
        if code.upper().endswith((".IB", ".SH", ".SZ", ".BJ")):
            code = code[:-3] + code[-3:].upper()
        if not name and not code:
            continue
        key = (code or name).upper()
        if key in seen:
            continue
        seen.add(key)
        found.append({"code": code, "name": name or code})
    anchor = outputs.get("券锚线索")
    if isinstance(anchor, dict):
        code = str(anchor.get("命中代码") or anchor.get("bond_code") or "").strip()
        name = str(anchor.get("命中简称片段") or anchor.get("bond_name") or "").strip()
        if code.upper().endswith((".IB", ".SH", ".SZ", ".BJ")):
            code = code[:-3] + code[-3:].upper()
        if name or code:
            key = (code or name).upper()
            if key not in seen:
                found.append({"code": code, "name": name or code})
    slots = outputs.get("slots")
    if isinstance(slots, dict):
        code = str(slots.get("bond_code") or "").strip()
        name = str(slots.get("bond_name") or slots.get("short_name") or "").strip()
        if code.lower() == "null":
            code = ""
        if name.lower() == "null":
            name = ""
        if code.upper().endswith((".IB", ".SH", ".SZ", ".BJ")):
            code = code[:-3] + code[-3:].upper()
        if name or code:
            key = (code or name).upper()
            if key not in seen:
                found.append({"code": code, "name": name or code})
    return found


def dify_inquiry_inputs(*, reply: str, prior: str, bond_codes: list[str], floor_bp: float | None) -> dict:
    """bondtrade-negotiator 入参。带当前回复、上一条发出、债券代码和底价。"""
    return {
        "workflow": "bondtrade-negotiator",
        "raw_text": reply or "",
        "prior_outbound_text": prior or "",
        "bond_code": bond_codes[0] if len(bond_codes) == 1 else "",
        "bond_codes": ",".join(bond_codes),
        "floor_bp": "" if floor_bp is None else floor_bp,
    }


def widen_floor_bp(floor_bp: float, step: float = 2.0) -> float:
    """底价放宽 2bp：新底价 = 原底价 - 2bp。"""
    return float(floor_bp) - float(step)


def downgrade_unreceipted_inquiring(mapping: dict[str, str]) -> dict[str, str]:
    """没有读库回执却标成询价中的，改为发送失败。不补发。"""
    out: dict[str, str] = {}
    for code, status in mapping.items():
        out[code] = "发送失败" if status == "询价中" else status
    return out


class FloorBook:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _nums(self, raw: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        if not isinstance(raw, dict):
            return out
        for key, value in raw.items():
            code = str(key or "").strip()
            if not code:
                continue
            try:
                out[code] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _load(self) -> tuple[dict[str, float], dict[str, float]]:
        if not self.path.exists():
            return {}, {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}, {}
        if not isinstance(data, dict):
            return {}, {}
        return self._nums(data.get("floors")), self._nums(data.get("yields"))

    def _read(self) -> dict[str, float]:
        return self._load()[0]

    def _save(self, floors: dict[str, float], yields: dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"floors": floors, "yields": yields}, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def _write(self, floors: dict[str, float]) -> None:
        _floors, yields = self._load()
        self._save(floors, yields)

    def get(self, code: str) -> float | None:
        with self._lock:
            return self._read().get(str(code or "").strip())

    def get_yield(self, code: str) -> float | None:
        with self._lock:
            return self._load()[1].get(str(code or "").strip())

    def set_yield(self, codes: list[str], value: float) -> dict[str, float]:
        with self._lock:
            floors, yields = self._load()
            for code in codes:
                key = str(code or "").strip()
                if key:
                    yields[key] = round(float(value), 2)
            self._save(floors, yields)
            return dict(yields)

    def set_many(self, codes: list[str], bp: float) -> dict[str, float]:
        with self._lock:
            floors = self._read()
            for code in codes:
                key = str(code or "").strip()
                if key:
                    floors[key] = float(bp)
            self._write(floors)
            return dict(floors)

    def widen(self, code: str, step: float = 2.0) -> tuple[float, float]:
        key = str(code or "").strip()
        with self._lock:
            floors = self._read()
            if key not in floors:
                raise KeyError("还没有底价")
            before = floors[key]
            after = widen_floor_bp(before, step)
            floors[key] = after
            self._write(floors)
            return before, after


class SessionGate:
    """整段会话暂停。交回机器后只等对方下一条再允许回复。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, bool]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        raw = data.get("sessions") if isinstance(data, dict) else None
        out: dict[str, dict[str, bool]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                peer = str(key or "").strip()
                if peer and isinstance(value, dict):
                    out[peer] = {
                        "paused": bool(value.get("paused")),
                        "await_inbound": bool(value.get("await_inbound")),
                        "resumed_at": float(value.get("resumed_at") or 0),
                    }
        return out

    def _write(self, sessions: dict[str, dict[str, bool]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"sessions": sessions}, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def pause(self, peer: str) -> None:
        key = str(peer or "").strip()
        if not key:
            raise ValueError("没有会话对象")
        with self._lock:
            sessions = self._read()
            sessions[key] = {"paused": True, "await_inbound": False, "resumed_at": 0.0}
            self._write(sessions)

    def resume(self, peer: str) -> None:
        key = str(peer or "").strip()
        if not key:
            raise ValueError("没有会话对象")
        with self._lock:
            sessions = self._read()
            sessions[key] = {"paused": False, "await_inbound": True, "resumed_at": time.time()}
            self._write(sessions)

    def allow_reply(self, peer: str) -> bool:
        """暂停中不回复。交回后的第一条对方消息才允许回复，并就此结束等待。"""
        key = str(peer or "").strip()
        with self._lock:
            sessions = self._read()
            state = sessions.get(key) or {"paused": False, "await_inbound": False, "resumed_at": 0.0}
            if state["paused"]:
                return False
            if state["await_inbound"]:
                sessions[key] = {
                    "paused": False,
                    "await_inbound": False,
                    "resumed_at": float(state.get("resumed_at") or 0),
                }
                self._write(sessions)
            return True

    def accept_inbound(self, peer: str, ts, consume: bool = True) -> bool:
        """暂停不调用。交回后，只有比交回时间更晚的入站才算下一条。"""
        key = str(peer or "").strip()
        with self._lock:
            sessions = self._read()
            state = sessions.get(key) or {"paused": False, "await_inbound": False, "resumed_at": 0.0}
            if state["paused"]:
                return False
            if state["await_inbound"]:
                moment = ts if isinstance(ts, (int, float)) and ts > 0 else None
                if isinstance(moment, (int, float)) and moment > 10_000_000_000:
                    moment = float(moment) / 1000.0
                elif isinstance(moment, (int, float)):
                    moment = float(moment)
                if moment is None or moment <= float(state.get("resumed_at") or 0):
                    return False
                if consume:
                    sessions[key] = {
                        "paused": False,
                        "await_inbound": False,
                        "resumed_at": float(state.get("resumed_at") or 0),
                    }
                    self._write(sessions)
            return True
