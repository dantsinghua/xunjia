"""监控聊天合并。债券字段为空的消息保留，不因为归不上券就丢掉。

真实企点号的消息只来自已登记的列表，或采集库里该号自己的记录。
没有记录就是空，不生成示例对手、示例债券或示例句子。
正文里出现的债券代码记入该对手的在聊债券和询价明细。
一条消息无论带几只代码，聊天里仍只留一个气泡。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any, Callable, Optional

from .inbound_context import attach_prior_outbound

log = logging.getLogger("xunjia.monitor")
_LOGGED_INBOUND: set[str] = set()

# 银行间 / 交易所代码，例如 032680366.IB、127045.SH。不把价格、期限、日期当成代码。
# 超过 1 分钟还没有企点读库回执（ext_msg_id），这条发出就不算发出。
RECEIPT_WAIT_S = 60

_BOND_CODE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9]{4,20}\.(?:IB|SH|SZ|BJ))(?![A-Za-z0-9])",
    re.IGNORECASE,
)


_CODE_NAME = re.compile(
    r"([0-9A-Za-z]{4,14}\.(?:IB|SH|SZ|BJ))\s+([0-9A-Za-z\u4e00-\u9fff]{2,})",
    re.IGNORECASE,
)
_NAME_LINE = re.compile(r"^(?:MTN|SCP|PPN|PRN|ABN)\d+", re.IGNORECASE)


def _is_code(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9A-Za-z]{4,14}\.(?:IB|SH|SZ|BJ)", value or "", re.IGNORECASE))


def short_names_in(text: str) -> dict[str, str]:
    """从本方报价里取出代码旁边的简称。只用于把页面上的代码换成简称。"""
    raw = text or ""
    found: dict[str, str] = {}
    for match in _CODE_NAME.finditer(raw):
        code = match.group(1)
        code = code[:-3] + code[-3:].upper()
        name = match.group(2)
        if _is_code(name):
            continue
        nxt = re.match(r"\s+([A-Za-z]+\d+)", raw[match.end():])
        if nxt and _NAME_LINE.match(nxt.group(1)):
            name += nxt.group(1)
        found[code.upper()] = name
    return found



def _text_with_short_names(text: str, names: dict[str, str]) -> str:
    """展示时把单独出现的代码换成简称。原文里代码后面已经写了简称的，保持原句。"""
    raw_text = text or ""

    def repl(match: re.Match) -> str:
        raw = match.group(1)
        code = raw[:-3] + raw[-3:].upper()
        name = names.get(code.upper())
        if not name:
            return raw
        if raw_text[match.end():].lstrip().startswith(name):
            return raw
        return name
    return _BOND_CODE.sub(repl, raw_text)


def bond_codes_in(text: str) -> list[str]:
    """按出现顺序取出正文里的债券代码。同一只只留一次。"""
    found: list[str] = []
    seen: set[str] = set()
    for match in _BOND_CODE.finditer(text or ""):
        raw = match.group(1)
        code = raw[:-3] + raw[-3:].upper()
        key = code.upper()
        if key in seen:
            continue
        seen.add(key)
        found.append(code)
    return found


def listed_bonds(row: dict[str, Any]) -> list[str]:
    """显式债券字段优先。字段为空时从正文补代码。不把一条消息拆成多条。"""
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        key = name.upper()
        if not name or key in seen:
            return
        seen.add(key)
        names.append(name)

    for ctx in row.get("contexts") or []:
        if isinstance(ctx, dict):
            add(str(ctx.get("code") or "").strip())
    explicit = str(row.get("bond") or "").strip()
    if explicit:
        parsed = bond_codes_in(explicit)
        if parsed:
            for code in parsed:
                add(code)
        else:
            add(explicit)
    for code in bond_codes_in(str(row.get("text") or "")):
        add(code)
    return names


def blank_bond(msg: dict[str, Any]) -> dict[str, Any]:
    out = dict(msg)
    bond = out.get("bond")
    out["bond"] = "" if bond is None else str(bond)
    return out


def merge_monitor_messages(
    thread_msgs: Optional[list[dict[str, Any]]],
    account_msgs: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """线程里的消息全部保留，再并入该企点号的消息。没有债券字段的不丢弃。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for msg in thread_msgs or []:
        item = blank_bond(msg)
        out.append(item)
        mid = str(item.get("id") or "")
        if mid:
            seen.add(mid)
    for msg in account_msgs or []:
        mid = str(msg.get("id") or "")
        if mid and mid in seen:
            continue
        item = blank_bond(msg)
        out.append(item)
        if mid:
            seen.add(mid)
    return attach_prior_outbound(out)


