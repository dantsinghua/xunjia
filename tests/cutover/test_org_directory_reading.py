"""Independent organization reader/projection gates; synthetic local data only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from tests.cutover.test_im_catalog_selection import selection
from tests.institution_test_support import directory_request

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture
def reader():
    path=ROOT/'deploy/collector-qtrade-v22/source/src/qtrade_agent/adapters/qidian/org.py'
    spec=importlib.util.spec_from_file_location('independent_native_org_reader',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def local_org(tmp_path):
    uid='3000000001'
    org=tmp_path/(uid+'-org.db');people=tmp_path/(uid+'-org-member.db')
    with sqlite3.connect(org) as db:
        db.executescript('CREATE TABLE OrgDepartment(uid TEXT,pid TEXT,name TEXT);CREATE TABLE OrgD2M(did TEXT,mid TEXT,ord INT);CREATE TABLE OrgD2D(did TEXT,ord INT);')
        db.executemany('INSERT INTO OrgDepartment VALUES(?,?,?)',[('desk','0','合成总台'),('team','desk','合成交易组')])
        db.executemany('INSERT INTO OrgD2M VALUES(?,?,?)',[('desk','peer-a',2),('team','peer-a',1)])
        db.executemany('INSERT INTO OrgD2D VALUES(?,?)',[('desk',7),('team',3)])
    with sqlite3.connect(people) as db:
        db.execute('CREATE TABLE OrgMember(uin TEXT,name TEXT,account TEXT)')
        db.executemany('INSERT INTO OrgMember VALUES(?,?,?)',[('peer-a','合成人员甲','synthetic-a'),('peer-b','未分组人员乙','synthetic-b')])
    key=tmp_path/'protected-org-key.bin';key.write_bytes(b'synthetic-only-not-real-password');key.chmod(0o600)
    config=tmp_path/'native.json';config.write_text(json.dumps({'account_uid':uid,'org_password_file':key.name,'org_password_format':'raw'}));config.chmod(0o600)
    return {'source':SimpleNamespace(path=tmp_path/(uid+'.db')),'config':config,'org':org,'people':people,'key':key,'uid':uid}


def test_existing_local_reader_preserves_parent_order_multiple_memberships_and_unassigned_people(reader,local_org):
    s=local_org;before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in [s['org'],s['people'],s['config'],s['key']]}
    result=reader.read_account_org(s['source'],s['uid'],s['config'],clock=lambda:1000)
    assert result['org_state']=='ready' and result['org_updated_at']==1000 and result['org_error'] is None
    assert result['org']['departments']==[{'id':'desk','parent_id':'','name':'合成总台','order':7},{'id':'team','parent_id':'desk','name':'合成交易组','order':3}]
    members=result['org']['members'];assert len(members)==3
    assert {m['department_id'] for m in members if m['uin']=='peer-a'}=={'desk','team'}
    assert next(m for m in members if m['uin']=='peer-b')['department_id']==''
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in before.items())


def test_unchanged_native_sources_reuse_cache_without_fabricating_new_report_time(reader,local_org):
    s=local_org;cache={}
    first=reader.read_account_org(s['source'],s['uid'],s['config'],cache=cache,clock=lambda:1000)
    second=reader.read_account_org(s['source'],s['uid'],s['config'],cache=cache,clock=lambda:2000)
    assert first==second and second['org_updated_at']==1000


def test_successfully_read_empty_schema_is_empty_while_legacy_parser_remains_strict(reader,local_org):
    s=local_org
    with sqlite3.connect(s['org']) as db:db.executescript('DELETE FROM OrgD2M;DELETE FROM OrgDepartment;DELETE FROM OrgD2D;')
    with sqlite3.connect(s['people']) as db:db.execute('DELETE FROM OrgMember')
    result=reader.read_account_org(s['source'],s['uid'],s['config'],clock=lambda:1000)
    assert result['org_state']=='empty' and result['org']=={'departments':[],'members':[]}
    with pytest.raises(ValueError):reader.load_org_plain_bytes(s['org'].read_bytes(),s['people'].read_bytes())


def test_no_authorized_org_key_is_unreported_not_an_empty_native_organization(reader,local_org):
    s=local_org;s['config'].write_text(json.dumps({'account_uid':s['uid']}))
    with pytest.raises(reader.OrgReadError) as exc:reader.read_account_org(s['source'],s['uid'],s['config'])
    assert exc.value.state=='unreported' and exc.value.code=='ORG_CREDENTIALS_NOT_CONFIGURED'


@pytest.mark.parametrize('mode',['config-account','source-account','unsafe-key-permissions','bad-encrypted-data','missing-schema'])
def test_invalid_account_credentials_or_sources_never_turn_into_successful_empty_org(reader,local_org,mode):
    s=local_org
    if mode=='config-account':
        cfg=json.loads(s['config'].read_text());cfg['account_uid']='3999999999';s['config'].write_text(json.dumps(cfg))
    if mode=='source-account':s['source']=SimpleNamespace(path=s['org'].parent/'3999999999.db')
    if mode=='unsafe-key-permissions':s['key'].chmod(0o644)
    if mode=='bad-encrypted-data':s['org'].write_bytes(b'bad-encrypted-data'*64)
    if mode=='missing-schema':
        with sqlite3.connect(s['org']) as db:db.execute('DROP TABLE OrgDepartment')
    with pytest.raises(reader.OrgReadError) as exc:reader.read_account_org(s['source'],s['uid'],s['config'])
    assert exc.value.state=='error'
    assert s['key'].read_text() not in str(exc.value)


@pytest.mark.parametrize('state',['ready','empty','unreported','error'])
def test_real_signed_contacts_keep_org_state_and_full_pair_in_projection(selection,state):
    s=selection;org={'departments':[{'id':'synthetic-d','parent_id':'','name':'Org A','order':1}],
         'members':[{'uin':'person-a','name':'A','account':'a','department_id':'synthetic-d','order':1}]}
    # Institution organization is shared. Failed/unreported refreshes retain the last good version.
    s['publish']('wb-a','account-a','contacts',extra={'org':org,'org_state':'ready','org_updated_at':s['now']-20})
    s['refresh']()
    extra={'org':org if state=='ready' else {'departments':[],'members':[]},'org_state':state,'org_updated_at':s['now']-10}
    s['publish']('wb-a','account-a','contacts',extra=extra);s['refresh']();calls=list(s['wire'].calls)
    response=s['api'].get('/api/im/contacts');assert response.status_code==200,response.text
    result=response.json()['data'];assert (result['workbench_id'],result['account_uid'])==('wb-a','account-a')
    if state in ('ready','empty'):
        assert result['org']==extra['org'] and result['org_state']==state and result['org_updated_at']==s['now']-10
        assert result['org_error'] is None
    else:
        assert result['org']==org and result['org_state']=='error' and result['org_updated_at']==s['now']-20
        assert result['org_error']=='ORGANIZATION_REFRESH_FAILED'
    other=s['runtime'].directory.contacts('wb-b','account-c','another-owner')
    assert other['org']==result['org'] and other['org_updated_at']==result['org_updated_at']
    assert other['contacts']!=result['contacts'] and s['wire'].calls==calls


@pytest.fixture
def native_host_module():
    path=ROOT/'deploy/collector-qtrade-v22/source/src/qtrade_agent/ossbox/xunjia_v22.py'
    spec=importlib.util.spec_from_file_location('independent_native_org_host',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_unexpected_org_provider_failure_is_published_as_error_with_last_success_preserved(native_host_module,tmp_path):
    import asyncio
    from relay.protocol import Binding,Codec
    from relay.collector_bridge import CollectorBridge
    from relay.qtrade_native_port import QTradeNativePort
    from server.receiver_ledger import ReceiverLedger
    from tests.mq_fixtures import MQWire
    from tests.cutover.file_gateway_fakes import NOW
    pair=Binding('synthetic-org-host-owner','3000000001','synthetic-org-wb',b's'*32,b'h'*32)
    now=[float(NOW)];wire=MQWire();channel=wire.transport(pair,tmp_path/'publications.db',role='collector',clock=lambda:now[0])
    ledger=ReceiverLedger(tmp_path/'receiver.db',pair,clock=lambda:now[0])
    account={'id':'synthetic-native','self_uid':pair.account_uid,'state':'running','channel':'qidian'}
    store=SimpleNamespace(list_accounts=lambda **kwargs:[account],outbox_seq_bounds=lambda:(0,0))
    bus=SimpleNamespace(supports_xunjia_v22_guard=True)
    loop=asyncio.new_event_loop()
    try:
        port=QTradeNativePort(binding=pair,store=store,bus=bus,loop=loop,legacy_inbox_disabled=lambda:True)
        bridge=CollectorBridge(ledger=ledger,adapters={'oss':channel},native_send=port.native_send,native_reconcile=port.native_reconcile)
        old_org={'departments':[{'id':'desk','parent_id':'','name':'Synthetic desk','order':1}],
                 'members':[{'uin':'peer-a','name':'Synthetic A','account':'a','department_id':'desk','order':1}]}
        old_people=[{'uin':'peer-a','name':'Synthetic A'}]
        provider=lambda a:{'contacts':old_people,'org':old_org,'org_state':'ready','org_updated_at':now[0]}
        host=native_host_module.XunjiaV22Host(store=store,bus=bus,loop=loop,binding=pair,bridge=bridge,port=port,
            state_path=tmp_path/'host.db',legacy_inbox_disabled=lambda:True,contacts_provider=provider)
        host._publish_directory()
        def contacts_packets():
            return [env for _,_,blob in wire.uploaded for env in [Codec(pair,clock=lambda:now[0]).unpack(blob)] if env['kind']=='contacts']
        first=contacts_packets()[-1]
        with host._db() as db:first_pointer=db.execute("SELECT value FROM host_meta WHERE key='org_publication_id'").fetchone()[0]
        now[0]+=60
        def broken(_):raise RuntimeError('synthetic-private-provider-detail-not-for-output')
        host.contacts_provider=broken;host._publish_directory()
        observed=contacts_packets();assert len(observed)==2
        error=observed[-1]['payload']
        assert error['org_state']=='error' and error['org_error']=='ORG_PROVIDER_UNAVAILABLE'
        assert error['org']==old_org and error['contacts']==old_people
        assert error['org_updated_at']==first['payload']['org_updated_at']
        assert 'synthetic-private-provider-detail' not in json.dumps(error)
        with host._db() as db:
            assert db.execute("SELECT value FROM host_meta WHERE key='org_publication_id'").fetchone()[0]==first_pointer
        assert not hasattr(port,'allow_native_send')
    finally:loop.close()


def test_native_from_env_accepts_authorized_org_key_references_without_legacy_network_branch(native_host_module,tmp_path,monkeypatch):
    import asyncio
    from tests.institution_test_support import AUTH, BASE, YAML
    from relay.gateway_paths import CONTRACT,InstitutionKeys,account_paths
    auth=tmp_path/'auth.yaml';auth.write_text(YAML);auth.chmod(0o600)
    cfg={'contract':CONTRACT,'workbench_id':'synthetic-wb','account_uid':'3000000001',
         'org_password_file':'authorized-org.bin','org_password_format':'raw',
         'file_gateway':{'base_url':BASE,'auth_file':'auth.yaml'}}
    path=tmp_path/'collector.json';path.write_text(json.dumps(cfg));path.chmod(0o600)
    monkeypatch.setenv('XUNJIA_COLLECTOR_VERSION','2.2');monkeypatch.setenv('XUNJIA_COLLECTOR_CONFIG_FILE',str(path))
    loop=asyncio.new_event_loop()
    try:
        host=native_host_module.XunjiaV22Host.from_env(store=SimpleNamespace(),
            bus=SimpleNamespace(supports_xunjia_v22_guard=True),loop=loop,box=None,data_dir=tmp_path/'runtime',
            legacy_inbox_disabled=lambda:True,contacts_provider=lambda a:{'contacts':[]})
        assert host.binding.account_uid==cfg['account_uid'] and not hasattr(host,'allow_native_send')
        keys=InstitutionKeys.from_auth(AUTH);assert host.binding.owner_user_id==keys.owner_id
        assert host.bridge.adapters['oss'].outbound.path==account_paths('synthetic-wb','3000000001')['uplink_path']
    finally:loop.close()


@pytest.mark.parametrize('field',['owner_user_id','seal_key_file','uplink_url','downlink_url'])
def test_bundled_host_rejects_retired_queue_fields_without_opening_old_ledger(native_host_module,tmp_path,monkeypatch,field):
    import asyncio
    from relay.gateway_paths import CONTRACT
    cfg={'contract':CONTRACT,'workbench_id':'synthetic-wb','account_uid':'3000000001',
         'file_gateway':{'base_url':'https://synthetic.invalid','auth_file':'not-read.yaml'}}
    if field.endswith('_url'):cfg['file_gateway'][field]='https://retired.invalid/queue'
    else:cfg[field]='must-not-be-read'
    path=tmp_path/'collector.json';path.write_text(json.dumps(cfg));path.chmod(0o600)
    root=tmp_path/'runtime';legacy=root/'xunjia-v22';legacy.mkdir(parents=True)
    marker=legacy/'unknown.evidence';marker.write_bytes(b'preserve-unknown')
    monkeypatch.setenv('XUNJIA_COLLECTOR_VERSION','2.2')
    monkeypatch.setenv('XUNJIA_COLLECTOR_CONFIG_FILE',str(path))
    loop=asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError,match='invalid_institution_collector_configuration'):
            native_host_module.XunjiaV22Host.from_env(store=SimpleNamespace(),bus=SimpleNamespace(),loop=loop,
                box=None,data_dir=root,legacy_inbox_disabled=lambda:True)
        assert marker.read_bytes()==b'preserve-unknown'
        assert not (root/'institution-path-v1').exists()
    finally:loop.close()
