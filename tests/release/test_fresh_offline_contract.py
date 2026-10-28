"""Independent fresh-deployment boundaries; all database/service actors are synthetic."""
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import database_upgrade as database
from deploy import fresh_install as fresh


DSN = 'oceanbase://synthetic:synthetic-only@127.0.0.1:2881/xunjia_business'


class DatabaseDouble:
    def __init__(self, *, timeout=False):
        self.objects = {f'legacy_{index}': 'BASE TABLE' for index in range(9)}
        self.sql = []
        self.selected = None
        self.closed = False
        self.timeout = timeout

    def cursor(self):
        connection = self
        class Cursor:
            rows = []
            def execute(self, sql, args=()):
                connection.sql.append((sql, args))
                if sql == 'SELECT VERSION() AS version':
                    self.rows = [{'version': 'OceanBase synthetic'}]
                elif sql.startswith('SHOW VARIABLES'):
                    self.rows = [{'Value': 'MYSQL'}]
                elif 'information_schema.SCHEMATA' in sql:
                    self.rows = [{'SCHEMA_NAME': 'xunjia_business'}]
                elif sql == 'SELECT DATABASE() AS selected':
                    self.rows = [{'selected': connection.selected}]
                elif 'information_schema.TABLES' in sql:
                    self.rows = [{'TABLE_NAME': name, 'TABLE_TYPE': kind}
                                 for name, kind in sorted(connection.objects.items())]
                elif sql.startswith('SELECT @@SESSION.FOREIGN_KEY_CHECKS'):
                    self.rows = [{'enabled': 1}]
                elif sql.startswith('DROP TABLE'):
                    if connection.timeout:
                        raise TimeoutError('synthetic-private-dsn-must-not-leak')
                    connection.objects.pop(sql.split('`')[-2], None)
                elif sql.startswith('CREATE TABLE'):
                    connection.objects[sql.split('`')[3]] = 'BASE TABLE'
                elif sql.startswith('SET SESSION') or 'information_schema.' in sql:
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

    def select_db(self, name):
        assert name == 'xunjia_business'
        self.selected = name

    def close(self):
        self.closed = True


@pytest.mark.parametrize('dsn', [DSN.replace('/xunjia_business', '/deposit'),
                                 DSN.replace('/xunjia_business', '/xunjia_other'),
                                 DSN + '?database=deposit', DSN + '#deposit',
                                 DSN.replace('oceanbase:', 'mysql:'),
                                 DSN.replace('/xunjia_business', '/xunjia_business/'),
                                 DSN.replace('synthetic-only', 'REPLACE_DATABASE_PASSWORD')])
def test_other_database_or_ambiguous_dsn_refused_before_connect(tmp_path, dsn):
    def forbidden(**kwargs):
        pytest.fail('Invalid DSN reached database connection')
    with pytest.raises(database.UpgradeError, match='EXACT_XUNJIA_BUSINESS_DSN_REQUIRED'):
        database.run_fresh({'XUNJIA_DATABASE_URL': dsn}, tmp_path / 'evidence', connect=forbidden)
    assert list(tmp_path.iterdir()) == []


def test_database_check_only_reads_target_metadata_and_never_calls_service(tmp_path, monkeypatch):
    connection = DatabaseDouble()
    before = dict(connection.objects)
    monkeypatch.setattr(database, 'require_service_stopped', lambda service: pytest.fail('Read-only check touched service'))
    result = database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, tmp_path / 'check', connect=lambda **_: connection)
    assert result['ddl_executed'] == 0 and result['status'] == 'CHECK_ONLY'
    assert connection.objects == before and connection.closed
    assert all(sql.startswith(('SELECT ', 'SHOW ')) for sql, _ in connection.sql)
    for sql, args in connection.sql:
        if 'information_schema.' in sql:
            assert args and set(args) == {'xunjia_business'}


