"""Offline incremental capsule. Reuses verified assets and bounded migration plans.

The builder fills CAPSULE/PAYLOAD. Default mode prepares and checks, --apply
switches the dedicated Xunjia release and mounts /xunjia/ transactionally.
"""
from __future__ import annotations
import argparse
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zlib

CAPSULE = None
PAYLOAD = {}

class IncrementalError(ValueError):
    pass

def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()

def relative(name):
    if not isinstance(name, str) or not name or '\\' in name or any(ord(c) < 32 for c in name):
        raise IncrementalError('UNSAFE_PACKAGE_PATH')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != name:
        raise IncrementalError('UNSAFE_PACKAGE_PATH')
    return path

def manifest(root):
    result = {}
    for line in (Path(root) / 'SHA256SUMS.txt').read_text(encoding='utf8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match:
            raise IncrementalError('INVALID_MANIFEST')
        name = relative(match[2]).as_posix()
        if name in result or name in ('SHA256SUMS.txt', 'config/xunjia.env'):
            raise IncrementalError('INVALID_MANIFEST')
        result[name] = match[1]
    if 'VERSION.json' not in result:
        raise IncrementalError('INVALID_MANIFEST')
    return result

def verify_tree(root, expected_build, expected_manifest):
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise IncrementalError('PACKAGE_PATH_INVALID')
    for item in root.rglob('*'):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise IncrementalError('PACKAGE_LINK_OR_SPECIAL_FILE')
    if digest(root / 'SHA256SUMS.txt') != expected_manifest:
        raise IncrementalError('PACKAGE_MANIFEST_MISMATCH')
    names = manifest(root)
    for name, wanted in names.items():
        path = root / name
        if not path.is_file() or digest(path) != wanted:
            raise IncrementalError('PACKAGE_INPUT_CHANGED')
    version = json.loads((root / 'VERSION.json').read_text(encoding='utf8'))
    if version.get('build_id') != expected_build:
        raise IncrementalError('PACKAGE_BUILD_MISMATCH')
    for item in root.rglob('*'):
        if item.is_file():
            name = item.relative_to(root).as_posix()
            if name not in names and name != 'SHA256SUMS.txt' and not name.startswith('config/'):
                raise IncrementalError('UNLISTED_PACKAGE_INPUT')
    return names

def checked_capsule():
    if not isinstance(CAPSULE, dict) or CAPSULE.get('schema') != 1:
        raise IncrementalError('BUILT_CAPSULE_REQUIRED')
    for name, sha in CAPSULE['files'].items():
        relative(name)
        if name == 'config/xunjia.env' or not re.fullmatch('[0-9a-f]{64}', sha):
            raise IncrementalError('INVALID_CAPSULE')
        if type(CAPSULE['sizes'].get(name)) is not int or CAPSULE['sizes'][name] < 0:
            raise IncrementalError('INVALID_CAPSULE')
    if set(PAYLOAD) - CAPSULE['files'].keys():
        raise IncrementalError('INVALID_CAPSULE')
    if CAPSULE['files'].get('SHA256SUMS.txt') != CAPSULE['manifest_sha256']:
        raise IncrementalError('INVALID_CAPSULE')
    return CAPSULE

def supported_base(root):
    meta = checked_capsule()
    root = Path(root).absolute()
    sha = digest(root / 'SHA256SUMS.txt')
    build = meta['baselines'].get(sha)
    if not build:
        raise IncrementalError('BASE_RELEASE_NOT_SUPPORTED')
    verify_tree(root, build, sha)
    return root

def decode_payload(name):
    expected = CAPSULE['sizes'][name]
    inflater = zlib.decompressobj()
    raw = inflater.decompress(base64.b64decode(PAYLOAD[name], validate=True), expected + 1)
    if (len(raw) != expected or not inflater.eof or inflater.unconsumed_tail or inflater.unused_data
            or hashlib.sha256(raw).hexdigest() != CAPSULE['files'][name]):
        raise IncrementalError('PAYLOAD_INTEGRITY_FAILED')
    return raw

def materialize(base, destination=None):
    meta = checked_capsule()
    source = supported_base(base)
    target = Path(destination).absolute() if destination is not None else source.parent / ('xunjia-update-' + meta['build_id'][:16])
    if (target.parent.resolve() != target.parent or target == source or target.is_symlink()
            or target.is_relative_to(source) or source.is_relative_to(target)):
        raise IncrementalError('UNSAFE_TARGET_PATH')
    if target.exists():
        verify_tree(target, meta['build_id'], meta['manifest_sha256'])
        return target
    required = sum(meta['sizes'].values()) + 64 * 1024 * 1024
    if shutil.disk_usage(target.parent).free < required:
        raise IncrementalError('INSUFFICIENT_DISK_SPACE')
    with tempfile.TemporaryDirectory(prefix='.xunjia-incremental-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'bundle'
        stage.mkdir(mode=0o700)
        for name, wanted in meta['files'].items():
            dest = stage / relative(name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if name in PAYLOAD:
                dest.write_bytes(decode_payload(name))
            else:
                origin = source / name
                if origin.is_symlink() or not origin.is_file() or digest(origin) != wanted:
                    raise IncrementalError('REUSED_ASSET_MISMATCH')
                shutil.copyfile(origin, dest)
            if digest(dest) != wanted:
                raise IncrementalError('STAGED_HASH_MISMATCH')
            dest.chmod(0o755 if name.endswith('.sh') or name == 'runtime/tools/busybox-aarch64' else 0o644)
        # Customer credentials are neither copied from the base nor regenerated.
        envfile = stage / 'config/xunjia.env'
        shutil.copyfile(stage / 'config/xunjia.env.example', envfile)
        envfile.chmod(0o600)
        verify_tree(stage, meta['build_id'], meta['manifest_sha256'])
        supported_base(source)
        os.rename(stage, target)
    return target

DISCOVERY_ROOTS = (Path('/home'), Path('/root'))


def bundle_candidates(prefix=Path('/home/xunjia')):
    """Bounded, read-only discovery; remembered locations are hints, never proof."""
    prefix = Path(prefix).absolute()
    found = {}
    def add(value):
        if not isinstance(value, (str, Path)) or not str(value):
            return
        try:
            path = Path(value).expanduser().absolute()
            if path.resolve() == path and path.is_dir():
                found.setdefault(str(path), path)
        except (OSError, ValueError, RuntimeError):
            pass
    add(os.environ.get('XUNJIA_UPGRADE_BASE_HINT', ''))
    try:
        config = prefix / 'config/upgrade-channel.json'
        if not config.is_symlink() and config.is_file() and config.stat().st_size <= 32768:
            value = json.loads(config.read_text())
            if isinstance(value, dict):add(value.get('base_bundle'))
    except (OSError, ValueError, TypeError):
        pass
    try:
        import sqlite3
        journal = prefix / 'state/upgrade/upgrades.db'
        if journal.resolve() == journal and journal.is_file():
            with contextlib.closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                row = db.execute("SELECT value FROM setting WHERE key='base_bundle'").fetchone()
                if row and len(row[0]) <= 8192:add(json.loads(row[0]))
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    add(Path.cwd())
    add(Path(__file__).absolute().parent)
    cache = prefix / 'state/upgrade/bundles'
    add(cache)
    try:
        if cache.resolve() == cache and cache.is_dir():
            for child in sorted(cache.iterdir())[:512]:add(child)
    except OSError:
        pass
    visited = 0
    def product_directory(name):
        name = name.lower()
        return name.startswith(('xunjia', 'xj-', 'xjbridge')) or name in ('loginfix', 'update-ready', 'unpack', 'verified', 'remote')
    def scan(parent, depth):
        nonlocal visited
        if depth < 0 or visited >= 2048:
            return
        try:
            if parent.resolve() != parent or not parent.is_dir():return
            children = sorted(parent.iterdir())[:512]
        except (OSError, RuntimeError):
            return
        for child in children:
            visited += 1
            if visited > 2048:return
            if not product_directory(child.name):continue
            add(child)
            # A bundle's contents are verified later, not recursively searched.
            if not (child / 'SHA256SUMS.txt').is_file():scan(child, depth - 1)
    for parent in DISCOVERY_ROOTS:scan(Path(parent).absolute(), 3)
    return list(found.values())


def installed_build(prefix):
    try:
        current = json.loads((Path(prefix) / 'app/VERSION.json').read_text()).get('build_id')
    except (OSError, ValueError, AttributeError):
        raise IncrementalError('INSTALLED_BUILD_UNPROVEN') from None
    if not isinstance(current, str) or not current:
        raise IncrementalError('INSTALLED_BUILD_UNPROVEN')
    if current not in set(CAPSULE['baselines'].values()) | {CAPSULE['build_id']}:
        raise IncrementalError('INSTALLED_BUILD_NOT_SUPPORTED')
    return current


def discover_base(prefix=Path('/home/xunjia')):
    current = installed_build(prefix)
    matches = []
    for root in bundle_candidates(prefix):
        try:
            sha = digest(root / 'SHA256SUMS.txt')
            build = CAPSULE['baselines'].get(sha)
            if build is not None:matches.append((build != current, str(root), root))
        except OSError:
            continue
    # Every supported base has an authenticated manifest and the capsule carries
    # all differences needed to reconstruct exactly the same target. Prefer the
    # running version, then use another verified supported base when necessary.
    for _, _, root in sorted(matches):
        try:
            return supported_base(root)
        except (IncrementalError, OSError, ValueError):
            continue
    raise IncrementalError('NO_INTACT_SUPPORTED_BASE_BUNDLE')


def prepare_automatic(prefix):
    """Keep the next exact baseline in a stable managed cache, not next to a ZIP."""
    prefix = Path(prefix).absolute()
    if prefix.resolve() != prefix or len(prefix.parts) < 3:
        raise IncrementalError('UNSAFE_PREFIX')
    current = installed_build(prefix)
    if current == CAPSULE['build_id']:
        for candidate in bundle_candidates(prefix):
            try:
                verify_tree(candidate, CAPSULE['build_id'], CAPSULE['manifest_sha256'])
                return candidate
            except (IncrementalError, OSError, ValueError):
                continue
    base = discover_base(prefix)
    cache = prefix / 'state/upgrade/bundles'
    if cache.resolve() != cache:
        raise IncrementalError('UNSAFE_TARGET_PATH')
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    return materialize(base, cache / ('xunjia-update-' + CAPSULE['build_id'][:16]))

def verify_installed_files(prefix, build):
    import common
    expected = CAPSULE.get('installed_files', {}).get(build)
    if not expected or common.snapshot_payload(Path(prefix) / 'app') != expected:
        raise IncrementalError('INSTALLED_FILES_DO_NOT_MATCH_REVIEWED_BASELINE')

def deploy(bundle, prefix, *, apply=False, manager_factory=None, mount_function=None, nginx_config=None,
           database_function=None, qa_history_function=None):
    meta = checked_capsule()
    verify_tree(bundle, meta['build_id'], meta['manifest_sha256'])
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(bundle) / 'deploy'))
    import common
    import manager
    import mount_xunjia
    prefix = common.safe_prefix(prefix)
    cfg = prefix / 'config'
    if not (cfg / 'xunjia.env').is_file() or not (prefix / 'app/server/app.py').is_file():
        raise IncrementalError('EXISTING_INSTALL_REQUIRED_NO_FRESH_INSTALL')
    installed = prefix / 'app/VERSION.json'
    if not installed.is_file():
        raise IncrementalError('INSTALLED_BUILD_UNPROVEN')
    current_build = json.loads(installed.read_text())['build_id']
    if current_build not in set(meta['baselines'].values()) | {meta['build_id']}:
        raise IncrementalError('INSTALLED_BUILD_NOT_SUPPORTED')
    automatic = meta.get('automatic_reply_migration', False)
    qa_history = meta.get('automatic_qa_history_migration', False)
    if automatic or qa_history:
        verify_installed_files(prefix, current_build)
    factory = manager_factory or manager.Manager
    ctl = factory(Path(bundle), prefix)
    plan = ctl.preflight()
    if automatic or qa_history:
        current = ctl.current()
        if current is None or ctl.validate_release(current).get('build_id') != current_build:
            raise IncrementalError('INSTALLED_RELEASE_METADATA_MISMATCH')
        ctl.verify_installed(liveness=True)
    config_root = Path('/etc/nginx') if nginx_config is None else Path(nginx_config)
    mount_xunjia.plan(config_root, Path(bundle) / 'deploy')
    before = common.config_fingerprint(cfg)
    report = {'build_id': meta['build_id'], 'preflight': plan, 'database_ddl_executed': False,
              'config_preserved': True, 'dify_publish_required': True, 'external_live_acceptance': 'NOT_PERFORMED',
              'optional_database_migrations': meta.get('optional_database_migrations', [])}
    if automatic:
        from incremental_database import run as migrate_database
        database_function = database_function or migrate_database
        report['database'] = database_function(prefix, apply=False)
    if qa_history:
        from qa_history_migration import run as migrate_qa_history
        qa_history_function = qa_history_function or migrate_qa_history
        report['qa_history'] = qa_history_function(prefix, apply=False)
    if not apply:
        return {**report, 'status': 'CHECK_PASSED_NO_SERVICE_CHANGES'}
    if not ctl.service.active():
        raise IncrementalError('EXISTING_SERVICE_MUST_BE_RUNNING')
    lockpath = prefix / '.xunjia-incremental.lock'
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IncrementalError('INCREMENTAL_ALREADY_RUNNING') from None
        if common.config_fingerprint(cfg) != before:
            raise IncrementalError('CONFIG_CHANGED_DURING_CHECK')
        if json.loads(installed.read_text())['build_id'] != current_build:
            raise IncrementalError('INSTALLED_RELEASE_CHANGED_DURING_CHECK')
        result = None
        try:
            if automatic:
                with ctl.lock():
                    ctl.ensure_idle()
                    verify_installed_files(prefix, current_build)
                    if common.config_fingerprint(cfg) != before:
                        raise IncrementalError('CONFIG_CHANGED_DURING_CHECK')
                    ctl.service.validate()
                    if not ctl.service.active():
                        raise IncrementalError('SERVICE_CHANGED_BEFORE_MIGRATION')
                    needs_ddl = database_function(prefix, apply=False)['status'] == 'MIGRATION_REQUIRED'
                    try:
                        if needs_ddl:
                            ctl.service.stop()
                            if ctl.service.active():
                                raise IncrementalError('SERVICE_NOT_STOPPED_FOR_MIGRATION')
                        report['database'] = database_function(prefix, apply=True)
                        report['database_ddl_executed'] = report['database']['ddl_executed']
                    finally:
                        if needs_ddl:
                            ctl.service.start()
            if current_build == meta['build_id']:
                if qa_history and report['qa_history']['status']!='ALREADY_APPLIED':
                    with ctl.lock():
                        ctl.ensure_idle()
                        ctl.service.stop()
                        try:
                            if ctl.service.active():raise IncrementalError('SERVICE_NOT_STOPPED_FOR_QA_MIGRATION')
                            report['qa_history'] = qa_history_function(prefix, apply=True)
                            report['database_ddl_executed'] |= report['qa_history']['ddl_executed']
                        finally:ctl.service.start()
                ctl.verify_installed()
            else:
                if qa_history:
                    # Manager owns stop/switch/recovery. Run the migration at its
                    # actual stop boundary so the old SQLite writer cannot run
                    # again between the copy and the current-pointer switch.
                    stop = ctl.service.stop
                    attempted = False
                    def migrate_after_stop():
                        nonlocal attempted
                        stop()
                        if attempted:return
                        attempted = True
                        if ctl.service.active():raise IncrementalError('SERVICE_NOT_STOPPED_FOR_QA_MIGRATION')
                        if common.config_fingerprint(cfg)!=before:raise IncrementalError('CONFIG_CHANGED_BEFORE_QA_MIGRATION')
                        report['qa_history'] = qa_history_function(prefix, apply=True)
                        report['database_ddl_executed'] |= report['qa_history']['ddl_executed']
                    ctl.service.stop = migrate_after_stop
                    try:
                        result = ctl.install()
                        if not attempted:raise IncrementalError('QA_HISTORY_MIGRATION_STOP_BOUNDARY_NOT_REACHED')
                    finally:ctl.service.stop = stop
                else:
                    result = ctl.install()
            if common.config_fingerprint(cfg) != before:
                raise IncrementalError('CONFIG_CHANGED_DURING_UPGRADE')
            if mount_function is not None:
                mounted = mount_function()
            else:
                command = [sys.executable, '-B', str(Path(bundle) / 'deploy/mount_xunjia.py'), '--apply']
                child = subprocess.run(command, capture_output=True, text=True, timeout=180)
                if child.returncode:
                    raise IncrementalError('NGINX_MOUNT_FAILED')
                mounted = json.loads(child.stdout)
            return {**report, 'status': 'UPGRADE_APPLIED_LOCAL_CHECKS_PASSED', 'application': result,
                    'nginx': mounted, 'already_installed': result is None}
        except BaseException:
            # mount_xunjia restores its exact original files on its own failure.
            # Restore only the app release switched by this invocation.
            if result is not None:
                current = ctl.current()
                if current is None or current.name != result['release_id']:
                    raise IncrementalError('CONCURRENT_RELEASE_CHANGED_MANUAL_RECOVERY_REQUIRED') from None
                ctl.rollback()
            raise
    finally:
        os.close(fd)

def main():
    os.umask(0o077)
    checked_capsule()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--prefix', type=Path, default=Path('/home/xunjia'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.apply and (args.prepare_only or args.check):
        raise IncrementalError('CONFLICTING_OPTIONS')
    if args.apply and os.geteuid() != 0:
        raise IncrementalError('ROOT_REQUIRED_FOR_APPLY')
    target = materialize(args.bundle) if args.bundle is not None else prepare_automatic(args.prefix)
    if args.prepare_only:
        result = {'status': 'PREPARED_NO_SERVICE_CHANGES', 'bundle': str(target), 'build_id': CAPSULE['build_id']}
    else:
        legacy_stage = None
        job_id = Path(__file__).absolute().parent.name
        expected_stage = Path(args.prefix) / 'state/upgrade/jobs' / job_id
        if (args.apply and re.fullmatch('[0-9a-f]{32}',job_id) and Path(__file__).absolute().parent == expected_stage
                and (target/'deploy/upgrade_log_return.py').is_file()):
            sys.path[:0] = [str(target/'app'),str(target/'deploy')]
            try:
                from upgrade_log_return import arm as arm_log_return
                arm_log_return(args.prefix,target,job_id)
            except Exception:pass  # Diagnostic delivery cannot stop a business upgrade.
        if (args.apply and not os.environ.get('XUNJIA_UPGRADE_JOB_ID') and re.fullmatch('[0-9a-f]{32}',job_id)
                and Path(__file__).absolute().parent == expected_stage):
            sys.path[:0] = [str(target/'app'),str(target/'deploy')]
            from upgrade_monitor import arm
            legacy_stage = arm(args.prefix,target,job_id)
            os.environ['XUNJIA_UPGRADE_JOB_ID'] = job_id
        try:
            result = deploy(target, args.prefix, apply=args.apply)
        except BaseException:
            if legacy_stage is not None:
                import common
                common.atomic_json(legacy_stage/'capsule-result.json',{'success':False})
            raise
        if legacy_stage is not None:
            import common
            common.atomic_json(legacy_stage/'capsule-result.json',{'success':True,'build_id':CAPSULE['build_id']})
        result['bundle'] = str(target)
    print(json.dumps(result, ensure_ascii=True))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raw_code = getattr(exc, 'code', str(exc))
        code = raw_code if isinstance(raw_code, str) and re.fullmatch('[A-Z_]+', raw_code) else 'INCREMENTAL_FAILED_' + type(exc).__name__
        print(code, file=sys.stderr)
        raise SystemExit(2)
