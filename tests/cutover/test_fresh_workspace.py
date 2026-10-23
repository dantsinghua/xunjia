"""Workspace retirement: real DB/routes/codecs, synthetic vendor only."""
import json
from pathlib import Path
import pytest
from tests.institution_test_support import selection, OWNER, collector
from tests.cutover.test_im_catalog_selection import seed_business, choose
from server.receiver_ledger import ReceiverLedger
from relay.gateway_paths import CONTRACT
from relay.protocol import ProtocolError, digest
from server.negotiation.prices import InvalidFact


def seed_command(s, state='UNKNOWN'):
    gid,mid,tid=seed_business(s)
    s['repository'].ingest(OWNER,tid,event_id='old-native',source_seq=1,text='old account content',trade_day='2026-10-22')
    with s['repository'].write(OWNER) as tx:
        tx.execute('INSERT INTO xj2_command(command_id,request_id,member_id,group_id,thread_id,state,content_hash,body_json,basis_json,result_json,cancel_requested,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('old-command','old-request',mid,gid,tid,state,'a'*64,'{}','{}','{}',0,s['now'],s['now']+120))
    return gid,mid,tid


def counts(s):
    with s['repository'].db.transaction(write=False) as tx:
        return {table:tx.execute('SELECT COUNT(*) AS n FROM '+table).fetchone()['n'] for table in
            ('xj2_group','xj2_peer','xj2_message','xj2_command','xj2_batch','xj2_thread')}


@pytest.mark.parametrize('state',['QUEUED','PENDING','UNKNOWN','FAILED','CANCELLED','SENT'])
def test_switch_retains_old_business_states_and_isolates_new_context(selection,state):
    s=selection;_,mid,tid=seed_command(s,state);before=counts(s)
    with s['repository'].db.transaction(write=False) as tx:
        command=dict(tx.execute('SELECT * FROM xj2_command WHERE command_id=?',('old-command',)).fetchone())
        messages=[dict(r) for r in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(tid,)).fetchall()]
    old=s['repository'].binding_for(OWNER)
    result=choose(s).json()['data']
    assert result['reset'] is True and result['binding']['context_id']!=old['context_id']
    assert counts(s)==before
    with s['repository'].db.transaction(write=False) as tx:
        retained=dict(tx.execute('SELECT * FROM xj2_command WHERE command_id=?',('old-command',)).fetchone())
        assert retained['state']==('CANCELLED' if state=='QUEUED' else state)
        for key in ('content_hash','body_json','basis_json','member_id','thread_id'):
            assert retained[key]==command[key]
        if state!='QUEUED':assert retained['result_json']==command['result_json']
        assert [dict(r) for r in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(tid,)).fetchall()]==messages
    with pytest.raises(InvalidFact,match='selected collector/account'):
        s['repository'].context(OWNER,mid)
    assert s['app'].state.inquiry.commands_core.claim(OWNER,'old-command') is None
    assert s['repository'].binding_for(OWNER)['account_uid']=='account-c'
    assert not s['repository'].binding_for(OWNER)['bot_enabled']
    assert s['api'].get('/api/im/commands/unknown').json()['data']==[]
    assert s['api'].get('/api/monitor/counterparties').json()['data']==[]
    assert result['contacts']['contacts'][0]['uin']=='peer-wb-b-account-c'


def test_same_pair_save_is_not_a_destructive_reset_even_with_unknown(selection):
    s=selection;seed_command(s);before=counts(s);binding=s['repository'].binding_for(OWNER)
    with s['repository'].write(OWNER) as tx:tx.execute('UPDATE xj2_owner SET send_allowed=1 WHERE owner_id=?',(OWNER,))
    response=choose(s,'wb-a','account-a');assert response.status_code==200,response.text
    assert not response.json()['data']['reset'] and counts(s)==before
    assert s['repository'].binding_for(OWNER)['context_id']==binding['context_id']
    assert s['repository'].binding_for(OWNER)['version']==binding['version']


