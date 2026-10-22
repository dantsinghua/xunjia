"""行情接入测试：换令牌并连上 WebSocket，然后关掉。

不把 cookie、sid、device_id、token 写进返回说明。不在这里常驻重连。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable


def _auth_note(exc: BaseException) -> str:
    """网络失败不要说成凭据错误。说明里不带异常原文，避免把地址或令牌带出去。"""
    name = type(exc).__name__
    text = str(exc).lower()
    network = name in {
        "ConnectTimeout", "ConnectError", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "ProxyError", "TimeoutException", "NetworkError",
    }
    if network or "timed out" in text or "proxy" in text or "connect" in text:
        return "连不上行情认证服务。请检查到 rest.innodealing.com:443 的网络。"
    return "换令牌失败，请检查 Cookie、SID 和设备标识"


from collector.qb_gateway import merge_sid as _merge_sid

async def probe_quote(
    *,
    creds: Callable[[], tuple[str, str, str]],
    authenticate: Callable[[str, str], dict],
    connect: Callable[..., Any],
    ws_url: Callable[[str, str], str],
    ws_base: str,
    timeout_s: float = 8,
) -> dict[str, Any]:
    """creds 返回 cookie、device_id、sid。connect 是 websockets.connect。"""
    cookie, device_id, sid = creds()
    cookie = _merge_sid(cookie.strip(), sid.strip())
    if not cookie or not device_id.strip():
        return {"ok": False, "wss_open": False, "note": "请先填写 Cookie 和设备标识"}
    try:
        tok = await asyncio.to_thread(authenticate, cookie, device_id.strip())
    except Exception as exc:
        return {"ok": False, "wss_open": False, "note": _auth_note(exc)}
    token = ""
    if isinstance(tok, dict):
        token = str(tok.get("t") or tok.get("token") or "")
    if not token:
        return {"ok": False, "wss_open": False, "note": "没有换到行情令牌"}
    url = ws_url(ws_base, token)
    opened = False
    try:
        async with connect(url, cookie, token, timeout_s) as ws:
            opened = True
            try:
                await asyncio.wait_for(ws.recv(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return {"ok": True, "wss_open": True, "note": "行情 WebSocket 已连上，暂未收到推送"}
    except Exception:
        if opened:
            return {"ok": False, "wss_open": False, "note": "行情 WebSocket 握手后已断开，未通过测试"}
        return {"ok": False, "wss_open": False, "note": "WebSocket 没有连上"}
    return {"ok": True, "wss_open": True, "note": "行情 WebSocket 已接入"}


def market_connect(url: str, cookie: str, token: str, open_timeout: float):
    """按采集器同一套请求头去连。调用方负责退出上下文，测试连接不会常驻。"""
    import os
    import urllib.parse

    import websockets

    subproto = urllib.parse.quote(token, safe="")
    options = dict(
        subprotocols=[subproto],
        open_timeout=open_timeout,
        ping_interval=None,
        max_size=None,
        additional_headers={
            "Origin": "https://web.innodealing.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
            ),
            "Cookie": cookie,
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    proxy = os.environ.get("QB_WS_PROXY")
    if proxy:
        options["proxy"] = proxy
    return websockets.connect(url, **options)
