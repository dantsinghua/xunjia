"""QB 行情入库(MySQL bondtrade_test.qb_live_*) —— 采集器与 mock 后端共用同一张真值表。"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import threading

DB = dict(
    host=os.environ.get("QB_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("QB_MYSQL_PORT", "3306")),
    user=os.environ.get("QB_MYSQL_USER", "bondtrade"),
    password=os.environ.get("QB_MYSQL_PASSWORD", "Bondtrade@2026"),
    database=os.environ.get("QB_MYSQL_DB", "bondtrade_test"),
    charset="utf8mb4", autocommit=True,
)
_DDL = pathlib.Path(__file__).with_name("schema_qb_live.sql")

BEST_QUOTES = "/api/best/quotes/list"
TRADES = "/api/today/trades/stat/list"

Q_COLS = ["capture_time", "quote_key", "quote_uuid", "seq_ns", "bond_uni_code", "broker_id", "tenor",
          "bid_quote_type", "ofr_quote_type", "cnbd_date",
          "remain_days", "maturity_date", "coupon_rate", "issue_amount", "bid_yield", "bid_str",
          "bid_pending", "bid_net_price", "bid_vol", "bid_vol_str", "ofr_yield", "ofr_str", "ofr_pending",
          "ofr_net_price", "ofr_vol", "ofr_vol_str", "cnbd_yield_ex", "cnbd_yield_mat", "cnbd_net_price",
          "cnbd_duration", "csi_yield_ex", "csi_yield_mat", "update_time", "raw_json"]
T_COLS = ["capture_time", "trade_key", "trade_uuid", "seq_ns", "bond_uni_code", "broker_id", "tenor",
          "remain_days", "maturity_date", "trade_time", "trade_yield", "trade_net_price", "chg_bp",
          "cnbd_yield", "vs_cnbd_bp", "deal_type", "deal_flag10", "settle_text", "day_last", "day_high",
          "day_low", "day_count", "raw_json"]


class Store:
    def __init__(self, use_db: bool = True):
        self._lock = threading.RLock()
        self.conn = None
        if use_db:
            import pymysql
            self.conn = pymysql.connect(**DB)
            ddl = _DDL.read_text(encoding="utf-8")
            with self.conn.cursor() as c:
                for s in re.sub(r"^--.*$", "", ddl, flags=re.M).split(";"):
                    if s.strip():
                        c.execute(s)
                # 既有库必须显式迁移；启动不静默 ALTER。
                c.execute("SHOW COLUMNS FROM qb_live_best_quote")
                columns = {row[0] for row in c.fetchall()}
                missing = {"bid_quote_type", "ofr_quote_type", "cnbd_date"} - columns
            if missing:
                self.conn.close()
                self.conn = None
                raise RuntimeError("行情表缺列 " + ", ".join(sorted(missing)) +
                                   "；请核对后显式执行 collector/migrate_quote_truth.sql")

    def _insert(self, table: str, cols: list[str], rows: list[dict]) -> int:
        if not self.conn or not rows:
            return 0
        sql = f"INSERT IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})"
        with self._lock:
            self.conn.ping(reconnect=True)
            self.conn.begin()
            try:
                with self.conn.cursor() as c:
                    count = c.executemany(sql, [[r.get(k) for k in cols] for r in rows])
                self.conn.commit()
                return count
            except BaseException:
                self.conn.rollback()
                raise

    def save(self, path: str, rows: list[dict]) -> int:
        now = dt.datetime.now()
        rows = [dict(r) for r in rows]
        for r in rows:
            r["capture_time"] = now
            r["raw_json"] = json.dumps(r.pop("raw", None), ensure_ascii=False, default=str)
        if path == BEST_QUOTES:
            return self._insert("qb_live_best_quote", Q_COLS, rows)
        return self._insert("qb_live_trade", T_COLS, rows)
