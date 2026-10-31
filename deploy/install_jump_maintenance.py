"""Offline installer for the dedicated jump-host maintenance service (Python 3.7+)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import fcntl

SERVICE = 'xunjia-network-maintenance.service'
PROGRAM = 'opt/xunjia-network-maintenance/jump_maintenance.py'
UNIT = 'etc/systemd/system/' + SERVICE
OWNER = 'opt/xunjia-network-maintenance/INSTALL.json'
STATE = 'var/lib/xunjia-network-maintenance'


class InstallError(ValueError):
    pass


def command(args):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError('COMMAND_UNAVAILABLE_OR_TIMEOUT') from None
    return result.returncode, result.stdout


def required(runner, args):
    code, output = runner(args)
    if code:
        raise InstallError('SERVICE_OPERATION_FAILED')
    return output


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, name):
    path = root / name
    for part in [path] + list(path.parents):
        if part.is_symlink():
            raise InstallError('SYMLINK_PATH_REFUSED')
        if part == root:
            break
    return path


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.maintenance-', dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unit_bytes(python):
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', python):
        raise InstallError('PYTHON_PATH_UNSUPPORTED')
    return ('''[Unit]
Description=Xunjia jump host network maintenance
Wants=network-online.target
After=network-online.target nginx.service

[Service]
Type=simple
ExecStart=%s -I -B /opt/xunjia-network-maintenance/jump_maintenance.py --daemon --state-dir /var/lib/xunjia-network-maintenance
Restart=on-failure
RestartSec=15
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
''' % python).encode('ascii')


def ownership(root):
    paths = {n: safe_path(root, n) for n in (PROGRAM, UNIT, OWNER)}
    if not paths[OWNER].exists():
        if any(paths[n].exists() for n in (PROGRAM, UNIT)):
            raise InstallError('UNOWNED_EXISTING_INSTALLATION')
        return paths, None
    try:
        owner = json.loads(paths[OWNER].read_text())
        if owner['schema'] != 1 or set(owner['files']) != {PROGRAM, UNIT}:
            raise ValueError()
        for name, sha in owner['files'].items():
            if digest(paths[name].read_bytes()) != sha:
                raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError('INSTALLED_FILES_CHANGED') from None
    return paths, owner


def service_state(runner):
    active = runner(['systemctl', 'is-active', SERVICE])[1].strip()
    enabled = runner(['systemctl', 'is-enabled', SERVICE])[1].strip()
    if active not in ('active', 'inactive', 'failed', 'unknown', ''):
        raise InstallError('MAINTENANCE_SERVICE_TRANSITION_IN_PROGRESS')
    if enabled not in ('enabled', 'disabled', 'not-found', ''):
        raise InstallError('MAINTENANCE_ENABLE_STATE_UNSUPPORTED')
    return active == 'active', enabled == 'enabled'


def await_ready(root, runner, started):
    deadline = time.monotonic() + 240
    path = safe_path(root, STATE + '/status.json')
    while time.monotonic() < deadline:
        if not service_state(runner)[0]:
            raise InstallError('MAINTENANCE_SERVICE_EXITED')
        pid = required(runner, ['systemctl', 'show', SERVICE, '--property=MainPID', '--value']).strip()
        try:
            value = json.loads(path.read_text())
            if (pid.isdigit() and int(pid) > 0 and value.get('pid') == int(pid)
                    and value.get('updated_at', 0) >= started and value.get('phase') == 'ready'):
                return
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(1)
    raise InstallError('MAINTENANCE_STATUS_NOT_READY')


def change(bundle, *, apply=False, uninstall=False, root=Path('/'), runner=command,
           python=None, readiness=await_ready):
    root = Path(root).absolute()
    bundle = Path(bundle).absolute()
    python = python or str(Path(sys.executable).resolve())
    source = bundle / 'jump_maintenance.py'
    if source.is_symlink() or not source.is_file():
        raise InstallError('BUNDLE_RUNTIME_REQUIRED')
    compile(source.read_bytes(), str(source), 'exec')
    # Uninstall must work even when nginx is unavailable.
    addresses = required(runner, ['ip', '-o', '-4', 'addr', 'show'])
    if re.search(r'\binet\s+172\.26\.64\.36/', addresses) is None:
        raise InstallError('WRONG_HOST')
    # Runtime owns unit, effective nginx configuration and dependency checks.
    if not uninstall:
        required(runner, [python, '-I', '-B', str(source), '--check'])
    paths, owner = ownership(root)
    previous_active, previous_enabled = service_state(runner)
    if owner is None and (previous_active or previous_enabled):
        raise InstallError('UNOWNED_MAINTENANCE_SERVICE')
    payload = {PROGRAM: source.read_bytes(), UNIT: unit_bytes(python)}
    metadata = {'schema': 1, 'files': {n: digest(v) for n, v in payload.items()}}
    report = {'service': SERVICE, 'nginx_configuration_changed': False,
              'state_preserved': True, 'payload_sha256': metadata['files'][PROGRAM]}
    if not apply:
        return dict(report, status='CHECK_PASSED_NO_CHANGES')
    state = safe_path(root, STATE)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = safe_path(root, STATE + '/install.lock')
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('INSTALL_ALREADY_RUNNING') from None
        current_paths, current_owner = ownership(root)
        if current_owner != owner or service_state(runner) != (previous_active, previous_enabled):
            raise InstallError('INSTALLATION_CHANGED_DURING_CHECK')
        old = {n: p.read_bytes() if p.exists() else None for n, p in current_paths.items()}
        if uninstall and owner is None:
            return dict(report, status='ALREADY_UNINSTALLED')
        if not uninstall and owner == metadata and previous_active and previous_enabled:
            return dict(report, status='ALREADY_INSTALLED_AND_RUNNING')
        backup_root = safe_path(root, STATE + '/install-backups')
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=str(backup_root)))
        atomic(backup/'before.json', json.dumps({'active': previous_active, 'enabled': previous_enabled,
               'files': {n: digest(v) if v is not None else None for n, v in old.items()}}).encode())
        for n, data in old.items():
            if data is not None:
                atomic(backup / Path(n).name, data)
        try:
            if previous_active:
                required(runner, ['systemctl', 'stop', SERVICE])
            if uninstall:
                required(runner, ['systemctl', 'disable', SERVICE])
                for n in (PROGRAM, UNIT, OWNER):
                    paths[n].unlink()
            else:
                for n, data in payload.items():
                    atomic(paths[n], data, 0o700 if n == PROGRAM else 0o644)
                atomic(paths[OWNER], json.dumps(metadata, sort_keys=True).encode())
            required(runner, ['systemctl', 'daemon-reload'])
            if not uninstall:
                required(runner, ['systemctl', 'enable', SERVICE])
                started = time.time()
                required(runner, ['systemctl', 'start', SERVICE])
                if service_state(runner) != (True, True):
                    raise InstallError('MAINTENANCE_SERVICE_NOT_RUNNING')
                readiness(root, runner, started)
        except Exception:
            # Only this service's exact files and activation state are restored.
            recovery_ok = runner(['systemctl', 'stop', SERVICE])[0] == 0
            for n, data in old.items():
                if data is None:
                    if paths[n].exists():
                        paths[n].unlink()
                else:
                    atomic(paths[n], data, 0o700 if n == PROGRAM else 0o644 if n == UNIT else 0o600)
            recovery_ok = runner(['systemctl', 'daemon-reload'])[0] == 0 and recovery_ok
            activation = 'enable' if previous_enabled else 'disable'
            recovery_ok = runner(['systemctl', activation, SERVICE])[0] == 0 and recovery_ok
            if previous_active:
                recovery_ok = runner(['systemctl', 'start', SERVICE])[0] == 0 and recovery_ok
            if not recovery_ok:
                raise InstallError('INSTALL_FAILED_RECOVERY_REQUIRES_OPERATOR') from None
            raise InstallError('INSTALL_FAILED_PREVIOUS_FILES_RESTORED') from None
        return dict(report, status='UNINSTALLED_STATE_RETAINED' if uninstall else 'INSTALLED_AND_RUNNING',
                    backup=str(backup))
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--check', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    if sys.version_info < (3, 7):
        raise InstallError('PYTHON_37_OR_NEWER_REQUIRED')
    if os.geteuid() != 0:
        raise InstallError('ROOT_REQUIRED')
    os.umask(0o077)
    print(json.dumps(change(Path(__file__).resolve().parent, apply=args.apply,
                            uninstall=args.uninstall), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc) if isinstance(exc, InstallError) else 'INSTALL_FAILED', file=sys.stderr)
        sys.exit(2)
