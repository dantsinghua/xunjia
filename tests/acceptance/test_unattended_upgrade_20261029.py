"""Independent offline acceptance of the reviewed 1027 -> 1029 upgrade.

Synthetic service/database seams never constitute customer-site acceptance.
"""
from pathlib import Path
import json
from contextlib import contextmanager

import pytest

from deploy import build_incremental as builder
from tests.release.test_incremental_capsule import sample, seal, write, load

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def unattended(tmp_path):
    old = sample(tmp_path / 'old', 'reviewed-old', 'old source\n')
    new = sample(tmp_path / 'new', 'reviewed-new', 'new source\n')
    current_sql = (ROOT / 'sql/oceanbase/001_xunjia_schema_mysql.sql').read_text()
    current_generator = (ROOT / 'server/negotiation/schema.py').read_text()
    for root in (old, new):
        write(root, 'sql/oceanbase/001_xunjia_schema_mysql.sql', current_sql)
        write(root, 'app/server/negotiation/schema.py', current_generator)
    write(old, 'sql/oceanbase/001_xunjia_schema_mysql.sql',
          current_sql.replace(', reply_json LONGTEXT,', ',', 1))
    write(old, 'app/server/negotiation/schema.py',
          current_generator.replace(', reply_json {text},', ',', 1))
    write(new, 'sql/oceanbase/004_message_reply.sql',
          (ROOT / 'sql/oceanbase/004_message_reply.sql').read_bytes())
    write(new, 'deploy/incremental_database.py', (ROOT / 'deploy/incremental_database.py').read_bytes())
    seal(old, 'reviewed-old')
    seal(new, 'reviewed-new')
    return old, new, tmp_path / 'upgrade.py'


def test_unattended_capsule_binds_installed_files_to_each_reviewed_version(unattended):
    old, new, script = unattended
    builder.build([old], new, script, automatic_reply_migration=True)
    capsule = load(script)
    assert capsule.CAPSULE['automatic_reply_migration'] is True
    assert capsule.CAPSULE['optional_database_migrations'][0]['automatic'] is True
    expected = capsule.CAPSULE['installed_files']
    assert set(expected) == {'reviewed-old', 'reviewed-new'}
    assert expected['reviewed-old']['server/app.py'] != expected['reviewed-new']['server/app.py']
    assert 'config/xunjia.env' not in expected['reviewed-old']
    assert 'synthetic_private_never_ship' not in script.read_text()


@pytest.mark.parametrize('damage', ['destructive_sql', 'required_column', 'unrelated_table', 'missing_runtime'])
def test_unattended_builder_refuses_unreviewed_database_work(unattended, damage):
    old, new, script = unattended
    sql = new / 'sql/oceanbase/001_xunjia_schema_mysql.sql'
    if damage == 'destructive_sql':
        with (new / 'sql/oceanbase/004_message_reply.sql').open('a') as stream:
            stream.write('\nDELETE FROM xj2_message;\n')
    elif damage == 'required_column':
        sql.write_text(sql.read_text().replace('reply_json LONGTEXT,', 'reply_json LONGTEXT NOT NULL,'))
    elif damage == 'unrelated_table':
        with sql.open('a') as stream:
            stream.write('\nCREATE TABLE extra_table (id INT);\n')
    else:
        (new / 'deploy/incremental_database.py').unlink()
    seal(new, 'reviewed-new')
    with pytest.raises(builder.runtime.IncrementalError):
        builder.build([old], new, script, automatic_reply_migration=True)
    assert not script.exists()


def test_materialized_target_cannot_reuse_tampered_runtime(unattended):
    old, new, script = unattended
    builder.build([old], new, script, automatic_reply_migration=True)
    capsule = load(script)
    (old / 'runtime/python-runtime-aarch64.tar.gz').write_bytes(b'tampered runtime')
    with pytest.raises(capsule.IncrementalError):
        capsule.materialize(old)
    assert not (old.parent / 'xunjia-update-reviewed-new').exists()


