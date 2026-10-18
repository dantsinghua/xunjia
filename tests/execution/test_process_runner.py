"""Independent P0 gates: only owned synthetic processes may be signalled."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest


FIXTURE = Path(__file__).with_name('runner_fixture.py').resolve()


def module():
    return importlib.import_module('scripts.process_runner')


def recorded(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def current_identity(row):
    try:
        tail = Path('/proc', str(row['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
        if tail[0] == 'Z':
            return False
        environment = Path('/proc', str(row['pid']), 'environ').read_bytes().split(b'\0')
        return (int(tail[19]) == row['start_ticks'] and int(tail[2]) == row['pgid']
                and int(tail[3]) == row['sid']
                and ('P0_FIXTURE_TOKEN=' + row['token']).encode() in environment)
    except (FileNotFoundError, ProcessLookupError):
        return False
    except PermissionError:
        # A process can become a non-dumpable zombie between these reads.
        tail = Path('/proc', str(row['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
        if tail[0] == 'Z':
            return False
        raise


def live(row):
    try:
        tail = Path('/proc', str(row['pid']), 'stat').read_text().rsplit(')', 1)[1].split()
        return (tail[0] != 'Z' and int(tail[19]) == row['start_ticks']
                and int(tail[2]) == row['pgid'] and int(tail[3]) == row['sid'])
    except FileNotFoundError:
        return False


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)  # Bounded readiness polling, never timing a race by chance.
    raise AssertionError('Synthetic synchronization deadline expired')


@contextmanager
def owned(tmp_path, mode):
    folder = tmp_path / (mode + '-' + uuid.uuid4().hex)
    folder.mkdir()
    registry = folder / 'owned.jsonl'
    env = dict(os.environ, P0_FIXTURE_TOKEN=uuid.uuid4().hex,
               P0_FIXTURE_REGISTRY=str(registry), P0_FIXTURE_MODE=mode,
               P0_FIXTURE_ARTIFACT=str(folder / 'artifact.txt'))
    command = [sys.executable, str(FIXTURE), '--mode', mode]
    case = {'folder': folder, 'registry': registry, 'env': env, 'command': command,
            'log': folder / 'command.log', 'artifact': folder / 'artifact.txt'}
    try:
        yield case
    finally:
        # A failed runner must not leak the deliberately broken fixture. Signal
        # each exact identity; never use a process name or an unverified group.
        rows = recorded(registry)
        for row in reversed(rows):
            if live(row):
                assert current_identity(row), 'Refuse to signal an unverified process identity'
                os.kill(row['pid'], signal.SIGKILL)
        wait_for(lambda: not any(live(row) for row in rows))
        (folder / 'test-owned-cleanup.json').write_text(json.dumps({
            'owned': rows, 'active_remaining': [row for row in rows if live(row)]}, indent=2)+'\n')


@contextmanager
def sentinel(tmp_path):
    with owned(tmp_path, 'sentinel') as case:
        process = subprocess.Popen(case['command'], env=case['env'], start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(lambda: len(recorded(case['registry'])) == 1)
            yield recorded(case['registry'])[0]
        finally:
            rows = recorded(case['registry'])
            if rows and live(rows[0]):
                assert current_identity(rows[0])
                os.kill(rows[0]['pid'], signal.SIGTERM)
            process.wait(timeout=2)


def run(case, **changes):
    options = dict(cwd=case['folder'], log_path=case['log'], timeout=0.35,
                   env=case['env'], term_grace=0.12, kill_grace=0.5,
                   diagnostic_timeout=0.08)
    options.update(changes)
    return module().run_command(case['command'], **options)


def clean(result, case):
    assert {'passed', 'status', 'exit_code', 'process_exit_code', 'timed_out',
            'cancelled', 'elapsed_seconds', 'errors', 'cleanup', 'evidence'} <= set(result)
    assert type(result['passed']) is type(result['timed_out']) is type(result['cancelled']) is bool
    assert isinstance(result['status'], str) and result['status']
    assert type(result['exit_code']) is int and isinstance(result['errors'], list)
    assert result['cleanup']['complete'] is True
    assert result['cleanup']['remaining'] == []
    assert not any(live(row) for row in recorded(case['registry']))
    for row in result['cleanup']['owned_processes']:
        assert {'pid', 'pgid', 'sid', 'start_ticks', 'token'} <= set(row)
        assert row['token'] and all(type(row[key]) is int for key in ('pid', 'pgid', 'sid', 'start_ticks'))


def evidence(result):
    expected = {'log', 'result', 'manifest', 'events'}
    assert expected <= set(result['evidence'])
    for key in expected:
        assert Path(result['evidence'][key]).is_file(), key
    events = Path(result['evidence']['events']).read_text()
    assert 'BEGIN' in events and 'END' in events
    return json.loads(Path(result['evidence']['result']).read_text())


def test_t01_success_keeps_output_artifact_timing_and_reopenable_evidence(tmp_path):
    with owned(tmp_path, 'success') as case:
        result = run(case, expected_markers=('FIXTURE_BEGIN', 'FIXTURE_END'),
                     expected_artifacts=(case['artifact'],))
        assert result['passed'] is True and result['exit_code'] == result['process_exit_code'] == 0
        assert not result['timed_out'] and not result['cancelled'] and not result['errors']
        assert 0 <= result['elapsed_seconds'] < 3
        assert case['artifact'].read_text() == 'synthetic-result\n'
        assert 'FIXTURE_BEGIN' in case['log'].read_text() and 'FIXTURE_END' in case['log'].read_text()
        clean(result, case)
        assert evidence(result)['passed'] is True


def test_t01_silent_zero_exit_is_valid_without_output_contract(tmp_path):
    with owned(tmp_path, 'silent') as case:
        result = run(case)
        assert result['passed'] and result['process_exit_code'] == 0
        clean(result, case)
        evidence(result)


def test_t02_nonzero_preserves_original_exit_code_and_stderr(tmp_path):
    with owned(tmp_path, 'error') as case:
        result = run(case)
        assert not result['passed'] and result['exit_code'] == result['process_exit_code'] == 23
        assert not result['timed_out'] and not result['cancelled']
        assert 'SYNTHETIC_ORIGINAL_STDERR' in case['log'].read_text()
        clean(result, case)
        assert evidence(result)['process_exit_code'] == 23


def test_t03_timeout_is_bounded_has_process_evidence_and_cleans(tmp_path):
    with owned(tmp_path, 'wait') as case:
        result = run(case)
        assert not result['passed'] and result['timed_out'] and result['exit_code'] == 124
        assert 0.3 <= result['elapsed_seconds'] < 3
        assert len(recorded(case['registry'])) == 1
        assert result['cleanup']['term_sent']
        clean(result, case)
        evidence(result)
        all_evidence = '\n'.join(Path(path).read_text() for path in result['evidence'].values()
                                 if isinstance(path, str) and Path(path).is_file())
        assert str(recorded(case['registry'])[0]['pid']) in all_evidence
        assert 'diagnos' in all_evidence.lower() or 'process' in all_evidence.lower()


@pytest.mark.parametrize('mode', ['term-tree', 'ignore-tree'], ids=['T04-leader-exits', 'T05-all-ignore'])
def test_t04_t05_descendants_are_cleaned_even_after_the_session_leader_exits(tmp_path, mode):
    with sentinel(tmp_path) as outside, owned(tmp_path, mode) as case:
        result = run(case, timeout=0.5)
        assert len(recorded(case['registry'])) == 3, 'The synthetic tree must exist before timeout'
        assert result['timed_out'] and result['exit_code'] == 124 and not result['passed']
        assert result['cleanup']['term_sent'] and result['cleanup']['kill_sent']
        assert result['elapsed_seconds'] < 3 and live(outside)
        clean(result, case)
        evidence(result)


def test_t06_leader_exits_at_diagnosis_without_losing_end_or_harming_sentinel(tmp_path, monkeypatch):
    runner = module()
    with sentinel(tmp_path) as outside, owned(tmp_path, 'wait') as case:
        def vanished(*args, **kwargs):
            wait_for(lambda: len(recorded(case['registry'])) == 1)
            row = recorded(case['registry'])[0]
            assert current_identity(row)
            os.kill(row['pid'], signal.SIGTERM)
            wait_for(lambda: not live(row))
            raise ProcessLookupError('SYNTHETIC_LEADER_EXIT_RACE')
        monkeypatch.setattr(runner, 'diagnose', vanished)
        result = run(case)
        assert result['timed_out'] and not result['passed'] and result['exit_code'] == 124
        assert 'SYNTHETIC_LEADER_EXIT_RACE' in json.dumps(result['errors'])
        assert live(outside)
        clean(result, case)
        evidence(result)


@pytest.mark.parametrize('failure', ['timeout', 'nonzero'])
def test_t07_real_diagnostic_failure_cannot_skip_primary_cleanup(tmp_path, failure):
    with owned(tmp_path, 'wait') as case, owned(tmp_path, 'diagnostic') as diagnostic:
        if failure == 'timeout':
            setup = {key: value for key, value in diagnostic['env'].items() if key.startswith('P0_FIXTURE_')}
            script = 'import os,runpy;os.environ.update('+repr(setup)+');runpy.run_path('+repr(str(FIXTURE))+',run_name="__main__")'
        else:
            script = 'import sys;sys.stderr.write("SYNTHETIC_DIAGNOSTIC_ERROR\\n");sys.exit(19)'
        result = run(case, diagnostic_command=[sys.executable, '-c', script], diagnostic_timeout=0.15)
        assert result['timed_out'] and result['exit_code'] == 124 and not result['passed']
        assert result['elapsed_seconds'] < 3 and result['errors']
        assert 'diagnos' in json.dumps(result).lower()
        clean(result, case)
        if failure == 'timeout':
            assert len(recorded(diagnostic['registry'])) == 1
            assert not any(live(row) for row in recorded(diagnostic['registry'])), 'Diagnostic itself leaked'
        evidence(result)


def test_t08_keyboard_interrupt_cleans_only_the_owned_task(tmp_path, monkeypatch):
    runner = module()
    original = runner.append_progress
    with sentinel(tmp_path) as outside, owned(tmp_path, 'wait') as case:
        def interrupted(path, event):
            if event.get('event') == 'BEGIN':
                wait_for(lambda: len(recorded(case['registry'])) == 1)
                raise KeyboardInterrupt('SYNTHETIC_USER_CANCEL')
            return original(path, event)
        monkeypatch.setattr(runner, 'append_progress', interrupted)
        result = run(case, progress_path=case['folder'] / 'progress.jsonl', timeout=5)
        assert result['cancelled'] is True and not result['passed'] and result['exit_code'] != 0
        assert result['elapsed_seconds'] < 3 and live(outside)
        clean(result, case)
        evidence(result)


@pytest.mark.parametrize('missing', ['marker', 'artifact', 'pytest_summary'])
def test_t09_exit_zero_cannot_replace_expected_evidence(tmp_path, missing):
    with owned(tmp_path, 'silent') as case:
        changes = ({'expected_markers': ('REQUIRED_END',)} if missing == 'marker' else
                   {'expected_artifacts': (case['artifact'],)} if missing == 'artifact' else
                   {'pytest_mode': 'run', 'expected_nodeids': ['test_synthetic.py::test_one']})
        result = run(case, **changes)
        assert result['process_exit_code'] == 0 and not result['passed'] and result['exit_code'] != 0
        assert result['errors']
        clean(result, case)
        evidence(result)


@pytest.mark.parametrize('failure', ['empty', 'collection_error', 'skip', 'count_mismatch', 'wrong_nodeid'])
def test_t09_actual_pytest_requires_nonempty_successful_expected_collection(tmp_path, failure):
    with owned(tmp_path, 'pytest') as case:
        target = case['folder'] / 'test_synthetic.py'
        source = {'empty': '', 'collection_error': 'def invalid(:\n',
                  'skip': 'import pytest\n@pytest.mark.skip(reason="injected P0 fault")\ndef test_one(): pass\n',
                  'count_mismatch': 'def test_one(): pass\n',
                  'wrong_nodeid': 'def test_one(): pass\n'}[failure]
        target.write_text(source)
        case['command'] = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                           '--confcutdir', str(case['folder']), target.name]
        mode = 'collect' if failure in ('empty', 'collection_error', 'wrong_nodeid') else 'run'
        if mode == 'collect':
            case['command'].append('--collect-only')
        expected = ['test_synthetic.py::test_one']
        if failure == 'count_mismatch':
            expected.append('test_synthetic.py::test_missing')
        elif failure == 'wrong_nodeid':
            expected = ['test_synthetic.py::test_different']
        result = run(case, timeout=5, pytest_mode=mode, expected_nodeids=expected)
        assert not result['passed'] and result['exit_code'] != 0 and result['errors']
        if failure in ('empty', 'collection_error'):
            assert result['process_exit_code'] != 0
        else:
            assert result['process_exit_code'] == 0
        clean(result, case)
        evidence(result)


@pytest.mark.parametrize('mode', ['collect', 'run'])
def test_t09_real_pytest_expected_single_node_can_pass(tmp_path, mode):
    with owned(tmp_path, 'pytest') as case:
        target = case['folder'] / 'test_synthetic.py'
        target.write_text('def test_one(): assert 1 + 1 == 2\n')
        case['command'] = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                           '--confcutdir', str(case['folder']), target.name]
        if mode == 'collect':
            case['command'].append('--collect-only')
        result = run(case, timeout=5, pytest_mode=mode,
                     expected_nodeids=['test_synthetic.py::test_one'])
        assert result['passed'] and result['process_exit_code'] == 0
        assert not result['errors']
        clean(result, case)
        evidence(result)


@pytest.mark.parametrize('failure', ['cwd', 'executable', 'dependency'])
def test_t10_preflight_or_launch_failure_has_explicit_early_failure(tmp_path, failure):
    with owned(tmp_path, 'silent') as case:
        changes = {'timeout': 5}
        if failure == 'cwd':
            changes['cwd'] = case['folder'] / 'missing-working-directory'
        elif failure == 'executable':
            case['command'] = [str(case['folder'] / 'missing-executable')]
        else:
            case['command'] = [sys.executable, '-c', 'import synthetic_p0_nonexistent_dependency']
        result = run(case, **changes)
        assert not result['passed'] and result['exit_code'] != 0 and not result['timed_out']
        assert result['elapsed_seconds'] < 2 and result['errors']
        clean(result, case)
        evidence(result)


@pytest.mark.parametrize('fault', ['begin', 'result'])
def test_t11_recording_failure_is_not_success_and_cannot_leak_task(tmp_path, monkeypatch, fault):
    runner = module()
    with owned(tmp_path, 'wait' if fault == 'begin' else 'silent') as case:
        if fault == 'begin':
            original = runner.append_progress
            def broken_begin(path, event):
                if event.get('event') == 'BEGIN':
                    wait_for(lambda: len(recorded(case['registry'])) == 1)
                    raise OSError('SYNTHETIC_BEGIN_FAILURE')
                return original(path, event)
            monkeypatch.setattr(runner, 'append_progress', broken_begin)
        else:
            def broken_result(path, value):
                raise OSError('SYNTHETIC_RESULT_FAILURE')
            monkeypatch.setattr(runner, 'write_result', broken_result)
        result = run(case, progress_path=case['folder'] / 'progress.jsonl')
        assert not result['passed'] and result['exit_code'] != 0
        assert 'SYNTHETIC_' in json.dumps(result['errors'])
        clean(result, case)
        events = Path(result['evidence']['events']).read_text()
        assert 'END' in events


def test_t11_concurrent_progress_writers_preserve_all_begin_end_events(tmp_path):
    progress = tmp_path / 'shared-progress.jsonl'
    def one(index):
        with owned(tmp_path, 'silent') as case:
            result = run(case, progress_path=progress)
            assert result['passed']
            clean(result, case)
            return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, range(4)))
    text = progress.read_text()
    try:
        value = json.loads(text)
        events = value['events'] if isinstance(value, dict) else value
    except json.JSONDecodeError:
        events = [json.loads(line) for line in text.splitlines()]
    assert sum(row.get('event') == 'BEGIN' for row in events) == 4
    assert sum(row.get('event') == 'END' for row in events) == 4
    assert len({result['evidence']['result'] for result in results}) == 4


def test_t12_evidence_reopens_in_new_process_and_same_log_cannot_be_overwritten(tmp_path):
    with owned(tmp_path, 'success') as case:
        result = run(case, expected_markers=('FIXTURE_BEGIN', 'FIXTURE_END'),
                     expected_artifacts=(case['artifact'],))
        assert result['passed']
        persisted = evidence(result)
        assert persisted['exit_code'] == 0 and persisted['cleanup']['complete']
        paths = {key: str(value) for key, value in result['evidence'].items()
                 if key in ('log', 'result', 'manifest', 'events')}
        script = ('import hashlib,json,pathlib,sys; paths=json.loads(sys.argv[1]);'
                  'print(json.dumps({k:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for k,p in paths.items()}))')
        read = subprocess.run([sys.executable, '-c', script, json.dumps(paths)],
                              capture_output=True, text=True, timeout=3, check=True)
        actual = json.loads(read.stdout)
        assert actual == {key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key, path in paths.items()}
        manifest = Path(paths['manifest']).read_text()
        assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() in manifest
        before = {key: Path(path).read_bytes() for key, path in paths.items()}
        try:
            again = run(case)
        except FileExistsError:
            pass
        else:
            assert not again['passed'] and again['exit_code'] != 0
        assert {key: Path(path).read_bytes() for key, path in paths.items()} == before
