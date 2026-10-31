"""Installer transactions use an isolated filesystem and synthetic systemd."""
from pathlib import Path
import json
import pytest
from deploy import install_jump_maintenance as install


class Service:
    def __init__(self):
        self.active = False
        self.enabled = False
        self.fail_start = False
        self.host = '172.26.64.36'
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == 'ip':
            return 0, '2: eth0 inet ' + self.host + '/24'
        if argv[0] != 'systemctl':
            return 0, '{}'
        cmd = argv[1]
        if cmd == 'is-active':
            return (0, 'active') if self.active else (3, 'inactive')
        if cmd == 'is-enabled':
            return (0, 'enabled') if self.enabled else (1, 'disabled')
        if cmd == 'start':
            if self.fail_start:
                self.fail_start = False
                return 1, ''
            self.active = True
        if cmd == 'stop':
            self.active = False
        if cmd == 'enable':
            self.enabled = True
        if cmd == 'disable':
            self.enabled = False
        return 0, ''


@pytest.fixture
def case(tmp_path):
    bundle = tmp_path/'bundle'
    bundle.mkdir()
    (bundle/'jump_maintenance.py').write_text('VERSION = 1\n')
    root = tmp_path/'host'
    root.mkdir()
    return bundle, root, Service()


def invoke(case, **kw):
    bundle, root, runner = case
    return install.change(bundle, root=root, runner=runner, python='/usr/bin/python3',
                          readiness=lambda *args: None, **kw)


def test_check_does_not_create_install_or_call_mutation(case):
    assert invoke(case)['status'] == 'CHECK_PASSED_NO_CHANGES'
    assert list(case[1].iterdir()) == []
    assert not any(c[0] == 'systemctl' and c[1] in ('start','stop','enable','disable','daemon-reload') for c in case[2].calls)


def test_install_and_repeat_preserve_persistent_budget(case):
    assert invoke(case, apply=True)['status'] == 'INSTALLED_AND_RUNNING'
    state = case[1]/install.STATE/'budget.json'
    state.write_text('{"attempts":[123]}')
    before = len(case[2].calls)
    assert invoke(case, apply=True)['status'] == 'ALREADY_INSTALLED_AND_RUNNING'
    assert state.read_text() == '{"attempts":[123]}'
    assert not any(c[:2] == ['systemctl','stop'] for c in case[2].calls[before:])


def test_failed_upgrade_restores_bytes_and_activation(case):
    invoke(case, apply=True)
    original = {n:(case[1]/n).read_bytes() for n in (install.PROGRAM, install.UNIT, install.OWNER)}
    (case[0]/'jump_maintenance.py').write_text('VERSION = 2\n')
    case[2].fail_start = True
    with pytest.raises(install.InstallError, match='PREVIOUS_FILES_RESTORED'):
        invoke(case, apply=True)
    assert case[2].active and case[2].enabled
    assert {n:(case[1]/n).read_bytes() for n in original} == original


def test_failed_first_install_removes_only_owned_files(case):
    untouched = case[1]/'customer-config'
    untouched.write_text('keep')
    case[2].fail_start = True
    with pytest.raises(install.InstallError, match='PREVIOUS_FILES_RESTORED'):
        invoke(case, apply=True)
    assert untouched.read_text() == 'keep'
    assert not any((case[1]/n).exists() for n in (install.PROGRAM,install.UNIT,install.OWNER))
    assert not case[2].active and not case[2].enabled


def test_uninstall_preserves_state_and_backups(case):
    invoke(case, apply=True)
    state = case[1]/install.STATE/'status.json'
    state.write_text('preserve evidence')
    assert invoke(case, apply=True, uninstall=True)['status'] == 'UNINSTALLED_STATE_RETAINED'
    assert not case[2].active and not case[2].enabled
    assert state.read_text() == 'preserve evidence'
    assert (case[1]/install.STATE/'install-backups').is_dir()


@pytest.mark.parametrize('damage', ['unowned','changed','symlink','wrong-host'])
def test_refuses_foreign_or_modified_install_before_actions(case, damage):
    if damage == 'wrong-host':
        case[2].host = '127.0.0.1'
    elif damage == 'unowned':
        p = case[1]/install.UNIT
        p.parent.mkdir(parents=True)
        p.write_text('foreign service')
    else:
        invoke(case, apply=True)
        p = case[1]/install.PROGRAM
        if damage == 'changed':
            p.write_text('foreign code')
        else:
            p.unlink()
            p.symlink_to(case[0]/'jump_maintenance.py')
    before = len(case[2].calls)
    with pytest.raises(install.InstallError):
        invoke(case, apply=True)
    assert not any(c[0]=='systemctl' and c[1] in ('start','stop','enable','disable') for c in case[2].calls[before:])
