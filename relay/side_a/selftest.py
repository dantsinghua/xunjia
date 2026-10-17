"""A 侧自测(对 mock 目标):
  1. 元操作通路:dump / screencap / tap / back / ADBKeyboard 输入
  2. 上行:向 mock 注入一条对方消息 → collect → 邮箱 b 里出现 inbound 信封
  3. 下行:构造一条回复投到邮箱 a → handle → 目标 APP 对应会话里出现回填(读 APP 落盘 store.json 验证)
  4. 闸门:红线词 / 券锚不符 / 重复 in_reply_to 均不回填

    cd <仓库根> && .venv/bin/python -m relay.side_a.selftest   # WSL x86 见 README-wsl-x86.md
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid

from relay.mailbus import MAIL, _parse, send

from . import config, downlink, uplink
from .cursor import CursorStore
from .downlink import DoneStore
from .state import Rpa, State

log = logging.getLogger("side_a.selftest")
PEER = "张三-华泰固收"
PKG = "com.xunjia.mockqidian"
RECEIVER = f"{PKG}/.PeerMsgReceiver"


def app_store(rpa: Rpa) -> dict:
    """读 mock APP 落盘的 store.json 作为"目标 APP 里可见"的证据(APK 标了 debuggable,走 run-as)。"""
    raw = rpa.adb.shell("run-as", PKG, "cat", "files/store.json")
    return json.loads(raw)


def inject_peer(rpa: Rpa, peer: str, text: str) -> None:
    rpa.adb.broadcast(f"{PKG}.PEER_MSG", RECEIVER, peer=peer, text=text)
    time.sleep(0.8)


def mailbox_has(box: str, msg_id: str) -> dict | None:
    """在 box 的 new/ 与 seen/ 里找 msg_id(B 若在跑会把它消费进 seen/)。eml 正文含中文时是 base64,必须走 email 解析。"""
    for sub in ("new", "seen"):
        for f in sorted((MAIL / box / sub).glob("*.eml")):
            try:
                env = _parse(f.read_bytes())
            except Exception:  # noqa: BLE001
                continue
            if env.get("msg_id") == msg_id:
                return env
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, info: str = "") -> None:
        results.append((name, ok, info))
        log.info("%s %s %s", "PASS" if ok else "FAIL", name, info)

    rpa = Rpa()
    if not rpa.adb.connect():
        log.error("adb 未就绪"); return 2
    check("ime", rpa.adb.ensure_ime(), config.ADBKEYBOARD_IME)

    # 重置 mock 数据 + 清本地游标,从干净状态开始
    rpa.adb.broadcast(f"{PKG}.RESET", RECEIVER)
    time.sleep(0.8)
    for f in ("cursor.json", "done.json"):
        (config.STATE_DIR / f).unlink(missing_ok=True)
    cursor, done = CursorStore(), DoneStore()

    # 1. 元操作
    st = rpa.to_ready()
    check("to_ready", st is State.READY, st.value)
    rows = rpa.list_threads()
    check("list_threads", any(n == PEER for n, _, _ in rows), str([n for n, _, _ in rows]))
    shot = rpa.adb.screencap("selftest")
    check("screencap", shot.exists() and shot.stat().st_size > 1000, str(shot))

    # 2. 上行:先建基线,再注入对方消息
    uplink.collect(rpa, cursor, only_unread=False)
    check("baseline", cursor.anchor(PEER) is not None, str(cursor.anchor(PEER)))
    peer_text = f"3000W 估值+8 能出么 #{uuid.uuid4().hex[:4]}"
    inject_peer(rpa, PEER, peer_text)
    ids = uplink.collect(rpa, cursor)
    env = mailbox_has("b", ids[0]) if ids else None
    check("uplink→b", bool(env) and env["type"] == "inbound" and env["text"] == peer_text and env["thread"] == PEER,
          json.dumps(env, ensure_ascii=False) if env else "no envelope")
    hint_ok = bool(env) and env.get("session_hint", {}).get("windcode") == "102400895.IB"
    check("session_hint", hint_ok, str(env.get("session_hint") if env else None))
    ids2 = uplink.collect(rpa, cursor, only_unread=False)
    check("uplink idempotent", ids2 == [], str(ids2))

    # 3. 下行:构造 B 的回复
    reply = "bid 102400895.IB 24苏科技城MTN001 3000W 估值+6能出么"
    rid = ids[0] if ids else "selftest-" + uuid.uuid4().hex[:6]
    send("a", {"v": 1, "type": "outbound", "in_reply_to": rid, "thread": PEER,
               "session": {"windcode": "102400895.IB"}, "action": "send", "text": reply})
    res = downlink.handle(rpa, done)
    hist = app_store(rpa).get(PEER, [])
    landed = any(m["dir"] == "me" and m["text"] == reply for m in hist[-2:])
    check("downlink→app", res == ["sent"] and landed, f"{res} last={hist[-1] if hist else None}")

    # 4. 闸门
    send("a", {"v": 1, "type": "outbound", "in_reply_to": rid, "thread": PEER,
               "session": {"windcode": "102400895.IB"}, "action": "send", "text": reply})
    check("dup in_reply_to", downlink.handle(rpa, done) == ["duplicate"])
    send("a", {"v": 1, "type": "outbound", "in_reply_to": "g-" + uuid.uuid4().hex[:6], "thread": PEER,
               "session": {"windcode": "102400895.IB"}, "action": "send", "text": "好的 成交"})
    check("redline block", downlink.handle(rpa, done) == ["blocked"])
    send("a", {"v": 1, "type": "outbound", "in_reply_to": "g-" + uuid.uuid4().hex[:6], "thread": PEER,
               "session": {"windcode": "102400895.IB"}, "action": "send",
               "text": "bid 102400896.IB 别的券 1000W 估值+3能出么"})
    check("anchor block", downlink.handle(rpa, done) == ["blocked"])
    send("a", {"v": 1, "type": "outbound", "in_reply_to": "h-" + uuid.uuid4().hex[:6], "thread": PEER,
               "session": {"windcode": "102400895.IB"}, "action": "to_human", "reason": "对方给实价"})
    check("to_human no send", downlink.handle(rpa, done) == ["to_human"])
    n_me = sum(1 for m in app_store(rpa).get(PEER, []) if m["dir"] == "me")
    check("app me-count", n_me == 2, f"me msgs={n_me}(seed 1 + 回填 1)")
    check("final READY", rpa.to_ready() is State.READY)

    bad = [r for r in results if not r[1]]
    print("\n==== side_a selftest:", f"{len(results) - len(bad)}/{len(results)} passed ====")
    for name, ok, info in results:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}  {info[:160]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
