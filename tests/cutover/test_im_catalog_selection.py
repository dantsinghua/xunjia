"""Independent real-handler catalog selection gates; no production network or accounts."""
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay.protocol import Binding
from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.negotiation.application_store import ApplicationStore
from server.negotiation.prices import LimitPolicy
from tests.institution_test_support import selection, OWNER, directory_request, collector
from relay.gateway_paths import CONTRACT, account_paths



def choose(s, wid='wb-b', uid='account-c', version=None):
    if version is None:
        version = s['repository'].binding_for(OWNER)['version']
    return s['api'].post('/api/im/binding/select', json={
        'workbench_id': wid, 'account_uid': uid, 'expected_version': version})


def snapshot(s):
    return (s['repository'].binding_for(OWNER), s['profiles'].read_bytes(),
            dict(s['app'].state.transport.adapters))


def assert_rejected_without_change(s, response, before):
    assert response.status_code in (400, 403, 409, 422), response.text
    assert snapshot(s) == before


def seed_business(s):
    store = s['repository']
    group = store.create_group(OWNER, bond_code='QA001.IB', our_side='BUY', target_volume='1000',
        trade_day='2026-10-20', policy=LimitPolicy(mode='ABSOLUTE', value='2'), send_allowed=False)
    member, _ = store.add_member(OWNER, group, account_uid='account-a', peer_uid='old-peer')
    store.set_sending(OWNER, False, member_id=member)
    with store.db.transaction(write=False) as tx:
        peer = dict(tx.execute('SELECT * FROM xj2_peer WHERE member_id=?', (member,)).fetchone())
    return group, member, peer['thread_id']


def test_authenticated_directory_aggregates_real_signed_pairs_and_exact_selected_binding(selection):
    s=selection;calls=list(s['wire'].calls)
    data=s['api'].get('/api/im/catalog').json()['data']
    assert data['selected']=={'workbench_id':'wb-a','account_uid':'account-a','version':1}
    assert {(w['workbench_id'],a['account_uid']) for w in data['workbenches'] for a in w['accounts']}=={
        ('wb-a','account-a'),('wb-a','account-b'),('wb-b','account-c'),('wb-b','account-d')}
    assert s['wire'].calls==calls


def test_select_changes_path_adapter_contacts_and_persists(selection):
    s=selection;response=choose(s);assert response.status_code==200,response.text
    result=response.json()['data'];assert result['selected']=={'workbench_id':'wb-b','account_uid':'account-c','version':2}
    adapter=s['app'].state.transport.adapters[(OWNER,'account-c','oss')]
    assert adapter.binding.workbench_id=='wb-b'
    paths=account_paths('wb-b','account-c')
    assert adapter.inbound.base_url==adapter.outbound.base_url==s['base']
    assert adapter.inbound.path==paths['uplink_path'] and adapter.outbound.path==paths['downlink_path']
    assert result['contacts']['contacts'][0]['uin']=='peer-wb-b-account-c'
    app=s['make_app']()
    with TestClient(app) as api:
        api.headers['Authorization']='Bearer '+s['bearer']
        assert api.get('/api/im/catalog').json()['data']['selected']==result['selected']
        assert api.get('/api/im/contacts').json()['data']['contacts'][0]['uin']=='peer-wb-b-account-c'
    assert s['profiles'].read_bytes()==snapshot(s)[1]


def test_same_uid_simultaneous_workbenches_conflict_then_explicit_offline_handoff(selection):
    s=selection;s['publish']('wb-b','account-a','catalog');s['refresh']()
    before=snapshot(s);assert_rejected_without_change(s,choose(s,'wb-b','account-a'),before)
    assert not s['runtime'].online('wb-a','account-a') and not s['runtime'].online('wb-b','account-a')
    s['publish']('wb-a','account-a','catalog',online=False)
    s['publish']('wb-b','account-a','contacts');s['refresh']()
    response=choose(s,'wb-b','account-a');assert response.status_code==200,response.text
    assert response.json()['data']['contacts']['contacts'][0]['uin']=='peer-wb-b-account-a'


@pytest.mark.parametrize('wid,uid', [('wb-missing', 'account-a'), ('wb-a', 'not-a-member')])
def test_missing_workbench_or_account_cannot_be_filled_in_by_client(selection, wid, uid):
    s = selection
    before = snapshot(s)
    assert_rejected_without_change(s, choose(s, wid, uid), before)


