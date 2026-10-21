"""Developer checks for the delivered account-bound organization source and host."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from relay.protocol import Binding
from tests.mq_fixtures import MQWire

ROOT = Path(__file__).resolve().parents[2]
MIRROR = ROOT / 'deploy/collector-qtrade-v22/source/src/qtrade_agent'


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, MIRROR / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

ORG = load('org_source_development', 'adapters/qidian/org.py')
HOST = load('org_host_development', 'ossbox/xunjia_v22.py')
B = Binding('org-owner', '100001', 'org-wb', b'k' * 32, b's' * 32)


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


def host(tmp_path, provider):
    wire = MQWire(); now = [100.0]
    adapter = wire.transport(B, tmp_path / 'publication.db', role='collector', clock=lambda: now[0])
    instance = HOST.XunjiaV22Host.__new__(HOST.XunjiaV22Host)
    instance.state_path = tmp_path / 'host.db'
    with instance._db() as db:
        db.execute('CREATE TABLE host_meta(key TEXT PRIMARY KEY,value TEXT)')
    instance.binding = B; instance.ledger = SimpleNamespace(clock=lambda: now[0])
    instance.port = SimpleNamespace(_account=lambda: {'id': 'qd-fixture', 'self_uid': B.account_uid})
    instance.bridge = SimpleNamespace(adapters={'oss': adapter}, publish_capabilities=lambda: None)
    instance.contacts_provider = provider
    return instance, adapter, wire, now


def latest(adapter):
    with adapter.store.db() as db:
        return json.loads(db.execute('SELECT data FROM tx_publication ORDER BY rowid DESC LIMIT 1').fetchone()[0])['payload']


def test_host_preserves_previous_successful_org_on_error_and_restart_with_pointer_only(tmp_path):
    organization = {'departments': [{'id': 'd', 'parent_id': '', 'name': 'Synthetic', 'order': 0}],
                    'members': [{'uin': 'm', 'name': 'Synthetic', 'account': 'staff', 'department_id': 'd', 'order': 0}]}
    provider = lambda _: {'contacts': [{'uin': 'friend', 'name': 'Synthetic'}], 'org': organization,
                          'org_state': 'ready', 'org_updated_at': 99.0}
    h, adapter, _, now = host(tmp_path, provider)
    h._publish_directory()
    with h._db() as db:
        pointer = db.execute("SELECT value FROM host_meta WHERE key='org_publication_id'").fetchone()[0]
    assert pointer.startswith('contacts-') and len(pointer) < 128
    now[0] = 160
    h.contacts_provider = lambda _: {'contacts': [{'uin': 'friend', 'name': 'Synthetic'}], 'org_state': 'error', 'org_error': 'ORG_SOURCE_UNAVAILABLE'}
    h._publish_directory()
    book = latest(adapter)
    assert book['org'] == organization and book['org_updated_at'] == 99
    assert book['org_state'] == 'error' and book['org_error'] == 'ORG_SOURCE_UNAVAILABLE'
    assert h._previous_org() == {'org': organization, 'org_updated_at': 99.0}
    with h._db() as db:
        assert db.execute("SELECT value FROM host_meta WHERE key='org_publication_id'").fetchone()[0] == pointer


def test_host_distinguishes_true_empty_from_no_report(tmp_path):
    h, adapter, _, _ = host(tmp_path, lambda _: {'contacts': [{'uin': 'friend'}], 'org_state': 'unreported', 'org_error': 'ORG_CREDENTIALS_NOT_CONFIGURED'})
    h._publish_directory(); assert latest(adapter)['org_state'] == 'unreported'
    h.contacts_provider = lambda _: {'contacts': [], 'org': {'departments': [], 'members': []}, 'org_state': 'empty', 'org_updated_at': 100}
    h._publish_directory(); assert latest(adapter)['org_state'] == 'empty'


def test_host_oversize_org_reports_error_without_losing_private_contacts(tmp_path):
    org = {'departments': [{'id': 'd', 'name': 'x' * (4 * 1024 * 1024)}], 'members': []}
    h, adapter, _, _ = host(tmp_path, lambda _: {'contacts': [{'uin': 'friend'}], 'org': org, 'org_state': 'ready', 'org_updated_at': 100})
    h._publish_directory(); book = latest(adapter)
    assert book['org_state'] == 'error' and book['org_error'] == 'ORG_DOCUMENT_TOO_LARGE'
    assert book['org'] == {} and book['org_updated_at'] is None and book['contacts'] == [{'uin': 'friend'}]


def test_unexpected_provider_exception_publishes_error_with_previous_contacts_and_org(tmp_path):
    previous = {'contacts': [{'uin': 'friend', 'name': 'Synthetic'}],
                'org': {'departments': [{'id': 'd', 'name': 'Synthetic'}], 'members': []},
                'org_state': 'ready', 'org_updated_at': 99}
    h, adapter, _, now = host(tmp_path, lambda _: previous)
    h._publish_directory()
    def fail(_): raise RuntimeError('synthetic secret must not escape')
    h.contacts_provider = fail; now[0] = 160
    h._publish_directory(); book = latest(adapter)
    assert book['contacts'] == previous['contacts'] and book['org'] == previous['org']
    assert book['org_updated_at'] == 99
    assert book['org_state'] == 'error' and book['org_error'] == 'ORG_PROVIDER_UNAVAILABLE'
    assert 'synthetic secret' not in json.dumps(book)
