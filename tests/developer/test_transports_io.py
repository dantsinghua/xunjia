import json
import pytest
from relay.protocol import Binding, Codec, ProtocolError, envelope, result
from server.transports import TransportRouter, TransportUnavailable
from server.receiver_ledger import ReceiverLedger
from relay.file_gateway import FileGatewayError
from tests.mq_fixtures import MQWire

B=Binding('owner-a','account-a','wb-a',b's'*32,b'h'*32)
def cmd(route='oss'):
    return dict(command_id='cmd-1',owner_user_id=B.owner_user_id,account_uid=B.account_uid,workbench_id=B.workbench_id,peer_id='peer-a',content_hash='a'*64,control_version=1,expires_at=200,text='合成询价',transport=route)
def test_router_never_falls_back_or_accepts_unconfigured_route(tmp_path):
    class Fail:
        binding=B
        def send(self,c):raise TransportUnavailable('NOT_CONFIGURED')
    class Trap:
        binding=B
        def send(self,c):pytest.fail('fallback forbidden')
    router=TransportRouter({'oss':Fail(),'mail':Trap()})
    with pytest.raises(TransportUnavailable):router.send(cmd())
    with pytest.raises(TransportUnavailable):TransportRouter({}).send(cmd())







def test_event_business_hash_ignores_arrival_transport_metadata(tmp_path):
    wire=MQWire();transport=wire.transport(B,tmp_path/'t.db',role='collector',clock=lambda:100)
    reader=wire.transport(B,tmp_path/'reader.db',role='service',clock=lambda:100)
    event={'event_id':'native-1','peer_id':'peer-a','text':'same','source_message_id':'native-1','transport':'oss','received_at':100,'object_key':'old'}
    assert transport.publish_event(event)==1
    event.update(transport='mail',received_at=101,object_key='new',mail_uid=9,uidvalidity=2)
    assert transport.publish_event(event)==1
    payload=reader.poll()[0]['envelope']['payload']
    assert not {'transport','received_at','object_key','mail_uid','uidvalidity'} & payload.keys()


def test_expiry_after_submission_is_not_proof_of_failed_native_execution(tmp_path):
    wire=MQWire();now=[100];transport=wire.transport(B,tmp_path/'t.db',role='service',clock=lambda:now[0])
    transport.send(cmd());now[0]=201
    assert transport.send(cmd())['status'] in ('pending','unknown')
    assert len(wire.uploaded)==1

def test_unknown_receipt_gets_new_readonly_query_round_without_resending_command(tmp_path):
    wire=MQWire();now=[100];transport=wire.transport(B,tmp_path/'t.db',role='service',clock=lambda:now[0])
    collector=wire.transport(B,tmp_path/'collector.db',role='collector',clock=lambda:now[0])
    transport.send(cmd());collector.publish_receipt(cmd(),result(cmd(),'unknown','no-native-proof'))
    assert transport.reconcile(cmd())['status']=='unknown'
    first=[blob for _,_,blob in wire.uploaded if Codec(B,clock=lambda:now[0]).unpack(blob)['kind']=='query']
    now[0]=140
    assert transport.reconcile(cmd())['status']=='unknown'
    later=[blob for _,_,blob in wire.uploaded if Codec(B,clock=lambda:now[0]).unpack(blob)['kind']=='query']
    assert len(first)==1 and len(later)==2
    assert sum(Codec(B,clock=lambda:now[0]).unpack(blob)['kind']=='command' for _,_,blob in wire.uploaded)==1

def test_expired_cancel_envelope_can_refresh_without_extending_original_send_authorization(tmp_path):
    wire=MQWire();now=[100];transport=wire.transport(B,tmp_path/'t.db',role='service',clock=lambda:now[0],envelope_ttl=10)
    transport.cancel(cmd())
    first=next(Codec(B,clock=lambda:100).unpack(blob) for _,_,blob in wire.uploaded if Codec(B,clock=lambda:100).unpack(blob)['kind']=='cancel')
    now[0]=111;transport.cancel(cmd())
    refreshed=next(Codec(B,clock=lambda:111).unpack(blob) for _,_,blob in reversed(wire.uploaded) if Codec(B,clock=lambda:111).unpack(blob)['kind']=='cancel')
    assert refreshed['expires_at']>first['expires_at']
    assert refreshed['payload']['expires_at']==200
    assert refreshed['command_id']==first['command_id']=='cmd-1'


def test_authenticated_contacts_and_org_roundtrip_over_both_routes(tmp_path):
    payload={'account_uid':B.account_uid,'contacts':[{'uin':'peer-a','name':'合成对手'}],'org':{'departments':[{'id':'d','name':'部门'}],'members':[{'uin':'peer-a','department_id':'d'}]}}
    wire=MQWire();oss=wire.transport(B,tmp_path/'o.db',role='collector',clock=lambda:100)
    mq_reader=wire.transport(B,tmp_path/'mq-reader.db',role='service',clock=lambda:100)
    oss.publish_contacts(payload);assert mq_reader.contacts()==payload