def test_stale_binding_version_rejects_without_second_write(selection):
    s = selection
    assert choose(s).status_code == 200
    before = snapshot(s)
    assert_rejected_without_change(s, choose(s, 'wb-a', 'account-b', version=1), before)


@pytest.mark.parametrize('field,value', [('expected_version', True), ('expected_version', -1),
    ('workbench_id', '../outside'), ('account_uid', ''), ('unexpected_identity', 'spoofed')])
def test_exact_selection_schema_rejects_malformed_identity_or_version(selection, field, value):
    s = selection
    body = {'workbench_id': 'wb-b', 'account_uid': 'account-c', 'expected_version': 1, field: value}
    before = snapshot(s)
    assert_rejected_without_change(s, s['api'].post('/api/im/binding/select', json=body), before)


@pytest.mark.parametrize('guard', ['owner', 'group', 'member'])
def test_remaining_send_flags_are_cleared_on_explicit_switch(selection, guard):
    s=selection;group,member,_=seed_business(s)
    table,column,value={'owner':('xj2_owner','owner_id',OWNER),'group':('xj2_group','group_id',group),'member':('xj2_peer','member_id',member)}[guard]
    with s['repository'].write(OWNER) as tx:tx.execute('UPDATE '+table+' SET send_allowed=1 WHERE '+column+'=?',(value,))
    response=choose(s);assert response.status_code==200,response.text
    assert response.json()['data']['reset']
    with s['repository'].db.transaction(write=False) as tx:
        assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?',(OWNER,)).fetchone()['send_allowed']==0
        retained=tx.execute('SELECT archived,send_allowed FROM xj2_group WHERE group_id=?',(group,)).fetchone()
        assert retained['archived']==1 and retained['send_allowed']==0
        assert tx.execute('SELECT send_allowed FROM xj2_peer WHERE member_id=?',(member,)).fetchone()['send_allowed']==0
    assert s['api'].get('/api/monitor/counterparties').json()['data']==[]



