"""Thirty-day cleanup of explicitly registered downloads, never arbitrary paths."""
from __future__ import annotations
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import time

SECONDS = 30 * 86400
LEGACY_DIRECTORIES = ('/root/xjbridge', '/root/loginfix', '/root/xj-watch-1029')


def legacy_file(path):
    name = path.name
    if any(word in name.lower() for word in ('private', 'credential', 'secret', 'auth', 'cert', 'key')):
        return False
    if path.parent == Path('/root'):
        return (re.fullmatch(r'(?:Xunjia-(?:Offline-Incremental|Jump-Network-Maintenance)-[A-Za-z0-9-]+|xj-(?:update|login-fix|remote-upgrade)[A-Za-z0-9-]*)\.zip', name) is not None
                or re.fullmatch(r'net-result-[0-9]+\.json', name) is not None)
    if not any(path.is_relative_to(Path(root)) for root in LEGACY_DIRECTORIES):
        return False
    return (name in ('upgrade.py', 'upgrade.sh', 'upgrade.py.sha256', 'SHA256SUMS.txt', 'VERSION.json', 'README.txt',
                     'check.py', 'data.txt', 'patch.txt', 'upgrade-apply.log', 'install.py', 'install.sh', 'channel.json')
            or re.fullmatch(r'net-result-[0-9]+\.json', name) is not None
            or re.fullmatch(r'xj-(?:update|login-fix|remote-upgrade)[A-Za-z0-9-]*\.zip', name) is not None)


def fingerprint(path):
    """Inventory metadata without following links or reading private contents."""
    path = Path(path)
    digest = hashlib.sha256()
    newest = 0
    stack = [path]
    while stack:
        item = stack.pop()
        s = item.lstat()
        if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)):
            raise ValueError('RETENTION_SPECIAL_FILE')
        newest = max(newest, s.st_mtime)
        row = [str(item.relative_to(path)), s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns]
        if item.is_symlink():
            row.append(os.readlink(item))
        digest.update(json.dumps(row).encode())
        if stat.S_ISDIR(s.st_mode):
            stack.extend(sorted(item.iterdir(), reverse=True))
    return digest.hexdigest(), newest


def register_artifact(store, path, kind):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path or not path.exists():
        raise ValueError('RETENTION_PATH_INVALID')
    if kind not in ('job', 'bundle', 'release', 'legacy', 'partial'):
        raise ValueError('RETENTION_KIND_INVALID')
    digest, stamp = fingerprint(path)
    with store.db() as db:
        db.execute('''INSERT INTO artifact(path,kind,created_at,fingerprint) VALUES(?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET kind=excluded.kind,
              created_at=CASE WHEN artifact.fingerprint=excluded.fingerprint THEN artifact.created_at ELSE excluded.created_at END,
              fingerprint=excluded.fingerprint,removed_at=NULL''',
                   (str(path), kind, stamp, digest))


def allowed(path, kind, store, prefix):
    if path.is_symlink() or path.resolve() != path:
        return False
    if kind == 'job':
        return path.parent == store.root / 'jobs' and len(path.name) == 32 and all(c in '0123456789abcdef' for c in path.name)
    if kind == 'bundle':
        return ((path.parent == store.root / 'bundles' or path.parent == Path('/home/Xunjia-new-20261026'))
                and (path.name.startswith(('xunjia-update-', 'xunjia-agent-batch1-')) or path.name == 'update-ready'))
    if kind == 'partial':
        return (path.parent in (store.root / 'bundles', Path('/home/Xunjia-new-20261026'))
                and re.fullmatch(r'\.xunjia-incremental-[A-Za-z0-9_-]+', path.name) is not None)
    if kind == 'release':
        return path.parent == prefix / 'releases' and (path / 'RELEASE.json').is_file()
    if kind == 'legacy':
        return path.is_file() and legacy_file(path)
    return False


