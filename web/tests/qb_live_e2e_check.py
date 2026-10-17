"""对 qb-live-e2e.cjs 的采样结果按 MySQL 真值计算:端到端延迟 + 数值正确性 + 性能判定。

真值:bondtrade_test.qb_live_best_quote(旁路从 DM 页面收下、capture_time = 本机收到时刻)。
  延迟 = 页面单元格变成值 v 的时刻 − 该券最近一条「该侧收益率 = v」的行的 capture_time(60s 窗内)
         即「DM 推送到本机」之后的全部链路:入库 → xunjia 轮询 → 网关成批 → SharedWorker → pull → 渲染
  正确性 = 快照时刻各经纪商最新行里,最优 bid(收益率最低)/最优 ofr(收益率最高)与页面一致;
           该券在快照前 3s 内有新行的,算「在途」,不判错。
用法:~/work/qtrade/bondtrade/.venv/bin/python web/tests/qb_live_e2e_check.py
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import os
import statistics
import sys

required = ('QB_MYSQL_HOST', 'QB_MYSQL_PORT', 'QB_MYSQL_USER', 'QB_MYSQL_PASSWORD', 'QB_MYSQL_DB')
missing = [key for key in required if not os.environ.get(key)]
if missing:
    print('Missing required configuration: ' + ', '.join(missing), file=sys.stderr)
    raise SystemExit(2)
try:
    port = int(os.environ['QB_MYSQL_PORT'])
    if not 1 <= port <= 65535:
        raise ValueError
except ValueError:
    print('QB_MYSQL_PORT must be an integer from 1 to 65535', file=sys.stderr)
    raise SystemExit(2)

import pymysql

OUT = pathlib.Path(__file__).parent / "out"
raw = json.loads((OUT / "qb-live-e2e.raw.json").read_text(encoding="utf-8"))
c = pymysql.connect(host=os.environ['QB_MYSQL_HOST'], port=int(os.environ['QB_MYSQL_PORT']),
                    user=os.environ['QB_MYSQL_USER'], password=os.environ['QB_MYSQL_PASSWORD'],
                    database=os.environ['QB_MYSQL_DB'], charset="utf8mb4", connect_timeout=10, read_timeout=30)
cur = c.cursor()
cur.execute("SELECT bond_code, bond_uni_code FROM qb_bond_dict")
code2uni = {code: uni for code, uni in cur.fetchall() if code}


def uni_of(code: str):
    if code.startswith("UNI"):
        return int(code[3:])
    return code2uni.get(code)


def ms2dt(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000)


def num(s: str):
    try:
        return round(float(s), 4)
    except (TypeError, ValueError):
        return None


results = []


def judge(name, ok, detail):
    results.append({"name": name, "pass": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {name} — {detail}")


# ---------------- 延迟
lat, unmatched = [], 0
for e in raw["events"]:
    if e["kind"] not in ("ob", "oo"):
        continue
    v, u = num(e["to"]), uni_of(e["code"])
    if v is None or u is None:
        continue
    col = "bid_yield" if e["kind"] == "ob" else "ofr_yield"
    t = ms2dt(e["t"])
    cur.execute(f"SELECT MAX(capture_time) FROM qb_live_best_quote WHERE bond_uni_code=%s AND {col}=%s "
                "AND capture_time <= %s AND capture_time >= %s", (u, v, t, t - dt.timedelta(seconds=60)))
    ct = cur.fetchone()[0]
    if ct is None:
        unmatched += 1
        continue
    lat.append((t - ct).total_seconds() * 1000)
appear = sum(1 for e in raw["events"] if e["kind"] == "appear")
if lat:
    lat.sort()
    p50, p95, mx = statistics.median(lat), lat[int(len(lat) * 0.95) - 1] if len(lat) >= 20 else lat[-1], lat[-1]
    judge("端到端延迟(DM 推送到本机 → 页面单元格)", p95 <= 2500,
          f"样本 {len(lat)} 个,P50 {p50:.0f}ms / P95 {p95:.0f}ms / 最大 {mx:.0f}ms;未能对上真值 {unmatched} 个")
else:
    judge("端到端延迟(DM 推送到本机 → 页面单元格)", False, f"采样期间视口内没有价格变化(未匹配 {unmatched}),无法计算——确认是否盘中")

# ---------------- 正确性
snapT = ms2dt(raw["snapT"])
ok_n = bad_n = inflight = skipped = 0
bad = []
for r in raw["snapshot"]:
    u = uni_of(r["code"])
    if u is None:
        skipped += 1
        continue
    cur.execute("SELECT q.broker_id, q.bid_yield, q.ofr_yield, q.capture_time FROM qb_live_best_quote q "
                "JOIN (SELECT broker_id, MAX(seq_ns) s FROM qb_live_best_quote WHERE bond_uni_code=%s "
                "      AND capture_time <= %s AND update_time >= CURDATE() - INTERVAL 1 DAY GROUP BY broker_id) m "
                "ON q.broker_id=m.broker_id AND q.seq_ns=m.s WHERE q.bond_uni_code=%s", (u, snapT, u))
    rows = cur.fetchall()
    bids = [(float(b), bk) for bk, b, _, _ in rows if b is not None]
    ofrs = [(float(o), bk) for bk, _, o, _ in rows if o is not None]
    exp_b = round(min(bids)[0], 4) if bids else None
    exp_o = round(max(ofrs)[0], 4) if ofrs else None
    got_b, got_o = num(r["ob"]), num(r["oo"])
    if (exp_b, exp_o) == (got_b, got_o):
        ok_n += 1
        continue
    cur.execute("SELECT COUNT(*) FROM qb_live_best_quote WHERE bond_uni_code=%s AND capture_time BETWEEN %s AND %s",
                (u, snapT - dt.timedelta(seconds=3), snapT + dt.timedelta(seconds=1)))
    if cur.fetchone()[0]:
        inflight += 1
        continue
    bad_n += 1
    bad.append(f"{r['name']} 页面 {got_b}/{got_o} 真值 {exp_b}/{exp_o}")
judge("数值正确性(视口快照 vs 库内真值)", bad_n == 0,
      f"一致 {ok_n} 行,在途 {inflight},不一致 {bad_n},无法定位 {skipped}" + (";例:" + ";".join(bad[:3]) if bad else ""))

# ---------------- 性能(真实数据)
p, b = raw["perf"], raw["backend"]
judge("首屏", raw["firstMs"] <= 3000, f"{raw['firstMs']}ms")
judge("主线程占用", p["mainThreadPct"] <= 15, f"{p['mainThreadPct']:.1f}%(脚本 {p['scriptPct']:.1f}% / 布局 {p['layoutPct']:.1f}%)")
judge("浏览器全部进程 CPU", p["browserCpuPct"] <= 30, f"{p['browserCpuPct']:.1f}%(单核),RSS {p['rssMB']:.0f}MB")
h = p["heapMB"]
judge("JS 堆", h[-1] - h[0] <= 15, f"{h[0]:.1f} → {h[-1]:.1f}MB(峰 {max(h):.1f}),DOM 节点 {p['domNodes']:.0f}")
judge("长任务", p["longTasks"] <= 3, f"{p['longTasks']} 次 >50ms")
judge("真实数据在流动", b["flows"] > 0 and b["pushed"] > 0,
      f"{raw['secs']:.0f}s 内 MySQL 新行 {b['rowsRead']}、最优价变动 {b['flows']}、网关推送 {b['pushed']};视口价格变化事件 {len(lat) + unmatched},新券上屏 {appear}")
for f in raw["filters"]:
    judge(f"筛选·{f['filter']}", f["ms"] <= 1200 and f["longTasks"] == 0, f"{f['ms']}ms(含 300ms 防抖),命中 {f['total']},长任务 {f['longTasks']}")
judge("页面无 JS 报错", not raw["errors"], "; ".join(raw["errors"][:3]) or "0 条")

(OUT / "qb-live-e2e.result.json").write_text(json.dumps({"at": raw["at"], "results": results}, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
fail = sum(not r["pass"] for r in results)
print(f"\n合计 {len(results)} 项,失败 {fail}")
sys.exit(1 if fail else 0)