@pytest.mark.parametrize('state', ['QUEUED', 'PENDING', 'UNKNOWN'])
def test_old_unsettled_commands_are_retained_on_switch_without_replay(selection, state):
    s=selection;group,member,thread=seed_business(s)
    with s['repository'].write(OWNER) as tx:
        tx.execute('INSERT INTO xj2_command(command_id,request_id,member_id,group_id,thread_id,state,content_hash,body_json,basis_json,result_json,cancel_requested,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('old-command','old-request',member,group,thread,state,'a'*64,'{}','{}','{}',0,s['now'],s['now']+50))
    response=choose(s);assert response.status_code==200,response.text
    assert response.json()['data']['retained_counts']['xj2_command']==1
    with s['repository'].db.transaction(write=False) as tx:
        retained=tx.execute('SELECT * FROM xj2_command WHERE command_id=?',('old-command',)).fetchone()
        assert retained['state']==('CANCELLED' if state=='QUEUED' else state)
        assert retained['content_hash']=='a'*64 and retained['body_json']=='{}' and retained['basis_json']=='{}'
        if state in ('PENDING','UNKNOWN'):
            assert retained['result_json']=='{}' and retained['cancel_requested']==1
    assert s['api'].get('/api/im/commands/unknown').json()['data']==[]
    assert s['app'].state.inquiry.commands_core.claim(OWNER,'old-command') is None



def test_noop_does_not_bump_binding_version_or_replace_adapter(selection):
    s = selection
    before = snapshot(s)
    response = choose(s, 'wb-a', 'account-a')
    assert response.status_code == 200, response.text
    after = snapshot(s)
    assert {k: v for k, v in after[0].items() if k != 'config_json'} == {
        k: v for k, v in before[0].items() if k != 'config_json'}
    assert after[1:] == before[1:]
    check = s['repository'].preferences(OWNER)['collector_directory_checks']['oss']
    assert (check['workbench_id'], check['account_uid'], check['version']) == ('wb-a', 'account-a', 1)


@pytest.mark.parametrize('reason',['missing-contacts','missing-v22'])
def test_selection_requires_signed_online_capabilities_and_never_invents_missing_friends(selection,reason):
    s=selection;uid='new-account';wid='wb-unavailable'
    s['publish'](wid,uid,'catalog',capabilities=reason!='missing-v22')
    if reason!='missing-contacts':s['publish'](wid,uid,'contacts')
    s['refresh']();before=snapshot(s);response=choose(s,wid,uid)
    if reason=='missing-v22':assert_rejected_without_change(s,response,before)
    else:
        assert response.status_code==200,response.text
        book=response.json()['data']['contacts'];assert book['contacts']==[] and book['friends_updated_at'] is None


@pytest.mark.parametrize('expired_kind',['catalog','contacts'])
def test_offline_presence_rejects_selection_and_old_friends_keep_original_observation(selection,expired_kind):
    s=selection;wid='wb-offline';uid='offline-account'
    for kind in ['catalog','contacts']:s['publish'](wid,uid,kind,expired=kind==expired_kind)
    s['refresh']();before=snapshot(s);calls=list(s['wire'].calls)
    response=choose(s,wid,uid)
    if expired_kind=='catalog':assert_rejected_without_change(s,response,before)
    else:
        assert response.status_code==200,response.text
        book=response.json()['data']['contacts']
        assert book['friends_updated_at']==s['now']-61 and book['updated_at']==s['now']
        assert book['contacts'][0]['uin']=='peer-wb-offline-offline-account'
    if expired_kind=='catalog':assert s['wire'].calls==calls
    else:assert all(op=='upload' for _,op in s['wire'].calls[len(calls):])


def test_old_transport_unknown_does_not_hide_catalog_and_is_retained_on_switch(selection):
    s = selection
    adapter = s['app'].state.transport.adapters[(OWNER, 'account-a', 'oss')]
    with adapter.store.db() as db:
        seq = db.execute("INSERT INTO fg_download(scope,state,name,blob,code) VALUES(?,'UNKNOWN','old',NULL,'FILE_GATEWAY_HTTP_ERROR')", (adapter.store.scope,)).lastrowid
        before = tuple(db.execute('SELECT * FROM fg_download WHERE seq=?', (seq,)).fetchone())
    assert len(s['api'].get('/api/im/catalog').json()['data']['workbenches']) == 2
    assert choose(s).status_code == 200
    with adapter.store.db() as db:
        assert tuple(db.execute('SELECT * FROM fg_download WHERE seq=?', (seq,)).fetchone())==before


def test_select_failure_before_database_commit_preserves_binding_config_and_router(selection,monkeypatch):
    s=selection;before=snapshot(s)
    def fail(*args,**kwargs):raise OSError('synthetic audit persistence failure')
    monkeypatch.setattr(s['repository'],'audit',fail)
    with pytest.raises(OSError):s['app'].state.collector_selection.select(OWNER,{'workbench_id':'wb-b','account_uid':'account-c','expected_version':1})
    assert snapshot(s)==before


@pytest.mark.parametrize('path,method', [('/api/im/catalog', 'GET'),
    ('/api/im/catalog/refresh', 'POST'), ('/api/im/binding/select', 'POST')])
def test_catalog_and_selection_routes_require_login(selection, path, method):
    s = selection
    before = snapshot(s)
    response = s['api'].request(method, path, headers={'Authorization': ''}, json={})
    assert response.status_code == 401
    assert snapshot(s) == before


def test_other_logged_in_workspace_shares_directory_but_cannot_claim_occupied_account(selection):
    s=selection;before=snapshot(s)
    token=s['app'].state.auth.issue_token(AuthUser('other-owner','other-owner'))
    headers={'Authorization':'Bearer '+token}
    data=s['api'].get('/api/im/catalog',headers=headers).json()['data']
    assert len(data['workbenches'])==2 and data['selected'] is None
    response=s['api'].post('/api/im/binding/select',headers=headers,json={'workbench_id':'wb-a','account_uid':'account-a','expected_version':0})
    assert response.status_code in (400,409),response.text
    assert s['repository'].binding_for('other-owner') is None and snapshot(s)==before


def test_snapshot_refresh_is_explicit_and_never_changes_binding(selection):
    s = selection
    before = snapshot(s)
    response = s['api'].post('/api/im/catalog/refresh')
    assert response.status_code == 200, response.text
    assert response.json()['data']['contract']==CONTRACT
    assert snapshot(s) == before


def test_stage_adapter_failure_preserves_persisted_binding_and_profile(selection,monkeypatch):
    import server.institution_runtime as module
    s=selection;before=snapshot(s)
    def fail(*args,**kwargs):raise ValueError('synthetic adapter initialization failure')
    monkeypatch.setattr(module.InstitutionTransport,'__init__',fail)
    with pytest.raises(ValueError,match='synthetic adapter'):
        s['app'].state.collector_selection.select(OWNER,{'workbench_id':'wb-b','account_uid':'account-c','expected_version':1})
    assert snapshot(s)==before


def test_commit_failure_rolls_back_profile_router_and_binding(selection, monkeypatch):
    from contextlib import contextmanager
    s = selection
    before = snapshot(s)
    store = s['app'].state.repository
    original = store.write
    @contextmanager
    def failed_commit(owner):
        with original(owner) as tx:
            yield tx
            raise RuntimeError('synthetic transaction commit failure')
    monkeypatch.setattr(store, 'write', failed_commit)
    with pytest.raises(RuntimeError, match='synthetic transaction'):
        s['app'].state.collector_selection.select(OWNER, {'workbench_id': 'wb-b', 'account_uid': 'account-c', 'expected_version': 1})
    assert snapshot(s) == before


def test_selection_has_no_profile_write_or_disk_rollback_dependency(selection,monkeypatch):
    import server.institution_settings as module
    s=selection;original=s['profiles'].read_bytes()
    def forbidden(*args,**kwargs):pytest.fail('Selection must not write connection profile')
    monkeypatch.setattr(module,'atomic_write',forbidden)
    assert choose(s).status_code==200
    assert s['profiles'].read_bytes()==original


def test_selection_keeps_shared_connection_and_pending_test_is_path_scoped(selection):
    s=selection;original=s['connection'].read_bytes();assert choose(s).status_code==200
    view=s['api'].get('/api/im/mq-config').json()['data']
    assert view['configured'] and not view['applied'] and not view['background_enabled']
    assert (view['workbench_id'],view['account_uid'])==('wb-b','account-c')
    body={'base_url':view['base_url'],'config_yaml':'','expected_revision':view['revision']}
    response=s['api'].put('/api/im/mq-config',json=body);assert response.status_code==200,response.text
    assert response.json()['data']['saved'] and not response.json()['data']['applied']
    calls=len(s['wire'].calls);result=s['api'].post('/api/im/mq-config/test',json=body)
    assert result.status_code==200 and result.json()['data']['passed'] and not result.json()['data']['saved']
    assert s['wire'].calls[calls:]==[('xj_dir_v1','pending')]
    assert s['connection'].read_bytes()==original


def test_switch_starts_empty_business_and_uses_new_accounts_contacts(selection):
    s=selection;group,member,thread=seed_business(s)
    s['repository'].ingest(OWNER,thread,event_id='old-history',source_seq=1,text='old account private history',trade_day='2026-10-20',historical=True)
    with s['repository'].db.transaction(write=False) as tx:
        messages=[dict(r) for r in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(thread,)).fetchall()]
    assert choose(s,'wb-b','account-d').status_code==200
    with s['repository'].db.transaction(write=False) as tx:
        assert tx.execute('SELECT archived FROM xj2_group WHERE group_id=?',(group,)).fetchone()['archived']==1
        assert [dict(r) for r in tx.execute('SELECT * FROM xj2_message WHERE thread_id=?',(thread,)).fetchall()]==messages
    from server.negotiation.prices import InvalidFact
    with pytest.raises(InvalidFact,match='selected collector/account'):
        s['repository'].context(OWNER,member)
    assert s['api'].get('/api/monitor/counterparties').json()['data']==[]
    assert s['api'].get('/api/im/contacts').json()['data']['contacts'][0]['uin']=='peer-wb-b-account-d'



