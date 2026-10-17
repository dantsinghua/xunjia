"""从对象存储拉取工作台目录，并用询价密钥解开。

对象体是 UTF-8 JSON 封套：``{"v":1,"seals":{"deposit":"...","xunjia":"..."}}``。
只解 ``seals.xunjia``。密文布局与 ``open_sealed`` 相同。本模块只发 GET，不上传、不覆盖。
密钥、访问凭证不得出现在异常、日志或接口响应里。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

from .catalog import public_catalog
from .crypto import open_sealed
from .oss_http import OSSHTTP, authorization as request_authorization
from .sts_credentials import CredentialSet, CredentialsError, NoCredentialRedirect, STSCredentials

_WB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_BUCKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DEFAULT_ENDPOINT = "https://oss-cn-hangzhou.aliyuncs.com"
_DEFAULT_BUCKET = "qtrade-nyoss-lab"
_DEFAULT_WORKBENCH = "anlin-dev"

Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class CatalogError(Exception):
    """给页面看的原因。消息里不得带密钥、访问凭证或密文。"""


def decode_seal_key(text: str) -> bytes:
    """把环境里的询价密钥还原成 AES 密钥。失败时不回显原文。"""
    raw = str(text or "").strip()
    if all(c in "0123456789abcdefABCDEF" for c in raw) and len(raw) in (32, 48, 64):
        key = bytes.fromhex(raw)
        if len(key) in (16, 24, 32):
            return key
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) in (16, 24, 32):
        return key
    as_text = raw.encode("utf-8")
    if len(as_text) in (16, 24, 32):
        return as_text
    raise CatalogError("询价密钥长度无效")


def object_name(workbench_id: str) -> str:
    """旧整包。页面选号改读 index.json，不再下载这一份。"""
    wid = str(workbench_id or "").strip()
    if _WB.fullmatch(wid) is None:
        raise CatalogError("终端编号无效")
    return f"wb/{wid}/catalog.json"


def index_object_name(workbench_id: str) -> str:
    """总文件。只有工作台和账号，没有微信群，也没有企点通讯录。"""
    wid = str(workbench_id or "").strip()
    if _WB.fullmatch(wid) is None:
        raise CatalogError("终端编号无效")
    return f"wb/{wid}/index.json"


def object_url(endpoint: str, bucket: str, key: str) -> str:
    ep = str(endpoint or "").strip().rstrip("/")
    if ep.startswith("https://"):
        scheme, host = "https", ep[len("https://"):]
    elif ep.startswith("http://"):
        scheme, host = "http", ep[len("http://"):]
    else:
        scheme, host = "https", ep
    host = host.strip("/")
    if _BUCKET.fullmatch(bucket) is None:
        raise CatalogError("桶名无效")
    path = quote(key, safe="/")
    if host.startswith(bucket + "."):
        return f"{scheme}://{host}/{path}"
    return f"{scheme}://{bucket}.{host}/{path}"


def authorization(access_id: str, access_secret: str, date: str, bucket: str, key: str, security_token: str = "") -> str:
    """阿里云 OSS 签名版本 1。只用于 GET。"""
    return request_authorization("GET", access_id, access_secret, date, bucket, key, security_token=security_token)


def _b64_blob(text: str) -> bytes:
    raw = str(text or "").strip()
    pad = "=" * ((-len(raw)) % 4)
    for decoder in (
        lambda: base64.b64decode(raw, validate=True),
        lambda: base64.b64decode(raw + pad, validate=False),
        lambda: base64.urlsafe_b64decode(raw + pad),
    ):
        try:
            blob = decoder()
        except (binascii.Error, ValueError):
            continue
        if blob:
            return blob
    raise CatalogError("询价密封不是有效编码")


def open_envelope(body: bytes, key: bytes) -> Any:
    """解开封套里的询价槽。存款槽不读。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CatalogError("目录对象不是封套") from None
    if not isinstance(data, dict):
        raise CatalogError("目录对象不是封套")
    seals = data.get("seals")
    if not isinstance(seals, dict) or not isinstance(seals.get("xunjia"), str):
        raise CatalogError("封套里没有询价密封")
    blob = _b64_blob(seals["xunjia"])
    try:
        plain = open_sealed(key, blob)
    except ValueError:
        raise CatalogError("询价密封解不开") from None
    try:
        catalog = json.loads(plain.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CatalogError("解开后不是工作台目录") from None
    if not isinstance(catalog, (dict, list)):
        raise CatalogError("解开后不是工作台目录")
    return catalog


def _order(value: Any) -> int:
    """排序号。缺省或写歪了就当作 0，不因此丢掉整份组织架构。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _org(row: dict[str, Any]) -> dict[str, list]:
    """保留这个企点号的组织架构。没有 org，或部门、成员是空数组，都不是错误。"""
    raw = row.get("org")
    if not isinstance(raw, dict):
        return {"departments": [], "members": []}
    departments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("departments") or []:
        if not isinstance(item, dict):
            continue
        did = str(item.get("id") or "").strip()
        if not did or did in seen:
            continue
        seen.add(did)
        departments.append({
            "id": did,
            "parent_id": str(item.get("parent_id") or "").strip(),
            "name": str(item.get("name") or "").strip() or did,
            "order": _order(item.get("order")),
        })
    members: list[dict[str, Any]] = []
    for item in raw.get("members") or []:
        if not isinstance(item, dict):
            continue
        uin = str(item.get("uin") or "").strip()
        if not uin:
            continue
        members.append({
            "uin": uin,
            "name": str(item.get("name") or "").strip() or uin,
            "account": str(item.get("account") or "").strip(),
            "department_id": str(item.get("department_id") or "").strip(),
            "order": _order(item.get("order")),
        })
    return {"departments": departments, "members": members}


def _account(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    if row.get("channel") != "qidian":
        return None
    uid = str(row.get("account_uid") or "").strip()
    if not uid:
        return None
    contacts = []
    for item in row.get("contacts") or []:
        if not isinstance(item, dict):
            continue
        uin = str(item.get("uin") or "").strip()
        if not uin:
            continue
        contacts.append({"uin": uin, "name": str(item.get("name") or uin)})
    groups = []
    for item in row.get("groups") or []:
        if not isinstance(item, dict):
            continue
        native_id = str(item.get("native_id") or "").strip()
        if not native_id:
            continue
        groups.append({"native_id": native_id, "name": str(item.get("name") or native_id)})
    out = {
        "channel": "qidian",
        "account_uid": uid,
        "account_nick": str(row.get("account_nick") or ""),
        "groups": groups,
        "contacts": contacts,
        "org": _org(row),
    }
    book = str(row.get("book") or "").strip()
    if qidian_book_rel(book, uid):
        out["book"] = book
    return out


def _updated(value: Any, now: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp = stamp / 1000.0
        if stamp > 0:
            return stamp
    return now


def rows_from_catalog(catalog: Any, *, workbench_id: str, now: float) -> list[dict[str, Any]]:
    if isinstance(catalog, dict) and isinstance(catalog.get("workbenches"), list):
        books = catalog["workbenches"]
    elif isinstance(catalog, dict):
        books = [catalog]
    elif isinstance(catalog, list):
        books = catalog
    else:
        raise CatalogError("解开后不是工作台目录")
    rows = []
    for book in books:
        if not isinstance(book, dict):
            continue
        wid = str(book.get("workbench_id") or workbench_id or "").strip()
        if _WB.fullmatch(wid) is None:
            continue
        accounts = []
        for item in book.get("accounts") or []:
            if not isinstance(item, dict):
                continue
            acc = _account(item)
            if acc is not None:
                accounts.append(acc)
        if not accounts:
            continue
        rows.append({
            "workbench_id": wid,
            "updated_at": _updated(book.get("updated_at"), now),
            "accounts": accounts,
        })
    return rows


def _settings() -> dict[str, str]:
    return {
        "endpoint": os.environ.get("XUNJIA_OSS_ENDPOINT", os.environ.get("OSS_ENDPOINT", _DEFAULT_ENDPOINT)).strip() or _DEFAULT_ENDPOINT,
        "bucket": os.environ.get("OSS_BUCKET", _DEFAULT_BUCKET).strip() or _DEFAULT_BUCKET,
        "workbench_id": os.environ.get("OSS_WORKBENCH_ID", _DEFAULT_WORKBENCH).strip() or _DEFAULT_WORKBENCH,
        "access_id": os.environ.get("OSS_ACCESS_KEY_ID", "").strip(),
        "access_secret": os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip(),
    }


def _open_request(req: urllib.request.Request, timeout: float):
    """直连。不使用环境里的 HTTP(S)_PROXY。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoCredentialRedirect())
    return opener.open(req, timeout=timeout)


