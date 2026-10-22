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
