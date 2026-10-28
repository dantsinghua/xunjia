"""Offline incremental capsule. Reuses verified assets; never executes database DDL.

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

def discover_base():
    roots = [Path.cwd(), Path(__file__).absolute().parent]
    for parent in (Path('/home'), Path('/root')):
        for pattern in ('*new-20261026/xunjia-agent-batch1-*', '*new-20261026/update-ready',
                        'xunjia-agent-batch1-*', 'xunjia*/xunjia-agent-batch1-*', 'xunjia*/update-ready'):
            roots.extend(parent.glob(pattern))
    matches = []
    for root in dict.fromkeys(roots):
        if not (root / 'SHA256SUMS.txt').is_file():
            continue
        try:
            sha = digest(root / 'SHA256SUMS.txt')
            if sha in CAPSULE['baselines']:
                matches.append(root.absolute())
        except OSError:
            continue
    if len(matches) != 1:
        raise IncrementalError('EXACT_BASE_NOT_UNIQUE_USE_BUNDLE')
    return matches[0]

def deploy(bundle, prefix, *, apply=False, manager_factory=None, mount_function=None, nginx_config=None):
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
    factory = manager_factory or manager.Manager
    ctl = factory(Path(bundle), prefix)
    plan = ctl.preflight()
    config_root = Path('/etc/nginx') if nginx_config is None else Path(nginx_config)
    mount_xunjia.plan(config_root, Path(bundle) / 'deploy')
    before = common.config_fingerprint(cfg)
    report = {'build_id': meta['build_id'], 'preflight': plan, 'database_ddl_executed': False,
              'config_preserved': True, 'dify_publish_required': True, 'external_live_acceptance': 'NOT_PERFORMED'}
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
            if current_build == meta['build_id']:
                ctl.verify_installed()
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
    base = args.bundle if args.bundle is not None else discover_base()
    target = materialize(base)
    if args.prepare_only:
        result = {'status': 'PREPARED_NO_SERVICE_CHANGES', 'bundle': str(target), 'build_id': CAPSULE['build_id']}
    else:
        result = deploy(target, args.prefix, apply=args.apply)
        result['bundle'] = str(target)
    print(json.dumps(result, ensure_ascii=True))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        code = str(exc) if re.fullmatch('[A-Z_]+', str(exc)) else 'INCREMENTAL_FAILED_' + type(exc).__name__
        print(code, file=sys.stderr)
        raise SystemExit(2)
