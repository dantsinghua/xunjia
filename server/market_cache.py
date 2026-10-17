"""实时行情管线 · SQLite 落地层(契约 docs/09 §1 / §3)。

三张表:
  bond_static  静态主数据,只读,worker 端整表缓存
  quote_flow   报价流水,flow_id 自增 = 行版本号
  quote_latest 每券最新一条的物化视图,列表首屏/翻页走它

为什么用 SQLite 而不是继续全内存:契约要求 flow_id 是"全局单调、重启不回退"的行版本,
内存计数器一重启就归零,前端 last-write-wins 会被旧版本号骗过去;AUTOINCREMENT 天然满足。
生产替换成 MySQL 时表结构同构,本模块只需换驱动。

线程模型:FastAPI 同步路由跑在线程池,生产端跑在事件循环(经 to_thread 写库),
所以这里用单连接 + RLock 串行化,而不是每次开连接——量级(每秒几十条)远不到需要连接池。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from itertools import count
from typing import Iterable

DB_PATH = os.environ.get("XUNJIA_QUOTE_CACHE", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "quotes.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qb_source_checkpoint (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bond_static (
  bond_uni_code TEXT PRIMARY KEY,
  windcode TEXT NOT NULL,
  sec_name TEXT NOT NULL,
  bond_type TEXT, residual_maturity TEXT, residual_days INTEGER,
  coupon REAL, bond_rating TEXT, issuer_rating TEXT, implied_rating TEXT,
  issuer TEXT, nature TEXT, industry TEXT, prov TEXT,
  is_public INTEGER, is_guaranteed INTEGER, is_subordinated INTEGER,
  is_perpetual INTEGER, is_udic INTEGER, embedded_option TEXT,
  tags TEXT,
  val_yield REAL, net_price REAL, mod_dur REAL, val_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_static_windcode ON bond_static(windcode);
CREATE INDEX IF NOT EXISTS idx_static_type   ON bond_static(bond_type);
CREATE INDEX IF NOT EXISTS idx_static_rating ON bond_static(bond_rating);
CREATE INDEX IF NOT EXISTS idx_static_prov   ON bond_static(prov);

CREATE TABLE IF NOT EXISTS quote_flow (
  flow_id INTEGER PRIMARY KEY AUTOINCREMENT,
  bond_uni_code TEXT NOT NULL,
  group_name TEXT NOT NULL,
  issue_time INTEGER NOT NULL,
  best_bid REAL, best_bid_vol INTEGER, best_bid_side TEXT,
  best_ofr REAL, best_ofr_vol INTEGER, best_ofr_side TEXT,
  firmness TEXT, clearing TEXT, status TEXT,
  ib REAL, ibv INTEGER, io REAL, iov INTEGER
);
CREATE INDEX IF NOT EXISTS idx_flow_group ON quote_flow(group_name, flow_id DESC);
CREATE INDEX IF NOT EXISTS idx_flow_bond  ON quote_flow(bond_uni_code, flow_id DESC);

CREATE TABLE IF NOT EXISTS quote_latest (
  bond_uni_code TEXT PRIMARY KEY,
  flow_id INTEGER NOT NULL,
  group_name TEXT NOT NULL, issue_time INTEGER NOT NULL,
  best_bid REAL, best_bid_vol INTEGER, best_bid_side TEXT,
  best_ofr REAL, best_ofr_vol INTEGER, best_ofr_side TEXT,
  firmness TEXT, clearing TEXT, status TEXT,
  ib REAL, ibv INTEGER, io REAL, iov INTEGER
);
CREATE INDEX IF NOT EXISTS idx_latest_page ON quote_latest(flow_id DESC,bond_uni_code DESC);
CREATE INDEX IF NOT EXISTS idx_latest_group ON quote_latest(group_name, flow_id DESC);
"""

STATIC_COLS = (
    "bond_uni_code", "windcode", "sec_name",
    "bond_type", "residual_maturity", "residual_days",
    "coupon", "bond_rating", "issuer_rating", "implied_rating",
    "issuer", "nature", "industry", "prov",
    "is_public", "is_guaranteed", "is_subordinated", "is_perpetual", "is_udic", "embedded_option",
    "tags", "val_yield", "net_price", "mod_dur", "val_date",
)
# 推送只带这些(契约 §2.2 QuoteDelta 的可选字段),顺序与建表一致
DYN_COLS = (
    "best_bid", "best_bid_vol", "best_bid_side",
    "best_ofr", "best_ofr_vol", "best_ofr_side",
    "firmness", "clearing", "status",
    "ib", "ibv", "io", "iov",
)
FLOW_COLS = ("bond_uni_code", "group_name", "issue_time") + DYN_COLS

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()
_transactions = count()


