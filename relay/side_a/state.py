"""状态机 + to_ready 归一化(04 §3.1 / §3.2)。

READY / LOGGED_OUT 稳态,CHAT_OPEN 只允许存在于 chat_session 括号内,UNKNOWN 自愈失败。
每轮主循环、每次操作前后把系统拉回 READY:back 清浮层 + 滚到列表顶 + 验证。
"""
from __future__ import annotations

import enum
import logging
import time

from . import config
from .primitives import Adb, Node, find, first

log = logging.getLogger("side_a.state")


class State(enum.Enum):
    READY = "READY"
    LOGGED_OUT = "LOGGED_OUT"
    CHAT_OPEN = "CHAT_OPEN"
    NOT_FOREGROUND = "NOT_FOREGROUND"
    UNKNOWN = "UNKNOWN"


class Unrecoverable(Exception):
    """to_ready 连续自愈失败。"""


class Rpa:
    """把 profile + adb 捆在一起的只读/写态工具;session.py 在其上做会话括号。"""

    def __init__(self, adb: Adb | None = None, prof: dict | None = None):
        self.adb = adb or Adb()
        self.p = prof or config.profile()
        self._last_alarm = 0.0

    # ------------------------------------------------------------ 只读
    def detect_state(self, nodes: list[Node] | None = None) -> State:
        pkg = self.adb.foreground_package()
        if pkg and pkg != self.p["package"]:
            return State.NOT_FOREGROUND
        nodes = nodes if nodes is not None else self.adb.dump()
        if self.p.get("login_marker") and first(nodes, rid=self.p["login_marker"]):
            return State.LOGGED_OUT
        if first(nodes, rid=self.p["chat_title"]):
            return State.CHAT_OPEN
        if first(nodes, rid=self.p["list_root"]):
            return State.READY
        return State.UNKNOWN

    def list_threads(self, nodes: list[Node] | None = None) -> list[tuple[str, Node, int]]:
        """会话列表可见项 [(peer, node, unread)],只认列表区(区域约束防误命中,04 §2a)。"""
        nodes = nodes if nodes is not None else self.adb.dump()
        root = first(nodes, rid=self.p["list_root"])
        if root is None:
            return []
        out = []
        names = find(nodes, rid=self.p["thread_name"], within=root)
        badges = find(nodes, rid=self.p["thread_unread"], within=root) if self.p.get("thread_unread") else []
        for n in names:
            unread = 0
            for b in badges:
                if abs(b.center[1] - n.center[1]) < 40 and b.text.isdigit():
                    unread = int(b.text)
                    break
            out.append((n.text, n, unread))
        return out

    def current_chat_title(self, nodes: list[Node] | None = None) -> str | None:
        nodes = nodes if nodes is not None else self.adb.dump()
        t = first(nodes, rid=self.p["chat_title"])
        return t.text if t else None

    # ------------------------------------------------------------ 归一化
    def to_ready(self, rounds: int = 4) -> State:
        for i in range(rounds):
            try:
                st = self.detect_state()
            except Exception as e:  # noqa: BLE001
                log.warning("detect_state 失败(%s): %s", i, e)
                st = State.UNKNOWN
            if st is State.READY:
                self._scroll_top()
                return st
            if st is State.LOGGED_OUT:
                self.alarm("LOGGED_OUT: 目标 APP 在登录页,需人工登录")
                return st
            log.info("to_ready round %d state=%s", i, st.value)
            if st is State.NOT_FOREGROUND:
                self.adb.launch(self.p["activity"])
            else:  # CHAT_OPEN / UNKNOWN → back 清浮层
                self.adb.back()
            time.sleep(0.5)
        shot = self.adb.screencap("unknown")
        self.alarm(f"UNKNOWN: 连续 {rounds} 轮自愈失败,截图 {shot}")
        raise Unrecoverable("to_ready failed")

    def _scroll_top(self) -> None:
        # 列表短时一次上滑即到顶;简单起见固定 2 次
        nodes = self.adb.dump()
        root = first(nodes, rid=self.p["list_root"])
        if root is None:
            return
        x1, y1, x2, y2 = root.bounds
        if y2 - y1 < 200:
            return
        names = find(nodes, rid=self.p["thread_name"], within=root)
        if names and names[0].bounds[1] <= y1 + 5:
            return
        cx = (x1 + x2) // 2
        for _ in range(2):
            self.adb.swipe(cx, y1 + 100, cx, y2 - 100, 200)

    def alarm(self, msg: str) -> None:
        now = time.time()
        if now - self._last_alarm > config.ALARM_RATE_S:
            self._last_alarm = now
            log.error("ALARM %s", msg)
        else:
            log.warning("(限频)%s", msg)
