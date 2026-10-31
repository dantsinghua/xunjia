"""Independent systemd upgrade worker; survives xunjia-agent restart."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from contextvars import ContextVar

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from server.upgrade_control import UpgradeStore, load_channel, private_directory
from server.upgrade_retention import register_artifact
from server.upgrade_diagnostics import event
from deploy.upgrade_recovery import snapshot_baseline, recover_job
from deploy.upgrade_supervisor import run_install, stop_installer, InstallerNotStopped

CURRENT_JOB = ContextVar('upgrade_job', default=None)


def recover_interrupted(store, prefix, job, stage):
    """A restarted worker restores its saved baseline, never replays a download."""
    identity = job['id']
    if job['state'] == 'installing':
        try:
            path = stage / 'baseline.json'
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError('RECOVERY_BASELINE_UNAVAILABLE')
            baseline = json.loads(path.read_text())
            store.detail(identity, phase='rolling_back', rollback='running')
            event(store, identity, 'recovery', 'UPGRADE_WORKER_INTERRUPTED')
            stop_installer('xunjia-upgrade-apply-' + identity + '.service')
            restored = recover_job(prefix, identity, baseline)
            code = 'UPGRADE_ROLLBACK_SUCCEEDED' if restored['rollback'] == 'restored' else 'UPGRADE_FAILED_OLD_VERSION_HEALTHY'
            store.update(identity, 'failed', code=code)
            store.detail(identity, phase='failed', **restored, code='UPGRADE_WORKER_INTERRUPTED')
            event(store, identity, 'failed', code, **restored)
        except Exception:
            store.update(identity, 'unknown', code='UPGRADE_ROLLBACK_FAILED')
            store.detail(identity, phase='failed', rollback='failed', health='unverified')
            event(store, identity, 'unknown', 'UPGRADE_ROLLBACK_FAILED')
    else:
        state = 'unknown' if job['state'] == 'downloading' else 'failed'
        store.update(identity, state, code='UPGRADE_INTERRUPTED_BEFORE_SWITCH')
        store.detail(identity, phase=state, rollback='not_needed', health='unverified')
        event(store, identity, state, 'UPGRADE_INTERRUPTED_BEFORE_SWITCH')


def atomic_bytes(path, blob):
    temporary = path.with_suffix(path.suffix + '.partial')
    with temporary.open('xb') as stream:
        stream.write(blob)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply_capsule(stage, base, prefix, *, apply):
    output = stage / ('apply.log' if apply else 'check.log')
    env = dict(os.environ)
    env.pop('XUNJIA_UPGRADE_BASE_HINT', None)
    if base is not None and Path(base).is_absolute():
        env['XUNJIA_UPGRADE_BASE_HINT'] = str(base)
    # The installer already verifies baseline bytes, takes release locks,
    # preserves configuration and rolls back on failed service health checks.
    if apply and CURRENT_JOB.get():
        job_id = CURRENT_JOB.get()
        store = UpgradeStore(Path(prefix) / 'state/upgrade')
        seen = set()
        def progress():
            names = {'PREPARING':'preparing','STAGED':'prepared','STOPPING':'switching',
                     'MIGRATING':'switching','LAYOUT_READY':'switching','SWITCHING':'switching',
                     'STARTING':'health_check','COMMITTED':'checking','ABORTED':'rollback_check',
                     'RECOVERY_REQUIRED':'rolling_back'}
            for path in sorted((Path(prefix) / 'deployments').glob('*.json'))[-128:]:
                if path.is_symlink() or path.stat().st_size > 65536:continue
                record = json.loads(path.read_text())
                key = (record.get('id'),record.get('phase'))
                if record.get('upgrade_job') != job_id or key in seen or record.get('phase') not in names:continue
                seen.add(key)
                phase = names[record['phase']]
                store.detail(job_id, phase=phase)
                event(store, job_id, phase, 'DEPLOY_' + record['phase'])
        returncode = run_install(prefix, CURRENT_JOB.get(), sys.executable,
                                 APP / 'deploy/upgrade_subprocess.py', progress=progress)
    else:
        with output.open('xb') as log:
            process = subprocess.run([sys.executable, '-I', '-B', str(stage / 'upgrade.py'),
                                  '--prefix', str(prefix),
                                  '--apply' if apply else '--check'],
                                 stdout=log, stderr=subprocess.STDOUT, timeout=3600, env=env)
            log.flush()
            os.fsync(log.fileno())
        returncode = process.returncode
    if returncode == 124:
        raise ValueError('INSTALLER_TIMEOUT')
    if returncode and not output.is_file():
        raise ValueError('INSTALLER_FAILED_NO_OUTPUT')
    if returncode:
        with output.open('rb') as log:
            log.seek(max(0, output.stat().st_size - 4096))
            lines = log.read().decode('utf8', errors='replace').strip().splitlines()
        code = lines[-1] if lines else ''
        if code in {'NO_INTACT_SUPPORTED_BASE_BUNDLE', 'NO_INTACT_CURRENT_BASE_BUNDLE',
                    'INSTALLED_BUILD_NOT_SUPPORTED', 'INSTALLED_BUILD_UNPROVEN'}:
            raise ValueError(code)
        if re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', code):
            raise ValueError(code)
        raise ValueError('UPGRADE_APPLY_FAILED_SEE_LOCAL_LOG' if apply else 'UPGRADE_PREFLIGHT_FAILED_SEE_LOCAL_LOG')
    if output.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('UPGRADE_RESULT_INVALID')
    try:
        result = json.loads(output.read_text().strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError('UPGRADE_RESULT_INVALID') from None
    expected = 'UPGRADE_APPLIED_LOCAL_CHECKS_PASSED' if apply else 'CHECK_PASSED_NO_SERVICE_CHANGES'
    if result.get('status') != expected:
        raise ValueError('UPGRADE_RESULT_INVALID')
    return result


def run(prefix, job_id):
    if not re.fullmatch('[0-9a-f]{32}', job_id):
        raise ValueError('INVALID_JOB')
    prefix = Path(prefix).absolute()
    if prefix.resolve() != prefix or not (prefix / 'config/xunjia.env').is_file():
        raise ValueError('INVALID_PREFIX')
    store = UpgradeStore(prefix / 'state/upgrade')
    with store.lock(blocking=True):
        job = store.latest()
        if job and job['id'] == job_id and job['state'] in ('downloading', 'verifying', 'installing'):
            recover_interrupted(store, prefix, job, private_directory(store.root / 'jobs' / job_id))
            return
        if not job or job['id'] != job_id or not (job['state'] == 'queued' or
                job['state'] == 'unknown' and job['code'] == 'UPGRADE_START_NOT_CONFIRMED'):
            return
        stage = private_directory(store.root / 'jobs' / job_id)
        installed = lambda: json.loads((prefix / 'app/VERSION.json').read_text())['build_id']
        phase = 'queued'
        operator_authorized = False
        baseline = None
        store.detail(job_id, phase=phase, rollback='not_needed', health='unverified')
        try:
            config = load_channel(prefix)
            if '*' not in config['operators'] and job['owner'] not in config['operators']:
                raise ValueError('UPGRADE_OPERATOR_REVOKED')
            operator_authorized = True
            # Import the literal env parser under a unique name: the installer
            # subprocess loads its own verified deployment modules later.
            spec = importlib.util.spec_from_file_location('upgrade_env', APP / 'deploy/common.py')
            common = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(common)
            env = common.effective_env(common.read_env(prefix / 'config/xunjia.env'), prefix, APP)
            os.environ['XUNJIA_EGRESS_GATEWAY'] = env.get('XUNJIA_EGRESS_GATEWAY', '')
            connection_path = Path(env['XUNJIA_INSTITUTION_CONFIG'])
            from relay.file_gateway_connection import protected_auth_path, resolve_tls_files, connection_client
            from relay.file_gateway import GatewayAuth
            if connection_path.stat().st_mode & 0o077:
                raise ValueError('INSTITUTION_CONFIG_NOT_PRIVATE')
            connection = resolve_tls_files(json.loads(connection_path.read_text()), connection_path.parent)
            auth = GatewayAuth.from_file(protected_auth_path(connection_path, connection['auth_file']))
            client = connection_client(connection, auth, path=config['queue'])
            client.timeout = 60
            if client.pending() == 0:
                store.update(job_id, 'empty')
                event(store, job_id, 'empty', 'UPGRADE_QUEUE_EMPTY')
                return
            phase = 'downloading'
            store.update(job_id, phase, code='')
            store.detail(job_id, phase=phase)
            event(store, job_id, phase, 'UPGRADE_DOWNLOAD_STARTED')
            file = client.download()  # Consuming operation: NO implicit retry.
            if file is None:
                store.update(job_id, 'empty')
                return
            atomic_bytes(stage / 'package.zip', file.content)
            phase = 'verifying'
            store.update(job_id, phase, package_sha256=hashlib.sha256(file.content).hexdigest())
            store.detail(job_id, phase=phase)
            event(store, job_id, phase, 'UPGRADE_PACKAGE_STORED', package_sha256=hashlib.sha256(file.content).hexdigest())
            from relay.upgrade_package import verify_package
            manifest, source = verify_package(file.content, config['public_key'].encode('ascii'),
                                              config['site_id'], installed())
            target_build = manifest['target_build']
            store.update(job_id, phase, target_build=target_build)
            event(store, job_id, phase, 'UPGRADE_SIGNATURE_VERIFIED', target_build=target_build)
            if installed() == target_build:
                # Duplicate delivery is harmless, but is not a new install.
                store.update(job_id, 'succeeded', code='ALREADY_INSTALLED')
                event(store, job_id, 'succeeded', 'ALREADY_INSTALLED')
                return
            atomic_bytes(stage / 'upgrade.py', source)
            try:
                hint = store.get('base_bundle', config.get('base_bundle', ''))
            except (OSError, ValueError):
                hint = None
            base = Path(hint) if isinstance(hint, str) and hint and Path(hint).is_absolute() else None
            checked = apply_capsule(stage, base, prefix, apply=False)
            if checked.get('build_id') != target_build:
                raise ValueError('UPGRADE_TARGET_MISMATCH')
            target_bundle = Path(checked['bundle'])
            register_artifact(store, target_bundle, 'bundle')
            baseline = snapshot_baseline(prefix)
            atomic_bytes(stage / 'baseline.json', json.dumps(baseline).encode())
            store.detail(job_id, phase='preflight', previous_build=baseline['build_id'], health='ready')
            event(store, job_id, 'preflight', 'UPGRADE_PREFLIGHT_PASSED', previous_build=baseline['build_id'])
            phase = 'installing'
            store.update(job_id, phase)
            store.detail(job_id, phase=phase, health='unverified')
            event(store, job_id, phase, 'UPGRADE_INSTALL_STARTED')
            token = CURRENT_JOB.set(job_id)
            try:
                result = apply_capsule(stage, base, prefix, apply=True)
            finally:
                CURRENT_JOB.reset(token)
            if result.get('build_id') != target_build or installed() != target_build:
                raise ValueError('UPGRADE_TARGET_MISMATCH')
            store.set('base_bundle', str(target_bundle))
            store.update(job_id, 'succeeded', result=json.dumps(result))
            store.detail(job_id, phase='succeeded', rollback='not_needed', health='ready', previous_build=baseline['build_id'])
            event(store, job_id, 'succeeded', 'UPGRADE_SUCCEEDED', target_build=target_build, health='ready')
        except Exception as exc:
            # An ambiguous consumed download or an interrupted installer needs
            # reconciliation, never another automatic download/apply attempt.
            state = 'unknown' if phase in ('downloading', 'installing') else 'failed'
            code = str(exc)
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', code):
                code = 'UPGRADE_' + type(exc).__name__.upper()
            event(store, job_id, phase, code)
            store.detail(job_id, phase=phase, code=code)
            if phase == 'installing' and baseline is not None and not isinstance(exc, InstallerNotStopped):
                store.detail(job_id, phase='rolling_back', rollback='running')
                event(store, job_id, 'rolling_back', 'UPGRADE_RECOVERY_STARTED', previous_build=baseline['build_id'])
                try:
                    restored = recover_job(prefix, job_id, baseline)
                    outcome_code = 'UPGRADE_ROLLBACK_SUCCEEDED' if restored['rollback'] == 'restored' else 'UPGRADE_FAILED_OLD_VERSION_HEALTHY'
                    store.update(job_id, 'failed', code=outcome_code)
                    store.detail(job_id, phase='failed', **restored, code=code)
                    event(store, job_id, 'failed', outcome_code, **restored)
                    return
                except Exception as recovery_error:
                    store.detail(job_id, phase='failed', rollback='failed', health='unverified')
                    event(store, job_id, 'failed', 'UPGRADE_ROLLBACK_FAILED')
                    store.update(job_id, 'unknown', code='UPGRADE_ROLLBACK_FAILED')
                    return
            if isinstance(exc, InstallerNotStopped):
                store.detail(job_id, rollback='unknown', health='unverified')
            store.update(job_id, state, code=code)
        finally:
            register_artifact(store, stage, 'job')
            try:
                from server.upgrade_automation import upload_log
                if operator_authorized:upload_log(prefix,job_id)
            except Exception:pass


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--job', required=True)
    args = parser.parse_args()
    run(args.prefix, args.job)
