"""游标锚块(04 §3.6):企点无稳定消息 id,用"最后 K 条 (方向,文本)"序列做游标。

- diff_since(hist, anchor) → (new, truncated):从可见历史倒序找锚块,其后为新消息;
  找不到(被洪峰顶出)→ 全部可见视为新 + truncated=True。
- 首次(无锚)只建基线不产出新消息(顶层"baseline 期只建基线不动作")。
- 发送成功才前进游标(调用方负责 advance),失败不动、下轮重收。
- 另存已上报消息指纹集合做二次去重(需求 16.5-23)。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config

K = config.CURSOR_K
Msg = tuple[str, str]  # (dir, text)


def fingerprint(thread: str, dir_: str, text: str, ordinal: int) -> str:
    """消息指纹 = 会话 + 方向 + 文本 + 同文本在该会话内的序号(同一句话重复发不会被吞)。"""
    return hashlib.sha1(f"{thread}|{dir_}|{text}|{ordinal}".encode("utf-8")).hexdigest()[:20]


def diff_since(hist: list[Msg], anchor: list[Msg] | None) -> tuple[list[Msg], bool]:
    if not hist:
        return [], False
    if anchor is None:
        return [], False  # 基线期
    k = len(anchor)
    if k == 0:
        return list(hist), False
    for i in range(len(hist) - k, -1, -1):
        if hist[i:i + k] == anchor:
            return list(hist[i + k:]), False
    return list(hist), True


def next_anchor(hist: list[Msg]) -> list[Msg]:
    return list(hist[-K:])


class CursorStore:
    """cursor.json:{thread: {"anchor": [[dir,text],...], "seen": [fp,...]}}"""

    def __init__(self, path: Path | None = None):
        self.path = path or (config.STATE_DIR / "cursor.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def anchor(self, thread: str) -> list[Msg] | None:
        a = self.data.get(thread, {}).get("anchor")
        return [tuple(x) for x in a] if a is not None else None

    def seen(self, thread: str) -> set[str]:
        return set(self.data.get(thread, {}).get("seen", []))

    def advance(self, thread: str, hist: list[Msg], new_fps: list[str] | None = None) -> None:
        ent = self.data.setdefault(thread, {"anchor": None, "seen": []})
        ent["anchor"] = [list(x) for x in next_anchor(hist)]
        if new_fps:
            ent["seen"] = (ent.get("seen", []) + new_fps)[-500:]
        self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
