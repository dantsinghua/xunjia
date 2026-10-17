"""询价入站上下文。

对方回复进后续解析前，带上同一会话里最近一条本方发出的原文。
这里只组装字段，不调用 Dify，也不发送消息。
"""
from __future__ import annotations

from typing import Any


def _session_of(row: dict[str, Any]) -> str:
    for key in ("session_id", "native_id", "peer_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _outbound(row: dict[str, Any]) -> bool:
    if row.get("dir") == "out":
        return True
    if row.get("is_self") in (1, True, "1"):
        return True
    if str(row.get("role") or "") in ("bot", "self"):
        return True
    return str(row.get("tag") or "") == "发出"


def _stamp(row: dict[str, Any], index: int) -> tuple:
    for key in ("ts_ms", "time"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return (value, index)
    return (index, index)


def _original(row: dict[str, Any]) -> str:
    text = row.get("text")
    if text is None:
        return ""
    return str(text)


def attach_prior_outbound(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """给入站回复填 prior_outbound_text。同一会话按时间取最近一条本方原文。"""
    if not rows:
        return []
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        key = _session_of(row)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append((index, row))
    out: list[dict[str, Any] | None] = [None] * len(rows)
    for key in order:
        items = sorted(grouped[key], key=lambda pair: _stamp(pair[1], pair[0]))
        last: str | None = None
        for index, row in items:
            item = dict(row)
            if _outbound(item):
                last = _original(item)
            else:
                item["prior_outbound_text"] = "" if last is None else last
            out[index] = item
    return [item if item is not None else {} for item in out]
