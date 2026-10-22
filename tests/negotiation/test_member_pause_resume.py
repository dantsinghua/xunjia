"""Peer/bond-only pauses keep previous negotiations, facts and unknown receipts."""
import pytest


@pytest.mark.parametrize('state',['QUEUED','PENDING','UNKNOWN'])
def test_pause_resume_never_replays_old_command_or_changes_other_peer(world,state):
    group,member,thread=world.make()
    other,_=world.store.add_member(world.owner,group,account_uid='synthetic-account',peer_uid='peer-other')
    group_b,member_b,_=world.make('SYN-B')
    world.quote(member,thread)
    old_quote=world.store.quote(world.owner,member,'ofr').dump()
    old_context=world.store.context(world.owner,member)
    cid=world.queue(member)['command_id']
    if state in ('PENDING','UNKNOWN'):assert world.commands.claim(world.owner,cid)
    if state=='UNKNOWN':assert world.receipt(cid,'UNKNOWN')=='UNKNOWN'
    world.store.set_sending(world.owner,False,member_id=member)
    paused=world.commands.get(world.owner,cid)
    assert paused['state']==('CANCELLED' if state=='QUEUED' else state)
    if state!='QUEUED':assert paused['cancel_requested']==1
    assert world.store.group(world.owner,group)['send_allowed']==1
    assert world.member(other)['send_allowed']==1 and world.member(member_b)['send_allowed']==1
    assert world.store.group(world.owner,group_b)['send_allowed']==1
    world.store.set_sending(world.owner,True,member_id=member)
    resumed=world.commands.get(world.owner,cid)
    assert resumed['state']==paused['state'] and resumed['cancel_requested']==paused['cancel_requested']
    assert world.commands.claim(world.owner,cid) is None
    assert world.store.quote(world.owner,member,'ofr').dump()==old_quote
    assert world.store.context(world.owner,member)==old_context
    assert world.member(member)['group_id']==group and world.member(member)['thread_id']==thread
    new=world.queue(member)
    if state=='QUEUED':assert new['state']=='QUEUED' and new['command_id']!=cid
    else:assert new['state']=='REJECTED' and new['error']['reason']=='MEMBER_HAS_PENDING_COMMAND'
