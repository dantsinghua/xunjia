"""Resolve only observed bond identifiers; unknown identities remain pending.

External dictionaries are read-only. The one worker writes validated names to
the existing quote dictionary without blocking the WebSocket receive loop.
"""
from __future__ import annotations
import asyncio
import datetime as dt
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import stat
import time

_FIELDS = ('bond_uni_code', 'bond_code', 'bond_short_name', 'com_short_name')
_CAMEL = ('bondUniCode', 'bondCode', 'bondShortName', 'comShortName')


class DictionaryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def identifiers(values, *, maximum=None):
    if not isinstance(values, (list, tuple, set)):
        raise DictionaryError('DICTIONARY_IDS_INVALID')
    result, seen = [], set()
    for value in values:
        if type(value) is not int or not 0 < value <= 9223372036854775807:
            raise DictionaryError('DICTIONARY_ID_INVALID')
        if value not in seen:
            result.append(value)
            seen.add(value)
    if maximum is not None and len(result) > maximum:
        raise DictionaryError('DICTIONARY_BATCH_TOO_LARGE')
    return result


def normalize_rows(rows, requested):
    requested = set(identifiers(requested))
    if not isinstance(rows, list):
        raise DictionaryError('DICTIONARY_ROWS_INVALID')
    result = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise DictionaryError('DICTIONARY_ROW_INVALID')
        value = {}
        for key, camel in zip(_FIELDS, _CAMEL):
            if key in raw and camel in raw and raw[key] != raw[camel]:
                raise DictionaryError('DICTIONARY_IDENTITY_CONFLICT')
            value[key] = raw[key] if key in raw else raw.get(camel)
        uni = value['bond_uni_code']
        if type(uni) is not int or uni not in requested:
            raise DictionaryError('DICTIONARY_UNREQUESTED_ID')
        for key, size in (('bond_code', 32), ('bond_short_name', 64)):
            text = value[key]
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > size or any(ord(c) < 32 for c in text):
                raise DictionaryError('DICTIONARY_IDENTITY_MISSING')
            value[key] = text.strip()
        if value['bond_code'] in ('—', '-') or value['bond_code'].startswith('UNI') or value['bond_short_name'].startswith('未入字典'):
            raise DictionaryError('DICTIONARY_PLACEHOLDER_FORBIDDEN')
        company = value['com_short_name']
        if company is not None:
            if not isinstance(company, str) or len(company.strip()) > 64 or any(ord(c) < 32 for c in company):
                raise DictionaryError('DICTIONARY_COMPANY_INVALID')
            value['com_short_name'] = company.strip() or None
        if uni in result and result[uni] != value:
            raise DictionaryError('DICTIONARY_IDENTITY_CONFLICT')
        result[uni] = value
    return list(result.values())


def fetch_innodealing(ids, cookie, device_id, *, client_factory=None):
    ids = identifiers(ids, maximum=100)
    if not ids:
        return []
    if not isinstance(cookie, str) or not cookie.strip() or not isinstance(device_id, str) or not device_id.strip():
        raise DictionaryError('DICTIONARY_CREDENTIALS_MISSING')
    import httpx
    from collector.qb_gateway import API, FE_VER
    from collector.qb_sign import sign_params
    from relay.site_egress import httpx_request
    url = API.rstrip('/') + '/onshore-bond-basic/api/bond/filter/simple/v3/onshore/batch'
    headers = {'Cookie': cookie, 'Content-Type': 'text/plain',
               'Origin': 'https://web.innodealing.com', 'Referer': 'https://web.innodealing.com/'}
    url, headers, extensions = httpx_request(url, headers)
    options = {'extensions': extensions} if extensions else {}
    with (client_factory or httpx.Client)(proxy=None, timeout=15, verify=True, trust_env=False) as client:
        response = client.post(url, params=sign_params({}, device_id, FE_VER), headers=headers,
                               content=json.dumps({'bondUniCodes': ids}, separators=(',', ':')), **options)
        response.raise_for_status()
        try:
            body = response.json()
        except (ValueError, TypeError):
            raise DictionaryError('DICTIONARY_RESPONSE_INVALID') from None
    if not isinstance(body, dict) or type(body.get('code')) not in (int, float) or body['code'] != 0:
        raise DictionaryError('DICTIONARY_RESPONSE_REJECTED')
    rows = body.get('datas') if body.get('datas') is not None else body.get('data')
    return normalize_rows(rows, ids)