def inbox_for(account_uid: str) -> list[dict[str, Any]]:
    """只返回已经登记的该号消息。没有登记时才问采集库。两边都没有就是空。"""
    uid = str(account_uid or "").strip()
    if not uid:
        return []
    if uid in _INBOX:
        return attach_prior_outbound(_copy_rows(_INBOX[uid]))
    loader = _LOADER
    if loader is None:
        return []
    try:
        loaded = loader(uid)
    except Exception:
        return []
    return attach_prior_outbound(_copy_rows(loaded or []))


def _copy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    copied = []
    for row in rows:
        item = dict(row)
        if not isinstance(item.get("time"), (int, float)):
            item["time"] = now - 20
        copied.append(item)
    return copied


def remember_messages(account_uid: str, rows: list[dict[str, Any]]) -> None:
    """测试或信箱拉取把某个企点号的消息放进来。空列表表示这个号确实没有消息。"""
    _INBOX[str(account_uid or "").strip()] = [dict(row) for row in rows]


def bound_uid(link: Optional[dict[str, Any]]) -> str:
    """公网上报或邮件中转已选定的企点号。没有绑定则返回空。"""
    if not isinstance(link, dict):
        return ""
    mode = str(link.get("mode") or "")
    if mode not in ("oss", "mail"):
        return ""
    block = link.get(mode) or {}
    if not isinstance(block, dict):
        return ""
    return str(block.get("account_uid") or "").strip()


def _day(ts: Any) -> str:
    if isinstance(ts, (int, float)) and ts > 10_000_000_000:
        ts = ts / 1000.0
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def outbound_label(ext_msg_id: str, ts: Any, now: float) -> tuple[str, bool]:
    """有读库消息号才是已发出。没有回执时，1 分钟内是待回执，超过就是发送失败。"""
    if str(ext_msg_id or "").strip():
        return "发出", True
    age = None
    if isinstance(ts, (int, float)) and ts > 0:
        seconds = float(ts) / 1000.0 if ts > 10_000_000_000 else float(ts)
        age = now - seconds
    if age is not None and age < RECEIPT_WAIT_S:
        return "待回执", False
    return "发送失败", False


def _context_name(row: dict[str, Any], code: str) -> str:
    for ctx in row.get("contexts") or []:
        if not isinstance(ctx, dict):
            continue
        if str(ctx.get("code") or "").upper() != code.upper():
            continue
        name = str(ctx.get("name") or "").strip()
        if name and name.upper() != code.upper():
            return name
    return ""


def _bond_card(name: str, short: str = "") -> dict[str, Any]:
    return {
        "name": short or name,
        "code": name,
        "quote": "询价中",
        "stage": "询价中",
        "step": 1,
        "spread": "—",
        "updated": "",
        "ours": "—",
        "theirs": "—",
        "room": "—",
        "used": 0,
    }


def _detail_row(name: str, peer: str, row: dict[str, Any], code: str = "") -> dict[str, Any]:
    """明细只填正文里已经有的代码。价格、期限、交易员没有就不编。"""
    return {
        "cp_id": peer,
        "date": _day(row.get("time")),
        "name": name,
        "code": code or name,
        "term": "",
        "dir": "",
        "val": "",
        "theirs": "",
        "theirVol": "",
        "ours": "",
        "ourVol": "",
        "spread": "",
        "trader": "",
        "pm": "",
        "st": "询价中",
    }


def _inquiry_bond(row: dict[str, Any], code: str, statuses: Optional[dict[str, str]]) -> bool:
    """当前会话里、并且状态是询价中，才进在聊列表。没读到回执的发出不算。"""
    if row.get("landed") is False or row.get("tag") in ("发送失败", "待回执"):
        return False
    if row.get("landed") is not True and row.get("tag") != "发出" and row.get("dir") != "in":
        return False
    if not statuses:
        return row.get("tag") == "发出" or row.get("landed") is True
    status = statuses.get(code) or statuses.get(code.upper()) or ""
    return status == "询价中"


def _label_bonds(labels):
    out = {}
    if not isinstance(labels, dict):
        return out
    for key, entry in labels.items():
        if isinstance(entry, list):
            out[str(key)] = entry
        elif isinstance(entry, dict):
            out[str(key)] = list(entry.get("bonds") or [])
    return out


