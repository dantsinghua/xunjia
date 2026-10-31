"""Legacy-page compatibility guard, independent of the old worker's cgroup."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'app'),str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from common import atomic_json, verify_bundle
from upgrade_recovery import snapshot_baseline, recover_job
from upgrade_supervisor import unit_status, stop_installer
from server.upgrade_control import UpgradeStore
from server.upgrade_diagnostics import event


def arm(prefix, bundle, job_id):
    import subprocess
    stage=Path(prefix)/'state/upgrade/jobs'/job_id
    if not re.fullmatch('[0-9a-f]{32}',job_id) or stage.resolve()!=stage:
        raise ValueError('INVALID_UPGRADE_JOB')
    baseline=snapshot_baseline(Path(prefix))
    atomic_json(stage/'baseline.json',baseline)
    result=subprocess.run(['/usr/bin/systemd-run','--unit=xunjia-upgrade-monitor-'+job_id,
        '--property=Type=simple','--property=UMask=0077','--property=Restart=on-failure',
        sys.executable,'-I','-B',str(Path(bundle)/'deploy/upgrade_monitor.py'),
        '--prefix',str(prefix),'--bundle',str(bundle),'--job',job_id],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
    if result.returncode:raise ValueError('UPGRADE_MONITOR_START_FAILED')
    return stage


def monitor(prefix,bundle,job_id,timeout=900):
    prefix,bundle=Path(prefix),Path(bundle)
    if not re.fullmatch('[0-9a-f]{32}',job_id):raise ValueError('INVALID_UPGRADE_JOB')
    stage=prefix/'state/upgrade/jobs'/job_id
    if stage.resolve()!=stage or (stage/'baseline.json').is_symlink():raise ValueError('INVALID_UPGRADE_STORAGE')
    baseline=json.loads((stage/'baseline.json').read_text())
    target=verify_bundle(bundle)['build_id']
    store=UpgradeStore(prefix/'state/upgrade')
    deadline=time.monotonic()+timeout
    unit='xunjia-upgrade-'+job_id+'.service'
    try:
        while True:
            path=stage/'capsule-result.json'
            if path.is_file() and not path.is_symlink():
                result=json.loads(path.read_text())
                if result.get('success') is True:
                    # Old worker owns its result write; only retain diagnostics.
                    event(store,job_id,'succeeded','UPGRADE_SUCCEEDED',target_build=target,health='ready')
                    return
                break
            status=unit_status(unit)
            if status.get('LoadState')=='not-found' or status.get('ActiveState') in ('inactive','failed'):break
            if time.monotonic()>=deadline:break
            time.sleep(1)
        stop_installer(unit)  # Stops only this old worker and its installer children.
        restored=recover_job(prefix,job_id,baseline)
        with store.lock(blocking=True):
            store.update(job_id,'failed',code='UPGRADE_ROLLBACK_SUCCEEDED' if restored['rollback']=='restored' else 'UPGRADE_FAILED_OLD_VERSION_HEALTHY')
            store.detail(job_id,phase='failed',**restored)
            event(store,job_id,'failed','UPGRADE_ROLLBACK_SUCCEEDED',**restored)
    except Exception:
        try:
            with store.lock():
                store.update(job_id,'unknown',code='UPGRADE_ROLLBACK_FAILED')
                store.detail(job_id,phase='failed',rollback='failed',health='unverified')
        except BlockingIOError:pass
        event(store,job_id,'unknown','UPGRADE_ROLLBACK_FAILED')
    finally:
        try:
            from server.upgrade_automation import upload_log
            upload_log(prefix,job_id)
        except Exception:pass


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',type=Path,required=True);parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--job',required=True)
    a=parser.parse_args();os.umask(0o077);monitor(a.prefix,a.bundle,a.job)
