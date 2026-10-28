"""Fixed TCP passthrough routes; HTTP and TLS identities remain the original host.

No proxy discovery, DNS mutation, certificate bypass, or site-specific credentials.
The optional gateway is an IP literal, never an HTTP proxy URL.
"""
from dataclasses import dataclass
import http.client
import ipaddress
import os
import urllib.parse
import urllib.request


_PORTS = {
    'its-relay.yingmi.cn': 50443,
    'rest.innodealing.com': 12443,
    'bosh2.innodealing.com': 13443,
    'web.innodealing.com': 11443,
}


class EgressError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Destination:
    host: str
    gateway: str
    port: int


def gateway_ip(value=None):
    value = os.environ.get('XUNJIA_EGRESS_GATEWAY', '') if value is None else value
    if value == '':
        return ''
    if not isinstance(value, str) or value != value.strip() or '%' in value:
        raise EgressError('EGRESS_GATEWAY_IP_REQUIRED')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise EgressError('EGRESS_GATEWAY_IP_REQUIRED') from None


def destination(url, gateway=None):
    gateway = gateway_ip(gateway)
    if not gateway:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
    except (TypeError, ValueError):
        raise EgressError('EGRESS_TARGET_URL_INVALID') from None
    if host not in _PORTS:
        return None
    try:
        port = parsed.port
    except ValueError:
        raise EgressError('EGRESS_TARGET_PORT_FORBIDDEN') from None
    if port is not None and port != 443:
        raise EgressError('EGRESS_TARGET_PORT_FORBIDDEN')
    if parsed.scheme not in ('https', 'wss'):
        raise EgressError('EGRESS_TARGET_TLS_REQUIRED')
    if parsed.username is not None or parsed.password is not None:
        raise EgressError('EGRESS_TARGET_AUTHORITY_INVALID')
    return Destination(host, gateway, _PORTS[host])


class SiteHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, route=None, **kwargs):
        super().__init__(host, **kwargs)
        if route is not None:
            if self.host.lower() != route.host or self.port != 443:
                raise EgressError('EGRESS_CONNECTION_IDENTITY_MISMATCH')
            original = self._create_connection
            # HTTPSConnection.connect still passes self.host to wrap_socket;
            # only the underlying TCP address is replaced, including for mTLS.
            self._create_connection = lambda address, *args, **kw: original((route.gateway, route.port), *args, **kw)


class SiteHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, context=None, gateway=None):
        self.gateway = gateway_ip(gateway)
        super().__init__(context=context)

    def https_open(self, request):
        route = destination(request.full_url, self.gateway)
        def connection(host, **kwargs):
            return SiteHTTPSConnection(host, route=route, **kwargs)
        return self.do_open(connection, request, context=self._context)


def httpx_request(url, headers):
    route = destination(url)
    if route is None:
        return url, headers, {}
    parsed = urllib.parse.urlsplit(url)
    gateway = '[' + route.gateway + ']' if ':' in route.gateway else route.gateway
    dial_url = urllib.parse.urlunsplit(parsed._replace(netloc=f'{gateway}:{route.port}'))
    original_headers = {key: value for key, value in headers.items() if key.lower() != 'host'}
    original_headers['Host'] = parsed.netloc
    # httpcore uses this extension for both SNI and certificate hostname checking.
    return dial_url, original_headers, {'sni_hostname': route.host}


def websocket_options(url, proxy=None):
    gateway = gateway_ip()
    if gateway and proxy:
        raise EgressError('EGRESS_WS_PROXY_CONFLICT')
    route = destination(url, gateway)
    if route is None:
        # Explicit QB_WS_PROXY remains supported. Ambient proxy variables are
        # not trusted for this credential-bearing connection.
        return {'proxy': proxy or None}
    return {'host': route.gateway, 'port': route.port,
            'server_hostname': route.host, 'proxy': None}
