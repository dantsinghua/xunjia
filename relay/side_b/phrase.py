"""话术库(固定模板,docs/05)+ 红线闸(docs/05 §8)。不经模型。

- 询价轮:`bid {code} {简称} {量}W 估值+{N}能出么`;卖出镜像 `ofr {code} {简称} {量}W 估值-{N}能收么`。
- 追问量:`{简称} 这个价能出多少量?`(每会话一次)
- 转人工对对手:`您好,等我内部请示下`;到边界收口同口径。
- 红线闸:禁要约/成交词、禁内部字段外泄;命中 → 改发请示话术 + blocked 标记。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

HOLD_TEXT = "您好,等我内部请示下"
CLOSE_TEXT = "您好,这个价我这边请示一下,有消息回您"


def fmt_bp(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def round_text(direction: str, windcode: str, name: str, vol_wan: int, offset_bp: float) -> str:
    """一轮询价话术,必带券锚(代码+简称不能省)。offset_bp 为绝对值,符号按方向写死。"""
    if direction == "ofr":
        return f"ofr {windcode} {name} {vol_wan}W 估值-{fmt_bp(offset_bp)}能收么".replace("  ", " ")
    return f"bid {windcode} {name} {vol_wan}W 估值+{fmt_bp(offset_bp)}能出么".replace("  ", " ")


def ask_vol_text(name: str) -> str:
    return f"{name} 这个价能出多少量?".strip()


def human_note(direction: str, windcode: str, name: str, vol_wan: int, price_desc: str, reason: str) -> str:
    """给本方交易员的转人工提示(内网,可含方向/代码/简称/量/对方价)。"""
    return f"{windcode} {name} {vol_wan}W,{reason}{(':' + price_desc) if price_desc else ''},是否介入"


# ---------- 红线闸 ----------
DEAL_WORDS = [r"成交", r"tkn", r"taken", r"gvn", r"given", r"trd", r"done", r"拿了", r"打掉", r"敲了", r"敲定",
              r"我们要了", r"要了", r"就这么定", r"定了", r"我确认", r"按这个价做", r"按这个做", r"就按这个"]
LEAK_WORDS = [r"可投量", r"含担保", r"不含担保", r"产品代码", r"组合号", r"组合", r"授信", r"持仓", r"底价", r"边界"]
_DEAL_RE = re.compile("|".join(DEAL_WORDS), re.IGNORECASE)
_LEAK_RE = re.compile("|".join(LEAK_WORDS))


@dataclass
class GuardResult:
    allowed: bool
    text: str
    hit: str | None = None
    reason: str = ""


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s​\-_.·、,，]+", "", t)


def guard(text: str) -> GuardResult:
    """出口闸:命中要约/成交词或内部字段 → 拦下改发请示话术(不 raise)。"""
    n = _normalize(text)
    m = _DEAL_RE.search(n)
    if m:
        return GuardResult(False, HOLD_TEXT, hit=m.group(0), reason="待发话术含要约/成交意思表示,已拦截改发请示话术")
    m = _LEAK_RE.search(n)
    if m:
        return GuardResult(False, HOLD_TEXT, hit=m.group(0), reason="待发话术含内部字段,已拦截改发请示话术")
    return GuardResult(True, text)
