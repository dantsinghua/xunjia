"""Persisted member HANDOFF projection, using synthetic isolated application state."""
import json
import pytest
from server.negotiation.application_store import ApplicationStore, status_label
from server.negotiation.commands import Commands
from server.negotiation.prices import LimitPolicy, InvalidFact
from server.negotiation.service import Inquiry
from server.negotiation.store import encoded

NOW=1800000000
OWNER='synthetic-owner'


def desk(tmp_path,owner=OWNER):
    repo=ApplicationStore('sqlite:///'+str(tmp_path/'handoff.db'),initialize=True,clock=lambda:NOW)
    repo.bind_account(owner,'synthetic-account','synthetic-workbench')
    gid=repo.create_group(owner,bond_code='SYN-A',our_side='BUY',target_volume='2000',
        trade_day='2027-01-15',policy=LimitPolicy('ABSOLUTE','3.00'),send_allowed=True)
    first,_=repo.add_member(owner,gid,account_uid='synthetic-account',peer_uid='peer-a')
    second,_=repo.add_member(owner,gid,account_uid='synthetic-account',peer_uid='peer-b')
    return repo,gid,first,second


def record_handoff(repo,mid,decision_id='synthetic-handoff',state='HANDOFF',owner=OWNER):
    with repo.write(owner)as tx:
        basis=Commands(repo)._basis(tx,owner,mid,True)
        tx.execute('UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE member_id=?',(mid,))
        repo._cancel_commands(tx,member_id=mid,reason='CHATFLOW_HANDOFF')
        tx.execute('INSERT INTO xj2_decision(decision_id,member_id,basis_json,state,result_json,created_at) VALUES(?,?,?,?,?,?)',
                   (decision_id,mid,encoded(basis),state,encoded({'state':state,'trader_view':{}}),NOW))


def rows(repo):
    groups=repo.group_rows(OWNER);group=groups[0]
    return group,{p['member_id']:p for p in group['peers']}


def test_member_handoff_is_visible_and_does_not_complete_or_stop_other_peer(tmp_path):
    repo,gid,first,second=desk(tmp_path);record_handoff(repo,first)
    group,peers=rows(repo)
    assert group['state']=='OPEN' and group['send_allowed']==1
    assert group['summary']['goal_ready'] is False
    assert peers[first]['member_handoff_pending'] is True
    assert peers[second]['member_handoff_pending'] is False and peers[second]['send_allowed']==1
    assert status_label(group,peers[first])=='待人工确认'
    assert status_label(group,peers[second])=='询价中'
    assert status_label(group)=='询价中'
    sessions={s['session_id']:s for s in Inquiry(repo).sessions(OWNER)}
    assert sessions[first]['state']=='HANDOFF' and sessions[second]['state']=='NEGOTIATING'


def test_handoff_survives_restart_but_resume_then_pause_does_not_reuse_old_decision(tmp_path):
    repo,_,first,_=desk(tmp_path);record_handoff(repo,first)
    restored=ApplicationStore('sqlite:///'+str(tmp_path/'handoff.db'),clock=lambda:NOW)
    group,peers=rows(restored);assert status_label(group,peers[first])=='待人工确认'
    restored.set_sending(OWNER,True,member_id=first)
    group,peers=rows(restored);assert status_label(group,peers[first])=='询价中'
    restored.set_sending(OWNER,False,member_id=first)
    group,peers=rows(restored);assert status_label(group,peers[first])=='已暂停'
    assert peers[first]['member_handoff_pending'] is False
    restored.set_sending(OWNER,True,member_id=first)
    record_handoff(restored,first,decision_id='a-new-same-time')
    group,peers=rows(restored);assert status_label(group,peers[first])=='待人工确认'


def test_wait_rejected_or_malformed_decisions_do_not_claim_handoff(tmp_path):
    repo,_,first,_=desk(tmp_path)
    record_handoff(repo,first,state='WAIT')
    with repo.write(OWNER)as tx:
        for n,basis in enumerate(({'member_control':True},{'member_control':1,'member_id':'another-member'},None)):
            tx.execute('INSERT INTO xj2_decision(decision_id,member_id,basis_json,state,result_json,created_at) VALUES(?,?,?,?,?,?)',
                       ('invalid-'+str(n),first,encoded(basis),'HANDOFF','{}',NOW))
    group,peers=rows(repo)
    assert status_label(group,peers[first])=='已暂停'
    assert peers[first]['member_handoff_pending'] is False


def test_group_goal_handoff_keeps_new_round_boundary_and_read_projection_is_not_a_write(tmp_path):
    repo,gid,first,_=desk(tmp_path);record_handoff(repo,first)
    with repo.write(OWNER)as tx:
        tx.execute("UPDATE xj2_group SET state='HANDOFF',send_allowed=0 WHERE group_id=?",(gid,))
    before=repo.snapshot(OWNER)
    group,peers=rows(repo)
    assert status_label(group)=='待人工确认'
    assert all(s['state']=='HANDOFF'for s in Inquiry(repo).sessions(OWNER))
    assert repo.snapshot(OWNER)==before
    with pytest.raises(InvalidFact,match='explicit new round'):repo.set_sending(OWNER,True,group_id=gid)


def test_other_owner_does_not_receive_member_handoff(tmp_path):
    repo,_,first,_=desk(tmp_path);record_handoff(repo,first)
    other='synthetic-other';repo.bind_account(other,'another-account','another-workbench')
    gid=repo.create_group(other,bond_code='SYN-A',our_side='BUY',target_volume='2000',trade_day='2027-01-15',policy=LimitPolicy('ABSOLUTE','3.00'),send_allowed=True)
    mid,_=repo.add_member(other,gid,account_uid='another-account',peer_uid='peer-a')
    group=repo.group_rows(other)[0]
    assert group['peers'][0]['member_handoff_pending'] is False
    assert Inquiry(repo).sessions(other)[0]['state']=='NEGOTIATING'