def _render_messages(messages, labels):
    labeled = apply_dify_bond_labels(list(messages or []), _label_bonds(labels))
    if not isinstance(labels, dict):
        return labeled
    seen = {str(item.get("id") or "") for item in labeled}
    extra = []
    for item in labeled:
        mid = str(item.get("id") or "")
        entry = labels.get(mid)
        if not isinstance(entry, dict):
            continue
        reply = str(entry.get("reply") or "").strip()
        rid = mid + ":dify"
        if not reply or rid in seen:
            continue
        already = {
            " ".join(str(one.get("text") or "").split())
            for one in labeled
            if one.get("dir") == "out" and one.get("tag") != "机器回复"
        }
        if " ".join(reply.split()) in already:
            continue
        bonds = list(entry.get("bonds") or [])
        if entry.get("dispatched") not in ("uploaded", "already"):
            continue
        extra.append({
            "id": rid,
            "role": "bot",
            "text": reply,
            "tag": "机器回复",
            "landed": True,
            "time": item.get("time"),
            "bond": (bonds[0].get("code") if bonds else "") or "",
            "bond_name": (bonds[0].get("name") if bonds else "") or "",
            "bond_state": "已识别" if bonds else "",
            "contexts": bonds,
            "dir": "out",
            "peer_id": item.get("peer_id") or item.get("native_id"),
            "peer_name": item.get("peer_name"),
            "native_id": item.get("native_id"),
        })
    merged = labeled + extra

    def _when(row):
        try:
            stamp = float(row.get("time") or 0)
        except (TypeError, ValueError):
            stamp = 0.0
        if row.get("tag") == "机器回复":
            stamp += 0.001
        return stamp

    merged.sort(key=_when)
    return merged


def peer_directory(org: dict | None, contacts: list | None = None) -> dict[str, dict[str, str]]:
    """企点号 → 姓名和用户名。组织成员优先，私人好友只补组织里没有的号。"""
    found: dict[str, dict[str, str]] = {}
    for member in (org or {}).get("members") or []:
        if not isinstance(member, dict):
            continue
        uin = str(member.get("uin") or "").strip()
        if not uin:
            continue
        found[uin] = {
            "name": str(member.get("name") or "").strip(),
            "account": str(member.get("account") or "").strip(),
        }
    for person in contacts or []:
        if not isinstance(person, dict):
            continue
        uin = str(person.get("uin") or "").strip()
        if not uin or uin in found:
            continue
        found[uin] = {
            "name": str(person.get("name") or "").strip(),
            "account": str(person.get("account") or "").strip(),
        }
    return found