@contextmanager
def transaction():
    """串行事务；嵌套使用保存点，让静态、流水和消费水位一起提交。"""
    with _lock:
        c = conn()
        nested = c.in_transaction
        marker = f"market_{next(_transactions)}"
        c.execute(f"SAVEPOINT {marker}" if nested else "BEGIN")
        try:
            yield c
            c.execute(f"RELEASE {marker}" if nested else "COMMIT")
        except BaseException:
            if c.in_transaction:
                if nested:
                    c.execute(f"ROLLBACK TO {marker}")
                    c.execute(f"RELEASE {marker}")
                else:
                    c.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- 订阅组(契约 §3)
# 剩余期限桶,按 residual_days 半开区间划分;SQL 谓词与 Python 端必须用同一张表,
# 否则 REST 翻页和 WS 推送对同一只券会给出不同的组归属。
TENOR_BUCKETS: dict[str, tuple[int | None, int | None]] = {
    "lt1": (None, 365), "1to3": (365, 1095), "3to5": (1095, 1825),
    "5to7": (1825, 2555), "7to10": (2555, 3650), "gt10": (3650, None),
}


def tenor_bucket(days: int | None) -> str:
    d = days or 0
    for name, (lo, hi) in TENOR_BUCKETS.items():
        if (lo is None or d >= lo) and (hi is None or d < hi):
            return name
    return "gt10"


def clearing_group(clearing: str | None) -> str:
    """清算沿用展示口径 '+0/+1/+N',组名按契约用 T0/T1/TN。"""
    return {"+0": "bond-clearing_T0", "+1": "bond-clearing_T1"}.get(clearing or "", "bond-clearing_TN")


def static_groups(s: dict) -> frozenset[str]:
    """一只券由静态属性决定的组:all / 券种 / 评级 / 期限 / 区域。
    种子里有契约枚举之外的值(券种"其他"、评级"无评级"/"A"、区域"其他"),
    不做丢弃而是原样落成 bond-xxx_其他,让前端能订到全部数据;枚举差异见交付报告。"""
    return frozenset({
        "bond-all",
        f"bond-type_{s.get('bond_type') or '其他'}",
        f"bond-rating_{s.get('bond_rating') or '无评级'}",
        f"bond-tenor_{tenor_bucket(s.get('residual_days'))}",
        f"bond-prov_{s.get('prov') or '其他'}",
    })


def groups_of(static_row: dict, clearing: str | None) -> frozenset[str]:
    """一条流水所属的全部组 = 券的静态组 ∪ 该笔报价的清算组。

    取舍(多组归属):契约 §1 里 quote_flow.group_name 是单值列,而一只券天然同时属于
    六个维度的组。这里**不**建 6 倍行数的成员表,而是:
      - group_name 只存"主组" = 清算组 bond-clearing_*。选它是因为六个维度里只有清算
        由报价本身决定,其余五个都是券的静态属性,匹配时 join bond_static 即可推出;
      - 网关 / REST 按订阅匹配时用本函数(或 _group_predicate 的 SQL 等价物)算全集。
    代价:idx_latest_group 只对清算维度有索引价值,静态维度走 bond_static 自己的索引;
    在 400 只券的量级下无感,生产若要按组高频扫表再换成员表,协议不受影响。
    """
    return static_groups(static_row) | {clearing_group(clearing)}


