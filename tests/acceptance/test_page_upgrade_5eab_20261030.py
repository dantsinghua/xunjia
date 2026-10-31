"""Final signed delivery compatibility with pinned field baseline verifiers.

Set XUNJIA_FINAL_UPGRADE_PACKAGE, XUNJIA_FINAL_UPGRADE_PUBLIC_KEY and
XUNJIA_FINAL_UPGRADE_SITE_ID. No signing, installer execution or site access.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

FIELD_ROOT = Path('/home/anlin/.local/state/xunjia-auto-base-20261030/release/target')
FIELD_BUILD = '5eab3189d0fe54510deef4cf560aa6bec726b53942a0b85c5b1a5e1a55152703'
RECOVERY_BUILD = '404a02b31e9dd429134deb31242c50b13cfdd05853e6724905b20606fff45353'
DSL_BUILD = 'ff0d47e83f092c22da96e743c006f15eb14818a4345d02c80ed11bb5fdf7171e'
VERIFIER_SHA256 = '550c063dd44e8ca53dbd56240814a3677e0dff580e88ddb24a8b5c41a2551e78'
# Independently verified complete local bundles, 2026-10-31. Inputs may select
# these identities; they cannot supply an arbitrary path or replacement hash.
BASELINES = {
    FIELD_BUILD: (FIELD_ROOT, '113c6ab3eef51faa0e8bb3ad296ee6276838babb5f23cbe30b9cf45ca211cc20'),
    RECOVERY_BUILD: (Path('/home/anlin/.local/state/xunjia-upgrade-recovery-20261030/release-v2/target'),
                     'c933fabf61c4a6cdaac1df7ae7ea951b3dd9d9c3db682cd15a5c4879e0e882f4'),
    DSL_BUILD: (Path('/home/anlin/.local/state/xunjia-qa-dsl060-20261030/target'),
                'f0fde5a7c8d3824617bb3f5131e202706f3d05f6cfc34504c105cf9105c02f4e'),
}


def checked_baselines(value):
    assert isinstance(value, list) and value and all(isinstance(item, str) for item in value)
    assert len(value) == len(set(value)) and set(value) <= set(BASELINES)
    assert set(value) == {FIELD_BUILD} or {RECOVERY_BUILD, DSL_BUILD} <= set(value), 'New field release must support both 404a02 and ff0d47'
    return value


def expected_baselines():
    path = os.environ.get('QA_FINAL_UPGRADE_INPUTS')
    data = json.loads(Path(path).read_text()) if path else {}
    return checked_baselines(data.get('expected_baselines', [FIELD_BUILD]))


def final_inputs():
    input_file = os.environ.get('QA_FINAL_UPGRADE_INPUTS')
    if input_file:
        data = json.loads(Path(input_file).read_text())
        assert set(data) in ({'package', 'public_key', 'site_id'}, {'package', 'public_key', 'site_id', 'expected_baselines'})
        assert all(isinstance(data[key], str) and data[key] for key in ('package','public_key','site_id'))
        checked_baselines(data.get('expected_baselines', [FIELD_BUILD]))
        package, key, site = data['package'], data['public_key'], data['site_id']
        assert Path(package).is_file() and Path(key).is_file()
        return Path(package), Path(key), site
    names = ('XUNJIA_FINAL_UPGRADE_PACKAGE', 'XUNJIA_FINAL_UPGRADE_PUBLIC_KEY',
             'XUNJIA_FINAL_UPGRADE_SITE_ID')
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip('Awaiting final signed delivery inputs: ' + ', '.join(missing))
    package, key, site = (os.environ[name] for name in names)
    assert Path(package).is_file() and Path(key).is_file()
    return Path(package), Path(key), site


def field_source_verified(relative):
    return baseline_source_verified(FIELD_BUILD, relative)


def baseline_source_verified(build, relative):
    root, expected_manifest = BASELINES[build]
    assert root.resolve() == root and not Path(relative).is_absolute() and '..' not in Path(relative).parts
    source = root / relative
    assert source.is_file() and not source.is_symlink()
    assert hashlib.sha256((root / 'SHA256SUMS.txt').read_bytes()).hexdigest() == expected_manifest
    hashes = {line.split('  ', 1)[1]: line.split('  ', 1)[0]
              for line in (root / 'SHA256SUMS.txt').read_text().splitlines()}
    assert hashlib.sha256(source.read_bytes()).hexdigest() == hashes[relative]
    assert hashlib.sha256((root / 'VERSION.json').read_bytes()).hexdigest() == hashes['VERSION.json']
    assert json.loads((root / 'VERSION.json').read_text())['build_id'] == build
    if relative == 'app/relay/upgrade_package.py':
        assert hashes[relative] == VERIFIER_SHA256
    return source


def test_final_signed_capsule_matches_explicit_verified_field_baselines():
    package, _, site = final_inputs()
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        source = archive.read('upgrade.py')
    assert manifest['site_id'] == site
    expected = expected_baselines()
    assert len(manifest['base_builds']) == len(expected) and set(manifest['base_builds']) == set(expected)
    assert manifest['target_build'] not in expected
    assert hashlib.sha256(source).hexdigest() == manifest['upgrade_sha256']
    assignments = {node.targets[0].id: ast.literal_eval(node.value)
                   for node in ast.parse(source).body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id in ('CAPSULE', 'PAYLOAD')}
    capsule = assignments['CAPSULE']
    assert capsule['build_id'] == manifest['target_build']
    assert capsule['baselines'] == {BASELINES[build][1]: build for build in expected}
    for build in expected:
        baseline_source_verified(build, 'app/relay/upgrade_package.py')
    assert assignments['PAYLOAD'], 'A bootstrap/no-op capsule is not final upgrade evidence'
    assert any(name.startswith('frontend/dist/') for name in assignments['PAYLOAD'])
    assert any(name.startswith('app/server/') for name in assignments['PAYLOAD'])


@pytest.mark.parametrize('baseline', expected_baselines())
def test_each_pinned_old_verifier_accepts_signature_and_rejects_tamper(tmp_path, baseline):
    package, public_key, site = final_inputs()
    old_source = baseline_source_verified(baseline, 'app/relay/upgrade_package.py')
    script = r'''
import importlib.util,json,pathlib,sys
spec=importlib.util.spec_from_file_location('field_5eab_verifier',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
blob=pathlib.Path(sys.argv[2]).read_bytes();key=pathlib.Path(sys.argv[3]).read_bytes()
manifest,source=module.verify_package(blob,key,sys.argv[4],sys.argv[5])
assert set(manifest['base_builds'])==set(json.loads(sys.argv[6]))
assert sys.argv[5] in manifest['base_builds']
assert manifest['target_build'] not in manifest['base_builds']
try:module.verify_package(blob,key,sys.argv[4],'0'*64)
except module.UpgradePackageError as error:assert error.code=='UPGRADE_BASE_NOT_SUPPORTED'
else:raise AssertionError('Unsupported baseline accepted')
import io,zipfile
out=io.BytesIO()
with zipfile.ZipFile(io.BytesIO(blob)) as original,zipfile.ZipFile(out,'w') as modified:
 for item in original.infolist():
  raw=original.read(item.filename)
  if item.filename=='signature.bin':raw=bytes([raw[0]^1])+raw[1:]
  modified.writestr(item,raw)
try:module.verify_package(out.getvalue(),key,sys.argv[4],sys.argv[5])
except module.UpgradePackageError as error:assert error.code=='UPGRADE_SIGNATURE_INVALID'
else:raise AssertionError('Invalid signature accepted')
print(json.dumps({'verifier':str(pathlib.Path(sys.argv[1]).resolve()),'target_build':manifest['target_build'],
 'base_builds':manifest['base_builds'],'signature':'accepted','tamper':'rejected','unsupported_base':'rejected'}))
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(old_source), str(package),
                             str(public_key), site, baseline, json.dumps(expected_baselines())], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['verifier'] == str(old_source.resolve())
    print(json.dumps(evidence, ensure_ascii=False))


@pytest.mark.parametrize('value', [[], [FIELD_BUILD, FIELD_BUILD], ['0'*64], [RECOVERY_BUILD], [DSL_BUILD],
                                  [{'build': FIELD_BUILD, 'root': '/tmp/unverified'}]])
def test_expected_baselines_cannot_widen_or_silently_drop_required_field_versions(value):
    with pytest.raises(AssertionError):
        checked_baselines(value)


def test_legacy_single_5eab_and_explicit_three_baseline_modes_remain_supported():
    assert checked_baselines([FIELD_BUILD]) == [FIELD_BUILD]
    assert checked_baselines([FIELD_BUILD, RECOVERY_BUILD, DSL_BUILD]) == [FIELD_BUILD, RECOVERY_BUILD, DSL_BUILD]