def discover(store, prefix):
    # Exact product namespaces only. No home directory or disk-wide traversal.
    candidates = []
    for root in map(Path, LEGACY_DIRECTORIES):
        if root.is_dir() and not root.is_symlink():
            candidates.extend((p, 'legacy') for p in root.rglob('*') if p.is_file() and legacy_file(p))
    for pattern in ('Xunjia-*.zip', 'xj-*.zip', 'net-result-*.json'):
        candidates.extend((p, 'legacy') for p in Path('/root').glob(pattern) if p.is_file())
    candidates.extend((p, 'release') for p in (prefix / 'releases').glob('*') if (p / 'RELEASE.json').is_file())
    candidates.extend((p, 'job') for p in (store.root / 'jobs').glob('*') if p.is_dir())
    for parent in (store.root / 'bundles', Path('/home/Xunjia-new-20261026')):
        for pattern in ('xunjia-update-*', 'xunjia-agent-batch1-*', 'update-ready'):
            candidates.extend((p, 'bundle') for p in parent.glob(pattern) if (p / 'SHA256SUMS.txt').is_file())
        candidates.extend((p, 'partial') for p in parent.glob('.xunjia-incremental-*') if p.is_dir())
    for path, kind in candidates:
        if allowed(path, kind, store, prefix):
            register_artifact(store, path, kind)


