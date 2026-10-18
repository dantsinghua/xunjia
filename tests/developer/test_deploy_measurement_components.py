"""Installed releases retain the measurement modules shipped in the bundle."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy'))
import manager as deployment
from common import DeployError, snapshot_payload, verify_payload


def prepare(tmp_path, monkeypatch, *, with_dify):
    bundle = tmp_path / 'bundle'
    for name in ('server', 'imlink', 'relay'):
        shutil.copytree(ROOT / name, bundle / 'app' / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(ROOT / 'deploy', bundle / 'deploy',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (bundle / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/measure_dify_v22.py', bundle / 'scripts/measure_dify_v22.py')
    for name in ('sql', 'frontend'):
        (bundle / name).mkdir()
    if with_dify:
        shutil.copytree(ROOT / 'dify/negotiation_v22', bundle / 'dify/negotiation_v22',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (bundle / 'VERSION.json').write_text(json.dumps({'build_id': 'synthetic-measurement-build'}))
    prefix = tmp_path / 'site'
    manager = deployment.Manager(bundle, prefix, service=object())
    monkeypatch.setattr(manager, 'python_base', lambda: Path(sys.executable))
    # Only environment preparation is synthetic; the production release copy,
    # metadata write, payload hashing and measurement subprocess all execute.
    monkeypatch.setattr(deployment, 'run', lambda *args, **kwargs: '')
    target = prefix / 'releases/synthetic-release'
    target.parent.mkdir(parents=True)
    release = manager.prepare_release({'id': 'synthetic-release', 'target': str(target)},
                                      {'build_id': 'synthetic-measurement-build'})
    return bundle, release


def test_prepared_release_measurement_help_imports_dify_and_hashes_components(tmp_path, monkeypatch):
    bundle, release = prepare(tmp_path, monkeypatch, with_dify=True)
    installed = release / 'app'
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('XUNJIA_', 'QB_', 'PYTHON'))}
    checked = subprocess.run([sys.executable, '-I', '-B',
        str(installed / 'scripts/measure_dify_v22.py'), '--help'], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=20)
    assert checked.returncode == 0, checked.stderr
    assert '--credentials' in checked.stdout and '--stage-budget-seconds' in checked.stdout
    copied = installed / 'dify/negotiation_v22/measurement.py'
    assert copied.read_bytes() == (bundle / 'dify/negotiation_v22/measurement.py').read_bytes()
    payload = snapshot_payload(installed)
    assert 'dify/negotiation_v22/measurement.py' in payload
    copied.write_text(copied.read_text() + '\n# synthetic integrity mutation\n')
    with pytest.raises(DeployError, match='RELEASE_INTEGRITY_FAILED'):
        verify_payload(installed, payload)


def test_legacy_bundle_without_dify_keeps_existing_prepare_contract(tmp_path, monkeypatch):
    _, release = prepare(tmp_path, monkeypatch, with_dify=False)
    assert (release / 'app/server/app.py').is_file()
    assert not (release / 'app/dify').exists()
