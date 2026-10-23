"""Developer checks for the delivered account-bound organization source and host."""
import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from relay.file_gateway import GatewayAuth
from relay.gateway_paths import InstitutionKeys, ORGANIZATION_PATH
from relay.protocol import Codec

ROOT = Path(__file__).resolve().parents[2]
MIRROR = ROOT / 'deploy/collector-qtrade-v22/source/src/qtrade_agent'


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, MIRROR / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

ORG = load('org_source_development', 'adapters/qidian/org.py')


def fixtures(tmp_path, *, empty=False):
    base = tmp_path / '100001.db'
    base.touch()
    for suffix, schema in [('-org.db', 'CREATE TABLE OrgDepartment(uid,pid,name);CREATE TABLE OrgD2M(did,mid,ord);'),
                           ('-org-member.db', 'CREATE TABLE OrgMember(uin,name,account);')]:
        db = sqlite3.connect(tmp_path / ('100001' + suffix))
        db.executescript(schema)
        if not empty:
            if suffix == '-org.db':
                db.executemany('INSERT INTO OrgDepartment VALUES(?,?,?)', [('d1', '', 'Synthetic root'), ('d2', 'd1', 'Synthetic child')])
                db.executemany('INSERT INTO OrgD2M VALUES(?,?,?)', [('d1', 'm1', 1), ('d2', 'm1', 2)])
            else:
                db.executemany('INSERT INTO OrgMember VALUES(?,?,?)', [('m1', 'Synthetic member', 'staff1'), ('m2', 'Synthetic ungrouped', 'staff2')])
        db.commit()
        db.close()
    key = tmp_path / 'key.private'
    key.write_bytes(b'synthetic-only')
    key.chmod(0o600)
    cfg = tmp_path / 'collector.json'
    cfg.write_text(json.dumps({'account_uid': '100001', 'org_password_file': str(key), 'org_password_format': 'raw'}))
    cfg.chmod(0o600)
    return SimpleNamespace(path=str(base)), cfg, key


def test_reader_preserves_departments_multi_membership_and_cached_observation_time(tmp_path):
    source, cfg, _ = fixtures(tmp_path)
    cache = {}
    result = ORG.read_account_org(source, '100001', cfg, cache=cache, clock=lambda: 100)
    assert result['org_state'] == 'ready' and result['org_updated_at'] == 100
    assert len(result['org']['departments']) == 2
    assert [(m['uin'], m['department_id'], m['order']) for m in result['org']['members']] == [('m1', 'd1', 1), ('m1', 'd2', 2), ('m2', '', 0)]
    cached = ORG.read_account_org(source, '100001', cfg, cache=cache, clock=lambda: 200)
    assert cached == result, 'Unchanged source must not receive an invented new organization time'


def test_reader_reports_verified_empty_but_legacy_parser_default_remains_strict(tmp_path):
    source, cfg, _ = fixtures(tmp_path, empty=True)
    result = ORG.read_account_org(source, '100001', cfg, clock=lambda: 100)
    assert result == {'org': {'departments': [], 'members': []}, 'org_state': 'empty', 'org_updated_at': 100, 'org_error': None}
    with pytest.raises(ValueError):
        ORG.load_org_plain_bytes((tmp_path / '100001-org.db').read_bytes(), (tmp_path / '100001-org-member.db').read_bytes())


def test_missing_key_configuration_is_unreported_not_empty(tmp_path):
    source, cfg, _ = fixtures(tmp_path)
    cfg.write_text(json.dumps({'account_uid': '100001'}))
    with pytest.raises(ORG.OrgReadError) as exc:
        ORG.read_account_org(source, '100001', cfg)
    assert exc.value.state == 'unreported' and exc.value.code == 'ORG_CREDENTIALS_NOT_CONFIGURED'


@pytest.mark.parametrize('mode', ['other-config-account', 'other-source-account', 'missing-key', 'public-key', 'bad-schema'])
def test_reader_rejects_wrong_scope_missing_key_or_schema_without_returning_fake_empty(tmp_path, mode):
    source, cfg, key = fixtures(tmp_path)
    if mode == 'other-config-account':
        value = json.loads(cfg.read_text()); value['account_uid'] = '100002'; cfg.write_text(json.dumps(value))
    elif mode == 'other-source-account':
        source.path = str(tmp_path / '100002.db')
    elif mode == 'missing-key':
        key.rename(tmp_path / 'key.retained')
    elif mode == 'public-key':
        key.chmod(0o644)
    else:
        db = sqlite3.connect(tmp_path / '100001-org.db'); db.execute('DROP TABLE OrgDepartment'); db.close()
    with pytest.raises(ORG.OrgReadError) as exc:
        ORG.read_account_org(source, '100001', cfg)
    assert exc.value.state == 'error'
    assert 'synthetic-only' not in str(exc.value)



# Import only the reference package's publisher and receiver core; no complete collector is installed.
INSTITUTION = importlib.import_module('deploy.collector-qtrade-v22.source.src.qtrade_agent.ossbox.institution')