def test_old_unmarked_same_account_is_reset_not_migrated(selection):
    s=selection;_,mid,_=seed_command(s);before=counts(s)
    with s['repository'].write(OWNER) as tx:
        tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?',('{}',OWNER))
    result=choose(s,'wb-a','account-a')
    assert result.status_code==200,result.text
    assert result.json()['data']['reset'] and counts(s)==before
    assert s['repository'].context(OWNER,mid)==[]
    assert s['app'].state.inquiry.commands_core.claim(OWNER,'old-command') is None


def test_context_handshake_roundtrip_and_restart_recovery(selection,tmp_path):
    s=selection
    assert choose(s,'wb-b','account-c').status_code==200
    channel=collector(s,'wb-b','account-c');ledger=ReceiverLedger(tmp_path/'receiver.db',channel.binding,clock=lambda:s['clock'][0])
    def control(data):
        p=data['payload'];assert p['op']=='activate_context'
        state=ledger.activate_context(p['context_id'],p['context_version'],published_source_highwater=channel.store.source_highwater)
        channel.publish_manifest({'contract':CONTRACT,'op':'context_ready',**state})
    channel.manifest_handler=control
    active=s['runtime'].current_adapter(OWNER);assert not active.context_ready;active._context_last=0
    s['runtime'].request_context(active)
    channel.poll();s['app'].state.inbox.poll_once()
    active=s['runtime'].current_adapter(OWNER)
    assert active.context_ready
    restored=s['make_app']().state.institution.current_adapter(OWNER)
    assert restored.context_ready and restored.context_id==active.context_id
    assert counts(s)['xj2_command']==0


def test_replacement_config_clears_binding_and_retains_isolated_business(selection):
    s=selection;seed_command(s);before=counts(s)
    status=s['api'].get('/api/im/mq-config').json()['data']
    response=s['api'].put('/api/im/mq-config',json={'base_url':'https://replacement.invalid',
        'config_yaml':'','expected_revision':status['revision']})
    assert response.status_code==200,response.text
    assert response.json()['data']['reset']
    assert s['repository'].binding_for(OWNER) is None
    assert counts(s)==before
    assert s['app'].state.transport.adapters=={}
    assert json.loads(s['connection'].read_text())['base_url']=='https://replacement.invalid'
    assert s['api'].get('/api/im/catalog').json()['data']['selected'] is None


def test_same_config_and_failed_candidate_never_clear_business(selection):
    s=selection;seed_command(s);before=counts(s);binding=s['repository'].binding_for(OWNER)
    status=s['api'].get('/api/im/mq-config').json()['data']
    for body,expected in [({'base_url':status['base_url'],'config_yaml':''},200),
                          ({'base_url':'bad://invalid','config_yaml':''},400),
                          ({'base_url':status['base_url'],'config_yaml':'not-a-valid-key'},400)]:
        response=s['api'].put('/api/im/mq-config',json={**body,'expected_revision':status['revision']})
        assert response.status_code==expected,response.text
        assert counts(s)==before and s['repository'].binding_for(OWNER)==binding


def test_offline_contacts_remain_readable_but_no_sync_or_send(selection):
    s=selection;seed_command(s);s['advance'](61);before=counts(s);calls=list(s['wire'].calls)
    book=s['api'].get('/api/im/contacts')
    assert book.status_code==200 and book.json()['data']['stale']
    assert s['wire'].calls==calls and counts(s)==before
    refresh=s['api'].get('/api/im/contacts?refresh=1')
    assert refresh.status_code==400
    assert '心跳' in refresh.text or '收发未就绪' in refresh.text
    assert counts(s)==before


