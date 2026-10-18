"""A successful ignored systemctl request does not prove a loaded service."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy'))
import manager as deployment


@pytest.mark.parametrize('load_state,active_exit,expected', [
    ('', 0, False),
    ('not-found\n', 0, False),
    ('loaded\n', 0, True),
    ('loaded\n', 3, False),
])
def test_service_activity_requires_a_loaded_unit(tmp_path, monkeypatch, load_state, active_exit, expected):
    calls = []
    monkeypatch.setattr(deployment.shutil, 'which', lambda name: '/usr/bin/systemctl')

    def command(args, **kwargs):
        calls.append(args)
        assert args[0] == 'systemctl'
        if args[1] == 'show':
            return SimpleNamespace(returncode=0, stdout=load_state, stderr='')
        assert args[1] == 'is-active'
        return SimpleNamespace(returncode=active_exit, stdout='', stderr='')

    monkeypatch.setattr(deployment.subprocess, 'run', command)
    service = deployment.SystemService('xunjia-agent-synthetic.service', tmp_path)
    assert service.active() is expected
    if load_state.strip() != 'loaded':
        assert all(args[1] != 'is-active' for args in calls)


def test_missing_systemctl_stays_inactive_without_running_a_command(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment.shutil, 'which', lambda name: None)

    def forbidden(*args, **kwargs):
        raise AssertionError('An unavailable service manager must not be invoked')

    monkeypatch.setattr(deployment.subprocess, 'run', forbidden)
    assert deployment.SystemService('xunjia-agent-synthetic.service', tmp_path).active() is False
