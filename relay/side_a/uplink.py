"""上行:轮询目标 APP → 对方新消息 → inbound 信封(带 session_hint 券锚)→ mailbus.send("b", env)。"""
from __future__ import annotations

import logging
import re
import time

from relay.mailbus import send

from . import config
from .cursor import CursorStore, diff_since, fingerprint
from .session import chat_session
from .state import Rpa

log = logging.getLogger("side_a.uplink")
_ANCHOR_RE = re.compile(r"(\d{6,9}\.(?:IB|SH|SZ))\s+(\S+)", re.IGNORECASE)


def session_hint(thread: str, hist: list[tuple[str, str]]) -> dict:
    """券锚:首选从我方历史外发文本里回读(bid {code} {简称} ...),兜底静态表。"""
    for d, t in reversed(hist):
        if d == "me":
            m = _ANCHOR_RE.search(t)
            if m:
                return {"windcode": m.group(1).upper(), "简称": m.group(2)}
    return dict(config.THREAD_SESSIONS.get(thread, {}))


def build_inbound(thread: str, text: str, msg_id: str, hint: dict, cursor: list, truncated: bool) -> dict:
    env = {
        "v": 1, "type": "inbound", "msg_id": msg_id, "channel": "qidian", "thread": thread,
        "session_hint": hint, "text": text, "ts": time.time(),
        "cursor": "|".join(f"{d}:{t}" for d, t in cursor),
    }
    if truncated:
        env["truncated"] = True
    return env


def collect_thread(rpa: Rpa, thread: str, store: CursorStore) -> list[str]:
    """进一个会话,按游标取新对方消息并逐条上行。返回本轮上行的 msg_id 列表。"""
    sent: list[str] = []
    with chat_session(rpa, thread) as chat:
        hist = chat.read_history()
        if not hist:
            log.warning("%s 历史为空,跳过", thread)
            return sent
        anchor = store.anchor(thread)
        if anchor is None:
            store.advance(thread, hist)
            log.info("%s 建基线(%d 条),本轮不上行", thread, len(hist))
            return sent
        new, truncated = diff_since(hist, anchor)
        if truncated:
            log.warning("%s 游标锚块被顶出,全部可见视为新(truncated)", thread)
        seen = store.seen(thread)
        hint = session_hint(thread, hist)
        base = len(hist) - len(new)
        fps: list[str] = []
        for i, (d, t) in enumerate(new):
            if d != "peer":
                continue
            ordinal = sum(1 for dd, tt in hist[: base + i] if dd == d and tt == t)
            fp = fingerprint(thread, d, t, ordinal)
            if fp in seen:
                continue
            env = build_inbound(thread, t, fp, hint, hist[-config.CURSOR_K:], truncated)
            send("b", env)
            log.info("UP → b msg_id=%s thread=%s text=%r hint=%s", fp, thread, t, hint)
            sent.append(fp)
            fps.append(fp)
        store.advance(thread, hist, fps)  # 发送成功才前进游标
    return sent


def collect(rpa: Rpa, store: CursorStore, only_unread: bool = True) -> list[str]:
    rpa.to_ready()
    rows = rpa.list_threads()
    todo = [name for name, _, unread in rows if (unread > 0 or not only_unread or store.anchor(name) is None)]
    out: list[str] = []
    for name in todo:
        try:
            out += collect_thread(rpa, name, store)
        except Exception as e:  # noqa: BLE001
            log.error("collect %s 失败(降级仅记录): %s", name, e)
    return out
