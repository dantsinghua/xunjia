"""下行:mailbus.poll("a") 取 outbound 回复 → action=send 用 chat_session 双闸门回填;
to_human 告警不回填;按 in_reply_to 幂等去重(宁可漏发不可重发)。

幂等实现:回填前先把 in_reply_to 写进 done.json(状态 inflight),再发送;
发送验证成功改 sent,失败改 failed 并告警——任何情况下同一 in_reply_to 不会二次点发送。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from relay.mailbus import poll

from . import config
from .gate import AnchorMismatch, GuardBlock
from .session import chat_session
from .state import Rpa

log = logging.getLogger("side_a.downlink")


class DoneStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (config.STATE_DIR / "done.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def status(self, key: str) -> str | None:
        return self.data.get(key, {}).get("status")

    def mark(self, key: str, status: str, **extra) -> None:
        self.data[key] = {"status": status, "ts": time.time(), **extra}
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")


def handle_one(rpa: Rpa, env: dict, done: DoneStore) -> str:
    """处理一封 outbound,返回结果标签。"""
    if env.get("type") != "outbound":
        return "ignored"
    key = env.get("in_reply_to") or ""
    action = env.get("action", "none")
    thread = env.get("thread") or ""
    if not key:
        log.error("outbound 缺 in_reply_to,丢弃: %s", env)
        return "invalid"
    prev = done.status(key)
    if prev is not None:
        log.warning("重复回复 in_reply_to=%s(已 %s),跳过", key, prev)
        return "duplicate"
    if action == "to_human":
        rpa.alarm(f"TO_HUMAN thread={thread} reason={env.get('reason')!r}")
        done.mark(key, "to_human", thread=thread, reason=env.get("reason"))
        return "to_human"
    if action != "send":
        done.mark(key, "none", thread=thread)
        return "none"
    text = (env.get("text") or "").strip()
    windcode = (env.get("session") or {}).get("windcode")
    if not text or not thread:
        done.mark(key, "invalid", thread=thread)
        return "invalid"
    done.mark(key, "inflight", thread=thread, text=text)  # 先占位再发:宁可漏发不可重发
    try:
        with chat_session(rpa, thread) as chat:
            chat.send_text(text, windcode)
    except (GuardBlock, AnchorMismatch) as e:
        done.mark(key, "blocked", thread=thread, text=text, reason=str(e))
        rpa.alarm(f"BLOCKED thread={thread} {e}")
        return "blocked"
    except Exception as e:  # noqa: BLE001
        done.mark(key, "failed", thread=thread, text=text, reason=str(e))
        rpa.alarm(f"SEND_FAILED thread={thread} in_reply_to={key} {e}")
        return "failed"
    done.mark(key, "sent", thread=thread, text=text)
    log.info("DOWN ← a in_reply_to=%s thread=%s 已回填 %r", key, thread, text)
    return "sent"


def handle(rpa: Rpa, done: DoneStore) -> list[str]:
    results = []
    for env, ack in poll("a"):
        try:
            results.append(handle_one(rpa, env, done))
        except Exception as e:  # noqa: BLE001
            log.error("handle_one 异常(降级仅记录): %s env=%s", e, env)
            results.append("error")
        ack()
    return results
