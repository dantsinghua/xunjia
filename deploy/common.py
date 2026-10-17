"""Shared deployment primitives. Standard library only; no secret values in errors."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shlex
import stat
from pathlib import Path
from urllib.parse import urlsplit
class DeployError(RuntimeError):
    def __init__(self, code: str, detail: str = ''):
        self.code = code
        super().__init__(code + (': ' + detail if detail else ''))
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()
def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.new-' + str(os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        if temp.exists(): temp.unlink()
def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try: os.fsync(fd)
    finally: os.close(fd)
def safe_prefix(value: str | Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute() or '..' in p.parts or len(p.parts) < 3:
        raise DeployError('UNSAFE_PREFIX', 'use an absolute dedicated application directory')
    if p in (Path('/tmp'), Path('/home'), Path('/usr'), Path('/var'), Path('/opt')):
        raise DeployError('UNSAFE_PREFIX')
    if any(x.lower().startswith(('njcb', 'nanyin')) for x in p.parts):
        raise DeployError('OTHER_PROJECT_PREFIX_FORBIDDEN')
    # Follow no prefix/parent symlink: unexpected link destinations are not managed.
    if p.resolve() != p:
        raise DeployError('PREFIX_SYMLINK_FORBIDDEN')
    return p
def safe_service(value: str) -> str:
    if not re.fullmatch(r'xunjia-agent(?:-[a-zA-Z0-9_-]{1,50})?\.service', value):
        raise DeployError('OTHER_SERVICE_FORBIDDEN', 'only xunjia-agent*.service is supported')
    return value
def relative_path(text: str) -> Path:
    p = Path(text)
    if p.is_absolute() or not text or '..' in p.parts or '\\' in text or '\n' in text:
        raise DeployError('UNSAFE_MANIFEST_PATH')
    return p
def load_json(path: Path):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): raise DeployError('INVALID_METADATA', path.name) from None
def verify_bundle(root: Path) -> dict:
    """Configuration is mutable; program, templates, wheels and SQL are not."""
    version = load_json(root / 'VERSION.json')
    if version.get('deployment_layout') != 2:
        raise DeployError('UNSUPPORTED_PACKAGE_LAYOUT', 'use the batch-1 release package')
    manifest = root / 'SHA256SUMS.txt'
    try: lines = manifest.read_text(encoding='utf-8').splitlines()
    except OSError: raise DeployError('MANIFEST_MISSING') from None
    entries = {}
    for line in lines:
        if not line: continue
        parts = line.split('  ', 1)
        if len(parts) != 2 or not re.fullmatch('[0-9a-f]{64}', parts[0]):
            raise DeployError('INVALID_MANIFEST')
        digest, name = parts
        relative = relative_path(name)
        if name in entries or name == 'config/xunjia.env':
            raise DeployError('INVALID_MANIFEST', 'duplicate entry or mutable env covered')
        f = root / relative
        if f.is_symlink() or not f.is_file() or sha256(f) != digest:
            raise DeployError('INTEGRITY_MISMATCH', name)
        entries[name] = digest
    for required in ('VERSION.json', 'config/xunjia.env.example', 'requirements.lock',
                     'app/server/app.py', 'deploy/manager.py', 'deploy/runtime.py',
                     'sql/oceanbase/001_xunjia_schema_mysql.sql'):
        if required not in entries: raise DeployError('INCOMPLETE_MANIFEST', required)
    for f in root.rglob('*'):
        rel = f.relative_to(root)
        if f.is_symlink():
            raise DeployError('PACKAGE_SYMLINK_FORBIDDEN', str(rel))
        if not f.is_file(): continue
        name = rel.as_posix()
        if name in entries or name == 'SHA256SUMS.txt': continue
        # Operators may edit xunjia.env and add JSON/certificates under config/.
        if rel.parts[0] == 'config' and not name.endswith('.example'): continue
        raise DeployError('UNMANIFESTED_PAYLOAD', name)
    return version
def read_env(path: Path, *, require_private=True) -> dict[str, str]:
    """Read single-line KEY=value assignments without executing a shell."""
    if not path.is_file(): raise DeployError('CONFIG_MISSING', str(path))
    if require_private and stat.S_IMODE(path.stat().st_mode) not in (0o600, 0o400):
        raise DeployError('CONFIG_PERMISSIONS', 'xunjia.env must be 600 or 400')
    try: text = path.read_text(encoding='utf-8-sig')
    except UnicodeError: raise DeployError('CONFIG_ENCODING', 'use UTF-8') from None
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'): continue
        match = re.fullmatch(r'(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=(.*)', line)
        if not match: raise DeployError('CONFIG_SYNTAX', 'line ' + str(number))
        key, raw = match.groups()
        if key in result: raise DeployError('CONFIG_DUPLICATE_KEY', key)
        try: parts = shlex.split(raw, comments=True, posix=True)
        except ValueError: raise DeployError('CONFIG_SYNTAX', key + ' quoting') from None
        if len(parts) > 1: raise DeployError('CONFIG_SYNTAX', key + ' must be a single literal value')
        value = parts[0] if parts else ''
        if '\x00' in value: raise DeployError('CONFIG_NUL', key)
        result[key] = value
    return result
def effective_env(values: dict, prefix: Path, app: Path) -> dict[str, str]:
    env = dict(values)
    defaults = {'XUNJIA_HOST': '127.0.0.1', 'XUNJIA_PORT': '18770',
                'XUNJIA_RUN_BACKGROUND': '0', 'XUNJIA_ENABLE_LIVE_TRANSPORT': '0',
                'XUNJIA_DIFY_URL': '', 'XUNJIA_DIFY_ANALYSIS_APP_KEY': '',
                'XUNJIA_DIFY_REPLY_APP_KEY': '', 'XUNJIA_AGGREGATION_PROFILE': '',
                'XUNJIA_TRANSPORT_CONFIG': '', 'XUNJIA_QUOTE_SOURCE': ''}
    for key, val in defaults.items(): env.setdefault(key, val)
    if env.get('XUNJIA_WEB_DIST', '') in ('', '/home/xunjia/app/frontend/dist', str(prefix/'app/frontend/dist')):
        env['XUNJIA_WEB_DIST'] = str(app/'frontend/dist')
    if env.get('XUNJIA_STATE_DIR', '') in ('', '/home/xunjia/state'):
        env['XUNJIA_STATE_DIR'] = str(prefix/'state')
    return env
def validate_config(values: dict, *, ready=False) -> list[str]:
    missing = []
    dsn = values.get('XUNJIA_DATABASE_URL', '')
    try:
        url = urlsplit(dsn)
        _port = url.port
    except ValueError: raise DeployError('CONFIG_DATABASE_URL_INVALID') from None
    if url.scheme != 'oceanbase' or not url.hostname or not url.path.lstrip('/').startswith('xunjia_'):
        raise DeployError('CONFIG_DATABASE_URL_INVALID', 'select an explicit OceanBase xunjia_* schema')
    if not url.username or not url.password or 'REPLACE_' in dsn:
        missing.append('XUNJIA_DATABASE_URL')
    for flag in ('XUNJIA_RUN_BACKGROUND', 'XUNJIA_ENABLE_LIVE_TRANSPORT', 'XUNJIA_ALLOW_INSECURE_MQ_HTTP'):
        if values.get(flag, '0') not in ('0', '1'): raise DeployError('CONFIG_BOOLEAN_INVALID', flag)
    try:
        if not 1 <= int(values.get('XUNJIA_PORT', '18770')) <= 65535: raise ValueError
    except (ValueError, TypeError): raise DeployError('CONFIG_PORT_INVALID', 'XUNJIA_PORT') from None
    host = values.get('XUNJIA_HOST', '127.0.0.1')
    if not host or not re.fullmatch(r'[a-zA-Z0-9.:-]+', host): raise DeployError('CONFIG_HOST_INVALID')
    dify = values.get('XUNJIA_DIFY_URL', '')
    if dify:
        try:
            parsed = urlsplit(dify); _ = parsed.port
            if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or
                    parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in ('','/v1')):
                raise ValueError
        except ValueError: raise DeployError('CONFIG_DIFY_URL_INVALID', 'full existing Chatflow API URL required') from None
    keys=('XUNJIA_DIFY_ANALYSIS_APP_KEY','XUNJIA_DIFY_REPLY_APP_KEY')
    if any(values.get(k) for k in keys) and not dify: missing.append('XUNJIA_DIFY_URL')
    if any(values.get(k) for k in keys):
        missing.extend(k for k in keys if not values.get(k))
    if not values.get('XUNJIA_SSO_SECRET'): missing.append('XUNJIA_SSO_SECRET')
    if len(values.get('XUNJIA_AUTH_SECRET', '').encode('utf8')) < 32: missing.append('XUNJIA_AUTH_SECRET')
    if values.get('XUNJIA_RUN_BACKGROUND') == '1':
        for key in (*keys,'XUNJIA_DIFY_URL','XUNJIA_AGGREGATION_PROFILE'):
            if not values.get(key): missing.append(key)
    if values.get('XUNJIA_ENABLE_LIVE_TRANSPORT')=='1' and not values.get('XUNJIA_TRANSPORT_CONFIG'):
        missing.append('XUNJIA_TRANSPORT_CONFIG')
    if ready and missing:
        raise DeployError('CONFIG_INCOMPLETE', ', '.join(sorted(set(missing))))
    return sorted(set(missing))
def config_fingerprint(root: Path) -> dict:
    result = {}
    if not root.exists(): return result
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            # Keep existing operator-owned certificate links verbatim. Do not follow/copy their targets.
            result[rel] = {'link': os.readlink(p)}
        elif p.is_file():
            result[rel] = {'sha256': sha256(p), 'mode': stat.S_IMODE(p.stat().st_mode)}
        elif not p.is_dir(): raise DeployError('CONFIG_SPECIAL_FILE', rel)
    return result
def snapshot_payload(root: Path) -> dict[str, str]:
    files = {}
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if rel.parts[0] == 'config' or '__pycache__' in rel.parts or p.suffix in ('.pyc','.pyo'):
            continue
        if rel.as_posix() in ('.deployment.json', 'RELEASE-MANIFEST.json'): continue
        if p.is_symlink():
            if not p.is_file(): continue
            # Older externally-linked code is not safe to migrate automatically.
            raise DeployError('CODE_SYMLINK_FORBIDDEN', str(rel))
        if p.is_file(): files[rel.as_posix()] = sha256(p)
    if 'server/app.py' not in files: raise DeployError('NOT_XUNJIA_APPLICATION')
    return files
def verify_payload(root: Path, expected: dict[str, str]) -> None:
    if snapshot_payload(root) != expected: raise DeployError('RELEASE_INTEGRITY_FAILED')
def health_url(values: dict, path='/api/health/ready') -> str:
    host = values.get('XUNJIA_HOST', '127.0.0.1')
    if host in ('0.0.0.0','localhost'): host = '127.0.0.1'
    if host == '::': host = '::1'
    if ':' in host: host = '[' + host + ']'
    return 'http://' + host + ':' + str(int(values.get('XUNJIA_PORT','18770'))) + path