def test_apply_requires_stopped_service_before_any_database_connection(tmp_path, monkeypatch):
    def active(service):
        raise database.UpgradeError('SERVICE_NOT_STOPPED')
    monkeypatch.setattr(database, 'require_service_stopped', active)
    with pytest.raises(database.UpgradeError, match='SERVICE_NOT_STOPPED'):
        database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, tmp_path / 'apply', apply=True,
                           connect=lambda **_: pytest.fail('Connected while service active'))
    assert list(tmp_path.iterdir()) == []


def test_apply_drops_all_nine_old_tables_and_creates_only_sixteen_xj2(tmp_path, monkeypatch):
    connection = DatabaseDouble()
    stopped = []
    monkeypatch.setattr(database, 'require_service_stopped', stopped.append)
    monkeypatch.setattr(database, 'schema_health', lambda repository: {'ready': True})
    result = database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, tmp_path / 'apply', apply=True,
                               connect=lambda **_: connection)
    assert len(stopped) == 2
    assert result['status'] == 'FRESH_SCHEMA_VERIFIED'
    assert result['history_preserved'] is False
    assert len(database.TABLES) == 16 and set(connection.objects) == set(database.TABLES)
    ddl = [sql for sql, _ in connection.sql if sql.startswith(('DROP ', 'CREATE '))]
    assert len(ddl) == 25
    assert all('`xunjia_business`.' in sql for sql in ddl)
    assert all('deposit' not in sql.lower() for sql, _ in connection.sql)


def test_timeout_persists_unknown_without_retry_and_blocks_new_evidence(tmp_path, monkeypatch):
    connection = DatabaseDouble(timeout=True)
    monkeypatch.setattr(database, 'require_service_stopped', lambda _: None)
    evidence = tmp_path / 'failed'
    with pytest.raises(database.UpgradeError, match='DDL_OUTCOME_UNKNOWN'):
        database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, evidence, apply=True, connect=lambda **_: connection)
    journal = json.loads((evidence / 'ddl-journal.json').read_text())
    assert journal['status'] == 'UNKNOWN' and journal['steps'][0]['status'] == 'UNKNOWN'
    assert len([sql for sql, _ in connection.sql if sql.startswith('DROP ')]) == 1
    assert not any(sql.startswith('CREATE ') for sql, _ in connection.sql)
    assert 'synthetic-private-dsn-must-not-leak' not in ''.join(p.read_text() for p in tmp_path.rglob('*.json'))
    with pytest.raises(database.UpgradeError, match='PREVIOUS_DDL_REQUIRES_READONLY_RECONCILIATION'):
        database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, tmp_path / 'second', apply=True,
                           connect=lambda **_: pytest.fail('UNKNOWN operation was retried'))


def test_same_evidence_path_is_never_replayed(tmp_path):
    evidence = tmp_path / 'same'
    database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, evidence, connect=lambda **_: DatabaseDouble())
    before = {p.name: p.read_bytes() for p in evidence.iterdir()}
    with pytest.raises(database.UpgradeError, match='FRESH_EVIDENCE_DIRECTORY_REQUIRED'):
        database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, evidence,
                           connect=lambda **_: pytest.fail('Repeated evidence reached connection'))
    assert {p.name: p.read_bytes() for p in evidence.iterdir()} == before


@pytest.mark.parametrize('key,value', [('external_references', [{'CONSTRAINT_SCHEMA': 'deposit'}]),
                                     ('routines', ['procedure']), ('events', ['scheduled_event'])])
def test_external_database_dependencies_or_unhandled_objects_fail_closed(key, value):
    before = {'database_exists': True, 'objects': [], 'external_references': [], 'routines': [], 'events': []}
    before[key] = value
    with pytest.raises(database.UpgradeError):
        database.plan_fresh(before)


