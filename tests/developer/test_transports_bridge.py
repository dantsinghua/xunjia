"""Backend dispatcher -> authenticated transport -> collector -> native fake -> receipt."""
from server.business import InquiryService
from server.repository import Repository
from server.runtime import Dispatcher
from server.receiver_ledger import ReceiverLedger
from server.transports import MailTransport, MailConfig, TransportRouter
from server.file_gateway_transport import FileGatewayTransport
from tests.cutover.file_gateway_fakes import Gateway
from relay.protocol import Binding
from relay.collector_bridge import CollectorBridge
from tests.developer.test_transports_io import Mailboxes, mailcfg

NOW=1800000000
B=Binding('owner-a','account-a','wb-a',b's'*32,b'h'*32)

def setup(tmp_path,route):
    objects=Gateway();boxes=Mailboxes()
    repo=Repository('sqlite:///'+str(tmp_path/'business.db'));repo.bind_account(B.owner_user_id,B.account_uid,B.workbench_id,transport=route)
    service=InquiryService(repo,clock=lambda:NOW)
    preview=service.preview(B.owner_user_id,'preview',[{'code':'000100.IB','name':'合成券','peer_ids':['peer-a'],'our_side':'BUY','volume_wan':'1000','opening_yield_pct':'2.5','limit_yield_pct':'2.4','step_bp':'2'}])
    service.confirm(B.owner_user_id,preview['batch_id'],preview['version'],'confirm')
    inner={'oss':FileGatewayTransport(B,objects.client('up'),objects.client('down'),tmp_path/'inner.db',role='service',clock=lambda:NOW),'mail':MailTransport(B,mailcfg(),tmp_path/'inner.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:NOW)}
    outer={'oss':FileGatewayTransport(B,objects.client('down'),objects.client('up'),tmp_path/'outer.db',role='collector',clock=lambda:NOW),'mail':MailTransport(B,mailcfg(True),tmp_path/'outer.db',smtp_factory=boxes.smtp,imap_factory=boxes.imap,clock=lambda:NOW)}
    ledger=ReceiverLedger(tmp_path/'receiver.db',B,clock=lambda:NOW);calls=[]
    bridge=CollectorBridge(ledger=ledger,adapters=outer,object_store=None,native_send=lambda command:(calls.append(command) or {'status':'sent','evidence':'verified-native','native_id':'native-out-1'}),native_reconcile=lambda command:None,primary_transport=route)
    return service,Dispatcher(service,TransportRouter(inner),clock=lambda:NOW),bridge,inner,outer,calls,objects

def test_oss_backend_to_bridge_to_native_to_backend_sent(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    dispatcher.run_once();assert service.commands(B.owner_user_id)[0]['status']=='pending'
    bridge.run_once();dispatcher.run_once()
    assert service.commands(B.owner_user_id)[0]['status']=='sent' and len(calls)==1
    bridge.run_once();assert len(calls)==1
    # Dispatcher already persisted the native receipt; ACK that real MQ input
    # before sampling a later event on this destructive FIFO subscription.
    for receipt in inner['oss'].poll():
        assert receipt['kind']=='receipt'
        inner['oss'].ack(receipt['source_token'])
    event={'ext_msg_id':'native-out-1','peer_id':'peer-a','direction':'out','text':calls[0]['text'],'occurred_at':NOW}
    bridge.publish_native(event)
    payload=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope']['payload']
    assert payload['source_command_id']==calls[0]['command_id']
    assert payload['historical'] is False

def test_mail_backend_to_bridge_to_native_to_backend_sent(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'mail')
    dispatcher.run_once();bridge.run_once();dispatcher.run_once()
    assert service.commands(B.owner_user_id)[0]['status']=='sent' and len(calls)==1
    bridge.run_once();assert len(calls)==1

def test_cancel_objects_have_priority_over_earlier_send_objects(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    dispatcher.run_once();command=service.commands(B.owner_user_id)[0]
    # Dispatcher emits the signed cancellation after a persistent stop.
    service.set_bot(B.owner_user_id,False);dispatcher.run_once()
    bridge.run_once()
    # Honor the dispatcher's persistent five-second reconciliation cadence.
    dispatcher.clock=lambda:NOW+6
    dispatcher.run_once()
    assert calls==[]
    assert service.commands(B.owner_user_id)[0]['status']=='cancelled'

def test_native_uplink_both_routes_same_sequence_one_failure_does_not_block_other(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'mail')
    # An explicit auth rejection is safely retryable; an ambiguous FIFO upload
    # must remain UNKNOWN rather than inheriting old object-overwrite retries.
    objects.forced_status=401
    seq=bridge.publish_native({'ext_msg_id':'native-in-1','peer_id':'peer-a','direction':'in','text':'对方报价','occurred_at':NOW})
    mail_rows=inner['mail'].poll()
    assert len(mail_rows)==1 and mail_rows[0]['envelope']['payload']['source_seq']==seq==1
    objects.forced_status=None
    bridge.run_once()
    oss_rows=inner['oss'].poll()
    assert len(oss_rows)==1
    assert oss_rows[0]['envelope']['payload']==mail_rows[0]['envelope']['payload']
    assert calls==[]

def test_duplicate_wire_on_both_inputs_executes_native_once(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    dispatcher.run_once()
    blob=next(blob for _name,blob in objects.queues['down.invalid'] if inner['oss'].codec.unpack(blob)['kind']=='command')
    env=inner['oss'].codec.unpack(blob)
    inner['mail']._publish(env,blob)  # Deliberate duplicated delivery, not route fallback.
    bridge.run_once();dispatcher.run_once()
    assert len(calls)==1 and service.commands(B.owner_user_id)[0]['status']=='sent'

def test_boolean_native_callback_never_becomes_sent(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    bridge.native_send=lambda command:True
    dispatcher.run_once();bridge.run_once();dispatcher.run_once()
    assert service.commands(B.owner_user_id)[0]['status']=='unknown'

def test_input_is_not_acked_until_outcome_and_receipt_queue_are_durable(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'mail')
    dispatcher.run_once();original=outer['mail'].queue_receipt
    outer['mail'].queue_receipt=lambda *args:(_ for _ in ()).throw(OSError('disk full'))
    assert 'COMMAND_OR_RECEIPT_PERSISTENCE_UNAVAILABLE' in bridge.run_once()['errors']
    assert len(outer['mail'].poll())==1 and len(calls)==1
    outer['mail'].queue_receipt=original
    bridge.run_once();dispatcher.run_once()
    assert outer['mail'].poll()==[] and len(calls)==1
    assert service.commands(B.owner_user_id)[0]['status']=='sent'

def test_bounded_admin_replay_keeps_facts_and_marks_history_transport_metadata(tmp_path):
    import pytest
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    bridge.publish_native({'ext_msg_id':'native-history','peer_id':'peer-a','direction':'in','text':'原生事实','occurred_at':NOW-20})
    original=inner['oss'].poll()[0]['envelope']
    assert bridge.replay_events(1,1)['source_sequences']==[1]
    # An unACKed local event holds intake; inspect the newly authenticated
    # upload bytes rather than pretending FIFO supports object-key rereads.
    assert inner['oss'].poll()[0]['envelope']==original
    renewed=inner['oss'].codec.unpack(objects.queues['up.invalid'][-1][1])
    assert renewed['payload']==original['payload'] and renewed['content_hash']==original['content_hash']
    assert renewed['transport_replay'] is True
    assert calls==[]
    with pytest.raises(ValueError):bridge.replay_events(0,1)
    with pytest.raises(ValueError):bridge.replay_events(1,201)


def native_out(native_id='native-out-1', **extra):
    return dict(ext_msg_id=native_id, peer_id='peer-a', direction='out', text='相同正文', occurred_at=NOW, **extra)


def test_echo_before_native_return_is_deferred_then_exactly_linked(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    def send(command):
        calls.append(command)
        assert bridge.publish_native(native_out()) is None
        assert inner['oss'].poll()==[]
        return {'status':'sent','evidence':'native-verified','native_id':'native-out-1'}
    bridge.native_send=send
    dispatcher.run_once();bridge.run_once();dispatcher.run_once()
    payload=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope']['payload']
    assert payload['source_command_id']==calls[0]['command_id']
    assert service.commands(B.owner_user_id)[0]['status']=='sent'


def test_unmatched_manual_out_holds_new_auto_and_query_never_executes_it(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    held=[]
    def send(command):
        calls.append(command)
        bridge.publish_native(native_out('real-manual'))
        other=dict(command,command_id='held-command')
        held.append(other)
        assert bridge.ledger.execute(other,lambda c:False)['status']=='pending'
        assert bridge.ledger.reconcile(other,lambda c:(_ for _ in ()).throw(AssertionError('query called native')))['status']=='pending'
        return {'status':'sent','evidence':'native-verified','native_id':'native-out-1'}
    bridge.native_send=send
    dispatcher.run_once();bridge.run_once()
    payload=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope']['payload']
    assert 'source_command_id' not in payload
    assert bridge.ledger.execute(held[0],lambda c:(_ for _ in ()).throw(AssertionError('manual barrier bypass')))['status']=='cancelled'
    assert len(calls)==1


def test_confirmed_echo_releases_held_without_marking_native_unknown(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss');held=[]
    def send(command):
        bridge.publish_native(native_out())
        held.append(dict(command,command_id='held-command'))
        assert bridge.ledger.execute(held[0],lambda c:False)['status']=='pending'
        return {'status':'sent','evidence':'verified','native_id':'native-out-1'}
    bridge.native_send=send;dispatcher.run_once();bridge.run_once()
    outcome=bridge.ledger.execute(held[0],lambda c:(calls.append(c) or {'status':'sent','evidence':'verified','native_id':'native-out-2'}))
    assert outcome['status']=='sent' and len(calls)==1


def test_trusted_human_is_immediate_and_late_mapping_never_reverses_manual(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    def send(command):
        bridge.publish_native(native_out(role='human'))
        payload=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope']['payload']
        assert 'source_command_id' not in payload
        return {'status':'sent','evidence':'verified','native_id':'native-out-1'}
    bridge.native_send=send;dispatcher.run_once();bridge.run_once()
    bridge.publish_native(native_out(role='human'))
    payload=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope']['payload']
    assert 'source_command_id' not in payload


def test_deferred_timeout_and_restart_fail_closed_without_late_reclassification(tmp_path):
    for mode in ('timeout','restart'):
        root=tmp_path/mode;root.mkdir()
        service,dispatcher,bridge,inner,outer,calls,objects=setup(root,'oss')
        def send(command):
            bridge.publish_native(native_out())
            if mode=='timeout':bridge.ledger.clock=lambda:NOW+6
            else:
                recovered=CollectorBridge(ledger=ReceiverLedger(root/'receiver.db',B,clock=lambda:NOW),adapters=outer,object_store=None,native_send=lambda c:False,native_reconcile=lambda c:None)
                recovered.flush_deferred()
            if mode=='timeout':bridge.flush_deferred()
            return {'status':'sent','evidence':'verified','native_id':'native-out-1'}
        bridge.native_send=send;dispatcher.run_once();bridge.run_once()
        payload=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope']['payload']
        assert 'source_command_id' not in payload
        bridge.publish_native(native_out())
        payload=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope']['payload']
        assert 'source_command_id' not in payload


def test_held_mail_keeps_original_source_until_echo_resolves_and_native_runs_once(tmp_path):
    import threading
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'mail')
    command=dict(command_id='first-native',owner_user_id=B.owner_user_id,account_uid=B.account_uid,workbench_id=B.workbench_id,peer_id='peer-a',content_hash='a'*64,control_version=1,expires_at=NOW+100,text='明确已授权正文',transport='mail')
    ready=threading.Event();release=threading.Event()
    def native(command):
        bridge.publish_native(native_out())
        ready.set()
        if not release.wait(10):raise RuntimeError('test release timeout')
        return {'status':'sent','evidence':'verified','native_id':'native-out-1'}
    thread=threading.Thread(target=lambda:bridge.ledger.execute(command,native));thread.start()
    try:
        assert ready.wait(10)
        held=dict(command,command_id='mail-held')
        inner['mail'].send(held)
        assert bridge.run_once()['processed']==0
        assert len(outer['mail'].poll())==1 and calls==[]
        recovered=ReceiverLedger(tmp_path/'receiver.db',B,clock=lambda:NOW)
        assert recovered.reconcile(held,lambda c:(_ for _ in ()).throw(AssertionError('held query touched native')))['status']=='pending'
    finally:
        release.set();thread.join(10)
    assert not thread.is_alive()
    bridge.native_send=lambda c:(calls.append(c) or {'status':'sent','evidence':'verified','native_id':'native-out-2'})
    bridge.run_once()
    assert outer['mail'].poll()==[] and len(calls)==1
    bridge.run_once();assert len(calls)==1


def test_echo_wait_budget_rejects_unbounded_or_inconsistent_values(tmp_path):
    import pytest
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    for wait,budget in ((6,10),(2,1),(0,5),(float('nan'),5)):
        with pytest.raises(ValueError):
            CollectorBridge(ledger=bridge.ledger,adapters=outer,native_send=lambda c:False,native_reconcile=lambda c:None,echo_wait_seconds=wait,native_verification_budget=budget)


def test_manual_control_is_signed_top_level_same_on_both_routes_and_history(tmp_path):
    service,dispatcher,bridge,inner,outer,calls,objects=setup(tmp_path,'oss')
    bridge.publish_native(native_out('manual-one',role='human'))
    oss=next(row for row in inner['oss'].poll() if row['kind']=='event')['envelope'];mail=inner['mail'].poll()[0]['envelope']
    assert oss['manual_fence_id']==mail['manual_fence_id']
    assert oss['manual_fence_version']==mail['manual_fence_version']==1
    assert 'manual_fence_id' not in oss['payload']
    from relay.protocol import digest
    assert oss['content_hash']==digest(oss['payload'])
    bridge.replay_events(1,1)
    renewed=inner['oss'].codec.unpack(objects.queues['up.invalid'][-1][1])
    assert renewed['manual_fence_id']==oss['manual_fence_id'] and renewed['manual_fence_version']==1
    assert renewed['payload']==oss['payload']
