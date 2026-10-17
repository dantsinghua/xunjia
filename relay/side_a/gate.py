"""闸门(04 §3.4 / §3.5):券锚一致性 + 成交红线。纯函数,不碰设备。

- guard(text):命中要约/成交词 → GuardBlock。机器人永不发成交意思表示(需求 10.4)。
- check_anchor(text, windcode):待发文本里出现的券代码必须 == 当前会话绑定的 windcode,
  防止把 A 券的报价发进 B 券的语境。文本里没有券代码时放行(追问量/寒暄类),记 warning。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("side_a.gate")

# 出口词表:沿用 bondtrade demo guard 的思路。大小写不敏感;中文词直接子串匹配。
REDLINE_WORDS = ["要了", "成交", "按这个价做", "就这个价", "拿了", "给了", "确认成交", "done", "tkn", "gvn", "taken", "given"]
_REDLINE_RE = re.compile(
    "|".join(re.escape(w) if re.search(r"[一-鿿]", w) else rf"(?<![a-z]){re.escape(w)}(?![a-z])" for w in REDLINE_WORDS),
    re.IGNORECASE,
)
WINDCODE_RE = re.compile(r"\b(\d{6,9}\.(?:IB|SH|SZ))\b", re.IGNORECASE)


class GuardBlock(Exception):
    """红线闸拦截。"""


class AnchorMismatch(Exception):
    """券锚不一致。"""


def guard(text: str) -> None:
    m = _REDLINE_RE.search(text)
    if m:
        log.error("guard_block hit=%r text=%r", m.group(0), text)
        raise GuardBlock(f"红线词 {m.group(0)!r}")


def anchors_in(text: str) -> list[str]:
    return [c.upper() for c in WINDCODE_RE.findall(text)]


def check_anchor(text: str, windcode: str | None) -> None:
    codes = anchors_in(text)
    if not windcode:
        if codes:
            log.warning("会话未绑定券锚但文本含券 %s,放行", codes)
        return
    bad = [c for c in codes if c != windcode.upper()]
    if bad:
        log.error("anchor_mismatch session=%s text_codes=%s", windcode, codes)
        raise AnchorMismatch(f"文本券 {bad} != 会话券 {windcode}")
    if not codes:
        log.warning("外发文本不含券锚(会话 %s):%r", windcode, text)
