import json
import pytest
from relay.protocol import Binding, Codec, ProtocolError, envelope, result
from server.transports import MailTransport, MailConfig, TransportRouter, TransportUnavailable
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

class Mailboxes:
    def __init__(self):self.boxes={};self.epoch=1;self.sent=0;self.lose=False
    def smtp(self,cfg):
        parent=self
        class SMTP:
            def login(self,*args):pass
            def send_message(self,msg,*,from_addr,to_addrs):
                assert from_addr==cfg.outbound_from and tuple(to_addrs)==cfg.outbound_to
                parent.sent+=1
                for address in to_addrs:
                    box=parent.boxes.setdefault(address,{});box[max(box,default=0)+1]=msg.as_bytes()
                if parent.lose:raise TimeoutError()
                return {}
            def quit(self):pass
        return SMTP()
    def imap(self,cfg):
        parent=self
        class IMAP:
            def login(self,*args):pass
            def select(self,mailbox,readonly=False):assert readonly;return 'OK',[b'1']
            def response(self,name):return name,[str(parent.epoch).encode()]
            def uid(self,action,*args):
                box=parent.boxes.get(cfg.imap_user,{})
                if action=='search':
                    start=int(args[-1].split()[1].split(':')[0]);return 'OK',[b' '.join(str(i).encode() for i in box if i>=start)]
                uid=int(args[0]);raw=box[uid]
                if args[1]=='(RFC822.SIZE)':return 'OK',[f'{uid} (RFC822.SIZE {len(raw)})'.encode()]
                return 'OK',[(b'BODY',raw),b')']
            def logout(self):pass
        return IMAP()

def mailcfg(outer=False):
    own,other=('outer@example.test','inner@example.test') if outer else ('inner@example.test','outer@example.test')
    return MailConfig('smtp.example.test',own,'test-only','imap.example.test',own,'test-only',own,(other,),(other,),(own,))

def test_real_mail_adapter_calls_smtp_imap_and_native_receipt_required(tmp_path):
    boxes=Mailboxes();inner=MailTransport(B,mailcfg(),tmp_path/'i.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    outer=MailTransport(B,mailcfg(True),tmp_path/'o.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    c=cmd('mail');assert inner.send(c)['status']=='accepted'
    rows=outer.poll();assert len(rows)==1 and rows[0]['kind']=='command'
    calls=[];ledger=ReceiverLedger(tmp_path/'native.db',B,clock=lambda:100)
    outcome=ledger.execute(rows[0]['envelope']['payload'],lambda c:(calls.append(c['command_id']) or {'status':'sent','evidence':'native-mail-1'}))
    outer.ack(rows[0]['source_token']);outer.publish_receipt(c,outcome)
    assert inner.reconcile(c)['status']=='sent'
    assert calls==['cmd-1']

def test_mail_uidvalidity_restart_bad_mail_quarantine_and_stable_event_dedup(tmp_path):
    from email.message import EmailMessage
    boxes=Mailboxes();cfg=mailcfg();outer=MailTransport(B,mailcfg(True),tmp_path/'o.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    bad=EmailMessage();bad['From']='attacker@example.test';bad['To']=cfg.outbound_from;bad.set_content('bad')
    boxes.boxes[cfg.imap_user]={1:bad.as_bytes()}
    outer.publish_event({'event_id':'native-1','peer_id':'peer-a','text':'same','source_message_id':'native-1'})
    inner=MailTransport(B,cfg,tmp_path/'i.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    rows=inner.poll();assert len(rows)==1 and rows[0]['kind']=='event'
    assert len(inner.poll())==1
    inner.ack(rows[0]['source_token']);assert inner.poll()==[]
    boxes.epoch=2
    assert MailTransport(B,cfg,tmp_path/'i.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100).poll()==[]
    with inner.store.db() as db:assert db.execute("SELECT count(*) FROM tx_source WHERE kind='quarantine'").fetchone()[0]==2

def test_smtp_response_loss_is_unknown_and_no_resend_under_same_id(tmp_path):
    boxes=Mailboxes();boxes.lose=True
    inner=MailTransport(B,mailcfg(),tmp_path/'i.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    assert inner.send(cmd('mail'))['status']=='unknown'
    assert inner.send(cmd('mail'))['status']=='unknown'
    assert boxes.sent==1

def test_missing_mail_configuration_and_attachment_path_are_rejected(tmp_path):
    from email.message import EmailMessage
    with pytest.raises(TransportUnavailable):MailConfig.from_env({})
    transport=MailTransport(B,mailcfg(),tmp_path/'i.db',clock=lambda:100)
    msg=EmailMessage();msg['From']='outer@example.test';msg['To']='inner@example.test';msg.set_content('bad')
    msg.add_attachment(b'payload',maintype='application',subtype='octet-stream',filename='../xunjia-v2.bin')
    with pytest.raises(ProtocolError):transport._parse(msg.as_bytes())

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

def test_receipt_smtp_failure_is_durable_and_flushable(tmp_path):
    boxes=Mailboxes();boxes.lose=True;cfg=mailcfg(True);path=tmp_path/'o.db'
    outer=MailTransport(B,cfg,path,smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    with pytest.raises(TimeoutError):outer.publish_receipt(cmd('mail'),result(cmd('mail'),'sent','native-proof'))
    boxes.lose=False
    recovered=MailTransport(B,cfg,path,smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:101)
    assert recovered.flush_publications()==1
    inner=MailTransport(B,mailcfg(),tmp_path/'i.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:101)
    rows=inner.poll();assert len(rows)==1 and rows[0]['kind']=='receipt'

def test_authenticated_contacts_and_org_roundtrip_over_both_routes(tmp_path):
    payload={'account_uid':B.account_uid,'contacts':[{'uin':'peer-a','name':'合成对手'}],'org':{'departments':[{'id':'d','name':'部门'}],'members':[{'uin':'peer-a','department_id':'d'}]}}
    wire=MQWire();oss=wire.transport(B,tmp_path/'o.db',role='collector',clock=lambda:100)
    mq_reader=wire.transport(B,tmp_path/'mq-reader.db',role='service',clock=lambda:100)
    oss.publish_contacts(payload);assert mq_reader.contacts()==payload
    boxes=Mailboxes();publisher=MailTransport(B,mailcfg(True),tmp_path/'mo.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    reader=MailTransport(B,mailcfg(),tmp_path/'mi.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    publisher.publish_contacts(payload);assert reader.contacts()==payload
    with pytest.raises(ProtocolError):oss.publish_contacts({**payload,'account_uid':'other'})


def test_event_flush_never_silently_changes_the_selected_route(tmp_path):
    wire=MQWire();wire.lose_upload=True;boxes=Mailboxes();path=tmp_path/'shared.db'
    oss=wire.transport(B,path,role='collector',clock=lambda:100)
    mail=MailTransport(B,mailcfg(True),path,smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:100)
    with pytest.raises(FileGatewayError):oss.publish_event({'event_id':'native-route','peer_id':'peer-a','text':'x'})
    assert mail.flush_events()==0 and boxes.sent==0

