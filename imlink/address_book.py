"""询价通讯录单独落在 data/address_book.json。

只保留企点私人好友和组织架构。不写微信群、群号、存款绑定或邮件群配置。
换一个企点号只更新这一条，其它号留在文件里。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

# 这些键一旦出现就说明把别的业务写进来了，落盘前直接丢掉。
_FORBIDDEN = {
    "groups",
    "group",
    "group_native_ids",
    "group_native_id",
    "wx_groups",
    "wechat",
    "wechat_groups",
    "imap",
    "mail",
    "deposit",
    "bindings",
}


def empty_org() -> dict[str, list]:
    return {"departments": [], "members": []}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _order(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _contacts(rows: Any) -> list[dict[str, str]]:
    people = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        uin = _text(row.get("uin"))
        if not uin:
            continue
        people.append({
            "uin": uin,
            "name": _text(row.get("name")) or uin,
            "remark": _text(row.get("remark")),
        })
    return people


def _org(raw: Any) -> dict[str, list]:
    if not isinstance(raw, dict):
        return empty_org()
    departments = []
    seen: set[str] = set()
    for row in raw.get("departments") or []:
        if not isinstance(row, dict):
            continue
        did = _text(row.get("id"))
        if not did or did in seen:
            continue
        seen.add(did)
        departments.append({
            "id": did,
            "parent_id": _text(row.get("parent_id")),
            "name": _text(row.get("name")) or did,
            "order": _order(row.get("order")),
        })
    members = []
    for row in raw.get("members") or []:
        if not isinstance(row, dict):
            continue
        uin = _text(row.get("uin"))
        if not uin:
            continue
        members.append({
            "uin": uin,
            "name": _text(row.get("name")) or uin,
            "account": _text(row.get("account")),
            "department_id": _text(row.get("department_id")),
            "order": _order(row.get("order")),
        })
    return {"departments": departments, "members": members}


def _entry(workbench_id: str, account_uid: str, contacts: Any, org: Any) -> dict[str, Any]:
    return {
        "workbench_id": workbench_id,
        "account_uid": account_uid,
        "contacts": _contacts(contacts),
        "org": _org(org),
    }


def _without_forbidden(value: Any) -> Any:
    """递归去掉微信群、存款、邮件配置。组织成员只留契约字段。"""
    if isinstance(value, dict):
        return {key: _without_forbidden(item) for key, item in value.items() if key not in _FORBIDDEN}
    if isinstance(value, list):
        return [_without_forbidden(item) for item in value]
    return value


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"entries": []}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"entries": []}
    if not isinstance(data, dict):
        return {"entries": []}
    entries = []
    for row in data.get("entries") or []:
        if not isinstance(row, dict):
            continue
        wid = _text(row.get("workbench_id"))
        uid = _text(row.get("account_uid"))
        if not wid or not uid:
            continue
        entries.append(_entry(wid, uid, row.get("contacts"), row.get("org")))
    return {"entries": entries}


def _write(path: Path, data: dict[str, Any]) -> None:
    body = _without_forbidden({"entries": data.get("entries") or []})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def save_entry(path: Path, workbench_id: str, account_uid: str, contacts: Any, org: Any) -> dict[str, Any]:
    """写入当前工作台的当前企点号，并读回这一条。其它号保持原样。"""
    wid = _text(workbench_id)
    uid = _text(account_uid)
    if not wid or not uid:
        raise ValueError("通讯录必须带工作台和企点号")
    fresh = _entry(wid, uid, contacts, org)
    with _LOCK:
        data = _read(path)
        entries = []
        replaced = False
        for row in data["entries"]:
            if row["workbench_id"] == wid and row["account_uid"] == uid:
                entries.append(fresh)
                replaced = True
            else:
                entries.append(row)
        if not replaced:
            entries.append(fresh)
        _write(path, {"entries": entries})
        saved = _read(path)
    for row in saved["entries"]:
        if row["workbench_id"] == wid and row["account_uid"] == uid:
            return {"contacts": row["contacts"], "org": row["org"]}
    return {"contacts": [], "org": empty_org()}


def load_entry(path: Path, workbench_id: str, account_uid: str) -> dict[str, Any]:
    wid = _text(workbench_id)
    uid = _text(account_uid)
    with _LOCK:
        data = _read(path)
    for row in data["entries"]:
        if row["workbench_id"] == wid and row["account_uid"] == uid:
            return {"contacts": row["contacts"], "org": row["org"]}
    return {"contacts": [], "org": empty_org()}
