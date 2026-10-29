"""DM web login over HTTP. No browser, no extra system packages.

POST https://web.innodealing.com/auth-service/signin with username/password,
using the same site-egress TCP rewrite as the quote collector.
"""
from __future__ import annotations

import re

from relay.site_egress import httpx_request

SIGNIN = "https://web.innodealing.com/auth-service/signin"
ORIGIN = "https://web.innodealing.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


class QuoteLoginError(RuntimeError):
    """Username/password did not produce a usable DM session."""


def cookie_header(jar) -> str:
    """Last value per cookie name, the same string authenticate() sends."""
    values = {}
    for item in jar:
        values[item.name] = item.value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _request(client, method, url, headers, **kwargs):
    dial, hdrs, extensions = httpx_request(url, headers)
    options = {"extensions": extensions} if extensions else {}
    return getattr(client, method)(dial, headers=hdrs, **options, **kwargs)


def _error_text(response) -> str:
    location = ""
    headers = getattr(response, "headers", None) or {}
    if hasattr(headers, "get"):
        location = str(headers.get("location") or "")
    text = getattr(response, "text", "") or ""
    match = re.search(r'id="errorTip"[^>]*>([^<]*)', text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    if "error" in location:
        return "账号或密码错误"
    return "登录未获得会话，请检查用户名和密码"


def signin(username: str, password: str, *, client_factory=None) -> dict:
    """Return {'cookie', 'sid'} for an existing DM account. Never logs secrets."""
    username, password = username.strip(), password.strip()
    if not username or not password:
        raise QuoteLoginError("请填写用户名和密码")
    import httpx
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": ORIGIN,
        "Referer": SIGNIN,
    }
    with (client_factory or httpx.Client)(proxy=None, timeout=20, verify=True, trust_env=False) as client:
        _request(client, "get", SIGNIN, headers)
        response = _request(
            client, "post", SIGNIN + "?redirect_to=", headers,
            data={"username": username, "password": password, "redirect_to": ""},
        )
        cookie = cookie_header(client.cookies.jar)
        sid = ""
        for item in client.cookies.jar:
            if item.name == "sid":
                sid = item.value
        location = ""
        if hasattr(response.headers, "get"):
            location = str(response.headers.get("location") or "")
        landed = response.status_code in (301, 302, 303, 307, 308) and "/dashboard" in location
        if not (landed and sid):
            raise QuoteLoginError(_error_text(response))
    return {"cookie": cookie, "sid": sid}
