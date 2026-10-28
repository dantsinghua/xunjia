"""Independent boundary gates for the observed 2026-10-15 nested site layout.

All files and systemctl output are synthetic. No installed service or customer
configuration is read, stopped, moved, or executed by these tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy'))
from deploy import fresh_install as fresh


MARKERS = ('app/app/server/app.py', 'app/.venv/bin/python',
           'app/VERSION.json', 'app/SOURCE-HASHES.json', 'app/deploy/start.sh')


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf8')
    path.chmod(mode)
    return path


def nested(prefix):
    app = write(prefix / MARKERS[0], '# XUNJIA_DATABASE_URL\ndef create_app(): pass\n')
    write(prefix / MARKERS[1], '# synthetic interpreter, never executed\n', 0o755)
    write(prefix / MARKERS[2], json.dumps({'source': 'synthetic-1015-package'}))
    write(prefix / MARKERS[3], json.dumps({'app/server/app.py': hashlib.sha256(app.read_bytes()).hexdigest()}))
    write(prefix / MARKERS[4], '#!/bin/bash\n# synthetic site entry, never executed\n', 0o755)
    (prefix / 'python').mkdir()
    (prefix / 'state').mkdir()
    return prefix


def conventional(prefix):
    write(prefix / 'app/server/app.py', '# XUNJIA_DATABASE_URL\ndef create_app(): pass\n')
    write(prefix / 'venv/bin/python', '# synthetic interpreter\n', 0o755)
    write(prefix / 'app/deploy/start.sh', '#!/bin/bash\nexit 0\n', 0o755)
    return prefix


def snapshot(directory):
    return {str(path.relative_to(directory)): ('link', str(path.readlink())) if path.is_symlink()
            else ('file', hashlib.sha256(path.read_bytes()).hexdigest())
            for path in directory.rglob('*') if path.is_file() or path.is_symlink()}


def test_observed_nested_layout_is_recognized_without_reading_site_state(tmp_path):
    prefix = nested(tmp_path / 'xunjia')
    write(prefix / 'state/keep.dat', 'synthetic saved state')
    before = snapshot(prefix)
    assert fresh.verify_old_prefix(prefix) == fresh.identity(prefix)
    assert snapshot(prefix) == before


def test_nested_venv_link_to_own_python_runtime_is_allowed(tmp_path):
    prefix = nested(tmp_path / 'xunjia')
    runtime = write(prefix / 'python/bin/python3.12', '# synthetic owned runtime\n', 0o755)
    interpreter = prefix / 'app/.venv/bin/python'
    interpreter.unlink()  # Exact synthetic fixture created above.
    interpreter.symlink_to(runtime)
    assert fresh.verify_old_prefix(prefix) == fresh.identity(prefix)


def test_conventional_layout_still_accepted(tmp_path):
    prefix = conventional(tmp_path / 'xunjia')
    assert fresh.verify_old_prefix(prefix) == fresh.identity(prefix)


def test_mixed_nested_and_conventional_application_identity_is_rejected(tmp_path):
    prefix = nested(tmp_path / 'xunjia')
    write(prefix / 'app/server/app.py', '# XUNJIA_DATABASE_URL\ndef create_app(): pass\n')
    with pytest.raises(fresh.DeployError):
        fresh.verify_old_prefix(prefix)


@pytest.mark.parametrize('marker', ['app/VERSION.json', 'app/SOURCE-HASHES.json'])
@pytest.mark.parametrize('content', ['{}', '[]', 'not-json'])
def test_nested_package_metadata_must_be_nonempty_readable_object(tmp_path, marker, content):
    prefix = nested(tmp_path / 'xunjia')
    (prefix / marker).write_text(content)
    with pytest.raises(fresh.DeployError):
        fresh.verify_old_prefix(prefix)


@pytest.mark.parametrize('missing', MARKERS)
def test_nested_layout_missing_identity_component_is_rejected(tmp_path, missing):
    prefix = nested(tmp_path / 'xunjia')
    (prefix / missing).unlink()
    with pytest.raises(fresh.DeployError):
        fresh.verify_old_prefix(prefix)


@pytest.mark.parametrize('escaped', MARKERS)
def test_nested_identity_file_must_resolve_inside_dedicated_prefix(tmp_path, escaped):
    prefix = nested(tmp_path / 'xunjia')
    path = prefix / escaped
    outside = tmp_path / 'outside-file'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    before = outside.read_bytes()
    with pytest.raises(fresh.DeployError):
        fresh.verify_old_prefix(prefix)
    assert outside.read_bytes() == before


@pytest.mark.parametrize('damage', ['unknown-top-level', 'missing-app-markers', 'prefix-symlink', 'mount'])
def test_nested_recognition_preserves_unknown_layout_link_and_mount_guards(tmp_path, monkeypatch, damage):
    prefix = nested(tmp_path / 'xunjia')
    if damage == 'unknown-top-level':
        write(prefix / 'deposit-service/config', 'unrelated synthetic project')
    elif damage == 'missing-app-markers':
        (prefix / MARKERS[0]).write_text('def unrelated_app(): pass\n')
    elif damage == 'prefix-symlink':
        link = tmp_path / 'site-link'
        link.symlink_to(prefix, target_is_directory=True)
        prefix = link
    else:
        def mounted(path):
            raise fresh.DeployError('FRESH_PREFIX_CONTAINS_MOUNT')
        monkeypatch.setattr(fresh, 'reject_mounts', mounted)
    with pytest.raises(fresh.DeployError):
        fresh.verify_old_prefix(prefix)


def test_nanyin_prefix_is_still_rejected_before_package_or_service_access(tmp_path, monkeypatch):
    prefix = nested(tmp_path / 'nanyin-agent')
    manager = SimpleNamespace(prefix=prefix, bundle=tmp_path / 'bundle',
                              service=SimpleNamespace(name='xunjia-agent.service'))
    monkeypatch.setattr(fresh, 'verify_bundle', lambda *_: pytest.fail('Unrelated prefix reached bundle inspection'))
    with pytest.raises(fresh.DeployError, match='OTHER_PROJECT_PREFIX_FORBIDDEN'):
        fresh.plan(manager)


def service_fixture(prefix, monkeypatch, *, executable='/bin/bash', arguments=None,
                    working='', dropins='', state='active', extra_command=''):
    target = str(prefix / 'app/deploy/start.sh')
    arguments = executable + ' ' + target if arguments is None else arguments
    replies = {'ExecStart': f'{{ path={executable}; argv[]={arguments}; ignore_errors=no; }}' + extra_command,
               'WorkingDirectory': working, 'DropInPaths': dropins, 'ActiveState': state}
    calls = []
    def systemctl(*args):
        calls.append(args)
        assert args[0] == 'show'
        assert args[1] == 'xunjia-agent.service'
        field = next(part.partition('=')[2] for part in args if part.startswith('--property='))
        return replies[field]
    monkeypatch.setattr(fresh, 'systemctl', systemctl)
    monkeypatch.setattr(fresh.shutil, 'which', lambda _: '/synthetic/systemctl')
    return SimpleNamespace(prefix=prefix, service=SimpleNamespace(name='xunjia-agent.service', exists=lambda: True)), calls


@pytest.mark.parametrize('shell', ['/bin/bash', '/usr/bin/bash'])
@pytest.mark.parametrize('working', ['', '/', 'app'])
def test_exact_bash_wrapper_accepts_observed_default_working_directory(tmp_path, monkeypatch, shell, working):
    prefix = nested(tmp_path / 'xunjia')
    manager, calls = service_fixture(prefix, monkeypatch, executable=shell,
                                     working=str(prefix / 'app') if working == 'app' else working)
    fresh.verify_service(manager)
    assert calls and all(call[0] == 'show' for call in calls)


def test_bash_wrapper_second_validation_works_after_old_nested_payload_has_moved(tmp_path, monkeypatch):
    prefix = conventional(tmp_path / 'xunjia')
    assert not (prefix / 'app/app').exists()
    manager, _ = service_fixture(prefix, monkeypatch)
    fresh.verify_service(manager)


@pytest.mark.parametrize('kind', ['missing', 'external-link'])
def test_bash_wrapper_cannot_execute_missing_or_outside_prefix_script(tmp_path, monkeypatch, kind):
    prefix = conventional(tmp_path / 'xunjia')
    script = prefix / 'app/deploy/start.sh'
    script.unlink()
    if kind == 'external-link':
        outside = write(tmp_path / 'outside-start.sh', '#!/bin/bash\nexit 0\n')
        script.symlink_to(outside)
    manager, _ = service_fixture(prefix, monkeypatch)
    with pytest.raises(fresh.DeployError):
        fresh.verify_service(manager)


@pytest.mark.parametrize('working', ['', '/'])
def test_direct_script_keeps_existing_strict_working_directory(tmp_path, monkeypatch, working):
    prefix = conventional(tmp_path / 'xunjia')
    script = str(prefix / 'app/deploy/start.sh')
    manager, _ = service_fixture(prefix, monkeypatch, executable=script, arguments=script, working=working)
    with pytest.raises(fresh.DeployError, match='FRESH_SERVICE_DIRECTORY_UNPROVEN'):
        fresh.verify_service(manager)


def test_direct_script_with_dedicated_working_directory_remains_valid(tmp_path, monkeypatch):
    prefix = conventional(tmp_path / 'xunjia')
    script = str(prefix / 'app/deploy/start.sh')
    manager, _ = service_fixture(prefix, monkeypatch, executable=script, arguments=script, working=str(prefix / 'app'))
    fresh.verify_service(manager)


@pytest.mark.parametrize('damage', ['extra-arg', 'shell-command', 'other-shell', 'other-project',
                                   'argv-shell-mismatch', 'working-elsewhere', 'dropin', 'second-command', 'transition'])
def test_wrapper_rejects_scope_expansion_or_unproven_service_state(tmp_path, monkeypatch, damage):
    prefix = nested(tmp_path / 'xunjia')
    script = str(prefix / 'app/deploy/start.sh')
    kwargs = {}
    if damage == 'extra-arg': kwargs['arguments'] = '/bin/bash ' + script + ' --other-mode'
    elif damage == 'shell-command': kwargs['arguments'] = '/bin/bash -c ' + script
    elif damage == 'other-shell': kwargs['executable'] = '/bin/sh'
    elif damage == 'other-project': kwargs['arguments'] = '/bin/bash /home/nanyin-agent/start.sh'
    elif damage == 'argv-shell-mismatch': kwargs['arguments'] = '/usr/bin/bash ' + script
    elif damage == 'working-elsewhere': kwargs['working'] = '/home/nanyin-agent'
    elif damage == 'dropin': kwargs['dropins'] = '/etc/systemd/system/xunjia-agent.service.d/override.conf'
    elif damage == 'second-command': kwargs['extra_command'] = ' { path=/bin/bash; argv[]=/bin/bash /home/other/start.sh; }'
    elif damage == 'transition': kwargs['state'] = 'activating'
    manager, _ = service_fixture(prefix, monkeypatch, **kwargs)
    with pytest.raises(fresh.DeployError):
        fresh.verify_service(manager)
