"""Load one existing file gateway connection, including an optional local DNS pin."""
from __future__ import annotations

import http.client
import hashlib
import ipaddress
import json
from pathlib import Path
import urllib.parse
import urllib.request

from .file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth, NoRedirect


def protected_auth_path(config_path, value):
    path = Path(value)
    if not path.is_absolute():
        path = Path(config_path).parent / path
    if path.stat().st_mode & 0o077:
        raise ValueError('文件中转凭据文件须仅当前部署用户可读（0600）')
    return path


def pinned_opener(config):
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
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), HTTP(), HTTPS())


def connection_client(config, auth, *, allow_http=False, path=None):
    return FileGatewayClient(config['base_url'], auth, allow_http=allow_http, timeout=10,
                             opener=pinned_opener(config), path=path)


def connection_identity(client):
    # SM4 rotation preserves the logical queue; a different business key does not.
    return hashlib.sha256(json.dumps([client.queue_identity, client.auth.key],
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def load_connection(path, *, allow_http=False, raw=None):
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
    return connection_client(config, auth, allow_http=allow_http and config.get('allow_http', False))
