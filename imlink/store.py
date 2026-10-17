"""多条采集端配置。每条 = 一台采集机 + 一个企点账号，或一条公网信箱绑定。

本机、局域网、互联网三种方式字段不同。公网信箱只存工作台和企点号。
密钥只落在 data/im_link.json（0600），读接口只回「是否已填」和末四位，不回明文。
公网信箱的密钥不进这个文件，只看环境变量 OSSBOX_KEY_XUNJIA 是否已配置。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .catalog import bind_error, fill_nick, status_of
from .commands import key_configured

MODES = ("local", "lan", "public", "oss", "mail")
MODE_LABEL = {"local": "本机", "lan": "局域网", "public": "互联网", "oss": "公网上报", "mail": "邮件中转"}

_SECRET = {
    "local": "bearer_token",
    "lan": "bearer_token",
    "public": "hmac_secret",
    "mail": "mail_password",
}

_DEFAULT_BLOCK = {
    "local": {
        "base_url": "http://127.0.0.1:17600",
        "bearer_token": "",
        "callback_url": "http://127.0.0.1:8120/api/im/events",
    },
    "lan": {
        "scheme": "http",
        "host": "",
        "port": 17600,
        "bearer_token": "",
        "callback_url": "",
        "tls_insecure": False,
    },
    "public": {
        "base_url": "",
        "app_id": "",
        "hmac_secret": "",
        "callback_url": "",
        "tls_insecure": False,
    },
    "oss": {
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "qtrade-nyoss-lab",
        "workbench_id": "anlin-dev",
        "account_uid": "",
        "account_nick": "",
    },
    "mail": {
        "workbench_id": "",
        "account_uid": "",
        "account_nick": "",
        "mail_host": "",
        "mail_port": 993,
        "mail_ssl": True,
        "mail_user": "",
        "mail_password": "",
    },
}


def default_path() -> Path:
    env = os.environ.get("IMLINK_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "data" / "im_link.json"


def _blank_link(name: str = "") -> dict[str, Any]:
    return {
        "id": "lk_" + uuid.uuid4().hex[:8],
        "name": name,
        "mode": "lan",
        "account_id": "",
        "local": deepcopy(_DEFAULT_BLOCK["local"]),
        "lan": deepcopy(_DEFAULT_BLOCK["lan"]),
        "public": deepcopy(_DEFAULT_BLOCK["public"]),
        "oss": deepcopy(_DEFAULT_BLOCK["oss"]),
        "mail": deepcopy(_DEFAULT_BLOCK["mail"]),
        "last_test": None,
    }


def _as_bool(v: Any) -> bool:
    return bool(v) and str(v).lower() not in ("0", "false", "no")


def endpoint_of(link: dict[str, Any]) -> str:
    mode = link.get("mode")
    if mode == "oss":
        return str((link.get("oss") or {}).get("workbench_id") or "").strip()
    if mode == "mail":
        block = link.get("mail") or {}
        host = str(block.get("mail_host") or "").strip()
        return f"{host}:{block.get('mail_port') or ''}" if host else ""
    if mode == "lan":
        b = link.get("lan") or {}
        host = str(b.get("host") or "").strip()
        if not host:
            return ""
        return f"{b.get('scheme') or 'http'}://{host}:{int(b.get('port') or 17600)}"
    block = link.get(mode) or {}
    return str(block.get("base_url") or "").strip().rstrip("/")


def callback_of(link: dict[str, Any]) -> str:
    if link.get("mode") in ("oss", "mail"):
        return ""
    block = link.get(link.get("mode")) or {}
    return str(block.get("callback_url") or "").strip()


def _secret_of(link: dict[str, Any]) -> str:
    mode = link.get("mode") or "local"
    if mode not in _SECRET:
        return ""
    return str((link.get(mode) or {}).get(_SECRET[mode]) or "")


def fingerprint(link: dict[str, Any]) -> str:
    """连接要素的摘要。改地址、账号或密钥后，旧的测试结果作废。"""
    mode = link.get("mode") or "local"
    if mode in ("oss", "mail"):
        block = link.get(mode) or {}
        material = {
            "mode": mode,
            "workbench_id": block.get("workbench_id") or "",
            "account_uid": block.get("account_uid") or "",
            "key_set": "1" if key_configured() else "0",
        }
    else:
        block = link.get(mode) or {}
        secret = _secret_of(link)
        material = {
            "mode": mode,
            "endpoint": endpoint_of(link),
            "account_id": link.get("account_id") or "",
            "app_id": block.get("app_id") or "",
            "callback": callback_of(link),
            "tls_insecure": bool(block.get("tls_insecure")),
            "secret": hashlib.sha256(secret.encode()).hexdigest() if secret else "",
        }
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def test_fresh(link: dict[str, Any]) -> bool:
    last = link.get("last_test") or {}
    return bool(last.get("passed")) and last.get("fingerprint") == fingerprint(link)


def oss_catalog_ready(link: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    """公网上报只要目录里仍有这条企点号，监控就可以接入。不要求先点过测试。"""
    if link.get("mode") != "oss":
        return False
    block = link.get("oss") or {}
    if not str(block.get("account_uid") or "").strip():
        return False
    info = status_of(link, rows)
    return bool(info.get("present"))


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 4:
        return "••••"
    return "••••" + secret[-4:]


def _check_url(url: str, *, what: str) -> Optional[str]:
    if not url:
        return f"请填写{what}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"{what}须是 http 或 https 地址"
    if parts.username or parts.password:
        return f"{what}不要把令牌写进地址"
    return None


def validate(link: dict[str, Any]) -> Optional[str]:
    mode = link.get("mode")
    if mode not in MODES:
        return "连接方式须是本机、局域网、互联网或公网信箱"
    if not str(link.get("name") or "").strip():
        return "请填写名称，用来区分 A 机 / B 机"
    if mode == "oss":
        block = link.get("oss") or {}
        endpoint = str(block.get("endpoint") or "").strip()
        bucket = str(block.get("bucket") or "").strip()
        workbench_id = str(block.get("workbench_id") or "").strip()
        if not endpoint.startswith("https://") and not endpoint.startswith("http://"):
            return "请填写上报地址"
        if not bucket or "/" in bucket:
            return "请填写桶名"
        if not workbench_id or not workbench_id[0].isalnum():
            return "请填写终端编号"
        return None
    if mode == "mail":
        block = link.get("mail") or {}
        if not str(block.get("mail_host") or "").strip():
            return "请填写收信服务器"
        try:
            port = int(block.get("mail_port") or 0)
        except (TypeError, ValueError):
            return "端口须是数字"
        if not 1 <= port <= 65535:
            return "端口须在 1 到 65535"
        return None
    if mode == "local":
        return _check_url(endpoint_of(link), what="采集端地址")
    if mode == "lan":
        b = link.get("lan") or {}
        if b.get("scheme") not in ("http", "https"):
            return "局域网协议须是 http 或 https"
        host = str(b.get("host") or "").strip()
        if not host or "://" in host or "/" in host:
            return "局域网只填主机名或 IP，例如 192.168.1.11"
        try:
            port = int(b.get("port") or 0)
        except (TypeError, ValueError):
            return "端口须是数字"
        if not 1 <= port <= 65535:
            return "端口须在 1 到 65535"
        cb = str(b.get("callback_url") or "").strip()
        if cb:
            return _check_url(cb, what="回呼地址")
        return None
    err = _check_url(endpoint_of(link), what="采集端地址")
    if err:
        return err
    if not str((link.get("public") or {}).get("app_id") or "").strip():
        return "互联网方式须填写 AppId"
    return None


def _merge_block(mode: str, old: dict, patch: dict) -> dict:
    if mode == "mail":
        out = deepcopy(_DEFAULT_BLOCK["mail"])
        for key in ("workbench_id", "account_uid", "account_nick", "mail_host", "mail_user"):
            if isinstance(old, dict) and old.get(key):
                out[key] = old.get(key)
        if isinstance(old, dict) and old.get("mail_port"):
            out["mail_port"] = int(old.get("mail_port") or 993)
        if isinstance(old, dict) and "mail_ssl" in old:
            out["mail_ssl"] = bool(old.get("mail_ssl"))
        if isinstance(old, dict) and old.get("mail_password"):
            out["mail_password"] = old.get("mail_password")
        patch = patch or {}
        for key in ("workbench_id", "account_uid", "mail_host", "mail_user"):
            if key in patch:
                out[key] = str(patch.get(key) or "").strip()
        if "mail_port" in patch:
            out["mail_port"] = int(patch.get("mail_port") or 993)
        if "mail_ssl" in patch:
            out["mail_ssl"] = bool(patch.get("mail_ssl"))
        incoming = patch.get("mail_password")
        if isinstance(incoming, str) and incoming.strip() and not incoming.startswith("••••"):
            out["mail_password"] = incoming.strip()
        if str(out.get("account_uid") or "") != str((old or {}).get("account_uid") or ""):
            out["account_nick"] = ""
        return out
    if mode == "oss":
        out = deepcopy(_DEFAULT_BLOCK["oss"])
        for key in ("endpoint", "bucket", "workbench_id", "account_uid", "account_nick"):
            if isinstance(old, dict) and old.get(key):
                out[key] = old.get(key)
        for key in ("endpoint", "bucket", "workbench_id", "account_uid"):
            if key in (patch or {}):
                out[key] = str((patch or {}).get(key) or "").strip()
        if str(out.get("account_uid") or "") != str((old or {}).get("account_uid") or ""):
            out["account_nick"] = ""
        return out
    out = deepcopy(_DEFAULT_BLOCK[mode])
    out.update(old or {})
    secret_key = _SECRET[mode]
    for k, v in (patch or {}).items():
        if k == secret_key:
            continue
        if k in out:
            out[k] = v
    incoming = patch.get(secret_key) if patch else None
    if isinstance(incoming, str) and incoming.strip() and not incoming.startswith("••••"):
        out[secret_key] = incoming.strip()
    elif secret_key not in (old or {}):
        out[secret_key] = ""
    if mode == "lan":
        out["port"] = int(out.get("port") or 17600)
        out["tls_insecure"] = _as_bool(out.get("tls_insecure"))
        out["scheme"] = out.get("scheme") or "http"
    if mode == "public":
        out["tls_insecure"] = _as_bool(out.get("tls_insecure"))
        out["base_url"] = str(out.get("base_url") or "").strip().rstrip("/")
    if mode == "local":
        out["base_url"] = str(out.get("base_url") or "").strip().rstrip("/")
    return out


def public_view(link: dict[str, Any], active_id: str, catalog_rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    mode = link.get("mode") or "local"
    last = link.get("last_test") or {}
    rows = [] if catalog_rows is None else catalog_rows
    probe_fresh = test_fresh(link)
    fresh = probe_fresh or oss_catalog_ready(link, rows)
    form = {}
    for m in MODES:
        block = deepcopy(link.get(m) or _DEFAULT_BLOCK[m])
        if m == "oss":
            block["secret_set"] = False
            block["secret_mask"] = ""
            form[m] = block
            continue
        secret_key = _SECRET[m]
        secret = str(block.get(secret_key) or "")
        block.pop(secret_key, None)
        block["secret_set"] = bool(secret)
        block["secret_mask"] = _mask(secret)
        form[m] = block
    account = last.get("account") if probe_fresh else None
    if isinstance(account, dict):
        account = dict(account)
    workbench_id = ""
    account_uid = ""
    account_nick = ""
    catalog_stale = False
    catalog_at = None
    if mode == "oss":
        block = link.get("oss") or {}
        workbench_id = str(block.get("workbench_id") or "")
        account_uid = str(block.get("account_uid") or "")
        info = status_of(link, rows)
        account_nick = str(info.get("account_nick") or block.get("account_nick") or "")
        catalog_stale = bool(info.get("stale"))
        catalog_at = info.get("updated_at")
        if isinstance(account, dict):
            account["account_nick"] = account_nick
            account["self_uid"] = account_uid or account.get("self_uid") or ""
            account["label"] = account_nick
    elif fresh and isinstance(account, dict):
        account_uid = str(account.get("self_uid") or "")
        account_nick = str(account.get("account_nick") or "")
    return {
        "id": link["id"],
        "name": link.get("name") or "",
        "mode": mode,
        "mode_label": MODE_LABEL.get(mode, mode),
        "account_id": "" if mode in ("oss", "mail") else (link.get("account_id") or ""),
        "workbench_id": workbench_id,
        "account_uid": account_uid,
        "account_nick": account_nick,
        "catalog_stale": catalog_stale,
        "catalog_at": catalog_at,
        "endpoint": endpoint_of(link),
        "callback_url": callback_of(link),
        "secret_set": False if mode == "oss" else bool(_secret_of(link)),
        "tested": fresh,
        "tested_at": last.get("at") if probe_fresh else None,
        "test_note": "" if fresh else (last.get("note") or ""),
        "kicked_by": "" if probe_fresh else (last.get("kicked_by") or ""),
        "displaced": (last.get("displaced") or []) if probe_fresh else [],
        "account": account,
        "contacts": [],
        "contacts_source": "",
        "active": link["id"] == active_id,
        "form": form,
    }


def _drop_saved_book(data: dict[str, Any]) -> dict[str, Any]:
    """通道文件只留配置和测试状态。通讯录不进 last_test。"""
    for link in data.get("links") or []:
        if not isinstance(link, dict):
            continue
        last = link.get("last_test")
        if isinstance(last, dict):
            for key in ("contacts", "contacts_source", "org", "groups", "group_native_ids"):
                last.pop(key, None)
    return data


class Store:
    def __init__(self, path: Optional[Path] = None, catalog_rows: Optional[list[dict[str, Any]]] = None):
        self.path = path or default_path()
        # 和通道文件同目录，正式环境就是 data/address_book.json。单测用临时目录，不碰正式文件。
        self.address_book_path = self.path.parent / "address_book.json"
        self._lock = threading.Lock()
        # None 表示走对象存储。注入的列表（含空列表）只给单测，不回落到南京台样例。
        self._catalog_rows = catalog_rows
        self._live_rows: list[dict[str, Any]] = []
        self._live_err = ""
        self._live_at = 0.0

    def catalog_rows(self) -> list[dict[str, Any]]:
        if self._catalog_rows is not None:
            return self._catalog_rows
        rows, _err = self._live_catalog()
        return rows

    def catalog_error(self) -> str:
        """密钥已装载但目录没拉下来时的原因。没装载密钥或用了注入目录时为空。"""
        if self._catalog_rows is not None:
            return ""
        _rows, err = self._live_catalog()
        return err

    def _live_catalog(self) -> tuple[list[dict[str, Any]], str]:
        if not key_configured():
            return [], ""
        now = time.time()
        ttl = 3.0 if self._live_err else 10.0
        if self._live_at and (now - self._live_at) < ttl:
            return self._live_rows, self._live_err
        from .oss_catalog import CatalogError, load_live_catalog
        try:
            rows = load_live_catalog()
            err = ""
        except CatalogError as exc:
            rows = []
            err = str(exc)
        self._live_rows = rows
        self._live_err = err
        self._live_at = now
        return rows, err

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"active_id": "", "links": []}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {"active_id": "", "links": []}
        if not isinstance(data, dict):
            return {"active_id": "", "links": []}
        data.setdefault("active_id", "")
        data.setdefault("links", [])
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data = _drop_saved_book(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def list_public(self) -> dict[str, Any]:
        with self._lock:
            data = self._read()
        rows = self.catalog_rows()
        active = str(data.get("active_id") or "")
        current_link = next((x for x in data["links"] if x["id"] == active), None)
        current_ok = current_link is not None and (test_fresh(current_link) or oss_catalog_ready(current_link, rows))
        if not current_ok:
            ready = [x["id"] for x in data["links"] if oss_catalog_ready(x, rows)]
            if len(ready) == 1:
                chosen = ready[0]
                with self._lock:
                    data = self._read()
                    still = next((x for x in data["links"] if x["id"] == data.get("active_id")), None)
                    if still is None or not (test_fresh(still) or oss_catalog_ready(still, rows)):
                        data["active_id"] = chosen
                        self._write(data)
                active = chosen
        links = [public_view(x, active, rows) for x in data["links"]]
        current = next((x for x in links if x["active"] and x["tested"]), None)
        return {"active_id": current["id"] if current else "", "links": links}

    def get(self, link_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            data = self._read()
        return next((x for x in data["links"] if x["id"] == link_id), None)

    def _prepare(self, merged: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any]:
        rows = self.catalog_rows()
        err = bind_error(merged, rows, others)
        if err:
            raise ValueError(err)
        merged = fill_nick(merged, rows)
        err = validate(merged)
        if err:
            raise ValueError(err)
        return merged

    def create(self, patch: dict[str, Any]) -> dict[str, Any]:
        link = _blank_link()
        merged = self._apply(link, patch)
        with self._lock:
            data = self._read()
            merged = self._prepare(merged, data["links"])
            data["links"].append(merged)
            self._write(data)
        return merged

    def update(self, link_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            old = next((x for x in data["links"] if x["id"] == link_id), None)
            if old is None:
                raise KeyError(link_id)
            merged = self._apply(old, patch)
            merged = self._prepare(merged, data["links"])
            if fingerprint(merged) != fingerprint(old):
                merged["last_test"] = None
                if data.get("active_id") == link_id:
                    data["active_id"] = ""
            data["links"] = [merged if x["id"] == link_id else x for x in data["links"]]
            self._write(data)
        return merged

    def delete(self, link_id: str) -> None:
        with self._lock:
            data = self._read()
            data["links"] = [x for x in data["links"] if x["id"] != link_id]
            if data.get("active_id") == link_id:
                data["active_id"] = ""
            self._write(data)

    def save_test(self, link_id: str, result: dict[str, Any], account_id: str = "") -> dict[str, Any]:
        with self._lock:
            data = self._read()
            old = next((x for x in data["links"] if x["id"] == link_id), None)
            if old is None:
                raise KeyError(link_id)
            if account_id and old.get("mode") != "oss":
                old["account_id"] = account_id
            account = result.get("account") or {}
            seat = str(account.get("self_uid") or "") if result.get("passed") else ""
            displaced: list[str] = []
            # 同一个企点号同一时间只能在一台采集机上在线。公网信箱重复绑定在保存时就拒绝。
            if seat and old.get("mode") != "oss":
                for other in data["links"]:
                    if other["id"] == link_id:
                        continue
                    prev = other.get("last_test") or None
                    if not prev or not prev.get("passed"):
                        continue
                    prev_uid = str((prev.get("account") or {}).get("self_uid") or "")
                    if not prev_uid or prev_uid != seat:
                        continue
                    prev["passed"] = False
                    prev["kicked_by"] = old.get("name") or link_id
                    prev["kicked_uid"] = seat
                    prev["note"] = (
                        f"企点号 {seat} 已在「{prev['kicked_by']}」登录，本机登录被踢下线。"
                        "要再使用这台采集机，需重新登录后重测。"
                    )
                    displaced.append(other.get("name") or other["id"])
                    if data.get("active_id") == other["id"]:
                        data["active_id"] = ""
            old["last_test"] = {
                "passed": bool(result.get("passed")),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "fingerprint": fingerprint(old),
                "note": result.get("note") or "",
                "kicked_by": "",
                "displaced": displaced,
                "steps": result.get("steps") or [],
                "account": result.get("account"),
                "accounts": result.get("accounts") or [],
            }
            if not result.get("passed") and data.get("active_id") == link_id:
                data["active_id"] = ""
            self._write(data)
        return old

    def activate(self, link_id: str) -> dict[str, Any]:
        rows = self.catalog_rows()
        with self._lock:
            data = self._read()
            link = next((x for x in data["links"] if x["id"] == link_id), None)
            if link is None:
                raise KeyError(link_id)
            if not (test_fresh(link) or oss_catalog_ready(link, rows)):
                raise ValueError("这条通道还没测试通过，或改过配置后还没重测")
            if link.get("mode") in ("oss", "mail"):
                block = link.get(link.get("mode")) or {}
                if not str(block.get("account_uid") or "").strip():
                    raise ValueError("请先选择企点号")
            elif not link.get("account_id"):
                raise ValueError("请先指定企点账号")
            data["active_id"] = link_id
            self._write(data)
        return link

    def saved_binding(self) -> Optional[dict[str, Any]]:
        """已选中的公网上报或邮件中转。目录暂时拉不到也不当作没绑定。"""
        with self._lock:
            data = self._read()
        link = next((x for x in data["links"] if x["id"] == data.get("active_id")), None)
        mode = (link or {}).get("mode")
        block = (link or {}).get(mode) if isinstance(link, dict) else None
        if isinstance(block, dict) and str(block.get("account_uid") or "").strip():
            return link
        return None

    def active(self) -> Optional[dict[str, Any]]:
        with self._lock:
            data = self._read()
        link = next((x for x in data["links"] if x["id"] == data.get("active_id")), None)
        if link is None:
            return None
        if test_fresh(link) or oss_catalog_ready(link, self.catalog_rows()):
            return link
        return None

    @staticmethod
    def _apply(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        out = deepcopy(old)
        if "oss" not in out:
            out["oss"] = deepcopy(_DEFAULT_BLOCK["oss"])
        if "mail" not in out:
            out["mail"] = deepcopy(_DEFAULT_BLOCK["mail"])
        if patch.get("name") is not None:
            out["name"] = str(patch.get("name") or "").strip()
        if patch.get("mode") in MODES:
            out["mode"] = patch["mode"]
        if "account_id" in patch:
            out["account_id"] = str(patch.get("account_id") or "").strip()
        for mode in MODES:
            if isinstance(patch.get(mode), dict):
                out[mode] = _merge_block(mode, old.get(mode) or {}, patch[mode])
        return out