def test_previously_unbound_owner_can_select_authenticated_pair_with_expected_version_zero(selection):
    s = selection
    with s['repository'].write(OWNER) as tx:
        tx.execute('DELETE FROM xj2_binding WHERE owner_id=?', (OWNER,))
    response = choose(s, 'wb-b', 'account-d', version=0)
    assert response.status_code == 200, response.text
    assert response.json()['data']['selected'] == {'workbench_id': 'wb-b', 'account_uid': 'account-d', 'version': 1}
    assert s['repository'].binding_for(OWNER)['workbench_id'] == 'wb-b'


def test_directory_download_failure_keeps_saved_pair_and_reports_reason(selection):
    s=selection;before=snapshot(s)
    s['publish']('wb-a','account-a','catalog');s['wire'].lose_download=True
    response=s['api'].post('/api/im/catalog/refresh');assert response.status_code==200,response.text
    data=response.json()['data'];assert data['refresh_error']
    assert data['selected']=={'workbench_id':'wb-a','account_uid':'account-a','version':1}
    assert snapshot(s)==before
    with s['runtime'].directory.db() as db:assert db.execute("SELECT COUNT(*) FROM ip_input WHERE state='UNKNOWN'").fetchone()[0]==1


def test_legacy_manual_binding_endpoint_cannot_bypass_authenticated_directory(selection):
    s = selection
    with s['repository'].write(OWNER) as tx:
        tx.execute('DELETE FROM xj2_binding WHERE owner_id=?', (OWNER,))
    old_profile = s['profiles'].read_bytes()
    response = s['api'].post('/api/im/binding', json={
        'workbench_id': 'forged-workbench', 'account_uid': 'forged-account', 'transport': 'oss'})
    assert response.status_code in (400, 403, 409), response.text
    assert s['repository'].binding_for(OWNER) is None
    assert s['profiles'].read_bytes() == old_profile