def installed_case(unattended):
    old, new, script = unattended
    builder.build([old], new, script, automatic_reply_migration=True)
    capsule = load(script)
    bundle = capsule.materialize(old)
    prefix = old.parent / 'installed'
    for name in capsule.CAPSULE['installed_files']['reviewed-old']:
        source = old / ('app/' + name)
        if not source.is_file():
            source = old / name
        write(prefix, 'app/' + name, source.read_bytes())
    write(prefix, 'config/xunjia.env', 'SYNTHETIC_CONFIG=preserve\n')
    write(prefix, 'state/history', 'historical messages remain')
    config = old.parent / 'nginx'
    write(config, 'nginx.conf', f'events {{}}\nhttp {{\n include {config}/conf.d/*.conf;\n}}\n')
    write(config, 'conf.d/njcb.conf', 'server {\n listen 80;\n location /api/ { proxy_pass http://127.0.0.1:8000; }\n}\n')
    return capsule, bundle, prefix, config


class Service:
    def __init__(self, events, stop_error=False):
        self.events, self.running, self.stop_error = events, True, stop_error

    def active(self):
        return self.running

    def validate(self):
        pass

    def stop(self):
        self.events.append('stop')
        self.running = False
        if self.stop_error:
            raise RuntimeError('stop response lost after service stopped')

    def start(self):
        self.events.append('start')
        self.running = True


class Controller:
    def __init__(self, prefix, events, stop_error=False):
        self.prefix, self.events = prefix, events
        self.service = Service(events, stop_error)

    def preflight(self):
        return {}

    def validate_release(self, current):
        return {'build_id': json.loads((self.prefix / 'app/VERSION.json').read_text())['build_id']}

    def verify_installed(self, *, liveness=False):
        return {'status': 'liveness_only' if liveness else 'business_ready'}

    @contextmanager
    def lock(self):
        yield

    def ensure_idle(self):
        pass

    def install(self):
        self.events.append('install')
        return {'release_id': 'owned-target'}

    def current(self):
        return self.prefix / 'releases/owned-target'

    def rollback(self):
        self.events.append('rollback')


@pytest.mark.parametrize('damage', ['changed', 'extra', 'missing'])
def test_installed_code_drift_blocks_before_database_or_service(unattended, damage):
    capsule, bundle, prefix, config = installed_case(unattended)
    if damage == 'changed':
        (prefix / 'app/server/app.py').write_text('modified live code')
    elif damage == 'extra':
        write(prefix, 'app/server/extra.py', 'unknown executable code')
    else:
        (prefix / 'app/server/schema.py').unlink()
    events = []
    with pytest.raises(capsule.IncrementalError, match='INSTALLED_FILES'):
        capsule.deploy(bundle, prefix, apply=True, nginx_config=config,
                       manager_factory=lambda *_: events.append('manager'),
                       database_function=lambda *a, **k: events.append('database'))
    assert events == []


def test_database_failure_recovers_original_service_without_switch(unattended):
    capsule, bundle, prefix, config = installed_case(unattended)
    events = []
    ctl = Controller(prefix, events)
    def migrate(*_, apply):
        events.append('apply-db' if apply else 'check-db')
        if apply:
            assert not ctl.service.active()
            raise RuntimeError('synthetic DDL failure')
        return {'ddl_executed': False, 'status': 'MIGRATION_REQUIRED'}
    with pytest.raises(RuntimeError, match='DDL failure'):
        capsule.deploy(bundle, prefix, apply=True, nginx_config=config,
                       manager_factory=lambda *_: ctl, database_function=migrate)
    assert events == ['check-db', 'check-db', 'stop', 'apply-db', 'start']
    assert ctl.service.active()
    assert (prefix / 'state/history').read_text() == 'historical messages remain'


