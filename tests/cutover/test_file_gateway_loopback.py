"""All network round-trips now require mutual TLS; plaintext fixture retired."""
from test_production_mtls import pki, wire, AUTH
from relay.file_gateway_connection import connection_client

def test_real_socket_pending_upload_download(wire):
    cfg,state=wire;client=connection_client(cfg,AUTH,path='own-test-queue')
    assert client.pending()==0
    body='synthetic multilingual bytes: \u4e2d\u6587'.encode()
    client.upload('probe.bin',body)
    assert client.pending()==1
    got=client.download();assert got.content==body and got.name=='probe.bin'
    assert client.pending()==0 and client.download() is None
    assert all(r['client'] for r in state['requests'])