def private_file(path, *, maximum):
    path = Path(path)
    try:
        info = path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or not 0 < info.st_size <= maximum:
            raise ValueError()
        return path.read_text(encoding='utf-8-sig')
    except (OSError, ValueError):
        raise DictionaryError('DICTIONARY_PRIVATE_FILE_INVALID') from None


def identifier(value, *, qualified=False):
    pattern = r'[A-Za-z_][A-Za-z0-9_]{0,63}'
    if not isinstance(value, str) or not re.fullmatch(pattern + (r'(?:\.' + pattern + ')?' if qualified else ''), value):
        raise DictionaryError('DICTIONARY_SQL_IDENTIFIER_INVALID')
    return '.'.join('`' + part + '`' for part in value.split('.'))


class JsonDictionary:
    def __init__(self, path):
        try:
            rows = json.loads(private_file(path, maximum=32 * 1024 * 1024))
            if not isinstance(rows, list) or len(rows) > 200000:
                raise ValueError()
            ids = [row['bond_uni_code'] for row in rows]
            self.rows = {row['bond_uni_code']: row for row in normalize_rows(rows, ids)}
        except DictionaryError:
            raise
        except (ValueError, TypeError, KeyError):
            raise DictionaryError('DICTIONARY_JSON_INVALID') from None

    def lookup(self, ids):
        return [dict(self.rows[uni]) for uni in identifiers(ids, maximum=100) if uni in self.rows]


class MySQLDictionary:
    def __init__(self, value, directory, *, connect=None):
        connection = value.get('connection')
        if not isinstance(connection, dict) or set(connection) - {'host', 'port', 'user', 'database', 'password_file'}:
            raise DictionaryError('DICTIONARY_MYSQL_CONFIG_INVALID')
        for key in ('host', 'user', 'database', 'password_file'):
            if not isinstance(connection.get(key), str) or not connection[key].strip():
                raise DictionaryError('DICTIONARY_MYSQL_CONFIG_INVALID')
        port = connection.get('port', 3306)
        if type(port) is not int or not 1 <= port <= 65535:
            raise DictionaryError('DICTIONARY_MYSQL_CONFIG_INVALID')
        password_path = Path(connection['password_file'])
        if not password_path.is_absolute():
            password_path = directory / password_path
        password = private_file(password_path, maximum=16384).rstrip('\r\n')
        if not password or '\n' in password or '\r' in password:
            raise DictionaryError('DICTIONARY_MYSQL_PASSWORD_INVALID')
        self.options = {key: connection[key] for key in ('host', 'user', 'database')}
        self.options.update(port=port, password=password, charset='utf8mb4', autocommit=False,
                            connect_timeout=5, read_timeout=8, write_timeout=8)
        self.table = identifier(value.get('table'), qualified=True)
        columns = value.get('columns')
        if not isinstance(columns, dict) or set(columns) - set(_FIELDS) or not set(_FIELDS[:3]).issubset(columns):
            raise DictionaryError('DICTIONARY_MYSQL_COLUMNS_INVALID')
        self.columns = {key: identifier(column) for key, column in columns.items()}
        self.connect = connect

    def lookup(self, ids):
        ids = identifiers(ids, maximum=100)
        if not ids:
            return []
        import pymysql
        connect = self.connect or (lambda **kwargs: pymysql.connect(**kwargs, cursorclass=pymysql.cursors.DictCursor))
        connection = connect(**self.options)
        try:
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                selection = ','.join(self.columns.get(key, 'NULL') + ' AS `' + key + '`' for key in _FIELDS)
                cursor.execute('SELECT ' + selection + ' FROM ' + self.table + ' WHERE ' + self.columns['bond_uni_code']
                               + ' IN (' + ','.join(['%s'] * len(ids)) + ')', tuple(ids))
                rows = cursor.fetchall()
            return normalize_rows(list(rows), ids)
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()


