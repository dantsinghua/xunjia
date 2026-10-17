"""QB(羿珩 DM)经纪商行情 apollo 接口响应解码:zlib → protobuf → 行字典。

数据来源:浏览器旁路(tools/qb_tap.user.js)转发页面自己拉到的响应,本模块不发任何网络请求。
接口(均 POST,rest.innodealing.com/onshore-broker-quotation-apollo):
  /api/best/quotes/list        body {"validQuoteStatus":1,"isSnapshot":0}  → 经纪商最优报价
  /api/today/trades/stat/list  body {"isSnapshot":0}                       → 今日成交(+ 该券当日统计)
字段号 → 含义的映射见 docs/QB-websocket-逆向笔记.md;标「?」者为推测,原始 protobuf 另存 raw_json 以便重映射。
"""
from __future__ import annotations

import datetime as dt
import zlib

from collector.pb_raw import as_double, as_sint, as_str, dump, fields

BEST_QUOTES = "/api/best/quotes/list"
TRADES = "/api/today/trades/stat/list"


def inflate(raw: bytes) -> bytes:
    return zlib.decompress(raw)


def _ms(v) -> dt.datetime | None:
    if not v:
        return None
    return dt.datetime.fromtimestamp(v / 1000)


class _R:
    """对 fields() 结果的类型化取值。"""

    def __init__(self, b: bytes):
        self.f = fields(b)
        self.b = b

    def i(self, n):
        v = self.f.get(n, [None])[0]
        return as_sint(v) if isinstance(v, int) else None

    def d(self, n):
        return as_double(self.f.get(n, [None])[0])

    def s(self, n):
        return as_str(self.f.get(n, [None])[0])

    def msgs(self, n):
        return [_R(x) for x in self.f.get(n, [])]


def parse_best_quotes(pb: bytes) -> list[dict]:
    top = _R(pb).msgs(3)
    if not top:
        return []
    return [_quote_row(r) for r in top[0].msgs(7)]


def parse_push_quote(pb: bytes) -> dict:
    """SharedWorker 推送 groupName=1_<经纪商>、messageType=1:单条报价行(未压缩,结构同列表行)。"""
    return _quote_row(_R(pb))


def parse_push_trade(pb: bytes) -> dict | None:
    """推送 groupName=5_<经纪商>、messageType=5:单条成交(结构同成交列表的 item)。"""
    return _trade_item(_R(pb))


def _quote_row(r: "_R") -> dict:
    def quote_yield(kind, raw, converted):
        if r.i(kind) not in (1, 3):
            return None
        value = r.d(converted)
        if value is not None:
            return value
        return r.d(raw) if r.i(kind) == 3 else None

    if True:
        return {
            "quote_key": r.s(2),                  # "<经纪商id>_<bondUniCode>"
            "quote_uuid": r.s(3),
            "bond_uni_code": r.i(4),
            "broker_id": r.i(5),
            "tenor": r.s(7),                      # 剩余期限,如 5.59Y / 57D
            "remain_days": r.i(8),
            "maturity_date": _ms(r.i(9)),
            "coupon_rate": r.d(11),               # ? 票面利率
            "issue_amount": r.d(12),              # ? 发行规模(亿)
            # 16/30 = 报价方式(DM 枚举 Yield=3 / NetPrice=1 / 0=无价);14/28 = 原始报价值,
            # 17/31 = DM 换算后的收益率 —— 净价报价时 14 是净价(如 99.85),必须取 17/31 才能与收益率比较
            "bid_quote_type": r.i(16),
            "bid_yield": quote_yield(16, 14, 17),
            "bid_str": r.s(15),                   # 带 * = 请示盘
            "bid_net_price": r.d(14) if r.i(16) == 1 else r.d(18),
            "bid_pending": r.i(19),               # 1=请示(*)
            "bid_vol_str": r.s(24),               # 买量原文,如 5000(明天+0 整量)+3000
            "bid_vol": r.d(23),
            "ofr_quote_type": r.i(30),
            "ofr_yield": quote_yield(30, 28, 31),
            "ofr_str": r.s(29),
            "ofr_net_price": r.d(28) if r.i(30) == 1 else r.d(32),
            "ofr_pending": r.i(33),
            "ofr_vol_str": r.s(38),
            "ofr_vol": r.d(37),
            "cnbd_yield_ex": r.d(42),             # 中债估值 行权(?)
            "cnbd_yield_mat": r.d(43),            # 中债估值 到期(?)
            "cnbd_net_price": r.d(45),
            "cnbd_duration": r.d(46),
            "csi_yield_ex": r.d(50),              # ? 中证估值
            "csi_yield_mat": r.d(51),
            "update_time": _ms(r.i(56)),
            "seq_ns": r.i(59),                    # 纳秒级版本戳,行变化即变
            "raw": dump(r.b),
        }


def parse_trades(pb: bytes) -> list[dict]:
    top = _R(pb).msgs(3)
    if not top:
        return []
    return [x for x in (_trade_item(i) for i in top[0].msgs(1)) if x]


def _trade_item(item: "_R") -> dict | None:
    ts = item.msgs(1)
    if not ts:
        return None
    t = ts[0]
    st = item.msgs(2)
    s = st[0] if st else None
    if True:
        return {
            "trade_key": t.s(1),                  # "<经纪商id>_<成交id>_<bondUniCode>"
            "broker_id": t.i(2),
            "trade_uuid": t.s(3),
            "seq_ns": t.i(4),
            "bond_uni_code": t.i(5),
            "tenor": t.s(7),
            "remain_days": t.i(8),
            "maturity_date": _ms(t.i(9)),
            "deal_flag10": t.i(10),               # ? 0/1
            "trade_yield": t.d(11),
            "trade_net_price": t.d(17),
            "chg_bp": t.d(18),                    # ? 较前笔变动 bp
            "trade_time": _ms(t.i(19)),
            "cnbd_yield": t.d(21),                # ? 中债估值
            "vs_cnbd_bp": t.d(22),                # ? 偏离估值 bp
            "deal_type": t.i(28),                 # ? TKN/GVN/TRD 编码
            "settle_text": t.s(38),               # 清算速度原文,如 +1 / 明天+0
            "day_last": s.d(4) if s else None,    # 该券当日统计(?):最新/最高/最低/笔数
            "day_high": s.d(5) if s else None,
            "day_low": s.d(6) if s else None,
            "day_count": s.i(7) if s else None,
            "raw": dump(item.b),
        }
