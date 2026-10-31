"""Automatic baseline contracts using real temporary manifests; no site I/O."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from deploy import remote_upgrade, upgrade_bootstrap
from server.upgrade_control import UpgradeStore
from tests.release.test_incremental_capsule import case, write


@pytest.fixture
def discovery(case, monkeypatch):
    case.home = case.root / 'home'
    case.operator = case.root / 'root'
    case.prefix = case.root / 'installed'
    case.home.mkdir()
    case.operator.mkdir()
    workspace = case.root / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv('XUNJIA_UPGRADE_BASE_HINT', raising=False)
    monkeypatch.setattr(case.module, 'DISCOVERY_ROOTS', (case.home, case.operator))
    write(case.prefix, 'app/VERSION.json', json.dumps({'build_id': 'base-one'}))
    return case


def copy_base(discovery, relative, source=None, *, root=None):
    return Path(shutil.copytree(source or discovery.old, (root or discovery.home) / relative))


@pytest.mark.parametrize('relative', [
    'Xunjia-new-20261026/xunjia-update-cb2811',
    'xunjia-new-20261026/xunjia-update-cb2811',
    'XUNJIA-NEW-20261026/XUNJIA-UPDATE-CB2811',
    'xjbridge/verified/xj-remote-upgrade/xunjia-update-cb2811',
    'xjbridge/loginfix/xunjia-update-cb2811',
])
def test_actual_directory_search_accepts_case_and_supported_extraction_layouts(discovery, relative):
    base = copy_base(discovery, relative)
    assert discovery.module.discover_base(discovery.prefix) == base


def test_identical_copies_do_not_make_a_verified_baseline_ambiguous(discovery):
    first = copy_base(discovery, 'Xunjia-first')
    second = copy_base(discovery, 'xunjia-second', root=discovery.operator)
    assert discovery.module.discover_base(discovery.prefix) in {first, second}
    output = discovery.module.prepare_automatic(discovery.prefix)
    assert output.parent == discovery.prefix / 'state/upgrade/bundles'
    discovery.module.verify_tree(output, 'target-release', discovery.module.CAPSULE['manifest_sha256'])


@pytest.mark.parametrize('damage', ['json', 'manifest', 'content', 'missing-runtime', 'extra-file', 'symlink'])
def test_damaged_candidate_is_skipped_without_weakening_integrity(discovery, damage):
    bad = copy_base(discovery, 'xunjia-0-broken')
    good = copy_base(discovery, 'xunjia-1-good')
    if damage == 'json':
        (bad / 'VERSION.json').write_text('{broken')
    elif damage == 'manifest':
        (bad / 'SHA256SUMS.txt').write_text('not a manifest')
    elif damage == 'content':
        (bad / 'app/server/app.py').write_text('unreviewed local change')
    elif damage == 'missing-runtime':
        (bad / 'runtime/python-runtime-aarch64.tar.gz').unlink()
    elif damage == 'extra-file':
        write(bad, 'unexpected.py', 'unreviewed')
    else:
        (bad / 'config/alias').symlink_to(discovery.old)
    assert discovery.module.discover_base(discovery.prefix) == good


def test_running_build_wins_over_earlier_intact_supported_version(discovery, monkeypatch):
    other = copy_base(discovery, 'xunjia-0-other', discovery.other)
    current = copy_base(discovery, 'xunjia-1-current')
    monkeypatch.setenv('XUNJIA_UPGRADE_BASE_HINT', str(other))
    assert discovery.module.discover_base(discovery.prefix) == current


def test_other_supported_complete_base_can_reconstruct_target_when_current_base_is_missing(discovery):
    copy_base(discovery, 'xunjia-other', discovery.other)
    target = discovery.module.prepare_automatic(discovery.prefix)
    discovery.module.verify_tree(target, 'target-release', discovery.module.CAPSULE['manifest_sha256'])
    assert (target / 'app/server/app.py').read_bytes() == (discovery.target / 'app/server/app.py').read_bytes()


def test_corrupt_current_base_falls_back_only_to_an_intact_supported_base(discovery):
    current = copy_base(discovery, 'xunjia-current')
    other = copy_base(discovery, 'xunjia-other', discovery.other)
    (current / 'app/server/app.py').write_text('corrupt current base')
    assert discovery.module.discover_base(discovery.prefix) == other
    (other / 'runtime/python-runtime-aarch64.tar.gz').write_bytes(b'corrupt runtime')
    with pytest.raises(discovery.module.IncrementalError, match='NO_INTACT_SUPPORTED_BASE_BUNDLE'):
        discovery.module.prepare_automatic(discovery.prefix)


@pytest.mark.parametrize('source', ['environment', 'channel', 'journal'])
@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_stale_hint_falls_back_to_a_verified_discovered_copy(discovery, monkeypatch, source, damage):
    hint = discovery.root / 'retired-copy'
    if damage == 'corrupt':
        shutil.copytree(discovery.old, hint)
        (hint / 'app/server/app.py').write_text('corrupt remembered source')
    if source == 'environment':
        monkeypatch.setenv('XUNJIA_UPGRADE_BASE_HINT', str(hint))
    elif source == 'channel':
        write(discovery.prefix, 'config/upgrade-channel.json', json.dumps({'base_bundle': str(hint)}))
    else:
        UpgradeStore(discovery.prefix / 'state/upgrade').set('base_bundle', str(hint))
    good = copy_base(discovery, 'xunjia-recovered')
    assert discovery.module.discover_base(discovery.prefix) == good


def test_target_rerun_uses_verified_cached_target_without_an_old_base(discovery):
    base = copy_base(discovery, 'xunjia-base')
    target = discovery.module.prepare_automatic(discovery.prefix)
    write(discovery.prefix, 'app/VERSION.json', json.dumps({'build_id': 'target-release'}))
    base.rename(discovery.root / 'retired-base')
    before = {p: p.read_bytes() for p in target.rglob('*') if p.is_file()}
    assert discovery.module.prepare_automatic(discovery.prefix) == target
    assert all(p.read_bytes() == content for p, content in before.items())


def test_target_rerun_can_reconstruct_from_an_intact_supported_old_base(discovery):
    copy_base(discovery, 'xunjia-other', discovery.other)
    write(discovery.prefix, 'app/VERSION.json', json.dumps({'build_id': 'target-release'}))
    target = discovery.module.prepare_automatic(discovery.prefix)
    discovery.module.verify_tree(target, 'target-release', discovery.module.CAPSULE['manifest_sha256'])


def test_target_rerun_rejects_corrupt_target_when_no_intact_base_remains(discovery):
    base = copy_base(discovery, 'xunjia-base')
    target = discovery.module.prepare_automatic(discovery.prefix)
    write(discovery.prefix, 'app/VERSION.json', json.dumps({'build_id': 'target-release'}))
    base.rename(discovery.root / 'retired-base')
    (target / 'app/server/app.py').write_text('tampered target')
    with pytest.raises(discovery.module.IncrementalError, match='NO_INTACT_SUPPORTED_BASE_BUNDLE'):
        discovery.module.prepare_automatic(discovery.prefix)


def test_installed_app_without_complete_bundle_is_not_a_reusable_base(discovery):
    shutil.copytree(discovery.old / 'app', discovery.prefix / 'app', dirs_exist_ok=True)
    with pytest.raises(discovery.module.IncrementalError, match='NO_INTACT_SUPPORTED_BASE_BUNDLE'):
        discovery.module.prepare_automatic(discovery.prefix)
    assert not (discovery.prefix / 'state/upgrade/bundles').exists()


def test_unknown_installed_build_refuses_even_when_supported_base_exists(discovery):
    copy_base(discovery, 'xunjia-base')
    write(discovery.prefix, 'app/VERSION.json', json.dumps({'build_id': 'unknown-newer-build'}))
    with pytest.raises(discovery.module.IncrementalError, match='INSTALLED_BUILD_NOT_SUPPORTED'):
        discovery.module.prepare_automatic(discovery.prefix)
    assert not (discovery.prefix / 'state/upgrade/bundles').exists()


@pytest.mark.parametrize('apply', [False, True])
@pytest.mark.parametrize('hint', ['absolute-stale', 'relative', 'absent'])
def test_remote_worker_passes_optional_hint_but_never_forces_bundle_argument(tmp_path, monkeypatch, apply, hint):
    monkeypatch.setenv('XUNJIA_UPGRADE_BASE_HINT', '/inherited/stale/value')
    base = tmp_path / 'missing-base' if hint == 'absolute-stale' else ('relative/base' if hint == 'relative' else None)
    expected = str(base) if hint == 'absolute-stale' else None
    stage = tmp_path / 'stage'
    write(stage, 'upgrade.py',
          'import json, os, sys\n'
          'assert "--bundle" not in sys.argv\n'
          f'assert os.environ.get("XUNJIA_UPGRADE_BASE_HINT") == {expected!r}\n'
          'status = "UPGRADE_APPLIED_LOCAL_CHECKS_PASSED" if "--apply" in sys.argv else "CHECK_PASSED_NO_SERVICE_CHANGES"\n'
          'print(json.dumps({"status": status}))\n')
    result = remote_upgrade.apply_capsule(stage, base, tmp_path / 'installed', apply=apply)
    assert result['status'] == ('UPGRADE_APPLIED_LOCAL_CHECKS_PASSED' if apply else 'CHECK_PASSED_NO_SERVICE_CHANGES')


@pytest.fixture
def bootstrap(tmp_path):
    directory = tmp_path / 'bootstrap'
    for name in ('install.py', 'install.sh', 'upgrade.py', 'channel.json'):
        write(directory, name, '{}\n' if name == 'channel.json' else '# synthetic\n')
    rows = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name for p in sorted(directory.iterdir())]
    write(directory, 'SHA256SUMS.txt', '\n'.join(rows) + '\n')
    return directory, tmp_path / 'installed'


def test_bootstrap_needs_no_manual_base_and_enables_channel_only_after_upgrade(bootstrap):
    directory, prefix = bootstrap
    calls = []
    def run(argv, **kwargs):
        assert kwargs == {'check': True}
        calls.append(argv)
    assert upgrade_bootstrap.install(directory, prefix, runner=run)['status'] == 'PAGE_UPGRADE_AND_RETENTION_ENABLED'
    assert calls == [
        [sys.executable, '-I', '-B', str(directory / 'upgrade.py'), '--prefix', str(prefix), '--apply'],
        [str(prefix / 'venv/bin/python'), '-I', '-B', str(prefix / 'app/deploy/enable_remote_upgrade.py'),
         '--prefix', str(prefix), '--channel', str(directory / 'channel.json')],
    ]


def test_bootstrap_failed_upgrade_cannot_enable_channel_or_claim_success(bootstrap):
    directory, prefix = bootstrap
    calls = []
    def fail(argv, **kwargs):
        calls.append(argv)
        raise subprocess.CalledProcessError(2, argv)
    with pytest.raises(subprocess.CalledProcessError):
        upgrade_bootstrap.install(directory, prefix, runner=fail)
    assert len(calls) == 1 and str(directory / 'upgrade.py') in calls[0]


@pytest.mark.parametrize('name', ['install.py', 'install.sh', 'upgrade.py', 'channel.json'])
def test_bootstrap_checks_every_shipped_input_before_any_execution(bootstrap, name):
    directory, prefix = bootstrap
    (directory / name).write_text('tampered')
    with pytest.raises(ValueError, match='BOOTSTRAP_INTEGRITY_FAILED'):
        upgrade_bootstrap.install(directory, prefix, runner=lambda *a, **k: pytest.fail('unverified execution'))
