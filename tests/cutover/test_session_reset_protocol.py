"""Independent signed reset/epoch gates; no native service or real network."""
from copy import deepcopy

import pytest

from relay import protocol
from relay.protocol import Binding, Codec, ProtocolError, digest, envelope, validate_command

NOW = 1800000000
B = Binding('synthetic-reset-owner', 'synthetic-reset-account', 'synthetic-reset-workbench', b'k'*32, b's'*32)
COMMON = ('reset_id', 'owner_user_id', 'workbench_id', 'account_uid', 'binding_version',
          'expected_account_epoch', 'account_epoch', 'transport')


def request(*, reset_id='synthetic-reset-1', expected=0, epoch=1):
    value = {'reset_id': reset_id, 'owner_user_id': B.owner_user_id,
        'workbench_id': B.workbench_id, 'account_uid': B.account_uid, 'binding_version': 1,
        'expected_account_epoch': expected, 'account_epoch': epoch, 'transport': 'oss'}
    value['request_hash'] = digest(value)
    value['phase'] = 'request'
    return value


def snapshot():
    return {'account_epoch': 1, 'snapshot_at': NOW, 'published_source_highwater': 7,
        'peers': {'synthetic-peer': {'source_watermark': 7, 'receiver_fence_id': 'synthetic-fence',
            'receiver_fence_version': 4, 'min_control_version': 2}},
        'unresolved_history': [], 'old_native_tasks_running': 0,
        'pending_inbound_count': 0, 'pending_echo_count': 0}


def acknowledgment(phase='barrier_ready'):
    value = request(); value['phase'] = phase
    value.update(snapshot=snapshot(), snapshot_hash=digest(snapshot()),
                 code='SESSION_RESET_' + phase.upper())
    return value


def command(**changes):
    return {'owner_user_id': B.owner_user_id, 'account_uid': B.account_uid,
        'workbench_id': B.workbench_id, 'command_id': 'synthetic-reset-command',
        'peer_id': 'synthetic-peer', 'text': 'isolated reset command, no real send',
        'content_hash': 'a'*64, 'control_version': 1, 'expires_at': NOW+120,
        'kind': 'send_text', 'op': 'send_text', 'transport': 'oss', 'business_version': '2.2',
        'source_watermark': 0, 'dispatch_watermark': 0, **changes}


@pytest.mark.parametrize('kind,payload', [('session_reset', request()),
    ('session_reset_ack', acknowledgment())])
def test_new_control_kind_roundtrips_through_real_authenticated_codec(kind, payload):
    codec = Codec(B, clock=lambda: NOW)
    data = envelope(B, kind, payload, message_id=kind+'-synthetic-1', created_at=NOW, expires_at=NOW+120)
    assert codec.unpack(codec.pack(data)) == data


def test_reset_hash_is_shared_and_does_not_change_across_handshake_phase():
    fn = getattr(protocol, 'session_reset_hash', None)
    assert callable(fn), 'Shared reset hash is missing from actual protocol'
    original = request()
    assert fn(original) == original['request_hash']
    applied = {**original, 'phase': 'applied', 'snapshot_hash': digest(snapshot())}
    assert fn(applied) == original['request_hash']
    assert fn({**original, 'account_epoch': 2}) != original['request_hash']


@pytest.mark.parametrize('bad', [-1, True, 1.5, '1', None])
def test_command_epoch_never_accepts_bool_negative_or_noninteger(bad):
    with pytest.raises(ProtocolError):
        validate_command(command(account_epoch=bad), B)


@pytest.mark.parametrize('epoch', [0, 1, 9])
def test_valid_optional_epoch_is_not_rejected_before_receiver_state_check(epoch):
    validate_command(command(account_epoch=epoch), B)
    validate_command(command(), B)


