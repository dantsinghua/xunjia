"""把询价入站交给 bondtrade-negotiator。

入参是当前回复、上一条本方发出、债券代码和底价。出参里的简称交给页面。
工作流回复只进监控聊天，不向企点发出。没有地址或密钥时不调用。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .inquiry_rules import (
    FloorBook,
    SessionGate,
    bonds_from_dify_outputs,
    dify_inquiry_inputs,
)
from .monitor_feed import bond_codes_in

log = logging.getLogger("xunjia.dify")
_LOGGED: set[str] = set()
_DIFY_LOCK = threading.Lock()
_DEFAULT_DB = "/home/anlin/work/qtrade-build/real-chain-20261010/agent.db"
_HOLD = {
    "已撤价", "已放弃", "已成交", "待人工确认", "转人工",
    "已报价", "还价中", "已暂停", "未成交",
}
def message_key(row: dict[str, Any]) -> str:
    mid = str(row.get("id") or "").strip()
    if mid:
        return mid
    return "|".join([
        str(row.get("peer_id") or row.get("native_id") or ""),
        str(row.get("time") or ""),
        str(len(str(row.get("text") or ""))),
        str(row.get("dir") or ""),
    ])


def reply_from_outputs(outputs: dict | None) -> str:
    """只取工作流写出的回复。不把原文回声当回复。"""
    if not isinstance(outputs, dict):
        return ""
    for key in ("reply_text", "reply", "answer", "negotiation_reply"):
        value = outputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _role_from_prior(prior: str) -> str:
    text = (prior or "").lower()
    if "ofr" in text:
        return "SELL"
    if "bid" in text:
        return "BUY"
    return ""


def http_inputs(inputs: dict[str, Any]) -> dict[str, str]:
    """这个工作流只要 message 和 context_json。底价放进上下文，放宽后 remaining_bp 变大。"""
    message = str(inputs.get("message") if inputs.get("message") is not None else inputs.get("raw_text") or "")
    ready = inputs.get("context_json")
    if isinstance(ready, str) and ready.strip():
        context = ready
    else:
        if isinstance(ready, dict):
            context_obj = dict(ready)
        else:
            floor = inputs.get("floor_bp")
            remaining = abs(float(floor)) if isinstance(floor, (int, float)) else ""
            context_obj = {
                "role": _role_from_prior(str(inputs.get("prior_outbound_text") or "")),
                "floor_bp": "" if floor is None else floor,
                "remaining_bp": remaining,
                "target_reached": False,
                "bond_code": inputs.get("bond_code") or "",
                "bond_codes": inputs.get("bond_codes") or "",
                "prior_outbound_text": inputs.get("prior_outbound_text") or "",
                "floors_bp": inputs.get("floors_bp") or "",
                "profile": {},
            }
        context = json.dumps(context_obj, ensure_ascii=False)
    return {"message": message, "context_json": context}


class DifyClient:
    """POST /v1/workflows/run。密钥只放请求头，不写日志。"""

    def __init__(self, base_url: str, app_key: str, opener: Callable | None = None, timeout: float = 90):
        self.base_url = str(base_url or "").rstrip("/")
        self.app_key = str(app_key or "")
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def __call__(self, inputs: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({
            "inputs": http_inputs(inputs),
            "response_mode": "blocking",
            "user": "xunjia-inquiry",
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/v1/workflows/run",
            data=body,
            headers={
                "Authorization": "Bearer " + self.app_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError("dify http %s" % exc.code) from None
        return _outputs_or_fail(raw)


def _outputs_or_fail(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("status") not in (None, "succeeded"):
        err = str(data.get("error") or "")
        if "insufficient_quota" in err or "429" in err:
            raise RuntimeError("dify quota")
        raise RuntimeError("dify status failed")
    return outputs_from_body(raw)


def outputs_from_body(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {}
    outputs: Any = {}
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("outputs"), dict):
            outputs = data["outputs"]
        elif isinstance(payload.get("outputs"), dict):
            outputs = payload["outputs"]
    if not isinstance(outputs, dict):
        return {}
    for key in ("result", "json", "text"):
        blob = outputs.get(key)
        if isinstance(blob, str) and blob.strip().startswith("{"):
            try:
                parsed = json.loads(blob)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                return {**outputs, **parsed}
    return outputs


def client_from_env() -> DifyClient | None:
    url = os.environ.get("XUNJIA_DIFY_BASE_URL", "").strip()
    key = os.environ.get("XUNJIA_DIFY_APP_KEY", "").strip()
    if not url or not key:
        return None
    return DifyClient(url, key)


class LabelBook:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        raw = data.get("labels") if isinstance(data, dict) else None
        out: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if str(key) and isinstance(value, dict):
                    out[str(key)] = value
        return out

    def _write(self, labels: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"labels": labels}, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def all(self) -> dict[str, dict[str, Any]]:
        return self._read()

    def mark_dispatch(self, key: str, status: str) -> None:
        labels = self._read()
        row = labels.get(key) if isinstance(labels.get(key), dict) else {}
        row = dict(row or {})
        row["dispatched"] = status
        labels[key] = row
        self._write(labels)

    def put(self, key: str, bonds: list[dict[str, str]], reply: str) -> None:
        labels = self._read()
        prev = labels.get(key) or {}
        labels[key] = {
            "bonds": bonds,
            "reply": reply,
            "attempts": int(prev.get("attempts") or 0) + 1,
        }
        self._write(labels)

    def fail(self, key: str) -> int:
        labels = self._read()
        prev = labels.get(key) or {}
        count = int(prev.get("attempts") or 0) + 1
        labels[key] = {"attempts": count}
        self._write(labels)
        return count


def _floor_for(codes: list[str], floors: FloorBook) -> float | None:
    values = [floors.get(code) for code in codes]
    present = [value for value in values if value is not None]
    if len(codes) == 1:
        return present[0] if present else None
    if present and len(set(present)) == 1 and len(present) == len(codes):
        return present[0]
    return None


def _note_unconfigured(key: str, row: dict[str, Any]) -> None:
    if key in _LOGGED:
        return
    _LOGGED.add(key)
    prior = row.get("prior_outbound_text")
    log.info(
        "询价入站未调用Dify peer=%s text_len=%s prior_len=%s reason=没有工作流地址或密钥",
        row.get("peer_id") or row.get("native_id") or "",
        len(str(row.get("text") or "")),
        len(prior) if isinstance(prior, str) else 0,
    )


def dispatch_dify_reply(peer: str, text: str, msg_id: str) -> str:
    """把 Dify 回复经公网信箱交给采集端，发给这条入站消息的对方。"""
    target = str(peer or "").strip()
    body = str(text or "").strip()
    if not target:
        return "skip-peer"
    if not body:
        return "empty"
    from .routes import _oss_fields, _write_oss, store
    from .uplink import upload_command

    link = store.active()
    if link is None or link.get("mode") != "oss":
        return "no-link"
    fields = _oss_fields(link)
    account = str(fields.get("account_uid") or "")
    if not account or target == account:
        return "wrong-account"
    from .commands import log as command_log
    recent = [
        str(item.get("text") or "")
        for item in command_log.list_all()
        if str((item.get("session") or {}).get("native_id") or "") == target
    ]
    if body in recent[-8:]:
        log.warning("询价回复与刚发出的相同，不再重复 msg=%s text_len=%s", msg_id, len(body))
        return "duplicate"
    result = _write_oss(link, target, body, "dify-" + str(msg_id), "private")
    if result.get("blocked"):
        log.warning("询价回复被红线拦住 msg=%s", msg_id)
        return "blocked"
    if not result.get("ok"):
        log.warning("询价回复没有写入指令 msg=%s", msg_id)
        return "not-written"
    cmd = result.get("command") or {}
    if cmd.get("status") == "delivered":
        return "already"
    ok = upload_command(cmd)
    return "uploaded" if ok else "upload-failed"


def flush_replies(messages: list[dict[str, Any]] | None, command_path: Path) -> None:
    book = LabelBook(Path(command_path).with_name("rfq_dify_labels.json"))
    table = book.all()
    for row in messages or []:
        if not isinstance(row, dict):
            continue
        if row.get("dir") != "in" and row.get("tag") != "收到":
            continue
        mid = message_key(row)
        entry = table.get(mid)
        if not isinstance(entry, dict):
            continue
        reply = str(entry.get("reply") or "").strip()
        if not reply or entry.get("dispatched") in ("uploaded", "already", "blocked"):
            continue
        peer = str(row.get("peer_id") or row.get("native_id") or "")
        status = dispatch_dify_reply(peer, reply, mid)
        book.mark_dispatch(mid, status)
        log.warning("询价回复交公网信箱 peer=%s text_len=%s result=%s", peer, len(reply), status)


def run_pending(
    messages: list[dict[str, Any]] | None,
    *,
    floors: FloorBook,
    gate: SessionGate,
    labels: LabelBook,
    client: Callable[[dict[str, Any]], dict[str, Any]] | None,
    limit: int | None = 2,
    sender: Callable[[str, str, str], str] | None = None,
) -> dict[str, dict[str, Any]]:
    """对还没识别的入站调用工作流。暂停、或交回后还没到的下一条，都不调用。"""
    table = labels.all()
    calls = 0
    if client is None and any((row.get("dir") == "in" or row.get("tag") == "收到") for row in (messages or []) if isinstance(row, dict)):
        if "unconfigured" not in _LOGGED:
            _LOGGED.add("unconfigured")
            log.warning("询价入站未调用Dify reason=没有XUNJIA_DIFY_BASE_URL或XUNJIA_DIFY_APP_KEY")
    pending = [row for row in (messages or []) if isinstance(row, dict)]
    pending.sort(key=lambda row: float(row.get("time") or 0), reverse=True)
    for row in pending:
        incoming = row.get("dir") == "in" or row.get("tag") == "收到"
        if not incoming or not isinstance(row, dict):
            continue
        key = message_key(row)
        if not str(row.get("id") or "").strip():
            row["id"] = key
        current = table.get(key) or {}
        if "bonds" in current:
            continue
        if int(current.get("attempts") or 0) >= 3:
            continue
        if client is None:
            _note_unconfigured(key, row)
            continue
        peer = str(row.get("peer_id") or row.get("native_id") or "")
        if not gate.accept_inbound(peer, row.get("time"), consume=False):
            continue
        if limit is not None and calls >= limit:
            continue
        calls += 1
        codes = bond_codes_in(str(row.get("prior_outbound_text") or ""))
        floor = _floor_for(codes, floors)
        inputs = dify_inquiry_inputs(
            reply=str(row.get("text") or ""),
            prior=str(row.get("prior_outbound_text") or ""),
            bond_codes=codes,
            floor_bp=floor,
        )
        if len(codes) > 1:
            pairs = []
            for code in codes:
                value = floors.get(code)
                if value is not None:
                    pairs.append("%s:%s" % (code, value))
            if pairs:
                inputs["floors_bp"] = ",".join(pairs)
        try:
            outputs = client(inputs)
        except AssertionError:
            raise
        except Exception as exc:
            labels.fail(key)
            table[key] = {"attempts": int(current.get("attempts") or 0) + 1}
            reason = str(exc)
            if not reason.startswith("dify "):
                reason = "dify error"
            log.warning(
                "询价入站调用Dify失败 peer=%s text_len=%s reason=%s",
                peer,
                len(str(row.get("text") or "")),
                reason,
            )
            continue
        if not isinstance(outputs, dict):
            outputs = {}
        bonds = bonds_from_dify_outputs(outputs)
        reply = reply_from_outputs(outputs)
        labels.put(key, bonds, reply)
        if reply and sender is not None and not (labels.all().get(key) or {}).get("dispatched"):
            def _send(peer=peer, reply=reply, key=key, book=labels):
                status = sender(peer, reply, key)
                book.mark_dispatch(key, status)
                log.warning("询价回复交公网信箱 peer=%s text_len=%s result=%s", peer, len(reply), status)
            threading.Thread(target=_send, name="xunjia-send", daemon=True).start()
        gate.accept_inbound(peer, row.get("time"), consume=True)
        table[key] = {"bonds": bonds, "reply": reply}
        log.info(
            "询价入站已调用Dify peer=%s text_len=%s prior_len=%s floor=%s codes=%s bonds=%s",
            peer,
            len(str(row.get("text") or "")),
            len(str(row.get("prior_outbound_text") or "")),
            "yes" if floor is not None else "no",
            len(codes),
            len(bonds),
        )
    return labels.all()


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    if isinstance(value, str) and "T" in value:
        from datetime import datetime
        try:
            return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
        except ValueError:
            return None
    return None


def confirm_bonds(
    mapping: dict[str, str],
    commands: list[Any],
    receipts: list[dict[str, Any]],
    now: float,
) -> dict[str, str]:
    """有读库消息号且正文对上，才升为询价中。一分钟没有回执则为发送失败。"""
    out = dict(mapping)
    landed: dict[str, str] = {}
    for rec in receipts:
        ext = str(rec.get("ext_msg_id") or "").strip()
        if not ext:
            continue
        landed[_norm(rec.get("text"))] = ext
    latest: dict[str, tuple[float, str]] = {}
    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        text = cmd.get("text") if isinstance(cmd.get("text"), str) else ""
        moment = _epoch(cmd.get("created_at") if cmd.get("created_at") is not None else cmd.get("ts"))
        stamp = moment if moment is not None else 0.0
        for code in bond_codes_in(text):
            prev = latest.get(code)
            if prev is None or stamp >= prev[0]:
                latest[code] = (stamp, text)
    for code, (stamp, text) in latest.items():
        if code not in out or out[code] in _HOLD:
            continue
        if _norm(text) in landed:
            out[code] = "询价中"
            continue
        if stamp <= 0:
            continue
        age = now - stamp
        if age > 60 and out[code] in ("待回执", "询价中"):
            out[code] = "发送失败"
        elif age <= 60 and out[code] == "询价中":
            out[code] = "待回执"
    return out


def _receipts_of(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for row in messages:
        if row.get("dir") != "out":
            continue
        ext = str(row.get("id") or "").strip()
        if not ext:
            continue
        if row.get("tag") == "机器回复":
            continue
        if row.get("landed") is not True and row.get("tag") != "发出":
            continue
        found.append({"text": row.get("text") or "", "ext_msg_id": ext, "ts": row.get("time")})
    return found


def sync_receipts(book: Any, commands: list[Any], messages: list[dict[str, Any]], now: float) -> dict[str, str]:
    current = book.mapping()
    updated = confirm_bonds(current, commands, _receipts_of(messages), now)
    for code, status in updated.items():
        if current.get(code) != status:
            book.mark([code], status)
    return book.mapping()


def prepare_live(
    messages: list[dict[str, Any]] | None,
    command_path: Path,
    client: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    limit: int | None = 2,
    sender: Callable[[str, str, str], str] | None = None,
) -> dict[str, dict[str, Any]]:
    path = Path(command_path)
    floors = FloorBook(path.with_name("rfq_floors.json"))
    gate = SessionGate(path.with_name("rfq_sessions.json"))
    labels = LabelBook(path.with_name("rfq_dify_labels.json"))
    if client is None:
        client = client_from_env()
    return run_pending(
        messages, floors=floors, gate=gate, labels=labels, client=client, limit=limit, sender=sender,
    )


def sync_live(
    command_path: Path,
    binding: dict | None,
    book: Any,
    commands: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """读采集库，确认债券状态，并在已装配时调用 Dify。不写采集库，不发消息。"""
    import time

    from .monitor_feed import bound_uid, inbox_for, use_collector

    uid = bound_uid(binding)
    if not uid:
        return [], {}
    use_collector(os.environ.get("QTRADE_AGENT_DB", _DEFAULT_DB))
    try:
        rows = inbox_for(uid)
    except Exception:
        log.info("询价状态确认没有读到采集库")
        return [], {}
    try:
        sync_receipts(book, list(commands or []), rows, time.time())
    except Exception:
        log.info("询价状态确认没有完成")
    try:
        cached = LabelBook(Path(command_path).with_name("rfq_dify_labels.json")).all()
    except Exception:
        cached = {}

    def _later() -> None:
        if not _DIFY_LOCK.acquire(blocking=False):
            return
        try:
            prepare_live(rows, command_path, sender=dispatch_dify_reply)
            flush_replies(rows, command_path)
        except Exception:
            log.info("询价入站没有进入Dify")
        finally:
            _DIFY_LOCK.release()

    threading.Thread(target=_later, name="xunjia-dify", daemon=True).start()
    return rows, cached
