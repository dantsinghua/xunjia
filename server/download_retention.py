"""Bounded cleanup of known download journals, retaining durable business facts.

Only redundant, acknowledged payload bytes expire. Identity rows, decoded data,
UNKNOWN outcomes and unacknowledged inputs are never removed here.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

DOWNLOAD_RETENTION_DAYS = 30
DOWNLOAD_RETENTION_SECONDS = DOWNLOAD_RETENTION_DAYS * 86400


def _safe_path(path: Path, root: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if not path.resolve().is_relative_to(root.resolve()):
        return False
    return not any(parent.is_symlink() for parent in (path.parent, *path.parents))


@contextmanager
def _journal(path):
    db = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=rw', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        with db:
            yield db
    finally:
        db.close()


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS download_retention (
        source TEXT,seq INTEGER,observed_at REAL NOT NULL,sha256 TEXT,
        size INTEGER,pruned_at REAL,code TEXT,PRIMARY KEY(source,seq))''')
    db.execute('''CREATE TABLE IF NOT EXISTS download_retention_cursor (
        source TEXT PRIMARY KEY,seq INTEGER NOT NULL,ceiling INTEGER)''')
    if 'ceiling' not in {row[1] for row in db.execute('PRAGMA table_info(download_retention_cursor)')}:
        db.execute('ALTER TABLE download_retention_cursor ADD COLUMN ceiling INTEGER')


def _stamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _candidates(db, source, stamp_column, now, limit):
    # Source and column are fixed internal literals, never request parameters.
    cursor = db.execute('SELECT seq,ceiling FROM download_retention_cursor WHERE source=?', (source,)).fetchone()
    position = cursor['seq'] if cursor else 0
    ceiling = cursor['ceiling'] if cursor else None
    if ceiling is None or position >= ceiling:
        position = 0
        ceiling = db.execute('SELECT COALESCE(MAX(seq),0) FROM ' + source).fetchone()[0]
    fields = 'seq,state,scope,received_at' if source == 'fg_download' else 'seq,state,at'
    if stamp_column not in {row[1] for row in db.execute('PRAGMA table_info(' + source + ')')}:
        fields = fields.replace(',' + stamp_column, '')
    query = f'SELECT {fields} FROM {source} WHERE blob IS NOT NULL AND seq>? AND seq<=? ORDER BY seq LIMIT ?'
    rows = db.execute(query, (position, ceiling, limit)).fetchall()
    if not rows and position:
        ceiling = db.execute('SELECT COALESCE(MAX(seq),0) FROM ' + source).fetchone()[0]
        rows = db.execute(query, (0, ceiling, limit)).fetchall()
    for row in rows:
        db.execute('INSERT OR IGNORE INTO download_retention(source,seq,observed_at) VALUES(?,?,?)',
                   (source, row['seq'], now))
    if rows:
        db.execute('INSERT INTO download_retention_cursor VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET seq=excluded.seq,ceiling=excluded.ceiling',
                   (source, rows[-1]['seq'], ceiling))
    return rows


def prune_journal(path, *, now=None, limit=100):
    """Remove at most ``limit`` redundant blobs from one existing SQLite file."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('DOWNLOAD_CLEANUP_LIMIT_INVALID')
    now = time.time() if now is None else now
    cutoff = now - DOWNLOAD_RETENTION_SECONDS
    report = {'scanned': 0, 'payloads_removed': 0, 'bytes_released': 0,
              'protected': 0, 'observing': 0, 'retention_days': DOWNLOAD_RETENTION_DAYS}
    with _journal(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        source = 'fg_download' if 'fg_download' in tables else 'ip_input' if 'ip_input' in tables else None
        if source is None:
            return report
        _schema(db)
        stamp_column = 'received_at' if source == 'fg_download' else 'at'
        columns = {row[1] for row in db.execute('PRAGMA table_info(' + source + ')')}
        for row in _candidates(db, source, stamp_column, now, limit):
            report['scanned'] += 1
            observed = db.execute('SELECT observed_at FROM download_retention WHERE source=? AND seq=?',
                                  (source, row['seq'])).fetchone()[0]
            stamp = row[stamp_column] if stamp_column in columns else None
            if not _stamp(stamp):
                stamp = observed
            if stamp > cutoff:
                report['observing'] += 1
                continue
            archived = False
            if source == 'fg_download' and row['state'] == 'STAGED' and {'tx_source', 'tx_message'} <= tables:
                saved = db.execute('''SELECT m.data,m.kind,m.id FROM tx_source s JOIN tx_message m
                    ON m.scope=s.scope AND m.kind=s.kind AND m.id=s.id
                    WHERE s.scope=? AND s.position=? AND s.epoch='v1'
                    AND s.stream IN ('file-gateway-service','file-gateway-collector')
                    AND s.acked=1 AND m.acked=1''', (row['scope'], row['seq'])).fetchall()
                if len(saved) == 1:
                    try:
                        data = json.loads(saved[0]['data'])
                        archived = (isinstance(data, dict) and data.get('kind') == saved[0]['kind']
                                    and data.get('message_id') == saved[0]['id'] and isinstance(data.get('payload'), dict))
                    except (ValueError, TypeError):
                        pass
            elif source == 'ip_input' and row['state'] == 'APPLIED' and 'ip_input_data' in tables:
                saved = db.execute('SELECT body FROM ip_input_data WHERE seq=?', (row['seq'],)).fetchone()
                if saved:
                    try:
                        data = json.loads(saved[0])
                        archived = isinstance(data, dict) and isinstance(data.get('payload'), dict)
                    except (ValueError, TypeError):
                        pass
            if not archived:
                report['protected'] += 1
                db.execute('UPDATE download_retention SET code=? WHERE source=? AND seq=?',
                           ('UNACKNOWLEDGED_OR_NOT_ARCHIVED', source, row['seq']))
                continue
            blob = bytes(db.execute('SELECT blob FROM ' + source + ' WHERE seq=?', (row['seq'],)).fetchone()[0])
            db.execute('UPDATE download_retention SET sha256=?,size=?,pruned_at=?,code=? WHERE source=? AND seq=?',
                       (hashlib.sha256(blob).hexdigest(), len(blob), now, 'PAYLOAD_PRUNED', source, row['seq']))
            db.execute('UPDATE ' + source + ' SET blob=NULL WHERE seq=?', (row['seq'],))
            report['payloads_removed'] += 1
            report['bytes_released'] += len(blob)
    return report


def known_journals(root, directory_root=None):
    """Enumerate only the product's fixed institution/account layouts."""
    root = Path(root)
    found = {}
    for base in {root, Path(directory_root) if directory_root else root}:
        if not base.is_dir() or base.is_symlink():
            continue
        for institution in base.iterdir():
            if not re.fullmatch(r'[a-f0-9]{32}', institution.name) or not institution.is_dir() or institution.is_symlink():
                continue
            directory = institution / 'directory.db'
            if _safe_path(directory, base):
                found[str(directory)] = ('journal', directory)
            if base != root:
                continue
            accounts = institution / 'accounts'
            if accounts.is_dir() and not accounts.is_symlink():
                for path in accounts.iterdir():
                    if re.fullmatch(r'[a-f0-9]{64}\.db', path.name) and _safe_path(path, root):
                        found[str(path)] = ('journal', path)
            archive = institution / 'archive' / 'archive.db'
            if _safe_path(archive, root):
                found[str(archive)] = ('archive', archive)
    return [found[key] for key in sorted(found)]


def prune_known_downloads(root, *, directory_root=None, now=None, after='', file_limit=8, row_limit=100):
    """Rotate bounded work across current and inactive institution/account data."""
    if type(file_limit) is not int or not 1 <= file_limit <= 64:
        raise ValueError('DOWNLOAD_CLEANUP_FILE_LIMIT_INVALID')
    now = time.time() if now is None else now
    report = {'at': now, 'retention_days': DOWNLOAD_RETENTION_DAYS, 'journals_checked': 0,
              'payloads_removed': 0, 'media_removed': 0, 'protected': 0, 'observing': 0,
              'errors': [], 'next': ''}
    paths = known_journals(root, directory_root)
    pending = [item for item in paths if str(item[1]) > after]
    if not pending:
        pending = paths
    for kind, path in pending[:file_limit]:
        report['next'] = str(path)
        try:
            if kind == 'archive':
                from .retained_archive import InstitutionArchive
                archive = InstitutionArchive(path.parent, clock=lambda: now, db_timeout=1)
                outcome = archive.prune(limit=min(row_limit, 50))
            else:
                outcome = prune_journal(path, now=now, limit=row_limit)
            report['journals_checked'] += 1
            for key in ('payloads_removed', 'media_removed', 'protected', 'observing'):
                report[key] += outcome.get(key, 0)
        except Exception as exc:
            # Diagnostics contain types and product paths, never response bytes.
            report['errors'].append({'file': str(path), 'code': type(exc).__name__})
    report['more_journals'] = len(pending) > file_limit
    return report
