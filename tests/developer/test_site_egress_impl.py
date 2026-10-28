"""Local-only TCP egress checks, including verified TLS and client certificates."""
import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
from pathlib import Path
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def egress():
    assert (Path(__file__).resolve().parents[2] / 'relay/site_egress.py').is_file(), 'Site egress helper is missing'
    return importlib.import_module('relay.site_egress')


@pytest.mark.parametrize('host,port', [
    ('its-relay.yingmi.cn', 50443), ('rest.innodealing.com', 12443),
    ('bosh2.innodealing.com', 13443), ('web.innodealing.com', 11443),
])
def test_only_allowed_destinations_receive_fixed_tcp_ports(monkeypatch, host, port):
    helper = egress()
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '172.26.64.36')
    route = helper.destination('https://' + host + '/unchanged?a=1')
    assert (route.host, route.gateway, route.port) == (host, '172.26.64.36', port)
    assert helper.destination('https://dify.internal/v1') is None


@pytest.mark.parametrize('value', ['https://172.26.64.36', '172.26.64.36:50443', 'gateway.internal', '172.26.64.36/path', ' 172.26.64.36', 'fe80::1%eth0'])
def test_invalid_gateway_rejected_before_any_socket(monkeypatch, value):
    helper = egress()
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', value)
    with pytest.raises(ValueError, match='EGRESS_GATEWAY_IP_REQUIRED'):
        helper.destination('https://its-relay.yingmi.cn')


def test_wrong_known_target_port_is_rejected(monkeypatch):
    helper = egress()
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    with pytest.raises(ValueError, match='EGRESS_TARGET_PORT_FORBIDDEN'):
        helper.destination('https://rest.innodealing.com:444/path')


def test_no_gateway_keeps_direct_url_and_explicit_websocket_proxy(monkeypatch):
    helper = egress()
    monkeypatch.delenv('XUNJIA_EGRESS_GATEWAY', raising=False)
    assert helper.destination('https://rest.innodealing.com') is None
    assert helper.websocket_options('wss://bosh2.innodealing.com/ws', 'http://proxy.local:8080') == {'proxy': 'http://proxy.local:8080'}


def test_gateway_and_explicit_websocket_proxy_conflict(monkeypatch):
    helper = egress()
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    with pytest.raises(ValueError, match='EGRESS_WS_PROXY_CONFLICT'):
        helper.websocket_options('wss://bosh2.innodealing.com/ws', 'http://proxy.local:8080')


def test_gateway_rejects_pinned_or_custom_file_opener(monkeypatch):
    egress()
    from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    with pytest.raises(FileGatewayError, match='EGRESS_OPENER_CONFLICT'):
        FileGatewayClient('https://its-relay.yingmi.cn', GatewayAuth('synthetic', b'0' * 16), opener=object())


def material(root, server_name):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-egress-ca')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
          .not_valid_after(now + timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
          .sign(ca_key, hashes.SHA256()))
    ca_path = root / 'ca.pem'
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    output = {'ca': ca_path}
    for role, name, purpose in [('server', server_name, ExtendedKeyUsageOID.SERVER_AUTH),
                                 ('client', 'synthetic-client', ExtendedKeyUsageOID.CLIENT_AUTH)]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False)
                .add_extension(x509.ExtendedKeyUsage([purpose]), False).sign(ca_key, hashes.SHA256()))
        cert_path, key_path = root / (role + '.pem'), root / (role + '.key')
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        output[role] = (cert_path, key_path)
    return output


@contextmanager
def local_tls(root, hostname, *, mtls=False):
    files = material(root, hostname)
    seen = {'sni': [], 'requests': []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            seen['requests'].append({'host': self.headers.get('Host'), 'path': self.path,
                                     'peer': self.connection.getpeercert()})
            body = b'{"pending":0}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            seen['requests'].append({'host': self.headers.get('Host'), 'path': self.path,
                                     'origin': self.headers.get('Origin'), 'referer': self.headers.get('Referer')})
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            body = b'{"data":{"t":"synthetic-token"}}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*files['server'])
    context.set_servername_callback(lambda connection, name, ctx: seen['sni'].append(name))
    if mtls:
        context.load_verify_locations(files['ca'])
        context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    thread.start()
    try:
        yield server.server_port, files, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def redirect_local_port(monkeypatch, mapped_port, local_port):
    real = socket.create_connection
    seen = []
    def connect(address, *args, **kwargs):
        assert address == ('127.0.0.1', mapped_port), 'Unexpected network destination'
        seen.append(address)
        return real(('127.0.0.1', local_port), *args, **kwargs)
    monkeypatch.setattr(socket, 'create_connection', connect)
    return seen


def test_file_gateway_real_tls_preserves_sni_host_and_client_certificate(tmp_path, monkeypatch):
    egress()
    from relay.file_gateway import FileGatewayClient, GatewayAuth
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    with local_tls(tmp_path, 'its-relay.yingmi.cn', mtls=True) as (port, files, seen):
        connections = redirect_local_port(monkeypatch, 50443, port)
        context = ssl.create_default_context(cafile=str(files['ca']))
        context.load_cert_chain(*files['client'])
        client = FileGatewayClient('https://its-relay.yingmi.cn', GatewayAuth('synthetic-key', b'0' * 16), ssl_context=context)
        assert client.pending() == 0
        assert connections == [('127.0.0.1', 50443)]
        assert seen['sni'] == ['its-relay.yingmi.cn']
        assert seen['requests'][0]['host'] == 'its-relay.yingmi.cn'
        assert seen['requests'][0]['peer']['subject'][0][0] == ('commonName', 'synthetic-client')


