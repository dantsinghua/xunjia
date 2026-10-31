"""Independent upgrade contracts: synthetic keys/state, no production I/O.

Real Ed25519, ZIP parsing, SQLite persistence and installer subprocesses run here.
The institution gateway and systemd boundary are explicitly simulated.
"""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from relay import upgrade_package as package
from server import upgrade_control as control_module
from server.upgrade_control import UpgradeControl, UpgradeStore, load_channel
from deploy import remote_upgrade as worker
from deploy import enable_remote_upgrade as admin
from scripts import upgrade_upload as sender

BASE = 'a' * 64
TARGET = 'b' * 64
NOW = 1800000000
SITE = 'synthetic-bank'


def private_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600)
    return path


@pytest.fixture
def signing():
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    source = ('CAPSULE = ' + repr({'schema': 1, 'build_id': TARGET, 'baselines': {'c' * 64: BASE}}) + '\n').encode()
    return SimpleNamespace(key=key, private=private, public=public, source=source)


def signed(signing, now=NOW):
    return package.build_package(signing.source, signing.private, SITE, now=now)


def members(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def archive(entries, *, mode=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as result:
        for name, data in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (mode if mode is not None and name == 'upgrade.py' else stat.S_IFREG | 0o600) << 16
            result.writestr(info, data)
    return output.getvalue()


def resigned(signing, **changes):
    parts = members(signed(signing))
    value = json.loads(parts['manifest.json'])
    value.update(changes)
    parts['manifest.json'] = json.dumps(value).encode()
    parts['signature.bin'] = signing.key.sign(parts['manifest.json'])
    return archive(parts.items())


def test_signature_roundtrip_keeps_exact_source_and_supported_identity(signing):
    manifest, source = package.verify_package(signed(signing), signing.public, SITE, BASE, now=NOW)
    assert source == signing.source
    assert manifest['base_builds'] == [BASE]
    assert manifest['target_build'] == TARGET
    assert manifest['upgrade_sha256'] == hashlib.sha256(source).hexdigest()
    assert manifest['expires_at'] - manifest['created_at'] == 30 * 86400


def test_same_target_is_a_valid_idempotent_delivery(signing):
    assert package.verify_package(signed(signing), signing.public, SITE, TARGET, now=NOW)[0]['target_build'] == TARGET


@pytest.mark.parametrize('part', ['manifest.json', 'signature.bin', 'upgrade.py'])
def test_tampered_content_is_rejected_before_execution(signing, part):
    parts = members(signed(signing))
    parts[part] = parts[part][:-1] + bytes([parts[part][-1] ^ 1])
    with pytest.raises(package.UpgradePackageError):
        package.verify_package(archive(parts.items()), signing.public, SITE, BASE, now=NOW)


def test_wrong_signer_cannot_authorize_a_package(signing):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(package.UpgradePackageError, match='SIGNATURE'):
        package.verify_package(signed(signing), public, SITE, BASE, now=NOW)


@pytest.mark.parametrize('site,base,clock,code', [
    ('another-bank', BASE, NOW, 'SITE_MISMATCH'),
    (SITE, 'd' * 64, NOW, 'BASE_NOT_SUPPORTED'),
    (SITE, BASE, NOW - 1, 'FROM_FUTURE'),
    (SITE, BASE, NOW + 30 * 86400, 'EXPIRED'),
])
def test_site_baseline_and_validity_are_enforced(signing, site, base, clock, code):
    with pytest.raises(package.UpgradePackageError, match=code):
        package.verify_package(signed(signing), signing.public, site, base, now=clock)


@pytest.mark.parametrize('changes', [
    {'target_build': 'd' * 64}, {'base_builds': ['d' * 64]},
    {'expires_at': NOW + 31 * 86400}, {'created_at': True}, {'schema': True},
    {'upgrade_sha256': 'd' * 64}, {'base_builds': [BASE, BASE]}, {'extra': 'forbidden'},
])
def test_even_signed_manifest_must_match_capsule_and_bounded_contract(signing, changes):
    with pytest.raises(package.UpgradePackageError):
        package.verify_package(resigned(signing, **changes), signing.public, SITE, BASE, now=NOW)


@pytest.mark.parametrize('damage', ['extra', 'duplicate', 'traversal', 'symlink', 'oversize_source', 'trailing_bytes'])
def test_zip_members_cannot_escape_or_bypass_size_limits(signing, damage):
    parts = members(signed(signing))
    entries = list(parts.items())
    mode = None
    if damage == 'extra': entries.append(('README', b'extra'))
    elif damage == 'duplicate': entries.append(('upgrade.py', b'duplicate'))
    elif damage == 'traversal': entries = [(('../upgrade.py' if name == 'upgrade.py' else name), data) for name, data in entries]
    elif damage == 'symlink': mode = stat.S_IFLNK | 0o777
    elif damage == 'oversize_source': entries = [(name, b'x' * (package.MAX_SOURCE_BYTES + 1) if name == 'upgrade.py' else data) for name, data in entries]
    with pytest.warns(UserWarning) if damage == 'duplicate' else null_context():
        blob = archive(entries, mode=mode)
    if damage == 'trailing_bytes': blob += b'not part of archive'
    with pytest.raises(package.UpgradePackageError):
        package.verify_package(blob, signing.public, SITE, BASE, now=NOW)


@contextmanager
def null_context():
    yield


def test_literal_metadata_read_never_executes_script(signing, tmp_path):
    sentinel = tmp_path / 'executed'
    source = signing.source + f"open({str(sentinel)!r}, 'w').write('unsafe')\n".encode()
    assert package.capsule_metadata(source)['target_build'] == TARGET
    assert not sentinel.exists()


def test_oversize_archive_is_rejected_before_zip_parse(signing):
    with pytest.raises(package.UpgradePackageError, match='SIZE'):
        package.verify_package(b'x' * (package.MAX_PACKAGE_BYTES + 1), signing.public, SITE, BASE, now=NOW)


@pytest.fixture
def installation(tmp_path, signing, monkeypatch):
    import incremental
    monkeypatch.setattr(incremental, 'DISCOVERY_ROOTS', ())
    monkeypatch.delenv('XUNJIA_UPGRADE_BASE_HINT', raising=False)
    prefix = tmp_path / 'installed'
    app = prefix / 'releases/old/app'
    private_file(app / 'VERSION.json', json.dumps({'build_id': BASE}))
    (prefix / 'app').symlink_to(app, target_is_directory=True)
    config = {'site_id': SITE, 'queue': 'xjup_synthetic', 'public_key': signing.public.decode(),
              'operators': ['operator'], 'base_bundle': str(tmp_path / 'base')}
    private_file(prefix / 'config/upgrade-channel.json', json.dumps(config))
    private_file(prefix / 'config/auth.yaml', 'auth:\n  key: synthetic\n  sm4_key: "1234567890123456"\n')
    connection = prefix / 'config/connection.json'
    private_file(connection, json.dumps({'base_url': 'https://relay.invalid', 'auth_file': 'auth.yaml'}))
    private_file(prefix / 'config/xunjia.env', f'XUNJIA_INSTITUTION_CONFIG={connection}\nXUNJIA_EGRESS_GATEWAY=\n')
    ctl = UpgradeControl()
    ctl.prefix, ctl.app = prefix, app
    store = ctl.store()
    baseline = {'build_id': BASE, 'release': str(app.parent), 'bundle': str(app),
                'config': {}, 'active': True}
    def snapshot(checked_prefix):
        assert Path(checked_prefix) == prefix
        return dict(baseline)
    def unconfirmed_recovery(*_args, **_kwargs):
        raise RuntimeError('SYNTHETIC_RECOVERY_UNCONFIRMED')
    # This fixture tests worker orchestration. Real release restoration and
    # service/health validation have independent tests in test_upgrade_recovery.
    monkeypatch.setattr(worker, 'snapshot_baseline', snapshot)
    monkeypatch.setattr(worker, 'recover_job', unconfirmed_recovery)
    return SimpleNamespace(prefix=prefix, app=app, release=app.parent, baseline=baseline,
                           config=config, ctl=ctl, store=store, signing=signing)


def job(case, state='queued', code='', target=''):
    identity = '1' * 32
    with case.store.db() as db:
        db.execute('INSERT INTO job(id,state,owner,created_at,updated_at,code,target_build) VALUES(?,?,?,?,?,?,?)',
                   (identity, state, 'operator', NOW, NOW, code, target))
    return identity


def test_unauthorized_user_cannot_start_job(installation, monkeypatch):
    monkeypatch.setattr(control_module.subprocess, 'run', lambda *a, **k: pytest.fail('must not start a process'))
    assert not installation.ctl.status('other')['can_upgrade']
    with pytest.raises(PermissionError): installation.ctl.start('other')
    assert installation.store.latest() is None


def test_wildcard_authorizes_each_signed_in_user(installation, monkeypatch):
    installation.config['operators'] = ['*']
    private_file(installation.prefix / 'config/upgrade-channel.json', json.dumps(installation.config))
    monkeypatch.setattr(control_module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    result = installation.ctl.start('new-user')
    assert result['can_upgrade'] and result['job']['state'] == 'queued'


def test_job_is_durable_before_independent_systemd_submission(installation, monkeypatch):
    calls = []
    def launch(argv, **kwargs):
        persisted = UpgradeStore(installation.store.root).latest()
        assert persisted['state'] == 'queued'
        assert persisted['owner'] == 'operator'
        assert argv[0] == '/usr/bin/systemd-run'
        assert '--unit=xunjia-upgrade-' + persisted['id'] in argv
        assert '--property=Type=simple' in argv
        assert str(installation.app / 'deploy/remote_upgrade.py') in argv
        assert kwargs.get('shell', False) is False
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(control_module.subprocess, 'run', launch)
    result = installation.ctl.start('operator')
    assert len(calls) == 1 and result['job']['id'] == installation.store.latest()['id']
    # Reconstructing the controller simulates the web process restarting.
    recovered = UpgradeControl()
    recovered.prefix, recovered.app = installation.prefix, installation.app
    assert recovered.status('operator')['job'] == result['job']


@pytest.mark.parametrize('state', ['queued', 'downloading', 'verifying', 'installing', 'unknown'])
def test_running_or_uncertain_job_prevents_another_submission(installation, monkeypatch, state):
    job(installation, state)
    monkeypatch.setattr(control_module.subprocess, 'run', lambda *a, **k: pytest.fail('must not resubmit'))
    with pytest.raises(ValueError): installation.ctl.start('operator')
    with installation.store.db() as db: assert db.execute('SELECT count(*) FROM job').fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['timeout', 'nonzero', 'missing_binary'])
def test_uncertain_systemd_submission_stays_non_retryable(installation, monkeypatch, failure):
    def launch(*args, **kwargs):
        if failure == 'timeout': raise subprocess.TimeoutExpired(args[0], 15)
        if failure == 'missing_binary': raise FileNotFoundError('synthetic')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(control_module.subprocess, 'run', launch)
    result = installation.ctl.start('operator')
    assert result['job']['state'] == 'unknown'
    with pytest.raises(ValueError): installation.ctl.start('operator')


def test_abandoned_job_is_marked_unknown_but_live_locked_worker_is_not(installation, monkeypatch):
    job(installation, 'installing')
    monkeypatch.setattr(control_module.time, 'time', lambda: NOW + 100)
    with installation.store.lock():
        assert installation.ctl.status('operator')['job']['state'] == 'installing'
    assert installation.ctl.status('operator')['job']['state'] == 'unknown'


@pytest.mark.parametrize('damage', ['public_mode', 'symlink', 'business_queue'])
def test_channel_config_requires_private_fixed_upgrade_queue(installation, damage):
    path = installation.prefix / 'config/upgrade-channel.json'
    if damage == 'public_mode': path.chmod(0o644)
    elif damage == 'symlink':
        original = path.with_suffix('.saved')
        path.rename(original)
        path.symlink_to(original)
    else:
        installation.config['queue'] = 'xj_dir_v1'
        private_file(path, json.dumps(installation.config))
    with pytest.raises(ValueError): load_channel(installation.prefix)


class DownloadGateway:
    def __init__(self, content, *, count=1, error=None):
        self.content, self.count, self.error = content, count, error
        self.downloads = 0
    def pending(self): return self.count
    def download(self):
        self.downloads += 1
        if self.error: raise self.error
        return SimpleNamespace(name='untrusted-name.zip', content=self.content)


def worker_gateway(monkeypatch, gateway):
    from relay import file_gateway_connection
    monkeypatch.setattr(file_gateway_connection, 'connection_client', lambda *a, **k: gateway)


def test_worker_checks_then_applies_once_and_survives_status_reader_restart(installation, monkeypatch, tmp_path):
    identity = job(installation)
    gateway = DownloadGateway(signed(installation.signing, now=int(time.time())))
    worker_gateway(monkeypatch, gateway)
    target = tmp_path / 'xunjia-update-target'
    target.mkdir()
    (target / 'VERSION.json').write_text(json.dumps({'build_id': TARGET}))
    phases = []
    def apply(stage, base, prefix, *, apply):
        assert (stage / 'package.zip').read_bytes() == gateway.content
        assert (stage / 'upgrade.py').read_bytes() == installation.signing.source
        phases.append(apply)
        if apply:
            assert UpgradeStore(installation.store.root).latest()['state'] == 'installing'
            private_file(installation.app / 'VERSION.json', json.dumps({'build_id': TARGET}))
        return {'status': 'UPGRADE_APPLIED_LOCAL_CHECKS_PASSED' if apply else 'CHECK_PASSED_NO_SERVICE_CHANGES',
                'build_id': TARGET, 'bundle': str(target)}
    monkeypatch.setattr(worker, 'apply_capsule', apply)
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 1 and phases == [False, True]
    saved = UpgradeStore(installation.store.root)
    assert saved.latest()['state'] == 'succeeded'
    assert saved.get('base_bundle') == str(target)
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 1 and phases == [False, True]


def test_empty_queue_never_calls_consuming_download(installation, monkeypatch):
    identity = job(installation)
    gateway = DownloadGateway(b'', count=0)
    worker_gateway(monkeypatch, gateway)
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 0 and installation.store.latest()['state'] == 'empty'


def test_ambiguous_download_is_not_retried_by_same_worker_job(installation, monkeypatch):
    identity = job(installation)
    gateway = DownloadGateway(b'', error=TimeoutError('synthetic credential-must-not-leak'))
    worker_gateway(monkeypatch, gateway)
    worker.run(installation.prefix, identity)
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 1
    result = installation.store.latest()
    assert result['state'] == 'unknown'
    assert 'credential-must-not-leak' not in result['code']


def test_bad_signature_is_saved_but_never_installed(installation, monkeypatch):
    identity = job(installation)
    parts = members(signed(installation.signing, now=int(time.time())))
    parts['signature.bin'] = b'x' * 64
    gateway = DownloadGateway(archive(parts.items()))
    worker_gateway(monkeypatch, gateway)
    monkeypatch.setattr(worker, 'apply_capsule', lambda *a, **k: pytest.fail('unauthorized code execution'))
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 1
    assert installation.store.latest()['state'] == 'failed'
    assert (installation.store.root / 'jobs' / identity / 'package.zip').read_bytes() == gateway.content
    assert not (installation.store.root / 'jobs' / identity / 'upgrade.py').exists()


@pytest.mark.parametrize('at_apply,expected', [(False, 'failed'), (True, 'unknown')])
def test_installer_failures_preserve_evidence_and_do_not_retry(installation, monkeypatch, tmp_path, at_apply, expected):
    identity = job(installation)
    gateway = DownloadGateway(signed(installation.signing, now=int(time.time())))
    worker_gateway(monkeypatch, gateway)
    target = tmp_path / 'target'
    target.mkdir()
    calls = []
    def apply(stage, base, prefix, *, apply):
        calls.append(apply)
        if apply == at_apply: raise ValueError('SYNTHETIC_INSTALL_FAILED')
        return {'build_id': TARGET, 'bundle': str(target)}
    monkeypatch.setattr(worker, 'apply_capsule', apply)
    worker.run(installation.prefix, identity)
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 1
    assert calls == ([False, True] if at_apply else [False])
    assert installation.store.latest()['state'] == expected
    assert json.loads((installation.app / 'VERSION.json').read_text())['build_id'] == BASE


def test_worker_successful_recovery_is_failed_restored_and_does_not_reconsume(installation, monkeypatch, tmp_path):
    identity = job(installation)
    gateway = DownloadGateway(signed(installation.signing, now=int(time.time())))
    worker_gateway(monkeypatch, gateway)
    target = tmp_path / 'recovery-target'
    target.mkdir()
    stages, restorations = [], []
    def apply(stage, base, prefix, *, apply):
        stages.append(apply)
        if apply:
            assert worker.CURRENT_JOB.get() == identity
            private_file(installation.app / 'VERSION.json', json.dumps({'build_id': TARGET}))
            raise ValueError('SYNTHETIC_INSTALL_HEALTH_FAILED')
        return {'build_id': TARGET, 'bundle': str(target)}
    def recover(prefix, job_id, baseline):
        assert (Path(prefix), job_id, baseline) == (installation.prefix, identity, installation.baseline)
        assert json.loads((installation.app / 'VERSION.json').read_text())['build_id'] == TARGET
        assert json.loads(installation.store.latest()['result'])['rollback'] == 'running'
        private_file(installation.app / 'VERSION.json', json.dumps({'build_id': BASE}))
        restorations.append(job_id)
        return {'rollback': 'restored', 'health': 'ready', 'previous_build': BASE}
    monkeypatch.setattr(worker, 'apply_capsule', apply)
    monkeypatch.setattr(worker, 'recover_job', recover)
    worker.run(installation.prefix, identity)
    saved = UpgradeStore(installation.store.root).latest()
    assert saved['state'] == 'failed' and saved['code'] == 'UPGRADE_ROLLBACK_SUCCEEDED'
    assert json.loads(saved['result']) == {'phase': 'failed', 'rollback': 'restored', 'health': 'ready',
                                         'previous_build': BASE, 'code': 'SYNTHETIC_INSTALL_HEALTH_FAILED'}
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 1 and stages == [False, True] and restorations == [identity]
    assert json.loads((installation.app / 'VERSION.json').read_text())['build_id'] == BASE


def test_worker_unstopped_installer_never_attempts_recovery_or_retry(installation, monkeypatch, tmp_path):
    identity = job(installation)
    gateway = DownloadGateway(signed(installation.signing, now=int(time.time())))
    worker_gateway(monkeypatch, gateway)
    target = tmp_path / 'unstopped-target'
    target.mkdir()
    stages = []
    def apply(stage, base, prefix, *, apply):
        stages.append(apply)
        if apply:
            raise worker.InstallerNotStopped('INSTALLER_NOT_STOPPED')
        return {'build_id': TARGET, 'bundle': str(target)}
    monkeypatch.setattr(worker, 'apply_capsule', apply)
    monkeypatch.setattr(worker, 'recover_job', lambda *_: pytest.fail('Recovery raced an unconfirmed installer'))
    worker.run(installation.prefix, identity)
    saved = UpgradeStore(installation.store.root).latest()
    assert saved['state'] == 'unknown' and saved['code'] == 'INSTALLER_NOT_STOPPED'
    assert json.loads(saved['result'])['rollback'] == 'unknown'
    assert json.loads(saved['result'])['health'] == 'unverified'
    assert worker.CURRENT_JOB.get() is None
    worker.run(installation.prefix, identity)
    assert gateway.downloads == 1 and stages == [False, True]


def test_same_target_does_not_execute_installer_or_claim_fresh_health(installation, monkeypatch):
    private_file(installation.app / 'VERSION.json', json.dumps({'build_id': TARGET}))
    identity = job(installation)
    gateway = DownloadGateway(signed(installation.signing, now=int(time.time())))
    worker_gateway(monkeypatch, gateway)
    monkeypatch.setattr(worker, 'apply_capsule', lambda *a, **k: pytest.fail('duplicate install'))
    worker.run(installation.prefix, identity)
    result = installation.ctl.status('operator')['job']
    assert result['state'] == 'succeeded'
    assert '未重复安装或执行健康检查' in result['message']


def test_worker_rechecks_operator_before_any_gateway_io(installation, monkeypatch):
    identity = job(installation)
    installation.config['operators'] = ['different-user']
    private_file(installation.prefix / 'config/upgrade-channel.json', json.dumps(installation.config))
    from relay import file_gateway_connection
    monkeypatch.setattr(file_gateway_connection, 'connection_client', lambda *a, **k: pytest.fail('revoked operator did I/O'))
    worker.run(installation.prefix, identity)
    assert installation.store.latest()['code'] == 'UPGRADE_OPERATOR_REVOKED'


def test_apply_capsule_runs_real_isolated_child_and_requires_success_json(tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir()
    private_file(stage / 'upgrade.py', "import json,sys\nprint(json.dumps({'status':'UPGRADE_APPLIED_LOCAL_CHECKS_PASSED' if '--apply' in sys.argv else 'CHECK_PASSED_NO_SERVICE_CHANGES'}))\n")
    assert worker.apply_capsule(stage, tmp_path / 'base', tmp_path / 'prefix', apply=False)['status'] == 'CHECK_PASSED_NO_SERVICE_CHANGES'
    assert worker.apply_capsule(stage, tmp_path / 'base', tmp_path / 'prefix', apply=True)['status'] == 'UPGRADE_APPLIED_LOCAL_CHECKS_PASSED'
    assert (stage / 'check.log').is_file() and (stage / 'apply.log').is_file()


@pytest.mark.parametrize('body', ["raise SystemExit(3)\n", "print('not JSON')\n", "print('{}')\n"])
def test_installer_exit_or_unproven_output_cannot_be_success(tmp_path, body):
    stage = tmp_path / 'stage'
    stage.mkdir()
    private_file(stage / 'upgrade.py', body)
    with pytest.raises(ValueError): worker.apply_capsule(stage, tmp_path / 'base', tmp_path / 'prefix', apply=True)


@pytest.fixture
def upload_case(tmp_path, signing):
    key = private_file(tmp_path / 'signing.key', signing.private)
    file = private_file(tmp_path / 'package.zip', signed(signing, now=int(time.time())))
    config = {'private_key': key, '_directory': tmp_path, 'site_id': SITE, 'queue': 'xjup_synthetic'}
    return config, file


@pytest.mark.parametrize('outcome', ['success', 'unknown'])
def test_upload_attempt_has_durable_marker_and_cannot_be_automatically_repeated(upload_case, monkeypatch, outcome):
    config, file = upload_case
    calls = []
    class Gateway:
        def pending(self): return 0
        def upload(self, name, blob):
            markers = list((config['_directory'] / 'upgrade-upload-records').glob('*.json'))
            assert len(markers) == 1 and json.loads(markers[0].read_text())['state'] == 'uploading'
            calls.append(name)
            if outcome == 'unknown': raise TimeoutError('synthetic uncertain response')
            return {'file_name': name}
    @contextmanager
    def gateway(_): yield Gateway()
    monkeypatch.setattr(sender, 'gateway', gateway)
    if outcome == 'success': assert sender.upload(config, file)['state'] == 'uploaded'
    else:
        with pytest.raises(TimeoutError): sender.upload(config, file)
    records = list((config['_directory'] / 'upgrade-upload-records').glob('*.json'))
    assert json.loads(records[0].read_text())['state'] == ('uploaded' if outcome == 'success' else 'unknown')
    with pytest.raises(ValueError, match='ALREADY_ATTEMPTED'): sender.upload(config, file)
    assert len(calls) == 1


def test_nonempty_upload_queue_is_not_modified(upload_case, monkeypatch):
    config, file = upload_case
    @contextmanager
    def gateway(_):
        yield SimpleNamespace(pending=lambda: 1, upload=lambda *a: pytest.fail('must not upload'))
    monkeypatch.setattr(sender, 'gateway', gateway)
    with pytest.raises(ValueError, match='QUEUE_NOT_EMPTY'): sender.upload(config, file)
    assert not list((config['_directory'] / 'upgrade-upload-records').glob('*.json'))


def synthetic_bundle(case, name='xunjia-update-good'):
    path = case.prefix / 'state/upgrade/bundles' / name
    private_file(path / 'VERSION.json', json.dumps({'build_id': BASE}))
    return path


def test_enable_finds_intact_candidate_after_corrupt_metadata_and_manifest(installation, monkeypatch, tmp_path):
    bad_json = synthetic_bundle(installation, 'xunjia-update-0')
    private_file(bad_json / 'VERSION.json', '{broken')
    bad_manifest = synthetic_bundle(installation, 'xunjia-update-1')
    good = synthetic_bundle(installation, 'xunjia-update-2')
    checked = []
    def verify(path):
        checked.append(path)
        if path == bad_manifest: raise ValueError('corrupt')
        return {'build_id': BASE}
    monkeypatch.setattr(admin, 'verify_bundle', verify)
    config = {k:v for k,v in installation.config.items() if k != 'base_bundle'}
    channel = private_file(tmp_path / 'channel.json', json.dumps(config))
    admin.enable(installation.prefix, channel)
    assert checked == [bad_manifest, good]
    assert installation.store.get('base_bundle') == str(good)


def test_enable_does_not_overwrite_an_existing_different_channel(installation, monkeypatch, tmp_path):
    synthetic_bundle(installation)
    monkeypatch.setattr(admin, 'verify_bundle', lambda _: {'build_id': BASE})
    original = (installation.prefix / 'config/upgrade-channel.json').read_bytes()
    config = {k:v for k,v in installation.config.items() if k != 'base_bundle'}
    config['queue'] = 'xjup_other'
    channel = private_file(tmp_path / 'channel.json', json.dumps(config))
    with pytest.raises(ValueError, match='DIFFERENT_NO_OVERWRITE'): admin.enable(installation.prefix, channel)
    assert (installation.prefix / 'config/upgrade-channel.json').read_bytes() == original


def test_invalid_new_channel_leaves_no_partially_enabled_configuration(installation, monkeypatch, tmp_path):
    synthetic_bundle(installation)
    monkeypatch.setattr(admin, 'verify_bundle', lambda _: {'build_id': BASE})
    target = installation.prefix / 'config/upgrade-channel.json'
    target.unlink()
    config = {k:v for k,v in installation.config.items() if k != 'base_bundle'}
    config['queue'] = 'xj_dir_v1'
    channel = private_file(tmp_path / 'bad-channel.json', json.dumps(config))
    with pytest.raises(ValueError): admin.enable(installation.prefix, channel)
    assert not target.exists(), 'Rejected channel must not leave persistent invalid configuration'


@pytest.mark.parametrize('state', ['active', 'activating', 'deactivating'])
def test_reconcile_refuses_worker_that_is_not_proven_stopped(installation, monkeypatch, state):
    identity = job(installation, 'unknown', target=TARGET)
    monkeypatch.setattr(admin.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='ActiveState=' + state))
    with pytest.raises(ValueError, match='WORKER_NOT_PROVEN_STOPPED'):
        admin.reconcile(installation.prefix, identity, 'failed')
    assert installation.store.latest()['state'] == 'unknown'


def reconciliation_manager(installation, monkeypatch, events, *, unfinished=False):
    import manager
    # Reconciliation now verifies a discovered complete package before touching
    # the installer journal. Supply real manifest bytes for that boundary.
    base = installation.prefix / 'state/upgrade/bundles/xunjia-reconciliation'
    current = json.loads((installation.app / 'VERSION.json').read_text())['build_id']
    private_file(base / 'VERSION.json', json.dumps({'build_id': current, 'deployment_layout': 2}))
    for name in ('config/xunjia.env.example', 'requirements.lock', 'app/server/app.py',
                 'deploy/manager.py', 'deploy/runtime.py', 'sql/oceanbase/001_xunjia_schema_mysql.sql'):
        private_file(base / name, '# synthetic reconciliation input\n')
    rows = [hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.relative_to(base).as_posix()
            for path in sorted(base.rglob('*')) if path.is_file()]
    private_file(base / 'SHA256SUMS.txt', '\n'.join(rows) + '\n')
    class Manager:
        def __init__(self, *args): pass
        @contextmanager
        def lock(self): yield
        def ensure_idle(self):
            events.append('idle')
            if unfinished: raise ValueError('RECOVERY_REQUIRED')
        def verify_installed(self, *, liveness):
            assert liveness
            events.append('health')
    monkeypatch.setattr(manager, 'Manager', Manager)
    monkeypatch.setattr(admin.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='ActiveState=inactive'))


def test_reconcile_success_requires_running_target_and_health(installation, monkeypatch):
    identity = job(installation, 'unknown', target=TARGET)
    private_file(installation.app / 'VERSION.json', json.dumps({'build_id': TARGET}))
    events = []
    reconciliation_manager(installation, monkeypatch, events)
    monkeypatch.setattr(admin, 'verify_bundle', lambda _: {'build_id': TARGET})
    admin.reconcile(installation.prefix, identity, 'succeeded')
    assert events == ['idle', 'health']
    assert installation.store.latest()['state'] == 'succeeded'


def test_reconcile_does_not_clear_unfinished_installer_journal(installation, monkeypatch):
    identity = job(installation, 'unknown', target=TARGET)
    reconciliation_manager(installation, monkeypatch, [], unfinished=True)
    with pytest.raises(ValueError, match='RECOVERY_REQUIRED'):
        admin.reconcile(installation.prefix, identity, 'failed')
    assert installation.store.latest()['state'] == 'unknown'


def test_reconcile_cannot_label_installed_target_as_failed(installation, monkeypatch):
    identity = job(installation, 'unknown', target=TARGET)
    private_file(installation.app / 'VERSION.json', json.dumps({'build_id': TARGET}))
    reconciliation_manager(installation, monkeypatch, [])
    with pytest.raises(ValueError, match='TARGET_IS_INSTALLED'):
        admin.reconcile(installation.prefix, identity, 'failed')
    assert installation.store.latest()['state'] == 'unknown'


@pytest.fixture
def api_client(installation, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig, AuthUser
    monkeypatch.setattr(control_module, 'UpgradeControl', lambda: installation.ctl)
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'business.db'), enable_background=False,
                     auth_config=AuthConfig(sso_secret='synthetic-portal', session_secret='synthetic-session-secret-long-enough',
                                            clock=lambda: NOW))
    token = app.state.auth.issue_token(AuthUser('operator', 'Synthetic Operator'))
    with TestClient(app) as client:
        yield client, {'Authorization': 'Bearer ' + token}


