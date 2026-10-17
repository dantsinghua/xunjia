"""行情测试不连真实 innodealing。"""
from __future__ import annotations

import unittest

from imlink.quote_probe import probe_quote


class _Ws:
    def __init__(self, payload):
        self.payload = payload

    async def recv(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class ProbeTest(unittest.TestCase):
    def test_missing_cookie_does_not_call_auth(self):
        called = {"n": 0}

        def auth(cookie, device):
            called["n"] += 1
            return {"t": "x"}

        result = _run(probe_quote(
            creds=lambda: ("", "dev", ""),
            authenticate=auth,
            connect=lambda *a, **k: _Ws(b"x"),
            ws_url=lambda base, token: base,
            ws_base="wss://example.test",
        ))
        self.assertFalse(result["ok"])
        self.assertFalse(result["wss_open"])
        self.assertEqual(called["n"], 0)
        self.assertNotIn("dev", result["note"])

    def test_network_failure_is_not_blamed_on_credentials(self):
        def auth(cookie, device):
            raise TimeoutError("timed out")

        result = _run(probe_quote(
            creds=lambda: ("sid=abc", "dev", ""),
            authenticate=auth,
            connect=lambda *a, **k: _Ws(b"x"),
            ws_url=lambda base, token: base,
            ws_base="wss://example.test",
        ))
        self.assertFalse(result["wss_open"])
        self.assertIn("网络", result["note"])
        self.assertNotIn("Cookie", result["note"])
        self.assertNotIn("abc", result["note"])

    def test_auth_failure_stays_closed(self):
        def auth(cookie, device):
            raise RuntimeError("secret-cookie-should-not-leak")

        result = _run(probe_quote(
            creds=lambda: ("sid=abc", "dev", ""),
            authenticate=auth,
            connect=lambda *a, **k: _Ws(b"x"),
            ws_url=lambda base, token: base + token,
            ws_base="wss://example.test",
        ))
        self.assertFalse(result["wss_open"])
        self.assertNotIn("secret-cookie", result["note"])

    def test_open_socket_counts_as_connected(self):
        seen = {}

        def auth(cookie, device):
            seen["cookie"] = cookie
            return {"t": "tok"}

        def connect(url, cookie, token, open_timeout):
            seen["url"] = url
            seen["cookie"] = cookie
            return _Ws(b"frame")

        result = _run(probe_quote(
            creds=lambda: ("a=1", "dev", "abc"),
            authenticate=auth,
            connect=connect,
            ws_url=lambda base, token: f"{base}?t={token}",
            ws_base="wss://example.test",
        ))
        self.assertTrue(result["ok"])
        self.assertTrue(result["wss_open"])
        self.assertIn("sid=abc", seen["cookie"])
        self.assertNotIn("tok", result["note"])