@pytest.mark.parametrize('field', ['owner_user_id', 'account_uid', 'workbench_id'])
def test_signed_outer_scope_does_not_authorize_foreign_reset_payload(field):
    value = request(); value[field] = 'synthetic-foreign'
    value['request_hash'] = digest({k: value[k] for k in COMMON})
    codec = Codec(B, clock=lambda: NOW)
    with pytest.raises(ProtocolError):
        codec.pack(envelope(B, 'session_reset', value, message_id='foreign-synthetic', created_at=NOW, expires_at=NOW+120))


def ledger(tmp_path, binding=B, clock=None):
    from server.receiver_ledger import ReceiverLedger
    return ReceiverLedger(tmp_path/'receiver.sqlite3', binding, clock=clock or (lambda: NOW))


def applied(value, ack):
    return {**value, 'phase': 'applied', 'snapshot_hash': ack['snapshot_hash']}


def rejected_native(_):
    pytest.fail('Reset barrier must prevent reaching any native sender')


def test_missing_quiescence_still_installs_account_barrier_and_blocks_unseen_peer(tmp_path):
    receiver=ledger(tmp_path)
    ack=receiver.session_reset(request())
    assert ack['phase']=='blocked' and ack['code']=='SESSION_RESET_NATIVE_QUIESCENCE_UNAVAILABLE'
    assert receiver.account_epoch()==1
    old=command(peer_id='not-previously-registered',command_id='old-late-unseen')
    outcome=receiver.execute(old,rejected_native)
    assert outcome['status'] in ('cancelled','failed')
    assert receiver.permission(old) is False
    new=command(peer_id='another-unseen',command_id='new-too-soon',account_epoch=1)
    assert receiver.execute(new,rejected_native)['status'] in ('cancelled','failed')


def test_complete_reset_is_durable_and_idempotent_unknown_history_is_not_forged(tmp_path):
    receiver=ledger(tmp_path)
    old=command(command_id='old-uncertain')
    receiver.execute(old,lambda _: {'status':'unknown','evidence':'synthetic actual uncertain result'})
    with receiver._db() as db:
        original=tuple(db.execute('SELECT * FROM rx_command WHERE command_id=?',('old-uncertain',)).fetchone())
    value=request()
    ready=receiver.session_reset(value,native_quiescent=lambda:True)
    assert ready['phase']=='barrier_ready'
    assert ready['snapshot']['unresolved_history']==[{'command_id':'old-uncertain','peer_id':'synthetic-peer','account_epoch':0,'status':'unknown'}]
    assert ready['snapshot_hash']==digest(ready['snapshot'])
    restarted=ledger(tmp_path)
    assert restarted.session_reset(value,native_quiescent=lambda:False)==ready
    complete=restarted.session_reset(applied(value,ready))
    assert complete['phase']=='complete'
    assert complete['snapshot']==ready['snapshot']
    assert restarted.session_reset(value)==complete
    assert restarted.session_reset(applied(value,ready))==complete
    with restarted._db() as db:
        assert tuple(db.execute('SELECT * FROM rx_command WHERE command_id=?',('old-uncertain',)).fetchone())==original
        assert db.execute('SELECT COUNT(*) FROM rx_reset').fetchone()[0]==1
    calls=[]
    new=command(command_id='new-epoch-command',peer_id='unseen-peer',account_epoch=1)
    def native(wire):
        calls.append(wire['command_id'])
        return {'status':'sent','evidence':'synthetic native confirmation','native_id':'synthetic-new-native'}
    assert restarted.execute(new,native)['status']=='sent'
    assert restarted.execute(new,native)['status']=='sent'
    assert calls==['new-epoch-command']
    assert restarted.execute(command(command_id='old-epoch-after-complete',peer_id='brand-new-peer'),rejected_native)['status'] in ('failed','cancelled')