def test_database_check_and_apply_use_distinct_evidence_and_common_replay_fence(tmp_path, monkeypatch):
    manager = SimpleNamespace(prefix=tmp_path / 'site', service=SimpleNamespace(name='xunjia-agent.service'))
    monkeypatch.setattr(database, 'require_service_stopped', lambda _: None)
    monkeypatch.setattr(database, 'schema_health', lambda _: {'ready': True})
    calls = []
    def child(manager, evidence, name, script, arguments):
        assert script == 'database_upgrade.py'
        target = Path(arguments[arguments.index('--evidence') + 1])
        calls.append(target)
        database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, target, apply='--apply' in arguments,
                           connect=lambda **_: DatabaseDouble())
    monkeypatch.setattr(fresh, 'child_step', child)
    fresh.database_step(manager, tmp_path, False)
    fresh.database_step(manager, tmp_path, True)
    assert calls[0] != calls[1] and calls[0].parent == calls[1].parent


def test_separate_fresh_invocations_share_unknown_database_fence(tmp_path, monkeypatch):
    manager = SimpleNamespace(prefix=tmp_path / 'site', service=SimpleNamespace(name='xunjia-agent.service'))
    monkeypatch.setattr(database, 'require_service_stopped', lambda _: None)
    calls, connected = [], []
    def child(manager, evidence, name, script, arguments):
        target = Path(arguments[arguments.index('--evidence') + 1])
        calls.append(target)
        def connect(**kwargs):
            connected.append(True)
            return DatabaseDouble(timeout=True)
        database.run_fresh({'XUNJIA_DATABASE_URL': DSN}, target, apply=True, connect=connect)
    monkeypatch.setattr(fresh, 'child_step', child)
    with pytest.raises(database.UpgradeError, match='DDL_OUTCOME_UNKNOWN'):
        fresh.database_step(manager, tmp_path / 'first-operation', True)
    with pytest.raises(database.UpgradeError, match='PREVIOUS_DDL_REQUIRES_READONLY_RECONCILIATION'):
        fresh.database_step(manager, tmp_path / 'second-operation', True)
    assert len(connected) == 1
    assert calls[0] != calls[1] and calls[0].parent == calls[1].parent


def test_fresh_check_does_not_stop_install_delete_or_start(tmp_path, monkeypatch):
    prefix = tmp_path / 'site'
    prefix.mkdir()
    state = prefix / 'state.db'
    state.write_bytes(b'old customer state sentinel')
    config = prefix / 'config.env'
    config.write_bytes(b'old customer config sentinel')
    class ForbiddenService:
        name = 'xunjia-agent.service'
        def stop(self): pytest.fail('fresh-check stopped service')
        def start(self): pytest.fail('fresh-check started service')
    manager = SimpleNamespace(prefix=prefix, service=ForbiddenService())
    monkeypatch.setattr(fresh, 'plan', lambda _: ({'build_id': 'synthetic'}, {}, [1, 2], 'fingerprint'))
    monkeypatch.setattr(fresh, 'require_system', lambda: pytest.fail('fresh-check required privileged mutation'))
    monkeypatch.setattr(fresh, 'database_step', lambda *a: pytest.fail('fresh-check executed database mutation'))
    before = {p.name: p.read_bytes() for p in prefix.iterdir()}
    result = fresh.run_fresh_install(manager, apply=False)
    assert result['mode'] == 'check' and result['status'] == 'fresh_plan_valid'
    assert {p.name: p.read_bytes() for p in prefix.iterdir()} == before


