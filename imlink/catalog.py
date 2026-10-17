"""公网信箱目录。第一期用内置假目录，不访问 nyoss.yingmi.com。

对外只给工作台 id、企点号和登录昵称。不返回可编辑备注 label，也不返回密钥。
"""
from __future__ import annotations

import time
from typing import Any, Optional

# 目录超过这个秒数没有更新，视为工作台未上报。
STALE_AFTER_SEC = 300


def _now() -> float:
    return time.time()


def builtin_rows(now: Optional[float] = None) -> list[dict[str, Any]]:
    """假目录。微信账号会被询价接口滤掉，用来表示这台工作台没有企点号。"""
    now = _now() if now is None else now
    return [
        {
            "workbench_id": "wb-nanjing-a",
            "updated_at": now,
            "accounts": [
                {
                    "channel": "qidian",
                    "account_uid": "30088001",
                    "account_nick": "南银理财-现券A",
                    "groups": [],
                    "contacts": [
                        {"uin": "41501", "name": "王磊"},
                        {"uin": "41502", "name": "李静"},
                    ],
                },
                {
                    "channel": "qidian",
                    "account_uid": "30088002",
                    "account_nick": "南银理财-现券B",
                    "groups": [],
                    "contacts": [{"uin": "41503", "name": "赵敏"}],
                    # 本地假回执：这条号放行后回执为失败，便于界面看到失败。不连真实 OSS。
                    "demo_ack": {"ok": False, "code": "failed", "reason": "对端未确认"},
                },
            ],
        },
        {
            "workbench_id": "wb-nanjing-b",
            "updated_at": now,
            "accounts": [
                {
                    "channel": "qidian",
                    "account_uid": "30088003",
                    "account_nick": "询价台-03",
                    "groups": [],
                    "contacts": [{"uin": "41511", "name": "陈昊"}],
                },
            ],
        },
        {
            "workbench_id": "wb-empty",
            "updated_at": now,
            "accounts": [
                {
                    "channel": "wechat",
                    "account_uid": "wxid_demo",
                    "account_nick": "微信示例",
                    "groups": [{"native_id": "g1", "name": "示例群"}],
                },
            ],
        },
        {
            "workbench_id": "wb-silent",
            "updated_at": now - STALE_AFTER_SEC - 60,
            "accounts": [
                {
                    "channel": "qidian",
                    "account_uid": "30088009",
                    "account_nick": "未上报示例",
                    "groups": [],
                    "contacts": [],
                },
            ],
        },
    ]


