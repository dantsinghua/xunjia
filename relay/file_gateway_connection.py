"""Load one existing file gateway connection, including an optional local DNS pin."""
from __future__ import annotations

import http.client
import hashlib
import ipaddress
import json
import ssl
from pathlib import Path
import urllib.parse
import urllib.request

from .file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth, NoRedirect


TLS_FIELDS = ("client_cert_file", "client_key_file", "ca_file")


def resolve_tls_files(config, directory):
    """Resolve deployment-relative paths without ever reading or returning PEM values."""
    result = dict(config)
    for field in TLS_FIELDS:
        value = result.get(field)
        if value in (None, ""):
            result.pop(field, None)
            continue
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise FileGatewayError("FILE_TLS_PATH_INVALID")
        path = Path(value).expanduser()
        result[field] = str(path if path.is_absolute() else Path(directory) / path)
    return result


def client_tls_context(config):
    """Client authentication supplements, and never replaces, server verification."""
    cert, key, ca = (config.get(field) for field in TLS_FIELDS)
    if bool(cert) != bool(key):
        raise FileGatewayError("FILE_TLS_CERT_PAIR_REQUIRED")
    if not cert or not key:
        raise FileGatewayError("FILE_TLS_CLIENT_CERT_REQUIRED")
    for value in (cert, key, ca):
        if not value:
            continue
        try:
            path = Path(value)
            if not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024:
                raise OSError()
        except (OSError, TypeError, ValueError):
            raise FileGatewayError("FILE_TLS_FILE_UNAVAILABLE") from None
    try:
        context = ssl.create_default_context(cafile=ca or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (OSError, ValueError, ssl.SSLError):
        raise FileGatewayError("FILE_TLS_CA_INVALID") from None
    if cert:
        try:
            # Never prompt a headless service for an encrypted-key password.
            context.load_cert_chain(cert, key, password=lambda: "")
        except (OSError, ValueError, ssl.SSLError):
            raise FileGatewayError("FILE_TLS_CLIENT_CERT_INVALID") from None
    return context


def protected_auth_path(config_path, value):
    path = Path(value)
    if not path.is_absolute():
        path = Path(config_path).parent / path
    if path.stat().st_mode & 0o077:
        raise ValueError('文件中转凭据文件须仅当前部署用户可读（0600）')
    return path


def pinned_opener(config, *, context=None):
    """Keep the URL host (and TLS identity); override only this client's socket target."""
    host = urllib.parse.urlsplit(config['base_url']).hostname
    addresses = config.get('addresses')
    if config.get('hostname') != host or not addresses:
        return None
    if not isinstance(addresses, list):
        raise ValueError('文件中转地址映射无效')
    address = str(ipaddress.ip_address(addresses[0]))

    def factory(connection_class):
        def connection(target, **kwargs):
            result = connection_class(target, **kwargs)
            original = result._create_connection

            def connect(destination, *args, **kw):
                return original((address if destination[0] == host else destination[0], destination[1]), *args, **kw)
            result._create_connection = connect
            return result
        return connection

    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(factory(http.client.HTTPConnection), request)

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(factory(http.client.HTTPSConnection), request, context=self._context)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), HTTP(), HTTPS(context=context))


def connection_client(config, auth, *, path=None):
    if "allow_http" in config:
        raise FileGatewayError("FILE_GATEWAY_CONFIG_INVALID")
    if urllib.parse.urlsplit(config["base_url"]).scheme != "https":
        raise FileGatewayError("FILE_TLS_HTTPS_REQUIRED")
    context = client_tls_context(config)
    return FileGatewayClient(config['base_url'], auth, timeout=10,
                             opener=pinned_opener(config, context=context), path=path, ssl_context=context)


def connection_identity(client):
    # SM4 rotation preserves the logical queue; a different business key does not.
    return hashlib.sha256(json.dumps([client.queue_identity, client.auth.key],
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def load_connection(path, *, raw=None):
    path = Path(path)
    if path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
        raise ValueError('文件中转连接配置文件权限或大小无效')
    raw = path.read_bytes() if raw is None else raw
    if len(raw) > 65536:
        raise ValueError('文件中转连接配置文件过大')
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError('文件中转连接配置须为JSON对象')
    auth = GatewayAuth.from_file(protected_auth_path(path, config['auth_file']))
    return connection_client(resolve_tls_files(config, path.parent), auth)