def test_pending_inbound_and_echo_wait_for_real_journal_progress_then_freeze_snapshot(tmp_path):
    receiver=ledger(tmp_path)
    receiver.begin_inbound('synthetic-inbound','peer-in')
    receiver.stage_out({'event_id':'synthetic-human-out','peer_id':'peer-out','direction':'out',
        'ext_msg_id':'synthetic-human-id','occurred_at':NOW,'text':'isolated human text','role':'human'},1)
    value=request()
    waiting=receiver.session_reset(value,native_quiescent=lambda:True,published_source_highwater=0)
    assert waiting['phase']=='waiting_native'
    assert waiting['snapshot']['pending_inbound_count']==1 and waiting['snapshot']['pending_echo_count']==1
    receiver.finish_inbound_journal('synthetic-inbound','peer-in',7)
    receiver.deferred_published('synthetic-human-out')
    ready=receiver.session_reset(value,native_quiescent=lambda:True,published_source_highwater=7)
    assert ready['phase']=='barrier_ready'
    assert ready['snapshot']['peers']['peer-in']['source_watermark']==7
    fence=ready['snapshot']['peers']['peer-out']
    assert fence['receiver_fence_version']==1 and fence['receiver_fence_id']
    receiver.begin_inbound('post-snapshot-event','peer-in')
    receiver.finish_inbound_journal('post-snapshot-event','peer-in',8)
    duplicate=receiver.session_reset(value,native_quiescent=lambda:True,published_source_highwater=8)
    assert duplicate==ready, 'Snapshot is immutable; new source data is not silently absorbed into an existing reset'
    assert receiver.session_reset(applied(value,ready))['phase']=='complete'
    stale=command(command_id='new-epoch-but-old-source',peer_id='peer-in',account_epoch=1,source_watermark=7)
    assert receiver.execute(stale,rejected_native)['status'] in ('failed','cancelled')


@pytest.mark.parametrize('conflict', ['same-id-different-binding-version','new-operation-during-wait','applied-before-ready','bad-snapshot'])
def test_reset_conflicts_never_advance_epoch_or_mark_complete(tmp_path,conflict):
    receiver=ledger(tmp_path);value=request()
    ready=receiver.session_reset(value,native_quiescent=lambda:conflict=='bad-snapshot')
    if conflict=='same-id-different-binding-version':
        changed={**value,'binding_version':2};changed['request_hash']=digest({k:changed[k] for k in COMMON})
    elif conflict=='new-operation-during-wait':
        changed=request(reset_id='second-reset',expected=1,epoch=2)
    elif conflict=='applied-before-ready':
        changed={**value,'phase':'applied','snapshot_hash':'a'*64}
    else:
        changed={**value,'phase':'applied','snapshot_hash':'b'*64}
    reply=receiver.session_reset(changed,native_quiescent=lambda:True)
    assert reply['phase']=='blocked'
    assert receiver.account_epoch()==1 and receiver.reset_status()['state']!='complete'


def test_reset_of_one_account_does_not_change_other_account_epoch_or_command(tmp_path):
    other=Binding(B.owner_user_id,'other-account',B.workbench_id,B.seal_key,B.sign_key)
    receiver=ledger(tmp_path);second=ledger(tmp_path,other)
    other_command={**command(command_id='other-command'), 'account_uid':other.account_uid}
    second.execute(other_command,lambda _: {'status':'unknown','evidence':'synthetic other account uncertain'})
    with second._db() as db:
        old=tuple(db.execute('SELECT * FROM rx_command WHERE scope=?',(second.scope,)).fetchone())
    value=request();ready=receiver.session_reset(value,native_quiescent=lambda:True)
    receiver.session_reset(applied(value,ready))
    assert second.account_epoch()==0
    with second._db() as db:assert tuple(db.execute('SELECT * FROM rx_command WHERE scope=?',(second.scope,)).fetchone())==old


from tests.acceptance.test_qtrade_native_port_20261014 import loop as native_loop


