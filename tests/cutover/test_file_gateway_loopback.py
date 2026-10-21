import pytest
from relay.file_gateway import FileGatewayClient,GatewayAuth,FileGatewayError
from file_gateway_fakes import AUTH,NOW
from file_gateway_http_fixture import gateway_http

def test_real_http_multipart_binary_utf8_and_fifo_contract():
    with gateway_http() as (g,base):
        c=FileGatewayClient(base+"/up",AUTH,allow_http=True,clock=lambda:NOW)
        assert c.pending()==0
        name="\u6d4b\u8bd5.bin";body=b"\x00\xffsynthetic\r\n"
        assert c.upload(name,body)["file_name"]==name
        c.upload("next.bin",b"next");assert c.pending()==2
        one=c.download();assert one.name==name and one.content==body
        assert c.download().content==b"next"
        assert c.download() is None and c.pending()==0

@pytest.mark.parametrize("delta",[-61,61])
def test_real_http_rejects_expired_and_future_token_without_consumption(delta):
    with gateway_http() as (g,base):
        c=FileGatewayClient(base+"/up",AUTH,allow_http=True,clock=lambda:NOW+delta)
        with pytest.raises(FileGatewayError) as exc:c.pending()
        assert exc.value.code=="FILE_AUTH_REJECTED" and not g.queues["up.invalid"]

def test_real_http_rejects_wrong_business_key():
    with gateway_http() as (g,base):
        c=FileGatewayClient(base+"/up",GatewayAuth("wrong-synthetic",AUTH.sm4_key),allow_http=True,clock=lambda:NOW)
        with pytest.raises(FileGatewayError) as exc:c.upload("own.bin",b"own")
        assert exc.value.code=="FILE_AUTH_REJECTED" and not g.queues["up.invalid"]

def test_loopback_has_explicitly_independent_direction_queues():
    with gateway_http() as (g,base):
        up=FileGatewayClient(base+"/up",AUTH,allow_http=True,clock=lambda:NOW)
        down=FileGatewayClient(base+"/down",AUTH,allow_http=True,clock=lambda:NOW)
        up.upload("up.bin",b"up");assert down.pending()==0 and up.pending()==1
        assert up.download().content==b"up"
