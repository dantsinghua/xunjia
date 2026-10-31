"""Return diagnostics after the originating worker releases its task lock."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'app'),str(Path(__file__).resolve().parents[1])]


def arm(prefix,bundle,job_id):
    if not re.fullmatch('[0-9a-f]{32}',job_id):return
    subprocess.run(['/usr/bin/systemd-run','--unit=xunjia-upgrade-log-'+job_id,
        '--property=Type=simple','--property=UMask=0077',sys.executable,'-I','-B',
        str(Path(bundle)/'deploy/upgrade_log_return.py'),'--prefix',str(prefix),'--job',job_id],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)


def run(prefix,job_id):
    from server.upgrade_control import UpgradeStore
    from server.upgrade_automation import upload_log
    if not re.fullmatch('[0-9a-f]{32}',job_id):raise ValueError('INVALID_UPGRADE_JOB')
    store=UpgradeStore(Path(prefix)/'state/upgrade');deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        try:
            with store.lock():
                with store.db() as db:row=db.execute('SELECT state FROM job WHERE id=?',(job_id,)).fetchone()
                if not row:return
                if row[0] in ('succeeded','failed','unknown'):
                    result=upload_log(prefix,job_id)
                    if result.get('state')!='failed':return
        except BlockingIOError:pass
        time.sleep(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prefix',type=Path,required=True);parser.add_argument('--job',required=True)
    args=parser.parse_args();os.umask(0o077);run(args.prefix,args.job)
