"""Persistent public upgrade diagnostics; raw subprocess output stays private."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import quote


REDACTED = '[REDACTED]'
LOG_LIMIT = 64 * 1024
_SECRET_KEY = re.compile(
    r'password|passwd|pwd|token|secret|cookie|authorization|credential|'
    r'(?:^|[_-])sid(?:$|[_-])|(?:api|app|private|access|auth)[_-]?key', re.I)
_PAIR = re.compile(
    r'''(?ix)(["']?[\w.-]*(?:password|passwd|pwd|token|secret|cookie|authorization|credential|sid|(?:api|app)[_-]?key|private[_-]?key)[\w.-]*["']?\s*[:=]\s*)'''
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,;}]+)''')
_PRIVATE_KEY = re.compile(
    r'-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----.*?'
    r'(?:-----END (?:[A-Z0-9 ]* )?PRIVATE KEY-----|\Z)', re.S)
_DSN = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*://)[^\s/<>"\']*:[^\s/<>"\']*@')
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_JOB_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_META = frozenset({
    'phase', 'stage', 'status', 'code', 'build_id', 'target_build', 'previous_build',
    'current_build', 'version', 'target_version', 'previous_version', 'package_sha256',
    'sha256', 'hash', 'exit_code', 'returncode', 'rollback', 'rollback_status',
    'rollback_attempted', 'rollback_succeeded', 'health', 'healthy', 'health_status',
    'service_active', 'ready', 'live', 'applied', 'changed', 'restored',
})
_PHASES = frozenset({
    'queued', 'downloading', 'verifying', 'preflight', 'installing', 'checking',
    'health', 'health_check', 'rollback', 'rolling_back', 'rollback_check',
    'succeeded', 'failed', 'unknown', 'empty', 'recovery', 'reconciling',
    'preparing', 'prepared', 'switching', 'restarting',
})
_MESSAGES = {
    'queued': '升级任务已提交', 'downloading': '正在同步升级包',
    'verifying': '正在校验签名和版本', 'preflight': '正在检查安装条件',
    'installing': '正在安装升级', 'checking': '正在检查服务状态',
    'health': '正在检查服务健康状态', 'health_check': '正在检查服务健康状态',
    'rollback': '正在恢复升级前版本', 'rolling_back': '正在恢复升级前版本',
    'rollback_check': '正在检查回滚后的服务状态',
    'succeeded': '升级完成', 'failed': '升级未完成，请查看错误码',
    'unknown': '升级结果待核对', 'empty': '暂无待同步的升级包',
    'recovery': '正在核对中断任务', 'reconciling': '正在核对中断任务',
    'preparing': '正在准备新版本，旧版继续运行', 'prepared': '新版本准备完成',
    'switching': '正在切换版本', 'restarting': '正在重启服务',
}
_CODES = {
    'UPGRADE_FAILED_OLD_VERSION_HEALTHY': '升级未完成，旧版仍正常运行',
    'INSTALLER_TIMEOUT': '安装超时，已停止本次安装进程',
    'INSTALLER_START_FAILED': '安装任务启动失败',
    'INSTALLER_STOP_NOT_CONFIRMED': '未能确认安装进程已停止，已暂停恢复以避免冲突',
    'ALREADY_INSTALLED': '目标版本已安装，本次未重复安装',
    'NO_INTACT_SUPPORTED_BASE_BUNDLE': '未找到完整的升级基线，未执行安装',
    'NO_INTACT_CURRENT_BASE_BUNDLE': '未找到完整的当前基线，未执行安装',
    'INSTALLED_BUILD_NOT_SUPPORTED': '升级包不支持当前版本，未执行安装',
    'UPGRADE_ROLLBACK_SUCCEEDED': '升级失败，已恢复升级前版本并通过健康检查',
    'UPGRADE_ROLLBACK_FAILED': '升级失败，自动恢复未通过检查，请联系技术支持',
    'UPGRADE_WORKER_INTERRUPTED': '升级进程中断，正在核对结果',
}


def redact(text, secrets=()):
    """Defense in depth for known secrets and common credential representations."""
    text = str(text)
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = re.sub(r'(?im)^\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:.*$',
                  REDACTED, text)
    text = _DSN.sub(lambda m: m[1] + REDACTED + '@', text)
    text = _PAIR.sub(lambda m: m[1] + REDACTED, text)
    text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=~-]+', REDACTED, text)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def _read_file(path, limit, *, private=False, tail=False):
    """Open every component without following symlinks, including parent folders."""
    path = Path(path).absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('UNSAFE_DIAGNOSTIC_FILE')
            if private and info.st_mode & 0o077:
                raise ValueError('UNSAFE_DIAGNOSTIC_FILE')
            if info.st_size > limit:
                if not tail:
                    raise ValueError('DIAGNOSTIC_FILE_TOO_LARGE')
                stream.seek(info.st_size - limit)
            data = stream.read(limit)
            # A truncated first line may have lost its credential label.
            if tail and info.st_size > limit:
                data = data.partition(b'\n')[2]
            return data.decode('utf-8', errors='replace')
    finally:
        os.close(directory)


def _secrets(store):
    """Only inspect bounded protected config files inside the installation prefix."""
    root = Path(store.root)
    if root.name != 'upgrade' or root.parent.name != 'state':
        return ()
    config = root.parent.parent / 'config'
    if config.is_symlink() or config.resolve() != config:
        return ()
    values = set()

    def collect(value, sensitive=False, depth=0):
        if depth > 12:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, sensitive or bool(_SECRET_KEY.search(str(key))), depth + 1)
        elif isinstance(value, list):
            for item in value:
                collect(item, sensitive, depth + 1)
        elif isinstance(value, str) and sensitive and value:
            values.update((value, quote(value, safe=''), json.dumps(value)[1:-1]))

    try:
        names = sorted(p.name for p in config.iterdir()
                       if p.name == 'xunjia.env' or p.suffix in ('.json', '.yaml', '.yml'))[:64]
    except OSError:
        return ()
    for name in names:
        try:
            content = _read_file(config / name, 256 * 1024, private=True)
            if name == 'xunjia.env':
                for line in content.splitlines():
                    key, separator, value = line.removeprefix('export ').partition('=')
                    if separator and _SECRET_KEY.search(key.strip()):
                        collect(value.strip().strip('\"\''), True)
            elif name.endswith('.json'):
                collect(json.loads(content))
            else:
                import yaml
                collect(yaml.safe_load(content))
        except (OSError, ValueError, RecursionError):
            continue
        except ImportError:
            continue
        except Exception:
            # Invalid private configuration must never be reflected in diagnostics.
            continue
    return tuple(values)


def _metadata(value, secrets, depth=0):
    if depth > 4 or not isinstance(value, dict):
        return {}
    result = {}
    for key in sorted(_META.intersection(value)):
        item = value[key]
        if item is None or isinstance(item, bool):
            result[key] = item
        elif isinstance(item, int) and -(2 ** 63) <= item < 2 ** 63:
            result[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[key] = item
        elif isinstance(item, dict):
            result[key] = _metadata(item, secrets, depth + 1)
        elif isinstance(item, str) and _IDENTIFIER.fullmatch(item):
            result[key] = redact(item, secrets)
    return result


def _code(value, secrets):
    if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', value):
        return redact(value, secrets)
    return 'UPGRADE_DIAGNOSTIC_RECORDED'


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS upgrade_event(
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
        at REAL NOT NULL, phase TEXT NOT NULL, code TEXT NOT NULL,
        message TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}')''')
    db.execute('CREATE INDEX IF NOT EXISTS upgrade_event_job ON upgrade_event(job_id,id)')


def _validate_job(job_id):
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError('INVALID_JOB')


def event(store, job_id, phase, code, message='', **details):
    """Append one safe event. Free-form messages/stderr are deliberately not persisted."""
    _validate_job(job_id)
    secrets = _secrets(store)
    phase = phase if phase in _PHASES else 'unknown'
    code = _code(code, secrets)
    safe = {'job_id': job_id, 'at': time.time(), 'phase': phase, 'code': code,
            'message': _CODES.get(code, _MESSAGES[phase]),
            'details': _metadata(details, secrets)}
    with store.db() as db:
        if db.execute('SELECT id FROM job WHERE id=?', (job_id,)).fetchone() is None:
            raise ValueError('UPGRADE_JOB_NOT_FOUND')
        _schema(db)
        cursor = db.execute('''INSERT INTO upgrade_event(job_id,at,phase,code,message,details)
            VALUES(?,?,?,?,?,?)''', (job_id, safe['at'], phase, code, safe['message'],
                                   json.dumps(safe['details'], ensure_ascii=False)))
        safe['id'] = cursor.lastrowid
    return safe


def _object(value):
    try:
        result = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return {}
    return result if isinstance(result, dict) else {}


def snapshot(store, job_id):
    """Return the public job summary and chronological events, without owner data."""
    _validate_job(job_id)
    secrets = _secrets(store)
    with store.db() as db:
        row = db.execute('SELECT * FROM job WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('UPGRADE_JOB_NOT_FOUND')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upgrade_event'").fetchone()
        rows = db.execute('SELECT * FROM upgrade_event WHERE job_id=? ORDER BY id',
                          (job_id,)).fetchall() if exists else []
    row = dict(row)
    job = {key: row[key] for key in ('id', 'created_at', 'updated_at')}
    job['state'] = row['state'] if row['state'] in _PHASES else 'unknown'
    job['code'] = _code(row['code'], secrets) if row['code'] else ''
    job.update(_metadata({k: row[k] for k in ('target_build', 'package_sha256')}, secrets))
    job['result'] = _metadata(_object(row['result']), secrets)
    events = []
    for row in rows:
        phase = row['phase'] if row['phase'] in _PHASES else 'unknown'
        code = _code(row['code'], secrets)
        events.append({'id': row['id'], 'job_id': job_id, 'at': row['at'],
                       'phase': phase, 'code': code,
                       'message': _CODES.get(code, _MESSAGES[phase]),
                       'details': _metadata(_object(row['details']), secrets)})
    return {'job': job, 'events': events}


def log_text(store, job_id):
    """Downloadable event report plus safe structured summaries of fixed log tails."""
    data = snapshot(store, job_id)
    secrets = _secrets(store)
    lines = ['升级诊断（已脱敏）', json.dumps(data, ensure_ascii=False, indent=2)]
    for name in ('check.log', 'apply.log'):
        try:
            text = _read_file(Path(store.root) / 'jobs' / job_id / name, LOG_LIMIT, tail=True)
        except (OSError, ValueError):
            continue
        lines.append('\n' + name + '（仅展示结构化摘要，最多读取末尾 64 KiB）')
        safe_lines = []
        # Arbitrary output cannot be proven credential-free. Only publish known
        # codes and allowlisted JSON metadata; redact() is additional protection.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in _CODES:
                safe_lines.append(stripped + ': ' + _CODES[stripped])
            else:
                metadata = _metadata(_object(stripped), secrets)
                if metadata:
                    safe_lines.append(json.dumps(metadata, ensure_ascii=False))
        lines.extend(safe_lines or ['原始输出已隐藏，请参考上述阶段和错误码。'])
    return redact('\n'.join(lines) + '\n', secrets)
