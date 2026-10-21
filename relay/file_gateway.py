"""its-relay file API. Fresh SM4 tokens; no client AK/SK and no implicit retries."""
from __future__ import annotations
import base64
import hashlib
from dataclasses import dataclass, field
from email.message import Message
import http.client
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

class FileGatewayError(RuntimeError):
    def __init__(self, code, *, status=None, ambiguous=False):
        self.code, self.status, self.ambiguous = code, status, ambiguous
        super().__init__(code)

@dataclass(frozen=True)
class GatewayAuth:
    key: str = field(repr=False)
    sm4_key: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key or len(self.key.encode()) > 4096:
            raise FileGatewayError("FILE_AUTH_KEY_INVALID")
        if not isinstance(self.sm4_key, bytes) or len(self.sm4_key) != 16:
            raise FileGatewayError("FILE_AUTH_SM4_LENGTH_INVALID")

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        try:
            if not path.is_file() or path.stat().st_size > 16384:
                raise FileGatewayError("FILE_AUTH_CONFIG_INVALID")
            text = path.read_text(encoding="utf-8-sig")
            return cls.from_yaml(text)
        except FileGatewayError:
            raise
        except Exception:
            raise FileGatewayError("FILE_AUTH_CONFIG_INVALID") from None

    @classmethod
    def from_yaml(cls, text):
        try:
            if not isinstance(text, str) or len(text.encode("utf-8")) > 16384:
                raise FileGatewayError("FILE_AUTH_CONFIG_INVALID")
            if not text.strip():
                raise FileGatewayError("FILE_AUTH_CONFIG_EMPTY")
            import yaml
            class UniqueLoader(yaml.SafeLoader):
                pass
            def mapping(loader, node, deep=False):
                pairs = loader.construct_pairs(node, deep=deep)
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate configuration key")
                    result[key] = value
                return result
            UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
            value = yaml.load(text, Loader=UniqueLoader)
            if not isinstance(value, dict):
                raise ValueError()
            auth = value.get("auth", value)
            if not isinstance(auth, dict) or not isinstance(auth.get("sm4_key"), str):
                raise ValueError()
            return cls(auth.get("key"), auth["sm4_key"].encode("utf-8"))
        except FileGatewayError:
            raise
        except Exception:
            raise FileGatewayError("FILE_AUTH_CONFIG_INVALID") from None

    def token(self, *, now=None, iv=None):
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise FileGatewayError("FILE_AUTH_TIME_INVALID")
        iv = os.urandom(16) if iv is None else iv
        if not isinstance(iv, bytes) or len(iv) != 16:
            raise FileGatewayError("FILE_AUTH_IV_INVALID")
        plain = json.dumps({"key": self.key, "ts": str(int(now))}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        cipher = Cipher(algorithms.SM4(self.sm4_key), modes.CBC(iv)).encryptor()
        return base64.b64encode(iv + cipher.update(padded) + cipher.finalize()).decode("ascii")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

@dataclass(frozen=True)
class DownloadedFile:
    name: str
    content: bytes = field(repr=False)

def filename(header):
    extended = re.search(r"(?:^|;)\s*filename\*=UTF-8\x27\x27([^;]*)", header, re.I)
    try:
        if extended:
            name = urllib.parse.unquote_to_bytes(extended[1].strip()).decode("utf-8")
        else:
            message = Message(); message["Content-Disposition"] = header
            name = message.get_filename()
        if not isinstance(name, str) or not name or len(name) > 200:
            raise ValueError()
        if any(ord(c) < 32 or ord(c) == 127 for c in name) or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError()
        return name
    except (ValueError, UnicodeError, TypeError):
        raise FileGatewayError("FILE_DOWNLOAD_NAME_INVALID", ambiguous=True) from None

class FileGatewayClient:
    def __init__(self, base_url, auth, *, allow_http=False, timeout=15, max_bytes=4*1024*1024, opener=None, clock=time.time, path=None):
        if type(allow_http) is not bool or not isinstance(base_url, str):
            raise FileGatewayError("FILE_GATEWAY_CONFIG_INVALID")
        try:
            url = urllib.parse.urlsplit(base_url); port = url.port
            if url.scheme not in (("https", "http") if allow_http else ("https",)) or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError()
            if any(c.isspace() for c in base_url) or ".." in url.path.split("/") or "%" in url.path:
                raise ValueError()
            if url.path.rstrip("/").endswith("/api/files"):
                raise ValueError()
        except ValueError:
            raise FileGatewayError("FILE_GATEWAY_URL_INVALID") from None
        if not isinstance(auth, GatewayAuth) or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise FileGatewayError("FILE_GATEWAY_CONFIG_INVALID")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1024*1024*1024:
            raise FileGatewayError("FILE_GATEWAY_SIZE_INVALID")
        if path is not None and (not isinstance(path, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,20}', path) is None):
            raise FileGatewayError('FILE_GATEWAY_PATH_INVALID')
        self.path = path
        self.base_url, self.auth, self.timeout, self.max_bytes, self.clock = base_url.rstrip("/"), auth, timeout, max_bytes, clock
        self.queue_identity = (url.scheme.lower(), url.hostname.lower(), port or (443 if url.scheme == "https" else 80), url.path.rstrip("/"))
        if path is not None:
            self.queue_identity += (path, hashlib.sha256(auth.key.encode("utf8")).hexdigest())
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _request(self, operation, body=None, content_type=None):
        mutation = operation != "pending"
        headers = {"access_token": self.auth.token(now=self.clock()), "Cache-Control": "no-store"}
        if content_type:
            headers["Content-Type"] = content_type
        suffix = '?' + urllib.parse.urlencode({'path': self.path}) if self.path is not None else ''
        request = urllib.request.Request(self.base_url + "/api/files/" + operation + suffix, data=body, headers=headers, method="POST" if operation == "upload" else "GET")
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.code if hasattr(response, "code") else response.status
                if status not in (200, 204):
                    safe = {401: "FILE_AUTH_REJECTED", 403: "FILE_ACCESS_DENIED", 413: "FILE_TOO_LARGE", 429: "FILE_RATE_LIMITED"}
                    code = safe.get(status, "FILE_DOWNLOAD_OBJECT_MISSING" if status == 404 and operation == "download" else "FILE_GATEWAY_HTTP_ERROR")
                    raise FileGatewayError(code, status=status, ambiguous=mutation and (status == 404 or status >= 500))
                if status == 204:
                    if operation != "download":
                        raise FileGatewayError("FILE_GATEWAY_RESPONSE_INVALID", status=status, ambiguous=mutation)
                    return status, dict(response.headers), b""
                limit = self.max_bytes if operation == "download" else 16384
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise FileGatewayError("FILE_GATEWAY_RESPONSE_TOO_LARGE", ambiguous=mutation)
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) != len(data)):
                    raise FileGatewayError("FILE_GATEWAY_RESPONSE_TRUNCATED", ambiguous=mutation)
                return status, dict(response.headers), data
        except FileGatewayError:
            raise
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise FileGatewayError("FILE_GATEWAY_OUTCOME_UNKNOWN" if mutation else "FILE_GATEWAY_UNAVAILABLE", ambiguous=mutation) from None

    def pending(self):
        _, _, body = self._request("pending")
        try:
            data = json.loads(body); value = data["pending"]
            if type(value) is not int or value < 0:
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError):
            raise FileGatewayError("FILE_PENDING_RESPONSE_INVALID") from None

    def upload(self, name, data):
        if not isinstance(name, str) or not name or len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name) or any(c in name for c in ("/", "\\", chr(34))):
            raise FileGatewayError("FILE_UPLOAD_NAME_INVALID")
        if not isinstance(data, bytes) or len(data) > self.max_bytes:
            raise FileGatewayError("FILE_TOO_LARGE")
        boundary = "xunjia-" + uuid.uuid4().hex
        head = ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" + name + "\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        body = head + data + ("\r\n--" + boundary + "--\r\n").encode("ascii")
        _, _, raw = self._request("upload", body, "multipart/form-data; boundary=" + boundary)
        try:
            result = json.loads(raw)
            if str(uuid.UUID(result["file_key"])) != result["file_key"].lower() or not isinstance(result["file_name"], str):
                raise ValueError()
            return {"file_key": result["file_key"], "file_name": result["file_name"]}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise FileGatewayError("FILE_UPLOAD_RESPONSE_INVALID", ambiguous=True) from None

    def download(self):
        status, headers, body = self._request("download")
        if status == 204:
            return None
        headers = {key.lower(): value for key, value in headers.items()}
        name = filename(headers.get("content-disposition", ""))
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            raise FileGatewayError("FILE_DOWNLOAD_CONTENT_TYPE_INVALID", ambiguous=True)
        return DownloadedFile(name, body)