@contextmanager
def deployment_lock(prefix, name):
    fd = os.open(prefix / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def remove_tree(path):
    """Pin each parent using no-follow directory fds, then use safe stdlib removal."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('SAFE_RECURSIVE_REMOVAL_UNAVAILABLE')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        mode = os.stat(path.name, dir_fd=fd, follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            shutil.rmtree(path.name, dir_fd=fd)
        elif stat.S_ISREG(mode):
            os.unlink(path.name, dir_fd=fd)
        else:
            raise ValueError('RETENTION_OBJECT_CHANGED')
    finally:
        os.close(fd)


def configuration_references(prefix):
    """Protect external paths still named by live configuration; never log values."""
    result = set()
    queue = [(p, 0) for p in (prefix / 'config').rglob('*')]
    visited = set()
    def visit(value, directory, depth, field=''):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, directory, depth, str(key).lower())
        elif isinstance(value, list):
            for item in value:
                visit(item, directory, depth, field)
        elif isinstance(value, str) and value and '\n' not in value and '://' not in value:
            is_path = value.startswith('/') or field.endswith(('_file', '_path', '_dir', '_config')) or field in ('path', 'file', 'directory', 'base_bundle')
            if not is_path:
                return
            candidate = Path(value).expanduser()
            alias = (candidate if candidate.is_absolute() else directory / candidate).absolute()
            path = alias.resolve()
            if path not in (prefix, prefix / 'state') and len(path.parts) >= 3:
                result.add(path)
            if alias.is_file() and alias.suffix in ('.json', '.env', '.yaml', '.yml'):
                queue.append((alias, depth + 1))
    while queue:
        path, depth = queue.pop()
        if path.is_symlink():
            result.add(path.resolve())
        identity = (path.resolve(), path.parent.absolute())
        if not path.is_file() or identity in visited:
            continue
        if depth > 8 or len(visited) >= 512 or path.stat().st_size > 1024 * 1024:
            raise ValueError('CONFIG_REFERENCE_SCAN_LIMIT')
        visited.add(identity)
        if path.suffix == '.json':
            visit(json.loads(path.read_text()), path.parent, depth)
        elif path.suffix in ('.yaml', '.yml'):
            import yaml
            visit(yaml.safe_load(path.read_text()), path.parent, depth)
        elif path.suffix == '.env':
            for line in path.read_text().splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    values = shlex.split(line.split('=', 1)[1], comments=True)
                    if len(values) == 1:
                        visit(values[0], path.parent, depth, line.split('=', 1)[0].strip().lower())
    return result


def prune_artifacts(store, prefix):
    prefix = Path(prefix)
    now = time.time()
    previous_run = store.get('retention', {})
    if now - previous_run.get('last_run', 0) < 3600:
        return previous_run
    with ExitStack() as locks:
        for name in ('.xunjia-incremental.lock', '.xunjia-deploy.lock'):
            locks.enter_context(deployment_lock(prefix, name))
        # Retain both active and immediate rollback release, plus every object
        # involved in an unfinished deployment. Unknown journals fail closed.
        protected = {prefix / 'config', prefix / 'state'}
        referenced = configuration_references(prefix)
        protected.update(referenced)
        builds = set()
        current = (prefix / 'current').resolve(strict=True)
        for release in (current, Path(json.loads((current / 'RELEASE.json').read_text()).get('previous') or current)):
            protected.add(release.resolve())
            builds.add(json.loads((release / 'app/VERSION.json').read_text())['build_id'])
        for record in (prefix / 'deployments').glob('*.json'):
            data = json.loads(record.read_text())
            if data.get('phase') not in ('COMMITTED', 'ABORTED', 'RECOVERED'):
                store.set('retention', {'days': 30, 'last_run': now, 'removed': 0, 'blocked': 1,
                                       'hint': '部署记录需要核对，已暂停文件清理'})
                return
        base = store.get('base_bundle')
        if base:
            protected.add(Path(base).resolve())
        discover(store, prefix)
        removed = blocked = 0
        cursor = store.get('retention_cursor', [0, ''])
        horizon = store.get('retention_horizon', now - SECONDS)
        with store.db() as db:
            query = 'SELECT * FROM artifact WHERE removed_at IS NULL AND created_at<? AND (created_at>? OR (created_at=? AND path>?)) ORDER BY created_at,path LIMIT 100'
            candidates = db.execute(query, (horizon, cursor[0], cursor[0], cursor[1])).fetchall()
            if not candidates:
                horizon = now - SECONDS
                candidates = db.execute(query, (horizon, 0, 0, '')).fetchall()
        for row in candidates:
            cursor = [row['created_at'], row['path']]
            path = Path(row['path'])
            if not path.exists() and not path.is_symlink():
                with store.db() as db:
                    db.execute('UPDATE artifact SET removed_at=? WHERE path=?', (now, str(path)))
                continue
            if not allowed(path, row['kind'], store, prefix):
                blocked += 1
                continue
            # Protected state is an ancestor of jobs, but only its descendants
            # explicitly allowlisted above can be pruned. Ancestor deletion is forbidden.
            if any(path == p or p.is_relative_to(path) for p in protected):
                blocked += 1
                continue
            if any(path.is_relative_to(p) for p in referenced):
                blocked += 1
                continue
            if row['kind'] == 'bundle':
                try:
                    version = json.loads((path / 'VERSION.json').read_text())
                    if not isinstance(version, dict) or not isinstance(version.get('build_id'), str):
                        raise ValueError('BUNDLE_METADATA_INVALID')
                except (OSError, ValueError, TypeError):
                    blocked += 1
                    continue
                if version['build_id'] in builds:
                    blocked += 1
                    continue
            if row['kind'] == 'job':
                with store.db() as db:
                    job = db.execute('SELECT state FROM job WHERE id=?', (path.name,)).fetchone()
                if not job or job[0] in ('queued', 'downloading', 'verifying', 'installing', 'unknown'):
                    blocked += 1
                    continue
            if fingerprint(path)[0] != row['fingerprint']:
                blocked += 1
                continue
            remove_tree(path)
            with store.db() as db:
                db.execute('UPDATE artifact SET removed_at=? WHERE path=?', (now, str(path)))
            removed += 1
            if removed >= 10:
                break
        store.set('retention_cursor', cursor)
        store.set('retention_horizon', horizon)
        result = {'days': 30, 'last_run': now, 'removed': removed, 'blocked': blocked,
                  'hint': '已保留现役、回滚基线及待核对任务；业务数据库不按下载期限删除'}
        store.set('retention', result)
        return result
