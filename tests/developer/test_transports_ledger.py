import concurrent.futures
import threading
import pytest
from server.receiver_ledger import ReceiverLedger
from relay.protocol import Binding, ProtocolError

B=Binding('owner-a','account-a','workbench-a',b's'*32,b'h'*32)
def command(**changes):
    return dict(command_id='cmd-1',owner_user_id=B.owner_user_id,account_uid=B.account_uid,workbench_id=B.workbench_id,peer_id='peer-a',content_hash='a'*64,control_version=1,expires_at=200,text='合成正文',transport='oss',**changes)

def test_cross_transport_concurrency_claims_once_and_persists(tmp_path):
    calls=[];path=tmp_path/'receiver.db'
    def run(route):
        cmd=command();cmd['transport']=route
        return ReceiverLedger(path,B,clock=lambda:100).execute(cmd,lambda c:(calls.append(c['command_id']) or {'status':'sent','evidence':'native-1'}))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(run,['oss','mail']))
    assert len(calls)==1
    assert {r['status'] for r in results}=={'sent'}
    assert ReceiverLedger(path,B,clock=lambda:100).reconcile(command(),lambda c:None)['status']=='sent'

def test_pending_crash_never_blindly_reexecutes_and_reconciles(tmp_path):
    path=tmp_path/'receiver.db';ledger=ReceiverLedger(path,B,clock=lambda:100)
    calls=[]
    def crash(c):calls.append(1);raise RuntimeError('simulated native response loss')
    assert ledger.execute(command(),crash)['status']=='unknown'
    recovered=ReceiverLedger(path,B,clock=lambda:101)
    assert recovered.execute(command(),crash)['status']=='unknown'
    assert calls==[1]
    assert recovered.reconcile(command(),lambda c:{'status':'sent','evidence':'native-found'})['status']=='sent'

def test_cancel_before_original_blocks_late_delivery_on_both_routes(tmp_path):
    ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100)
    assert ledger.cancel(command())['status']=='cancelled'
    def forbidden(c):raise AssertionError('native must not run')
    for route in ['mail','oss']:
        cmd=command();cmd['transport']=route
        assert ledger.execute(cmd,forbidden)['status']=='cancelled'

def test_same_id_changed_payload_or_identity_rejected_and_expired_never_runs(tmp_path):
    ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100)
    ledger.execute(command(),lambda c:{'status':'sent','evidence':'native-1'})
    c=command();c['text']='different'
    with pytest.raises(ProtocolError):ledger.execute(c,lambda c:None)
    c=command();c['account_uid']='other'
    with pytest.raises(ProtocolError):ledger.execute(c,lambda c:None)
    c=command();c['command_id']='expired';c['expires_at']=99
    assert ledger.execute(c,lambda c:pytest.fail('expired native'))['status']=='failed'

def test_process_style_crash_leaves_pending_and_never_calls_native_again(tmp_path):
    ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100)
    with pytest.raises(SystemExit):ledger.execute(command(),lambda c:(_ for _ in ()).throw(SystemExit(7)))
    recovered=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:101)
    assert recovered.execute(command(),lambda c:pytest.fail('blind replay'))['status']=='unknown'

def test_cancel_version_barrier_blocks_new_id_with_stale_peer_version(tmp_path):
    ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100);ledger.cancel(command())
    old=command();old['command_id']='late-other-id'
    assert ledger.execute(old,lambda c:pytest.fail('stale control'))['status']=='cancelled'
    new=command();new.update(command_id='new-authorized',control_version=2)
    assert ledger.execute(new,lambda c:{'status':'sent','evidence':'native-2'})['status']=='sent'

def test_same_command_id_in_other_account_has_independent_execution(tmp_path):
    path=tmp_path/'r.db';ReceiverLedger(path,B,clock=lambda:100).cancel(command())
    other=Binding('owner-b','account-b','workbench-a',b's'*32,b'h'*32)
    c=command();c.update(owner_user_id=other.owner_user_id,account_uid=other.account_uid)
    assert ReceiverLedger(path,other,clock=lambda:100).execute(c,lambda c:{'status':'sent','evidence':'other-native'})['status']=='sent'

def test_cancel_during_native_action_is_unknown_and_persists_barrier_without_waiting(tmp_path):
    path=tmp_path/'r.db';started=threading.Event();release=threading.Event()
    ledger=ReceiverLedger(path,B,clock=lambda:100)
    def native(c):started.set();release.wait(3);return {'status':'sent','evidence':'native-inflight'}
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        execution=pool.submit(ledger.execute,command(),native)
        assert started.wait(1)
        cancel=pool.submit(ReceiverLedger(path,B,clock=lambda:100).cancel,command())
        try:assert cancel.result(timeout=.3)['status']=='unknown'
        finally:release.set()
        assert execution.result(timeout=1)['status']=='sent'
    assert ledger.execute(command(),lambda c:pytest.fail('duplicate after cancel'))['status']=='sent'

def test_native_outbound_mapping_uses_explicit_native_id_not_text(tmp_path):
    ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100)
    ledger.execute(command(),lambda c:{'status':'sent','evidence':'source-confirmed','native_id':'native-999'})
    assert ledger.command_for_native('native-999','peer-a')=='cmd-1'
    assert ledger.command_for_native('native-999','other-peer') is None
    assert ledger.command_for_native('合成正文','peer-a') is None

