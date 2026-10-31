"""Independent upgrade recovery acceptance; no real service or network operations.

Release pointers, transaction journals and financial SQLite sentinels are real
temporary files. Only OS service/process and health boundaries are simulated.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sqlite3
import socket
import subprocess
from types import SimpleNamespace

import pytest

from tests.release.test_deployment_contract import (
    CONFIG, FastManager, FakeService, bundle, dm, manifest, write,
)

JOB = '1' * 32
OTHER_JOB = '2' * 32
BASE = 'a' * 64
TARGET = 'b' * 64


@pytest.fixture(autouse=True)
def no_external_operations(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Unexpected external service, process or network operation')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(dm, 'read_health', forbidden)


@pytest.fixture
def recovery():
    return importlib.import_module('deploy.upgrade_recovery')


@pytest.fixture
def supervisor():
    return importlib.import_module('deploy.upgrade_supervisor')


@pytest.fixture
def installed(tmp_path, monkeypatch):
    prefix = tmp_path / 'installed'
    service = FakeService()
    old_bundle = bundle(tmp_path, BASE)
    write(old_bundle / 'app/VERSION.json', json.dumps({'build_id': BASE}))
    manifest(old_bundle)
    manager = FastManager(old_bundle, prefix, service, timeout=0, poll=0)
    manager.install()
    original = manager.current()
    service.running = True
    service.calls.clear()
    history = prefix / 'state/history.db'
    with sqlite3.connect(history) as db:
        db.execute('CREATE TABLE quote_history(id INTEGER PRIMARY KEY, content TEXT NOT NULL)')
        db.execute('INSERT INTO quote_history VALUES(1,?)', ('protected financial history',))
    config_before = (prefix / 'config/xunjia.env').read_bytes()
    calls = []
    health = {'ready': True}

    def healthy(url):
        calls.append(('health', manager.current()))
        if not service.running or not health['ready']:
            raise dm.DeployError('HEALTH_NOT_READY')
        current = manager.current()
        metadata = dm.load_json(current / 'RELEASE.json')
        return {'service': 'xunjia', 'ready': True, 'build_id': metadata['build_id'],
                'release_id': metadata['release_id'], 'boot_id': service.boot,
                'automation_ready': True}

    class ServiceBoundary:
        def __new__(cls, *_a, **_k):
            return service

    original_check_health = dm.Manager.check_health

    def immediate_check(self, *args, **kwargs):
        self.timeout = 0
        self.poll = 0
        return original_check_health(self, *args, **kwargs)

    monkeypatch.setattr(dm, 'read_health', healthy)
    monkeypatch.setattr(dm, 'SystemService', ServiceBoundary)
    monkeypatch.setattr(dm.Manager, 'check_health', immediate_check)
    # Keep release hashing and journal/pointer operations real; the fixture's
    # synthetic interpreter cannot be launched and never accesses customer DBs.
    monkeypatch.setattr(dm.Manager, 'validate_release', FastManager.validate_release)
    case = SimpleNamespace(prefix=prefix, service=service, manager=manager,
                           original=original, history=history, config=config_before,
                           calls=calls, health=health)
    yield case
    assert (prefix / 'config/xunjia.env').read_bytes() == config_before
    with sqlite3.connect(history) as db:
        assert db.execute('SELECT * FROM quote_history').fetchall() == [(1, 'protected financial history')]


def transaction(case, *, owner=JOB, previous=None, phase='STARTING', switched=True,
                touched=True, identity='candidate'):
    target = case.prefix / 'releases' / identity
    write(target / 'app/server/app.py', '# XUNJIA_DATABASE_URL\n# synthetic candidate\n')
    write(target / 'app/VERSION.json', json.dumps({'build_id': TARGET}))
    write(target / 'venv/bin/python', '# synthetic interpreter, never executed\n')
    dm.atomic_json(target / 'RELEASE.json', {
        'build_id': TARGET, 'release_id': identity, 'previous': str(case.original),
        'payload': dm.snapshot_payload(target / 'app'),
    })
    journal = {'id': identity, 'operation': 'install', 'upgrade_job': owner,
               'previous': str(previous or case.original), 'target': str(target),
               'phase': phase, 'switched': switched, 'service_touched': touched,
               'active_before': True}
    path = case.manager.txn_path(identity)
    dm.atomic_json(path, journal)
    if switched:
        case.manager.set_current(target)
    return target, path, journal


def assert_old_ready(case, result):
    assert result['rollback'] in ('restored', 'not_needed')
    assert result['health'] == 'ready'
    assert result['previous_build'] == BASE
    assert case.manager.current() == case.original
    assert case.service.running
    assert any(call[0] == 'health' and call[1] == case.original for call in case.calls), \
        'Recovery success requires a fresh old-release health response'


def test_no_installer_transaction_does_not_restart_healthy_old_service(recovery, installed):
    baseline = recovery.snapshot_baseline(installed.prefix)
    installed.calls.clear()
    result = recovery.recover_job(installed.prefix, JOB, baseline)
    assert_old_ready(installed, result)
    assert result['rollback'] == 'not_needed'
    assert 'stop' not in installed.service.calls
    assert 'start' not in installed.service.calls


def test_preparation_failure_preserves_old_pointer_and_service(recovery, installed):
    baseline = recovery.snapshot_baseline(installed.prefix)
    target, path, _ = transaction(installed, phase='PREPARING', switched=False, touched=False)
    result = recovery.recover_job(installed.prefix, JOB, baseline)
    assert_old_ready(installed, result)
    assert 'stop' not in installed.service.calls
    assert 'start' not in installed.service.calls
    assert json.loads(path.read_text())['phase'] in dm.TERMINAL
    assert not target.exists()


@pytest.mark.parametrize('phase', ['SWITCHING', 'STARTING', 'RECOVERY_REQUIRED'])
def test_owned_switched_failure_restores_exact_baseline_and_checks_health(recovery, installed, phase):
    baseline = recovery.snapshot_baseline(installed.prefix)
    target, path, _ = transaction(installed, phase=phase)
    installed.service.running = False
    installed.calls.clear()
    result = recovery.recover_job(installed.prefix, JOB, baseline)
    assert_old_ready(installed, result)
    assert result['rollback'] == 'restored'
    assert json.loads(path.read_text())['phase'] in dm.TERMINAL
    assert not target.exists()


def test_installer_already_restored_old_release_still_verifies_health(recovery, installed):
    baseline = recovery.snapshot_baseline(installed.prefix)
    _, path, txn = transaction(installed)
    installed.manager.restore(txn)
    installed.calls.clear()
    installed.service.calls.clear()
    result = recovery.recover_job(installed.prefix, JOB, baseline)
    assert_old_ready(installed, result)
    assert result['rollback'] == 'restored'
    assert json.loads(path.read_text())['phase'] in dm.TERMINAL
    assert 'stop' not in installed.service.calls


@pytest.mark.parametrize('owner', [OTHER_JOB, None, ''])
def test_foreign_or_untagged_transaction_never_changes_pointer_or_journal(recovery, installed, owner):
    baseline = recovery.snapshot_baseline(installed.prefix)
    target, path, _ = transaction(installed, owner=owner)
    before = path.read_bytes()
    installed.service.calls.clear()
    with pytest.raises(Exception):
        recovery.recover_job(installed.prefix, JOB, baseline)
    assert installed.manager.current() == target
    assert path.read_bytes() == before
    assert not {'stop', 'start'}.intersection(installed.service.calls)


def test_matching_job_with_different_previous_release_is_not_owned(recovery, installed):
    baseline = recovery.snapshot_baseline(installed.prefix)
    target, path, _ = transaction(installed, previous=installed.prefix / 'releases/unrelated')
    before = path.read_bytes()
    installed.service.calls.clear()
    with pytest.raises(Exception):
        recovery.recover_job(installed.prefix, JOB, baseline)
    assert installed.manager.current() == target
    assert path.read_bytes() == before
    assert not {'stop', 'start'}.intersection(installed.service.calls)


@pytest.mark.parametrize('failure', ['health', 'start', 'damaged_previous'])
def test_failed_recovery_never_reports_restored_ready(recovery, installed, failure):
    baseline = recovery.snapshot_baseline(installed.prefix)
    _, path, _ = transaction(installed)
    if failure == 'health':
        installed.health['ready'] = False
    elif failure == 'start':
        installed.service.fail_start = 10
    else:
        write(installed.original / 'app/server/app.py', '# changed original release\n')
    with pytest.raises(Exception):
        recovery.recover_job(installed.prefix, JOB, baseline)
    assert path.exists(), 'A failed recovery must retain its transaction for diagnosis'


def test_deploy_lock_prevents_recovery_from_racing_an_installer(recovery, installed):
    baseline = recovery.snapshot_baseline(installed.prefix)
    target, path, _ = transaction(installed)
    before = path.read_bytes()
    with installed.manager.lock():
        with pytest.raises(Exception):
            recovery.recover_job(installed.prefix, JOB, baseline)
    assert installed.manager.current() == target
    assert path.read_bytes() == before


class SystemdBoundary:
    """A unit state machine, deliberately independent of supervisor internals."""
    def __init__(self, *, completed=False, exit_code=0, start_failure=False,
                 stop_failure=False, child_remains=False):
        self.completed, self.exit_code = completed, exit_code
        self.start_failure, self.stop_failure = start_failure, stop_failure
        self.child_remains = child_remains
        self.running = False
        self.stopped = False
        self.calls = []

    def __call__(self, command, **kwargs):
        argv = [str(part) for part in command]
        self.calls.append(argv)
        assert not kwargs.get('shell'), 'Supervisor must never execute shell text'
        assert any(JOB in part for part in argv), 'Every service command must name this exact upgrade job'
        name = Path(argv[0]).name
        if name == 'systemd-run':
            assert 'KillMode=control-group' in ' '.join(argv)
            self.running = not self.start_failure and not self.completed
            return SimpleNamespace(returncode=1 if self.start_failure else 0,
                                   stdout='', stderr='synthetic start failure' if self.start_failure else '')
        assert name == 'systemctl', 'Unexpected process outside the systemd boundary'
        if 'stop' in argv or 'kill' in argv:
            if not self.stop_failure and not self.child_remains:
                self.running = False
                self.stopped = True
            return SimpleNamespace(returncode=1 if self.stop_failure else 0, stdout='', stderr='')
        if 'show' in argv:
            properties = {
                'LoadState': 'not-found' if self.start_failure else 'loaded',
                'ActiveState': 'active' if self.running else ('failed' if self.exit_code else 'inactive'),
                'SubState': 'running' if self.running else 'dead',
                'MainPID': '32123' if self.running else '0',
                'ControlPID': '0', 'ExecMainPID': '32123',
                'ExecMainCode': '1', 'ExecMainStatus': str(self.exit_code),
                'Result': 'exit-code' if self.exit_code else 'success',
            }
            text = '\n'.join(f'{key}={value}' for key, value in properties.items()) + '\n'
            return SimpleNamespace(returncode=0, stdout=text, stderr='')
        if 'reset-failed' in argv:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        pytest.fail('Unexpected systemctl operation: ' + repr(argv))


def run_installer(supervisor, installed, monkeypatch, boundary, *, timeout=0):
    wrapper = installed.prefix / 'state/upgrade/jobs' / JOB / 'apply.py'
    write(wrapper, '# synthetic apply wrapper; never executed\n', 0o600)
    monkeypatch.setattr(subprocess, 'run', boundary)
    monkeypatch.setattr(supervisor.time, 'sleep', lambda *_: None)
    ticks = iter(range(10000))
    monkeypatch.setattr(supervisor.time, 'monotonic', lambda: next(ticks))
    return supervisor.run_install(installed.prefix, JOB,
                                  installed.original / 'venv/bin/python', wrapper, timeout=timeout)


def test_independent_installer_success_returns_confirmed_native_exit(supervisor, installed, monkeypatch):
    boundary = SystemdBoundary(completed=True)
    assert run_installer(supervisor, installed, monkeypatch, boundary) == 0
    assert any('show' in call for call in boundary.calls)
    assert installed.manager.current() == installed.original
    assert not {'stop', 'start'}.intersection(installed.service.calls)


def test_independent_installer_failure_preserves_nonzero_exit(supervisor, installed, monkeypatch):
    boundary = SystemdBoundary(completed=True, exit_code=17)
    assert run_installer(supervisor, installed, monkeypatch, boundary) == 17
    assert not {'stop', 'start'}.intersection(installed.service.calls)


def test_apply_unit_start_failure_never_touches_old_service(supervisor, installed, monkeypatch):
    boundary = SystemdBoundary(start_failure=True)
    try:
        result = run_installer(supervisor, installed, monkeypatch, boundary)
    except (RuntimeError, ValueError, OSError):
        pass
    else:
        assert result != 0
    assert installed.manager.current() == installed.original
    assert installed.service.running
    assert not {'stop', 'start'}.intersection(installed.service.calls)


def test_timeout_returns_failure_only_after_observing_stopped_installer(supervisor, installed, monkeypatch):
    boundary = SystemdBoundary()
    result = run_installer(supervisor, installed, monkeypatch, boundary)
    assert result != 0
    assert boundary.stopped and not boundary.running
    stop_at = next(i for i, call in enumerate(boundary.calls) if 'stop' in call or 'kill' in call)
    assert any('show' in call for call in boundary.calls[stop_at + 1:]), \
        'A successful stop command alone is not evidence that installation has stopped'


@pytest.mark.parametrize('failure', ['stop_rejected', 'still_running'])
def test_unconfirmed_installer_stop_blocks_recovery(supervisor, installed, monkeypatch, failure):
    boundary = SystemdBoundary(stop_failure=failure == 'stop_rejected',
                               child_remains=failure == 'still_running')
    with pytest.raises(supervisor.InstallerNotStopped):
        run_installer(supervisor, installed, monkeypatch, boundary)
    assert boundary.running
    assert installed.manager.current() == installed.original
    assert not {'stop', 'start'}.intersection(installed.service.calls)
