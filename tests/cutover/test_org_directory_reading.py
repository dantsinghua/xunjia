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


def test_current_bundle_has_no_environment_transport_factory(native_host_module):
    """The installed collector owns configuration; no second factory may survive."""
    assert not hasattr(native_host_module.XunjiaV22Host, 'from_env')
    assert not hasattr(native_host_module.XunjiaV22Host, '_publish_directory')
    manifest=json.loads((ROOT/'deploy/collector-qtrade-v22/manifest.json').read_text())
    assert manifest['transport_contract']=='https-mtls-accesstoken'
    for item in manifest['files']:
        path=ROOT/'deploy/collector-qtrade-v22'/item['delivery_path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==item['result_sha256']


@pytest.mark.parametrize('field',['owner_user_id','seal_key_file','uplink_url','downlink_url'])
def test_current_connection_rejects_retired_queue_fields_without_creating_binding(tmp_path,monkeypatch,field):
    """Exercise the actual current configuration API, not a retired constructor."""
    from tests.institution_test_support import build_harness,BASE,YAML,OWNER
    from tests.tls_material import tls_config
    s=build_harness(tmp_path,monkeypatch,configured=False,selected=False)
    try:
        body={'base_url':BASE,'config_yaml':YAML,'expected_revision':'',**tls_config(),field:'retired-value'}
        response=s['api'].put('/api/im/mq-config',json=body)
        assert response.status_code==400, response.text
        assert not s['runtime'].configured
        assert s['repository'].binding_for(OWNER) is None
        assert s['wire'].calls==[]
        assert not s['connection'].exists()
    finally:s['stack'].close()
