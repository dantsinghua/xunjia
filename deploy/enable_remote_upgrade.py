"""Install a reviewed local upgrade channel, or reconcile an interrupted job."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic_json, safe_prefix, verify_bundle, read_env, effective_env
from server.upgrade_control import UpgradeStore, load_channel, validate_channel


def current_bundle(prefix, current):
    from incremental import bundle_candidates
    matches = []
    for path in bundle_candidates(prefix):
        try:
            if (path / 'VERSION.json').is_file() and json.loads((path / 'VERSION.json').read_text()).get('build_id') == current:
                matches.append(path)
        except (ValueError, OSError, TypeError, AttributeError):
            continue
    base = None
    for path in sorted(matches):
        try:
            if verify_bundle(path)['build_id'] == current:
                base = path
                break
        except (ValueError, OSError, RuntimeError):
            continue
    if base is None:
        raise ValueError('CURRENT_INTACT_BUNDLE_REQUIRED')
    return base


def enable(prefix, channel_file):
    config = json.loads(Path(channel_file).read_text())
    if set(config) != {'site_id', 'queue', 'public_key', 'operators'}:
        raise ValueError('CHANNEL_FIELDS_INVALID')
    validate_channel(config)
    current = json.loads((prefix / 'app/VERSION.json').read_text())['build_id']
    base = current_bundle(prefix, current)
    store = UpgradeStore(prefix / 'state/upgrade')
    with store.lock():
        target = prefix / 'config/upgrade-channel.json'
        if target.exists():
            existing = load_channel(prefix)
            if any(existing.get(key) != value for key, value in config.items()):
                raise ValueError('EXISTING_CHANNEL_DIFFERENT_NO_OVERWRITE')
        else:
            atomic_json(target, {**config, 'base_bundle': str(base)})
        load_channel(prefix)
        store.set('base_bundle', str(base))
    print(json.dumps({'status': 'UPGRADE_CHANNEL_ENABLED', 'build_id': current,
                      'queue': config['queue'], 'all_signed_in_users': '*' in config['operators'],
                      'retention_days': 30}))


def reconcile(prefix, job_id, outcome):
    if not re.fullmatch('[a-f0-9]{32}', job_id):
        raise ValueError('INVALID_JOB')
    store = UpgradeStore(prefix / 'state/upgrade')
    with store.lock():
        job = store.latest()
        if not job or job['id'] != job_id or job['state'] not in ('unknown', 'queued', 'downloading', 'verifying', 'installing'):
            raise ValueError('NO_MATCHING_INTERRUPTED_JOB')
        active = subprocess.run(['/usr/bin/systemctl', 'show', 'xunjia-upgrade-' + job_id,
                                 '--property=ActiveState'], capture_output=True, text=True, timeout=10)
        if active.returncode or active.stdout.strip() not in ('ActiveState=inactive', 'ActiveState=failed'):
            raise ValueError('WORKER_NOT_PROVEN_STOPPED')
        from manager import Manager
        current = json.loads((prefix / 'app/VERSION.json').read_text())['build_id']
        base = current_bundle(prefix, current)
        manager = Manager(base, prefix)
        with manager.lock():
            manager.ensure_idle()
            current = json.loads((prefix / 'app/VERSION.json').read_text())['build_id']
            if outcome == 'succeeded':
                if not job['target_build'] or current != job['target_build']:
                    raise ValueError('TARGET_NOT_INSTALLED')
                manager.verify_installed(liveness=True)
                store.set('base_bundle', str(base))
            elif job['target_build'] and current == job['target_build']:
                raise ValueError('TARGET_IS_INSTALLED_RECONCILE_SUCCESS_INSTEAD')
            store.update(job_id, outcome, code='OPERATOR_RECONCILED')
    print(json.dumps({'status': outcome, 'job': job_id, 'download_replayed': False}))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path('/home/xunjia'))
    parser.add_argument('--channel', type=Path)
    parser.add_argument('--resolve-job')
    parser.add_argument('--outcome', choices=('succeeded', 'failed'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('ROOT_REQUIRED')
    prefix = safe_prefix(args.prefix)
    if args.channel and not args.resolve_job and not args.outcome:
        enable(prefix, args.channel)
    elif args.resolve_job and args.outcome and not args.channel:
        reconcile(prefix, args.resolve_job, args.outcome)
    else:
        raise SystemExit('CHOOSE_CHANNEL_OR_RESOLVE_JOB')
