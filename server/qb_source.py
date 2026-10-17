"""意向询价行情的真实数据源:QB(羿珩 DM)经纪商实时报价 → 本服务的 quote 管线。

启用:显式配置 XUNJIA_QUOTE_SOURCE=qb 与独立数据源参数；不根据本机凭据自动启用。

数据来路(不在本进程里):浏览器 DM 页面 + 油猴旁路 → bondtrade 的 qb_ingest → MySQL
  bondtrade_test.qb_live_best_quote  每个「经纪商×券」每次变动一行(seq_ns 为行版本)
  bondtrade_test.qb_bond_dict        bondUniCode → 代码 / 简称
  bondtrade_test.qb_broker           经纪商 id → 名称(未配置时显示「经纪商N」)
本模块每 POLL_S 秒按自增 id 增量读新行,在内存里维护每券各经纪商的最新报价,
重算该券「最优 bid / 最优 ofr」,写入 server/market_cache 的 quote_flow / quote_latest 拿 flow_id,
再交给网关 Hub 按订阅组成批推送 —— 与合成生产端走同一条下游链路(契约 docs/09)。

最优口径(收益率报价,与 DM 最优条一致):最优 bid = 各经纪商 bid 收益率最低者(买方出价最高);
最优 ofr = 各经纪商 ofr 收益率最高者(卖方要价最低)。同价的量相加(DM 的 6K/3K),
报价方只列报在这个价上且量大于 0 的中介,量 0 或「--」不计入、也不挂到另一侧。
行身份 bond_uni_code = QB 的 bondUniCode(字符串),windcode 取字典里的代码,
没有真实代码和简称的券不进入列表。字典后来补上简称后,再作为新行出现。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
import time
from functools import wraps

import pymysql

from server import market_cache as db

POLL_S = 0.5

# 内部意向(本方底价/量),按 bondUniCode。行情之外的本方业务数据:由 /api/instruction/floor 等写入,
# 出行/推送时 join 进 ib/ibv/io/iov;DIRTY 标记需立即推送(即使最优行情未变)的券。
INTENT: dict[int, dict] = {}
DIRTY: set[int] = set()
MYSQL = dict(
    host=os.environ.get("QB_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("QB_MYSQL_PORT", "3306")),
    user=os.environ.get("QB_MYSQL_USER", "bondtrade"),
    password=os.environ.get("QB_MYSQL_PASSWORD", ""),
    database=os.environ.get("QB_MYSQL_DB", "bondtrade_test"),
    charset="utf8mb4", autocommit=True,
)
# 回放:XUNJIA_QB_REPLAY=<倍速>,把今天已录下的真实报价按原时间顺序加速重放(午休/收盘后压测用),
# 走与实时完全相同的下游链路;写单独的缓存库,不污染实时那份。
REPLAY = float(os.environ.get("XUNJIA_QB_REPLAY", "0") or 0)
DB_PATH = os.environ.get("XUNJIA_QUOTE_CACHE", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "quotes_qb_replay.db" if REPLAY else "quotes_qb.db"))

_COLS = ("id", "bond_uni_code", "broker_id", "seq_ns", "tenor", "remain_days", "coupon_rate",
         "bid_quote_type", "ofr_quote_type", "bid_net_price", "ofr_net_price", "cnbd_date",
         "bid_yield", "bid_vol", "bid_pending", "bid_vol_str",
         "ofr_yield", "ofr_vol", "ofr_pending", "ofr_vol_str",
         "cnbd_yield_mat", "cnbd_net_price", "cnbd_duration", "update_time", "capture_time")


_POLICY = ("国开", "农发", "进出", "口行")


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


def guess_type(code: str, name: str) -> str:
    """QB 行里没有券种,按简称/代码粗判,仅供筛选分组用(与 FILTER_GROUPS 的类型 chip 对齐)。"""
    n = name or ""
    if "国债" in n:
        return "国债"
    if any(k in n for k in _POLICY):
        return "政金债"
    if any(k in n for k in ("二级资本", "永续", "资本债", "TLAC")):
        return "次级债"
    if "ABN" in n or "ABS" in n or "优先" in n or "次级" in n:
        return "ABS"
    if "PPN" in n:
        return "PPN"
    if "SCP" in n or "CP" in n:
        return "短融"
    if "MTN" in n:
        return "中票"
    if "NCD" in n or "存单" in n:
        return "存单"
    if n.endswith("债") or "债" in n[-4:]:
        return "地方债"
    if (code or "").endswith((".SH", ".SZ")):
        return "公司债"
    if "企业债" in n or n[:2].isdigit() and "债" in n:
        return "企业债"
    return "其他"


class QBSource:
    def __init__(self):
        self._lock = threading.RLock()
        self.book_day: dt.date | None = None
        self._wall_day = dt.date.today()
        self._restored = False
        self.conn: pymysql.connections.Connection | None = None
        self.last_id = 0
        self.book: dict[int, dict[int, dict]] = {}     # uni -> broker -> 最新行
        self.dict: dict[int, tuple[str, str]] = {}     # uni -> (code, name)
        self.brokers: dict[int, str] = {}
        self.static_done: dict[int, bool] = {}         # uni -> 静态行是否已带字典信息
        self.last_best: dict[int, tuple] = {}          # uni -> 上次推送的最优(没变就不推)
        self.code2uni: dict[str, int] = {}
        self._dict_ts = 0.0
        self.stats = {"rows_read": 0, "flows": 0, "polls": 0, "errors": 0, "last_error": None,
                      "bonds": 0, "lag_ms": None}

    # ------------------------------------------------------------ MySQL
    def _c(self):
        if self.conn is None:
            self.conn = pymysql.connect(**MYSQL)
        else:
            self.conn.ping(reconnect=True)
        return self.conn

    def _refresh_dict(self, force=False):
        if not force and time.time() - self._dict_ts < 10:
            return
        with self._c().cursor() as c:
            c.execute("SELECT bond_uni_code, bond_code, bond_short_name FROM qb_bond_dict")
            self.dict = {int(u): (code or "—", name or "") for u, code, name in c.fetchall()}
            c.execute("SELECT broker_id, broker_name FROM qb_broker")
            self.brokers = {int(b): n for b, n in c.fetchall() if n}
        self.code2uni = {code: u for u, (code, _) in self.dict.items() if code and code != "—"}
        self._dict_ts = time.time()

    def _fetch(self, limit=5000) -> list[dict]:
        if REPLAY:
            return self._fetch_replay(limit)
        return self._fetch_live(limit)

    def _fetch_replay(self, limit: int) -> list[dict]:
        if not hasattr(self, "_rp"):
            today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            with self._c().cursor() as c:
                c.execute(f"SELECT {','.join(_COLS)} FROM qb_live_best_quote WHERE update_time >= %s "
                          "ORDER BY update_time, id", (today,))
                self._rp = [dict(zip(_COLS, r)) for r in c.fetchall()]
            # 录制有空档(浏览器没开/午休):把 >5s 的空档压成 1s,回放不空转
            shift = dt.timedelta(0)
            for i in range(1, len(self._rp)):
                gap = self._rp[i]["update_time"] - shift - self._rp[i - 1]["update_time"]
                if gap > dt.timedelta(seconds=5):
                    shift += gap - dt.timedelta(seconds=1)
                self._rp[i]["update_time"] -= shift
            self._rp_i = 0
            self._rp_t0 = time.time()
            self._rp_d0 = self._rp[0]["update_time"] if self._rp else dt.datetime.now()
            self.stats["replay"] = {"speed": REPLAY, "total": len(self._rp)}
        if self._rp_i >= len(self._rp) and self._rp:
            self._rp_i, self._rp_t0 = 0, time.time()
            self.book.clear()
            self.last_best.clear()
        clock = self._rp_d0 + dt.timedelta(seconds=(time.time() - self._rp_t0) * REPLAY)
        out = []
        while self._rp_i < len(self._rp) and len(out) < limit and self._rp[self._rp_i]["update_time"] <= clock:
            r = dict(self._rp[self._rp_i])
            r["update_time"] = dt.datetime.now()        # 重放时刻当作报价时刻,延迟统计才有意义
            out.append(r)
            self._rp_i += 1
        self.stats["replay"]["pos"] = self._rp_i
        self.stats["replay"]["clock"] = clock.strftime("%H:%M:%S")
        return out

    def _fetch_live(self, limit=5000) -> list[dict]:
        today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with self._c().cursor() as c:
            c.execute(f"SELECT {','.join(_COLS)} FROM qb_live_best_quote "
                      "WHERE id > %s AND update_time >= %s ORDER BY id LIMIT %s",
                      (self.last_id, today, limit))
            rows = [dict(zip(_COLS, r)) for r in c.fetchall()]
        return rows

    # ------------------------------------------------------------ 计算
    def broker_name(self, bid: int) -> str:
        return self.brokers.get(bid) or f"经纪商{bid}"

    @staticmethod
    def _f(v):
        return None if v is None else float(v)

    def _best_side(self, rows, yield_key: str, vol_key: str, pick_max: bool):
        """同一侧:先取最优收益率,再把报在这个价上的量加总。

        量必须和报价方是同一侧的。中诚买 2000@1.7775、卖 1.7700 且量为「--」时,
        2000 只能进 bid,不能因为同券有 1.7775 的卖价就把中诚和 2000 安到 ofr 上。
        多家同价(中诚/国利/信唐各 2000)时量是 6000,报价方把这些中介都列上。
        """
        parsed = []
        for r in rows:
            kind = yield_key.replace("yield", "quote_type")
            if kind in r and r[kind] not in (1, 3):
                continue
            y = r.get(yield_key)
            if y is None:
                continue
            y = float(y)
            if not math.isfinite(y):
                continue
            v = r.get(vol_key)
            parsed.append((round(y, 4), 0.0 if not v else float(v), r))
        if not parsed:
            return None
        target = max(p[0] for p in parsed) if pick_max else min(p[0] for p in parsed)
        at = [p for p in parsed if p[0] == target]
        sized = [p for p in at if p[1] > 0]
        # 这个价上没有大于 0 的量时，保留价格，报价方留空。
        named = sized
        named.sort(key=lambda p: (
            -p[1],
            -(p[2]["update_time"].timestamp() if p[2].get("update_time") else 0),
            p[2]["broker_id"],
        ))
        names = []
        for _, _, r in named:
            n = self.broker_name(r["broker_id"])
            if n not in names:
                names.append(n)
        vol = int(round(sum(p[1] for p in sized))) if sized else None
        row = named[0][2] if named else at[0][2]
        return {"yield": target, "vol": vol, "side": "/".join(names), "row": row}

    def _best_of(self, rows: list[dict]) -> dict | None:
        bb = self._best_side(rows, "bid_yield", "bid_vol", pick_max=False)
        bo = self._best_side(rows, "ofr_yield", "ofr_vol", pick_max=True)
        if bb is None and bo is None:
            return None
        vol_str = ((bb or {}).get("row") or {}).get("bid_vol_str") or ((bo or {}).get("row") or {}).get("ofr_vol_str") or ""
        pending = ((bb and bb["row"].get("bid_pending")) or (bo and bo["row"].get("ofr_pending")))
        times = [r["update_time"] for r in rows if r.get("update_time")]
        issue = max(times) if times else None
        return {
            "best_bid": bb["yield"] if bb else None,
            "best_bid_vol": bb["vol"] if bb else None,
            "best_bid_side": bb["side"] if bb else None,
            "best_ofr": bo["yield"] if bo else None,
            "best_ofr_vol": bo["vol"] if bo else None,
            "best_ofr_side": bo["side"] if bo else None,
            "firmness": "请示盘" if pending else "实盘",
            "clearing": "+0" if "+0" in vol_str and "明天" not in vol_str else "+1",
            "issue_time": int(issue.timestamp() * 1000) if issue else int(time.time() * 1000),
        }

    def _best(self, uni: int) -> dict | None:
        return self._best_of(list(self.book.get(uni, {}).values()))

    def broker_choices(self) -> list[dict]:
        """下拉固定六家。名称优先用库里的对照，没有则用标准名。"""
        from server.quote_channel import BROKERS
        return [{"id": i, "name": self.brokers.get(i) or name} for i, name in BROKERS]

    def _matches_groups(self, uni: int, clearing: str, groups: list[str]) -> bool:
        wanted = [g for g in groups if g and g != "bond-all"]
        if not wanted:
            return True
        static = self.static_row(uni)
        if isinstance(static.get("tags"), str):
            static = {**static, "tags": []}
        owned = set(db.static_groups(static)) | {db.clearing_group(clearing)}
        by_dim: dict[str, list[str]] = {}
        for g in wanted:
            dim = g.split("_", 1)[0]
            by_dim.setdefault(dim, []).append(g)
        return all(any(opt in owned for opt in opts) for opts in by_dim.values())

    @_locked
    def page_one_broker(self, broker_id: int, before_ms: int, before_uni: int,
                        limit: int, groups: list[str]) -> tuple[list[dict], int, bool, int, int]:
        """只看一家经纪商。买卖价、量、报价方都是这一家，不是跨经纪商最优。"""
        items: list[tuple[int, int, dict]] = []
        for uni, by_broker in self.book.items():
            row = by_broker.get(broker_id)
            if row is None or not self.named(uni):
                continue
            best = self._best_of([row])
            if not best:
                continue
            if not self._matches_groups(uni, best.get("clearing") or "+1", groups):
                continue
            items.append((int(best["issue_time"]), int(uni), best))
        items.sort(key=lambda x: (x[0], x[1]), reverse=True)
        total = len(items)
        if before_ms:
            items = [x for x in items if x[0] < before_ms or (x[0] == before_ms and x[1] < before_uni)]
        has_more = bool(limit) and len(items) > limit
        picked = items[:limit] if limit else items
        out = []
        for _issue, uni, best in picked:
            static = self.static_row(uni)
            tags = static.get("tags")
            if isinstance(tags, str):
                try:
                    static = {**static, "tags": json.loads(tags)}
                except json.JSONDecodeError:
                    static = {**static, "tags": []}
            out.append({"static": static, "best": best, "uni": uni})
        next_ms = picked[-1][0] if picked else 0
        next_uni = picked[-1][1] if picked else 0
        return out, total, has_more, next_ms, next_uni

    def named(self, uni: int) -> tuple[str, str] | None:
        """有真实市场代码和简称才返回。空名、占位名、UNI 代码都不算。"""
        code, name = self.dict.get(uni, ("", ""))
        code = (code or "").strip()
        name = (name or "").strip()
        if not code or code in ("—", "-") or code.startswith("UNI"):
            return None
        if not name or name.startswith("未入字典"):
            return None
        return code, name

    @_locked
    def static_row(self, uni: int) -> dict:
        any_row = max(self.book.get(uni, {}).values(),
                      key=lambda r: (r.get("update_time") or dt.datetime.min, r.get("seq_ns") or 0), default={})
        named = self.named(uni)
        code, name = named if named else ("", "")
        return {
            "bond_uni_code": str(uni), "windcode": code, "sec_name": name,
            "bond_type": guess_type(code, name), "residual_maturity": any_row.get("tenor"),
            "residual_days": any_row.get("remain_days"),
            "coupon": self._f(any_row.get("coupon_rate")),
            "bond_rating": None, "issuer_rating": None, "implied_rating": None,
            "issuer": None, "nature": None, "industry": None, "prov": None,
            "is_public": None, "is_guaranteed": None, "is_subordinated": None,
            "is_perpetual": None, "is_udic": None, "embedded_option": None,
            "tags": json.dumps([], ensure_ascii=False),
            "val_yield": self._f(any_row.get("cnbd_yield_mat")),
            "net_price": self._f(any_row.get("cnbd_net_price")),
            "mod_dur": self._f(any_row.get("cnbd_duration")),
            "val_date": any_row.get("cnbd_date").isoformat() if isinstance(any_row.get("cnbd_date"), dt.date) else any_row.get("cnbd_date"),
        }

    def _upsert_static(self, rows: list[dict]):
        if not rows:
            return
        with db.transaction() as c:
            c.executemany(
                f"INSERT OR REPLACE INTO bond_static ({','.join(db.STATIC_COLS)}) "
                f"VALUES ({','.join('?' * len(db.STATIC_COLS))})",
                [tuple(r.get(k) for k in db.STATIC_COLS) for r in rows])

    # ------------------------------------------------------------ 一轮
    def _restore_checkpoint(self):
        if self._restored or REPLAY:
            return
        with db._lock:
            row = db.conn().execute("SELECT payload FROM qb_source_checkpoint WHERE singleton=1").fetchone()
        if row:
            state = json.loads(row[0])
            self.last_id = state["last_id"]
            self.book_day = dt.date.fromisoformat(state["book_day"]) if state["book_day"] else None
            self._wall_day = dt.date.fromisoformat(state["wall_day"])
            self.book = {int(u): {int(b): r for b, r in brokers.items()} for u, brokers in state["book"].items()}
            for brokers in self.book.values():
                for r in brokers.values():
                    for key in ("update_time", "capture_time"):
                        if r.get(key):
                            r[key] = dt.datetime.fromisoformat(r[key])
            self.static_done = {int(u): done for u, done in state["static_done"].items()}
            self.last_best = {int(u): tuple(key) for u, key in state["last_best"].items()}
        self._restored = True

    @_locked
    def poll(self) -> list[tuple[int, dict]]:
        self._restore_checkpoint()
        # 读者也持同一把锁；失败时恢复缓存，成功后才对其它线程可见。
        saved = dict(self.__dict__)
        self.book = {u: dict(brokers) for u, brokers in self.book.items()}
        self.static_done = dict(self.static_done)
        self.last_best = dict(self.last_best)
        force = set(DIRTY)
        # 领取本批标记；写入过程中产生的新标记不能在提交时一并清除。
        DIRTY.difference_update(force)
        try:
            # 取源数据不占 SQLite 锁；MySQL 连接由 source 锁串行化。
            self._refresh_dict()
            rows = self._fetch()
            with db.transaction() as c:
                out = self._poll_rows(rows, force)
                if rows:
                    self.last_id = max(self.last_id, max(r["id"] for r in rows))
                if not REPLAY and (rows or out or self._wall_day != saved["_wall_day"]):
                    payload = {k: getattr(self, k) for k in ("last_id", "book_day", "book", "static_done", "last_best")}
                    payload["wall_day"] = self._wall_day
                    def encode(value):
                        if isinstance(value, (dt.datetime, dt.date)):
                            return value.isoformat()
                        return float(value)  # MySQL Decimal
                    c.execute("INSERT INTO qb_source_checkpoint VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
                              (json.dumps(payload, default=encode),))
        except BaseException:
            DIRTY.update(force)
            self.__dict__.clear()
            self.__dict__.update(saved)
            # COMMIT 后连接故障可能无法确认结果；下一轮以持久水位裁决，避免重复流水。
            self._restored = False
            raise
        return out

    def _poll_rows(self, rows, force) -> list[tuple[int, dict]]:
        """更新簿并持久化本批，返回 [(flow_id, quote)] 供上层推送。"""
        self.stats["polls"] += 1
        if not getattr(self, "_purged_unnamed", False):
            self.stats["dropped_unnamed"] = db.drop_unnamed()
            self._purged_unnamed = True
        self.stats["rows_read"] += len(rows)
        if rows and not REPLAY:   # 入库 → 本轮读到 的滞后(诊断延迟用)
            now = dt.datetime.now()
            lags = sorted((now - r["capture_time"]).total_seconds() * 1000 for r in rows if r.get("capture_time"))
            if lags:
                self._lags = (getattr(self, "_lags", []) + lags)[-2000:]
                sl = sorted(self._lags)
                self.stats["read_lag_ms"] = {"p50": int(sl[len(sl) // 2]), "p95": int(sl[int(len(sl) * .95) - 1]), "n": len(sl)}
        touched: set[int] = set()
        touched |= force
        today = dt.date.today()
        row_days = [r["update_time"].date() for r in rows if r.get("update_time")]
        next_day = max(row_days, default=self.book_day)
        if today > self._wall_day:
            next_day = max(next_day or today, today)
        if next_day and (self.book_day is None or next_day > self.book_day):
            touched.update(self.book)
            self.book = {}
            self.book_day = next_day
        self._wall_day = today
        cap: dict[int, int] = {}          # uni -> 本轮触发它的行的入库时刻(ms),用于端到端延迟埋点
        for r in rows:
            if self.book_day and (not r.get("update_time") or r["update_time"].date() < self.book_day):
                continue
            u = int(r["bond_uni_code"])
            if r.get("capture_time") and not REPLAY:
                cap[u] = max(cap.get(u, 0), int(r["capture_time"].timestamp() * 1000))
            b = self.book.setdefault(u, {})
            cur = b.get(r["broker_id"])
            if cur is None or (r.get("seq_ns") or 0) > (cur.get("seq_ns") or 0):
                b[r["broker_id"]] = r
                touched.add(u)
        # 字典新到的券:补静态行
        for u, done in list(self.static_done.items()):
            if not done and self.named(u):
                touched.add(u)
                self.last_best.pop(u, None)   # 强制出一条新 flow,前端 headSync 才会把补齐简称的行换上
        statics, quotes = [], []
        for u in touched:
            if not self.named(u):
                self.static_done[u] = False
                self.last_best.pop(u, None)
                continue
            statics.append(self.static_row(u))
            self.static_done[u] = True
            best = self._best(u)
            it = INTENT.get(u, {})
            if best is None:
                times = [r["update_time"] for r in self.book.get(u, {}).values() if r.get("update_time")]
                issue = max(times) if times else dt.datetime.now()
                best = {k: None for k in db.DYN_COLS if k.startswith("best_") or k == "firmness"}
                best.update(clearing="+1", issue_time=int(issue.timestamp() * 1000))
            key = tuple(best.get(k) for k in db.DYN_COLS) + (best["issue_time"], it.get("ib"), it.get("ibv"), it.get("io"), it.get("iov"))
            if u not in force and self.last_best.get(u) == key:
                continue
            self.last_best[u] = key
            q = {"bond_uni_code": str(u), "group_name": db.clearing_group(best.get("clearing", "+1")),
                 "status": "待询价", "ib": it.get("ib"), "ibv": it.get("ibv"),
                 "io": it.get("io"), "iov": it.get("iov"), **best, "cap_ms": cap.get(u)}
            quotes.append(q)
        self._upsert_static(statics)
        out = []
        if quotes:
            ids = db.upsert_quotes(quotes)
            now = time.time() * 1000
            self.stats["lag_ms"] = int(now - max(q["issue_time"] for q in quotes))
            for fid, q in zip(ids, quotes):
                out.append((fid, q))
            self.stats["flows"] += len(ids)
        self.stats["bonds"] = len(self.book)
        return out

    # ------------------------------------------------------------ 详情
    @_locked
    def detail(self, key: str) -> dict | None:
        """/api/bond/{windcode} 的 QB 版:key 可以是代码或 bondUniCode。"""
        u = self.code2uni.get(key)
        if u is None:
            try:
                u = int(key[3:] if key.startswith("UNI") else key)
            except ValueError:
                return None
        if u not in self.book:
            return None
        quotes = []
        for bid, r in sorted(self.book[u].items()):
            best = self._best_of([r]) or {}
            quotes.append({
                "channel": self.broker_name(bid),
                "best_bid": best.get("best_bid"), "best_bid_vol": best.get("best_bid_vol"),
                "best_bid_side": best.get("best_bid_side"),
                "best_ofr": best.get("best_ofr"), "best_ofr_vol": best.get("best_ofr_vol"),
                "best_ofr_side": best.get("best_ofr_side"),
                "firmness": best.get("firmness"),
                "clearing": best.get("clearing"),
                "updated_at": r["update_time"].timestamp() if r["update_time"] else None,
                "bid_vol_str": r["bid_vol_str"], "ofr_vol_str": r["ofr_vol_str"],
                "bid_quote_type": r.get("bid_quote_type"), "ofr_quote_type": r.get("ofr_quote_type"),
                "bid_net_price": self._f(r.get("bid_net_price")), "ofr_net_price": self._f(r.get("ofr_net_price")),
            })
        trades, intra_bid, intra_ofr, intra_val = [], [], [], []
        today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with self._c().cursor() as c:
            c.execute("SELECT trade_time, broker_id, deal_type, trade_yield FROM qb_live_trade "
                      "WHERE bond_uni_code=%s AND trade_time >= %s ORDER BY trade_time DESC LIMIT 50", (u, today))
            for t, bid, dtp, y in c.fetchall():
                trades.append({"time": t.timestamp(), "channel": self.broker_name(bid),
                               "mark": {0: "GVN", 1: "TKN"}.get(dtp, "TRD"),   # deal_type 含义待核
                               "yield": self._f(y), "vol_wan": None})
            # 盘中:该券各经纪商报价流水(今日),按时间取 bid/ofr 与中债估值
            c.execute("SELECT update_time, bid_yield, ofr_yield, cnbd_yield_mat, bid_quote_type, ofr_quote_type FROM qb_live_best_quote "
                      "WHERE bond_uni_code=%s AND update_time >= %s ORDER BY update_time LIMIT 2000", (u, today))
            for t, by, oy, vy, bt, ot in c.fetchall():
                ts = t.timestamp()
                if by is not None and bt in (1, 3):
                    intra_bid.append({"t": ts, "y": float(by)})
                if oy is not None and ot in (1, 3):
                    intra_ofr.append({"t": ts, "y": float(oy)})
                if vy is not None:
                    intra_val.append({"t": ts, "y": float(vy)})
        return {"uni": u, "quotes": quotes, "trades": trades,
                "intra": {"bid": intra_bid, "ofr": intra_ofr, "val": intra_val},
                "hist": {"cnbd_val": [], "avg_trade": []}}
