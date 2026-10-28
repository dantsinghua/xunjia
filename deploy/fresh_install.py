"""Explicit fresh installation into a proven, dedicated Xunjia prefix.

Database mutation runs only in the newly installed interpreter. This controller
uses the standard library and never imports a database driver or old site config.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit, unquote

from common import (DeployError, atomic_json, config_fingerprint, effective_env,
                    fsync_dir, read_env, safe_prefix, safe_service, validate_config,
                    verify_bundle)


def require_system():
    if os.geteuid() != 0:
        raise DeployError('FRESH_ROOT_REQUIRED')
    if not shutil.which('systemctl') or not Path('/run/systemd/system').is_dir():
        raise DeployError('FRESH_SYSTEMD_REQUIRED')


def systemctl(*arguments):
    try:
        result = subprocess.run(['systemctl', *arguments], capture_output=True,
                                text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError('FRESH_SYSTEMCTL_UNAVAILABLE') from None
    if result.returncode:
        raise DeployError('FRESH_SYSTEMCTL_FAILED')
    return result.stdout.strip()


def verify_service(manager):
    service = manager.service
    safe_service(service.name)
    if not shutil.which('systemctl'):
        raise DeployError('FRESH_SYSTEMD_REQUIRED')
    if not service.exists():
        if (Path('/etc/systemd/system') / service.name).exists():
            raise DeployError('FRESH_UNLOADED_UNIT_UNPROVEN')
        return
    command = systemctl('show', service.name, '--property=ExecStart', '--value')
    paths = re.findall(r'(?:^|[ {;])path=([^ ;}]+)', command)
    expected = str(manager.prefix / 'app/deploy/start.sh')
    wrapped = paths in (['/bin/bash'], ['/usr/bin/bash'])
    if paths != [expected] and not wrapped:
        raise DeployError('FRESH_SERVICE_ENTRYPOINT_UNPROVEN')
    argv = re.findall(r'argv\[\]=([^;]+)', command)
    expected_arguments = paths[0] + ' ' + expected if wrapped else expected
    if len(argv) != 1 or argv[0].strip() != expected_arguments:
        raise DeployError('FRESH_SERVICE_ARGUMENTS_UNPROVEN')
    if wrapped:
        script = Path(expected)
        if not script.is_file() or not script.resolve().is_relative_to(manager.prefix):
            raise DeployError('FRESH_SERVICE_ENTRYPOINT_UNPROVEN')
    working = systemctl('show', service.name, '--property=WorkingDirectory', '--value')
    allowed_working = ('', '/', str(manager.prefix / 'app')) if wrapped else (str(manager.prefix / 'app'),)
    if working not in allowed_working:
        raise DeployError('FRESH_SERVICE_DIRECTORY_UNPROVEN')
    if systemctl('show', service.name, '--property=DropInPaths', '--value'):
        raise DeployError('FRESH_SERVICE_DROPINS_UNPROVEN')
    state = systemctl('show', service.name, '--property=ActiveState', '--value')
    if state not in ('active', 'inactive', 'failed'):
        raise DeployError('SERVICE_TRANSITION_IN_PROGRESS')


def verify_port(manager, values):
    port = int(values['XUNJIA_PORT'])
    listening = False
    for name in ('tcp', 'tcp6'):
        path = Path('/proc/net') / name
        if not path.exists() and name == 'tcp6':
            continue
        try:
            for line in path.read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[3] == '0A' and int(fields[1].split(':')[1], 16) == port:
                    listening = True
        except (OSError, ValueError, IndexError):
            raise DeployError('FRESH_PORT_OWNERSHIP_UNVERIFIED') from None
    if listening and not manager.service.owns_listener(port):
        raise DeployError('FRESH_PORT_IN_USE_BY_OTHER_PROCESS')


def identity(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise DeployError('FRESH_DIRECTORY_IDENTITY_INVALID')
    return [info.st_dev, info.st_ino]


def reject_mounts(prefix):
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        raise DeployError('FRESH_MOUNTS_UNVERIFIED') from None
    for line in lines:
        mount = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), line.split()[4])
        if Path(mount).is_relative_to(prefix):
            raise DeployError('FRESH_PREFIX_CONTAINS_MOUNT')


def verify_old_prefix(prefix):
    if not prefix.exists():
        return None
    original = identity(prefix)
    reject_mounts(prefix)
    allowed = {'app', 'venv', 'python', 'config', 'state', 'current', 'releases',
               'backups', 'deployments', 'logs', '.xunjia-deploy.lock'}
    if any(path.name not in allowed for path in prefix.iterdir()):
        raise DeployError('FRESH_UNKNOWN_PREFIX_CONTENT')
    app = prefix / 'app/server/app.py'
    python = prefix / 'venv/bin/python'
    nested = prefix / 'app/app/server/app.py'
    if app.is_file() and nested.is_file():
        raise DeployError('FRESH_OLD_LAYOUT_AMBIGUOUS')
    if nested.is_file():
        app = nested
        python = prefix / 'app/.venv/bin/python'
        markers = (app, python, prefix / 'app/deploy/start.sh',
                   prefix / 'app/VERSION.json', prefix / 'app/SOURCE-HASHES.json')
        if any(not path.is_file() or not path.resolve().is_relative_to(prefix) for path in markers):
            raise DeployError('FRESH_OLD_LAYOUT_UNPROVEN')
        for metadata in markers[-2:]:
            try:
                if metadata.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError()
                value = json.loads(metadata.read_text(encoding='utf-8-sig'))
                if not isinstance(value, dict) or not value:
                    raise ValueError()
            except (OSError, ValueError):
                raise DeployError('FRESH_OLD_PACKAGE_METADATA_UNPROVEN') from None
    if not app.is_file() or not app.resolve().is_relative_to(prefix) or not python.is_file():
        raise DeployError('FRESH_OLD_LAYOUT_UNPROVEN')
    text = app.read_text(encoding='utf8')
    if 'XUNJIA_DATABASE_URL' not in text or 'create_app' not in text:
        raise DeployError('FRESH_OLD_APPLICATION_UNPROVEN')
    return original


def database_audit_root(prefix):
    token = hashlib.sha256(str(prefix).encode()).hexdigest()[:16]
    return prefix.parent / ('.xunjia-fresh-database-' + token)


def verify_database_history(prefix):
    root = database_audit_root(prefix)
    if root.is_symlink() or root.exists() and not root.is_dir():
        raise DeployError('FRESH_DATABASE_AUDIT_PATH_UNSAFE')
    if not root.exists():
        return
    for state in root.glob('database-fresh-*.json'):
        if state.is_symlink() or not state.is_file():
            raise DeployError('FRESH_DATABASE_AUDIT_PATH_UNSAFE')
        try:
            prior = json.loads(state.read_text(encoding='utf8'))
        except (OSError, ValueError):
            raise DeployError('FRESH_DATABASE_HISTORY_INVALID') from None
        if not isinstance(prior, dict) or prior.get('status') != 'VERIFIED':
            raise DeployError('FRESH_DATABASE_RECONCILIATION_REQUIRED')


def plan(manager):
    prefix = safe_prefix(manager.prefix)
    safe_service(manager.service.name)
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(prefix)):
        raise DeployError('FRESH_PREFIX_CHARACTERS_UNSUPPORTED')
    bundle = manager.bundle.resolve()
    if bundle.is_relative_to(prefix) or prefix.is_relative_to(bundle):
        raise DeployError('FRESH_PACKAGE_PREFIX_OVERLAP')
    version = verify_bundle(bundle)
    values = effective_env(read_env(bundle / 'config/xunjia.env'), prefix, prefix / 'app')
    validate_config(values, ready=True)
    if urlsplit(values['XUNJIA_DATABASE_URL']).path != '/xunjia_business':
        raise DeployError('FRESH_DATABASE_MUST_BE_XUNJIA_BUSINESS')
    if any('REPLACE_' in value for value in values.values()):
        raise DeployError('FRESH_TEMPLATE_VALUES_UNFILLED')
    if Path(values['XUNJIA_STATE_DIR']) != prefix / 'state':
        raise DeployError('FRESH_STATE_MUST_BE_DEDICATED')
    if Path(values['XUNJIA_WEB_DIST']) != prefix / 'app/frontend/dist':
        raise DeployError('FRESH_WEB_MUST_BE_DEDICATED')
    if values.get('XUNJIA_QUOTE_SOURCE')=='qb':
        db=urlsplit(values['XUNJIA_DATABASE_URL'])
        expected=(db.hostname,db.port or 3306,unquote(db.username or ''),unquote(db.password or ''),'xunjia_business')
        try:
            actual=(values.get('QB_MYSQL_HOST'),int(values.get('QB_MYSQL_PORT','3306')),
                    values.get('QB_MYSQL_USER'),values.get('QB_MYSQL_PASSWORD'),values.get('QB_MYSQL_DB'))
        except ValueError:raise DeployError('FRESH_QUOTE_DATABASE_NOT_SHARED') from None
        if actual!=expected:raise DeployError('FRESH_QUOTE_DATABASE_NOT_SHARED')
        if Path(values.get('QB_CONFIG_DIR',''))!=prefix/'config/innodealing':
            raise DeployError('FRESH_QB_CONFIG_MUST_BE_DEDICATED')
        if values.get('XUNJIA_QUOTE_CACHE'):
            cache=Path(values['XUNJIA_QUOTE_CACHE'])
            if '..' in cache.parts or not cache.is_relative_to(prefix/'state'):
                raise DeployError('FRESH_QUOTE_CACHE_MUST_BE_DEDICATED')
    for key in ('XUNJIA_AGGREGATION_PROFILE', 'XUNJIA_INSTITUTION_CONFIG', 'XUNJIA_REFERENCE_CONFIG', 'QB_DICTIONARY_CONFIG'):
        if not values.get(key):
            continue
        configured = Path(values[key])
        if not configured.is_absolute() or '..' in configured.parts or not configured.is_relative_to(prefix / 'config'):
            raise DeployError('FRESH_CONFIG_PATH_OUTSIDE_PREFIX', key)
        source = bundle / 'config' / configured.relative_to(prefix / 'config')
        if not source.is_file() or source.is_symlink():
            raise DeployError('FRESH_SITE_CONFIG_MISSING', key)
    verify_database_history(prefix)
    original = verify_old_prefix(prefix)
    verify_service(manager)
    verify_port(manager, values)
    return version, values, original, config_fingerprint(bundle / 'config')


@contextmanager
def fresh_lock(prefix):
    token = hashlib.sha256(str(prefix).encode()).hexdigest()[:16]
    lock = prefix.parent / ('.xunjia-fresh-' + token + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('DEPLOY_BUSY') from None
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def child_step(manager, evidence, name, script, arguments):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('XUNJIA_', 'QB_', 'PYTHON', 'PIP_'))}
    env.update(PYTHONPATH=str(manager.prefix / 'app'), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    command = [str(manager.prefix / 'venv/bin/python'), str(manager.prefix / 'app/deploy' / script), *arguments]
    try:
        with (evidence / (name + '.log')).open('x', encoding='utf8') as output:
            result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError('FRESH_CHILD_UNAVAILABLE', name) from None
    if result.returncode:
        raise DeployError('FRESH_CHILD_FAILED', name)


def runtime_check(manager, evidence):
    child_step(manager, evidence, 'runtime-check', 'runtime_check.py',
               ['--bundle', str(manager.bundle), '--prefix', str(manager.prefix), '--site-config'])


def database_step(manager, evidence, apply):
    stage = 'database-apply' if apply else 'database-check'
    root = database_audit_root(manager.prefix)
    if root.is_symlink():
        raise DeployError('FRESH_DATABASE_AUDIT_PATH_UNSAFE')
    root.mkdir(mode=0o700, exist_ok=True)
    child_step(manager, evidence, 'database-apply' if apply else 'database-check', 'database_upgrade.py',
               ['--config', str(manager.prefix / 'config/xunjia.env'),
                '--evidence', str(root / (evidence.name + '-' + stage)),
                '--apply' if apply else '--check', '--service', manager.service.name])


def install_unit(manager):
    name = safe_service(manager.service.name)
    target = Path('/etc/systemd/system') / name
    if target.is_symlink():
        raise DeployError('FRESH_UNIT_SYMLINK_FORBIDDEN')
    prefix = manager.prefix
    text = (f'[Unit]\nDescription=Xunjia Agent\nAfter=network-online.target\nWants=network-online.target\n'
            f'[Service]\nType=simple\nUser=root\nGroup=root\nWorkingDirectory={prefix}/app\n'
            f'ExecStart={prefix}/app/deploy/start.sh\nRestart=on-failure\nRestartSec=3\n'
            'UMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n[Install]\nWantedBy=multi-user.target\n')
    fd, temporary = tempfile.mkstemp(prefix='.' + name + '-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        fsync_dir(target.parent)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    systemctl('daemon-reload')


def enable_service(manager):
    systemctl('enable', safe_service(manager.service.name))


def remove_isolated(path, expected, parent):
    if (path.parent != parent or not path.name.startswith('.xunjia-old-')
            or identity(path) != expected):
        raise DeployError('FRESH_ISOLATION_IDENTITY_CHANGED')
    reject_mounts(path)
    shutil.rmtree(path)
    fsync_dir(parent)


def run_fresh_install(manager, *, apply=False):
    version, values, original, fingerprint = plan(manager)
    if not apply:
        return {'mode': 'check', 'status': 'fresh_plan_valid', 'build_id': version['build_id'],
                'database': 'xunjia_business', 'prefix': str(manager.prefix),
                'service': manager.service.name, 'old_prefix_present': original is not None}
    require_system()
    with fresh_lock(manager.prefix):
        # Re-read all mutable site input and ownership immediately before stopping.
        version, values, original, fingerprint = plan(manager)
        evidence = Path(tempfile.mkdtemp(prefix='.xunjia-fresh-evidence-', dir=manager.prefix.parent))
        os.chmod(evidence, 0o700)
        record = {'operation': 'fresh-install', 'build_id': version['build_id'],
                  'prefix': str(manager.prefix), 'service': manager.service.name,
                  'database_started': False, 'phase': 'PREPARED',
                  'database_check_evidence': str(database_audit_root(manager.prefix) / (evidence.name + '-database-check')),
                  'database_apply_evidence': str(database_audit_root(manager.prefix) / (evidence.name + '-database-apply'))}
        def phase(name):
            record['phase'] = name
            atomic_json(evidence / 'result.json', record)
        phase('PREPARED')
        isolated = None
        stopped = False
        try:
            phase('STOPPING')
            if manager.service.exists():
                manager.service.stop()
            stopped = True
            if manager.service.active():
                raise DeployError('FRESH_SERVICE_NOT_STOPPED')
            verify_port(manager, values)
            phase('STOPPED')
            if original is not None:
                with manager.lock():
                    if identity(manager.prefix) != original:
                        raise DeployError('FRESH_PREFIX_CHANGED')
                    isolated = Path(tempfile.mkdtemp(prefix='.xunjia-old-', dir=manager.prefix.parent))
                    record['isolated_prefix'] = str(isolated)
                    record['isolated_identity'] = original
                    phase('ISOLATING_OLD_PREFIX')
                    # Replace only our empty reserved destination with the proven old prefix.
                    os.replace(manager.prefix, isolated)
                    os.chmod(isolated, 0o700)
                    fsync_dir(manager.prefix.parent)
                    phase('OLD_ISOLATED')
            if config_fingerprint(manager.bundle / 'config') != fingerprint:
                raise DeployError('FRESH_SITE_CONFIG_CHANGED')
            installed = manager.install()
            if installed.get('status') != 'installed_stopped' or manager.service.active():
                raise DeployError('FRESH_INSTALL_DID_NOT_REMAIN_STOPPED')
            record['release_id'] = installed.get('release_id')
            phase('INSTALLED_STOPPED')
            with manager.lock():
                if config_fingerprint(manager.prefix / 'config') != fingerprint:
                    raise DeployError('FRESH_INSTALLED_CONFIG_CHANGED')
                runtime_check(manager, evidence)
                database_step(manager, evidence, False)
                phase('DATABASE_CHECKED')
                record['database_started'] = True
                phase('DATABASE_APPLYING')
                database_step(manager, evidence, True)
                phase('DATABASE_READY')
                verify_service(manager)
                install_unit(manager)
                manager.service.start()
                manager.check_health(manager.current(), values)
                phase('SERVICE_READY')
                enable_service(manager)
                if isolated is not None:
                    remove_isolated(isolated, original, manager.prefix.parent)
                phase('COMMITTED')
            return {'status': 'fresh_running_verified', 'build_id': version['build_id'],
                    'evidence': str(evidence), 'old_prefix_removed': isolated is not None}
        except BaseException as exc:
            # Never restore old code or old data after a fresh database operation.
            if stopped:
                try:
                    if manager.service.exists():
                        manager.service.stop()
                    record['service_stopped'] = not manager.service.active()
                except BaseException:
                    record['service_stopped'] = False
            record['failed_phase'] = record['phase']
            record['error'] = exc.code if isinstance(exc, DeployError) else type(exc).__name__
            phase('FAILED_STOPPED' if record.get('service_stopped') else 'FAILED_STOP_UNVERIFIED')
            raise DeployError('FRESH_INSTALL_FAILED', str(evidence)) from None