def test_manual_text_crosses_real_codec_and_ledger_without_op_escalation(tmp_path):
    from relay.protocol import Codec, envelope
    c=command();c.update(command_id='manual-1',kind='manual_send',op='send_text')
    codec=Codec(B,clock=lambda:100)
    data=envelope(B,'command',c,message_id=c['command_id'],command_id=c['command_id'],peer_id=c['peer_id'],content_hash=c['content_hash'],control_version=c['control_version'],created_at=100,expires_at=200)
    calls=[];ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100)
    got=ledger.receive(codec.pack(data),codec,lambda native:(calls.append(native) or {'status':'sent','evidence':'manual-native','native_id':'manual-msg-1'}),lambda c:None)
    assert got['status']=='sent' and calls[0]['kind']=='manual_send' and calls[0]['text']=='合成正文'
    bad=dict(c,command_id='manual-escalation',op='delete_account')
    with pytest.raises(ProtocolError):ledger.execute(bad,lambda c:pytest.fail('non-text operation'))


def test_held_query_expiry_is_terminal_without_native_but_claimed_pending_is_unknown(tmp_path):
    now=[100];ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:now[0]);second=command();second.update(command_id='held-short',expires_at=101)
    def native(first):
        ledger.stage_out(dict(event_id='echo',peer_id='peer-a',ext_msg_id='n1',direction='out',occurred_at=100,text='echo'),5)
        assert ledger.execute(second,lambda c:pytest.fail('held native'))['status']=='pending'
        return {'status':'sent','evidence':'native','native_id':'n1'}
    ledger.execute(command(),native);ledger.deferred_ready();now[0]=102
    assert ledger.reconcile(second,lambda c:pytest.fail('held query native'))['status']=='failed'
    assert ReceiverLedger(ledger.path,B,clock=lambda:102).reconcile(second,lambda c:pytest.fail('terminal query native'))['status']=='failed'
    claimed=command();claimed.update(command_id='claimed-crash',expires_at=103)
    with pytest.raises(SystemExit):ledger.execute(claimed,lambda c:(_ for _ in ()).throw(SystemExit(1)))
    now[0]=104
    assert ledger.reconcile(claimed,lambda c:None)['status']=='unknown'


def test_manual_fence_blocks_unseen_higher_epoch_and_old_human_draft(tmp_path):
    ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100)
    ledger.execute(command(),lambda c:{'status':'sent','evidence':'native','native_id':'n1'})
    event=dict(event_id='real-human',peer_id='peer-a',ext_msg_id='human1',direction='out',role='human',occurred_at=100,text='manual')
    ledger.stage_out(event,5)
    later=command();later.update(command_id='previously-authorized-higher',control_version=999)
    assert ledger.execute(later,lambda c:pytest.fail('unseen higher epoch bypass'))['status']=='cancelled'
    human=dict(later,command_id='old-human',kind='manual_send')
    assert ledger.execute(human,lambda c:pytest.fail('old human draft bypass'))['status']=='cancelled'
    fence=ledger.event_control('real-human')
    allowed=dict(later,command_id='causally-authorized',receiver_fence_id=fence['manual_fence_id'],receiver_fence_version=fence['manual_fence_version'])
    from relay.protocol import Codec, envelope
    codec=Codec(B,clock=lambda:100)
    wire=envelope(B,'command',allowed,message_id=allowed['command_id'],command_id=allowed['command_id'],peer_id=allowed['peer_id'],content_hash=allowed['content_hash'],control_version=allowed['control_version'],created_at=100,expires_at=allowed['expires_at'])
    assert ledger.receive(codec.pack(wire),codec,lambda c:{'status':'sent','evidence':'verified'},lambda c:None)['status']=='sent'
    ledger.stage_out(dict(event,event_id='human2',ext_msg_id='human2'),5)
    latest=ledger.event_control('human2')
    assert latest['manual_fence_version']==fence['manual_fence_version']+1
    ledger.stage_out(event,5)
    assert ledger.event_control('real-human')==fence
    assert ledger.execute(dict(allowed,command_id='old-fence-replay'),lambda c:pytest.fail('older fence bypass'))['status']=='cancelled'
    assert ReceiverLedger(ledger.path,B,clock=lambda:100).execute(dict(later,command_id='restart-high'),lambda c:pytest.fail('restart bypass'))['status']=='cancelled'


@pytest.mark.parametrize('barrier',['cancel-version','manual-fence'])
def test_held_query_stale_authorization_cancels_without_native(tmp_path,barrier):
    ledger=ReceiverLedger(tmp_path/'r.db',B,clock=lambda:100)
    held=dict(command(),command_id='held-command')
    def native(first):
        ledger.stage_out(dict(event_id='echo',peer_id='peer-a',ext_msg_id='n1',direction='out',occurred_at=100,text='echo'),5)
        assert ledger.execute(held,lambda c:pytest.fail('held action'))['status']=='pending'
        if barrier=='cancel-version':ledger.cancel(dict(command(),command_id='cancel-other'))
        else:ledger.stage_out(dict(event_id='human',peer_id='peer-a',ext_msg_id='human1',direction='out',role='human',occurred_at=100,text='manual'),5)
        return {'status':'sent','evidence':'verified','native_id':'n1'}
    assert ledger.execute(command(),native)['status']=='sent'
    assert ledger.reconcile(held,lambda c:pytest.fail('held query native'))['status']=='cancelled'
