"""Bounded exact-unit installer lifecycle, separate from the recovery worker."""
from pathlib import Path
import re
import subprocess
import time


class InstallerNotStopped(RuntimeError):
    pass


def unit_status(unit):
    result = subprocess.run(['/usr/bin/systemctl', 'show', unit],
                            capture_output=True, text=True, timeout=10)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode and fields.get('LoadState') != 'not-found':
        raise InstallerNotStopped('INSTALLER_STATUS_UNAVAILABLE')
    return fields


def stop_installer(unit):
    try:
        result = subprocess.run(['/usr/bin/systemctl', 'stop', unit],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        status = unit_status(unit)
        if status.get('LoadState') == 'not-found':
            return
        if result.returncode == 0 and status.get('ActiveState') in ('inactive', 'failed') and status.get('MainPID', '0') == '0':
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise InstallerNotStopped('INSTALLER_STOP_NOT_CONFIRMED')


def run_install(prefix, job_id, python, wrapper, timeout=900, progress=None):
    if not re.fullmatch('[0-9a-f]{32}', job_id):
        raise ValueError('INVALID_UPGRADE_JOB')
    unit = 'xunjia-upgrade-apply-' + job_id + '.service'
    command = ['/usr/bin/systemd-run', '--unit=' + unit, '--property=Type=simple',
               '--property=RemainAfterExit=yes', '--property=Restart=no',
               '--property=KillMode=control-group', '--property=TimeoutStopSec=20',
               '--property=UMask=0077', str(python), '-I', '-B', str(wrapper),
               '--prefix', str(prefix), '--job', job_id]
    try:
        launched = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if launched.returncode:
            stop_installer(unit)
            raise ValueError('INSTALLER_START_FAILED')
        deadline = time.monotonic() + timeout
        while True:
            if progress is not None:
                try:progress()
                except Exception:pass  # Diagnostics cannot control the installer.
            status = unit_status(unit)
            if status.get('SubState') in ('exited', 'failed', 'dead') or status.get('ActiveState') in ('inactive', 'failed'):
                code = status.get('ExecMainStatus')
                result = int(code) if code and code.isdigit() else 1
                if status.get('Result') != 'success' or status.get('ExecMainCode') not in ('1', 'exited'):
                    result = result or 1
                stop_installer(unit)
                return result
            if status.get('LoadState') == 'not-found':
                stop_installer(unit)
                raise ValueError('INSTALLER_RESULT_MISSING')
            if time.monotonic() >= deadline:
                stop_installer(unit)
                return 124
            time.sleep(1)
    except InstallerNotStopped:
        raise
    except (OSError, subprocess.TimeoutExpired):
        stop_installer(unit)
        raise ValueError('INSTALLER_CONTROL_FAILED') from None
