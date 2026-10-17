"""Persistent facts/analysis contract; no production database or transports."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from server.negotiation.prices import InvalidFact, LimitPolicy, Price, StaleDecision, Valuation
from server.negotiation.store import Store
def test_raw_immediate_and_analysis_runs_with_all_send_controls_paused(world):
    g,m,t=world.make(enabled=False)
    world.store.set_sending(world.owner,False)
    world.store.set_sending(world.owner,False,member_id=m)
    raw=world.message(t,'2.20, 0.5亿')
    assert world.store.messages(world.owner,t)[0]['text']=='2.20, 0.5亿'
    changes={'price':Price('YIELD','2.20').dump(),'volume_wan':'5000','confirmed_day':'2026-10-17'}
    result=world.analyze(t,{raw:[m]},[world.observation(m,raw,changes)])
    assert len(result['accepted'])==1
    assert world.store.quote(world.owner,m,'ofr').volume_wan=='5000'
    assert world.store.group(world.owner,g)['send_allowed']==0
def test_same_thread_two_bonds_bid_ofr_and_two_counterparties_do_not_mix(world):
    ga,a,t=world.make('SYN-A')
    gb,b,t2=world.make('SYN-B')
    a2,t3=world.store.add_member(world.owner,ga,account_uid='synthetic-account',peer_uid='peer-02')
    assert t==t2 and t!=t3
    raw=world.message(t,'A bid 2.20 1000; B ofr 3.10 2000')
    aa=world.observation(a,raw,{'price':Price('YIELD','2.20').dump(),'volume_wan':'1000'},'bid')
    bb=world.observation(b,raw,{'price':Price('YIELD','3.10').dump(),'volume_wan':'2000'})
    result=world.analyze(t,{raw:[a,b]},[aa,bb])
    assert len(result['accepted'])==2
    assert world.store.quote(world.owner,a,'ofr').price is None
    assert world.store.quote(world.owner,a,'bid').volume_wan=='1000'
    assert world.store.quote(world.owner,b,'ofr').volume_wan=='2000'
    assert world.store.quote(world.owner,a2,'ofr').price is None
def test_wrong_bond_evidence_rejected_without_losing_correct_other_bond(world):
    ga,a,t=world.make('SYN-A')
    gb,b,_=world.make('SYN-B')
    world.quote(a,t)
    raw=world.message(t,'Explicitly SYN-B 3.04 ofr')
    wrong=world.observation(a,raw,{'price':Price('YIELD','3.04').dump()})
    correct=world.observation(b,raw,{'price':Price('YIELD','3.04').dump()})
    result=world.analyze(t,{raw:[b]},[wrong,correct])
    assert len(result['rejected'])==1 and len(result['accepted'])==1
    assert world.store.quote(world.owner,a,'ofr').price.value=='3.10'
    assert world.store.quote(world.owner,b,'ofr').price.value=='3.04'
def test_price_first_quantity_later_is_partial_update_not_a_global_stop(world):
    g,m,t=world.make()
    p=world.message(t,'2.20')
    world.analyze(t,{p:[m]},[world.observation(m,p,{'price':Price('YIELD','2.20').dump()})])
    assert world.store.quote(world.owner,m,'ofr').volume_wan is None
    v=world.message(t,'0.5亿')
    world.analyze(t,{v:[m]},[world.observation(m,v,{'volume_wan':'5000'})])
    quote=world.store.quote(world.owner,m,'ofr')
    assert quote.price.value=='2.20' and quote.volume_wan=='5000'
    assert quote.evidence['price']==[p] and quote.evidence['volume_wan']==[v]
    assert world.store.group(world.owner,g)['state']=='OPEN'
def test_lease_recovery_preserves_snapshot_and_rejects_late_worker(world):
    g,m,t=world.make()
    first=world.message(t)
    job=world.store.claim_analysis(world.owner,t,force=True,lease_seconds=5)
    later=world.message(t,'later quote')
    world.clock.tick(6)
    recovery=Store(world.url,clock=world.clock)
    job2=recovery.claim_analysis(world.owner,t,force=True)
    assert job2['analysis_id']==job['analysis_id'] and job2['lease_token']!=job['lease_token']
    assert [x['message_id'] for x in job2['snapshot']['messages']]==[first]
    with pytest.raises(StaleDecision):
        world.store.commit_analysis(world.owner,job['analysis_id'],job['lease_token'],scopes={first:[m]},observations=[])
    recovery.commit_analysis(world.owner,job2['analysis_id'],job2['lease_token'],scopes={first:[m]},observations=[])
    next_job=recovery.claim_analysis(world.owner,t,force=True)
    assert [x['message_id'] for x in next_job['snapshot']['messages']]==[later]
def test_same_analysis_commit_replay_does_not_duplicate_facts(world):
    g,m,t=world.make()
    raw=world.message(t)
    job=world.store.claim_analysis(world.owner,t,force=True)
    obs=[world.observation(m,raw,{'volume_wan':'500'})]
    args=dict(scopes={raw:[m]},observations=obs)
    one=world.store.commit_analysis(world.owner,job['analysis_id'],job['lease_token'],**args)
    two=world.store.commit_analysis(world.owner,job['analysis_id'],job['lease_token'],**args)
    assert one==two and world.store.quote(world.owner,m,'ofr').revision==1
    with pytest.raises(InvalidFact):
        world.store.commit_analysis(world.owner,job['analysis_id'],job['lease_token'],scopes={raw:[m]},observations=[])
def test_two_workers_cannot_claim_the_same_live_lease(world):
    g,m,t=world.make()
    world.message(t)
    def claim(_):
        return Store(world.url,clock=world.clock).claim_analysis(world.owner,t,force=True)
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs=list(pool.map(claim,range(5)))
    assert sum(x is not None for x in jobs)==1
def test_failed_analysis_retries_without_hiding_raw_message(world):
    g,m,t=world.make(enabled=False)
    raw=world.message(t)
    job=world.store.claim_analysis(world.owner,t,force=True)
    world.store.fail_analysis(world.owner,job['analysis_id'],job['lease_token'],reason='synthetic model timeout',retry_after_seconds=1)
    assert world.store.messages(world.owner,t)[0]['message_id']==raw
    assert world.store.claim_analysis(world.owner,t,force=True) is None
    world.clock.tick(2)
    recovered=world.store.claim_analysis(world.owner,t,force=True)
    assert recovered['attempt']==2
def test_whole_lot_goal_pauses_only_target_group_analysis_still_updates(world):
    ga,a,t=world.make('SYN-A',target='1000')
    gb,b,_=world.make('SYN-B',target='1000')
    world.quote(a,t,price='3.14',volume='1000',finality='FINAL')
    assert world.store.group(world.owner,ga)['state']=='HANDOFF'
    assert world.store.group(world.owner,ga)['send_allowed']==0
    assert world.store.group(world.owner,gb)['state']=='OPEN'
    raw=world.message(t,'A withdrawn, B remains')
    world.analyze(t,{raw:[a]},[world.observation(a,raw,{'validity':'WITHDRAWN'})])
    group=world.store.group(world.owner,ga)
    assert group['state']=='HANDOFF' and group['review_needed']==1
    assert group['summary']['final_confirmed_volume_wan']=='0'
    assert world.store.group(world.owner,gb)['send_allowed']==1
def test_old_source_message_cannot_replace_newer_field_value(world):
    g,m,t=world.make()
    world.quote(m,t,price='3.10')
    newer=world.message(t,'3.11')
    world.analyze(t,{newer:[m]},[world.observation(m,newer,{'price':Price('YIELD','3.11').dump()})])
    old=world.store.ingest(world.owner,t,event_id='late-older-source',source_seq=0,text='3.09',trade_day='2026-10-17',occurred_at=world.clock()-10)
    result=world.analyze(t,{old:[m]},[world.observation(m,old,{'price':Price('YIELD','3.09').dump()})])
    assert result['rejected'] and world.store.quote(world.owner,m,'ofr').price.value=='3.11'
def test_all_rollovers_require_asking_same_peer_before_confirmation(world):
    g,m,t=world.make(limit='3.12')
    world.quote(m,t,price='3.10')
    world.store.rollover(world.owner,g,trade_day='2026-10-19')
    assert json.loads(world.store.group(world.owner,g)['boundary_json'])['value']=='3.12'
    assert world.store.quote(world.owner,m,'ofr').confirmed_day is None
    raw=world.message(t,day='2026-10-19')
    result=world.analyze(t,{raw:[m]},[world.observation(m,raw,{'confirmed_day':'2026-10-19','validity':'ACTIVE'})])
    assert result['rejected']
    assert world.member(m)['reconfirm_day']=='2026-10-19'
def test_context_default_is_20_bilateral_messages_with_explicit_anchors(world):
    g,m,t=world.make()
    all_ids=[]
    for i in range(22):
        raw=world.message(t,'history '+str(i))
        all_ids.append(raw)
        world.analyze(t,{raw:[m]})
    ctx=world.store.context(world.owner,m)
    assert len(ctx)==20 and ctx[0]['message_id']==all_ids[2]
    ctx=world.store.context(world.owner,m,anchors=[all_ids[0]])
    assert len(ctx)==21 and ctx[0]['message_id']==all_ids[0]
    g2,m2,t2=world.make('SYN-B','another-peer')
    other=world.message(t2)
    with pytest.raises(InvalidFact):world.store.context(world.owner,m,anchors=[other])


def test_replayed_event_without_native_time_is_still_idempotent(world):
    g,m,t=world.make()
    args=dict(event_id='delivery-without-native-time',source_seq=1,text='same content',trade_day='2026-10-17')
    first=world.store.ingest(world.owner,t,**args)
    world.clock.tick(5)
    second=world.store.ingest(world.owner,t,**args)
    assert first==second and len(world.store.messages(world.owner,t))==1
    with pytest.raises(InvalidFact):
        world.store.ingest(world.owner,t,**{**args,'text':'different content'})