def apply_peer_directory(messages: list[dict[str, Any]], directory: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """采集库只有号码时，用通讯录补姓名和用户名。已有真实姓名的不覆盖。"""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        peer = str(msg.get("peer_id") or msg.get("native_id") or "").strip()
        hit = directory.get(peer) or {}
        current = str(msg.get("peer_name") or "").strip()
        if hit.get("name") and (not current or current == peer or current.isdigit()):
            msg["peer_name"] = hit["name"]
        if hit.get("account"):
            msg["peer_account"] = hit["account"]
    return messages


def screen_for(
    messages: list[dict[str, Any]],
    statuses: Optional[dict[str, str]] = None,
    labels: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """监控三栏只从传入的消息来。没有消息时对手、债券和统计都是空，不填演示数据。

    在聊债券只留本会话里、状态为询价中、并且对应发出已经有读库回执的代码。
    简称只来自 Dify 出参。工作流回复只有在公网回执成功后才进聊天。
    """
    messages = _render_messages(messages, labels)
    names: dict[str, str] = {}
    for msg in messages:
        names.update(short_names_in(str(msg.get("text") or "")))
    for msg in messages:
        contexts = []
        for ctx in msg.get("contexts") or []:
            if not isinstance(ctx, dict):
                continue
            code = str(ctx.get("code") or "").strip()
            name = str(ctx.get("name") or "").strip()
            if not name or _is_code(name) or name.isdigit():
                name = names.get(code.upper(), "")
            if not name or _is_code(name) or name.isdigit():
                continue
            if not _is_code(code):
                continue
            contexts.append({"code": code, "name": name})
        msg["contexts"] = contexts
        if contexts:
            msg["bond_name"] = contexts[0]["name"]
            msg["bond_state"] = "已识别"
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for msg in messages:
        peer = str(msg.get("peer_id") or msg.get("native_id") or "").strip() or "session"
        if peer not in groups:
            groups[peer] = []
            order.append(peer)
        groups[peer].append(msg)
    cps: list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []
    for peer in order:
        rows = groups[peer]
        name = str(rows[0].get("peer_name") or peer)
        username = str(rows[0].get("peer_account") or "")
        bonds: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            for bond in listed_bonds(row):
                if not _inquiry_bond(row, bond, statuses):
                    continue
                key = bond.upper()
                if key in seen:
                    continue
                seen.add(key)
                short = _context_name(row, bond) or names.get(bond.upper(), "")
                if _is_code(short):
                    short = ""
                bonds.append(_bond_card(bond, short))
                detail.append(_detail_row(short or bond, peer, row, code=bond))
        cps.append({
            "cp_id": peer,
            "id": peer,
            "name": name,
            "username": username,
            "org": "企点",
            "thread_id": peer,
            "channel": "qidian",
            "is_broker": False,
            "bonds": bonds,
        })
    for msg in messages:
        msg["text"] = _text_with_short_names(str(msg.get("text") or ""), names)
    chats = {
        peer: [blank_bond(row) for row in groups[peer]]
        for peer in order
    }
    cards = [
        {"key": "active_dialogs", "label": "活跃对话", "value": len(cps), "unit": "个"},
        {"key": "today_rfq", "label": "消息", "value": len(messages), "unit": "条"},
        {"key": "today_quotes", "label": "已获报价", "value": 0, "unit": "笔", "sub": ""},
        {"key": "avg_reply", "label": "平均回复", "value": 0, "unit": "秒"},
        {"key": "to_human", "label": "转人工", "value": 0, "unit": "次", "sub": ""},
    ]
    return {"counterparties": cps, "chats": chats, "stats": cards, "detail": detail}


def detail_for_cp(screen: dict[str, Any], cp: str = "") -> list[dict[str, Any]]:
    """询价明细按对手过滤。不传对手时返回全部已收集的行。"""
    rows = list((screen or {}).get("detail") or [])
    wanted = str(cp or "").strip()
    if not wanted:
        return rows
    return [row for row in rows if str(row.get("cp_id") or "") == wanted]


def read_collector(db_path: str, account_uid: str) -> list[dict[str, Any]]:
    """只读采集库里这个企点号的消息。库不在、号不在，都返回空。不写库。

    采集库没有债券列，这里不把一条正文拆成多行。代码留在正文里，展示时再收集。
    """
    uid = str(account_uid or "").strip()
    if not uid or not db_path or not os.path.isfile(db_path):
        return []
    uri = "file:" + db_path.replace("?", "%3f") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        cur = con.cursor()
        found = cur.execute(
            "SELECT 1 FROM accounts WHERE channel='qidian' AND self_uid=? LIMIT 1",
            (uid,),
        ).fetchone()
        if found is None:
            return []
        rows = cur.execute(
            """
            SELECT m.ext_msg_id, m.dir, m.text, m.ts_ms, m.is_self, s.native_id, s.name
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            JOIN accounts a ON a.id = m.account_id
            WHERE m.channel='qidian' AND a.self_uid=?
            ORDER BY m.ts_ms, m.id
            """,
            (uid,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    now = time.time()
    for ext_id, direction, text, ts_ms, is_self, native_id, name in rows:
        outgoing = direction == "out" or bool(is_self)
        peer = str(native_id or "").strip() or "session"
        if outgoing:
            tag, landed = outbound_label(str(ext_id or ""), ts_ms, now)
        else:
            tag, landed = "收到", True
        out.append({
            "id": str(ext_id or ""),
            "role": "bot" if outgoing else "peer",
            "text": "" if text is None else str(text),
            "tag": tag,
            "landed": landed,
            "time": (int(ts_ms) / 1000) if isinstance(ts_ms, (int, float)) else None,
            "bond": "",
            "dir": "out" if outgoing else "in",
            "peer_id": peer,
            "peer_name": str(name or "").strip() or peer,
            "native_id": peer,
        })
    rows = attach_prior_outbound(out)
    _note_inbound_without_dify(rows)
    return rows


def collector_loader(db_path: str) -> Callable[[str], list[dict[str, Any]]]:
    def load(account_uid: str) -> list[dict[str, Any]]:
        return read_collector(db_path, account_uid)
    return load


def use_collector(db_path: str) -> None:
    global _LOADER
    _LOADER = collector_loader(db_path)


# 只放调用方登记过的消息。默认没有任何企点号，也不连库。
_INBOX: dict[str, list[dict[str, Any]]] = {}
_LOADER: Optional[Callable[[str], list[dict[str, Any]]]] = None


def _note_inbound_without_dify(rows: list[dict[str, Any]]) -> None:
    """入站回复只记一条跳过原因。不调用 Dify，不写正文。"""
    for row in rows:
        incoming = row.get("dir") == "in" or row.get("tag") == "收到"
        if not incoming:
            continue
        key = "|".join([
            str(row.get("id") or ""),
            str(row.get("peer_id") or row.get("native_id") or ""),
            str(row.get("time") or ""),
            str(len(str(row.get("text") or ""))),
        ])
        if key in _LOGGED_INBOUND:
            continue
        _LOGGED_INBOUND.add(key)
        prior = row.get("prior_outbound_text")
        log.info(
            "询价入站未调用Dify peer=%s text_len=%s prior_len=%s reason=只组装上一条发出不调用Dify",
            row.get("peer_id") or row.get("native_id") or "",
            len(str(row.get("text") or "")),
            len(prior) if isinstance(prior, str) else 0,
        )



_QUOTE_HEAD = re.compile(
    r"(\d+(?:\.\d+)?Y)\s+([A-Za-z0-9]{4,20}\.(?:IB|SH|SZ|BJ))\s+(\S+)",
    re.IGNORECASE,
)
_NAME_SUFFIX = re.compile(r"^(?:MTN|SCP|PPN|PRN|ABN)\d+$", re.IGNORECASE)
_HAN = re.compile(r"[\u4e00-\u9fff]{2,}")


def extract_quote_bonds(text: str) -> list[dict[str, str]]:
    """从一段报价里取出代码和紧跟的简称。MTN/SCP 这类后缀并进简称。"""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    raw = text or ""
    for match in _QUOTE_HEAD.finditer(raw):
        code = match.group(2)
        code = code[:-3] + code[-3:].upper()
        name = match.group(3)
        nxt = re.match(r"\s+(\S+)", raw[match.end():])
        if nxt and _NAME_SUFFIX.match(nxt.group(1)):
            name += nxt.group(1)
        key = code.upper()
        if key in seen:
            continue
        seen.add(key)
        found.append({"code": code, "name": name})
    return found


def _name_keys(name: str) -> list[str]:
    core = re.sub(r"^\d{2}", "", name or "")
    keys = [item.group(0) for item in _HAN.finditer(core)]
    if len(core) >= 2 and core not in keys:
        keys.append(core)
    return [key for key in keys if len(key) >= 2]


def annotate_bond_context(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每段消息标出涉及的债。没有代码时，用简称去对同一会话里已经出现过的债。"""
    catalog: list[dict[str, str]] = []
    known: set[str] = set()
    parsed: list[list[dict[str, str]]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            parsed.append([])
            continue
        bonds = extract_quote_bonds(str(msg.get("text") or ""))
        parsed.append(bonds)
        for bond in bonds:
            key = bond["code"].upper()
            if key in known:
                continue
            known.add(key)
            catalog.append(bond)
    out: list[dict[str, Any]] = []
    for msg, bonds in zip(messages, parsed):
        item = dict(msg) if isinstance(msg, dict) else {}
        if not bonds:
            text = str(item.get("text") or "")
            for bond in reversed(catalog):
                if any(key in text for key in _name_keys(bond["name"])):
                    bonds = [bond]
                    break
        item["contexts"] = bonds
        item["bond"] = bonds[0]["code"] if bonds else ""
        item["bond_name"] = bonds[0]["name"] if bonds else ""
        out.append(item)
    return out


def apply_dify_bond_labels(messages, table=None):
    """债券标记只来自 Dify 出参。还没有结果时标识别中，不从正文拆简称。"""
    src = table or {}
    out = []
    for msg in messages or []:
        item = dict(msg) if isinstance(msg, dict) else {}
        mid = str(item.get("id") or "")
        bonds = list(src.get(mid) or [])
        if bonds:
            item["contexts"] = bonds
            item["bond"] = bonds[0].get("code") or ""
            item["bond_name"] = bonds[0].get("name") or ""
            item["bond_state"] = "已识别"
        else:
            item["contexts"] = []
            item["bond"] = ""
            item["bond_name"] = ""
            age = None
            try:
                stamp = float(item.get("time") or 0)
            except (TypeError, ValueError):
                stamp = 0
            if stamp > 10_000_000_000:
                stamp = stamp / 1000.0
            if stamp > 0:
                age = time.time() - stamp
            fresh = age is not None and age < 60
            item["bond_state"] = "识别中" if str(item.get("text") or "").strip() and fresh else ""
        out.append(item)
    return out