def test_disabled_dispatch_rejects_new_direct_submission_without_queue(selection):
    s=selection;s['app'].state.inquiry.submit_guard=s['app'].state.channel_admission
    token=s['repository'].binding_for(OWNER)['context_id']
    response=s['api'].post('/api/im/rfq/send-direct',headers={'X-Xunjia-Context':token},json={
        'request_id':'no-dispatch','items':[{'code':'SYN-NEW','our_side':'BUY','volume_wan':'1000',
        'opening_yield_pct':'2','limit_yield_pct':'2.5','step_bp':'2','peer_ids':['peer-a']}]})
    assert response.status_code==503,response.text
    assert response.json()['submitted'] is False and response.json()['code']=='DISPATCH_DISABLED'
    assert all(v==0 for v in counts(s).values())


def test_stale_browser_cannot_submit_to_reselected_account(selection):
    s=selection;old=s['repository'].binding_for(OWNER)['context_id'];assert choose(s).status_code==200
    response=s['api'].post('/api/im/rfq/send-direct',headers={'X-Xunjia-Context':old},json={'request_id':'old-tab','items':[]})
    assert response.status_code==400 and response.json()['code']=='ACCOUNT_CONTEXT_CHANGED'
    assert response.json()['submitted'] is False and counts(s)['xj2_command']==0


def test_wrong_account_late_receipt_does_not_mutate_retained_unknown(selection):
    s=selection;seed_command(s);before=counts(s);assert choose(s).status_code==200
    with s['repository'].db.transaction(write=False) as tx:
        retained=dict(tx.execute('SELECT * FROM xj2_command WHERE command_id=?',('old-command',)).fetchone())
    adapter=s['runtime'].current_adapter(OWNER)
    with pytest.raises(InvalidFact,match='another collector thread'):
        s['app'].state.inbox.apply(adapter.binding,{'kind':'receipt','owner_user_id':OWNER,
            'account_uid':'account-c','workbench_id':'wb-b','payload':{'command_id':'old-command'}})
    assert counts(s)==before
    with s['repository'].db.transaction(write=False) as tx:
        assert dict(tx.execute('SELECT * FROM xj2_command WHERE command_id=?',('old-command',)).fetchone())==retained


def test_receiver_context_retires_unknown_content_and_rejects_old_native_send(selection,tmp_path):
    s=selection
    assert choose(s,'wb-b','account-c').status_code==200
    channel=collector(s,'wb-b','account-c');ledger=ReceiverLedger(tmp_path/'receiver.db',channel.binding,clock=lambda:s['clock'][0])
    b=channel.binding
    base={'owner_user_id':b.owner_user_id,'account_uid':b.account_uid,'workbench_id':b.workbench_id,
          'peer_id':'peer-a','command_id':'old-cmd','control_version':1,'text':'synthetic never a real message',
          'content_hash':digest('synthetic'),'expires_at':s['now']+120,'transport':'oss','context_id':'old-context'}
    version=int(s['now']*1000000)
    ledger.activate_context('old-context',version)
    first=ledger.execute(base,lambda c:{'status':'unknown','evidence':'synthetic-unknown'})
    assert first['status']=='unknown'
    with ledger._db() as db:
        retained=dict(db.execute('SELECT * FROM rx_command WHERE command_id=?',('old-cmd',)).fetchone())
    ledger.activate_context('new-context',version+1)
    with ledger._db() as db:
        assert dict(db.execute('SELECT * FROM rx_command WHERE command_id=?',('old-cmd',)).fetchone())==retained
    calls=[]
    assert ledger.execute(base,lambda c:calls.append(c))==first
    assert calls==[]
    with ledger._db() as db:
        assert dict(db.execute('SELECT * FROM rx_command WHERE command_id=?',('old-cmd',)).fetchone())==retained
    new={**base,'command_id':'new-cmd','context_id':'new-context'}
    result=ledger.execute(new,lambda c:(calls.append(c) or {'status':'sent','evidence':'synthetic-native','native_id':'native-new'}))
    assert result['status']=='sent' and len(calls)==1
    with pytest.raises(ProtocolError,match='SUPERSEDED'):ledger.activate_context('old-context',version)
