"""意向询价的报价渠道。

最优 = 当前已接入的全部报价里取最优。
经纪商 + 经纪商最优 = 只在经纪商报价里取最优。
经纪商 + 某一家 = 只看这一家的买卖价和量。
QT 意向、做市商尚未接入，列表必须为空。
"""
from __future__ import annotations

EMPTY_REASON = {
    "qt": "QT意向报价尚未接入",
    "mm": "做市商报价尚未接入",
}

# 银行间六家经纪商。下拉固定列出，不随当天簿子有没有成交而消失。
BROKERS = (
    (1, "国际"),
    (2, "中诚"),
    (3, "平安"),
    (7, "上田"),
    (8, "信唐"),
    (9, "国利"),
)


def classify(channel: str, broker: str = "") -> tuple[str, str]:
    """返回 (all|one|empty, 空表说明)。"""
    c = (channel or "best").strip().lower()
    if c in ("qt意向", "qtx"):
        c = "qt"
    if c in ("做市商", "maker"):
        c = "mm"
    if c in EMPTY_REASON:
        return "empty", EMPTY_REASON[c]
    picked = (broker or "").strip()
    if c == "broker" and picked and picked != "best":
        return "one", ""
    return "all", ""
