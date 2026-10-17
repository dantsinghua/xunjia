"""B 侧常驻主循环:poll("b") → handle_inbound → send("a")。

用法(仓库根目录):
    .venv/bin/python -m relay.side_b.run_b            # 常驻,默认 2s 轮询
    .venv/bin/python -m relay.side_b.run_b --once     # 处理完当前收件箱即退出
    .venv/bin/python -m relay.side_b.run_b --interval 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from relay import mailbus

from .engine import handle_inbound
from .sessions import STATE_DIR, SessionStore


def process_once(store: SessionStore, log=print) -> int:
    n = 0
    for env, ack in mailbus.poll("b"):
        try:
            if env.get("type") != "inbound":
                log(f"[B] skip non-inbound type={env.get('type')} msg_id={env.get('msg_id')}")
                ack()
                continue
            out = handle_inbound(store, env)
            if out is not None:
                mailbus.send("a", out)
                log(f"[B] {env.get('msg_id')} <{env.get('text')}> → intent={out['meta'].get('intent')} "
                    f"action={out['action']} text={out['text']!r} reason={out['reason']} "
                    f"state={out['meta'].get('state')} round={out['meta'].get('round')}")
            else:
                log(f"[B] dup {env.get('msg_id')} skipped")
            ack()
            n += 1
        except Exception as e:  # 单条失败不崩主循环;不 ack,下轮重试(留痕)
            store.trace(kind="error", msg_id=env.get("msg_id"), error=repr(e))
            log(f"[B] ERROR msg_id={env.get('msg_id')}: {e!r}", file=sys.stderr)
            ack()   # PoC:错误信封移入 seen 防死循环;真实部署改为隔离箱
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()
    store = SessionStore()
    print(f"[B] up. mail={mailbus.MAIL} state={STATE_DIR} sessions={len(store.sessions)}")
    if args.once:
        n = process_once(store)
        print(f"[B] processed {n}")
        return
    try:
        while True:
            process_once(store)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("[B] bye")


if __name__ == "__main__":
    main()