def _group_predicate(group: str) -> tuple[str, list] | None:
    """把一个组名翻成 SQL 谓词(l = quote_latest, s = bond_static)。未知组返回 None。"""
    if group == "bond-all":
        return "1=1", []
    if "_" not in group:
        return None
    dim, val = group.split("_", 1)
    if dim == "bond-type":
        return "s.bond_type = ?", [val]
    if dim == "bond-rating":
        return "s.bond_rating = ?", [val]
    if dim == "bond-prov":
        return "s.prov = ?", [val]
    if dim == "bond-clearing":
        return "l.group_name = ?", [group]
    if dim == "bond-tenor" and val in TENOR_BUCKETS:
        lo, hi = TENOR_BUCKETS[val]
        parts, params = [], []
        if lo is not None:
            parts.append("COALESCE(s.residual_days,0) >= ?"); params.append(lo)
        if hi is not None:
            parts.append("COALESCE(s.residual_days,0) < ?"); params.append(hi)
        return "(" + " AND ".join(parts) + ")", params
    return None


def _groups_where(groups: Iterable[str]) -> tuple[str, list]:
    """把组名集合翻成 SQL WHERE。

    语义:**同一维度内取 OR,跨维度取 AND**。
    例:{bond-type_中票, bond-type_短融, bond-tenor_1to3}
       → (券种=中票 OR 券种=短融) AND (剩余期限∈[1,3))

    为什么不是全并集:WS 订阅天然是并集(订了哪些组就收哪些组),但筛选 UI 要的是交集
    ——用户选「中票 + 1~3Y」期望的是"又是中票又是 1~3Y",不是"中票的 或 1~3Y 的"。
    两边语义不同是本来面目:订阅负责"把流收窄到超集",列表/前端再按完整谓词收紧。
    `bond-all` 是恒真,和任何维度 AND 都不改变结果。
    """
    by_dim: dict[str, list[tuple[str, list]]] = {}
    for g in groups:
        pr = _group_predicate(g)
        if not pr:
            continue
        dim = "all" if g == "bond-all" else g.split("_", 1)[0]
        by_dim.setdefault(dim, []).append(pr)

    if not by_dim:
        return "0", []  # 全是未知组:什么都不匹配,而不是退化成全量

    and_parts, params = [], []
    for dim, prs in by_dim.items():
        if dim == "all":
            continue  # 恒真,不必进 WHERE
        ors = " OR ".join(x[0] for x in prs)
        and_parts.append("(" + ors + ")")
        for x in prs:
            params.extend(x[1])
    if not and_parts:
        return "1=1", []
    return " AND ".join(and_parts), params


def _has_tag(raw, name, tag):
    try:
        tags = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        tags = []
    return tag in str(name or '') or isinstance(tags, list) and all(isinstance(value, str) for value in tags) and tag in tags


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    from .market import number, maturity_years
    c.create_function('xj_number', 1, number, deterministic=True)
    c.create_function('xj_years', 2, maturity_years, deterministic=True)
    c.create_function('xj_lower', 1, lambda value: str(value or '').lower(), deterministic=True)
    c.create_function('xj_has_tag', 3, _has_tag, deterministic=True)
    # WAL:生产端持续写、REST 并发读,不能让读把写卡住
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db 未初始化,先调 init_db()")
    return _conn


def init_db(static_rows: list[dict] | None = None, path: str = DB_PATH) -> sqlite3.Connection:
    """建表 + 灌静态主数据。幂等:表已有数据就跳过,重复调用不会重复灌。

    static_rows 每项按 STATIC_COLS 取值(tags 可给 list,这里转 JSON)。
    """
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect(path)
        c = _conn
        c.executescript(_SCHEMA)
        if static_rows:
            n = c.execute("SELECT COUNT(*) FROM bond_static").fetchone()[0]
            if n == 0:
                rows = []
                for s in static_rows:
                    r = dict(s)
                    if isinstance(r.get("tags"), (list, tuple)):
                        r["tags"] = json.dumps(r["tags"], ensure_ascii=False)
                    for k in ("is_public", "is_guaranteed", "is_subordinated", "is_perpetual", "is_udic"):
                        r[k] = None if r.get(k) is None else int(bool(r[k]))
                    rows.append(tuple(r.get(k) for k in STATIC_COLS))
                with transaction():
                    c.executemany(
                        f"INSERT INTO bond_static ({','.join(STATIC_COLS)}) VALUES ({','.join('?' * len(STATIC_COLS))})",
                        rows,
                    )
        return c


