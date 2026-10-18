"""The package QA wrapper reaches its fixed runner on a Python 3.7 host."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def fixture(tmp_path, *, old_python=True):
    bundle = tmp_path/'bundle with spaces'
    deploy = bundle/'deploy'
    deploy.mkdir(parents=True)
    shutil.copyfile(ROOT/'deploy/05-test-package.sh', deploy/'05-test-package.sh')
    (deploy/'package_tests.py').write_text('import json,sys\nprint("QA_ARGS="+json.dumps(sys.argv[1:]))\nraise SystemExit(37)\n')
    (deploy/'bootstrap_runtime.py').write_text('import json,sys\nprint("BOOTSTRAP_ARGS="+json.dumps(sys.argv[1:]))\nraise SystemExit(29)\n')
    fakebin = tmp_path/'bin'; fakebin.mkdir()
    (fakebin/'dirname').symlink_to(shutil.which('dirname'))
    if old_python:
        python = fakebin/'python3'
        python.write_text('#!/bin/bash\nif [[ "$1" == "-c" ]]; then\n'
            ' if [[ "$2" == *"3,12"* || "$2" == *"3, 12"* ]]; then exit 1; fi\n'
            ' if [[ "$2" == *"version_info"* ]]; then exit 0; fi\nfi\n'
            'exec '+shlex.quote(sys.executable)+' "$@"\n')
        python.chmod(0o755)
    env = {key:value for key,value in os.environ.items() if key not in ('XUNJIA_QA_PYTHON', 'XUNJIA_PREFIX')}
    env.update(PATH=str(fakebin), XUNJIA_PREFIX=str(tmp_path/'site'))
    return bundle, env


def execute(bundle, env, args=()):
    return subprocess.run(['/bin/bash', str(bundle/'deploy/05-test-package.sh'), *args],
                          env=env, capture_output=True, text=True, timeout=15)


def test_python379_qa_entry_uses_fixed_package_tests_bootstrap_mode(tmp_path):
    bundle, env = fixture(tmp_path)
    args = ['--evidence-parent', str(tmp_path/'evidence with spaces')]
    completed = execute(bundle, env, args)
    assert completed.returncode == 29, completed.stderr
    expected = ['--package-tests', '--bundle', str(bundle), *args]
    assert 'BOOTSTRAP_ARGS='+json.dumps(expected) in completed.stdout
    assert not Path(env['XUNJIA_PREFIX']).exists()


def test_explicit_qa_python_preserves_runner_arguments_and_failure_exit_code(tmp_path):
    bundle, env = fixture(tmp_path)
    env['XUNJIA_QA_PYTHON'] = sys.executable
    completed = execute(bundle, env, ['--evidence-parent', str(tmp_path/'evidence')])
    assert completed.returncode == 37
    assert 'QA_ARGS='+json.dumps(['--bundle', str(bundle), '--evidence-parent', str(tmp_path/'evidence')]) in completed.stdout
    assert 'BOOTSTRAP_ARGS' not in completed.stdout


def test_installed_private_python312_is_used_without_changing_path(tmp_path):
    bundle, env = fixture(tmp_path, old_python=False)
    private = Path(env['XUNJIA_PREFIX'])/'python/bin/python3.12'
    private.parent.mkdir(parents=True)
    private.symlink_to(sys.executable)
    completed = execute(bundle, env)
    assert completed.returncode == 37, completed.stderr
    assert 'QA_ARGS='+json.dumps(['--bundle', str(bundle)]) in completed.stdout


def test_missing_interpreter_fails_before_any_package_qa_or_bootstrap(tmp_path):
    bundle, env = fixture(tmp_path, old_python=False)
    completed = execute(bundle, env)
    assert completed.returncode != 0
    assert 'QA_PYTHON' in completed.stderr
    assert 'QA_ARGS' not in completed.stdout and 'BOOTSTRAP_ARGS' not in completed.stdout