def load_external(path, *, connect=None):
    path = Path(path)
    try:
        value = json.loads(private_file(path, maximum=65536))
    except (ValueError, TypeError):
        raise DictionaryError('DICTIONARY_CONFIG_INVALID') from None
    if not isinstance(value, dict):
        raise DictionaryError('DICTIONARY_CONFIG_INVALID')
    if value.get('source') == 'json' and set(value) == {'source', 'file'}:
        if not isinstance(value['file'], str) or not value['file']:
            raise DictionaryError('DICTIONARY_CONFIG_INVALID')
        source = Path(value['file'])
        return JsonDictionary(source if source.is_absolute() else path.parent / source)
    if value.get('source') == 'mysql' and set(value) == {'source', 'connection', 'table', 'columns'}:
        return MySQLDictionary(value, path.parent, connect=connect)
    raise DictionaryError('DICTIONARY_CONFIG_INVALID')


class DictionaryResolver:
    def __init__(self, cookie, device_id, *, external=None, fetch=None):
        self.cookie, self.device_id = cookie, device_id
        self.external, self.fetch = external, fetch or fetch_innodealing

    def resolve(self, ids):
        ids = identifiers(ids, maximum=100)
        rows = normalize_rows(self.external.lookup(ids), ids) if self.external is not None else []
        found = {row['bond_uni_code']: row for row in rows}
        missing = [uni for uni in ids if uni not in found]
        if missing:
            try:
                fetched = normalize_rows(self.fetch(missing, self.cookie, self.device_id), missing)
            except Exception:
                if not found:
                    raise
                fetched = []
            found.update({row['bond_uni_code']: row for row in fetched})
        return [found[uni] for uni in ids if uni in found]


def resolver_from_env(cookie, device_id, environ=None):
    environ = os.environ if environ is None else environ
    mode, config = environ.get('QB_DICTIONARY_SOURCE', ''), environ.get('QB_DICTIONARY_CONFIG', '')
    if not mode:
        if config:
            raise DictionaryError('DICTIONARY_EXPLICIT_MODE_REQUIRED')
        return None
    if mode != 'innodealing':
        raise DictionaryError('DICTIONARY_SOURCE_INVALID')
    if not cookie or not device_id:
        raise DictionaryError('DICTIONARY_CREDENTIALS_MISSING')
    external = load_external(config) if config else None
    return DictionaryResolver(cookie, device_id, external=external)


def load_pending_ids(store, *, since=None, limit=10000):
    """Recover current-day unknown IDs from bounded recent real rows, not history."""
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise DictionaryError('DICTIONARY_RECOVERY_LIMIT_INVALID')
    since = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) if since is None else since
    if not isinstance(since, dt.datetime):
        raise DictionaryError('DICTIONARY_RECOVERY_WINDOW_INVALID')
    found = set()
    with store._lock:
        store.conn.ping(reconnect=True)
        with store.conn.cursor() as cursor:
            for table in ('qb_live_best_quote', 'qb_live_trade'):
                remaining = limit - len(found)
                if not remaining:
                    break
                # Bound the inner primary-key scan before filtering time or names.
                sql = ('SELECT DISTINCT q.bond_uni_code FROM (SELECT bond_uni_code,capture_time FROM ' + table
                       + ' ORDER BY id DESC LIMIT %s) q LEFT JOIN qb_bond_dict d ON d.bond_uni_code=q.bond_uni_code '
                       'WHERE q.capture_time >= %s AND (d.bond_uni_code IS NULL OR d.bond_code IS NULL '
                       "OR TRIM(d.bond_code)='' OR d.bond_code IN ('—','-') OR d.bond_code LIKE %s "
                       "OR d.bond_short_name IS NULL OR TRIM(d.bond_short_name)='' OR d.bond_short_name LIKE %s) LIMIT %s")
                cursor.execute(sql, (limit, since, 'UNI%', '未入字典%', remaining))
                for row in cursor.fetchall():
                    uni = row.get('bond_uni_code') if isinstance(row, dict) else row[0]
                    if type(uni) is int and 0 < uni <= 9223372036854775807:
                        found.add(uni)
    return sorted(found)[:limit]


async def _finish_io(call, *args):
    task = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


