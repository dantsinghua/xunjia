"""进程内 STS 凭据；SM4 仅用于认证令牌，不参与 OSS 对象密封。"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_STS_URL = "https://nytoken.yingmi.com/api/sts"
MQ_STS_URL = "http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts"
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_EXPIRATION = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


class CredentialsError(ValueError):
    """安全的认证状态；不包含服务响应、密钥或底层异常。"""


@dataclass(frozen=True)
class CredentialSet:
    access_id: str = field(repr=False)
    access_secret: str = field(repr=False)
    security_token: str = field(repr=False)
    expiration: datetime
    bucket: str = ""


def build_access_token(auth_key: str, sm4_key: bytes, timestamp: int, iv: bytes | None = None) -> str:
    if not isinstance(auth_key, str) or not auth_key:
        raise CredentialsError("STS 认证口令未配置")
    if not isinstance(sm4_key, bytes) or len(sm4_key) != 16:
        raise CredentialsError("STS SM4 密钥必须为 16 字节")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        raise CredentialsError("STS 时间戳无效")
    iv = os.urandom(16) if iv is None else iv
    if not isinstance(iv, bytes) or len(iv) != 16:
        raise CredentialsError("STS IV 必须为 16 字节")
    plain = json.dumps({"key": auth_key, "ts": str(timestamp)}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.SM4(sm4_key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + encrypted).decode("ascii")


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    """不把认证头转发给重定向目标。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoCredentialRedirect())
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(65537)
            if len(body) > 65536:
                raise CredentialsError("STS 响应无效")
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code), b""
    except (OSError, urllib.error.URLError):
        raise CredentialsError("STS 暂不可用") from None


class STSCredentials:
    def __init__(self, *, auth_key: str, sm4_key: bytes, url: str = DEFAULT_STS_URL,
                 transport: Transport | None = None, clock: Callable[[], float] = time.time,
                 refresh_window: float = 300, request_budget: float = 65,
                 retry_backoff: float = 5, timeout: float = 20,
                 expected_bucket: str | None = None):
        if not isinstance(auth_key, str) or not auth_key:
            raise CredentialsError("STS 认证口令未配置")
        if not isinstance(sm4_key, bytes) or len(sm4_key) != 16:
            raise CredentialsError("STS SM4 密钥必须为 16 字节")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" and url != MQ_STS_URL) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise CredentialsError("STS 地址须为 HTTPS，或显式指定的 MQ 内网完整接口")
        if min(refresh_window, request_budget) < 0 or min(retry_backoff, timeout) <= 0:
            raise CredentialsError("STS 刷新参数无效")
        self._auth_key, self._sm4_key = auth_key, sm4_key
        self._url, self._transport, self._clock = url, transport or http_get, clock
        self._refresh_window, self._request_budget = refresh_window, request_budget
        self._retry_backoff, self._timeout = retry_backoff, timeout
        self._lock = threading.Lock()
        self._snapshot: CredentialSet | None = None
        self._next_attempt = 0.0
        self._retry_after = 0.0
        self._generation = 0
        self._refresh_failed = False

    def _usable(self, now: float, budget: float) -> bool:
        return self._snapshot is not None and self._snapshot.expiration.timestamp() > now + budget

    def _fetch(self) -> CredentialSet:
        token = build_access_token(self._auth_key, self._sm4_key, int(self._clock()))
        try:
            status, body = self._transport(self._url, {"access_token": token}, self._timeout)
        except Exception:
            raise CredentialsError("STS 暂不可用") from None
        if status != 200:
            raise CredentialsError("STS 认证失败" if status in (401, 403) else "STS 暂不可用")
        try:
            data = json.loads(body)
            fields = [data[name] for name in ("access_key_id", "access_key_secret", "security_token", "expiration")]
            if any(not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value for value in fields):
                raise ValueError
            access_id, access_secret, security_token, raw_expiration = fields
            if not _EXPIRATION.fullmatch(raw_expiration):
                raise ValueError
            expiration = datetime.fromisoformat(raw_expiration.replace("Z", "+00:00")).astimezone(timezone.utc)
            return CredentialSet(access_id, access_secret, security_token, expiration)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise CredentialsError("STS 凭据响应无效") from None

    def get(self, *, force: bool = False, min_validity: float | None = None) -> CredentialSet:
        budget = max(self._request_budget, min_validity or 0)
        generation = self._generation
        # 锁覆盖请求，等待者重新检查同一缓存；失败也受 next_attempt 限流。
        with self._lock:
            now = self._clock()
            usable = self._usable(now, budget)
            changed = generation != self._generation
            if usable and (changed or ((not force or self._refresh_failed) and now < self._next_attempt)):
                return self._snapshot
            if not usable and now < self._retry_after:
                raise CredentialsError("STS 凭据不可用或即将过期")
            try:
                fresh = self._fetch()
                now = self._clock()
                if fresh.expiration.timestamp() <= now + budget:
                    raise CredentialsError("STS 凭据不可用或即将过期")
            except CredentialsError:
                self._next_attempt = self._clock() + self._retry_backoff
                self._retry_after = self._next_attempt
                self._generation += 1
                self._refresh_failed = True
                if self._usable(self._clock(), budget):
                    return self._snapshot
                raise
            self._snapshot = fresh
            self._generation += 1
            self._refresh_failed = False
            self._retry_after = now + self._retry_backoff
            self._next_attempt = max(self._retry_after, fresh.expiration.timestamp() - self._refresh_window)
            return fresh

    def invalidate(self, snapshot: CredentialSet) -> None:
        """只失效服务端明确拒绝的那批；不撤销另一个线程的新凭据。"""
        with self._lock:
            if self._snapshot is snapshot:
                self._snapshot = None
                self._next_attempt = 0.0
                self._retry_after = 0.0
                self._refresh_failed = False


_env_lock = threading.Lock()
_env_config: tuple[str, ...] | None = None
_env_credentials: STSCredentials | None = None


def credentials_from_env() -> STSCredentials:
    """同一配置共享缓存。SM4 环境值按 UTF-8 原文字节，不猜 hex/base64。"""
    global _env_config, _env_credentials
    config = (os.environ.get("XUNJIA_STS_AUTH_KEY", ""), os.environ.get("XUNJIA_STS_SM4_KEY", ""),
              os.environ.get("XUNJIA_STS_URL", DEFAULT_STS_URL))
    with _env_lock:
        if _env_credentials is None or _env_config != config:
            auth_key, sm4_key, url = config
            provider = STSCredentials(auth_key=auth_key, sm4_key=sm4_key.encode("utf-8"), url=url,)
            _env_credentials, _env_config = provider, config
        return _env_credentials
