"""A 侧常驻主循环:to_ready 自愈 → 上行采集 → 下行回填 → sleep。

    cd <仓库根> && .venv/bin/python -m relay.side_a.run_a [--once] [--interval 4]   # WSL x86 见 README-wsl-x86.md
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from . import config, downlink, uplink
from .cursor import CursorStore
from .downlink import DoneStore
from .state import Rpa, Unrecoverable


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_DIR / "side_a.log", encoding="utf-8"),
    ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--interval", type=float, default=config.POLL_INTERVAL)
    ap.add_argument("--all-threads", action="store_true", help="每轮扫所有会话(默认只扫有未读的)")
    args = ap.parse_args(argv)
    setup_logging()
    log = logging.getLogger("side_a.run")

    rpa = Rpa()
    if not rpa.adb.connect():
        log.error("adb %s 未就绪", rpa.adb.serial)
        return 2
    rpa.adb.ensure_ime()
    cursor, done = CursorStore(), DoneStore()
    log.info("A 侧启动 target=%s serial=%s interval=%ss", config.TARGET, rpa.adb.serial, args.interval)
    fails = 0
    while True:
        try:
            up = uplink.collect(rpa, cursor, only_unread=not args.all_threads)
            down = downlink.handle(rpa, done)
            if up or down:
                log.info("round up=%s down=%s", up, down)
            fails = 0
        except Unrecoverable as e:
            fails += 1
            log.error("round unrecoverable(%d): %s", fails, e)
            time.sleep(min(60, 5 * fails))
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("round error: %s", e)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
