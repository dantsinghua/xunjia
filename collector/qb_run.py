"""QB 行情自建采集器主程序 —— 零浏览器、零篡改猎,程序直连 innodealing 并入库。

  authenticate(cookie+sign) → token → wss 连接(url sign + subprotocol token)
  → JOIN_GROUP 订阅 1_<经纪商>/5_<经纪商> → 收 GROUP_MESSAGE_NOTIFICATION
  → messageContent 交 qb_apollo 解 → qb_ingest.Store 入库(与旁路同一套表)
凭据 ~/.config/qb/{cookie.txt,device_id.txt};单连接、不自动重连(失败即停并报错)。
用法: .venv/bin/python -m agent.qb_run [--seconds N] [--brokers 1,2,3,7,8,9] [--no-db]
"""
from __future__ import annotations

import argparse
import asyncio
import time

import websockets

from collector import qb_apollo, qb_ws
from collector.qb_gateway import WS, authenticate, load_creds
from collector.store import Store

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


async def run(seconds: float | None, brokers: list[int], use_db: bool) -> None:
    cookie, dev = load_creds()
    tok = authenticate(cookie, dev)
    token = tok.get("t") or tok.get("token")
    print(f"[auth] token ok(len {len(token)}), ea={tok.get('ea')}")
    url = qb_ws.ws_url(WS, token)
    store = Store(use_db)
    groups = [f"1_{b}" for b in brokers] + [f"5_{b}" for b in brokers]
    stat = {"connect_notif": 0, "group_msg": 0, "quotes": 0, "trades": 0, "other_ev": {}}
    last_log = [time.time()]

    # subprotocol = encodeURIComponent(token)
    import os
    import urllib.parse
    subproto = urllib.parse.quote(token, safe="")
    # ws 默认直连 innodealing(WSL 可达,实测通);仅 QB_WS_PROXY 显式设置时走代理(需 websockets≥17)
    proxy = os.environ.get("QB_WS_PROXY")
    kw = dict(subprotocols=[subproto], open_timeout=20, ping_interval=None, max_size=None,
              additional_headers={
                  "Origin": "https://web.innodealing.com", "User-Agent": _UA,
                  "Cookie": cookie, "Accept-Language": "zh-CN,zh;q=0.9"})
    if proxy:
        kw["proxy"] = proxy
    print(f"[ws] connecting {WS.split('?')[0]} {'via '+proxy if proxy else '直连'} …")
    async with websockets.connect(url, **kw) as ws:
        print("[ws] open")
        await ws.send(qb_ws.encode_packet(qb_ws.PT_EVENT, qb_ws.EV_JOIN_GROUP,
                                          qb_ws.encode_join_group(groups), ack_id=1))
        print(f"[ws] JOIN_GROUP 订阅 {len(groups)} 组: {groups}")

        async def _hb():
            while True:
                await asyncio.sleep(20)
                try:
                    await ws.send(qb_ws.encode_packet(qb_ws.PT_PING))
                except Exception:
                    break
        hb = asyncio.create_task(_hb())
        # token 过期(ea, ms)前 90s 主动结束本次连接,交给外层重连换新 token
        tok_deadline = (tok.get("ea", 0) / 1000 - 90) if tok.get("ea") else None
        hard = None if seconds is None else time.time() + seconds
        deadline = min(x for x in (hard, tok_deadline) if x) if (hard or tok_deadline) else None
        try:
            while deadline is None or time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, str):
                    continue
                pkt = qb_ws.decode_packet(raw)
                if pkt["type"] == qb_ws.PT_PONG:
                    continue
                ev = pkt["eventType"]
                if ev == qb_ws.EV_CONNECT_NOTIFICATION:
                    stat["connect_notif"] += 1
                    continue
                if ev == qb_ws.EV_GROUP_MESSAGE_NOTIFICATION:
                    stat["group_msg"] += 1
                    gm = qb_ws.decode_group_message(pkt["data"])
                    mc, mt = gm["messageContent"], gm["messageType"]
                    if not mc:
                        continue
                    try:
                        if mt == 1:
                            store.save(qb_apollo.BEST_QUOTES, [qb_apollo.parse_push_quote(mc)])
                            stat["quotes"] += 1
                        elif mt == 5:
                            row = qb_apollo.parse_push_trade(mc)
                            if row:
                                store.save(qb_apollo.TRADES, [row]); stat["trades"] += 1
                    except Exception as e:  # noqa: BLE001
                        print(f"[decode] {gm['groupName']}/{mt} 失败: {type(e).__name__}: {e}")
                else:
                    stat["other_ev"][ev] = stat["other_ev"].get(ev, 0) + 1
                if time.time() - last_log[0] >= 60:
                    print(f"[stat] {time.strftime('%H:%M:%S')} 报价{stat['quotes']} 成交{stat['trades']} "
                          f"组消息{stat['group_msg']} 其它{stat['other_ev']}", flush=True)
                    last_log[0] = time.time()
        finally:
            hb.cancel()
    print(f"[done] {stat}", flush=True)
    return stat


async def supervise(brokers: list[int], use_db: bool) -> None:
    """常驻:断线或 token 到期就重连(退避 5→10→30→60s 循环);authenticate 失败(cookie 过期)停并报错。"""
    backoff = [5, 10, 30, 60]
    i = 0
    while True:
        try:
            await run(None, brokers, use_db)
            i = 0            # 正常收尾(token 到期)→ 立即重连换新 token
        except Exception as e:  # noqa: BLE001
            import httpx
            if isinstance(e, httpx.HTTPStatusError) or "authenticate" in str(e):
                print(f"[fatal] 换 token 失败(cookie 可能过期,需重新提供): {e}", flush=True)
                raise SystemExit(2)
            w = backoff[min(i, len(backoff) - 1)]; i += 1
            print(f"[reconnect] {type(e).__name__}: {str(e)[:100]} → {w}s 后重连", flush=True)
            await asyncio.sleep(w)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--brokers", default="1,2,3,6,7,8,9")
    ap.add_argument("--no-db", action="store_true")
    a = ap.parse_args()
    brokers = [int(x) for x in a.brokers.split(",") if x.strip()]
    try:
        if a.seconds:
            asyncio.run(run(a.seconds, brokers, not a.no_db))
        else:
            asyncio.run(supervise(brokers, not a.no_db))   # 常驻
    except KeyboardInterrupt:
        print("停止。")


if __name__ == "__main__":
    main()