def test_fresh_check_runs_actual_config_and_old_prefix_checks_without_mutation(tmp_path, monkeypatch):
    prefix, bundle = tmp_path / 'site', tmp_path / 'bundle'
    for name, data in {'app/server/app.py': '# XUNJIA_DATABASE_URL\ndef create_app(): pass\n',
                       'venv/bin/python': 'synthetic interpreter marker',
                       'config/old.env': 'synthetic old config',
                       'state/old.db': 'synthetic old state'}.items():
        path = prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    (bundle / 'config').mkdir(parents=True)
    config = bundle / 'config/xunjia.env'
    config.write_text(f"XUNJIA_DATABASE_URL='{DSN}'\nXUNJIA_RUN_BACKGROUND='0'\n"
                      "XUNJIA_SSO_SECRET='synthetic-only'\n"
                      "XUNJIA_AUTH_SECRET='synthetic-auth-secret-at-least-thirty-two-bytes'\n")
    config.chmod(0o600)
    manager = SimpleNamespace(prefix=prefix, bundle=bundle,
                              service=SimpleNamespace(name='xunjia-agent.service', exists=lambda: False))
    monkeypatch.setattr(fresh, 'verify_bundle', lambda _: {'build_id': 'synthetic'})
    monkeypatch.setattr(fresh.shutil, 'which', lambda _: '/synthetic/systemctl')
    monkeypatch.setattr(fresh, 'verify_port', lambda *args: None)
    monkeypatch.setattr(fresh, 'systemctl', lambda *args: pytest.fail('Read-only nonexistent service was mutated'))
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = fresh.run_fresh_install(manager, apply=False)
    assert result['old_prefix_present'] is True
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before


@pytest.mark.parametrize('override', ['other-database', 'old-credential-directory',
                                     'old-quote-cache', 'old-dictionary-config'])
def test_fresh_quote_configuration_cannot_reuse_other_database_or_old_credentials(tmp_path, monkeypatch, override):
    prefix, bundle = tmp_path / 'site', tmp_path / 'bundle'
    (bundle / 'config').mkdir(parents=True)
    values = {'XUNJIA_DATABASE_URL': DSN, 'XUNJIA_RUN_BACKGROUND': '0',
              'XUNJIA_QUOTE_SOURCE': 'qb', 'XUNJIA_SSO_SECRET': 'synthetic-only',
              'XUNJIA_AUTH_SECRET': 'synthetic-auth-secret-at-least-thirty-two-bytes'}
    if override == 'other-database': values['QB_MYSQL_DB'] = 'deposit'
    elif override == 'old-credential-directory': values['QB_CONFIG_DIR'] = str(tmp_path / 'old-cookie-directory')
    elif override == 'old-quote-cache': values['XUNJIA_QUOTE_CACHE'] = str(tmp_path / 'old-quotes.db')
    else: values['QB_DICTIONARY_CONFIG'] = str(tmp_path / 'old-dictionary.json')
    config = bundle / 'config/xunjia.env'
    config.write_text(''.join(f"{key}='{value}'\n" for key, value in values.items()))
    config.chmod(0o600)
    manager = SimpleNamespace(prefix=prefix, bundle=bundle,
                              service=SimpleNamespace(name='xunjia-agent.service', exists=lambda: False))
    monkeypatch.setattr(fresh, 'verify_bundle', lambda _: {'build_id': 'synthetic'})
    monkeypatch.setattr(fresh, 'verify_service', lambda *args: None)
    monkeypatch.setattr(fresh, 'verify_port', lambda *args: None)
    with pytest.raises(fresh.DeployError):
        fresh.run_fresh_install(manager, apply=False)
    assert not prefix.exists()


def test_quote_defaults_derive_only_from_new_explicit_env_not_old_process_environment(tmp_path, monkeypatch):
    from deploy.common import effective_env
    monkeypatch.setenv('QB_MYSQL_DB', 'deposit')
    monkeypatch.setenv('QB_CONFIG_DIR', str(tmp_path / 'old-cookie-directory'))
    prefix = tmp_path / 'site'
    values = effective_env({'XUNJIA_QUOTE_SOURCE': 'qb', 'XUNJIA_DATABASE_URL': DSN,
                            'QB_MYSQL_DB': '', 'QB_CONFIG_DIR': ''}, prefix, prefix / 'app')
    assert values['QB_MYSQL_DB'] == 'xunjia_business'
    assert values['QB_CONFIG_DIR'] == str(prefix / 'config/innodealing')
    assert values['QB_MYSQL_USER'] == 'synthetic' and values['QB_MYSQL_PASSWORD'] == 'synthetic-only'