def test_file_gateway_wrong_hostname_certificate_is_rejected(tmp_path, monkeypatch):
    egress()
    from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    with local_tls(tmp_path, 'wrong.example', mtls=True) as (port, files, seen):
        redirect_local_port(monkeypatch, 50443, port)
        context = ssl.create_default_context(cafile=str(files['ca']))
        context.load_cert_chain(*files['client'])
        client = FileGatewayClient('https://its-relay.yingmi.cn', GatewayAuth('synthetic-key', b'0' * 16), ssl_context=context)
        with pytest.raises(FileGatewayError, match='FILE_TLS_SERVER_CERT_INVALID'):
            client.pending()
        assert seen['requests'] == []


def test_httpx_authentication_real_tls_keeps_host_sni_origin_and_query(tmp_path, monkeypatch):
    egress()
    import httpx
    from collector import qb_gateway
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    monkeypatch.setattr(qb_gateway, 'API', 'https://rest.innodealing.com')
    real_client = httpx.Client
    with local_tls(tmp_path, 'rest.innodealing.com') as (port, files, seen):
        redirect_local_port(monkeypatch, 12443, port)
        context = ssl.create_default_context(cafile=str(files['ca']))
        def client(**kwargs):
            assert kwargs['verify'] is True and kwargs['trust_env'] is False and kwargs['proxy'] is None
            kwargs['verify'] = context
            return real_client(**kwargs)
        monkeypatch.setattr(qb_gateway.httpx, 'Client', client)
        assert qb_gateway.authenticate('sid=synthetic', 'synthetic-device')['t'] == 'synthetic-token'
        assert seen['sni'] == ['rest.innodealing.com']
        request = seen['requests'][0]
        assert request['host'] == 'rest.innodealing.com'
        assert request['path'].startswith('/onshore-auth-service/api/token/v1/web/authenticate?')
        assert 'sign=' in request['path']
        assert request['origin'] == 'https://web.innodealing.com'
        assert request['referer'] == 'https://web.innodealing.com/'


def test_websocket_adapter_keeps_original_url_and_tls_identity(monkeypatch):
    egress()
    import websockets
    from server.quote_probe import market_connect
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    monkeypatch.delenv('QB_WS_PROXY', raising=False)
    calls = []
    monkeypatch.setattr(websockets, 'connect', lambda url, **kw: calls.append((url, kw)))
    uri = 'wss://bosh2.innodealing.com/ws?sign=synthetic'
    market_connect(uri, 'sid=synthetic', 'token', 8)
    url, options = calls[0]
    assert url == uri
    assert (options['host'], options['port'], options['server_hostname'], options['proxy']) == ('127.0.0.1', 13443, 'bosh2.innodealing.com', None)
    assert options['additional_headers']['Origin'] == 'https://web.innodealing.com'


def test_websocket_real_tls_uses_original_host_and_sni(tmp_path, monkeypatch):
    egress()
    import websockets
    from server.quote_probe import market_connect
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY', '127.0.0.1')
    monkeypatch.delenv('QB_WS_PROXY', raising=False)
    files = material(tmp_path, 'bosh2.innodealing.com')
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(*files['server'])
    seen = {}
    server_context.set_servername_callback(lambda connection, name, ctx: seen.update(sni=name))
    client_context = ssl.create_default_context(cafile=str(files['ca']))
    real_connect = websockets.connect
    def connect(uri, **options):
        assert options['proxy'] is None
        return real_connect(uri, ssl=client_context, close_timeout=0.2, **options)
    monkeypatch.setattr(websockets, 'connect', connect)
    async def run():
        complete = asyncio.Event()
        async def handshake(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=3)
                lines = raw.decode('ascii').split('\r\n')
                headers = dict(line.split(': ', 1) for line in lines[1:] if ': ' in line)
                seen.update(host=headers['Host'], path=lines[0], origin=headers['Origin'])
                accept = base64.b64encode(hashlib.sha1((headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                              'Sec-WebSocket-Accept: ' + accept + '\r\nSec-WebSocket-Protocol: token\r\n\r\n').encode() + b'\x81\x02ok')
                await writer.drain()
                await asyncio.wait_for(reader.read(128), timeout=3)
                writer.write(b'\x88\x02\x03\xe8')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                complete.set()
        server = await asyncio.start_server(handshake, '127.0.0.1', 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        original = loop.create_connection
        async def dial(factory, host=None, port=None, **options):
            assert (host, port) == ('127.0.0.1', 13443)
            return await original(factory, host='127.0.0.1', port=server.sockets[0].getsockname()[1], **options)
        monkeypatch.setattr(loop, 'create_connection', dial)
        try:
            async with market_connect('wss://bosh2.innodealing.com/ws?sign=synthetic', 'sid=synthetic', 'token', 3) as ws:
                assert await ws.recv() == 'ok'
            await asyncio.wait_for(complete.wait(), timeout=3)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(run())
    assert seen == {'sni': 'bosh2.innodealing.com', 'host': 'bosh2.innodealing.com',
                    'path': 'GET /ws?sign=synthetic HTTP/1.1', 'origin': 'https://web.innodealing.com'}
