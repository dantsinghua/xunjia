"""把询价指令密封后交到公网信箱，并等待采集端回执。

对象名 ``wb/{workbench_id}/in/xunjia/{cmd_id}.bin``。只使用环境变量
``OSSBOX_KEY_XUNJIA``。存款密钥不参与密封，也不参与解回执。
只写本地指令文件、没有上行、没有成功回执，都不算送达。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional, Protocol

from .commands import receipt_view
from .crypto import open_sealed, seal
from .oss_catalog import decode_seal_key, http_get
from .oss_http import OSSHTTP, authorization as request_authorization
from .sts_credentials import CredentialSet, CredentialsError, NoCredentialRedirect, STSCredentials

_WB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_CONTENT_TYPE = "application/octet-stream"
log = logging.getLogger("xunjia.oss_send")


class OssSendError(Exception):
    """给页面的原因。消息里不放正文、密钥或访问凭证。"""


class Uplink(Protocol):
    def put(self, name: str, body: bytes) -> None: ...

    def get(self, name: str) -> Optional[bytes]: ...


_uplink_lock = threading.Lock()
_uplink: Any = None


def install_uplink(uplink: Any) -> None:
    """测试注入假的对象存储。传入 None 时走真实上行。"""
    global _uplink
    with _uplink_lock:
        _uplink = uplink


def installed_uplink() -> Any:
    with _uplink_lock:
        return _uplink


def xunjia_key() -> bytes:
    """只读询价密钥。不读取、不回退到存款密钥。"""
    text = os.environ.get("OSSBOX_KEY_XUNJIA", "").strip()
    if not text:
        raise OssSendError("没有询价密钥")
    try:
        return decode_seal_key(text)
    except Exception:
        raise OssSendError("询价密钥无效") from None


def in_object_name(workbench_id: str, cmd_id: str) -> str:
    wid = str(workbench_id or "").strip()
    cid = str(cmd_id or "").strip()
    if _WB.fullmatch(wid) is None or _WB.fullmatch(cid) is None:
        raise OssSendError("指令对象名无效")
    return f"wb/{wid}/in/xunjia/{cid}.bin"


def ack_object_name(workbench_id: str, cmd_id: str) -> str:
    wid = str(workbench_id or "").strip()
    cid = str(cmd_id or "").strip()
    if _WB.fullmatch(wid) is None or _WB.fullmatch(cid) is None:
        raise OssSendError("回执对象名无效")
    return f"wb/{wid}/ack/{cid}.bin"


def command_payload(cmd: dict[str, Any]) -> dict[str, Any]:
    """上行明文。``text`` 用指令里的原文，不按字段重拼。"""
    session = cmd.get("session") if isinstance(cmd.get("session"), dict) else {}
    kind = session.get("kind") if session.get("kind") in ("private", "group") else "private"
    native_id = str(session.get("native_id") or "").strip()
    text = cmd.get("text")
    if not isinstance(text, str) or text == "":
        raise OssSendError("没有报价正文")
    payload = {
        "cmd_id": str(cmd.get("cmd_id") or "").strip(),
        "workbench_id": str(cmd.get("workbench_id") or "").strip(),
        "channel": "qidian",
        "account_uid": str(cmd.get("account_uid") or "").strip(),
        "op": "send_text",
        "session": {"kind": kind, "native_id": native_id},
        "text": text,
        "idempotency_key": str(cmd.get("idempotency_key") or "").strip(),
    }
    if not all(payload[key] for key in ("cmd_id", "workbench_id", "account_uid", "idempotency_key")):
        raise OssSendError("指令缺少必要字段")
    if not native_id:
        raise OssSendError("指令缺少必要字段")
    return payload


def seal_command(cmd: dict[str, Any], key: bytes) -> bytes:
    plain = json.dumps(command_payload(cmd), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return seal(bytes(key), plain)


def open_payload(blob: bytes, key: bytes) -> dict[str, Any]:
    """解开回执。原始密封或封套里的询价槽都可以，不读存款槽。"""
    try:
        plain = _plain_bytes(blob, key)
        data = json.loads(plain.decode("utf-8"))
    except Exception:
        raise OssSendError("采集端回执解不开") from None
    if not isinstance(data, dict):
        raise OssSendError("采集端回执解不开")
    return data


def _plain_bytes(blob: bytes, key: bytes) -> bytes:
    try:
        return open_sealed(key, blob)
    except ValueError:
        pass
    try:
        env = json.loads(blob.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("密封包无法打开")
    seals = env.get("seals") if isinstance(env, dict) else None
    slot = seals.get("xunjia") if isinstance(seals, dict) else None
    if not isinstance(slot, str) or not slot:
        raise ValueError("密封包无法打开")
    raw = base64.b64decode(slot, validate=False)
    return open_sealed(key, raw)


def _timeout() -> float:
    raw = os.environ.get("XUNJIA_ACK_TIMEOUT_S", "").strip()
    if not raw:
        return 90.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 15.0


def _mark_failed(log: Any, cmd_id: str, reason: str) -> str:
    try:
        updated = log.apply_downlink(cmd_id, {"ok": False, "code": "failed", "reason": reason})
    except Exception:
        return reason or "发送失败"
    view = receipt_view(updated)
    return str(view.get("reason") or reason or "发送失败")


def deliver_uploaded(log: Any, cmd: dict[str, Any]) -> dict[str, Any]:
    """密封并上传。只有回执 ``ok`` 为真才返回成功。"""
    trace = logging.getLogger("xunjia.oss_send")
    cmd_id = str(cmd.get("cmd_id") or "")
    try:
        key = xunjia_key()
        name = in_object_name(str(cmd.get("workbench_id") or ""), cmd_id)
        ack_name = ack_object_name(str(cmd.get("workbench_id") or ""), cmd_id)
        blob = seal_command(cmd, key)
    except OssSendError as exc:
        hint = _mark_failed(log, cmd_id, str(exc))
        return {"ok": False, "hint": hint}
    uplink = installed_uplink()
    if uplink is None:
        uplink = LiveUplink()
    try:
        uplink.put(name, blob)
        # CommandLog 没有 info。这里若再调 log.info，写入成功也会被收成「没有上行」。
        trace.warning(
            "询价判定断点 cmd_id=%s object=%s branch=written nbytes=%s",
            cmd_id, name, len(blob),
        )
    except OssSendError as exc:
        trace.warning(
            "询价判定断点 cmd_id=%s object=%s branch=put_rejected hint=%s",
            cmd_id, name, exc,
        )
        hint = _mark_failed(log, cmd_id, str(exc))
        return {"ok": False, "hint": hint}
    except Exception as exc:
        trace.warning(
            "询价判定断点 cmd_id=%s object=%s branch=put_exception exc=%s",
            cmd_id, name, type(exc).__name__,
        )
        hint = _mark_failed(log, cmd_id, "指令没有上行到公网信箱")
        return {"ok": False, "hint": hint}
    deadline = time.monotonic() + _timeout()
    while True:
        try:
            raw = uplink.get(ack_name)
        except Exception:
            raw = None
        if raw:
            try:
                ack = open_payload(raw, key)
            except OssSendError as exc:
                hint = _mark_failed(log, cmd_id, str(exc))
                return {"ok": False, "hint": hint}
            ack_id = str(ack.get("cmd_id") or "")
            trace.warning(
                "询价回执断点 cmd_id=%s ack=%s ok=%s code=%s reason=%s",
                cmd_id, ack_name, ack.get("ok"), ack.get("code"), str(ack.get("reason") or ""),
            )
            if ack.get("ok") is True and ack_id == cmd_id:
                try:
                    log.apply_downlink(cmd_id, {"ok": True, "code": "delivered"})
                except Exception:
                    return {"ok": False, "hint": "没有采集端成功回执"}
                return {"ok": True, "hint": ""}
            reason = str(ack.get("reason") or "采集端发送失败")
            hint = _mark_failed(log, cmd_id, reason)
            return {"ok": False, "hint": hint or "采集端发送失败"}
        if time.monotonic() >= deadline:
            trace.warning("询价判定断点 cmd_id=%s object=%s branch=timeout", cmd_id, ack_name)
            hint = _mark_failed(log, cmd_id, "没有采集端成功回执")
            return {"ok": False, "hint": hint}
        time.sleep(0.4)


def put_authorization(access_id: str, access_secret: str, date: str, bucket: str, key: str, security_token: str = "") -> str:
    return request_authorization("PUT", access_id, access_secret, date, bucket, key,
                                 content_type=_CONTENT_TYPE, security_token=security_token)


_OSS_TAG = re.compile(br"<Code>([A-Za-z0-9]{1,80})</Code>")
_SAFE_OSS_CODES = {"AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId", "SecurityTokenExpired",
                   "ExpiredToken", "InvalidSecurityToken", "NoSuchBucket", "NoSuchKey", "RequestTimeTooSkewed"}


def oss_error_fields(status: int, body: bytes) -> dict[str, str]:
    """日志仅保留已知错误类别；服务返回的 Message 可能包含凭证。"""
    fields = {"status": str(status)}
    for value in _OSS_TAG.findall((body or b"")[:65536]):
        code = value.decode("ascii")
        if code in _SAFE_OSS_CODES:
            fields["code"] = code
            break
    return fields


def http_put(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoCredentialRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(1024)
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(1024)
        except Exception:
            payload = b""
        finally:
            exc.close()
        return int(exc.code), payload
    except (TimeoutError, urllib.error.URLError):
        raise OssSendError("指令没有上行到公网信箱") from None


class LiveUplink:
    """真实对象存储。单测注入 ``install_uplink`` 时不会走到这里。"""

    def __init__(self, *, credentials: STSCredentials | CredentialSet | None = None,
                 get_transport=None, put_transport=None):
        self._credentials = credentials
        self._get_transport = get_transport
        self._put_transport = put_transport

    def _cfg(self) -> dict[str, str]:
        from .oss_catalog import _settings

        return _settings()

    def _request(self, method: str, name: str, body: bytes | None = None) -> tuple[int, bytes]:
        def transport(method, url, headers, body, timeout):
            if method == "GET":
                return (self._get_transport or http_get)(url, headers, timeout)
            return (self._put_transport or http_put)(url, headers, body, timeout)
        try:
            return OSSHTTP(endpoint=self._cfg()["endpoint"], bucket=self._cfg()["bucket"], credentials=self._credentials,
                           transport=transport).request(method, name, body)
        except CredentialsError as exc:
            raise OssSendError(str(exc)) from None

    def put(self, name: str, body: bytes) -> None:
        try:
            status, payload = self._request("PUT", name, body)
        except OssSendError as exc:
            cause = exc.__cause__
            log.warning(
                "询价上行断点 object=%s branch=transport exc=%s nbytes=%s",
                name, type(cause).__name__ if cause is not None else "OssSendError",
                len(body),
            )
            raise
        if status not in (200, 204):
            fields = oss_error_fields(status, payload)
            log.warning(
                "询价上行断点 object=%s branch=rejected status=%s oss_code=%s nbytes=%s",
                name, fields["status"], fields.get("code", ""), len(body),
            )
            raise OssSendError("指令没有上行到公网信箱")
        log.warning("询价上行断点 object=%s branch=written status=%s nbytes=%s", name, status, len(body))

    def get(self, name: str) -> Optional[bytes]:
        try:
            status, body = self._request("GET", name)
        except Exception as exc:
            log.warning("询价回执断点 object=%s branch=read_exception exc=%s", name, type(exc).__name__)
            return None
        if status == 404 or not body:
            return None
        if status != 200:
            log.warning("询价回执断点 object=%s branch=read status=%s", name, status)
            return None
        return body
