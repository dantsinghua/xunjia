"""Single-consumer FIFO preservation with authenticated per-collector routing."""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager

from .protocol import ProtocolError, token
from .collector_directory import project

UP_KINDS = frozenset(('catalog', 'contacts', 'event', 'receipt', 'manifest', 'session_reset_ack'))
DOWN_KINDS = frozenset(('command', 'cancel', 'query', 'session_reset'))


class QueueStop(RuntimeError):
    pass


class Journal:
    def __init__(self, path, upstream):
        self.path = Path(path)
        self.upstream = upstream
        self.lock = threading.RLock()
        self.directory_codec = None
        self.default_pair = None
        self._next_directory_follow = 0.0
        self.directory_follow_error = None
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS upload(h TEXT PRIMARY KEY,lane TEXT,name TEXT,key TEXT,state TEXT,delivered INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS receive(id INTEGER PRIMARY KEY AUTOINCREMENT,state TEXT,h TEXT,name TEXT,body BLOB);
                CREATE TABLE IF NOT EXISTS routing(h TEXT PRIMARY KEY,lane TEXT NOT NULL,workbench_id TEXT NOT NULL,
                    account_uid TEXT NOT NULL,kind TEXT NOT NULL,envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS directory_subscription(singleton INTEGER PRIMARY KEY CHECK(singleton=1),enabled INTEGER NOT NULL);''')
            columns={row['name'] for row in db.execute('PRAGMA table_info(receive)')}
            if 'received_at' not in columns:
                db.execute('ALTER TABLE receive ADD COLUMN received_at REAL NULL')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def configure_directory(self, codec, default_binding=None):
        with self.lock:
            self.directory_codec = codec
            binding = default_binding or codec.binding
            self.default_pair = ((binding.workbench_id, binding.account_uid)
                                 if hasattr(binding,'workbench_id') and hasattr(binding,'account_uid') else None)
            with self.db() as db:
                rows = db.execute('SELECT h,body FROM receive WHERE body IS NOT NULL').fetchall()
            for row in rows:
                try:
                    self._index(row['h'], bytes(row['body']), allow_expired=True)
                except ProtocolError:
                    # Preserve unauthenticated history without projecting or relabeling it.
                    continue

    def _index(self, h, body, *, allow_expired=False, lane=None, pair=None):
        if self.directory_codec is None:
            return None
        if h != hashlib.sha256(body).hexdigest():
            raise ProtocolError('保全包摘要与原文不符')
        env = self.directory_codec.unpack_owned(body, allow_expired=allow_expired)
        if (env['expires_at'] <= self.directory_codec.clock()
                and env['kind'] not in ('catalog', 'contacts')):
            raise ProtocolError('过期业务信封不得路由')
        if env['kind'] == 'contacts' and not isinstance(env['payload'].get('contacts'), list):
            raise ProtocolError('通讯录内容无效')
        actual_lane = 'up' if env['kind'] in UP_KINDS else 'down'
        actual_pair = (env['workbench_id'], env['account_uid'])
        if lane is not None and lane != actual_lane:
            raise ProtocolError('签封方向与队列不符')
        if pair is not None and pair != actual_pair:
            raise ProtocolError('签封身份与目标采集端账号不符')
        # Business text stays in the retained signed/ciphered source. Only
        # directory publications need payloads for the local read-only view.
        metadata = env if env['kind'] in ('catalog', 'contacts') else {
            key: env[key] for key in ('owner_user_id', 'workbench_id', 'account_uid',
                                      'kind', 'created_at', 'expires_at')}
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO routing VALUES(?,?,?,?,?,?)',
                       (h, actual_lane, *actual_pair, env['kind'], json.dumps(metadata, ensure_ascii=False)))
        return env

    def _pair(self, pair):
        if pair is not None:
            return tuple(token(x) for x in pair)
        return self.default_pair

    def _owner(self):
        return self.directory_codec.binding.owner_user_id if self.directory_codec is not None else ''

    def upload(self, lane, name, body, pair=None):
        if lane not in ('up', 'down'):
            raise ValueError('lane')
        pair = self._pair(pair)
        h = hashlib.sha256(body).hexdigest()
        with self.lock:
            self._index(h, body, lane=lane, pair=pair)
            with self.db() as db:
                old = db.execute('SELECT * FROM upload WHERE h=?', (h,)).fetchone()
                if old:
                    if old['lane'] != lane or old['name'] != name:
                        raise QueueStop('IDENTITY_CONFLICT')
                    if old['state'] != 'UPLOADED':
                        raise QueueStop('UPLOAD_UNKNOWN')
                    return {'file_key': old['key'], 'file_name': name}
                db.execute('INSERT INTO upload(h,lane,name,key,state) VALUES(?,?,?,?,?)',
                           (h, lane, name, '', 'UPLOADING'))
            try:
                result = self.upstream.upload(name, body)
            except Exception:
                with self.db() as db:
                    db.execute('UPDATE upload SET state=? WHERE h=?', ('UNKNOWN', h))
                raise QueueStop('UPLOAD_UNKNOWN') from None
            with self.db() as db:
                db.execute('UPDATE upload SET state=?,key=? WHERE h=?', ('UPLOADED', result['file_key'], h))
            return result

    def pending(self, lane, pair=None, *, diagnostic=False):
        with self.lock, self.db() as db:
            pair = self._pair(pair)
            if pair is None:
                return db.execute('SELECT COUNT(*) FROM upload WHERE lane=? AND state=? AND delivered=0',
                                  (lane, 'UPLOADED')).fetchone()[0]
            return db.execute('''SELECT COUNT(DISTINCT m.h) FROM routing m
                LEFT JOIN upload u ON u.h=m.h LEFT JOIN receive r ON r.h=m.h
                WHERE m.lane=? AND m.workbench_id=? AND m.account_uid=?
                AND json_extract(m.envelope,'$.owner_user_id')=?
                AND json_extract(m.envelope,'$.expires_at')>?
                AND (r.state='STORED' OR (u.state='UPLOADED' AND u.delivered=0))''', (lane, *pair, self._owner(), self.directory_codec.clock())).fetchone()[0]

    def buffer(self):
        """Preserve upstream files without delivering or acknowledging any lane."""
        with self.lock:
            count = self.upstream.pending()
            for _ in range(min(count, 50)):
                with self.db() as db:
                    ident = db.execute('INSERT INTO receive(state) VALUES(?)', ('RECEIVING',)).lastrowid
                try:
                    value = self.upstream.download()
                except Exception:
                    with self.db() as db:
                        db.execute('UPDATE receive SET state=? WHERE id=?', ('UNKNOWN', ident))
                    raise QueueStop('DOWNLOAD_UNKNOWN') from None
                if value is None:
                    with self.db() as db:
                        db.execute('UPDATE receive SET state=? WHERE id=?', ('EMPTY', ident))
                    break
                h = hashlib.sha256(value.content).hexdigest()
                with self.db() as db:
                    known = db.execute('SELECT * FROM upload WHERE h=?', (h,)).fetchone()
                valid = known is not None and known['name'] == value.name
                if self.directory_codec is not None:
                    try:
                        env = self._index(h, value.content, allow_expired=True,
                                          lane=known['lane'] if valid else 'up')
                        valid = env is not None
                    except ProtocolError:
                        valid = False
                with self.db() as db:
                    db.execute('UPDATE receive SET state=?,h=?,name=?,body=?,received_at=? WHERE id=?',
                               ('STORED' if valid else 'FOREIGN', h, value.name, value.content, time.time(), ident))
                # Retain foreign/invalid files, but continue to other owned files.

    def download(self, lane, pair=None):
        with self.lock:
            pair = self._pair(pair)
            def cached():
                with self.db() as db:
                    if pair is None:
                        return db.execute('''SELECT r.* FROM receive r JOIN upload u ON u.h=r.h
                            WHERE u.lane=? AND r.state=? ORDER BY r.id LIMIT 1''', (lane, 'STORED')).fetchone()
                    return db.execute('''SELECT r.* FROM receive r JOIN routing m ON m.h=r.h
                        WHERE m.lane=? AND m.workbench_id=? AND m.account_uid=? AND r.state='STORED'
                        AND json_extract(m.envelope,'$.owner_user_id')=?
                        AND json_extract(m.envelope,'$.expires_at')>?
                        ORDER BY r.id LIMIT 1''', (lane, *pair, self._owner(), self.directory_codec.clock())).fetchone()
            row = cached()
            if row is None:
                self.buffer()
                row = cached()
            if row is None:
                return None
            with self.db() as db:
                db.execute('UPDATE receive SET state=? WHERE id=?', ('DELIVERED', row['id']))
                db.execute('UPDATE upload SET delivered=1 WHERE h=?', (row['h'],))
            return row['name'], bytes(row['body'])

    def files(self, workbench_id, account_uid, *, before=None, limit=50):
        """List retained authenticated metadata. Never fetch or acknowledge files."""
        if self.directory_codec is None:
            raise QueueStop('DIRECTORY_NOT_CONFIGURED')
        token(workbench_id);token(account_uid)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid page size')
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('Invalid page cursor')
        with self.lock, self.db() as db:
            rows=db.execute("""SELECT m.rowid AS cursor,m.kind,m.lane,m.envelope,
                COALESCE(r.name,u.name) AS name,u.state AS upload_state,r.state AS receive_state,r.received_at
                FROM routing m LEFT JOIN upload u ON u.h=m.h
                LEFT JOIN receive r ON r.id=(SELECT MAX(id) FROM receive WHERE h=m.h AND body IS NOT NULL)
                WHERE m.workbench_id=? AND m.account_uid=?
                AND json_extract(m.envelope,'$.owner_user_id')=?
                AND (u.h IS NOT NULL OR r.id IS NOT NULL)
                AND (? IS NULL OR m.rowid<?) ORDER BY m.rowid DESC LIMIT ?""",
                (workbench_id,account_uid,self._owner(),before,before,limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            name=row['name'] or ''
            items.append(dict(id=row['cursor'],name=name,extension=Path(name).suffix.lstrip('.'),
                              kind=row['kind'],lane=row['lane'],source_created_at=json.loads(row['envelope'])['created_at'],
                              received_at=row['received_at'],state=row['receive_state'] or row['upload_state'],
                              upload_state=row['upload_state'],receive_state=row['receive_state']))
        return dict(workbench_id=workbench_id,account_uid=account_uid,owner_user_id=self._owner(),
                    items=items,next_before=items[-1]['id'] if len(rows)>limit else None,
                    scope='retained_authenticated_files')

    def follow_directory_once(self, interval=5):
        """Continue the user's explicit directory connection on this sole consumer."""
        with self.lock:
            if self.directory_codec is None or time.monotonic()<self._next_directory_follow:return False
            with self.db() as db:
                enabled=db.execute('SELECT enabled FROM directory_subscription WHERE singleton=1').fetchone()
            if not enabled or not enabled[0]:return False
            self._next_directory_follow=time.monotonic()+interval
            try:
                self.buffer();self.directory_follow_error=None
            except Exception as exc:
                self.directory_follow_error=str(exc) if isinstance(exc,QueueStop) else 'DIRECTORY_REFRESH_UNAVAILABLE'
            return True

    def directory(self, workbench_id=None, account_uid=None, *, refresh=False):
        if self.directory_codec is None:
            raise QueueStop('DIRECTORY_NOT_CONFIGURED')
        if (workbench_id is None) != (account_uid is None):
            raise ValueError('采集端和账号须同时指定')
        if workbench_id is not None:
            token(workbench_id)
            token(account_uid)
        with self.lock:
            error = self.directory_follow_error
            if refresh:
                with self.db() as db:
                    db.execute('INSERT INTO directory_subscription VALUES(1,1) ON CONFLICT(singleton) DO UPDATE SET enabled=1')
                self._next_directory_follow=time.monotonic()+5
                try:
                    self.buffer();error=None
                except Exception as exc:
                    error = str(exc) if isinstance(exc, QueueStop) else 'DIRECTORY_REFRESH_UNAVAILABLE'
                self.directory_follow_error=error
            with self.db() as db:
                # An attempted local upload is not proof of a received publication.
                # Only source bytes actually preserved from the MQ download may
                # appear in the collector directory.
                rows = db.execute("SELECT DISTINCT m.envelope FROM routing m JOIN receive r ON r.h=m.h "
                                  "WHERE m.lane='up' AND m.kind IN ('catalog','contacts') "
                                  "AND r.state IN ('STORED','DELIVERED') AND r.body IS NOT NULL "
                                  "AND json_extract(m.envelope,'$.owner_user_id')=?", (self._owner(),)).fetchall()
            value = project([json.loads(x[0]) for x in rows], workbench_id=workbench_id,
                            account_uid=account_uid, now=self.directory_codec.clock())
            value.update(refresh_requested=bool(refresh), refresh_error=error)
            return value

    def stats(self):
        with self.lock, self.db() as db:
            return {'upload': [dict(x) for x in db.execute('SELECT lane,state,delivered,COUNT(*) AS n FROM upload GROUP BY lane,state,delivered')],
                    'receive': [dict(x) for x in db.execute('SELECT state,COUNT(*) AS n FROM receive GROUP BY state')]}