@pytest.fixture
def publisher_chain(tmp_path):
    source, cfg, key = fixtures(tmp_path)
    now = [1800000000.0]; uploaded = []; cache = {}
    account = {'id': 'qd-fixture', 'channel': 'qidian', 'self_uid': '100001', 'state': 'running'}
    class Accounts:
        def list_accounts(self, channel):
            return [account] if channel == 'qidian' else []
    class Client:
        def __init__(self, path): self.path = path
        def upload(self, name, blob): uploaded.append((self.path, name, blob))
    keys = InstitutionKeys.from_auth(GatewayAuth('synthetic', b'0123456789abcdef'))
    loop = asyncio.new_event_loop()
    reader = lambda row: ORG.read_account_org(source, row['self_uid'], cfg, cache=cache, clock=lambda: now[0])
    instance = INSTITUTION.InstitutionCollectorHost(store=Accounts(), bus=None, loop=loop, keys=keys,
        config={'workbench_name': 'Synthetic desk'}, settings=SimpleNamespace(workbench_id='org-wb'),
        data_dir=tmp_path, legacy_inbox_disabled=lambda: True,
        friends_provider=lambda row: [{'uin': 'friend', 'name': 'Synthetic friend'}],
        organization_provider=reader, clock=lambda: now[0], client_factory=Client)
    instance.active = True
    def publications():
        codec = Codec(keys.binding('org-wb', 'organization'), clock=lambda: now[0])
        return [codec.unpack(blob) for path, _, blob in uploaded if path == ORGANIZATION_PATH]
    try:
        yield SimpleNamespace(host=instance, now=now, uploaded=uploaded, publications=publications,
                              source=source, config=cfg, key=key, org=tmp_path/'100001-org.db',
                              people=tmp_path/'100001-org-member.db')
    finally:
        instance.active = False
        loop.close()


def test_current_provider_reads_real_org_and_publisher_signs_without_mutating_sources(publisher_chain):
    s = publisher_chain
    paths = [Path(s.source.path), s.config, s.key, s.org, s.people]
    import hashlib
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    s.host.metadata_once()
    [published] = s.publications()
    assert published['payload']['state'] == 'ready'
    assert len(published['payload']['org']['departments']) == 2
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == value for p, value in before.items())
    assert s.host.errors == {}


def test_current_provider_and_publisher_distinguish_verified_empty(publisher_chain):
    s = publisher_chain
    with sqlite3.connect(s.org) as db:
        db.executescript('DELETE FROM OrgD2M; DELETE FROM OrgDepartment;')
    with sqlite3.connect(s.people) as db:
        db.execute('DELETE FROM OrgMember')
    s.host.metadata_once()
    [published] = s.publications()
    assert published['payload']['state'] == 'empty'
    assert published['payload']['org'] == {'departments': [], 'members': []}


@pytest.mark.parametrize('failure', ['unreported', 'bad-data', 'unexpected-provider'])
def test_current_provider_failure_preserves_previous_signed_org_and_timestamp(publisher_chain, failure):
    s = publisher_chain
    s.host.metadata_once()
    [previous] = s.publications()
    stamp = s.host.publisher.last('organization:organization')
    previous_uploads = [item for item in s.uploaded if item[0] == ORGANIZATION_PATH]
    if failure == 'unreported':
        s.config.write_text(json.dumps({'account_uid': '100001'}))
    elif failure == 'bad-data':
        s.org.write_bytes(b'synthetic-bad-data' * 64)
    else:
        def broken(row): raise RuntimeError('synthetic-private-detail')
        s.host.organization_provider = broken
    s.now[0] += 60
    s.host.publisher.request_sync('100001', True)
    s.host.metadata_once()
    assert s.publications() == [previous]
    assert [item for item in s.uploaded if item[0] == ORGANIZATION_PATH] == previous_uploads
    assert s.host.publisher.last('organization:organization') == stamp
    assert 'contacts:100001' in s.host.errors and 'synthetic-private-detail' not in json.dumps(s.host.errors)
    assert len(s.host.publisher.pending_sync()) == 1


def test_current_oversize_org_preserves_last_publication_and_private_contacts(publisher_chain):
    s = publisher_chain
    s.host.metadata_once()
    [previous] = s.publications()
    previous_stamp = s.host.publisher.last('organization:organization')
    s.now[0] += 60
    s.host.organization_provider = lambda row: {'org_state': 'ready',
        'org': {'departments': [{'id': 'large', 'name': 'x' * (4 * 1024 * 1024)}], 'members': []}}
    s.host.publisher.request_sync('100001', True)
    s.host.metadata_once()
    assert s.publications() == [previous]
    assert s.host.publisher.last('organization:organization') == previous_stamp
    assert s.host.errors['contacts:100001'] == 'ProtocolError'
    assert len(s.host.publisher.pending_sync()) == 1
    from relay.gateway_paths import account_paths
    friends_path = account_paths('org-wb', '100001')['friends_path']
    friends = [Codec(s.host.keys.binding('org-wb', '100001'), clock=lambda:s.now[0]).unpack(blob)
               for path, _, blob in s.uploaded if path == friends_path]
    assert len(friends) == 2 and friends[-1]['payload']['contacts'] == friends[0]['payload']['contacts']
