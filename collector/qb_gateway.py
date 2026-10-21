"""QB(羿珩 DM)行情自建连接器 —— 完全脱离浏览器/篡改猴,程序直连。

链路(逆自 quote-web bundle):
  1. 登录 cookie(含 sid)+ device_id(sign 盐)取自安琳本人浏览器,存 ~/.config/qb/{cookie.txt,device_id.txt}(600)
  2. POST rest.innodealing.com/onshore-auth-service/api/token/v1/web/authenticate?…&sign=
     body {"cm":{}} → 返回 {"data":{"t":<token>,"ea":<过期ms>}}
  3. 连 wss://bosh2.innodealing.com/onshore-ws-gateway-apollo,token 走 Sec-WebSocket-Protocol(subprotocol)
  4. 上行 JOIN_GROUP(protobuf {groupNames:[...]}),下行 protobuf 报价/成交帧(与旁路同构,qb_apollo 解)
直连 443，不走本机代理。凭据只读、单连接、低频。
"""
from __future__ import annotations

import json
import os
import pathlib

import httpx

from collector.qb_sign import sign_params

CFG = pathlib.Path.home() / ".config" / "qb"
API = os.environ.get("QB_API_BASE", "https://rest.innodealing.com")
WS = os.environ.get("QB_WS_URL", "wss://bosh2.innodealing.com/onshore-ws-gateway")
FE_VER = os.environ.get("QB_FE_VER", "14260")


class QuoteAuthenticationError(RuntimeError):
    """The authentication response did not provide a usable market identity."""


def merge_sid(cookie: str, sid: str) -> str:
    """Cookie 的 sid 优先；独立 SID 只补缺，按完整 cookie 名判断。"""
    cookie, sid = cookie.strip(), sid.strip()
    if not sid or any(part.partition("=")[0].strip() == "sid" for part in cookie.split(";")):
        return cookie
    return cookie + (" " if cookie.endswith(";") else "; " if cookie else "") + "sid=" + sid


def load_creds() -> tuple[str, str]:
    from imlink.quote_source import load_for_probe
    cookie, device, sid = load_for_probe()
    cookie = merge_sid(cookie, sid)
    if not cookie or not device:
        raise ValueError("缺行情 Cookie/SID 或设备标识")
    return cookie, device


def authenticate(cookie: str, device_id: str) -> dict:
    """用 cookie 换 ws token。返回 {'t':token,'ea':过期ms,...}。"""
    params = sign_params({}, device_id, FE_VER)
    url = f"{API}/onshore-auth-service/api/token/v1/web/authenticate"
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Origin": "https://web.innodealing.com",
        "Referer": "https://web.innodealing.com/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    }
    with httpx.Client(proxy=None, timeout=15, verify=True, trust_env=False) as c:
        r = c.post(url, params=params, headers=headers,
                   content=json.dumps({"cm": {}}))
        r.raise_for_status()
        body = r.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not data or not (data.get("t") or data.get("token")):
        raise QuoteAuthenticationError("authenticate 未返回 token")
    return data


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-only", action="store_true", help="只验证换 token(不连 ws)")
    ap.parse_args()
    cookie, dev = load_creds()
    tok = authenticate(cookie, dev)
    t = tok.get("t") or tok.get("token")
    ea = tok.get("ea")
    print(f"✅ 换到 token(长度 {len(str(t))}),过期 ea={ea}")
    print("→ sign/cookie/device_id 全部正确,自建连接可行")


if __name__ == "__main__":
    main()
