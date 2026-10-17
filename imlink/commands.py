"""询价侧 OSS 指令。默认只落本地文件，不连接 nyoss.yingmi.com，也不上传对象。

红线闸沿用 relay.side_b.phrase.guard。被拦截时不产生指令。
已送达只来自可注入的下行函数返回成功。没有安装下行时保持已提交。
回执对外不带正文，也不带密钥。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from relay.side_b.phrase import guard


def key_configured() -> bool:
    """只判断环境变量是否已配置，不读取后写进日志或响应。"""
    return bool(os.environ.get("OSSBOX_KEY_XUNJIA", "").strip())


_downlink_lock = threading.Lock()
_downlink: Any = None


def install_downlink(fn: Any) -> None:
    """安装本进程的下行函数。传入 None 表示没有真实下行。"""
    global _downlink
    with _downlink_lock:
        _downlink = fn


def _installed_downlink() -> Any:
    with _downlink_lock:
        return _downlink


def _hide_secret(value: str, secret: str) -> str:
    text = str(value or "")
    hidden = str(secret or "")
    if hidden and hidden in text:
        text = text.replace(hidden, "")
    return text.strip()


def default_path() -> Path:
    env = os.environ.get("XUNJIA_COMMANDS_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "data" / "oss_commands.json"


def receipt_view(cmd: dict[str, Any]) -> dict[str, Any]:
    """给前端的回执。不含正文。"""
    ack = cmd.get("ack") or {}
    status = str(cmd.get("status") or "submitted")
    return {
        "cmd_id": str(cmd.get("cmd_id") or ""),
        "status": status,
        "ok": bool(ack.get("ok")) if ack else False,
        "code": str(ack.get("code") or ""),
        "reason": str(ack.get("reason") or ""),
    }


class CommandLog:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or default_path()
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read()

    def accept_text(self, text: str) -> tuple[bool, str]:
        judged = guard(text)
        if not judged.allowed:
            return False, judged.reason or "红线闸拦截"
        return True, ""

    def submit(
        self,
        *,
        workbench_id: str,
        account_uid: str,
        native_id: str,
        text: str,
        idempotency_key: str,
        session_kind: str = "private",
    ) -> dict[str, Any]:
        """调用方必须已经过红线闸。同一 idempotency_key 不另建指令。"""
        key = str(idempotency_key or "").strip()
        kind = session_kind if session_kind in ("private", "group") else "private"
        with self._lock:
            rows = self._read()
            if key:
                prev = next((x for x in rows if x.get("idempotency_key") == key), None)
                if prev is not None:
                    return prev
            cmd = {
                "cmd_id": "cmd_" + uuid.uuid4().hex[:12],
                "workbench_id": workbench_id,
                "channel": "qidian",
                "account_uid": account_uid,
                "op": "send_text",
                "session": {"kind": kind, "native_id": native_id},
                "text": text,
                "idempotency_key": key or ("xj-" + uuid.uuid4().hex[:12]),
                "status": "submitted",
                "ack": None,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            rows.append(cmd)
            self._write(rows)
        return cmd

    def get(self, cmd_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return next((x for x in self._read() if x.get("cmd_id") == cmd_id), None)

    def apply_downlink(self, cmd_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """把下行结果写进指令。成功才是已送达，失败原因里去掉正文。"""
        with self._lock:
            data = self._read()
            cmd = next((x for x in data if x.get("cmd_id") == cmd_id), None)
            if cmd is None:
                raise KeyError(cmd_id)
            if cmd.get("ack") or cmd.get("status") != "submitted":
                return cmd
            secret = str(cmd.get("text") or "")
            if result.get("ok") is True:
                code = _hide_secret(str(result.get("code") or "delivered"), secret) or "delivered"
                cmd["status"] = "delivered"
                cmd["ack"] = {"cmd_id": cmd_id, "ok": True, "code": code, "reason": ""}
            else:
                code = _hide_secret(str(result.get("code") or "failed"), secret) or "failed"
                reason = _hide_secret(str(result.get("reason") or ""), secret) or "下行失败"
                cmd["status"] = "failed"
                cmd["ack"] = {"cmd_id": cmd_id, "ok": False, "code": code, "reason": reason}
            self._write(data)
            return cmd

    def pull_receipt(self, cmd_id: str, *, stale: bool, present: bool, demo_ack: Optional[dict] = None) -> dict[str, Any]:
        """读取已有回执。目录在线本身不是送达。失败原因不含正文。"""
        with self._lock:
            data = self._read()
            cmd = next((x for x in data if x.get("cmd_id") == cmd_id), None)
            if cmd is None:
                raise KeyError(cmd_id)
            if not cmd.get("ack"):
                if stale or not present:
                    ack = {"cmd_id": cmd_id, "ok": False, "code": "unavailable", "reason": "工作台未上报"}
                    cmd["status"] = "failed"
                    cmd["ack"] = ack
                    self._write(data)
                elif isinstance(demo_ack, dict) and demo_ack.get("ok") is False:
                    ack = {
                        "cmd_id": cmd_id,
                        "ok": False,
                        "code": str(demo_ack.get("code") or "failed"),
                        "reason": str(demo_ack.get("reason") or "失败"),
                    }
                    cmd["status"] = "failed"
                    cmd["ack"] = ack
                    self._write(data)
            return receipt_view(cmd)


def prepare_send(
    log: CommandLog,
    text: str,
    *,
    allow_write: bool,
    downlink: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """红线不过就不写指令。放行后调用下行；没有下行函数时保持已提交。"""
    allowed, reason = log.accept_text(text)
    if not allowed:
        return {"ok": False, "blocked": True, "reason": reason}
    if not allow_write:
        return {"ok": False, "blocked": False, "reason": "工作台未上报"}
    cmd = log.submit(text=text, **kwargs)
    sender = downlink if downlink is not None else _installed_downlink()
    if sender is not None and not cmd.get("ack") and cmd.get("status") == "submitted":
        try:
            result = sender(cmd)
        except Exception:
            result = {"ok": False, "code": "downlink_error", "reason": "下行失败"}
        if not isinstance(result, dict):
            result = {"ok": False, "code": "downlink_error", "reason": "下行失败"}
        cmd = log.apply_downlink(str(cmd.get("cmd_id") or ""), result)
    return {"ok": True, "blocked": False, "reason": "", "command": cmd}


# 询价后端进程内共用的指令记录。单测请自建 CommandLog，不要碰这一份。
log = CommandLog()

def inbound_effect(message: dict):
    """公网上报明文与邮件中转解成同一种效果元组。"""
    from imlink.relay_equiv import from_oss
    call = from_oss(message)
    return None if call is None else call.effect()
