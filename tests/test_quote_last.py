"""最后一笔价来自行情库，不另造价格。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from imlink.quote_last import last_quotes

DAY = datetime(2026, 10, 12, 9, 0, 0)
TODAY = int(datetime(2026, 10, 12, 0, 0, 0).timestamp() * 1000)
YESTERDAY = int(datetime(2026, 10, 11, 17, 0, 0).timestamp() * 1000)
TODAY_LATE = int(datetime(2026, 10, 12, 15, 30, 0).timestamp() * 1000)
TODAY_EARLY = int(datetime(2026, 10, 12, 9, 15, 0).timestamp() * 1000)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE bond_static (
          bond_uni_code TEXT PRIMARY KEY,
          windcode TEXT,
          sec_name TEXT,
          residual_maturity TEXT,
          val_yield REAL,
          net_price REAL
        );
        CREATE TABLE quote_flow (
          flow_id INTEGER PRIMARY KEY,
          bond_uni_code TEXT,
          issue_time INTEGER,
          best_bid REAL, best_bid_vol INTEGER, best_bid_side TEXT,
          best_ofr REAL, best_ofr_vol INTEGER, best_ofr_side TEXT,
          ib REAL, ibv INTEGER, io REAL, iov INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO bond_static VALUES (?,?,?,?,?,?)",
        ("A", "A.IB", "甲债", "1.20Y", 2.50, 100.1),
    )
    conn.execute(
        "INSERT INTO bond_static VALUES (?,?,?,?,?,?)",
        ("B", "B.IB", "乙债", "2.00Y", 2.20, 99.5),
    )
    # A 只有 10-11 17:00 的 2.59 ofr
    conn.execute(
        "INSERT INTO quote_flow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "A", YESTERDAY, 2.57, 1000, "经纪商", 2.59, 2000, "经纪商", None, None, None, None),
    )
    # B 先有昨天 2.10，今天又有 2.40，最后一笔才是当天最新
    conn.execute(
        "INSERT INTO quote_flow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, "B", YESTERDAY, 2.00, 1000, "经纪商", 2.10, 1000, "经纪商", None, None, None, None),
    )
    conn.execute(
        "INSERT INTO quote_flow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (3, "B", TODAY_EARLY, 2.20, 1000, "经纪商", 2.30, 1000, "经纪商", None, None, None, None),
    )
    conn.execute(
        "INSERT INTO quote_flow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (4, "B", TODAY_LATE, 2.35, 3000, "经纪商", 2.40, 3000, "经纪商", None, None, None, None),
    )
    return conn


def test_previous_day_price_when_no_quote_today_and_today_latest_when_present():
    got = last_quotes(_db(), ["A.IB", "B.IB", "MISSING.IB"], TODAY)
    assert got["A.IB"]["oo"] == 2.59
    assert got["A.IB"]["day"] == "previous"
    assert got["A.IB"]["name"] == "甲债"
    assert got["A.IB"]["issueTime"] == YESTERDAY
    assert got["B.IB"]["oo"] == 2.40
    assert got["B.IB"]["ob"] == 2.35
    assert got["B.IB"]["day"] == "today"
    assert got["B.IB"]["issueTime"] == TODAY_LATE
    assert "MISSING.IB" not in got
    assert "status" not in got["A.IB"]
    assert DAY.day == 12


def test_full_latest_table_is_today_price_when_page_flow_is_older():
    """整张最新表才是当天最新价。已加载页上的旧流水不能盖过它。"""
    conn = _db()
    conn.execute(
        """
        CREATE TABLE quote_latest (
          bond_uni_code TEXT,
          flow_id INTEGER,
          issue_time INTEGER,
          best_bid REAL, best_bid_vol INTEGER, best_bid_side TEXT,
          best_ofr REAL, best_ofr_vol INTEGER, best_ofr_side TEXT,
          ib REAL, ibv INTEGER, io REAL, iov INTEGER
        )
        """
    )
    newer = TODAY_LATE + 60_000
    conn.execute(
        "INSERT INTO quote_latest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("B", 9, newer, 2.50, 1000, "经纪商", 2.55, 1000, "经纪商", None, None, None, None),
    )
    conn.execute(
        "INSERT INTO quote_latest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("A", 1, YESTERDAY, 2.57, 1000, "经纪商", 2.59, 2000, "经纪商", None, None, None, None),
    )
    got = last_quotes(conn, ["A.IB", "B.IB"], TODAY)
    assert got["B.IB"]["oo"] == 2.55
    assert got["B.IB"]["ob"] == 2.50
    assert got["B.IB"]["day"] == "today"
    assert got["B.IB"]["issueTime"] == newer
    assert got["A.IB"]["oo"] == 2.59
    assert got["A.IB"]["day"] == "previous"
