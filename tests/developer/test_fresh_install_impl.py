"""Synthetic fresh-install control tests; no service or database operations."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy'))
from common import DeployError


def module():
    path = ROOT / 'deploy/fresh_install.py'
    assert path.is_file(), 'Fresh installation controller is missing'
    spec = importlib.util.spec_from_file_location('fresh_install_impl', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class Service:
    name = 'xunjia-agent.service'
    def __init__(self, events):
        self.events = events
        self.running = True
    def stop(self):
        self.events.append('stop')
        self.running = False
    def active(self):
        return self.running
    def exists(self):
        return True
    def start(self):
        self.events.append('start')
        self.running = True


class Manager:
    def __init__(self, root):
        self.bundle = root / 'bundle'
        self.prefix = root / 'xunjia'
        self.events = []
        self.service = Service(self.events)
        (self.bundle / 'config').mkdir(parents=True)
        (self.bundle / 'config/xunjia.env').write_text('fixture')
        (self.bundle / 'config/xunjia.env').chmod(0o600)
        (self.prefix / 'app/server').mkdir(parents=True)
        (self.prefix / 'app/server/app.py').write_text('def create_app():\n    XUNJIA_DATABASE_URL = ""\n')
        (self.prefix / 'venv/bin').mkdir(parents=True)
        (self.prefix / 'venv/bin/python').write_text('old')
        (self.prefix / 'state').mkdir()
        (self.prefix / 'state/old').write_text('old-state')
    @contextmanager
    def lock(self):
        yield
    def install(self):
        self.events.append('install')
        assert not self.service.active()
        assert not self.prefix.exists()
        shutil.copytree(self.bundle / 'config', self.prefix / 'config')
        (self.prefix / 'venv/bin').mkdir(parents=True)
        (self.prefix / 'venv/bin/python').write_text('new')
        (self.prefix / 'app/deploy').mkdir(parents=True)
        return {'status': 'installed_stopped', 'build_id': 'build'}
    def current(self):
        return self.prefix / 'releases/new'
    def check_health(self, release, values):
        self.events.append('ready')
        return {'ready': True, 'pid': 15, 'build_id': 'build'}


def fixture(tmp_path, monkeypatch):
    fresh = module()
    manager = Manager(tmp_path)
    values = {
        'XUNJIA_DATABASE_URL': 'oceanbase://user:password@db:2881/xunjia_business',
        'XUNJIA_HOST': '0.0.0.0', 'XUNJIA_PORT': '18770',
        'XUNJIA_RUN_BACKGROUND': '1', 'XUNJIA_DIFY_URL': 'http://dify/v1',
        'XUNJIA_DIFY_ANALYSIS_APP_KEY': 'analysis', 'XUNJIA_DIFY_REPLY_APP_KEY': 'reply',
        'XUNJIA_AUTH_SECRET': 'a' * 32, 'XUNJIA_SSO_SECRET': 'sso',
        'XUNJIA_AGGREGATION_PROFILE': str(manager.prefix / 'config/profile.json'),
    }
    (manager.bundle / 'config/profile.json').write_text('{}')
    monkeypatch.setattr(fresh, 'verify_bundle', lambda root: {'build_id': 'build'})
    monkeypatch.setattr(fresh, 'read_env', lambda path: dict(values))
    monkeypatch.setattr(fresh, 'verify_service', lambda m: None)
    monkeypatch.setattr(fresh, 'verify_port', lambda m, v: None)
    monkeypatch.setattr(fresh, 'require_system', lambda: None)
    monkeypatch.setattr(fresh, 'install_unit', lambda m: m.events.append('unit'))
    monkeypatch.setattr(fresh, 'enable_service', lambda m: m.events.append('enable'))
    monkeypatch.setattr(fresh, 'runtime_check', lambda m, e: m.events.append('runtime'))
    monkeypatch.setattr(fresh, 'database_step', lambda m, e, apply: m.events.append('db-apply' if apply else 'db-check'))
    return fresh, manager, values


def test_check_is_read_only_and_uses_new_bundle_configuration(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    result = fresh.run_fresh_install(manager)
    assert result['mode'] == 'check'
    assert manager.events == []
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


@pytest.mark.parametrize('key,value', [
    ('XUNJIA_DATABASE_URL', 'oceanbase://user:password@db:2881/njcb'),
    ('XUNJIA_AUTH_SECRET', ''),
    ('XUNJIA_STATE_DIR', '/home/njcb/state'),
])
def test_bad_configuration_fails_before_stop(tmp_path, monkeypatch, key, value):
    fresh, manager, values = fixture(tmp_path, monkeypatch)
    values[key] = value
    with pytest.raises(DeployError):
        fresh.run_fresh_install(manager, apply=True)
    assert manager.events == []
    assert (manager.prefix / 'state/old').exists()


def test_fresh_success_orders_operations_and_removes_only_old_prefix(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    neighbor = tmp_path / 'njcb'
    neighbor.mkdir()
    (neighbor / 'keep').write_text('untouched')
    result = fresh.run_fresh_install(manager, apply=True)
    assert result['status'] == 'fresh_running_verified'
    assert manager.events == ['stop', 'install', 'runtime', 'db-check', 'db-apply', 'unit', 'start', 'ready', 'enable']
    assert not (manager.prefix / 'state/old').exists()
    assert not list(tmp_path.glob('.xunjia-old-*'))
    assert (neighbor / 'keep').read_text() == 'untouched'


def test_database_failure_retains_old_and_new_stopped_without_rollback(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    def fail(m, evidence, apply):
        m.events.append('db-apply' if apply else 'db-check')
        if apply:
            raise DeployError('DATABASE_UNKNOWN')
    monkeypatch.setattr(fresh, 'database_step', fail)
    with pytest.raises(DeployError):
        fresh.run_fresh_install(manager, apply=True)
    assert not manager.service.active()
    assert 'start' not in manager.events
    isolated = list(tmp_path.glob('.xunjia-old-*'))
    assert len(isolated) == 1
    assert (isolated[0] / 'state/old').exists()
    assert (manager.prefix / 'venv/bin/python').read_text() == 'new'


def test_ready_failure_stops_new_service_and_preserves_isolation(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    def fail(*args):
        raise DeployError('HEALTH_WRONG_PROCESS')
    monkeypatch.setattr(manager, 'check_health', fail)
    with pytest.raises(DeployError):
        fresh.run_fresh_install(manager, apply=True)
    assert not manager.service.active()
    assert 'enable' not in manager.events
    assert len(list(tmp_path.glob('.xunjia-old-*'))) == 1


def test_unknown_existing_layout_is_never_moved(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    (manager.prefix / 'another-project').mkdir()
    with pytest.raises(DeployError):
        fresh.run_fresh_install(manager, apply=True)
    assert manager.events == []
    assert (manager.prefix / 'another-project').exists()


def test_new_host_does_not_stop_a_nonexistent_unit(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    manager.service.running = False
    monkeypatch.setattr(manager.service, 'exists', lambda: False)
    def stop_missing():
        raise AssertionError('systemctl stop must not target an absent unit')
    monkeypatch.setattr(manager.service, 'stop', stop_missing)
    result = fresh.run_fresh_install(manager, apply=True)
    assert result['status'] == 'fresh_running_verified'


def test_database_check_and_apply_have_separate_new_evidence_directories(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    calls = []
    monkeypatch.undo()
    monkeypatch.setattr(fresh, 'child_step', lambda *args: calls.append(args))
    evidence = tmp_path / 'evidence'
    fresh.database_step(manager, evidence, False)
    fresh.database_step(manager, evidence, True)
    paths = [arguments[arguments.index('--evidence') + 1] for *_, arguments in calls]
    assert Path(paths[0]).parent == Path(paths[1]).parent
    assert Path(paths[0]).parent.parent == manager.prefix.parent
    assert Path(paths[0]).parent.name.startswith('.xunjia-fresh-database-')
    assert Path(paths[0]).name == 'evidence-database-check'
    assert Path(paths[1]).name == 'evidence-database-apply'


def test_isolated_cleanup_rejects_replaced_directory(tmp_path):
    fresh = module()
    isolated = tmp_path / '.xunjia-old-proof'
    isolated.mkdir()
    old_identity = fresh.identity(isolated)
    isolated.rename(tmp_path / 'original-kept')
    isolated.mkdir()
    (isolated / 'keep').write_text('replacement')
    with pytest.raises(DeployError):
        fresh.remove_isolated(isolated, old_identity, tmp_path)
    assert (isolated / 'keep').read_text() == 'replacement'


def test_empty_target_prefix_does_not_reuse_old_configuration(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    shutil.rmtree(manager.prefix)
    manager.service.running = False
    monkeypatch.setattr(manager.service, 'exists', lambda: False)
    result = fresh.run_fresh_install(manager, apply=True)
    assert result['old_prefix_removed'] is False
    assert (manager.prefix / 'config/xunjia.env').read_text() == 'fixture'


@pytest.mark.parametrize('suffix,dropins', [('-other', ''), ('', '/etc/systemd/system/foreign.conf')])
def test_service_ownership_is_exact_and_rejects_dropins(tmp_path, monkeypatch, suffix, dropins):
    fresh = module()
    manager = Manager(tmp_path)
    executable = str(manager.prefix) + suffix + '/app/deploy/start.sh'
    fields = {'ExecStart': '{ path=' + executable + ' ; argv[]=' + executable + ' ; }',
              'WorkingDirectory': str(manager.prefix / 'app'),
              'DropInPaths': dropins, 'ActiveState': 'inactive'}
    monkeypatch.setattr(fresh.shutil, 'which', lambda name: '/usr/bin/systemctl')
    monkeypatch.setattr(fresh, 'systemctl', lambda *args: fields[args[2].split('=')[1]])
    with pytest.raises(DeployError):
        fresh.verify_service(manager)


def test_database_child_uses_new_interpreter_and_does_not_pass_site_secrets(tmp_path, monkeypatch):
    fresh = module()
    manager = Manager(tmp_path)
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    calls = []
    monkeypatch.setenv('XUNJIA_DATABASE_URL', 'secret-do-not-inherit')
    def completed(command, **kwargs):
        calls.append((command, kwargs))
        return type('Completed', (), {'returncode': 0})()
    monkeypatch.setattr(fresh.subprocess, 'run', completed)
    fresh.database_step(manager, evidence, False)
    command, kwargs = calls[0]
    assert command[0] == str(manager.prefix / 'venv/bin/python')
    assert 'secret-do-not-inherit' not in repr(command)
    assert 'XUNJIA_DATABASE_URL' not in kwargs['env']


def test_second_fresh_run_refuses_prior_unknown_before_stopping(tmp_path, monkeypatch):
    fresh, manager, _ = fixture(tmp_path, monkeypatch)
    def unknown(m, evidence, apply):
        if apply:
            root = fresh.database_audit_root(m.prefix)
            root.mkdir(mode=0o700, exist_ok=True)
            (root / 'database-fresh-fixture.json').write_text('{"status":"UNKNOWN"}')
            raise DeployError('DDL_OUTCOME_UNKNOWN')
    monkeypatch.setattr(fresh, 'database_step', unknown)
    with pytest.raises(DeployError):
        fresh.run_fresh_install(manager, apply=True)
    before = list(manager.events)
    with pytest.raises(DeployError) as caught:
        fresh.run_fresh_install(manager, apply=True)
    assert caught.value.code == 'FRESH_DATABASE_RECONCILIATION_REQUIRED'
    assert manager.events == before
