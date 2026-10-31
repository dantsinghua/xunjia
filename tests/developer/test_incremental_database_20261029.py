"""Synthetic migration failure/recovery checks; no live database or service."""
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import incremental_database as db
from deploy import build_incremental as builder
from tests.developer.test_incremental_reply_compatibility_20261029 import change
from tests.release.test_incremental_capsule import load, seal, write, installed, FakeManager, case


@pytest.fixture
def database(tmp_path, monkeypatch):
    prefix = tmp_path / 'installed'
    env = write(prefix, 'config/xunjia.env',
                'XUNJIA_DATABASE_URL=oceanbase://user:synthetic_secret@localhost:2881/xunjia_business\n')
    env.chmod(0o600)
    state = SimpleNamespace(reply=False, closed=0, statements=[], fail=False, extra=False, kind='longtext')
    connection = SimpleNamespace(select_db=lambda name: None,
                                 close=lambda: setattr(state, 'closed', state.closed + 1))
    def query(_, sql, args=(), one=False):
        if sql.startswith('SELECT DATABASE'): return {'database_name': 'xunjia_business'}
        if 'information_schema.TABLES' in sql: return [{'TABLE_TYPE': 'BASE TABLE'}]
        if 'information_schema.COLUMNS' in sql:
            rows = [{'COLUMN_NAME': name, 'DATA_TYPE': kind, 'IS_NULLABLE': nullable,
                     'CHARACTER_MAXIMUM_LENGTH': length, 'COLUMN_DEFAULT': None, 'EXTRA': ''}
                    for name, (kind, nullable, length) in db.BASE_TYPES.items()]
            if state.extra: rows.append({'COLUMN_NAME': 'unexpected'})
            if state.reply:
                rows.append({'COLUMN_NAME': 'reply_json', 'DATA_TYPE': state.kind,
                             'IS_NULLABLE': 'YES', 'COLUMN_DEFAULT': None, 'EXTRA': ''})
            return rows
        if sql.startswith('SHOW CREATE TABLE'):
            return {'Table': 'xj2_message', 'Create Table': 'synthetic original definition'}
        raise AssertionError(sql)
    def execute(_, sql):
        state.statements.append(sql)
        if state.fail: raise RuntimeError('synthetic_secret must not appear')
        state.reply = True
    monkeypatch.setattr(db, 'verify_server', lambda _: None)
    monkeypatch.setattr(db, 'query', query)
    monkeypatch.setattr(db, 'execute', execute)
    return prefix, state, lambda **_: connection


def test_check_is_readonly_and_apply_is_verified_and_idempotent(database):
    prefix, state, connect = database
    assert db.run(prefix, connect=connect)['status'] == 'MIGRATION_REQUIRED'
    assert not (prefix / 'deployments').exists()
    assert db.run(prefix, apply=True, connect=connect)['ddl_executed']
    assert state.statements == [db.DDL]
    assert not db.run(prefix, apply=True, connect=connect)['ddl_executed']
    assert state.statements == [db.DDL] and state.closed == 3
    journal = prefix / 'deployments/reply-migration/journal.json'
    assert json.loads(journal.read_text())['status'] == 'VERIFIED'
    assert 'synthetic_secret' not in journal.read_text()
    assert journal.stat().st_mode & 0o777 == 0o600
    assert (journal.parent / 'schema-before.json').exists()


def test_unknown_ddl_cannot_be_replayed_but_completed_column_can_be_reconciled(database):
    prefix, state, connect = database
    state.fail = True
    with pytest.raises(db.MigrationError, match='OUTCOME_UNKNOWN'):
        db.run(prefix, apply=True, connect=connect)
    with pytest.raises(db.MigrationError, match='DBA_RECONCILIATION'):
        db.run(prefix, apply=True, connect=connect)
    assert len(state.statements) == 1
    state.reply = True
    assert db.run(prefix, apply=True, connect=connect)['status'] == 'ALREADY_APPLIED'
    assert len(state.statements) == 1


@pytest.mark.parametrize('damage', ['extra', 'wrong-type', 'wrong-server', 'wrong-database', 'public-config'])
def test_preflight_rejects_unrecognized_target_before_ddl(database, monkeypatch, damage):
    prefix, state, connect = database
    if damage == 'extra': state.extra = True
    elif damage == 'wrong-type': state.reply = True; state.kind = 'varchar'
    elif damage == 'wrong-server':
        monkeypatch.setattr(db, 'verify_server', lambda _: (_ for _ in ()).throw(ValueError('not OceanBase')))
    elif damage == 'wrong-database':
        path = prefix / 'config/xunjia.env'
        path.write_text(path.read_text().replace('/xunjia_business', '/other'))
    else: (prefix / 'config/xunjia.env').chmod(0o644)
    with pytest.raises(Exception): db.run(prefix, apply=True, connect=connect)
    assert not state.statements
    assert not (prefix / 'deployments/reply-migration/journal.json').exists()


