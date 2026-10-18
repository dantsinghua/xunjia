"""A zero systemctl exit in a chroot is not proof that a loaded unit is active."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'deploy'))
import manager as dm
from test_deployment_contract import FastManager, bundle


class SystemctlBoundary:
    """Only the OS boundary is synthetic; use the real service/transaction logic."""
    def __init__(self, prefix, *, load_state='loaded\n', load_exit=0, active=True):
        self.prefix = prefix
        self.load_state, self.load_exit = load_state, load_exit
        self.running = active
        self.calls, self.generation = [], 0

    def run(self, command, **kwargs):
        assert command[0] == 'systemctl', command
        self.calls.append(list(command))
        action = command[1]
        stdout, stderr, code = '', '', 0
        if action == 'show':
            if '--property=LoadState' in command:
                stdout, code = self.load_state, self.load_exit
                if not stdout:
                    stderr = 'Running in chroot, ignoring request: show\n'
            elif '--property=ActiveState' in command:
                stdout = 'active\n' if self.running else 'inactive\n'
            elif '--property=ExecStart' in command:
                stdout = str(self.prefix/'app/deploy/start.sh')+'\n'
            elif '--property=MainPID' in command:
                stdout = '4242\n'
            else:
                raise AssertionError(command)
        elif action == 'is-active':
            code = 0 if self.running else 3
            if self.load_state != 'loaded\n':
                # Match the real systemd 243 chroot response, including exit 0.
                code, stderr = 0, 'Running in chroot, ignoring request: is-active\n'
        elif action == 'stop':
            self.running = False
        elif action == 'start':
            self.running = True
            self.generation += 1
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    @property
    def mutations(self):
        return [command[1] for command in self.calls if command[1] in ('start', 'stop')]


def system_service(monkeypatch, prefix, *, load_state='loaded\n', load_exit=0, active=True, available=True):
    boundary = SystemctlBoundary(prefix, load_state=load_state, load_exit=load_exit, active=active)
    original_which = dm.shutil.which
    monkeypatch.setattr(dm.shutil, 'which', lambda name: ('/synthetic/systemctl' if available else None)
                        if name == 'systemctl' else original_which(name))
    monkeypatch.setattr(dm.subprocess, 'run', boundary.run)
    return dm.SystemService('xunjia-agent-presence-test.service', prefix), boundary


@pytest.mark.parametrize('load_state,load_exit', [('', 0), ('not-found\n', 0), ('masked\n', 0), ('loaded\n', 1)])
def test_nonloaded_or_failed_unit_lookup_is_not_active_despite_zero_is_active(monkeypatch, tmp_path, load_state, load_exit):
    service, boundary = system_service(monkeypatch, tmp_path/'site', load_state=load_state, load_exit=load_exit)
    assert service.exists() is False
    assert service.active() is False, 'is-active exit 0 must not override an absent/unloaded unit'
    assert boundary.mutations == []


@pytest.mark.parametrize('load_state', ['', 'not-found\n', 'masked\n'])
def test_first_install_without_a_loaded_unit_never_starts_stops_or_checks_health(monkeypatch, tmp_path, load_state):
    prefix = tmp_path/'site'
    service, boundary = system_service(monkeypatch, prefix, load_state=load_state)
    manager = FastManager(bundle(tmp_path), prefix, service, timeout=0, poll=0)
    health_calls = []
    def unavailable(url):
        health_calls.append(url)
        raise dm.DeployError('HEALTH_UNAVAILABLE')
    monkeypatch.setattr(dm, 'read_health', unavailable)
    result = manager.install()
    assert result['status'] == 'installed_stopped'
    assert manager.current() is not None
    records = [json.loads(path.read_text()) for path in (prefix/'deployments').glob('*.json')]
    assert len(records) == 1
    assert records[0]['phase'] == 'COMMITTED' and records[0]['active_before'] is False
    assert records[0]['previous'] is None and records[0]['service_touched'] is False
    assert boundary.mutations == [] and health_calls == []


@pytest.mark.parametrize('active', [False, True])
def test_loaded_unit_preserves_stopped_install_or_running_restart_and_health(monkeypatch, tmp_path, active):
    prefix = tmp_path/'site'
    service, boundary = system_service(monkeypatch, prefix, active=active)
    manager = FastManager(bundle(tmp_path), prefix, service, timeout=0, poll=0)
    health_calls = []
    def healthy(url):
        health_calls.append(url)
        current = manager.current()
        metadata = json.loads((current/'RELEASE.json').read_text()) if current else {}
        return {'service': 'xunjia', 'ready': True, 'build_id': metadata.get('build_id'),
                'release_id': metadata.get('release_id'), 'pid': 4242,
                'boot_id': 'synthetic-boot-'+str(boundary.generation)}
    monkeypatch.setattr(dm, 'read_health', healthy)
    assert service.active() is active
    result = manager.install()
    assert result['status'] == ('running_verified' if active else 'installed_stopped')
    assert boundary.mutations == (['stop', 'start'] if active else [])
    assert len(health_calls) == (2 if active else 0)
    transaction = json.loads(next((prefix/'deployments').glob('*.json')).read_text())
    assert transaction['phase'] == 'COMMITTED' and transaction['active_before'] is active


def test_no_systemctl_is_inactive_and_install_uses_no_system_command(monkeypatch, tmp_path):
    prefix = tmp_path/'site'
    service, boundary = system_service(monkeypatch, prefix, available=False)
    manager = FastManager(bundle(tmp_path), prefix, service, timeout=0, poll=0)
    assert service.active() is False
    result = manager.install()
    assert result['status'] == 'installed_stopped'
    assert boundary.calls == []