def http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """只读 GET。成功体收全，上限 32MB；截断的半截 JSON 不能当成封套。不走环境代理。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    if req.get_method() != "GET":
        raise CatalogError("下载目录失败")
    cap = 32_000_000
    try:
        with _open_request(req, timeout) as resp:
            chunks: list[bytes] = []
            total = 0
            while total <= cap:
                part = resp.read(1024 * 1024)
                if not part:
                    break
                chunks.append(part)
                total += len(part)
            if total > cap:
                raise CatalogError("目录对象过大")
            return int(resp.status), b"".join(chunks)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(1024)
        except Exception:
            body = b""
        finally:
            exc.close()
        return int(exc.code), body
    except TimeoutError:
        raise CatalogError("下载目录超时") from None
    except urllib.error.URLError:
        raise CatalogError("连不上对象存储") from None


def _status_hint(status: int) -> str:
    if status == 404:
        return "目录对象还不存在"
    if status == 403:
        return "对象存储拒绝读取目录"
    return f"下载目录失败（HTTP {status}）"



_QIDIAN_BOOK = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9_-]*/)?qidian/([A-Za-z0-9][A-Za-z0-9_-]*)/contacts\.json$"
)


def qidian_book_rel(relative: str, account_uid: str) -> bool:
    """只接受当前企点号自己的通讯录对象。微信群和别的号都不算。"""
    match = _QIDIAN_BOOK.fullmatch(str(relative or "").strip())
    return match is not None and match.group(1) == str(account_uid or "").strip()


def qidian_book_object_name(workbench_id: str, relative: str, account_uid: str) -> str:
    if not qidian_book_rel(relative, account_uid):
        raise CatalogError("这不是当前企点号的通讯录")
    wid = str(workbench_id or "").strip()
    if _WB.fullmatch(wid) is None:
        raise CatalogError("终端编号无效")
    return f"wb/{wid}/{str(relative).strip()}"


def load_qidian_book(
    workbench_id: str,
    account_uid: str,
    relative: str,
    *,
    transport: Optional[Transport] = None,
    credentials: STSCredentials | CredentialSet | None = None,
) -> dict[str, Any]:
    """下载当前企点号的通讯录对象。不读微信群，也不读别的号。"""
    key_text = os.environ.get("OSSBOX_KEY_XUNJIA", "").strip()
    if not key_text:
        raise CatalogError("还没有配置公网连接")
    key = decode_seal_key(key_text)
    cfg = _settings()
    name = qidian_book_object_name(workbench_id, relative, account_uid)
    status, body = _get_object(cfg, name, transport, credentials, 60)
    if status == 404:
        raise CatalogError("这个企点号的通讯录还没有上报")
    if status != 200 or not body:
        raise CatalogError(_status_hint(status))
    plain = open_envelope(body, key)
    if not isinstance(plain, dict):
        raise CatalogError("解开后不是企点通讯录")
    if str(plain.get("channel") or "") != "qidian" or str(plain.get("account_uid") or "") != str(account_uid):
        raise CatalogError("通讯录对象和当前企点号对不上")
    if "groups" in plain:
        raise CatalogError("这不是当前企点号的通讯录")
    acc = _account(plain)
    if acc is None:
        raise CatalogError("解开后不是企点通讯录")
    return {"contacts": acc["contacts"], "org": acc["org"]}


def load_live_catalog(
    *,
    transport: Optional[Transport] = None,
    now: Optional[float] = None,
    credentials: STSCredentials | CredentialSet | None = None,
) -> list[dict[str, Any]]:
    """下载并解开 ``wb/{工作台}/index.json``。调用方须已确认询价密钥已装载。

    总文件只有账号列表。微信群和企点通讯录是别的对象，选号不下载它们。
    总文件不存在就告诉页面账号列表还没上报，不读整包 ``catalog.json``。
    """
    import time

    key_text = os.environ.get("OSSBOX_KEY_XUNJIA", "").strip()
    if not key_text:
        raise CatalogError("还没有配置公网连接")
    key = decode_seal_key(key_text)
    cfg = _settings()
    key_name = index_object_name(cfg["workbench_id"])
    status, body = _get_object(cfg, key_name, transport, credentials, 20)
    if status == 404:
        raise CatalogError("工作台账号列表还没上报")
    if status != 200:
        raise CatalogError(_status_hint(status))
    if not body:
        raise CatalogError("目录对象是空的")
    catalog = open_envelope(body, key)
    stamp = time.time() if now is None else now
    rows = rows_from_catalog(catalog, workbench_id=cfg["workbench_id"], now=stamp)
    if not rows:
        raise CatalogError("目录里没有已登录的企点号")
    return rows


def _get_object(cfg: dict[str, str], name: str, transport: Transport | None,
                credentials: STSCredentials | CredentialSet | None, timeout: float) -> tuple[int, bytes]:
    # 旧离线调用方明确传入替身时，仍允许它用原 AK fixture 验证封套。
    # 生产入口没有这个参数；即使设置长期 AK，也只走 STS。
    if (transport is not None and transport is not http_get and credentials is None
            and not os.environ.get("XUNJIA_STS_AUTH_KEY") and not os.environ.get("XUNJIA_STS_SM4_KEY")
            and cfg["access_id"] and cfg["access_secret"]):
        credentials = CredentialSet(cfg["access_id"], cfg["access_secret"], "",
                                    datetime.max.replace(tzinfo=timezone.utc), cfg["bucket"])
    adapter = None if transport is None else lambda method, url, headers, body, budget: transport(url, headers, budget)
    try:
        return OSSHTTP(endpoint=cfg["endpoint"], bucket=cfg["bucket"], credentials=credentials, transport=adapter).request("GET", name, timeout=timeout)
    except CredentialsError as exc:
        raise CatalogError(str(exc)) from None


def catalog_body(rows: list[dict[str, Any]], error: str, *, key_ready: bool) -> dict[str, Any]:
    """GET /api/im/catalog 的响应。失败也带具体原因，不塞假工作台。"""
    data = public_catalog(rows)
    body: dict[str, Any] = {"ok": True, "data": data}
    if error:
        body["hint"] = error
    elif not data["workbenches"] or not any(wb.get("accounts") for wb in data["workbenches"]):
        body["hint"] = (
            "还没有配置公网连接，不能选择工作台"
            if not key_ready
            else "工作台未上报，不能手填企点号"
        )
    return body