def test_timed_out_native_future_stays_nonquiescent_until_real_coroutine_exits(tmp_path,native_loop):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace
    from relay.qtrade_native_port import QTradeNativePort
    from tests.acceptance.test_qtrade_native_port_20261014 import Store, factory
    released=asyncio.Event();started=threading.Event();native_actions=[]
    class Bus:
        supports_xunjia_v22_guard=True
        async def submit(self,wire):
            started.set()
            await released.wait()
            allowed=wire._xunjia_native_guard()
            if allowed:native_actions.append(wire)
            return SimpleNamespace(ok=False,code='GATE_BLOCKED',data={},
                error=SimpleNamespace(reason='xunjia_native_permission_changed'))
    account=Store();account.accounts[0]['self_uid']=B.account_uid
    receiver=ledger(tmp_path)
    bus=Bus()
    port=QTradeNativePort(binding=B,store=account,bus=bus,loop=native_loop,
        legacy_inbox_disabled=lambda:True,timeout_seconds=1,command_factory=factory,
        execution_permission=receiver.permission)
    try:
        assert port.quiescent() is True
        old=command()
        outcome=receiver.execute(old,port.native_send)
        assert started.is_set() and outcome['status']=='unknown'
        assert port.quiescent() is False, 'Timeout is not proof that the actual native coroutine ended'
        replacement=QTradeNativePort(binding=B,store=account,bus=bus,loop=native_loop,legacy_inbox_disabled=lambda:True,timeout_seconds=1,command_factory=factory,execution_permission=receiver.permission)
        assert replacement.quiescent() is False, 'Rebuilding a Port for the same Bus/account cannot erase an outstanding task'
        value=request();waiting=receiver.session_reset(value,native_quiescent=port.quiescent)
        assert waiting['phase']=='waiting_native' and waiting['snapshot']['old_native_tasks_running']==1
        assert receiver.permission(old) is False
        native_loop.call_soon_threadsafe(released.set)
        deadline=time.monotonic()+3
        while not port.quiescent() and time.monotonic()<deadline:time.sleep(.01)
        assert port.quiescent() is True
        ready=receiver.session_reset(value,native_quiescent=port.quiescent)
        assert ready['phase']=='barrier_ready' and ready['snapshot']['old_native_tasks_running']==0
        assert native_actions==[], 'The final Bus guard still rejects the old epoch after reset'
    finally:
        native_loop.call_soon_threadsafe(released.set)
        deadline=time.monotonic()+3
        while not port.quiescent() and time.monotonic()<deadline:time.sleep(.01)