class DictionaryWorker:
    def __init__(self, resolver, store, *, update=None, interval=1.0, retry_seconds=5, max_pending=10000):
        if not callable(getattr(resolver, 'resolve', None)) or interval < 0 or retry_seconds < 0 or not 1 <= max_pending <= 10000:
            raise DictionaryError('DICTIONARY_WORKER_CONFIG_INVALID')
        self.resolver, self.store, self.update = resolver, store, update
        self.interval, self.retry_seconds, self.max_pending = interval, retry_seconds, max_pending
        self.pending, self.known = set(), set()
        self._due, self._attempts = {}, {}
        self._event, self._lock = asyncio.Event(), asyncio.Lock()
        self.task, self._closed = None, False
        self.resolved, self.failures = 0, 0

    def enqueue(self, uni):
        if self._closed or type(uni) is not int or not 0 < uni <= 9223372036854775807 or uni in self.known or uni in self.pending:
            return False
        if len(self.pending) >= self.max_pending:
            return False  # The real quote remains stored; later frames may enqueue it.
        self.pending.add(uni)
        self._event.set()
        return True

    def _process(self, batch):
        store = self.store
        with store._lock:
            store.conn.ping(reconnect=True)
            with store.conn.cursor() as cursor:
                cursor.execute('SELECT ' + ','.join(_FIELDS) + ' FROM qb_bond_dict WHERE bond_uni_code IN ('
                               + ','.join(['%s'] * len(batch)) + ')', tuple(batch))
                existing = cursor.fetchall()
        known = set()
        for raw in existing:
            value = raw if isinstance(raw, dict) else dict(zip(_FIELDS, raw))
            try:
                known.update(row['bond_uni_code'] for row in normalize_rows([value], batch))
            except DictionaryError:
                pass  # Historical empty/placeholder entries remain unresolved.
        missing = [uni for uni in batch if uni not in known]
        rows = normalize_rows(self.resolver.resolve(missing), missing) if missing else []
        if rows:
            with store._lock:
                store.conn.ping(reconnect=True)
                store.conn.begin()
                try:
                    with store.conn.cursor() as cursor:
                        cursor.executemany('INSERT INTO qb_bond_dict (' + ','.join(_FIELDS) + ') VALUES (%s,%s,%s,%s) '
                                           'ON DUPLICATE KEY UPDATE bond_code=VALUES(bond_code),bond_short_name=VALUES(bond_short_name),'
                                           'com_short_name=COALESCE(VALUES(com_short_name),com_short_name)',
                                           [[row[key] for key in _FIELDS] for row in rows])
                    store.conn.commit()
                except BaseException:
                    store.conn.rollback()
                    raise
        return known | {row['bond_uni_code'] for row in rows}, len(rows)

    async def run_once(self):
        async with self._lock:
            now = time.monotonic()
            batch = sorted(uni for uni in self.pending if self._due.get(uni, 0) <= now)[:100]
            if not batch or self._closed:
                return False
            try:
                known, written = await _finish_io(self._process, batch)
            except Exception:
                self.failures += 1
                known, written = set(), 0
            self.known.update(known)
            self.pending.difference_update(known)
            self.resolved += written
            for uni in batch:
                if uni in known:
                    self._due.pop(uni, None)
                    self._attempts.pop(uni, None)
                else:
                    attempt = min(self._attempts.get(uni, 0) + 1, 5)
                    self._attempts[uni] = attempt
                    self._due[uni] = time.monotonic() + min(60, self.retry_seconds * 2 ** (attempt - 1))
            if self.update:
                with suppress(Exception):
                    self.update(dictionary_pending=len(self.pending), dictionary_resolved=self.resolved, dictionary_failures=self.failures)
            return True

    def start(self):
        if self._closed:
            raise DictionaryError('DICTIONARY_WORKER_CLOSED')
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name='innodealing-bond-dictionary')
        return self.task

    async def _run(self):
        try:
            recovered = await _finish_io(lambda: load_pending_ids(self.store, limit=self.max_pending))
            for uni in recovered:
                self.enqueue(uni)
        except Exception:
            self.failures += 1  # New incoming real quotes still enter this same queue.
        while not self._closed:
            await self.run_once()
            if self.pending:
                await asyncio.sleep(self.interval)
            else:
                self._event.clear()
                if not self.pending and not self._closed:
                    await self._event.wait()

    async def close(self):
        self._closed = True
        self._event.set()
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
