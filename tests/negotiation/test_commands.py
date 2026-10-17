"""State/concurrency probes use only a synthetic validator and local SQLite."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from server.negotiation.commands import Commands
from server.negotiation.prices import InvalidFact, Price
from server.negotiation.store import Store
def test_bad_reply_never_rolls_back_good_counterparty_facts(world):
    g,m,t=world.make()
    world.quote(m,t,price='2.20',volume='5000')
    before=world.store.quote(world.owner,m,'ofr').dump()
    def bad(_):raise ValueError('Proposed 2.48 but public reply says 2.49')
    result=world.queue(m,validator=bad)
    assert result['state']=='REJECTED' and result['error']['reason']=='REPLY_VALIDATION_FAILED'
    assert world.store.quote(world.owner,m,'ofr').dump()==before
    with world.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']==0
        assert tx.execute('SELECT state FROM xj2_decision').fetchone()['state']=='REJECTED'
def test_raw_new_message_blocks_dispatch_then_same_member_update_cancels_old_candidate(world):
    g,m,t=world.make()
    world.quote(m,t)
    result=world.queue(m)
    cid=result['command_id']
    assert cid
    raw=world.message(t,'new better price 3.20')
    assert world.commands.claim(world.owner,cid) is None
    assert world.commands.get(world.owner,cid)['state']=='QUEUED'
    world.analyze(t,{raw:[m]},[world.observation(m,raw,{'price':Price('YIELD','3.20').dump()})])
    assert world.commands.claim(world.owner,cid) is None
    assert world.commands.get(world.owner,cid)['state']=='CANCELLED'
    assert world.member(m)['pending_command_id'] is None
def test_unrelated_bond_does_not_permanently_cancel_this_bond_reply(world):
    ga,a,t=world.make('SYN-A')
    gb,b,_=world.make('SYN-B')
    cid=world.queue(a)['command_id']
    raw=world.message(t,'SYN-B only')
    assert world.commands.claim(world.owner,cid) is None
    world.analyze(t,{raw:[b]})
    envelope=world.commands.claim(world.owner,cid)
    assert envelope and envelope['dispatch_watermark']==1
def test_other_counterparty_price_is_independent_but_shared_amount_dependency_invalidates(world):
    g,a,ta=world.make()
    b,tb=world.store.add_member(world.owner,g,account_uid='synthetic-account',peer_uid='peer-02')
    basis_independent=world.commands.basis(world.owner,a)
    basis_dependent=world.commands.basis(world.owner,a,depends_on_group_volume=True)
    world.quote(b,tb,volume='500')
    denied=world.queue(a,kind='ASK_VOLUME',basis=basis_dependent)
    assert denied['state']=='REJECTED' and denied['error']['reason']=='STALE_BASIS'
    accepted=world.queue(a,basis=basis_independent)
    assert accepted['state']=='QUEUED'
    assert world.store.quote(world.owner,a,'ofr').price is None
def test_at_most_one_parallel_claim_gets_the_native_execution_envelope(world):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    def claim(_):
        return Commands(Store(world.url,clock=world.clock)).claim(world.owner,cid)
    with ThreadPoolExecutor(max_workers=5) as pool:
        values=list(pool.map(claim,range(5)))
    assert sum(v is not None for v in values)==1
def test_unknown_receipt_blocks_send_but_does_not_block_analysis(world):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    assert world.commands.claim(world.owner,cid)
    assert world.receipt(cid,'UNKNOWN')=='UNKNOWN'
    world.quote(m,t,price='3.15',volume='500')
    assert world.store.quote(world.owner,m,'ofr').price.value=='3.15'
    assert world.commands.get(world.owner,cid)['state']=='UNKNOWN'
    denied=world.queue(m)
    assert denied['state']=='REJECTED' and denied['error']['reason']=='MEMBER_HAS_PENDING_COMMAND'
@pytest.mark.parametrize('terminal',['FAILED','CANCELLED'])
def test_definite_failure_or_cancel_releases_only_its_pending_reference(world,terminal):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    world.commands.claim(world.owner,cid)
    assert world.receipt(cid,terminal)==terminal
    assert world.member(m)['pending_command_id'] is None
    assert json.loads(world.member(m)['our_quote_json'])=={}
    next_id=world.queue(m)['command_id']
    assert next_id
    assert world.receipt(cid,terminal)==terminal
    assert world.member(m)['pending_command_id']==next_id
def test_expired_queued_command_cleans_pending_without_pretending_sent(world):
    g,m,t=world.make()
    cid=world.queue(m,expires=world.clock()+1)['command_id']
    world.clock.tick(2)
    assert world.commands.claim(world.owner,cid) is None
    row=world.commands.get(world.owner,cid)
    assert row['state']=='CANCELLED'
    assert json.loads(row['result_json'])['reason']=='EXPIRED_BEFORE_DISPATCH'
    assert world.member(m)['pending_command_id'] is None
def test_stop_after_claim_requires_receiver_cancel_but_analysis_continues(world):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    world.commands.claim(world.owner,cid)
    world.store.set_sending(world.owner,False,member_id=m)
    assert not world.commands.execution_permission(world.owner,cid)
    assert world.commands.get(world.owner,cid)['cancel_requested']==1
    assert world.commands.get(world.owner,cid)['state']=='PENDING'
    world.quote(m,t)
    assert world.store.quote(world.owner,m,'ofr').volume_wan=='500'
def test_late_success_records_our_actual_sent_price_without_resuming_automation(world):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    world.commands.claim(world.owner,cid)
    world.store.set_sending(world.owner,False,group_id=g)
    world.clock.tick()
    assert world.receipt(cid,'SENT','native-synthetic-1')=='SENT'
    member=world.member(m)
    assert json.loads(member['our_quote_json'])['price']['value']=='3.14'
    assert member['pending_command_id'] is None
    assert world.store.group(world.owner,g)['send_allowed']==0
    assert world.store.context(world.owner,m)[0]['native_id']=='native-synthetic-1'
    raw=world.store.messages(world.owner,t)[0]
    echo=world.store.ingest(world.owner,t,event_id='native-echo-event',source_seq=10,
        text=raw['text'],trade_day='2026-10-17',direction='out',native_id='native-synthetic-1')
    assert echo==raw['message_id'] and len(world.store.messages(world.owner,t))==1
def test_contradictory_receipt_preserves_fact_and_requires_resolution(world):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    world.commands.claim(world.owner,cid)
    world.receipt(cid,'FAILED')
    assert world.receipt(cid,'SENT','unexpected-native-evidence')=='UNKNOWN'
    row=world.commands.get(world.owner,cid)
    assert json.loads(row['result_json'])['conflict']
    assert json.loads(world.member(m)['our_quote_json'])['native_id']=='unexpected-native-evidence'
    assert world.member(m)['send_allowed']==0
def test_one_hour_unresponsive_never_implies_final_price(world):
    g,m,t=world.make()
    world.quote(m,t,finality='NEGOTIABLE')
    cid=world.queue(m)['command_id']
    world.commands.claim(world.owner,cid)
    world.receipt(cid,'SENT','question-native')
    world.analyze(t)
    world.clock.tick(3599)
    assert world.commands.mark_unresponsive(world.owner)==[]
    world.clock.tick(1)
    assert world.commands.mark_unresponsive(world.owner)==[m]
    assert world.store.quote(world.owner,m,'ofr').finality=='NEGOTIABLE'
    assert world.member(m)['response_state']=='UNRESPONSIVE'
def test_new_raw_reply_defers_timeout_until_analysis_classifies_its_bond(world):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    world.commands.claim(world.owner,cid)
    world.receipt(cid,'SENT','q-native')
    world.analyze(t)
    world.clock.tick(3600)
    raw=world.message(t,'this bond reply')
    assert world.commands.mark_unresponsive(world.owner)==[]
    world.analyze(t,{raw:[m]})
    assert world.member(m)['response_state']=='REPLIED'
    assert world.commands.mark_unresponsive(world.owner)==[]
def test_cross_day_actual_reconfirmation_then_reply_makes_new_day_quote_usable(world):
    g,m,t=world.make()
    world.quote(m,t)
    world.store.rollover(world.owner,g,trade_day='2026-10-19')
    assert world.queue(m)['error']['reason']=='DAILY_RECONFIRMATION_REQUIRED'
    cid=world.queue(m,kind='RECONFIRM')['command_id']
    assert world.commands.claim(world.owner,cid)
    world.receipt(cid,'SENT','new-day-question')
    world.analyze(t)
    world.quote(m,t,day='2026-10-19',price='3.11')
    assert world.member(m)['reconfirm_day'] is None
    assert world.store.quote(world.owner,m,'ofr').confirmed_day=='2026-10-19'
def test_wrong_account_or_missing_native_id_receipt_cannot_claim_delivery(world):
    g,m,t=world.make()
    cid=world.queue(m)['command_id']
    world.commands.claim(world.owner,cid)
    with pytest.raises(InvalidFact):world.receipt(cid,'SENT')
    row=world.commands.get(world.owner,cid)
    with pytest.raises(InvalidFact):
        world.commands.receipt(world.owner,cid,account_uid='wrong',peer_uid='peer-01',
            content_hash=row['content_hash'],status='FAILED',evidence='synthetic')
    assert world.commands.get(world.owner,cid)['state']=='PENDING'
