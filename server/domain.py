"""Deterministic price checks, distinct from agent strategy selection."""
from decimal import Decimal, InvalidOperation
import re


def decimal(value, name: str = "数字") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name}不能为空且须为有限数字")
    raw = str(value).strip()
    if not re.fullmatch(r"-?\d{1,8}(?:\.\d{1,8})?", raw):
        raise ValueError(f"{name}格式无效")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{name}格式无效") from None
    if not number.is_finite():
        raise ValueError(f"{name}须为有限数字")
    return number


def goal(our_side: str, limit_yield_pct, peer_side: str, peer_yield_pct, *, valid: bool = True) -> dict:
    if our_side not in ("BUY", "SELL"):
        raise ValueError("必须明确本方BUY/SELL")
    expected = "ofr" if our_side == "BUY" else "bid"
    if not valid or peer_side != expected or peer_yield_pct is None:
        return {"raw_gap_bp": None, "remaining_gap_bp": None, "target_reached": False}
    boundary, peer = decimal(limit_yield_pct), decimal(peer_yield_pct)
    gap = (boundary - peer if our_side == "BUY" else peer - boundary) * 100
    return {"raw_gap_bp": str(gap), "remaining_gap_bp": str(max(Decimal(0), gap)), "target_reached": gap <= 0}


def policy(item: dict) -> dict:
    side = item.get("our_side")
    if side not in ("BUY", "SELL"):
        raise ValueError("须明确本方买入或卖出，不从行情原文推断")
    code, name = item.get("code"), item.get("name")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{4,12}\.(IB|SH|SZ|BJ)", code):
        raise ValueError("债券代码无效")
    if not isinstance(name, str) or not re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9 ()（）·.\-]{1,100}",name):
        raise ValueError("债券简称无效")
    from .public_text import reject_commitment_or_secret
    reject_commitment_or_secret(name)
    volume = decimal(item.get("volume_wan"), "询价量")
    opening = decimal(item.get("opening_yield_pct"), "起始收益率")
    boundary = decimal(item.get("limit_yield_pct"), "收益率边界")
    step = decimal(item.get("step_bp"), "步幅")
    if volume <= 0 or step <= 0:
        raise ValueError("询价量和步幅必须大于0")
    if (side == "BUY" and opening < boundary) or (side == "SELL" and opening > boundary):
        raise ValueError("起始收益率已越过授权边界")
    strict=item.get('strict_match',True)
    if type(strict) is not bool:raise ValueError('严格报量匹配须为布尔值')
    return {"strict_match":strict,"code": code, "name": name.strip(), "our_side": side, "volume_wan": str(volume),
            "opening_yield_pct": str(opening), "limit_yield_pct": str(boundary), "step_bp": str(step)}


def opening_text(value: dict) -> str:
    direction = "买入" if value["our_side"] == "BUY" else "卖出"
    return (f"您好，{value['code']} {value['name']}，我方考虑{direction}{value['volume_wan']}万，"
            f"收益率{value['opening_yield_pct']}%，还能帮忙争取吗？")