def test_failed_stop_response_recovers_original_service(unattended):
    capsule, bundle, prefix, config = installed_case(unattended)
    events = []
    ctl = Controller(prefix, events, stop_error=True)
    def migrate(*_, apply):
        assert not apply, 'DDL must not execute after uncertain stop'
        return {'ddl_executed': False, 'status': 'MIGRATION_REQUIRED'}
    with pytest.raises(RuntimeError, match='stop response lost'):
        capsule.deploy(bundle, prefix, apply=True, nginx_config=config,
                       manager_factory=lambda *_: ctl, database_function=migrate)
    assert ctl.service.active(), 'Failed stop must not leave the previously running service down'
    assert 'install' not in events


def test_same_version_and_applied_database_do_not_restart_or_reinstall(unattended):
    capsule, bundle, prefix, config = installed_case(unattended)
    for name in capsule.CAPSULE['installed_files']['reviewed-new']:
        source = bundle / ('app/' + name)
        if not source.is_file():
            source = bundle / name
        write(prefix, 'app/' + name, source.read_bytes())
    events = []
    ctl = Controller(prefix, events)
    report = capsule.deploy(bundle, prefix, apply=True, nginx_config=config,
                            manager_factory=lambda *_: ctl, mount_function=lambda: {},
                            database_function=lambda *a, **k: {'ddl_executed': False, 'status': 'ALREADY_APPLIED'})
    assert report['already_installed'] is True
    assert events == []


def test_release_metadata_disagreement_blocks_before_database(unattended):
    capsule, bundle, prefix, config = installed_case(unattended)
    ctl = Controller(prefix, [])
    ctl.validate_release = lambda _: {'build_id': 'unreviewed-running-release'}
    def no_database(*a, **k):
        pytest.fail('Must not connect to DB before release identity is established')
    with pytest.raises(capsule.IncrementalError, match='METADATA_MISMATCH'):
        capsule.deploy(bundle, prefix, apply=True, nginx_config=config,
                       manager_factory=lambda *_: ctl, database_function=no_database)
    assert ctl.service.active()


class Database:
    """Protocol fixture: only the reviewed SQL is accepted; no socket is opened."""
    def __init__(self):
        self.reply = False
        self.denied = False
        self.selected = None
        self.ddls = []
        self.closed = 0
        self.bad_column = False

    def select_db(self, name):
        self.selected = name

    def close(self):
        self.closed += 1

    def cursor(self):
        database = self
        class Cursor:
            def execute(self, sql, args=()):
                if sql == 'SELECT VERSION() AS version':
                    self.rows = [{'version': 'OceanBase synthetic 4'}]
                elif sql == "SHOW VARIABLES LIKE 'ob_compatibility_mode'":
                    self.rows = [{'Value': 'MYSQL'}]
                elif sql == 'SELECT DATABASE() AS database_name':
                    self.rows = [{'database_name': database.selected}]
                elif 'information_schema.TABLES' in sql:
                    assert args == ('xunjia_business', 'xj2_message')
                    self.rows = [{'TABLE_TYPE': 'BASE TABLE'}]
                elif 'information_schema.COLUMNS' in sql:
                    assert args == ('xunjia_business', 'xj2_message')
                    columns = [
                        ('message_id', 'varchar', 'NO', 64), ('thread_id', 'varchar', 'NO', 64),
                        ('event_id', 'varchar', 'NO', 160), ('content_hash', 'varchar', 'NO', 64),
                        ('source_seq', 'bigint', 'NO', None), ('ingest_revision', 'bigint', 'NO', None),
                        ('direction', 'varchar', 'NO', 8), ('kind', 'varchar', 'NO', 16),
                        ('text', 'longtext', 'NO', None), ('native_id', 'varchar', 'YES', 160),
                        ('target_id', 'varchar', 'YES', 64), ('occurred_at', 'double', 'NO', None),
                        ('received_at', 'double', 'NO', None), ('trade_day', 'varchar', 'NO', 10),
                        ('historical', 'int', 'NO', None)]
                    self.rows = [{'COLUMN_NAME': name, 'DATA_TYPE': kind, 'IS_NULLABLE': nullable,
                                  'CHARACTER_MAXIMUM_LENGTH': length, 'COLUMN_DEFAULT': None, 'EXTRA': ''}
                                 for name, kind, nullable, length in columns]
                    if database.bad_column:
                        self.rows[0]['DATA_TYPE'] = 'int'
                    if database.reply:
                        self.rows.append({'COLUMN_NAME': 'reply_json', 'DATA_TYPE': 'longtext',
                                          'IS_NULLABLE': 'YES', 'COLUMN_DEFAULT': None, 'EXTRA': ''})
                elif sql == 'SHOW CREATE TABLE `xunjia_business`.`xj2_message`':
                    self.rows = [{'Table': 'xj2_message', 'Create Table': 'synthetic original schema'}]
                elif sql == 'ALTER TABLE `xunjia_business`.`xj2_message` ADD COLUMN `reply_json` LONGTEXT NULL':
                    database.ddls.append(sql)
                    if database.denied:
                        raise PermissionError('synthetic database password must never leak')
                    database.reply = True
                    self.rows = []
                else:
                    raise AssertionError('Unexpected SQL: ' + sql)

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return self.rows

            def close(self):
                pass
        return Cursor()


