"""Portal-compatible SSO and application-specific signed sessions."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
import time
from typing import Callable


@dataclass(frozen=True)
class AuthConfig:
    sso_secret: str = field(default="", repr=False)
    session_secret: str = field(default="", repr=False)
    token_ttl_seconds: int = 3600
    sso_skew_seconds: int = 60
    clock: Callable[[], float] = field(default=time.time, repr=False)

    @classmethod
    def from_env(cls) -> "AuthConfig":
        return cls(
            sso_secret=os.environ.get("XUNJIA_SSO_SECRET", ""),
            session_secret=os.environ.get("XUNJIA_AUTH_SECRET", ""),
        )


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    display_name: str


class AuthError(ValueError):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class AuthService:
    def __init__(self, config: AuthConfig):
        self.config = config

    @property
    def configured(self) -> bool:
        return bool(self.config.sso_secret) and len(self.config.session_secret.encode()) >= 32

    def verify_sso(self, body: dict) -> AuthUser:
        if not self.configured:
            raise AuthError("登录服务尚未配置")
        legacy = body.get("userId") or body.get("user_id")
        uid = legacy or body.get("userName") or body.get("user_name")
        name = body.get("nickName") or body.get("nick_name") or (body.get("userName") if legacy else None)
        ts = body.get("ts") if "ts" in body else body.get("timestamp")
        sign = body.get("sign")
        if not isinstance(uid, str) or not isinstance(name, str):
            raise AuthError("登录参数无效")
        uid, name = uid.strip(), name.strip()
        if not uid or len(uid) > 160 or not name or len(name) > 160:
            raise AuthError("登录参数无效")
        if not isinstance(ts, str) or not re.fullmatch(r"[0-9]{1,12}", ts):
            raise AuthError("登录时间无效")
        if abs(self.config.clock() - int(ts)) > self.config.sso_skew_seconds:
            raise AuthError("登录链接已过期，请重新从门户进入")
        if not isinstance(sign, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", sign):
            raise AuthError("登录签名无效")
        expected = hashlib.md5(f"{uid}|{name}|{ts}|{self.config.sso_secret}".encode()).hexdigest()
        if not hmac.compare_digest(expected, sign.lower()):
            raise AuthError("登录签名无效")
        return AuthUser(uid, name)

    def issue_token(self, user: AuthUser) -> str:
        if not self.configured:
            raise AuthError("登录服务尚未配置")
        payload = {"aud": "xunjia", "sub": user.user_id, "name": user.display_name,
                   "exp": int(self.config.clock()) + self.config.token_ttl_seconds, "v": 1}
        encoded = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        signature = hmac.new(self.config.session_secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + "." + signature

    def verify_token(self, token: str) -> AuthUser | None:
        if not self.configured or not isinstance(token, str) or len(token) > 4096:
            return None
        try:
            payload, signature = token.split(".")
            expected = hmac.new(self.config.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            if data.get("aud") != "xunjia" or data.get("v") != 1:
                return None
            if type(data.get("exp")) is not int or data["exp"] <= self.config.clock():
                return None
            uid, name = data.get("sub"), data.get("name")
            if not isinstance(uid, str) or not uid or len(uid) > 160 or not isinstance(name, str):
                return None
            return AuthUser(uid, name)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None
