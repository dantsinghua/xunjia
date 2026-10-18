"""The deliverable entry must enforce P0 evidence and cancellation contracts."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from scripts import process_runner
from test_process_runner import clean, evidence, live, owned, recorded, run


ROOT = Path(__file__).resolve().parents[2]


def entry():
    spec = importlib.util.spec_from_file_location('independent_verification_entry', ROOT / 'tests/run_verification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_logged_uses_deliverable_runner_and_survives_actual_offline_conftest(tmp_path, monkeypatch):
    caller = entry()
    original = caller.run_command
    calls = []
    def observed(command, **options):
        calls.append((list(command), deepcopy(options)))
        return original(command, **options)
    monkeypatch.setattr(caller, 'run_command', observed)
    node = 'tests/negotiation/test_domain.py::test_tied_suffix_and_missing_volume_unit_are_not_guessed'
    details = {}
    code, seconds = caller.run_logged([sys.executable, '-m', 'pytest', '-q', node],
        ROOT, dict(os.environ), 5, tmp_path / 'real-entry.log', expected_nodeids=[node], details=details)
    assert len(calls) == 1 and calls[0][1]['pytest_mode'] == 'run'
    assert calls[0][1]['expected_nodeids'] == [node]
    assert code == 0 and details['passed'], details
    assert details['pytest']['nodeids'] == [node]
    assert details['pytest']['counts'] == {'passed': 1, 'failed': 0, 'skipped': 0, 'incomplete': 0}
    assert seconds >= 0 and process_runner.verify_evidence(details['evidence']['result'])['passed']


@pytest.mark.parametrize('kind', ['empty', 'mismatched_inventory'])
def test_real_run_logged_cannot_report_empty_or_different_pytest_inventory_as_pass(tmp_path, kind):
    caller = entry()
    target = tmp_path / 'test_local.py'
    target.write_text('' if kind == 'empty' else 'def test_actual(): pass\n')
    details = {}
    code, _ = caller.run_logged([sys.executable, '-m', 'pytest', '-q', '--collect-only', target.name],
        tmp_path, dict(os.environ), 5, tmp_path / 'collection.log',
        expected_nodeids=['test_local.py::test_expected'], details=details)
    assert code != 0 and not details['passed'] and details['errors']
    manifest = json.loads(Path(details['evidence']['manifest']).read_text())
    pytest_path = next(Path(path) for path in manifest['files'] if Path(path).name == 'pytest.json')
    assert json.loads(pytest_path.read_text())['nodeids'] == ([] if kind == 'empty' else ['test_local.py::test_actual'])
    evidence(details)


@pytest.mark.parametrize('missing', ['log', 'events', 'captured_input', 'captured_artifact'])
def test_persistent_verification_rejects_missing_owned_required_evidence(tmp_path, missing):
    with owned(tmp_path, 'success') as case:
        result = run(case, expected_artifacts=[case['artifact']])
        assert result['passed'] and process_runner.verify_evidence(result['evidence']['result'])['passed']
        manifest = json.loads(Path(result['evidence']['manifest']).read_text())
        if missing in ('log', 'events'):
            victim = Path(result['evidence'][missing])
        elif missing == 'captured_input':
            victim = next(Path(path) for path in manifest['files'] if Path(path).parent.name == 'inputs')
        else:
            victim = Path(next(iter(manifest['output_artifacts'].values())))
        assert victim.is_relative_to(case['folder']) and victim.is_file()
        victim.unlink()  # Exact file created by this test, never a shared artifact.
        with pytest.raises((ValueError, FileNotFoundError)):
            process_runner.verify_evidence(result['evidence']['result'])
        clean(result, case)


def test_running_source_mutation_is_a_failure_with_the_original_source_preserved(tmp_path):
    script = tmp_path / 'mutates_itself.py'
    script.write_text('from pathlib import Path\np=Path(__file__)\np.write_text(p.read_text()+"# changed during run\\n")\n')
    original = script.read_bytes()
    result = process_runner.run_command([sys.executable, str(script)], cwd=tmp_path,
        log_path=tmp_path / 'source-change.log', timeout=2)
    assert result['process_exit_code'] == 0 and not result['passed'] and result['exit_code'] != 0
    assert str(script) in result['source_drift'] and result['errors']
    manifest = json.loads(Path(result['evidence']['manifest']).read_text())
    assert any(Path(path).read_bytes() == original for path in manifest['files'] if Path(path).parent.name == 'inputs')
    assert not process_runner.verify_evidence(result['evidence']['result'])['passed']


def test_damaged_progress_is_preserved_and_spawned_task_is_cleaned(tmp_path):
    with owned(tmp_path, 'wait') as case:
        progress = case['folder'] / 'progress.json'
        original = b'{"events": [CORRUPTED_OWNED_JOURNAL'
        progress.write_bytes(original)
        result = run(case, progress_path=progress)
        assert not result['passed'] and result['errors'] and result['exit_code'] != 0
        assert progress.read_bytes() == original
        assert result['cleanup']['owned_processes'], 'Fault must exercise the already-spawned lifecycle'
        assert not any(live(row) for row in result['cleanup']['owned_processes'])
        clean(result, case)
        evidence(result)


def test_timeout_reason_survives_a_reported_cleanup_failure(tmp_path, monkeypatch):
    original = process_runner.OwnedTask.cleanup
    with owned(tmp_path, 'wait') as case:
        def cannot_clean(task, *args):
            if task.process.args != case['command']:
                return original(task, *args)
            rows = task.members()
            return {'complete': False, 'remaining': rows, 'term_sent': [], 'kill_sent': [],
                    'owned_processes': [{**row, 'token': task.token} for row in rows],
                    'errors': ['SYNTHETIC_CLEANUP_FAILURE']}
        monkeypatch.setattr(process_runner.OwnedTask, 'cleanup', cannot_clean)
        result = run(case)
        assert result['timed_out'] and not result['passed'] and result['exit_code'] == 124
        assert not result['cleanup']['complete'] and result['cleanup']['remaining']
        assert any(live(row) for row in recorded(case['registry'])), 'Test-owned fallback will clean this fault'
        assert 'SYNTHETIC_CLEANUP_FAILURE' in json.dumps(result)
        evidence(result)


def test_collection_cancellation_stops_pipeline_before_executing_any_group(tmp_path, monkeypatch):
    caller = entry()
    calls = []
    def cancelled_collection(command, cwd, env, budget, log_path, *, expected_nodeids=None, details=None):
        calls.append(list(command))
        Path(log_path).write_text('SYNTHETIC_COLLECTION_CANCELLED\n')
        if details is not None:
            details.update(passed=False, cancelled=True, exit_code=130)
        return 130, 0.01
    monkeypatch.setattr(caller, 'manifest', lambda: {})
    monkeypatch.setattr(caller, 'run_logged', cancelled_collection)
    monkeypatch.setattr(sys, 'argv', ['run_verification.py', '--browsers-path', str(tmp_path / 'unused-browser'),
                                    '--output-root', str(tmp_path / 'results')])
    assert caller.main() != 0
    assert len(calls) == 1 and '--collect-only' in calls[0], 'Cancellation cannot launch the next real task'