def qidian_accounts(wb: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in wb.get("accounts") or []:
        if not isinstance(row, dict):
            continue
        if row.get("channel") not in (None, "", "qidian"):
            continue
        uid = str(row.get("account_uid") or "").strip()
        if not uid:
            continue
        out.append(row)
    return out


def public_catalog(rows: list[dict[str, Any]], now: Optional[float] = None) -> dict[str, Any]:
    now = _now() if now is None else now
    workbenches = []
    for wb in rows:
        wid = str(wb.get("workbench_id") or "").strip()
        if not wid:
            continue
        updated = float(wb.get("updated_at") or 0)
        accounts = []
        for row in qidian_accounts(wb):
            accounts.append({
                "account_uid": str(row.get("account_uid") or ""),
                "account_nick": str(row.get("account_nick") or ""),
            })
        workbenches.append({
            "workbench_id": wid,
            "updated_at": updated,
            "stale": (now - updated) > STALE_AFTER_SEC,
            "accounts": accounts,
        })
    return {"workbenches": workbenches}


def find_workbench(rows: list[dict[str, Any]], workbench_id: str) -> Optional[dict[str, Any]]:
    wid = str(workbench_id or "").strip()
    for wb in rows:
        if str(wb.get("workbench_id") or "") == wid:
            return wb
    return None


def find_account(rows: list[dict[str, Any]], workbench_id: str, account_uid: str) -> Optional[dict[str, Any]]:
    wb = find_workbench(rows, workbench_id)
    if wb is None:
        return None
    uid = str(account_uid or "").strip()
    for row in qidian_accounts(wb):
        if str(row.get("account_uid") or "") == uid:
            return row
    return None


def is_stale(wb: dict[str, Any], now: Optional[float] = None) -> bool:
    now = _now() if now is None else now
    updated = float(wb.get("updated_at") or 0)
    return (now - updated) > STALE_AFTER_SEC


def bind_error(link: dict[str, Any], rows: list[dict[str, Any]], others: list[dict[str, Any]]) -> Optional[str]:
    """保存公网上报或邮件中转前的校验。同一企点号不能同时绑两种方式。"""
    mode = link.get("mode")
    if mode not in ("oss", "mail"):
        return None
    if not rows:
        return "工作台未上报，不能保存绑定"
    block = link.get(mode) or {}
    wid = str(block.get("workbench_id") or "").strip()
    uid = str(block.get("account_uid") or "").strip()
    if not wid:
        return "请选择工作台"
    wb = find_workbench(rows, wid)
    if wb is None:
        return "请从目录里选择工作台"
    if not qidian_accounts(wb):
        return "这台工作台上没有企点号，不能保存"
    if not uid:
        return "请选择企点号"
    if find_account(rows, wid, uid) is None:
        return "这个企点号不在所选工作台的目录里"
    me = str(link.get("id") or "")
    for other in others:
        if other.get("mode") not in ("oss", "mail"):
            continue
        if str(other.get("id") or "") == me:
            continue
        other_mode = other.get("mode")
        other_uid = str((other.get(other_mode) or {}).get("account_uid") or "").strip()
        if other_uid and other_uid == uid:
            return "这个企点号已经绑定，不能再绑。公网上报和邮件中转不能同时使用"
    return None


def fill_nick(link: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """登录昵称以目录为准，不采用调用方传来的备注。"""
    mode = link.get("mode")
    if mode not in ("oss", "mail"):
        return link
    out = dict(link)
    block = dict(out.get(mode) or {})
    acc = find_account(rows, str(block.get("workbench_id") or ""), str(block.get("account_uid") or ""))
    if acc is not None:
        block["account_nick"] = str(acc.get("account_nick") or "")
    out[mode] = block
    return out



def book_of(rows: list[dict[str, Any]], workbench_id: str, account_uid: str) -> str:
    """当前企点号自己的通讯录对象相对路径。没有就空字符串。"""
    from .oss_catalog import qidian_book_rel

    acc = find_account(rows, workbench_id, account_uid)
    if not isinstance(acc, dict):
        return ""
    book = str(acc.get("book") or "").strip()
    if qidian_book_rel(book, account_uid):
        return book
    return ""


def contacts_of(rows: list[dict[str, Any]], workbench_id: str, account_uid: str) -> list[dict[str, str]]:
    acc = find_account(rows, workbench_id, account_uid)
    if acc is None:
        return []
    people = []
    for row in acc.get("contacts") or []:
        if not isinstance(row, dict):
            continue
        uin = str(row.get("uin") or "").strip()
        if not uin:
            continue
        people.append({"uin": uin, "name": str(row.get("name") or uin), "remark": ""})
    return people


def _kept_order(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def org_of(rows: list[dict[str, Any]], workbench_id: str, account_uid: str) -> dict[str, Any]:
    """只取这一台工作台、这一个企点号的组织架构。对不上、缺 org、空数组都给空树。"""
    acc = find_account(rows, workbench_id, account_uid)
    org = acc.get("org") if isinstance(acc, dict) else None
    if not isinstance(org, dict):
        return {"departments": [], "members": []}
    departments = []
    for row in org.get("departments") or []:
        if not isinstance(row, dict):
            continue
        did = str(row.get("id") or "").strip()
        if not did:
            continue
        departments.append({
            "id": did,
            "parent_id": str(row.get("parent_id") or ""),
            "name": str(row.get("name") or did),
            "order": _kept_order(row.get("order")),
        })
    members = []
    for row in org.get("members") or []:
        if not isinstance(row, dict):
            continue
        uin = str(row.get("uin") or "").strip()
        if not uin:
            continue
        members.append({
            "uin": uin,
            "name": str(row.get("name") or uin),
            "account": str(row.get("account") or ""),
            "department_id": str(row.get("department_id") or ""),
            "order": _kept_order(row.get("order")),
        })
    return {"departments": departments, "members": members}


def probe_link(link: dict[str, Any], rows: list[dict[str, Any]], *, key_ready: bool) -> dict[str, Any]:
    """只查目录和密钥是否配置。不发网络请求，明细里不出现密钥。"""
    steps: list[dict[str, Any]] = []
    block = link.get("oss") or {}
    wid = str(block.get("workbench_id") or "").strip()
    uid = str(block.get("account_uid") or "").strip()

    def finish(passed: bool, note: str, account=None, contacts=None) -> dict[str, Any]:
        return {
            "passed": passed,
            "steps": steps,
            "accounts": [account] if account else [],
            "account": account,
            "contacts": contacts or [],
            "contacts_source": "catalog" if contacts else "",
            "note": note,
            "adopted_account_id": "",
        }

    if not rows:
        steps.append({"name": "目录", "ok": False, "detail": "无目录"})
        return finish(False, "无目录")
    wb = find_workbench(rows, wid) if wid else None
    if wb is None:
        steps.append({"name": "目录", "ok": False, "detail": "无目录"})
        return finish(False, "无目录")
    if is_stale(wb):
        steps.append({"name": "目录", "ok": False, "detail": "工作台未上报"})
        return finish(False, "工作台未上报")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(wb.get("updated_at") or 0)))
    steps.append({"name": "目录", "ok": True, "detail": f"最近一次目录时间 {stamp}"})
    acc = find_account(rows, wid, uid)
    if acc is None:
        steps.append({"name": "企点号", "ok": False, "detail": "号已下线"})
        return finish(False, "号已下线")
    nick = str(acc.get("account_nick") or "")
    shown = f"{nick} · {uid}" if nick else uid
    steps.append({"name": "企点号", "ok": True, "detail": shown})
    if not key_ready:
        steps.append({"name": "密钥", "ok": False, "detail": "密钥缺失"})
        return finish(False, "密钥缺失")
    steps.append({"name": "密钥", "ok": True, "detail": "已配置"})
    account = {
        "id": uid,
        "label": nick,
        "account_nick": nick,
        "self_uid": uid,
        "state": "online",
    }
    contacts = contacts_of(rows, wid, uid)
    steps.append({"name": "通讯录", "ok": True, "detail": f"通讯录 {len(contacts)} 人"})
    return finish(True, "", account, contacts)


def status_of(link: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    mode = link.get("mode") if link.get("mode") in ("oss", "mail") else "oss"
    block = link.get(mode) or {}
    wid = str(block.get("workbench_id") or "").strip()
    uid = str(block.get("account_uid") or "").strip()
    empty = {"present": False, "stale": True, "updated_at": None, "account_nick": "", "demo_ack": None}
    if not rows or not wid:
        return empty
    wb = find_workbench(rows, wid)
    if wb is None:
        return empty
    acc = find_account(rows, wid, uid)
    demo = acc.get("demo_ack") if isinstance(acc, dict) else None
    return {
        "present": acc is not None,
        "stale": is_stale(wb),
        "updated_at": float(wb.get("updated_at") or 0),
        "account_nick": str((acc or {}).get("account_nick") or ""),
        "demo_ack": demo if isinstance(demo, dict) else None,
    }
