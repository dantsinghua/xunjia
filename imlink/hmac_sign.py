"""公网入站签名。算法与采集端 qtrade_agent.hmac_inbound 的 §3.5 一致。

canonical = METHOD \\n PATH \\n CANONICAL_QUERY \\n sha256hex(BODY) \\n TIMESTAMP \\n NONCE
签名头 X-QT-Signature = v1= + hex(HMAC-SHA256(secret, canonical))
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional, Union
from urllib.parse import quote


def sha256_hex(body: Optional[Union[bytes, str]]) -> str:
    if body is None:
        body = b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _rfc3986(s: str) -> str:
    return quote(s, safe="-._~")


def canonical_query(query: Optional[str]) -> str:
    if not query:
        return ""
    raw = query[1:] if query.startswith("?") else query
    pairs: list[tuple[str, str]] = []
    for part in raw.split("&"):
        if not part:
            continue
        k, sep, v = part.partition("=")
        pairs.append((k, v if sep else ""))
    if not pairs:
        return ""
    return "&".join(f"{_rfc3986(k)}={_rfc3986(v)}" for k, v in sorted(pairs))


def canonical_string(method: str, path: str, query: Optional[str], body: Optional[Union[bytes, str]],
                     timestamp: Union[int, str], nonce: str) -> str:
    return "\n".join([
        method.upper(), path, canonical_query(query), sha256_hex(body), str(timestamp), nonce,
    ])


def sign(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def auth_headers(app_id: str, secret: str, method: str, path: str, query: Optional[str],
                 body: Optional[Union[bytes, str]]) -> dict[str, str]:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    canonical = canonical_string(method, path, query, body, ts, nonce)
    return {
        "X-QT-AppId": app_id,
        "X-QT-Timestamp": ts,
        "X-QT-Nonce": nonce,
        "X-QT-Signature": "v1=" + sign(secret, canonical),
    }
