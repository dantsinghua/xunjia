"""从现有行情库取一只债的最后一笔价。

不另建价格表。先读整张 quote_latest（每只债当前最后一笔），
没有这张表时再按 issue_time 取 quote_flow 的最后一笔。
当天有记录就是当天最新价，当天没有就是上一次有记录的价。
不读取、不返回行情行上的状态字段。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Optional


def local_midnight_ms(now: Optional[datetime] = None) -> int:
    current = now or datetime.now()
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _cell(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _num(value: Any) -> Any:
    if value is None or value == "":
        return None
    return value


def _val(yield_: Any, price: Any) -> str:
    if yield_ is None or price is None:
        return "—"
    try:
        return f"{float(yield_):.4f}/{float(price):.2f}"
    except (TypeError, ValueError):
        return "—"


def _public(static: dict[str, Any], flow: Any, today_start_ms: int) -> dict[str, Any]:
    issue = int(_cell(flow, "issue_time") or 0)
    return {
        "name": str(static.get("name") or ""),
        "term": str(static.get("term") or "—"),
        "val": _val(static.get("val_yield"), static.get("net_price")),
        "ob": _num(_cell(flow, "best_bid")),
        "obv": _num(_cell(flow, "best_bid_vol")),
        "obs": str(_cell(flow, "best_bid_side") or "—"),
        "oo": _num(_cell(flow, "best_ofr")),
        "oov": _num(_cell(flow, "best_ofr_vol")),
        "oos": str(_cell(flow, "best_ofr_side") or "—"),
        "ib": _num(_cell(flow, "ib")),
        "ibv": _num(_cell(flow, "ibv")),
        "io": _num(_cell(flow, "io")),
        "iov": _num(_cell(flow, "iov")),
        "issueTime": issue,
        "day": "today" if issue >= today_start_ms else "previous",
    }


def last_quotes(conn: sqlite3.Connection, codes: list[Any], today_start_ms: int) -> dict[str, dict[str, Any]]:
    """按债券代码取行情库最后一笔。当天有记录用当天，否则用更早的最后一笔。"""
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = str(raw or "").strip()
        if code and code not in seen:
            seen.add(code)
            wanted.append(code)
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    static_rows = conn.execute(
        f"""SELECT bond_uni_code, windcode, sec_name, residual_maturity, val_yield, net_price
            FROM bond_static WHERE windcode IN ({marks})""",
        wanted,
    ).fetchall()
    found: dict[str, dict[str, Any]] = {}
    for row in static_rows:
        code = str(_cell(row, "windcode") or "").strip()
        uni = str(_cell(row, "bond_uni_code") or "").strip()
        if not code or not uni:
            continue
        found[code] = {
            "uni": uni,
            "name": _cell(row, "sec_name") or "",
            "term": _cell(row, "residual_maturity") or "—",
            "val_yield": _cell(row, "val_yield"),
            "net_price": _cell(row, "net_price"),
        }
    out: dict[str, dict[str, Any]] = {}
    cols = """issue_time, best_bid, best_bid_vol, best_bid_side,
              best_ofr, best_ofr_vol, best_ofr_side, ib, ibv, io, iov"""
    unis = [item["uni"] for item in found.values()]
    latest_by_uni: dict[str, Any] = {}
    has_latest = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quote_latest'"
    ).fetchone()
    if has_latest and unis:
        marks = ",".join("?" * len(unis))
        rows = conn.execute(
            f"""SELECT bond_uni_code, {cols}
                FROM quote_latest WHERE bond_uni_code IN ({marks})""",
            unis,
        ).fetchall()
        for row in rows:
            uni = str(_cell(row, "bond_uni_code") or "").strip()
            if uni:
                latest_by_uni[uni] = row
    flow_sql = f"""SELECT {cols}
                   FROM quote_flow WHERE bond_uni_code=?
                   ORDER BY issue_time DESC, flow_id DESC LIMIT 1"""
    for code, static in found.items():
        flow = latest_by_uni.get(static["uni"])
        if flow is None:
            try:
                flow = conn.execute(flow_sql, (static["uni"],)).fetchone()
            except sqlite3.OperationalError:
                flow = None
        if flow is None:
            continue
        out[code] = _public(static, flow, today_start_ms)
    return out


def quotes_for_status(codes: list[Any]) -> dict[str, dict[str, Any]]:
    """读当前进程已经打开的行情库。库还没初始化时返回空，不新建价格表。"""
    try:
        from server import market_cache as db
        connection = db.conn()
    except Exception:
        return {}
    try:
        return last_quotes(connection, codes, local_midnight_ms())
    except Exception:
        return {}
