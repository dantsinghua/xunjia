"""A↔B 共享邮件总线(PoC,本机零凭据,语义等价 SMTP 提交 + 收件箱轮询)。

- `python -m relay.mailbus serve` 起本机 SMTP 服务 127.0.0.1:2526,按收件地址落盘到 relay/mail/<box>/new/。
- 库用法:
    from relay.mailbus import send, poll
    send("b", {...envelope...})      # A 发给 B
    for env, ack in poll("a"):       # B/A 取自己收件箱的新邮件
        ...; ack()                    # 处理完确认,移入 seen/,不再重复取

信封 = 一封邮件的 body(JSON),契约见 docs/07-email-relay-poc.md。
真实部署把 send/poll 换成 IMAP/SMTP 或邮件网关即可,A/B 两侧代码不用改。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as _POLICY
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIL = ROOT / "mail"
HOST, PORT = "127.0.0.1", 2526  # 8025 常被 frp 等占用,用 2526
BOX_BY_ADDR = {"a@relay.local": "a", "b@relay.local": "b"}
ADDR_BY_BOX = {v: k for k, v in BOX_BY_ADDR.items()}


def _box_dir(box: str) -> Path:
    d = MAIL / box
    (d / "new").mkdir(parents=True, exist_ok=True)
    (d / "seen").mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------------- 发送(SMTP 客户端)
def send(to_box: str, env: dict) -> str:
    """提交一封信到 to_box。返回 msg_id。走本机 SMTP;SMTP 未起则直接落盘兜底。"""
    env = dict(env)
    env.setdefault("ts", time.time())
    mid = env.get("msg_id") or env.get("in_reply_to") or uuid.uuid4().hex
    body = json.dumps(env, ensure_ascii=False).encode("utf-8")
    msg = EmailMessage()
    msg["From"] = ADDR_BY_BOX.get("a" if to_box == "b" else "b", "sys@relay.local")
    msg["To"] = ADDR_BY_BOX[to_box]
    msg["Subject"] = f"{env.get('type','msg')}:{mid}"
    msg.set_content(body.decode("utf-8"))
    # PoC 边界用可靠的收件箱直投(语义等价 SMTP 提交 + 收件箱轮询,真实部署换 IMAP/SMTP)。
    # aiosmtpd 服务(serve())仍在监听,供真实 SMTP 客户端投递;A↔B 内部走直投更稳。
    _deliver(to_box, body)
    return mid


# ----------------------------------------------------------------- 收取(收件箱轮询)
def poll(box: str):
    """取 box 收件箱所有未读信封,产出 (env, ack)。调用 ack() 标记已读。"""
    d = _box_dir(box)
    out = []
    for f in sorted((d / "new").glob("*.eml")):
        try:
            env = _parse(f.read_bytes())
        except Exception:
            f.rename(d / "seen" / f.name)
            continue

        def _ack(path=f):
            try:
                path.rename(d / "seen" / path.name)
            except FileNotFoundError:
                pass
        out.append((env, _ack))
    return out


def _parse(raw: bytes) -> dict:
    m = message_from_bytes(raw, policy=_POLICY)
    payload = m.get_content() if m.is_multipart() is False else m.get_payload(0).get_content()
    return json.loads(payload)


def _deliver(box: str, body: bytes) -> None:
    d = _box_dir(box)
    name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}.eml"
    msg = EmailMessage()
    msg["To"] = ADDR_BY_BOX[box]
    msg.set_content(body.decode("utf-8"))
    (d / "new" / name).write_bytes(bytes(msg))


# ----------------------------------------------------------------- SMTP 服务
def serve() -> None:
    from aiosmtpd.controller import Controller

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            raw = envelope.content if isinstance(envelope.content, bytes) else envelope.content.encode()
            for rcpt in envelope.rcpt_tos:
                box = BOX_BY_ADDR.get(rcpt.lower())
                if box:
                    _deliver(box, _body_of(raw))
            return "250 OK"

    def _body_of(raw: bytes) -> bytes:
        m = message_from_bytes(raw, policy=_POLICY)
        try:
            return (m.get_content() if not m.is_multipart() else m.get_payload(0).get_content()).encode("utf-8")
        except Exception:
            return raw

    c = Controller(Handler(), hostname=HOST, port=PORT)
    c.start()
    print(f"[mailbus] SMTP on {HOST}:{PORT}, mail dir {MAIL}")
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        c.stop()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        print("usage: python -m relay.mailbus serve")
