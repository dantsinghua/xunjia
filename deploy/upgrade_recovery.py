"""Restore only this upgrade's prior release and verify its business readiness."""
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DeployError, config_fingerprint, load_json
from manager import Manager, TERMINAL


def snapshot_baseline(prefix):
    prefix = Path(prefix)
    version = load_json(prefix / 'app/VERSION.json')['build_id']
    # Recovery needs the validated installed release, not an expendable raw
    # bundle. These Manager methods never materialize or install new code.
    bundle = (prefix / 'app').resolve()
    manager = Manager(bundle, prefix)
    with manager.lock():
        manager.ensure_idle()
        manager.service.validate()
        if not manager.service.active():
            raise DeployError('EXISTING_SERVICE_MUST_BE_RUNNING')
        release = manager.current()
        if release is None:raise DeployError('NO_MANAGED_RELEASE')
        manager.verify_installed()
        return {'release': str(release), 'build_id': version, 'bundle': str(bundle),
                'config': config_fingerprint(prefix / 'config'), 'active': True}


def recover_job(prefix, job_id, baseline):
    prefix = Path(prefix)
    if not re.fullmatch('[0-9a-f]{32}', job_id):
        raise DeployError('INVALID_UPGRADE_JOB')
    previous = Path(baseline['release'])
    if previous.resolve() != previous or previous.parent != prefix / 'releases':
        raise DeployError('RECOVERY_BASELINE_INVALID')
    manager = Manager(Path(baseline['bundle']), prefix)
    with manager.lock():
        manager.service.validate()
        old = manager.validate_release(previous)
        if old.get('build_id') != baseline['build_id'] or not baseline.get('active'):
            raise DeployError('RECOVERY_BASELINE_INVALID')
        if config_fingerprint(prefix / 'config') != baseline['config']:
            raise DeployError('RECOVERY_CONFIG_CHANGED')
        records = [load_json(p) for p in (prefix / 'deployments').glob('*.json')]
        if any(r.get('phase') not in TERMINAL and r.get('upgrade_job') != job_id for r in records):
            raise DeployError('RECOVERY_OTHER_TRANSACTION_ACTIVE')
        own = [r for r in records if r.get('upgrade_job') == job_id]
        installs = [r for r in own if r.get('operation') == 'install']
        if len(installs) > 1:
            raise DeployError('RECOVERY_TRANSACTION_AMBIGUOUS')
        current = manager.current()
        touched = False
        if installs:
            txn = installs[0]
            target = Path(txn.get('target', ''))
            if txn.get('previous') != str(previous) or target.parent != prefix / 'releases' or target.name != txn['id']:
                raise DeployError('RECOVERY_TRANSACTION_MISMATCH')
            if current not in (previous, target):
                raise DeployError('RECOVERY_CURRENT_CHANGED')
            for record in own:
                if record is txn:continue
                if record.get('operation') != 'rollback' or record.get('previous') != str(target) or record.get('target') != str(previous):
                    raise DeployError('RECOVERY_TRANSACTION_MISMATCH')
            touched = bool(txn.get('service_touched') or txn.get('switched'))
            if current != previous or any(r.get('phase') not in TERMINAL for r in own):
                manager.restore(txn)
                for record in own:
                    if record is not txn and record.get('phase') not in TERMINAL:
                        manager.phase(record, 'RECOVERED')
        elif own or current != previous:
            raise DeployError('RECOVERY_TRANSACTION_UNPROVEN')
        if manager.current() != previous:
            raise DeployError('RECOVERY_WRONG_RELEASE')
        if not manager.service.active():manager.service.start()
        health = manager.verify_installed()
        if health.get('build_id') != baseline['build_id']:
            raise DeployError('RECOVERY_WRONG_RELEASE')
        return {'rollback': 'restored' if touched else 'not_needed', 'health': 'ready',
                'previous_build': baseline['build_id']}
