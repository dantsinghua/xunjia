"""Actual application reset API; temporary SQLite and existing signed carrier only."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.institution_transport import InstitutionTransport
from server.transports import TransportRouter
from tests.cutover.file_gateway_fakes import Gateway, NOW
from tests.cutover.test_session_reset_protocol import B as ORIGINAL_B
from tests.institution_test_support import build_harness, AUTH, collector
from relay.gateway_paths import InstitutionKeys, account_paths
B=InstitutionKeys.from_auth(AUTH).binding(ORIGINAL_B.workbench_id,ORIGINAL_B.account_uid)
UP_PATH=account_paths(B.workbench_id,B.account_uid)['uplink_path']
DOWN_PATH=account_paths(B.workbench_id,B.account_uid)['downlink_path']


@pytest.fixture
def reset_case(tmp_path,monkeypatch):
    s=build_harness(tmp_path,monkeypatch,selected=False)
    s['seed'](B.workbench_id,B.account_uid);s['refresh']()
    app=s['app'];client=s['api']
    token=app.state.auth.issue_token(AuthUser(B.owner_user_id,'isolated reset trader'))
    client.headers['Authorization']='Bearer '+token
    response=client.post('/api/im/binding/select',json={'workbench_id':B.workbench_id,'account_uid':B.account_uid,'expected_version':0})
    assert response.status_code==200,response.text
    adapter=app.state.transport.adapters[(B.owner_user_id,B.account_uid,'oss')]
    case=SimpleNamespace(app=app,client=client,gateway=s['wire'],adapter=adapter,path=tmp_path,
        now=s['clock'],source=s,body={'request_id':'synthetic-reset-request','expected_binding_version':1,'confirmed':True})
    setup_wire(case);case.initial_calls=list(s['wire'].calls)
    try:yield case
    finally:s['stack'].close()


def test_actual_current_binding_reset_api_exists_without_starting_background_or_native(reset_case):
    response=reset_case.client.get('/api/im/session-reset')
    assert response.status_code==200,response.text
    assert response.json()['ok'] is True
    assert reset_case.app.state.enable_background is False
    assert reset_case.gateway.calls==reset_case.initial_calls


def test_authenticated_confirmed_request_is_durable_not_falsely_complete(reset_case):
    response=reset_case.client.post('/api/im/session-reset',json=reset_case.body)
    assert response.status_code==200,response.text
    assert response.json()['ok'] is True
    assert response.json()['data']['state']=='requested' and response.json()['data']['snapshot'] is None
    assert reset_case.app.state.repository.binding_for(B.owner_user_id)['account_uid']==B.account_uid
    with reset_case.app.state.repository.db.transaction(write=False) as tx:
        rows=tx.execute('SELECT * FROM xj2_session_reset').fetchall()
        assert len(rows)==1
    assert reset_case.receiver.account_epoch()==0
    assert reset_case.gateway.queues[DOWN_PATH]==[], 'No control or text is sent by the disabled test lifecycle'


@pytest.mark.parametrize('change', [{'confirmed':False},{'expected_binding_version':99},
    {'owner_user_id':'foreign-owner'},{'account_uid':'foreign-account'},{'workbench_id':'foreign-workbench'}])
def test_scope_override_stale_binding_or_missing_explicit_confirmation_rejected(reset_case,change):
    response=reset_case.client.post('/api/im/session-reset',json={**reset_case.body,**change})
    assert response.status_code in (400,403,409),response.text
    with reset_case.app.state.repository.db.transaction(write=False) as tx:
        rows=tx.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='xj2_session_reset'").fetchall()
        if rows:assert tx.execute('SELECT COUNT(*) AS n FROM xj2_session_reset').fetchone()['n']==0
    assert reset_case.gateway.calls==reset_case.initial_calls


def setup_wire(case):
    if hasattr(case,'bridge'):return case
    from server.receiver_ledger import ReceiverLedger
    from relay.collector_bridge import CollectorBridge
    case.receiver=ReceiverLedger(case.path/'receiver.sqlite3',B,clock=lambda:case.now[0])
    case.collector=collector(case.source,B.workbench_id,B.account_uid,filename='collector.sqlite3')
    case.quiet=[True]
    case.bridge=CollectorBridge(ledger=case.receiver,adapters={'oss':case.collector},
        native_send=lambda _:pytest.fail('Reset handshake cannot execute native text'),
        native_reconcile=lambda _:None,native_quiescent=lambda:case.quiet[0])
    # Complete the current-account control handshake before testing a distinct reset.
    from relay.gateway_paths import CONTRACT
    def contexts(data):
        p=data['payload']
        if p.get('op')=='activate_context':
            state=case.receiver.activate_context(p['context_id'],p['context_version'],published_source_highwater=case.collector.store.source_highwater)
            case.collector.publish_manifest({'contract':CONTRACT,'op':'context_ready',**state})
        elif p.get('op')!='sync_contacts':raise AssertionError('unexpected control')
    case.collector.manifest_handler=contexts
    case.bridge.run_once();case.app.state.inbox.poll_once();case.collector.poll()
    case.bridge.publish_capabilities()
    return case


def draft(case,key='synthetic-old-draft',code='SYN-RESET',peers=None):
    return case.app.state.inquiry.preview(B.owner_user_id,key,[{'code':code,
        'peer_ids':peers or ['synthetic-peer'],'our_side':'BUY','volume_wan':'1000',
        'opening_yield_pct':'3.20','limit_yield_pct':'3.12','step_bp':'2'}])


def confirmed(case,key='synthetic-old',peers=None):
    v=draft(case,key,peers=peers)
    return case.app.state.inquiry.confirm(B.owner_user_id,v['batch_id'],v['version'],key+'-confirm')


def start(case,body=None):
    response=case.client.post('/api/im/session-reset',json=body or case.body)
    assert response.status_code==200,response.text
    return response.json()['data']


def operation(case):
    response=case.client.get('/api/im/session-reset');assert response.status_code==200,response.text
    return response.json()['data']


def pump(case):
    case.app.state.session_reset.run_once()
    case.bridge.run_once()
    case.app.state.inbox.poll_once()
    return operation(case)


def finish(case):
    for _ in range(8):
        current=pump(case)
        if current['state']=='complete':return current
        case.now[0]+=6
    pytest.fail('Actual signed reset handshake did not reach complete: '+repr(current))


def test_real_signed_handshake_archives_scope_preserves_unknown_and_new_epoch_can_claim(reset_case):
    from server.negotiation.prices import InvalidFact
    import json
    c=setup_wire(reset_case);service=c.app.state.inquiry;commands=service.commands_core;store=c.app.state.repository
    old_draft=draft(c,'unconfirmed-preview-before-reset',code='SYN-PREVIEW')
    result=confirmed(c,peers=['synthetic-peer','synthetic-peer-b','synthetic-peer-c'])
    rows=[commands.get(B.owner_user_id,item['command_id']) for item in result['commands']]
    uncertain=rows[0];pending=rows[1];queued=rows[2]
    for row in [uncertain,pending]:assert commands.claim(B.owner_user_id,row['command_id'])
    commands.receipt(B.owner_user_id,uncertain['command_id'],account_uid=B.account_uid,
        peer_uid=next(x['peer_id'] for x in result['commands'] if x['command_id']==uncertain['command_id']),
        content_hash=uncertain['content_hash'],status='UNKNOWN',evidence='isolated old unknown')
    with store.db.transaction(write=False) as tx:
        before_messages=tx.execute('SELECT COUNT(*) AS n FROM xj2_message').fetchone()['n']
    old_group=uncertain['group_id']
    first=start(c)
    assert first['state']=='requested' and first['account_epoch']==1
    assert commands.get(B.owner_user_id,queued['command_id'])['state']=='CANCELLED'
    assert commands.get(B.owner_user_id,pending['command_id'])['state']=='PENDING'
    assert commands.get(B.owner_user_id,uncertain['command_id'])['state']=='UNKNOWN'
    with pytest.raises(InvalidFact):
        service.confirm(B.owner_user_id,old_draft['batch_id'],old_draft['version'],'confirm-during-reset')
    with pytest.raises(InvalidFact):service.manual_send(B.owner_user_id,'synthetic-peer','isolated denied during reset','manual-during-reset')
    for _ in range(8):
        first_apply=pump(c)
        if first_apply['state']=='local_applied':break
        c.now[0]+=1
    assert first_apply['state']=='local_applied',first_apply
    assert c.receiver.reset_status()['state']=='barrier_ready'
    completed=finish(c)
    assert completed['state']=='complete' and completed['reset_id']==first['reset_id']
    from relay.protocol import digest
    assert completed['snapshot_hash']==digest(c.receiver.reset_status()['snapshot'])
    assert store.group(B.owner_user_id,old_group)['archived']==1
    assert commands.get(B.owner_user_id,uncertain['command_id'])['state']=='UNKNOWN'
    assert commands.get(B.owner_user_id,pending['command_id'])['state']=='PENDING'
    with pytest.raises(InvalidFact):service.confirm(B.owner_user_id,old_draft['batch_id'],old_draft['version'],'confirm-after-reset')
    with store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_message').fetchone()['n']==before_messages
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_session_reset').fetchone()['n']==1
    # Current epoch authorization no longer inherits old-epoch UNKNOWN mutex.
    new=confirmed(c,key='new-after-reset',peers=[next(x['peer_id'] for x in result['commands'] if x['command_id']==uncertain['command_id'])])
    new_row=commands.get(B.owner_user_id,new['commands'][0]['command_id'])
    assert new_row['account_epoch']==1
    claim=commands.claim(B.owner_user_id,new_row['command_id'])
    assert claim and claim['state']=='PENDING'
    wire=c.app.state.dispatcher._wire(B.owner_user_id,claim)
    assert wire['account_epoch']==1
    assert start(c)==completed, 'Same request remains idempotent after completion'


def test_idempotent_pending_requests_and_cross_owner_status_are_protected(reset_case):
    from relay.protocol import Binding
    c=setup_wire(reset_case);first=start(c)
    assert start(c)==first
    assert start(c,{**c.body,'request_id':'another-concurrent-tab'})==first
    token=c.app.state.auth.issue_token(AuthUser('synthetic-other-owner','other isolated trader'))
    c.app.state.repository.bind_account('synthetic-other-owner','synthetic-other-account','synthetic-other-wb')
    response=c.client.get('/api/im/session-reset/'+first['reset_id'],headers={'Authorization':'Bearer '+token})
    assert response.status_code==403,response.text
    assert operation(c)==first


def test_coalesced_request_id_retry_after_complete_does_not_start_second_reset(reset_case):
    c=setup_wire(reset_case);first=start(c)
    concurrent={**c.body,'request_id':'second-tab-response-was-lost'}
    assert start(c,concurrent)['reset_id']==first['reset_id']
    finish(c)
    retry=start(c,concurrent)
    assert retry['reset_id']==first['reset_id'] and retry['account_epoch']==1


def test_pending_account_reset_prevents_route_version_change_that_would_orphan_handshake(reset_case):
    from server.negotiation.prices import InvalidFact
    c=setup_wire(reset_case);first=start(c)
    store=c.app.state.repository;before=store.binding_for(B.owner_user_id)
    # The actual /links/{id}/activate handler delegates to this public store
    # operation after checking the configured route. No transport send occurs.
    with pytest.raises(InvalidFact):
        store.switch_transport(B.owner_user_id,'mail',before['version'])
    assert store.binding_for(B.owner_user_id)==before
    assert operation(c)['reset_id']==first['reset_id']


def native_event(case,identity,direction='in'):
    return case.bridge.publish_native({'event_id':identity,'peer_id':'synthetic-peer',
        'direction':direction,'text':'isolated '+identity,'ext_msg_id':'native-'+identity,
        'occurred_at':case.now[0],**({'role':'human'} if direction=='out' else {})})


def wire_event(case,identity):
    from relay.protocol import Codec
    values=[Codec(B,clock=lambda:case.now[0]).unpack(body) for _,body in case.gateway.queues[UP_PATH]]
    return next(x for x in values if x['kind']=='event' and x['payload']['event_id']==identity)


def test_frozen_source_cut_keeps_newer_messages_fence_and_rejects_late_old_work(reset_case):
    import json
    from server.negotiation.prices import StaleDecision
    from server.negotiation.store import encoded
    c=setup_wire(reset_case);store=c.app.state.repository;service=c.app.state.inquiry
    old=confirmed(c);oldrow=service.commands_core.get(B.owner_user_id,old['commands'][0]['command_id'])
    tid,mid=oldrow['thread_id'],oldrow['member_id']
    assert native_event(c,'before-in')==1
    assert native_event(c,'before-human','out')==2
    delayed=wire_event(c,'before-human')
    # The vendor seam delays this original signed publication; no envelope is
    # fabricated or re-signed. It will arrive after the authenticated cut.
    for index,(_,blob) in enumerate(c.gateway.queues[UP_PATH]):
        from relay.protocol import Codec
        if Codec(B,clock=lambda:c.now[0]).unpack(blob)['payload'].get('event_id')=='before-human':
            held_packet=c.gateway.queues[UP_PATH].pop(index);break
    else:pytest.fail('Expected actual delayed signed packet')
    c.app.state.inbox.poll_once()
    old_analysis=store.claim_analysis(B.owner_user_id,tid,force=True)
    assert old_analysis and [x['event_id'] for x in old_analysis['snapshot']['messages']]==['before-in']
    basis=service.commands_core.basis(B.owner_user_id,mid)
    with store.write(B.owner_user_id) as tx:
        tx.execute("INSERT INTO xj2_reply_job VALUES(?,?,?,?,'RUNNING',?,?,1,'{}',?)",
            ('isolated-old-job',mid,'isolated-old-reply',encoded(basis),'isolated-old-token',c.now[0]+60,c.now[0]))
    start(c)
    c.app.state.session_reset.run_once();c.bridge.run_once()
    pending=c.adapter.poll()
    ack=next(row for row in pending if row['kind']=='session_reset_ack')
    snapshot=ack['envelope']['payload']['snapshot']
    assert snapshot['published_source_highwater']==2
    assert snapshot['peers']['synthetic-peer']['receiver_fence_version']==1
    c.now[0]+=1
    assert native_event(c,'after-in')==3
    c.app.state.inbox.apply(B,wire_event(c,'after-in'))
    c.now[0]+=1
    assert native_event(c,'after-human','out')==4
    c.app.state.inbox.apply(B,wire_event(c,'after-human'))
    with store.db.transaction(write=False) as tx:
        before=dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?',(tid,)).fetchone())
    assert before['ingest_revision']==3 and before['analyzed_revision']==0
    assert before['source_watermark']==3 and before['receiver_fence_version']==2
    c.app.state.inbox.apply(B,ack['envelope']);c.adapter.ack(ack['source_token'])
    with store.db.transaction(write=False) as tx:
        after=dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?',(tid,)).fetchone())
        reset=dict(tx.execute('SELECT * FROM xj2_session_reset').fetchone())
    assert (after['ingest_revision'],after['analyzed_revision'])==(3,0)
    assert (after['source_watermark'],after['receiver_fence_version'],after['receiver_fence_id'])==(before['source_watermark'],before['receiver_fence_version'],before['receiver_fence_id'])
    assert after['pending_since'] is not None
    cut=json.loads(reset['local_cut_json'])
    assert cut['source_highwater']==2 and cut['local_revisions'][tid]==1
    c.now[0]+=6
    assert finish(c)['state']=='complete'
    with pytest.raises(StaleDecision):
        store.commit_analysis(B.owner_user_id,old_analysis['analysis_id'],old_analysis['lease_token'],scopes={},observations=[])
    with pytest.raises(StaleDecision):
        service.commands_core.propose(B.owner_user_id,mid,basis=basis,request_id='isolated-old-reply',
            proposal={'kind':'CLARIFY','text':'isolated stale reply'},validator=lambda v:v,
            expires_at=c.now[0]+120,reply_job_id='isolated-old-job',reply_lease_token='isolated-old-token')
    new=confirmed(c,key='fresh-round-after-reset')
    newrow=service.commands_core.get(B.owner_user_id,new['commands'][0]['command_id'])
    newmid=newrow['member_id']
    analysis=store.claim_analysis(B.owner_user_id,tid,force=True)
    assert {x['event_id'] for x in analysis['snapshot']['messages']}=={'after-in','after-human'}
    store.commit_analysis(B.owner_user_id,analysis['analysis_id'],analysis['lease_token'],scopes={message['message_id']:[] for message in analysis['snapshot']['messages']},observations=[])
    with store.db.transaction(write=False) as tx:
        control=dict(tx.execute('SELECT send_allowed,control_version,pending_command_id FROM xj2_peer WHERE member_id=?',(newmid,)).fetchone())
    assert control['send_allowed']==1
    # Delayed original human echo belongs to the old source cut. Keep its raw
    # fact without applying its old manual fence/pause to the new round.
    c.app.state.inbox.apply(B,delayed)
    with store.db.transaction(write=False) as tx:
        current=dict(tx.execute('SELECT send_allowed,control_version,pending_command_id FROM xj2_peer WHERE member_id=?',(newmid,)).fetchone())
        raw=tx.execute('SELECT text,source_seq FROM xj2_message WHERE event_id=?',('before-human',)).fetchone()
    assert current==control and raw['text']==delayed['payload']['text'] and raw['source_seq']==2
    assert service.commands_core.get(B.owner_user_id,newrow['command_id'])['state']=='QUEUED'
    assert store.claim_analysis(B.owner_user_id,tid,force=True) is None
    view=store.raw_page(B.owner_user_id,tid)
    by_event={x['event_id']:x for x in view['messages']}
    assert by_event['before-in']['analysis_state']==by_event['before-human']['analysis_state']=='RESET_HISTORY'
    assert by_event['after-in']['analysis_state']=='ANALYZED'


@pytest.mark.parametrize('unavailable', ['old-catalog','offline'])
def test_first_reset_requires_real_signed_capability_without_pausing_or_persisting(reset_case,unavailable):
    c=setup_wire(reset_case);service=c.app.state.inquiry;store=c.app.state.repository
    result=confirmed(c);cid=result['commands'][0]['command_id'];row=service.commands_core.get(B.owner_user_id,cid)
    before=store.group(B.owner_user_id,row['group_id'])
    if unavailable=='old-catalog':
        c.source['publish'](B.workbench_id,B.account_uid,'catalog',extra={'capabilities':['negotiation-v22-watermark']});c.source['refresh']()
    else:c.source['advance'](61)
    response=c.client.post('/api/im/session-reset',json=c.body)
    assert response.status_code in (400,403,409,503),response.text
    assert store.group(B.owner_user_id,row['group_id'])==before
    assert service.commands_core.get(B.owner_user_id,cid)==row
    with store.db.transaction(write=False) as tx:assert tx.execute('SELECT COUNT(*) AS n FROM xj2_session_reset').fetchone()['n']==0
    assert c.gateway.queues[DOWN_PATH]==[]


def test_existing_operation_retries_and_complete_reads_do_not_depend_on_catalog_network(reset_case,monkeypatch):
    c=setup_wire(reset_case);first=start(c)
    def forbidden():pytest.fail('Existing durable reset retry/read must not revisit capability network')
    monkeypatch.setattr(c.adapter,'catalog',forbidden)
    assert start(c)['reset_id']==first['reset_id']
    assert operation(c)['reset_id']==first['reset_id']
    assert finish(c)['state']=='complete'
    assert start(c)['reset_id']==first['reset_id']
    assert operation(c)['state']=='complete'


def historical_other_member(case,group_id,account_uid,peer_uid):
    """Construct retained rows from an explicitly different earlier binding.
    Temporary DB fixture only: never weaken new-member scope checks for tests.
    """
    store=case.app.state.repository
    with store.write(B.owner_user_id) as tx:
        before=dict(tx.execute('SELECT account_uid,workbench_id FROM xj2_binding WHERE owner_id=?',(B.owner_user_id,)).fetchone())
        tx.execute('UPDATE xj2_binding SET account_uid=?,workbench_id=? WHERE owner_id=?',
            (account_uid,'synthetic-earlier-other-wb',B.owner_user_id))
    try:
        return store.add_member(B.owner_user_id,group_id,account_uid=account_uid,peer_uid=peer_uid)
    finally:
        with store.write(B.owner_user_id) as tx:
            tx.execute('UPDATE xj2_binding SET account_uid=?,workbench_id=? WHERE owner_id=?',
                (before['account_uid'],before['workbench_id'],B.owner_user_id))


def test_non_target_account_groups_and_messages_are_unchanged(reset_case):
    import json
    from server.negotiation.prices import LimitPolicy
    c=setup_wire(reset_case);store=c.app.state.repository
    confirmed(c)
    other=store.create_group(B.owner_user_id,bond_code='SYN-OTHER-ACCOUNT',our_side='SELL',
        target_volume='500',trade_day=c.app.state.inquiry.trade_day(),policy=LimitPolicy('ABSOLUTE','3.30'),send_allowed=True)
    member,tid=historical_other_member(c,other,'synthetic-other-account','other-peer')
    store.ingest(B.owner_user_id,tid,event_id='other-original-event',source_seq=50,text='isolated other account fact',
        trade_day=c.app.state.inquiry.trade_day(),occurred_at=c.now[0])
    def rows():
        with store.db.transaction(write=False) as tx:
            return {t:[dict(x) for x in tx.execute(sql,args).fetchall()] for t,sql,args in [
                ('group','SELECT * FROM xj2_group WHERE group_id=?',(other,)),
                ('peer','SELECT * FROM xj2_peer WHERE member_id=?',(member,)),
                ('thread','SELECT * FROM xj2_thread WHERE thread_id=?',(tid,)),
                ('messages','SELECT * FROM xj2_message WHERE thread_id=?',(tid,))]}
    before=rows();start(c);finish(c)
    assert rows()==before


def test_mixed_account_group_refuses_whole_reset_before_any_pause_or_cancel(reset_case):
    from server.negotiation.prices import LimitPolicy
    c=setup_wire(reset_case);store=c.app.state.repository
    result=confirmed(c);row=c.app.state.inquiry.commands_core.get(B.owner_user_id,result['commands'][0]['command_id'])
    legacy=store.create_group(B.owner_user_id,bond_code='SYN-LEGACY-MIXED',our_side='SELL',
        target_volume='500',trade_day=c.app.state.inquiry.trade_day(),policy=LimitPolicy('ABSOLUTE','3.30'))
    foreign,_=historical_other_member(c,legacy,'synthetic-foreign-account','foreign-peer')
    # Reproduce a retained pre-upgrade mixed group. New public writes correctly
    # reject this shape; reset must still fail atomically when old data has it.
    with store.db.transaction() as tx:tx.execute('UPDATE xj2_peer SET group_id=? WHERE member_id=?',(row['group_id'],foreign))
    def rows():
        with store.db.transaction(write=False) as tx:
            return {t:[dict(x) for x in tx.execute('SELECT * FROM '+t).fetchall()] for t in ['xj2_group','xj2_peer','xj2_command','xj2_session_reset']}
    before=rows();response=c.client.post('/api/im/session-reset',json=c.body)
    assert response.status_code in (400,409),response.text
    assert rows()==before


@pytest.mark.parametrize('old_state',['PENDING','UNKNOWN'])
def test_old_uncertain_cancel_continues_until_a_real_reset_barrier_is_applied(reset_case,old_state):
    from server.negotiation.runtime import Dispatcher
    from tests.cutover.test_dispatch_expiry_offline import RecordedTransport
    c=reset_case;service=c.app.state.inquiry
    result=confirmed(c);cid=result['commands'][0]['command_id']
    row=service.commands_core.get(B.owner_user_id,cid)
    claim=service.commands_core.claim(B.owner_user_id,cid);assert claim
    c.app.state.dispatcher._wire(B.owner_user_id,claim)
    if old_state=='UNKNOWN':
        service.commands_core.receipt(B.owner_user_id,cid,account_uid=B.account_uid,
            peer_uid=claim['peer_uid'],content_hash=row['content_hash'],status='UNKNOWN',evidence='isolated uncertain old execution')
    start(c)
    recorder=RecordedTransport();dispatcher=Dispatcher(service,recorder)
    assert operation(c)['state']=='requested'
    dispatcher.run_once()
    assert [(op,wire['command_id']) for op,wire in recorder.calls]==[('cancel',cid)]
    assert service.commands_core.get(B.owner_user_id,cid)['state']==old_state
    for _ in range(8):
        current=pump(c)
        if current['state']=='local_applied':break
        c.now[0]+=1
    assert current['state']=='local_applied',current
    c.now[0]+=6
    dispatcher.run_once()
    assert [(op,wire['command_id']) for op,wire in recorder.calls]==[('cancel',cid)]
    assert service.commands_core.get(B.owner_user_id,cid)['state']==old_state


def test_new_schema_pair_uniqueness_and_explicit_legacy_nullable_upgrade_preserve_facts(tmp_path):
    import sqlite3
    from pathlib import Path
    from server.negotiation.schema import statements
    fresh=sqlite3.connect(':memory:')
    fresh.executescript(';'.join(statements()))
    row=('old-thread','owner','account','peer',7,3,100.,101.,50,9,'original-fence',4)
    legacy=sqlite3.connect(':memory:')
    legacy.execute('''CREATE TABLE xj2_thread(thread_id VARCHAR(64) PRIMARY KEY,
        owner_id VARCHAR(160) NOT NULL,account_uid VARCHAR(80) NOT NULL,peer_uid VARCHAR(160) NOT NULL,
        ingest_revision BIGINT NOT NULL,analyzed_revision BIGINT NOT NULL,pending_since DOUBLE,last_ingest_at DOUBLE,
        source_watermark BIGINT NOT NULL DEFAULT 0,dispatch_version BIGINT NOT NULL DEFAULT 0,
        receiver_fence_id VARCHAR(160),receiver_fence_version BIGINT NOT NULL DEFAULT 0,
        UNIQUE(owner_id,account_uid,peer_uid))''')
    legacy.execute('INSERT INTO xj2_thread VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',row)
    sql=(Path(__file__).resolve().parents[2]/'sql/oceanbase/003_thread_workbench.sql').read_text()
    # This exact additive DDL is supported by SQLite too; OceanBase UNIQUE-index
    # replacement remains an explicit operator step requiring SHOW INDEX proof.
    alter=next(line for line in sql.splitlines() if line.startswith('ALTER TABLE xj2_thread ADD COLUMN'))
    legacy.execute(alter)
    columns=[r[1] for r in legacy.execute('PRAGMA table_info(xj2_thread)')]
    assert columns[-1]=='workbench_id'
    assert legacy.execute('SELECT * FROM xj2_thread').fetchone()==(*row,None)
    fresh.execute('INSERT INTO xj2_thread(thread_id,owner_id,workbench_id,account_uid,peer_uid,ingest_revision,analyzed_revision) VALUES(?,?,?,?,?,0,0)',('a','owner','wb-a','account','peer'))
    fresh.execute('INSERT INTO xj2_thread(thread_id,owner_id,workbench_id,account_uid,peer_uid,ingest_revision,analyzed_revision) VALUES(?,?,?,?,?,0,0)',('b','owner','wb-b','account','peer'))
    with pytest.raises(sqlite3.IntegrityError):
        fresh.execute('INSERT INTO xj2_thread(thread_id,owner_id,workbench_id,account_uid,peer_uid,ingest_revision,analyzed_revision) VALUES(?,?,?,?,?,0,0)',('duplicate','owner','wb-b','account','peer'))
    assert fresh.execute('SELECT COUNT(*) FROM xj2_thread').fetchone()[0]==2
    legacy.close();fresh.close()


def test_unattributed_legacy_thread_cannot_claim_or_build_production_wire(reset_case):
    from server.negotiation.prices import InvalidFact
    c=reset_case;service=c.app.state.inquiry
    result=confirmed(c);cid=result['commands'][0]['command_id']
    row=service.commands_core.get(B.owner_user_id,cid)
    with c.app.state.repository.db.transaction() as tx:
        tx.execute('UPDATE xj2_thread SET workbench_id=NULL WHERE thread_id=?',(row['thread_id'],))
    assert service.commands_core.claim(B.owner_user_id,cid) is None
    with pytest.raises(InvalidFact):c.app.state.dispatcher._wire(B.owner_user_id,row)
    rejected=service.commands_core.get(B.owner_user_id,cid)
    import json
    assert rejected['state']=='CANCELLED'
    assert json.loads(rejected['result_json'])=={'reason':'COLLECTOR_ACCOUNT_CHANGED'}
    with c.app.state.repository.db.transaction(write=False) as tx:
        assert tx.execute('SELECT workbench_id FROM xj2_thread WHERE thread_id=?',(row['thread_id'],)).fetchone()['workbench_id'] is None


from tests.cutover.test_im_catalog_selection import selection, choose, OWNER as DIRECTORY_OWNER


def test_same_uid_other_workbench_reset_isolated_from_original_thread_and_native_guard(selection):
    import time
    from relay.collector_bridge import CollectorBridge
    from server.receiver_ledger import ReceiverLedger
    from relay.protocol import Binding
    s=selection;app=s['app'];store=app.state.repository;service=app.state.inquiry;owner=DIRECTORY_OWNER
    binding_a=app.state.transport.adapters[(owner,'account-a','oss')].binding
    native_a=collector(s,'wb-a','account-a',filename='cross-native-a.sqlite3')
    rx_a=ReceiverLedger(s['tmp']/'cross-rx-a.sqlite3',native_a.binding)
    bridge_a=CollectorBridge(ledger=rx_a,adapters={'oss':native_a},native_send=lambda _:pytest.fail('No native send in namespace gate'),
        native_reconcile=lambda _:None,native_quiescent=lambda:True)
    item={'code':'SYN-WORKBENCH-A','peer_ids':['same-peer'],'our_side':'BUY','volume_wan':'1000',
          'opening_yield_pct':'3.20','limit_yield_pct':'3.12','step_bp':'2'}
    preview_a=service.preview(owner,'wb-a-preview',[item]);round_a=service.confirm(owner,preview_a['batch_id'],1,'wb-a-confirm')
    command_a=service.commands_core.get(owner,round_a['commands'][0]['command_id']);thread_a=command_a['thread_id']
    bridge_a.publish_native({'event_id':'native-a-original','peer_id':'same-peer','direction':'in',
        'text':'isolated original workbench A fact','ext_msg_id':'native-a-id','occurred_at':time.time()})
    for _ in range(3):app.state.inbox.poll_once()
    store.set_sending(owner,False)
    store.set_sending(owner,False,group_id=command_a['group_id'])
    with store.db.transaction(write=False) as tx:
        old_members=tx.execute('SELECT member_id FROM xj2_peer WHERE group_id=?',(command_a['group_id'],)).fetchall()
    for member in old_members:store.set_sending(owner,False,member_id=member['member_id'])
    with store.db.transaction(write=False) as tx:
        original_thread=dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?',(thread_a,)).fetchone())
        original_group=dict(tx.execute('SELECT * FROM xj2_group WHERE group_id=?',(command_a['group_id'],)).fetchone())
        original_messages=[dict(x) for x in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(thread_a,)).fetchall()]
    assert original_thread['source_watermark']==1
    s['publish']('wb-a','account-a','catalog',online=False)
    s['seed']('wb-b','account-a');s['refresh']()
    selected=choose(s,'wb-b','account-a');assert selected.status_code==200,selected.text
    # Selection retires old execution; its financial facts remain in their original namespace.
    with store.db.transaction(write=False) as tx:
        for table in ('xj2_command','xj2_group','xj2_thread','xj2_message','xj2_peer'):
            assert tx.execute('SELECT COUNT(*) AS n FROM '+table).fetchone()['n']>0
        assert tx.execute('SELECT archived FROM xj2_group WHERE group_id=?',(command_a['group_id'],)).fetchone()['archived']==1
        assert [dict(x) for x in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(thread_a,)).fetchall()]==original_messages
    assert s['api'].get('/api/monitor/counterparties').json()['data']==[]
    binding_b=app.state.transport.adapters[(owner,'account-a','oss')].binding
    from relay.protocol import Codec,envelope
    from server.negotiation.prices import InvalidFact
    import json
    for kind in ('receipt','event'):
        payload={'command_id':command_a['command_id'],'content_hash':command_a['content_hash'],
            'peer_id':'same-peer','account_uid':binding_b.account_uid,'status':'sent',
            'native_id':'wrong-workbench-native-id','executed_at':time.time(),'evidence':'isolated wrong-workbench native result'}
        if kind=='event':
            payload={'event_id':'wrong-workbench-echo','peer_id':'same-peer','direction':'out',
                'source_seq':1,'text':json.loads(command_a['body_json'])['text'],
                'source_command_id':command_a['command_id'],'ext_msg_id':'wrong-workbench-echo-id','occurred_at':time.time()}
        codec=Codec(binding_b)
        signed=codec.unpack(codec.pack(envelope(binding_b,kind,payload,message_id=payload.get('event_id','cross-wb-'+kind),
            peer_id='same-peer',command_id=command_a['command_id'],control_version=1,
            content_hash=command_a['content_hash'] if kind=='receipt' else '',
            created_at=time.time(),expires_at=time.time()+120)))
        with pytest.raises((InvalidFact,PermissionError)):app.state.inbox.apply(binding_b,signed)
        with store.db.transaction(write=False) as tx:
            assert [dict(x) for x in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(thread_a,)).fetchall()]==original_messages
            assert tx.execute('SELECT state FROM xj2_command WHERE command_id=?',(command_a['command_id'],)).fetchone()['state']=='CANCELLED'
        assert s['api'].get('/api/monitor/counterparties').json()['data']==[]
    native_b=collector(s,'wb-b','account-a',filename='cross-native-b.sqlite3')
    rx_b=ReceiverLedger(s['tmp']/'cross-rx-b.sqlite3',native_b.binding)
    bridge_b=CollectorBridge(ledger=rx_b,adapters={'oss':native_b},native_send=lambda _:pytest.fail('No native send in reset'),
        native_reconcile=lambda _:None,native_quiescent=lambda:True)
    bridge_b.publish_capabilities()
    version=store.binding_for(owner)['version']
    started=s['api'].post('/api/im/session-reset',json={'request_id':'wb-b-real-reset','expected_binding_version':version,'confirmed':True})
    assert started.status_code==200,started.text
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        app.state.session_reset.run_once();bridge_b.run_once();app.state.inbox.poll_once()
        status=s['api'].get('/api/im/session-reset').json()['data']
        if status['state']=='complete':break
        s['advance'](6)
        time.sleep(.2)
    assert status['state']=='complete' and status['snapshot']['published_source_highwater']==0
    with store.db.transaction(write=False) as tx:
        retained_thread=tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?',(thread_a,)).fetchone()
        assert retained_thread['workbench_id']==original_thread['workbench_id']=='wb-a'
        assert retained_thread['source_watermark']==original_thread['source_watermark']
        assert tx.execute('SELECT archived FROM xj2_group WHERE group_id=?',(original_group['group_id'],)).fetchone()['archived']==1
        assert [dict(x) for x in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(thread_a,)).fetchall()]==original_messages
    preview_b=service.preview(owner,'wb-b-preview',[{**item,'code':'SYN-WORKBENCH-B'}])
    round_b=service.confirm(owner,preview_b['batch_id'],1,'wb-b-confirm')
    row_b=service.commands_core.get(owner,round_b['commands'][0]['command_id'])
    assert row_b['thread_id']!=thread_a, 'Same UID/peer on a different workbench cannot reuse source/analysis state'
    claim=service.commands_core.claim(owner,row_b['command_id']);assert claim
    wire=app.state.dispatcher._wire(owner,claim)
    assert wire['workbench_id']=='wb-b' and wire['source_watermark']==0 and wire['account_epoch']==1
    bridge_b.publish_native({'event_id':'native-b-not-yet-observed','peer_id':'same-peer','direction':'in',
        'text':'isolated newer B fact','ext_msg_id':'native-b-id','occurred_at':time.time()})
    packet=envelope(binding_b,'command',wire,message_id=wire['command_id'],peer_id=wire['peer_id'],command_id=wire['command_id'],control_version=wire['control_version'],content_hash=wire['content_hash'],created_at=s['clock'][0],expires_at=wire['expires_at'])
    decoded=Codec(rx_b.binding,clock=lambda:s['clock'][0]).unpack(app.state.transport.adapters[(owner,'account-a','oss')].codec.pack(packet))['payload']
    answer=rx_b.execute(decoded,lambda _:pytest.fail('A watermark must never make an unseen B message appear observed'))
    assert answer['status']=='cancelled' and 'newer-native-message' in answer['evidence']
