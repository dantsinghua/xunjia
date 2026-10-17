"""chat_session 会话括号 + 双重发送闸门(04 §3.3,★最核心)。

唯一合法进入 CHAT_OPEN 的方式:
  进入: to_ready → find_thread(peer) → open_thread → 标题强校验 current_chat_title()==peer(4 次缓冲重试),失败 raise
  括号内: read_history / send_text 自由串接
  退出: finally → back + 滚顶,无论正常异常都交还 READY

send_text 发送前后各校验一次标题(闸门1 点输入框前、闸门2 点发送前),任一不符立即中止。
叠加券锚闸 + 红线闸(gate.py)。宁可不发,绝不发错。
"""
from __future__ import annotations

import contextlib
import logging
import time
import uuid

from . import config, gate
from .primitives import find, first
from .state import Rpa, State

log = logging.getLogger("side_a.session")


class WrongChat(Exception):
    """会话标题与目标不符。"""


class ThreadNotFound(Exception):
    pass


class SendFailed(Exception):
    """闸门通过但发送后未在历史里验证到。"""


class Chat:
    def __init__(self, rpa: Rpa, peer: str, tid: str):
        self.rpa, self.peer, self.tid = rpa, peer, tid
        self.p = rpa.p
        self._last_send = 0.0

    # ------------------------------------------------------------ 读
    def assert_title(self, where: str) -> None:
        t = self.rpa.current_chat_title()
        if t != self.peer:
            self.rpa.adb.screencap(f"wrongchat-{self.tid}")
            raise WrongChat(f"[{self.tid}] {where}: title={t!r} != peer={self.peer!r}")

    def read_history(self, retries: int = 3) -> list[tuple[str, str]]:
        """最近可见消息 [(dir, text)],dir ∈ {me, peer}。空读重试;判不出发送方保守判 peer(不漏收)。"""
        for i in range(retries):
            nodes = self.rpa.adb.dump()
            self.assert_title("read_history")
            root = first(nodes, rid=self.p["msg_list"])
            items = find(nodes, rid=self.p["msg_text"], within=root) if root else find(nodes, rid=self.p["msg_text"])
            if items:
                me = self.p.get("me_desc", "me")
                return [("me" if n.desc == me else "peer", n.text) for n in sorted(items, key=lambda n: n.bounds[1])]
            time.sleep(0.5 * (i + 1))
        return []

    # ------------------------------------------------------------ 写(双闸门)
    def send_text(self, text: str, windcode: str | None = None) -> None:
        text = text.strip()
        if not text:
            raise ValueError("empty text")
        gate.guard(text)                      # 红线闸(§3.5)
        gate.check_anchor(text, windcode)     # 券锚闸(§3.4)
        gap = config.SEND_MIN_GAP - (time.time() - self._last_send)
        if gap > 0:
            time.sleep(gap)

        # 闸门 1:点输入框前
        self.assert_title("gate1")
        nodes = self.rpa.adb.dump()
        box = first(nodes, rid=self.p["input"])
        if box is None:
            raise SendFailed(f"[{self.tid}] 找不到输入框")
        self.rpa.adb.tap(*box.center)
        if not self.rpa.adb.ensure_ime():
            log.warning("ADBKeyboard 未成为默认 IME,继续尝试")
        self.rpa.adb.clear_text()
        self.rpa.adb.input_text(text)

        # 回读输入框内容 == 待发文本(防 IME 吞字/半截)
        typed = None
        for _ in range(3):
            box = first(self.rpa.adb.dump(), rid=self.p["input"])
            typed = box.text if box else None
            if typed == text:
                break
            time.sleep(0.5)
        if typed != text:
            self.rpa.adb.clear_text()
            raise SendFailed(f"[{self.tid}] 输入框回读不符: {typed!r}")

        # 闸门 2:点发送前
        self.assert_title("gate2")
        btn = first(self.rpa.adb.dump(), rid=self.p["send"])
        if btn is None:
            self.rpa.adb.clear_text()
            raise SendFailed(f"[{self.tid}] 找不到发送按钮")
        log.info("[%s] SEND → %s: %r", self.tid, self.peer, text)
        self.rpa.adb.tap(*btn.center, settle=0.8)
        self._last_send = time.time()

        # 发送后验证:历史末尾出现我方同文本
        for i in range(4):
            hist = self.read_history()
            if any(d == "me" and t == text for d, t in hist[-3:]):
                return
            time.sleep(0.6 * (i + 1))
        self.rpa.adb.screencap(f"sendverify-{self.tid}")
        raise SendFailed(f"[{self.tid}] 发送后未在历史验证到")


def find_thread(rpa: Rpa, peer: str, max_scroll: int = 6):
    """按 peer 精确/前缀定位会话行(只认列表区),滚动查找。"""
    for i in range(max_scroll + 1):
        nodes = rpa.adb.dump()
        rows = rpa.list_threads(nodes)
        exact = [n for name, n, _ in rows if name == peer]
        if exact:
            return exact[0]
        pref = [n for name, n, _ in rows if name.startswith(peer)]
        if len(pref) == 1:
            return pref[0]
        root = first(nodes, rid=rpa.p["list_root"])
        if root is None or i == max_scroll:
            break
        x1, y1, x2, y2 = root.bounds
        rpa.adb.swipe((x1 + x2) // 2, y2 - 120, (x1 + x2) // 2, y1 + 120, 300)
    raise ThreadNotFound(peer)


@contextlib.contextmanager
def chat_session(rpa: Rpa, peer: str):
    tid = uuid.uuid4().hex[:8]
    log.info("[%s] open → %s", tid, peer)
    st = rpa.to_ready()
    if st is not State.READY:
        raise WrongChat(f"[{tid}] not READY: {st.value}")
    try:
        row = find_thread(rpa, peer)
        opened = False
        for attempt in range(3):
            rpa.adb.tap(*row.center, settle=0.8)
            for _ in range(4):  # 标题强校验,4 次缓冲重试
                if rpa.current_chat_title() == peer:
                    opened = True
                    break
                time.sleep(0.4)
            if opened:
                break
            log.warning("[%s] open_thread attempt %d 未验证到标题,归一后重试", tid, attempt)
            rpa.to_ready()
            row = find_thread(rpa, peer)
        if not opened:
            rpa.adb.screencap(f"open-fail-{tid}")
            raise WrongChat(f"[{tid}] 打开 {peer!r} 后标题不符: {rpa.current_chat_title()!r}")
        log.info("[%s] verify ok", tid)
        yield Chat(rpa, peer, tid)
    finally:
        log.info("[%s] exit → to_ready", tid)
        try:
            rpa.adb.back()
            rpa.to_ready()
        except Exception as e:  # noqa: BLE001
            log.error("[%s] exit 归一失败: %s", tid, e)