def test_barrier_install_precedes_wait_for_existing_account_execution_lock(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    receiver=ledger(tmp_path);entered=threading.Event();release=threading.Event();permissions=[]
    old=command()
    def native(wire):
        permissions.append(receiver.permission(wire));entered.set()
        assert release.wait(5)
        permissions.append(receiver.permission(wire))
        return {'status':'cancelled','evidence':'synthetic final guard observed reset'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(receiver.execute,old,native)
        try:
            assert entered.wait(3)
            waiting=receiver.session_reset(request(),native_quiescent=lambda:True)
            assert waiting['phase']=='waiting_native' and waiting['snapshot']['old_native_tasks_running']==1
            assert receiver.account_epoch()==1 and receiver.permission(old) is False
        finally:release.set()
        assert future.result(timeout=3)['status']=='cancelled'
    assert permissions==[True,False]
    assert receiver.session_reset(request(),native_quiescent=lambda:True)['phase']=='barrier_ready'


@pytest.fixture
def reset_wire(tmp_path):
    from relay.collector_bridge import CollectorBridge
    from server.file_gateway_transport import FileGatewayTransport
    from tests.cutover.file_gateway_fakes import Gateway
    from types import SimpleNamespace
    now=[NOW];gateway=Gateway()
    service=FileGatewayTransport(B,gateway.client('up'),gateway.client('down'),tmp_path/'service.sqlite3',role='service',clock=lambda:now[0])
    collector=FileGatewayTransport(B,gateway.client('down'),gateway.client('up'),tmp_path/'collector.sqlite3',role='collector',clock=lambda:now[0])
    receiver=ledger(tmp_path,clock=lambda:now[0])
    bridge=CollectorBridge(ledger=receiver,adapters={'oss':collector},native_send=rejected_native,
        native_reconcile=lambda _:None,native_quiescent=lambda:True)
    return SimpleNamespace(now=now,gateway=gateway,service=service,collector=collector,receiver=receiver,bridge=bridge)


def publish_request(wire,value=None):
    value=value or request()
    wire.service.queue_session_reset(value)
    wire.service.flush_publications()
    return value


def acknowledgments(wire):
    rows=wire.service.poll()
    return [(row,row['envelope']['payload']) for row in rows if row['kind']=='session_reset_ack']


@pytest.mark.parametrize('crash', ['ack-publication-write','source-business-ack'])
def test_crash_between_reset_install_and_source_ack_does_not_double_apply(reset_wire,monkeypatch,crash):
    w=reset_wire;publish_request(w)
    name='queue_session_reset_ack' if crash=='ack-publication-write' else 'ack'
    original=getattr(w.collector,name);calls=[0]
    def fail_once(*args,**kwargs):
        calls[0]+=1
        if calls[0]==1:raise OSError('synthetic crash before durable publication/source acknowledgment')
        return original(*args,**kwargs)
    monkeypatch.setattr(w.collector,name,fail_once)
    w.bridge.run_once()
    assert w.receiver.account_epoch()==1 and w.receiver.reset_status()['state']=='barrier_ready'
    from relay.collector_bridge import CollectorBridge
    from server.receiver_ledger import ReceiverLedger
    restarted=ReceiverLedger(w.receiver.path,B,clock=lambda:w.now[0])
    bridge=CollectorBridge(ledger=restarted,adapters={'oss':w.collector},native_send=rejected_native,
        native_reconcile=lambda _:None,native_quiescent=lambda:True)
    bridge.run_once()
    replies=acknowledgments(w)
    assert replies and all(x['phase']=='barrier_ready' for _,x in replies)
    with restarted._db() as db:assert db.execute('SELECT COUNT(*) FROM rx_reset').fetchone()[0]==1
    assert restarted.account_epoch()==1
    for row,_ in replies:w.service.ack(row['source_token'])


@pytest.mark.parametrize('lost', ['request','ack'])
def test_lost_destructive_control_response_recovers_in_next_bucket_without_new_logical_reset(reset_wire,lost):
    from relay.file_gateway import FileGatewayError
    w=reset_wire;value=publish_request(w)
    if lost=='request':
        w.gateway.lose_download=True
        with pytest.raises(FileGatewayError):w.collector.poll()
        w.gateway.lose_download=False
        assert w.receiver.account_epoch()==0
    else:
        w.bridge.run_once()
        assert w.receiver.account_epoch()==1
        w.gateway.lose_download=True
        with pytest.raises(FileGatewayError):w.service.poll()
        w.gateway.lose_download=False
    w.now[0]+=31
    publish_request(w,value)
    w.bridge.run_once()
    replies=acknowledgments(w)
    assert replies and any(x['phase']=='barrier_ready' for _,x in replies)
    ready=next(x for _,x in replies if x['phase']=='barrier_ready')
    assert ready['reset_id']==value['reset_id'] and ready['request_hash']==value['request_hash']
    assert w.receiver.account_epoch()==1
    for row,_ in replies:w.service.ack(row['source_token'])
    w.service.queue_session_reset(applied(value,ready));w.service.flush_publications();w.bridge.run_once()
    completed=acknowledgments(w)
    assert any(x['phase']=='complete' for _,x in completed)
    assert w.receiver.reset_status()['state']=='complete'
    assert not any(op=='send' for _,op in w.gateway.calls), 'Only files API upload/download is present'


def test_real_collector_control_intake_reaches_reset_behind_old_expired_staged_command(reset_wire):
    w=reset_wire
    old=command(command_id='old-staged-expired')
    assert w.service.send(old)['status']=='accepted'
    rows=w.collector.poll();assert len(rows)==1 and rows[0]['kind']=='command'
    w.now[0]+=121
    publish_request(w)
    result=w.bridge.run_once()
    assert w.receiver.account_epoch()==1 and w.receiver.reset_status() is not None,result
    replies=acknowledgments(w)
    assert any(payload['phase']=='barrier_ready' for _,payload in replies)
    with w.collector.store.db() as db:
        original=db.execute("SELECT data FROM tx_message WHERE kind='command'").fetchone()
        assert original and 'old-staged-expired' in original['data']


def test_real_collector_control_intake_reaches_reset_behind_unacked_held_command(reset_wire):
    import json
    from relay.protocol import canonical
    w=reset_wire
    # Reproduce a pre-existing claimed call/echo hold using the real ledger
    # journal. Its native outcome remains unknown; no native callback is run.
    pending=command(command_id='old-other-native-pending')
    with w.receiver._db() as db:
        w.receiver._record(db,pending,w.receiver._fingerprint(pending),'pending','synthetic earlier native claim')
        db.execute('INSERT INTO rx_deferred VALUES(?,?,?,?,?,?,?)',
            (w.receiver.scope,'unclassified-echo','synthetic-peer',canonical({'event_id':'unclassified-echo',
             'peer_id':'synthetic-peer','direction':'out','text':'isolated old echo','ext_msg_id':'old-echo',
             'occurred_at':NOW}).decode(),w.receiver.instance_id,NOW+1000,'waiting'))
    held=command(command_id='held-before-reset')
    w.service.send(held)
    w.bridge.run_once()
    with w.collector.store.db() as db:
        before=db.execute("SELECT COUNT(*) FROM tx_source WHERE kind='command' AND acked=0").fetchone()[0]
        assert before==1
    publish_request(w)
    result=w.bridge.run_once()
    assert w.receiver.account_epoch()==1 and w.receiver.reset_status() is not None,result
    with w.receiver._db() as db:
        old=db.execute('SELECT status FROM rx_command WHERE command_id=?',(pending['command_id'],)).fetchone()
        assert old['status']=='pending', 'Reset must not forge a terminal outcome for an earlier native call'


@pytest.mark.parametrize('failure', ['missing','raises','nonbool'])
def test_temporary_quiescence_failure_recovers_same_installed_epoch_not_new_operation(tmp_path,failure):
    receiver=ledger(tmp_path);value=request()
    def broken():
        raise RuntimeError('synthetic unavailable native observation')
    callback=None if failure=='missing' else broken if failure=='raises' else lambda:'true'
    blocked=receiver.session_reset(value,native_quiescent=callback)
    assert blocked['phase']=='blocked' and blocked['code']=='SESSION_RESET_NATIVE_QUIESCENCE_UNAVAILABLE'
    assert receiver.account_epoch()==1 and receiver.reset_status()['state']!='complete'
    ready=receiver.session_reset(value,native_quiescent=lambda:True)
    assert ready['phase']=='barrier_ready' and ready['reset_id']==value['reset_id']
    assert receiver.session_reset(applied(value,ready))['phase']=='complete'
    with receiver._db() as db:assert db.execute('SELECT COUNT(*) FROM rx_reset').fetchone()[0]==1


@pytest.mark.parametrize('missing', ['quiescence','ledger-method',None])
def test_real_signed_capability_catalog_never_advertises_unsupported_reset(reset_wire,monkeypatch,missing):
    w=reset_wire
    if missing=='quiescence':w.bridge.native_quiescent=None
    if missing=='ledger-method':monkeypatch.setattr(w.receiver,'session_reset',None)
    w.bridge.publish_capabilities()
    book=w.service.catalog()
    assert 'negotiation-v22-watermark' in book['capabilities']
    assert ('negotiation-v22-session-reset' in book['capabilities']) is (missing is None)