# ---------------------------------------------------------------- 写
_INSERT_FLOW = f"INSERT INTO quote_flow ({','.join(FLOW_COLS)}) VALUES ({','.join('?' * len(FLOW_COLS))})"
_LATEST_COLS = ("bond_uni_code", "flow_id", "group_name", "issue_time") + DYN_COLS
_UPSERT_LATEST = (
    f"INSERT INTO quote_latest ({','.join(_LATEST_COLS)}) VALUES ({','.join('?' * len(_LATEST_COLS))}) "
    "ON CONFLICT(bond_uni_code) DO UPDATE SET "
    + ", ".join(f"{k}=excluded.{k}" for k in _LATEST_COLS[1:])
    # 守卫:只允许版本号前进。生产端并发/重放时旧流水绝不能盖掉新的
    + " WHERE excluded.flow_id > quote_latest.flow_id"
)


def _write_one(c: sqlite3.Connection, delta: dict) -> int:
    cur = c.execute(_INSERT_FLOW, tuple(delta.get(k) for k in FLOW_COLS))
    fid = cur.lastrowid
    c.execute(_UPSERT_LATEST, (delta["bond_uni_code"], fid, delta["group_name"], delta["issue_time"])
              + tuple(delta.get(k) for k in DYN_COLS))
    return fid


def upsert_quote(delta: dict) -> int:
    """写一条流水 + 刷新 quote_latest,返回自增 flow_id。delta 键见 FLOW_COLS。"""
    with transaction() as c:
        fid = _write_one(c, delta)
    return fid


def upsert_quotes(deltas: list[dict]) -> list[int]:
    """批量版:一个事务写一整批,生产端每个 tick 只进一次锁。"""
    if not deltas:
        return []
    with transaction() as c:
        ids = [_write_one(c, d) for d in deltas]
    return ids


# ---------------------------------------------------------------- 读
def _static_dict(row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in STATIC_COLS}
    try:
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    except Exception:
        d["tags"] = []
    for k in ("is_public", "is_guaranteed", "is_subordinated", "is_perpetual", "is_udic"):
        d[k] = None if d[k] is None else bool(d[k])
    return d


def static_all() -> list[dict]:
    with _lock:
        rows = conn().execute(f"SELECT {','.join(STATIC_COLS)} FROM bond_static ORDER BY bond_uni_code").fetchall()
    return [_static_dict(r) for r in rows]