def test_automatic_builder_requires_runtime_and_embeds_exact_baseline_files(change):
    old, new, output = change
    with pytest.raises(builder.runtime.IncrementalError, match='IMPLEMENTATION_REQUIRED'):
        builder.build([old], new, output, automatic_reply_migration=True)
    write(new, 'deploy/incremental_database.py', Path(db.__file__).read_bytes())
    seal(new, 'target-release')
    builder.build([old], new, output, automatic_reply_migration=True)
    capsule = load(output)
    assert capsule.CAPSULE['automatic_reply_migration'] is True
    assert capsule.CAPSULE['optional_database_migrations'][0]['automatic'] is True
    expected = capsule.CAPSULE['installed_files']['base-one']
    assert expected['server/app.py'] == builder.inspect(old)[1]['app/server/app.py']
    prefix = output.parent / 'installed'
    for name in expected:
        source = old / ('app/' + name) if ('app/' + name) in builder.inspect(old)[1] else old / name
        write(prefix, 'app/' + name, source.read_bytes())
    capsule.verify_installed_files(prefix, 'base-one')
    (prefix / 'app/server/app.py').write_text('unreviewed local patch')
    with pytest.raises(capsule.IncrementalError, match='INSTALLED_FILES'):
        capsule.verify_installed_files(prefix, 'base-one')


@pytest.mark.parametrize('failure', [False, True])
def test_database_gate_precedes_install_and_service_is_restored_on_failure(change, failure):
    old, new, output = change
    write(new, 'deploy/incremental_database.py', Path(db.__file__).read_bytes())
    seal(new, 'target-release')
    builder.build([old], new, output, automatic_reply_migration=True)
    capsule = load(output)
    case = SimpleNamespace(module=capsule, old=old, root=output.parent)
    bundle, prefix, nginx = installed(case)
    for name in capsule.CAPSULE['installed_files']['base-one']:
        source = old / ('app/' + name) if ('app/' + name) in builder.inspect(old)[1] else old / name
        write(prefix, 'app/' + name, source.read_bytes())
    ctl = FakeManager(bundle, prefix)
    ctl.lock = nullcontext
    ctl.ensure_idle = lambda: None
    ctl.validate_release = lambda _: {'build_id': 'base-one'}
    ctl.verify_installed = lambda **_: ctl.calls.append('verify-live')
    running = [True]
    def service(action):
        ctl.calls.append(action)
        running[0] = action == 'start'
    ctl.service = SimpleNamespace(active=lambda: running[0], validate=lambda: None,
                                  stop=lambda: service('stop'), start=lambda: service('start'))
    def database(_, *, apply):
        if not apply: return {'status': 'MIGRATION_REQUIRED'}
        assert running == [False]
        ctl.calls.append('ddl')
        if failure: raise db.MigrationError('SYNTHETIC_DDL_FAILURE')
        return {'ddl_executed': True}
    kwargs = dict(apply=True, manager_factory=lambda *_: ctl, mount_function=lambda: {},
                  nginx_config=nginx, database_function=database)
    if failure:
        with pytest.raises(db.MigrationError, match='SYNTHETIC_DDL_FAILURE'):
            capsule.deploy(bundle, prefix, **kwargs)
        assert ctl.calls == ['preflight', 'verify-live', 'stop', 'ddl', 'start']
    else:
        assert capsule.deploy(bundle, prefix, **kwargs)['database_ddl_executed']
        assert ctl.calls == ['preflight', 'verify-live', 'stop', 'ddl', 'start', 'install']
    assert running == [True]


def test_discovery_prefers_running_version_and_accepts_identical_duplicate_bundles(change, monkeypatch):
    old, new, output = change
    builder.build([old], new, output, allow_optional_reply_migration=True)
    capsule = load(output)
    prefix = output.parent / 'installed'
    write(prefix, 'app/VERSION.json', json.dumps({'build_id': 'base-one'}))
    import shutil
    duplicate = output.parent / 'duplicate'
    shutil.copytree(old, duplicate)
    monkeypatch.setattr(capsule, 'bundle_candidates', lambda _: [duplicate, old])
    assert capsule.discover_base(prefix) == duplicate
    (duplicate / 'app/server/app.py').write_text('local corruption')
    assert capsule.discover_base(prefix) == old


def test_rerun_at_target_selects_an_intact_base_among_different_old_versions(case, monkeypatch):
    prefix = case.root / 'installed'
    write(prefix, 'app/VERSION.json', json.dumps({'build_id': 'target-release'}))
    monkeypatch.setattr(case.module, 'bundle_candidates', lambda _: [case.old, case.other])
    assert case.module.discover_base(prefix) == case.old
    (case.old / 'app/server/app.py').write_text('local corruption')
    assert case.module.discover_base(prefix) == case.other
    target = case.module.materialize(case.other)
    assert case.module.verify_tree(target, 'target-release', case.module.CAPSULE['manifest_sha256'])
    (prefix / 'app/VERSION.json').write_text(json.dumps({'build_id': 'unrecognized'}))
    with pytest.raises(case.module.IncrementalError, match='INSTALLED_BUILD_NOT_SUPPORTED'):
        case.module.discover_base(prefix)
