"""An unresolved native claim must not turn an unclassified echo into human input."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from relay.collector_bridge import CollectorBridge
from relay.protocol import digest
from server.file_gateway_transport import FileGatewayTransport
from server.receiver_ledger import ReceiverLedger
from tests.acceptance.test_qtrade_native_port_20261014 import binding, loop, NOW
from tests.cutover.test_late_native_readback import world as late_native_world
from file_gateway_fakes import Gateway


def world(tmp_path,binding,loop,*,claim=True):
    clock=[float(NOW)]
    native_store,bus,native,command=late_native_world(binding,loop)
    late=deepcopy(native_store.late);native_store.late=None
    gateway=Gateway()
    service=FileGatewayTransport(binding,gateway.client('up'),gateway.client('down'),
        tmp_path/'service.db',role='service',clock=lambda:clock[0])
    collector=FileGatewayTransport(binding,gateway.client('down'),gateway.client('up'),
        tmp_path/'collector.db',role='collector',clock=lambda:clock[0])
    ledger=ReceiverLedger(tmp_path/'receiver.db',binding,clock=lambda:clock[0])
    sends=[]
    def unknown(wire):
        sends.append(wire['command_id'])
        return {'status':'unknown','evidence':'synthetic-native-timeout-after-claim'}
    if claim:assert ledger.execute(command,unknown)['status']=='unknown'
    bridge=CollectorBridge(ledger=ledger,adapters={'oss':collector},
        native_send=lambda _:pytest.fail('An UNKNOWN native claim cannot be resubmitted'),
        native_reconcile=native.native_reconcile)
    event={'event_id':'synthetic-late-echo','peer_id':command['peer_id'],'direction':'out',
        'text':command['text'],'ext_msg_id':late['ext_msg_id'],'occurred_at':clock[0]}
    return SimpleNamespace(clock=clock,store=native_store,bus=bus,native=native,command=command,
        late=late,gateway=gateway,service=service,collector=collector,ledger=ledger,bridge=bridge,
        event=event,sends=sends,binding=binding)


def test_unknown_current_peer_native_out_is_durably_deferred_without_human_fence_or_body_inferred_origin(tmp_path,binding,loop):
    w=world(tmp_path,binding,loop)
    assert w.bridge.publish_native(w.event) is None, 'UNKNOWN is still in flight; no human publication is justified'
    assert w.service.poll()==[] and w.ledger.event_control(w.event['event_id'])=={}
    w.clock[0]+=6.410
    reopened=ReceiverLedger(w.ledger.path,binding,clock=lambda:w.clock[0])
    assert reopened.deferred_ready()==[], 'Restart or the old echo deadline does not resolve UNKNOWN ownership'
    assert reopened.event_control(w.event['event_id'])=={} and not reopened.permission(w.command)
    with reopened._db() as db:
        command=db.execute('SELECT status FROM rx_command WHERE scope=? AND command_id=?',
            (reopened.scope,w.command['command_id'])).fetchone()
        event=db.execute('SELECT data FROM rx_deferred WHERE scope=? AND event_id=?',
            (reopened.scope,w.event['event_id'])).fetchone()
    assert command['status']=='unknown' and event
    assert not json.loads(event['data']).get('source_command_id'), 'Matching body is not exact native provenance'
    assert reopened.execute(w.command,lambda _:pytest.fail('duplicate native send'))['status']=='unknown'
    next_command={**w.command,'command_id':'synthetic-next-auto','control_version':2}
    assert reopened.execute(next_command,lambda _:pytest.fail('unresolved echo must hold auto send'))['status']=='pending'
    assert w.sends==[w.command['command_id']] and w.bus.calls==[]


def test_late_exact_trace_readback_maps_deferred_echo_to_its_current_command_instead_of_manual_fence(tmp_path,binding,loop):
    w=world(tmp_path,binding,loop)
    w.ledger.stage_out(deepcopy(w.event),w.bridge.echo_wait_seconds)
    w.clock[0]+=6.410
    w.store.late=deepcopy(w.late)
    result=w.ledger.reconcile(w.command,w.native.native_reconcile)
    assert result['status']=='sent' and result['native_id']==w.event['ext_msg_id']
    assert w.store.reads==['exact-trace'] and w.bus.calls==[]
    w.bridge.flush_deferred()
    events=[row['envelope'] for row in w.service.poll() if row['envelope']['kind']=='event']
    assert len(events)==1 and events[0]['payload']['source_command_id']==w.command['command_id']
    assert 'manual_fence_id' not in events[0] and 'manual_fence_version' not in events[0]
    assert w.ledger.event_control(w.event['event_id'])=={}
    assert w.ledger.execute(w.command,lambda _:pytest.fail('late SENT cannot resend'))['status']=='sent'
    assert w.sends==[w.command['command_id']]


def test_real_human_out_without_any_receiver_claim_still_publishes_a_manual_fence(tmp_path,binding,loop):
    w=world(tmp_path,binding,loop,claim=False)
    assert w.bridge.publish_native(w.event) is not None
    events=[row['envelope'] for row in w.service.poll() if row['envelope']['kind']=='event']
    assert len(events)==1 and not events[0]['payload'].get('source_command_id')
    assert events[0]['manual_fence_id'] and events[0]['manual_fence_version']==1
    assert w.ledger.event_control(w.event['event_id'])['manual_fence_id']==events[0]['manual_fence_id']
    assert not w.sends and w.bus.calls==[]


def test_another_peer_unknown_does_not_lend_system_identity_to_this_native_out(tmp_path,binding,loop):
    w=world(tmp_path,binding,loop)
    event={**w.event,'event_id':'synthetic-other-peer-out','peer_id':'other-peer','ext_msg_id':'native-other-peer'}
    assert w.bridge.publish_native(event) is not None
    events=[row['envelope'] for row in w.service.poll() if row['envelope']['kind']=='event']
    assert len(events)==1 and events[0]['payload']['peer_id']=='other-peer'
    assert not events[0]['payload'].get('source_command_id') and events[0]['manual_fence_id']
    assert w.ledger.event_control(w.event['event_id'])=={}
    assert w.ledger.reconcile(w.command,w.native.native_reconcile)['status']=='unknown'
    assert w.sends==[w.command['command_id']] and w.bus.calls==[]


def test_old_epoch_unknown_does_not_hold_or_identify_a_new_epoch_out(tmp_path,binding,loop):
    w=world(tmp_path,binding,loop)
    request={'reset_id':'synthetic-epoch-switch','owner_user_id':binding.owner_user_id,
        'workbench_id':binding.workbench_id,'account_uid':binding.account_uid,'binding_version':1,
        'expected_account_epoch':0,'account_epoch':1,'transport':'oss'}
    request['request_hash']=digest(request);request['phase']='request'
    ack=w.ledger.session_reset(request,native_quiescent=lambda:True)
    assert ack['phase']=='barrier_ready' and any(row['status']=='unknown' for row in ack['snapshot']['unresolved_history'])
    complete=w.ledger.session_reset({**request,'phase':'applied','snapshot_hash':ack['snapshot_hash']},
        native_quiescent=lambda:True)
    assert complete['phase']=='complete' and w.ledger.account_epoch()==1
    w.clock[0]+=1
    event={**w.event,'event_id':'synthetic-new-epoch-out','ext_msg_id':'new-epoch-native','occurred_at':w.clock[0]}
    assert w.ledger.stage_out(event,5)=='ready'
    assert w.ledger.event_control(event['event_id'])['manual_fence_id']
    with w.ledger._db() as db:
        row=db.execute('SELECT status FROM rx_command WHERE scope=? AND command_id=?',
            (w.ledger.scope,w.command['command_id'])).fetchone()
    assert row['status']=='unknown' and w.sends==[w.command['command_id']]


def test_same_body_rpa_hint_and_wrong_trace_never_resolve_unknown_as_sent(tmp_path,binding,loop):
    w=world(tmp_path,binding,loop)
    event={**w.event,'role':'rpa'}
    assert w.bridge.publish_native(event) is None
    w.store.late={**w.late,'trace_id':'different-synthetic-trace'}
    assert w.ledger.reconcile(w.command,w.native.native_reconcile)['status']=='unknown'
    w.clock[0]+=6.410
    assert w.bridge.flush_deferred()=={} and w.service.poll()==[]
    assert w.ledger.command_for_native(event['ext_msg_id'],event['peer_id']) is None
    assert w.ledger.event_control(event['event_id'])=={} and w.bus.calls==[]
    assert w.sends==[w.command['command_id']]