def test_persisted_binding_institution_mismatch_refuses_restore_without_rewriting(selection):
    s=selection;prefs=s['repository'].preferences(OWNER);prefs['relay_institution_id']='wrong-institution'
    with s['repository'].write(OWNER) as tx:tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?',(json.dumps(prefs),OWNER))
    before=snapshot(s)
    with pytest.raises(ValueError,match='机构'):s['make_app']()
    assert snapshot(s)==before


def test_current_only_selection_preserves_legacy_mail_bytes_without_activating_route(selection):
    s=selection;legacy=s['tmp']/'retired-mail.json';legacy.write_text(json.dumps({'mail':{'smtp_password':'synthetic-private'}}));before=legacy.read_bytes()
    assert choose(s).status_code==200
    assert legacy.read_bytes()==before
    assert {k[2] for k in s['app'].state.transport.adapters if k[0]==OWNER}=={'oss'}
    assert 'synthetic-private' not in s['api'].get('/api/im/catalog').text


def test_other_owners_adapter_identity_is_untouched_when_current_owner_selects(selection):
    s = selection
    sentinel = object()
    key = ('different-owner', 'different-account', 'mail')
    s['app'].state.transport.adapters[key] = sentinel
    assert choose(s).status_code == 200
    assert s['app'].state.transport.adapters[key] is sentinel


def _verified_link(s):
    response = s['api'].get('/api/im/links')
    assert response.status_code == 200, response.text
    return next(row for row in response.json()['data']['links'] if row['mode'] == 'oss')


def test_actual_selection_check_enables_current_link_and_survives_restart_without_authorizing_send(selection):
    s=selection;assert choose(s).status_code==200
    current=_verified_link(s);assert current['tested'] and current['active'] and not current['catalog_stale']
    assert (current['workbench_id'],current['account_uid'],current['version'])==('wb-b','account-c',2)
    with s['repository'].db.transaction(write=False) as tx:
        assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?',(OWNER,)).fetchone()['send_allowed']==0
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']==0
    with TestClient(s['make_app']()) as api:
        api.headers['Authorization']='Bearer '+s['bearer']
        persisted=next(x for x in api.get('/api/im/links').json()['data']['links'] if x['mode']=='oss')
        assert persisted['tested'] and persisted['active'] and persisted['workbench_id']=='wb-b'


@pytest.mark.parametrize('field,value', [('workbench_id', 'wb-a'), ('account_uid', 'other-account'), ('version', 1)])
def test_directory_check_evidence_cannot_be_inherited_by_other_pair_or_binding_version(selection, field, value):
    s = selection
    assert choose(s).status_code == 200
    binding = s['repository'].binding_for(OWNER)
    preferences = s['repository'].preferences(OWNER)
    preferences['collector_directory_checks']['oss'][field] = value
    with s['repository'].write(OWNER) as tx:
        tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?', (json.dumps(preferences), OWNER))
    assert _verified_link(s)['tested'] is False


def test_offline_cached_directory_keeps_saved_binding_and_returns_stale_contacts(selection):
    s=selection;assert choose(s).status_code==200;before=snapshot(s);s['advance'](61)
    link=_verified_link(s);assert link['active'] and link['catalog_stale']
    result=s['api'].get('/api/im/contacts');assert result.status_code==200
    assert result.json()['data']['stale'] is True
    assert snapshot(s)==before


def test_check_evidence_ages_without_new_network_data_being_made_current(selection):
    s=selection;assert choose(s).status_code==200;calls=list(s['wire'].calls)
    stamp=s['runtime'].directory.account('wb-b','account-c')['updated_at'];s['advance'](61)
    link=_verified_link(s);assert link['catalog_stale']
    assert s['runtime'].directory.account('wb-b','account-c')['updated_at']==stamp
    assert s['wire'].calls==calls
