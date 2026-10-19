"""Account-scoped unique-person admission; paused analysis retains its slot."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from server.negotiation.prices import InvalidFact,LimitPolicy
from test_reference_window_policy import Desk,OWNER,DAY
def fill(case,count=50):
    peers=['synthetic-peer']+[f'synthetic-peer-{i}' for i in range(1,count)]
    pairs=[(case.member,case.thread)]
    for peer in peers[1:]:
        pairs.append(case.store.add_member(OWNER,case.group,account_uid='synthetic-account',peer_uid=peer))
    return peers,pairs
def test_fifty_allowed_fifty_first_rejected_without_partial_thread(tmp_path):
    c=Desk(tmp_path/'capacity.sqlite');fill(c)
    assert c.store.counterparty_capacity(OWNER,'synthetic-account')['active']==50
    before=c.store.snapshot(OWNER)
    with pytest.raises(InvalidFact,match='ACTIVE_COUNTERPARTY_LIMIT'):
        c.store.add_member(OWNER,c.group,account_uid='synthetic-account',peer_uid='person-51')
    assert c.store.snapshot(OWNER)==before
def test_same_fifty_people_can_negotiate_multiple_bonds_without_extra_people(tmp_path):
    c=Desk(tmp_path/'multibond.sqlite');peers,_=fill(c)
    for code in ('SYN-B','SYN-C'):
        group=c.store.create_group(OWNER,bond_code=code,our_side='BUY',target_volume='10000',
            trade_day=DAY,policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=True)
        for peer in peers:c.store.add_member(OWNER,group,account_uid='synthetic-account',peer_uid=peer)
    assert c.store.counterparty_capacity(OWNER,'synthetic-account')['active']==50
    assert sum(len(g['peers']) for g in c.store.snapshot(OWNER)['groups'])==150
def test_pause_does_not_free_an_analysis_slot_and_account_isolation(tmp_path):
    c=Desk(tmp_path/'paused.sqlite');fill(c)
    c.store.set_sending(OWNER,False,group_id=c.group)
    with pytest.raises(InvalidFact,match='ACTIVE_COUNTERPARTY_LIMIT'):
        c.store.add_member(OWNER,c.group,account_uid='synthetic-account',peer_uid='extra')
    # A genuinely different trader/account has its own quota.
    group=c.store.create_group('other-owner',bond_code='SYN-Z',our_side='BUY',target_volume='1',trade_day=DAY,
        policy=LimitPolicy('ABSOLUTE','3.12'))
    c.store.add_member('other-owner',group,account_uid='other-account',peer_uid='extra')
    assert c.store.counterparty_capacity('other-owner','other-account')['active']==1
def test_handoff_releases_last_group_only(tmp_path):
    c=Desk(tmp_path/'handoff.sqlite');peers,_=fill(c)
    second=c.store.create_group(OWNER,bond_code='SYN-B',our_side='BUY',target_volume='1',trade_day=DAY,
        policy=LimitPolicy('ABSOLUTE','3.12'))
    c.store.add_member(OWNER,second,account_uid='synthetic-account',peer_uid=peers[0])
    with c.store.write(OWNER) as tx:
        tx.execute("UPDATE xj2_group SET state='HANDOFF',send_allowed=0 WHERE group_id=?",(c.group,))
    assert c.store.counterparty_capacity(OWNER,'synthetic-account')['active']==1
def test_concurrent_admission_cannot_both_take_last_slot(tmp_path):
    c=Desk(tmp_path/'race.sqlite');fill(c,49)
    def add(peer):
        try:c.store.add_member(OWNER,c.group,account_uid='synthetic-account',peer_uid=peer);return True
        except InvalidFact as exc:
            assert 'ACTIVE_COUNTERPARTY_LIMIT' in str(exc);return False
    with ThreadPoolExecutor(max_workers=2) as executor:answers=list(executor.map(add,['race-a','race-b']))
    assert sorted(answers)==[False,True]
    assert c.store.counterparty_capacity(OWNER,'synthetic-account')['active']==50
def test_preview_and_confirm_recheck_distinct_total_atomically(tmp_path):
    c=Desk(tmp_path/'preview.sqlite');fill(c,49)
    def item(code,peers):return {'code':code,'name':code,'our_side':'BUY','peer_ids':peers,
        'volume_wan':'1000','step_bp':'1','opening_yield_pct':'3.15','limit_yield_pct':'3.12','trade_day':DAY}
    with pytest.raises(InvalidFact,match='ACTIVE_COUNTERPARTY_LIMIT'):
        c.service.preview(OWNER,'over',[item('SYN-B',['new-1','new-2'])])
    preview=c.service.preview(OWNER,'fits',[item('SYN-B',['new-1'])])
    c.store.add_member(OWNER,c.group,account_uid='synthetic-account',peer_uid='racer')
    groups=len(c.store.snapshot(OWNER)['groups'])
    with pytest.raises(InvalidFact,match='ACTIVE_COUNTERPARTY_LIMIT'):
        c.service.confirm(OWNER,preview['batch_id'],1,'confirm')
    assert len(c.store.snapshot(OWNER)['groups'])==groups
    with c.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT state FROM xj2_batch WHERE batch_id=?',(preview['batch_id'],)).fetchone()['state']=='PREVIEW'
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']==0
def test_fair_pages_cover_multibond_members_above_one_hundred(tmp_path):
    c=Desk(tmp_path/'pages.sqlite');peers,pairs=fill(c)
    for code in ('SYN-B','SYN-C'):
        group=c.store.create_group(OWNER,bond_code=code,our_side='BUY',target_volume='1000',trade_day=DAY,
            policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=True)
        pairs.extend(c.store.add_member(OWNER,group,account_uid='synthetic-account',peer_uid=p) for p in peers)
    c.store.set_sending(OWNER,True)
    target,thread=max(pairs)
    ref=c.store.ingest(OWNER,thread,event_id='last-pending',source_seq=1,text='稍等',trade_day=DAY)
    job=c.store.claim_analysis(OWNER,thread,force=True)
    c.store.commit_analysis(OWNER,job['analysis_id'],job['lease_token'],scopes={ref:[target]},observations=[])
    called=[]
    c.flows.decide=lambda context:called.append(context['member_id']) or {'schema_version':'2.2','action':'WAIT','proposal':None}
    for _ in range(2):c.engine.run_once(force=True)
    assert called==[target]