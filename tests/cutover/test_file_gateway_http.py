"""The attached HTML, not the obsolete STS response, defines these contracts."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from relay.file_gateway import GatewayAuth,FileGatewayClient,FileGatewayError,filename
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from file_gateway_fakes import AUTH,NOW,Gateway,Response,decode_token

def test_token_is_documented_sm4_cbc_iv_pkcs7_base64_with_string_seconds():
    iv=b"v"*16;token=AUTH.token(now=NOW,iv=iv)
    plain=json.dumps({'key':AUTH.key,'ts':str(NOW)},separators=(',',':')).encode()
    pad=padding.PKCS7(128).padder();padded=pad.update(plain)+pad.finalize()
    encrypt=Cipher(algorithms.SM4(AUTH.sm4_key),modes.CBC(iv)).encryptor()
    assert token==base64.b64encode(iv+encrypt.update(padded)+encrypt.finalize()).decode()
    assert decode_token(token)=={"key":AUTH.key,"ts":str(NOW)}
    assert base64.b64decode(token,validate=True)[:16]==iv
    assert AUTH.key not in repr(AUTH) and AUTH.sm4_key.decode() not in repr(AUTH)

def test_every_http_operation_uses_a_new_token_and_no_ak_sk():
    gateway=Gateway();client=gateway.client("up")
    assert client.pending()==0
    published=client.upload("synthetic.txt",b"synthetic bytes")
    assert published["file_name"]=="synthetic.txt" and client.pending()==1
    received=client.download();assert received.content==b"synthetic bytes" and received.name=="synthetic.txt"
    assert client.download() is None and client.pending()==0
    assert len(set(gateway.tokens))==len(gateway.tokens)==6
    assert all(decode_token(t)["ts"]==str(NOW) for t in gateway.tokens)

def test_empty_queue_is_not_an_empty_file():
    g=Gateway();c=g.client("up");c.upload("empty.bin",b"")
    assert c.download().content==b""
    assert c.download() is None

@pytest.mark.parametrize("nested",[True,False])
def test_flat_and_documented_nested_yaml(tmp_path,nested):
    value={"key":AUTH.key,"sm4_key":AUTH.sm4_key.decode(),"token_ttl_seconds":60}
    p=tmp_path/"private.yaml";p.write_text(json.dumps({"auth":value} if nested else value))
    assert GatewayAuth.from_file(p)==AUTH

@pytest.mark.parametrize("text,code",[("","FILE_AUTH_CONFIG_EMPTY"),("null","FILE_AUTH_CONFIG_INVALID"),("key: x\nkey: y\nsm4_key: 0123456789abcdef","FILE_AUTH_CONFIG_INVALID"),("key: synthetic\nsm4_key: short","FILE_AUTH_SM4_LENGTH_INVALID")])
def test_invalid_private_config_is_precise_without_echoing_content(tmp_path,text,code):
    p=tmp_path/"private.yaml";p.write_text(text)
    with pytest.raises(FileGatewayError) as error:GatewayAuth.from_file(p)
    assert error.value.code==code and "synthetic" not in str(error.value)

@pytest.mark.parametrize("url",["http://up.invalid","https://user:secret@up.invalid","https://up.invalid?token=x","https://up.invalid/#frag","https://up.invalid/api/files","https://up.invalid/a/../b"])
def test_unsafe_or_wrong_base_url_rejected(url):
    with pytest.raises(FileGatewayError):FileGatewayClient(url,AUTH)

def test_http_is_an_explicit_configuration_opt_in():
    assert FileGatewayClient("http://127.0.0.1:8888",AUTH,allow_http=True).base_url.endswith(":8888")

@pytest.mark.parametrize("status,code",[(401,"FILE_AUTH_REJECTED"),(403,"FILE_ACCESS_DENIED"),(413,"FILE_TOO_LARGE"),(429,"FILE_RATE_LIMITED"),(502,"FILE_GATEWAY_HTTP_ERROR")])
def test_statuses_are_classified_without_response_echo(status,code):
    g=Gateway();g.forced_status=status
    with pytest.raises(FileGatewayError) as err:g.client("up").upload("own.bin",b"own")
    assert err.value.code==code and err.value.status==status
    assert err.value.ambiguous==(status>=500)
    assert len(g.calls)==1

def test_upload_response_lost_is_unknown_and_not_retried():
    g=Gateway();g.lose_upload=True
    with pytest.raises(FileGatewayError) as err:g.client("up").upload("own.bin",b"own")
    assert err.value.ambiguous and len(g.calls)==1 and len(g.queues["up.invalid"])==1
    assert "secret" not in str(err.value)

def test_download_response_lost_is_consumption_unknown_and_not_retried():
    g=Gateway();g.queues["up.invalid"].append(("own.bin",b"own"));g.lose_download=True
    with pytest.raises(FileGatewayError) as err:g.client("up").download()
    assert err.value.ambiguous and len(g.calls)==1 and not g.queues["up.invalid"]

def test_missing_oss_object_is_reported_as_consumed_not_as_empty():
    g=Gateway();g.forced_status=404
    with pytest.raises(FileGatewayError) as err:g.client("up").download()
    assert err.value.code=="FILE_DOWNLOAD_OBJECT_MISSING" and err.value.ambiguous

@pytest.mark.parametrize("name",["../bad.bin","C:\\bad.bin","a\r\nb.bin", "a\"b", "x"*201])
def test_bad_upload_filename_never_reaches_network(name):
    g=Gateway()
    with pytest.raises(FileGatewayError):g.client("up").upload(name,b"x")
    assert not g.calls

def test_upload_maximum_is_checked_before_network():
    g=Gateway()
    with pytest.raises(FileGatewayError):g.client("up",max_bytes=3).upload("own.bin",b"1234")
    assert not g.calls

def test_extended_utf8_filename_has_priority_over_ascii_fallback():
    assert filename("attachment; filename=\"fallback\"; filename*=UTF-8\x27\x27%E6%B5%8B%E8%AF%95.bin")=="\u6d4b\u8bd5.bin"

@pytest.mark.parametrize("header",["", "attachment; filename=\"../oops\"", "attachment; filename*=UTF-8\x27\x27%FF"])
def test_invalid_download_name_is_not_a_filesystem_path(header):
    with pytest.raises(FileGatewayError):filename(header)

def test_response_length_and_media_type_are_checked():
    for response in [Response(200,b"data",{"Content-Length":"7"}),Response(200,b"oops",{"Content-Type":"text/html","Content-Disposition":"attachment; filename=\"x.bin\""})]:
        c=FileGatewayClient("https://up.invalid",AUTH,opener=SimpleNamespace(open=lambda *a,**k:response))
        with pytest.raises(FileGatewayError) as err:c.download()
        assert err.value.ambiguous

def test_no_redirect_handler_never_forwards_an_access_token():
    from relay.file_gateway import NoRedirect
    assert NoRedirect().redirect_request(None,None,302,"redirect",{},"https://other.invalid")==None