@pytest.fixture
def database_case(tmp_path):
    from deploy import incremental_database
    prefix = tmp_path / 'site'
    env = write(prefix, 'config/xunjia.env',
                'XUNJIA_DATABASE_URL=oceanbase://tester:synthetic@127.0.0.1:2881/xunjia_business\n')
    env.chmod(0o600)
    db = Database()
    return incremental_database, prefix, db


def test_actual_migrator_checks_backs_up_applies_once_and_rechecks(database_case):
    module, prefix, db = database_case
    connect = lambda **_: db
    assert module.run(prefix, connect=connect)['status'] == 'MIGRATION_REQUIRED'
    assert not (prefix / 'deployments').exists()
    first = module.run(prefix, apply=True, connect=connect)
    assert first['ddl_executed'] is True and first['column_verified'] is True
    assert len(db.ddls) == 1
    second = module.run(prefix, apply=True, connect=connect)
    assert second['ddl_executed'] is False and second['column_verified'] is True
    assert len(db.ddls) == 1 and db.closed == 3
    folder = prefix / 'deployments/reply-migration'
    assert json.loads((folder / 'schema-before.json').read_text())['Create Table'] == 'synthetic original schema'
    assert json.loads((folder / 'journal.json').read_text())['status'] == 'VERIFIED'
    assert (folder / 'journal.json').stat().st_mode & 0o777 == 0o600


def test_uncertain_ddl_is_not_replayed_and_late_success_can_be_reconciled(database_case):
    module, prefix, db = database_case
    db.denied = True
    connect = lambda **_: db
    with pytest.raises(module.MigrationError, match='OUTCOME_UNKNOWN') as failure:
        module.run(prefix, apply=True, connect=connect)
    assert 'password' not in str(failure.value)
    with pytest.raises(module.MigrationError, match='RECONCILIATION'):
        module.run(prefix, apply=True, connect=connect)
    assert len(db.ddls) == 1
    db.reply = True
    assert module.run(prefix, apply=True, connect=connect)['status'] == 'ALREADY_APPLIED'
    assert len(db.ddls) == 1


def test_connection_permission_failure_cannot_create_migration_or_leak_secret(database_case):
    module, prefix, db = database_case
    def denied(**options):
        raise PermissionError('password=' + options['password'])
    with pytest.raises(module.MigrationError) as failure:
        module.run(prefix, apply=True, connect=denied)
    assert 'synthetic' not in str(failure.value)
    assert not (prefix / 'deployments').exists()
    assert db.ddls == []


def test_existing_column_type_drift_blocks_before_ddl(database_case):
    module, prefix, db = database_case
    db.bad_column = True
    with pytest.raises(module.MigrationError, match='COLUMN_INCOMPATIBLE'):
        module.run(prefix, apply=True, connect=lambda **_: db)
    assert db.ddls == []
    assert not (prefix / 'deployments').exists()
