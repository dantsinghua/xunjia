"""开发者协议红绿测试；所有密钥/身份均为合成值。"""
import json
import pytest
from tests.protocol_crypto import open_sealed, seal
from relay.protocol import Binding, Codec, ProtocolError, envelope

B = Binding('owner-a', 'account-a', 'workbench-a', b's'*32, b'h'*32)

def packet(kind='event'):
    return envelope(B, kind, {'text':'合成内容'}, message_id='event-1', peer_id='peer-a', created_at=100, expires_at=200)

def test_existing_aes_gcm_wire_format_and_authenticated_identity():
    codec=Codec(B,clock=lambda:150)
    blob=codec.pack(packet())
    plain=json.loads(open_sealed(B.seal_key,blob))
    assert plain['account_uid']=='account-a'
    assert codec.unpack(blob)['payload']['text']=='合成内容'
    plain['peer_id']='wrong'
    with pytest.raises(ProtocolError):codec.unpack(seal(B.seal_key,json.dumps(plain).encode()))

def test_wrong_binding_tamper_expiry_and_legacy_objects_are_rejected():
    codec=Codec(B,clock=lambda:150);blob=codec.pack(packet())
    wrong=Codec(Binding('owner-b','account-a','workbench-a',b's'*32,b'h'*32),clock=lambda:150)
    with pytest.raises(ProtocolError):wrong.unpack(blob)
    with pytest.raises(ProtocolError):Codec(B,clock=lambda:201).unpack(blob)
    with pytest.raises(ProtocolError):codec.unpack(blob[:-1]+bytes([blob[-1]^1]))
    with pytest.raises(ProtocolError):codec.unpack(seal(B.seal_key,b'{"ok":true}'))

def test_historical_event_requires_real_occurrence_and_cannot_hide_as_live():
    codec=Codec(B,clock=lambda:150)
    data=packet();data['payload']['historical']=True
    data=envelope(B,'event',data['payload'],message_id='event-1',peer_id='peer-a',created_at=100,expires_at=200)
    with pytest.raises(ProtocolError):codec.pack(data)
    data=envelope(B,'event',{'event_id':'event-1','historical':True,'occurred_at':50,'text':'history'},message_id='event-1',peer_id='peer-a',created_at=100,expires_at=200)
    assert codec.unpack(codec.pack(data))['payload']['occurred_at']==50

def test_sso_owner_identifier_is_not_restricted_to_object_key_alphabet():
    owner=Binding('交易员@example.test','10001','wb',b's'*32,b'h'*32)
    codec=Codec(owner,clock=lambda:100)
    data=envelope(owner,'event',{'event_id':'e','text':'x'},message_id='e',peer_id='p',created_at=100,expires_at=200)
    assert codec.unpack(codec.pack(data))['owner_user_id']=='交易员@example.test'


def test_manual_fence_is_authenticated_control_metadata_not_native_payload():
    codec=Codec(B,clock=lambda:150)
    value={'event_id':'manual-1','peer_id':'peer-a','direction':'out','text':'manual'}
    data=envelope(B,'event',value,message_id='manual-1',peer_id='peer-a',created_at=100,expires_at=200)
    data.update(manual_fence_id='fence-1',manual_fence_version=1)
    verified=codec.unpack(codec.pack(data))
    assert verified['payload']==value and verified['manual_fence_version']==1
    for changes in ({'manual_fence_version':True},{'manual_fence_version':0},{'manual_fence_id':''},{'peer_id':'wrong-peer'}):
        with pytest.raises(ProtocolError):codec.pack({**data,**changes})
    tampered=json.loads(open_sealed(B.seal_key,codec.pack(data)));tampered['manual_fence_version']=2
    with pytest.raises(ProtocolError):codec.unpack(seal(B.seal_key,json.dumps(tampered).encode()))