def static_by_codes(codes: Iterable[str]) -> list[dict]:
    codes = [c for c in codes if c]
    if not codes:
        return []
    out = []
    with _lock:
        # SQLite 单条语句绑定参数上限 999(旧版),分片查
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            rows = conn().execute(
                f"SELECT {','.join(STATIC_COLS)} FROM bond_static WHERE bond_uni_code IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
            out.extend(_static_dict(r) for r in rows)
    return out


def drop_unnamed() -> int:
    """删掉没有真实简称的占位券。列表不再出现「未入字典」和 UNI 代码。"""
    with transaction() as c:
        ids = [r[0] for r in c.execute(
            """SELECT bond_uni_code FROM bond_static
               WHERE sec_name IS NULL OR TRIM(sec_name) = ''
                  OR sec_name LIKE '未入字典%'
                  OR windcode IS NULL OR TRIM(windcode) = ''
                  OR windcode IN ('—', '-')
                  OR windcode LIKE 'UNI%'"""
        ).fetchall()]
        if not ids:
            return 0
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            c.execute(f"DELETE FROM quote_latest WHERE bond_uni_code IN ({marks})", chunk)
            c.execute(f"DELETE FROM quote_flow WHERE bond_uni_code IN ({marks})", chunk)
            c.execute(f"DELETE FROM bond_static WHERE bond_uni_code IN ({marks})", chunk)
        return len(ids)


def latest_by_groups(groups: Iterable[str], before: int = 0, limit: int = 0) -> tuple[list[dict], int]:
    """quote_latest ⋈ bond_static,按 flow_id 倒序做 keyset 翻页。

    返回 (rows, total):rows 每项 = {"latest": {...}, "static": {...}};
    before>0 只取 flow_id < before;limit=0 表示不限。total 是命中组的券数(≤ 券总数)。
    """
    groups = list(groups) or ["bond-all"]
    where, params = _groups_where(groups)
    sql_from = (
        "FROM quote_latest l JOIN bond_static s ON s.bond_uni_code = l.bond_uni_code WHERE " + where
        + " AND TRIM(IFNULL(s.sec_name,'')) != '' AND s.sec_name NOT LIKE '未入字典%'"
        + " AND TRIM(IFNULL(s.windcode,'')) != '' AND s.windcode NOT IN ('—', '-')"
        + " AND s.windcode NOT LIKE 'UNI%'"
    )
    with _lock:
        c = conn()
        total = c.execute("SELECT COUNT(*) " + sql_from, params).fetchone()[0]
        q = (f"SELECT {','.join('l.' + k for k in _LATEST_COLS)}, "
             f"{','.join('s.' + k for k in STATIC_COLS[1:])} " + sql_from)
        p = list(params)
        if before:
            q += " AND l.flow_id < ?"; p.append(before)
        q += " ORDER BY l.flow_id DESC"
        if limit:
            q += " LIMIT ?"; p.append(limit)
        rows = c.execute(q, p).fetchall()
    out = []
    for r in rows:
        latest = {k: r[k] for k in _LATEST_COLS}
        out.append({"latest": latest, "static": _static_dict(r)})
    return out, total


def counts() -> dict:
    """自测 / dev 用:各表行数与 flow_id 高水位。"""
    with _lock:
        c = conn()
        return {
            "bond_static": c.execute("SELECT COUNT(*) FROM bond_static").fetchone()[0],
            "quote_flow": c.execute("SELECT COUNT(*) FROM quote_flow").fetchone()[0],
            "quote_latest": c.execute("SELECT COUNT(*) FROM quote_latest").fetchone()[0],
            "max_flow_id": c.execute("SELECT COALESCE(MAX(flow_id),0) FROM quote_flow").fetchone()[0],
        }


# Public-query path. Private cache columns are deliberately absent here.
_PUBLIC_LATEST_COLS = ('bond_uni_code', 'flow_id', 'issue_time', 'best_bid', 'best_bid_vol',
                       'best_bid_side', 'best_ofr', 'best_ofr_vol', 'best_ofr_side', 'firmness', 'clearing')


def _filter_where(f: dict, statuses: dict) -> tuple[str, list]:
    """Compile the full public FilterSpec; values never become SQL identifiers.

    The small scalar functions registered on the cache connection preserve the
    Python contract for Unicode case folding, finite numbers and textual tenors.
    Filtering happens before LIMIT, without hydrating public rows in Python.
    """
    from .market import CHIPS
    from .domain import decimal
    parts = ["TRIM(IFNULL(s.sec_name,'')) != ''", "s.sec_name NOT LIKE '未入字典%'",
             "TRIM(IFNULL(s.windcode,'')) != ''", "s.windcode NOT IN ('—', '-')", "s.windcode NOT LIKE 'UNI%'"]
    params = []
    keyword = f['keyword'].strip().lower()
    if keyword:
        parts.append("(instr(xj_lower(s.windcode),?) > 0 OR instr(xj_lower(s.sec_name),?) > 0 OR instr(xj_lower(s.issuer),?) > 0)")
        params.extend([keyword] * 3)
    for name, field in [('报价类型', 'l.firmness'), ('清算', 'l.clearing'), ('类型', 's.bond_type')]:
        index = f['chips'].get(name, 0)
        if index:
            parts.append(field + ' = ?'); params.append(CHIPS[name][index])
    # Scheme is not supplied by the public quote source, just as in projection.
    if f['chips'].get('方案', 0):
        parts.append('0')
    index = f['chips'].get('当前状态', 0)
    if index:
        label = CHIPS['当前状态'][index]
        if label == '待询价':
            codes = [code for code, status in statuses.items() if status != label]
            parts.append('s.windcode NOT IN (SELECT value FROM json_each(?))')
        else:
            codes = [code for code, status in statuses.items() if status == label]
            parts.append('s.windcode IN (SELECT value FROM json_each(?))')
        params.append(json.dumps(codes))
    index = f['chips'].get('标签', 0)
    if index:
        tag = CHIPS['标签'][index]
        aliases = {'绿色债': ['绿色债', '绿'], '科创债': ['科创债', '科创']}.get(tag, [tag])
        parts.append('(' + ' OR '.join('xj_has_tag(s.tags,s.sec_name,?)' for _ in aliases) + ')')
        params.extend(aliases)
    years = 'xj_years(s.residual_days,s.residual_maturity)'
    intervals = [(0, None), (0, 1), (1, 3), (3, 5), (5, 7), (7, 10), (10, None)]

    def tenor(lo, hi):
        if hi is None:
            return years + ' >= ?', [lo]
        return '(' + years + ' >= ? AND ' + years + ' < ?)', [lo, hi]

    index = f['chips'].get('期限', 0)
    if index:
        clause, values = tenor(*intervals[index]); parts.append(clause); params.extend(values)
    dimensions = {}
    for group in f['groups']:
        if group == 'bond-all':
            continue
        dim, _, value = group.partition('_')
        field = {'bond-type': 's.bond_type', 'bond-rating': 's.bond_rating', 'bond-prov': 's.prov'}.get(dim)
        clause, values = '0', []
        if field:
            clause, values = field + ' = ?', [value]
        elif dim == 'bond-clearing':
            clearing = {'T0': '+0', 'T1': '+1', 'TN': '+N'}.get(value)
            clause, values = ('0', []) if clearing is None else ('l.clearing = ?', [clearing])
        elif dim == 'bond-tenor' and value in ('lt1', '1to3', '3to5', '5to7', '7to10', 'gt10'):
            clause, values = tenor(*intervals[('lt1', '1to3', '3to5', '5to7', '7to10', 'gt10').index(value) + 1])
        dimensions.setdefault(dim, []).append((clause, values))
    for clauses in dimensions.values():
        parts.append('(' + ' OR '.join(clause for clause, _ in clauses) + ')')
        for _, values in clauses:
            params.extend(values)
    prices = {'Bid': 'xj_number(l.best_bid)', 'Ofr': 'xj_number(l.best_ofr)',
              'Bid-中债': '(xj_number(l.best_bid)-xj_number(s.val_yield))*100',
              '中债-Ofr': '(xj_number(s.val_yield)-xj_number(l.best_ofr))*100',
              '买量': 'xj_number(l.best_bid_vol)', '卖量': 'xj_number(l.best_ofr_vol)'}
    for key, bounds in f['prices'].items():
        for bound, operator, epsilon in [('lo', '>=', -1e-9), ('hi', '<=', 1e-9)]:
            if bounds.get(bound) not in (None, ''):
                parts.append('(' + prices[key] + ') ' + operator + ' ?')
                params.append(float(decimal(bounds[bound])) + epsilon)
    return ' AND '.join(parts), params


def query_public(f: dict, statuses: dict, *, before=0, before_uni=0, limit=50,
                 codes=None, excluded=()) -> tuple[list[dict], int]:
    """Count and bounded keyset query in one cache snapshot (never OFFSET)."""
    if type(limit) is not int or not 1 <= limit <= 100001:
        raise ValueError('行情读取上限无效')
    where, params = _filter_where(f, statuses)
    if codes is not None:
        if not codes:
            return [], 0
        if len(codes) > 500:
            raise ValueError('券列表超过500条')
        where += ' AND s.windcode IN (' + ','.join('?' for _ in codes) + ')'
        params.extend(codes)
    if excluded:
        where += ' AND s.windcode NOT IN (SELECT value FROM json_each(?))'
        params.append(json.dumps(list(excluded)))
    sql_from = ' FROM quote_latest l JOIN bond_static s ON s.bond_uni_code=l.bond_uni_code WHERE ' + where
    with transaction() as c:
        total = c.execute('SELECT COUNT(*)' + sql_from, params).fetchone()[0]
        select = (','.join('l.' + col for col in _PUBLIC_LATEST_COLS) + ','
                  + ','.join('s.' + col for col in STATIC_COLS[1:]))
        sql = 'SELECT ' + select + sql_from
        page_params = list(params)
        if before:
            if before_uni:
                sql += ' AND (l.flow_id,l.bond_uni_code) < (?,?)'
                page_params.extend([before, str(before_uni)])
            else:
                sql += ' AND l.flow_id < ?'; page_params.append(before)
        sql += ' ORDER BY l.flow_id DESC,l.bond_uni_code DESC LIMIT ?'
        page_params.append(limit)
        rows = c.execute(sql, page_params).fetchall()
    return [{'latest': {col: row[col] for col in _PUBLIC_LATEST_COLS}, 'static': _static_dict(row)} for row in rows], total
