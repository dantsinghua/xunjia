"""Measured configuration and local benchmark gates travel with offline QA.

No archive, installer, model, service or ARM runtime is started here.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from deploy import build_offline, package_tests
from server.negotiation.settings import Aggregation


ROOT = Path(__file__).resolve().parents[2]
PROFILE = 'config/aggregation-profile.measured.json'
METADATA = 'config/aggregation-measurement.json'
OLD_SCOPES = ('tests/release', 'tests/acceptance', 'tests/cutover', 'tests/negotiation')
BENCHMARKS = ('aggregation_cost.py', 'evaluate_aggregation.py',
              'test_aggregation_cost.py', 'test_evaluate_aggregation_inputs.py')


def write_pair(root, profile=None):
    value = profile or {'quiet_ms': 1463, 'max_wait_ms': 4389,
        'reference_ms': 11706.479666666666, 'measurement_id': 'synthetic-release-measurement'}
    path = root / PROFILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2)+'\n', encoding='utf8')
    metadata = {'profile_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (root / METADATA).write_text(json.dumps(metadata)+'\n', encoding='utf8')
    return value, metadata


def test_valid_measured_pair_is_included_without_rewriting_bytes(tmp_path):
    profile, metadata = write_pair(tmp_path)
    Aggregation(**profile)
    before = {name: (tmp_path/name).read_bytes() for name in (PROFILE, METADATA)}
    files = build_offline.input_files(tmp_path)
    assert files.get(PROFILE) == tmp_path/PROFILE
    assert files.get(METADATA) == tmp_path/METADATA
    assert all(files[name].read_bytes() == before[name] for name in before)
    assert hashlib.sha256(files[PROFILE].read_bytes()).hexdigest() == metadata['profile_sha256']


@pytest.mark.parametrize('present', [PROFILE, METADATA])
def test_a_single_measured_file_is_rejected_instead_of_silently_omitted(tmp_path, present):
    path = tmp_path/present
    path.parent.mkdir(parents=True)
    path.write_text('{}\n', encoding='utf8')
    before = path.read_bytes()
    with pytest.raises(build_offline.DeployError):
        build_offline.input_files(tmp_path)
    assert path.read_bytes() == before


def test_metadata_must_match_the_actual_profile_bytes(tmp_path):
    write_pair(tmp_path)
    path = tmp_path/PROFILE
    # JSON meaning remains identical, but the delivered-byte digest is stale.
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(build_offline.DeployError):
        build_offline.input_files(tmp_path)


def test_exact_half_reference_is_illegal_even_with_a_matching_digest(tmp_path):
    profile = {'quiet_ms': 1463, 'max_wait_ms': 4389,
               'reference_ms': 8778, 'measurement_id': 'synthetic-half-boundary'}
    write_pair(tmp_path, profile)
    with pytest.raises(ValueError):
        Aggregation(**profile)
    with pytest.raises(build_offline.DeployError):
        build_offline.input_files(tmp_path)


def test_legacy_fixture_without_either_measured_file_still_has_its_original_inputs(tmp_path):
    files = build_offline.input_files(tmp_path)
    assert PROFILE not in files and METADATA not in files
    assert files['config/xunjia.env.example'] == tmp_path/'config/xunjia.env.example'
    assert files['tests/conftest.py'] == tmp_path/'tests/conftest.py'


def test_selected_repository_profile_and_all_benchmark_sources_are_package_inputs():
    files = build_offline.input_files(ROOT)
    assert files.get(PROFILE) == ROOT/PROFILE and files.get(METADATA) == ROOT/METADATA
    profile = json.loads(files[PROFILE].read_text())
    assert set(profile) == {'quiet_ms', 'max_wait_ms', 'reference_ms', 'measurement_id'}
    assert (profile['quiet_ms'], profile['max_wait_ms'], profile['reference_ms']) == (
        1463, 4389, 11706.479666666666)
    Aggregation(**profile)
    metadata = json.loads(files[METADATA].read_text())
    assert metadata['profile_sha256'] == hashlib.sha256(files[PROFILE].read_bytes()).hexdigest()
    for name in BENCHMARKS:
        relative = 'tests/benchmarks/'+name
        assert (ROOT/relative).is_file()
        assert files.get(relative) == ROOT/relative
    env = build_offline.template_is_clean(ROOT/'config/xunjia.env.example')
    assert env['XUNJIA_RUN_BACKGROUND'] == '1'
    assert 'XUNJIA_ENABLE_LIVE_TRANSPORT' not in env


@pytest.mark.parametrize('benchmarks_present', [False, True])
def test_package_qa_executes_both_collection_and_tests_for_the_present_benchmark_scope(
        tmp_path, monkeypatch, benchmarks_present):
    bundle = tmp_path/'synthetic-bundle'
    bundle.mkdir()
    if benchmarks_present:
        (bundle/'tests/benchmarks').mkdir(parents=True)
    expected = [*OLD_SCOPES, *(['tests/benchmarks'] if benchmarks_present else [])]
    calls = []

    def local_process(command, *, cwd, env, stdout, stderr, timeout):
        # Keep the real QA orchestration, stage logger, collection parsing and
        # cleanup. Substitute only external subprocesses; no pip or archive work.
        calls.append(deepcopy(command))
        assert Path(cwd) == bundle
        if 'pytest' in command and '--collect-only' in command:
            scopes = [item for item in command if item.startswith('tests/')]
            stdout.write('\n'.join(scope+'/test_synthetic.py::test_retained_gate' for scope in scopes)+'\n')
        else:
            stdout.write('synthetic subprocess completed\n')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(package_tests.subprocess, 'run', local_process)
    evidence = tmp_path/'evidence'
    code = package_tests.main(['--bundle', str(bundle), '--evidence-parent', str(evidence)])
    assert code == 0
    reports = list(evidence.glob('xunjia-package-evidence-*/result.json'))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report['passed'] and report['work_removed'] and report['test_scopes'] == expected
    assert [row['name'] for row in report['results']] == [
        'verify-package', 'create-venv', 'install-offline', 'pip-check', 'pytest-collect', 'pytest']
    pytest_calls = [command for command in calls if 'pytest' in command]
    assert len(pytest_calls) == 2
    for command in pytest_calls:
        assert [item for item in command if item.startswith('tests/')] == expected
    assert '--collect-only' in pytest_calls[0] and '--collect-only' not in pytest_calls[1]
    install = next(command for command in calls if 'install' in command)
    assert '--no-index' in install and '--no-deps' in install
    nodes = json.loads(Path(report['collection']['nodeids']).read_text())
    assert nodes == sorted(scope+'/test_synthetic.py::test_retained_gate' for scope in expected)
