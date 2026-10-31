"""First 5eab page upgrade monitor; real journals/state, isolated OS boundaries."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from server.upgrade_control import UpgradeStore
from tests.acceptance.test_upgrade_recovery_20261030 import (
    BASE, TARGET, JOB, OTHER_JOB, installed, no_external_operations, transaction,
)
from tests.release.test_deployment_contract import FastManager, bundle, manifest, write


@pytest.fixture
def monitor_case(installed, monkeypatch):
    module = importlib.import_module('deploy.upgrade_monitor')
    baseline = module.snapshot_baseline(installed.prefix)
    stage = installed.prefix / 'state/upgrade/jobs' / JOB
    stage.mkdir(parents=True)
    (stage / 'baseline.json').write_text(json.dumps(baseline))
    store = UpgradeStore(installed.prefix / 'state/upgrade')
    with store.db() as db:
        db.execute('INSERT INTO job(id,state,owner,created_at,updated_at) VALUES(?,?,?,?,?)',
                   (JOB, 'installing', 'synthetic-owner', 1, 1))
    monkeypatch.setattr(module, 'verify_bundle', lambda _: {'build_id': TARGET})
    calls = []
    monkeypatch.setattr(module, 'unit_status', lambda unit: {'ActiveState': 'inactive'})
    monkeypatch.setattr(module, 'stop_installer', lambda unit: calls.append(('stop', unit)))
    actual = module.recover_job
    def recover(prefix, identity, snapshot):
        calls.append(('recover', identity))
        assert calls[0] == ('stop', 'xunjia-upgrade-' + JOB + '.service')
        return actual(prefix, identity, snapshot)
    monkeypatch.setattr(module, 'recover_job', recover)
    installed.calls.clear()
    return SimpleNamespace(module=module, case=installed, stage=stage, store=store, calls=calls)


def run_monitor(case):
    case.module.monitor(case.case.prefix, case.case.original, JOB, timeout=0)


def test_first_page_failure_stops_exact_old_worker_then_restores_owned_release(monitor_case):
    case = monitor_case; target, _, _ = transaction(case.case)
    (case.stage / 'capsule-result.json').write_text(json.dumps({'success': False}))
    run_monitor(case)
    job = case.store.latest(); detail = json.loads(job['result'])
    assert job['state'] == 'failed' and job['code'] == 'UPGRADE_ROLLBACK_SUCCEEDED'
    assert detail['rollback'] == 'restored' and detail['health'] == 'ready'
    assert detail['previous_build'] == BASE
    assert case.case.manager.current() == case.case.original != target
    assert any(kind == 'health' and release == case.case.original for kind, release in case.case.calls)
    assert case.calls == [('stop', 'xunjia-upgrade-' + JOB + '.service'), ('recover', JOB)]


def test_successful_first_page_capsule_never_stops_or_rolls_back(monitor_case):
    case = monitor_case; target, _, _ = transaction(case.case, phase='COMMITTED')
    (case.stage / 'capsule-result.json').write_text(json.dumps({'success': True, 'build_id': TARGET}))
    run_monitor(case)
    assert case.calls == []
    assert case.case.manager.current() == target
    assert case.store.latest()['state'] == 'installing', 'The old worker owns its success write'


def test_unconfirmed_old_worker_stop_never_runs_recovery(monitor_case, monkeypatch):
    case = monitor_case; target, _, _ = transaction(case.case)
    def stop(_):raise RuntimeError('INSTALLER_STOP_NOT_CONFIRMED')
    monkeypatch.setattr(case.module, 'stop_installer', stop)
    run_monitor(case)
    assert case.calls == [] and case.case.manager.current() == target
    job = case.store.latest(); detail = json.loads(job['result'])
    assert job['state'] == 'unknown'
    assert detail['rollback'] == 'failed' and detail['health'] == 'unverified'


def test_foreign_transaction_blocks_first_page_recovery_without_pointer_mutation(monitor_case):
    case = monitor_case; target, journal, _ = transaction(case.case, owner=OTHER_JOB)
    before = journal.read_bytes()
    run_monitor(case)
    assert case.store.latest()['state'] == 'unknown'
    assert case.case.manager.current() == target and journal.read_bytes() == before
    assert not any(kind == 'health' for kind, _ in case.case.calls)


def test_failed_old_release_health_never_reports_restored_ready(monitor_case):
    case = monitor_case; transaction(case.case); case.case.health['ready'] = False
    run_monitor(case)
    job = case.store.latest(); detail = json.loads(job['result'])
    assert job['state'] == 'unknown' and detail['health'] == 'unverified'
    assert detail['rollback'] == 'failed'


def test_live_old_worker_timeout_requires_stop_before_recovery(monitor_case, monkeypatch):
    case = monitor_case
    monkeypatch.setattr(case.module, 'unit_status', lambda _: {'ActiveState': 'active'})
    run_monitor(case)
    assert case.calls == [('stop', 'xunjia-upgrade-' + JOB + '.service'), ('recover', JOB)]
    detail = json.loads(case.store.latest()['result'])
    assert detail['rollback'] == 'not_needed' and detail['health'] == 'ready'


def test_arming_saves_verified_baseline_before_launching_independent_monitor(installed, monkeypatch):
    module = importlib.import_module('deploy.upgrade_monitor')
    stage = installed.prefix / 'state/upgrade/jobs' / JOB; stage.mkdir(parents=True)
    calls = []
    def run(command, **options):
        baseline = json.loads((stage / 'baseline.json').read_text())
        assert baseline['release'] == str(installed.original) and baseline['build_id'] == BASE
        assert baseline['active'] is True
        calls.append(command)
        assert command[0] == '/usr/bin/systemd-run'
        assert '--unit=xunjia-upgrade-monitor-' + JOB in command
        assert str(installed.original / 'deploy/upgrade_monitor.py') in command
        assert command[-1] == JOB and '--job' in command
        assert not options.get('shell')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    assert module.arm(installed.prefix, installed.original, JOB) == stage
    assert len(calls) == 1 and installed.manager.current() == installed.original


@pytest.mark.parametrize('identity', ['../foreign', OTHER_JOB + '/extra'])
def test_invalid_task_identity_cannot_arm_or_write_baseline(installed, identity):
    module = importlib.import_module('deploy.upgrade_monitor')
    with pytest.raises(ValueError, match='INVALID_UPGRADE_JOB'):
        module.arm(installed.prefix, installed.original, identity)
    assert not (installed.prefix / 'state/upgrade/jobs').exists()


def test_manager_persists_first_page_job_identity_in_real_install_journal(installed, monkeypatch, tmp_path):
    source = bundle(tmp_path, TARGET)
    write(source / 'app/VERSION.json', json.dumps({'build_id': TARGET})); manifest(source)
    monkeypatch.setenv('XUNJIA_UPGRADE_JOB_ID', JOB)
    manager = FastManager(source, installed.prefix, installed.service, timeout=0, poll=0)
    result = manager.install()
    journal = json.loads(manager.txn_path(result['release_id']).read_text())
    assert journal['upgrade_job'] == JOB and journal['phase'] == 'COMMITTED'
    assert journal['previous'] == str(installed.original)


def test_legacy_capsule_failure_publishes_failed_marker_for_monitor(installed, monkeypatch):
    module = importlib.import_module('deploy.incremental')
    stage = installed.prefix / 'state/upgrade/jobs' / JOB; stage.mkdir(parents=True)
    monkeypatch.setattr(module, '__file__', str(stage / 'upgrade.py'))
    monkeypatch.setattr(module, 'checked_capsule', lambda: None)
    monkeypatch.setattr(module, 'prepare_automatic', lambda _: installed.original)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('XUNJIA_UPGRADE_JOB_ID', '')
    monkeypatch.setitem(sys.modules, 'upgrade_monitor', SimpleNamespace(arm=lambda *_: stage))
    def deploy(*args, **kwargs):
        assert module.os.environ['XUNJIA_UPGRADE_JOB_ID'] == JOB
        raise RuntimeError('synthetic installer failure')
    monkeypatch.setattr(module, 'deploy', deploy)
    monkeypatch.setattr(sys, 'argv', ['upgrade.py', '--prefix', str(installed.prefix), '--apply'])
    with pytest.raises(RuntimeError, match='synthetic installer failure'):module.main()
    assert json.loads((stage / 'capsule-result.json').read_text()) == {'success': False}


@pytest.mark.parametrize('mode', ['legacy', 'new-worker', 'cli', 'check', 'prepare'])
def test_capsule_only_arms_monitor_for_old_page_apply_stage(installed, monkeypatch, mode):
    module = importlib.import_module('deploy.incremental')
    prefix = installed.prefix; stage = prefix / 'state/upgrade/jobs' / JOB; stage.mkdir(parents=True)
    location = stage / 'upgrade.py' if mode != 'cli' else prefix / 'manual' / JOB / 'upgrade.py'
    target = prefix / 'prepared'; target.mkdir()
    arms = []; deployed = []
    def arm(root, bundle, identity):
        arms.append((root, bundle, identity)); return stage
    monkeypatch.setitem(sys.modules, 'upgrade_monitor', SimpleNamespace(arm=arm))
    monkeypatch.setattr(module, '__file__', str(location))
    monkeypatch.setattr(module, 'checked_capsule', lambda: None)
    monkeypatch.setattr(module, 'CAPSULE', {'build_id': TARGET})
    monkeypatch.setattr(module, 'prepare_automatic', lambda _: target)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('XUNJIA_UPGRADE_JOB_ID', '')
    if mode == 'new-worker':monkeypatch.setenv('XUNJIA_UPGRADE_JOB_ID', JOB)
    def deploy(bundle, root, apply):
        deployed.append((bundle, root, apply))
        if mode in ('legacy', 'new-worker'):assert module.os.environ['XUNJIA_UPGRADE_JOB_ID'] == JOB
        else:assert not module.os.environ.get('XUNJIA_UPGRADE_JOB_ID')
        return {'status': 'synthetic'}
    monkeypatch.setattr(module, 'deploy', deploy)
    option = '--check' if mode == 'check' else '--prepare-only' if mode == 'prepare' else '--apply'
    monkeypatch.setattr(sys, 'argv', [str(location), '--prefix', str(prefix), option])
    module.main()
    assert arms == ([(prefix, target, JOB)] if mode == 'legacy' else [])
    marker = stage / 'capsule-result.json'
    assert marker.exists() is (mode == 'legacy')
    if marker.exists():assert json.loads(marker.read_text()) == {'success': True, 'build_id': TARGET}
    assert bool(deployed) is (mode != 'prepare')
