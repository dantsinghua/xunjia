"""按通道配置访问采集端。测试只做读：健康、版本、账号、通讯录。不发消息。"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit

from .hmac_sign import auth_headers
from .store import endpoint_of

_API = "/api/v1"


class ProbeError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _insecure(link: dict[str, Any]) -> bool:
    mode = link.get("mode")
    return bool((link.get(mode) or {}).get("tls_insecure"))


def _opener(link: dict[str, Any]) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_NoRedirect()]
    if _insecure(link):
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    return urllib.request.build_opener(*handlers)


def _auth(link: dict[str, Any], method: str, path: str, query: str, body: Optional[bytes]) -> dict[str, str]:
    mode = link.get("mode")
    headers = {"Accept": "application/json", "X-QT-Api-Min": "1.0"}
    if mode == "public":
        block = link.get("public") or {}
        app_id = str(block.get("app_id") or "").strip()
        secret = str(block.get("hmac_secret") or "")
        if not app_id or not secret:
            raise ProbeError("互联网方式须填写 AppId 和 HMAC 密钥")
        headers.update(auth_headers(app_id, secret, method, path, query, body))
        return headers
    token = str((link.get(mode) or {}).get("bearer_token") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def call(link: dict[str, Any], method: str, path: str, *, query: str = "",
         body: Optional[dict] = None, timeout: float = 8) -> tuple[int, Any]:
    base = endpoint_of(link)
    if not base:
        raise ProbeError("还没有采集端地址")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _auth(link, method, path, query, data)
    if data is not None:
        headers["Content-Type"] = "application/json"
    url = base + path + (("?" + query) if query else "")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with _opener(link).open(req, timeout=timeout) as resp:
            raw = resp.read(1_000_000)
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read(4000)
        status = e.code
    except urllib.error.URLError as e:
        raise ProbeError(f"连不上 {urlsplit(base).netloc}：{e.reason}") from e
    except TimeoutError as e:
        raise ProbeError("采集端超时") from e
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = {"raw": text[:300]}
    return status, parsed


def _short_error(status: int, payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else None
        code = payload.get("code") or ""
        if msg:
            return f"HTTP {status} {code} {msg}".strip()
    return f"HTTP {status}"


def _qidian_accounts(payload: Any) -> list[dict[str, str]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("channel") not in (None, "", "qidian"):
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "label": str(row.get("label") or ""),
            "account_nick": str(row.get("self_nick") or row.get("account_nick") or ""),
            "self_uid": str(row.get("self_uid") or ""),
            "state": str(row.get("state") or ""),
        })
    return [x for x in out if x["id"]]


def _people(payload: Any) -> list[dict[str, str]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    people = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        if kind in ("group", "troop"):
            continue
        uin = str(row.get("uin") or row.get("native_id") or "")
        if not uin and isinstance(row.get("id"), str) and ":" in row["id"]:
            uin = row["id"].split(":", 1)[1]
        if not uin or uin.startswith("g_"):
            continue
        name = str(row.get("name") or row.get("remark") or row.get("label") or uin)
        people.append({"uin": uin, "name": name, "remark": str(row.get("remark") or "")})
    return people[:200]


def probe(link: dict[str, Any]) -> dict[str, Any]:
    """只读探测。passed 为真时，这条通道可以拿来做监控和读通讯录。"""
    if link.get("mode") == "oss":
        from .catalog import probe_link
        from .commands import key_configured
        return probe_link(link, [], key_ready=key_configured())
    steps: list[dict[str, Any]] = []
    accounts: list[dict[str, str]] = []
    account = None
    contacts: list[dict[str, str]] = []
    source = ""
    note = ""

    def add(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    base = endpoint_of(link)
    mode = link.get("mode")
    try:
        if mode == "public" and urlsplit(base).scheme != "https" and not _insecure(link):
            add("地址", True, f"{base}（互联网建议用 https）")
        else:
            add("地址", True, base or "空")
        if not base:
            add("地址", False, "还没有采集端地址")
            return _result(False, steps, accounts, None, [], "", "地址为空")

        status, health = call(link, "GET", f"{_API}/system/health")
        if status == 200:
            add("健康", True, "采集端有响应")
        elif status == 401 and mode != "local":
            add("健康", True, "局域网/互联网的健康检查要求令牌，改看版本接口")
        else:
            add("健康", True, _short_error(status, health) + "，继续以版本接口为准")

        status, ver = call(link, "GET", f"{_API}/system/version")
        if status == 200 and isinstance(ver, dict) and ver.get("ok", True):
            api_ver = ver.get("api_version") or ""
            agent = (ver.get("agent") or {}).get("version") if isinstance(ver.get("agent"), dict) else ""
            add("版本", True, f"API {api_ver}  Agent {agent}".strip())
        else:
            add("版本", False, _short_error(status, ver))
            return _result(False, steps, accounts, None, [], "", "鉴权或版本接口未通过")

        status, listing = call(link, "GET", f"{_API}/accounts", query="channel=qidian")
        if status != 200:
            add("企点账号", False, _short_error(status, listing))
            return _result(False, steps, accounts, None, [], "", "读不到账号列表")
        accounts = _qidian_accounts(listing)
        if not accounts:
            add("企点账号", False, "这台采集端上没有企点账号")
            return _result(False, steps, [], None, [], "", "没有企点账号")
        brief = "、".join(a["id"] for a in accounts)
        add("企点账号", True, f"{len(accounts)} 个：{brief}")

        preset = str(link.get("account_id") or "").strip()
        if not preset and len(accounts) == 1:
            want = accounts[0]["id"]
        elif not preset:
            add("选定账号", False, "有多个企点账号，请指定要用的那一个（例如 01 或 02）")
            return _result(False, steps, accounts, None, [], "", "请指定企点账号")
        else:
            want = preset
        account = next((a for a in accounts if a["id"] == want), None)
        if account is None:
            add("选定账号", False, f"{want} 不在这台采集端上，在线的是 {brief}")
            return _result(False, steps, accounts, None, [], "", "账号不在这台采集端")
        picked = f"{account['id']} {account['label']} {account['self_uid']} {account['state']}".strip()
        if not preset:
            add("选定账号", True, f"这台采集端只有一个企点账号，已用 {picked}")
        else:
            add("选定账号", True, picked)

        status, book = call(link, "GET", f"{_API}/accounts/{want}/contacts")
        if status == 200:
            contacts = _people(book)
            source = "contacts"
            add("通讯录", True, f"通讯录 {len(contacts)} 人")
        elif status == 404:
            q = f"account_id={want}&kind=private&limit=200"
            status, sessions = call(link, "GET", f"{_API}/sessions", query=q)
            if status == 200:
                contacts = _people(sessions)
                source = "sessions"
                add("通讯录", True, f"采集端还没有通讯录接口，已改读私聊会话 {len(contacts)} 人")
            else:
                add("通讯录", False, _short_error(status, sessions))
                return _result(False, steps, accounts, account, [], "", "通讯录和会话都读失败", adopted=want)
        else:
            add("通讯录", False, _short_error(status, book))
            return _result(False, steps, accounts, account, [], "", "通讯录读取失败", adopted=want)
    except ProbeError as e:
        add("连接", False, str(e))
        return _result(False, steps, accounts, account, contacts, source, str(e))

    passed = all(s["ok"] for s in steps)
    return _result(passed, steps, accounts, account, contacts, source, note, adopted=want if account else "")


def _result(passed: bool, steps, accounts, account, contacts, source, note, adopted: str = "") -> dict[str, Any]:
    return {
        "passed": passed,
        "steps": steps,
        "accounts": accounts,
        "account": account,
        "contacts": contacts,
        "contacts_source": source,
        "note": note,
        "adopted_account_id": adopted if adopted else (account or {}).get("id", "") if account else "",
    }


def fetch_contacts(link: dict[str, Any]) -> dict[str, Any]:
    if link.get("mode") == "oss":
        from .catalog import contacts_of
        block = link.get("oss") or {}
        people = contacts_of(
            [],
            str(block.get("workbench_id") or ""),
            str(block.get("account_uid") or ""),
        )
        return {"source": "catalog", "contacts": people}
    want = str(link.get("account_id") or "").strip()
    if not want:
        raise ProbeError("还没指定企点账号")
    status, book = call(link, "GET", f"{_API}/accounts/{want}/contacts")
    if status == 200:
        return {"source": "contacts", "contacts": _people(book)}
    if status == 404:
        q = f"account_id={want}&kind=private&limit=200"
        status, sessions = call(link, "GET", f"{_API}/sessions", query=q)
        if status == 200:
            return {"source": "sessions", "contacts": _people(sessions)}
        raise ProbeError(_short_error(status, sessions))
    raise ProbeError(_short_error(status, book))


def send_text(link: dict[str, Any], uin: str, text: str, idempotency_key: str) -> dict[str, Any]:
    if link.get("mode") == "oss":
        raise ProbeError("公网信箱不直连企点")
    account_id = str(link.get("account_id") or "").strip()
    native = uin.split(":", 1)[1] if uin.startswith(account_id + ":") else uin
    body = {
        "session": f"{account_id}:{native}",
        "text": text,
        "idempotency_key": idempotency_key,
        "async": link.get("mode") == "public",
    }
    status, payload = call(link, "POST", f"{_API}/accounts/{account_id}/send", body=body, timeout=30)
    if status not in (200, 202):
        raise ProbeError(_short_error(status, payload))
    return {"status": status, "payload": payload if isinstance(payload, dict) else {}}