def test_fresh_runtime_step_requires_local_new_site_config_validation(tmp_path, monkeypatch):
    manager = SimpleNamespace(bundle=tmp_path / 'bundle', prefix=tmp_path / 'site')
    calls = []
    monkeypatch.setattr(fresh, 'child_step', lambda *args: calls.append(args))
    fresh.runtime_check(manager, tmp_path / 'evidence')
    assert len(calls) == 1
    assert calls[0][3] == 'runtime_check.py'
    assert '--site-config' in calls[0][4]


@pytest.mark.parametrize('database_timeout', [False, True])
def test_fresh_apply_order_and_no_old_restart_after_uncertain_database(tmp_path, monkeypatch, database_timeout):
    prefix = tmp_path / 'site'
    prefix.mkdir()
    (prefix / 'old-state').write_text('old synthetic state')
    original = fresh.identity(prefix)
    bundle = tmp_path / 'bundle'
    (bundle / 'config').mkdir(parents=True)
    events = []
    class Service:
        name = 'xunjia-agent.service'
        running = True
        def exists(self): return True
        def active(self): return self.running
        def stop(self):
            events.append('stop')
            self.running = False
        def start(self):
            events.append('start')
            self.running = True
    class Manager:
        service = Service()
        def lock(self): return nullcontext()
        def install(self):
            assert not self.service.running
            assert not (self.prefix / 'old-state').exists()
            events.append('install')
            (self.prefix / 'config').mkdir(parents=True)
            (self.prefix / 'new-state').write_text('new synthetic state')
            return {'status': 'installed_stopped', 'release_id': 'new'}
        def current(self): return self.prefix
        def check_health(self, current, values): events.append('health')
    manager = Manager()
    manager.prefix, manager.bundle = prefix, bundle
    monkeypatch.setattr(fresh, 'plan', lambda _: ({'build_id': 'synthetic'}, {}, original, 'fingerprint'))
    monkeypatch.setattr(fresh, 'config_fingerprint', lambda _: 'fingerprint')
    monkeypatch.setattr(fresh, 'require_system', lambda: None)
    monkeypatch.setattr(fresh, 'verify_port', lambda *args: None)
    monkeypatch.setattr(fresh, 'runtime_check', lambda *args: events.append('runtime'))
    monkeypatch.setattr(fresh, 'verify_service', lambda _: events.append('service-identity'))
    def database_step(manager, evidence, apply):
        assert not manager.service.running
        events.append('database-apply' if apply else 'database-check')
        if apply and database_timeout:
            raise fresh.DeployError('FRESH_CHILD_FAILED', 'database-apply')
    monkeypatch.setattr(fresh, 'database_step', database_step)
    monkeypatch.setattr(fresh, 'install_unit', lambda _: events.append('unit'))
    monkeypatch.setattr(fresh, 'enable_service', lambda _: events.append('enable'))
    if database_timeout:
        with pytest.raises(fresh.DeployError, match='FRESH_INSTALL_FAILED'):
            fresh.run_fresh_install(manager, apply=True)
        assert events == ['stop', 'install', 'runtime', 'database-check', 'database-apply', 'stop']
        assert not manager.service.running
        assert (prefix / 'new-state').is_file() and not (prefix / 'old-state').exists()
        retained = list(tmp_path.glob('.xunjia-old-*'))
        assert len(retained) == 1 and (retained[0] / 'old-state').is_file()
        report = json.loads(next(tmp_path.glob('.xunjia-fresh-evidence-*/result.json')).read_text())
        assert report['phase'] == 'FAILED_STOPPED' and report['database_started'] is True
    else:
        result = fresh.run_fresh_install(manager, apply=True)
        assert events == ['stop', 'install', 'runtime', 'database-check', 'database-apply', 'service-identity', 'unit', 'start', 'health', 'enable']
        assert result['old_prefix_removed'] is True and manager.service.running
        assert not list(tmp_path.glob('.xunjia-old-*'))
