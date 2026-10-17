"""GET/PUT 共用的 OSS V1 认证边界；请求期间固定一组短期凭据。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from email.utils import formatdate
from typing import Callable

from .sts_credentials import CredentialSet, CredentialsError, STSCredentials, credentials_from_env

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]
_EXPIRED = re.compile(br"<Code>\s*(SecurityTokenExpired|ExpiredToken)\s*</Code>")


def authorization(method: str, access_id: str, access_secret: str, date: str,
                  bucket: str, key: str, *, content_type: str = "", security_token: str = "") -> str:
    canonical_headers = f"x-oss-security-token:{security_token}\n" if security_token else ""
    string_to_sign = f"{method}\n\n{content_type}\n{date}\n{canonical_headers}/{bucket}/{key}"
    digest = hmac.new(access_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return f"OSS {access_id}:{base64.b64encode(digest).decode('ascii')}"


class OSSHTTP:
    def __init__(self, *, endpoint: str, credentials: STSCredentials | CredentialSet | None = None,
                 transport: Transport | None = None, bucket: str = ""):
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
            raise CredentialsError("OSS 业务桶必须明确配置")
        self._endpoint, self._credentials, self._transport, self._bucket = endpoint, credentials, transport, bucket

    def request(self, method: str, name: str, body: bytes | None = None, timeout: float = 20) -> tuple[int, bytes]:
        from .oss_catalog import http_get, object_url
        from .oss_send import http_put

        if method not in ("GET", "PUT"):
            raise ValueError("不支持的对象存储请求")
        provider = self._credentials if self._credentials is not None else credentials_from_env()
        for attempt in range(2):
            snapshot = provider if isinstance(provider, CredentialSet) else provider.get(min_validity=timeout + 5)
            if isinstance(provider, CredentialSet) and (snapshot.expiration.tzinfo is None
                    or snapshot.expiration.timestamp() <= time.time() + timeout + 5):
                raise CredentialsError("STS 凭据不可用或即将过期")
            date = formatdate(usegmt=True)
            content_type = "application/octet-stream" if method == "PUT" else ""
            headers = {"Date": date, "Authorization": authorization(
                method, snapshot.access_id, snapshot.access_secret, date, self._bucket, name,
                content_type=content_type, security_token=snapshot.security_token)}
            if snapshot.security_token:
                headers["x-oss-security-token"] = snapshot.security_token
            if content_type:
                headers["Content-Type"] = content_type
            url = object_url(self._endpoint, self._bucket, name)
            if not url.startswith("https://"):
                raise CredentialsError("对象存储地址必须为 HTTPS")
            if self._transport is not None:
                status, payload = self._transport(method, url, headers, body, timeout)
            elif method == "GET":
                status, payload = http_get(url, headers, timeout)
            else:
                status, payload = http_put(url, headers, body, timeout)
            if status == 403 and _EXPIRED.search(payload[:65536]) and isinstance(provider, STSCredentials):
                provider.invalidate(snapshot)
                if attempt == 0:
                    continue
            return status, payload
        raise AssertionError("unreachable")