@pytest.mark.parametrize('method', ['get', 'post'])
def test_upgrade_endpoint_requires_application_login(api_client, installation, monkeypatch, method):
    client, _ = api_client
    monkeypatch.setattr(control_module.subprocess, 'run', lambda *a, **k: pytest.fail('unauthenticated launch'))
    response = getattr(client, method)('/api/system/upgrade')
    assert response.status_code == 401
    assert installation.store.latest() is None


def test_authenticated_api_reads_state_and_submits_without_arbitrary_command(api_client, installation, monkeypatch):
    client, headers = api_client
    launched = []
    monkeypatch.setattr(control_module.subprocess, 'run', lambda command, **kwargs: (launched.append(command) or SimpleNamespace(returncode=0)))
    response = client.get('/api/system/upgrade', headers=headers)
    assert response.status_code == 200 and response.json()['data']['current_build'] == BASE
    assert client.post('/api/system/upgrade', headers=headers, json={'command': 'arbitrary'}).status_code == 400
    assert not launched
    response = client.post('/api/system/upgrade', headers=headers, json={})
    assert response.status_code == 200 and response.json()['data']['job']['state'] == 'queued'
    assert len(launched) == 1


def test_cleanup_failure_is_visible_in_upgrade_page_status_not_old_success(installation, monkeypatch):
    from server import upgrade_retention
    installation.store.set('retention', {'days': 30, 'last_run': NOW, 'removed': 4, 'blocked': 0,
                                         'hint': '上次清理完成'})
    def fail(*args):
        raise PermissionError('synthetic-private-path-must-not-leak')
    monkeypatch.setattr(upgrade_retention, 'prune_artifacts', fail)
    try:
        installation.ctl.maintain()
    except PermissionError:
        # The application scheduler catches exceptions. That must not conceal
        # failure from the dedicated page's status payload.
        pass
    status = installation.ctl.status('operator')
    hint = status['retention'].get('hint', '')
    assert any(word in hint for word in ('失败', '异常', '未完成')), 'Cleanup failure must be visible in its page status'
    assert 'synthetic-private-path-must-not-leak' not in json.dumps(status)


def test_archive_summary_cannot_mask_failed_file_cleanup_in_http_status(api_client, installation):
    client, headers = api_client
    installation.store.set('retention', {'days': 30, 'last_run': NOW, 'removed': 0, 'blocked': 1,
                                         'hint': '文件清理失败，请检查本地日志'})
    client.app.state.institution = SimpleNamespace(download_retention={
        'retention_days': 30, 'payloads_removed': 1, 'media_removed': 0, 'protected': 0, 'errors': []})
    response = client.get('/api/system/upgrade', headers=headers)
    assert response.status_code == 200
    retention = response.json()['data']['retention']
    assert '失败' in retention['hint'], 'Successful archive cleanup must not conceal failed artifact cleanup'
    assert retention['blocked'] >= 1
