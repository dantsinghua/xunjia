"""Collector-side durable command execution boundary shared by OSS and mail.

Only receive() accepts network bytes. execute()/cancel()/reconcile() are trusted
internal calls after Codec verification. A durable pending claim precedes the
non-transactional native action; uncertain claims are never blindly executed.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from relay.protocol import (Binding, Codec, ProtocolError, RESET_FIELDS, canonical, digest,
                            result, validate_command, validate_session_reset)


@contextmanager
def account_lock(path, binding: Binding, *, blocking=True):
    lock_dir = Path(path).with_name(Path(path).name + '.locks')
    lock_dir.mkdir(parents=True, exist_ok=True)
    # OS file locks are released on process death, unlike a guessed lease.
    name = hashlib.sha256(f'{binding.workbench_id}|{binding.account_uid}'.encode()).hexdigest()
    with (lock_dir / (name + '.lock')).open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if stream.seek(0,os.SEEK_END)==0:stream.write(b'0');stream.flush()
            stream.seek(0)
            try:msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError:
                if not blocking:raise BlockingIOError('account execution in progress') from None
                raise
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ReceiverLedger:
    def __init__(self, path, binding: Binding, *, clock=time.time):
        self.path, self.binding, self.clock = Path(path), binding, clock
        self.instance_id = uuid.uuid4().hex
        self.scope = 'xunjia:'+digest([binding.workbench_id,binding.account_uid])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.with_name(self.path.name + '.locks')
        self.lock_dir.mkdir(exist_ok=True)
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS rx_command (
                scope TEXT NOT NULL, command_id TEXT NOT NULL, fingerprint TEXT NOT NULL, command_json TEXT NOT NULL,
                status TEXT NOT NULL, evidence TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(scope,command_id));
            CREATE TABLE IF NOT EXISTS rx_tombstone (
                scope TEXT NOT NULL, command_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,command_id));
            CREATE TABLE IF NOT EXISTS rx_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, command_id TEXT NOT NULL,
                status TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS rx_source (scope TEXT NOT NULL,source TEXT NOT NULL,fingerprint TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(scope,source,fingerprint));
            CREATE TABLE IF NOT EXISTS rx_native (scope TEXT NOT NULL,native_id TEXT NOT NULL,peer_id TEXT NOT NULL,command_id TEXT NOT NULL,PRIMARY KEY(scope,native_id));
            CREATE TABLE IF NOT EXISTS rx_control (scope TEXT NOT NULL, peer_id TEXT NOT NULL, min_version INTEGER NOT NULL, PRIMARY KEY(scope,peer_id));
            CREATE TABLE IF NOT EXISTS rx_deferred (scope TEXT NOT NULL,event_id TEXT NOT NULL,peer_id TEXT NOT NULL,data TEXT NOT NULL,instance TEXT NOT NULL,deadline REAL NOT NULL,state TEXT NOT NULL,PRIMARY KEY(scope,event_id));
            CREATE TABLE IF NOT EXISTS rx_manual_fence (scope TEXT NOT NULL,peer_id TEXT NOT NULL,fence_id TEXT NOT NULL,version INTEGER NOT NULL,PRIMARY KEY(scope,peer_id));
            CREATE TABLE IF NOT EXISTS rx_event_fence (scope TEXT NOT NULL,event_id TEXT NOT NULL,fence_id TEXT NOT NULL,version INTEGER NOT NULL,PRIMARY KEY(scope,event_id));
            CREATE TABLE IF NOT EXISTS rx_inbound_event (scope TEXT NOT NULL,event_id TEXT NOT NULL,peer_id TEXT NOT NULL,source_seq INTEGER,PRIMARY KEY(scope,event_id));
            CREATE TABLE IF NOT EXISTS rx_reset (scope TEXT NOT NULL,reset_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,request_json TEXT NOT NULL,expected_epoch INTEGER NOT NULL,
                account_epoch INTEGER NOT NULL,state TEXT NOT NULL,snapshot_json TEXT,
                updated REAL NOT NULL,PRIMARY KEY(scope,reset_id),UNIQUE(scope,account_epoch));
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _account_lock(self):
        return account_lock(self.path, self.binding)

    def _latest_reset(self, db):
        return db.execute('SELECT * FROM rx_reset WHERE scope=? ORDER BY account_epoch DESC LIMIT 1',
                          (self.scope,)).fetchone()

    def account_epoch(self):
        with self._db() as db:
            row = self._latest_reset(db)
            return row['account_epoch'] if row else 0

    def _epoch_permitted(self, db, command):
        row = self._latest_reset(db)
        epoch = row['account_epoch'] if row else 0
        return command.get('account_epoch', 0) == epoch and (row is None or row['state'] == 'complete')

    def reset_status(self, reset_id=None):
        with self._db() as db:
            row = (self._latest_reset(db) if reset_id is None else
                   db.execute('SELECT * FROM rx_reset WHERE scope=? AND reset_id=?',
                              (self.scope, reset_id)).fetchone())
        if row is None:
            return None
        request = json.loads(row['request_json'])
        snapshot = json.loads(row['snapshot_json']) if row['snapshot_json'] else None
        return {**self._reset_ack(request, row['state'], snapshot,
                                 'SESSION_RESET_' + row['state'].upper()),
                'state': row['state'], 'updated_at': row['updated']}

    def pending_session_resets(self):
        with self._db() as db:
            rows = db.execute("SELECT request_json FROM rx_reset WHERE scope=? AND state!='complete' ORDER BY account_epoch",
                              (self.scope,)).fetchall()
        return [json.loads(row['request_json']) for row in rows]

    @staticmethod
    def _reset_ack(request, phase, snapshot, code):
        return {**{key: request[key] for key in (*RESET_FIELDS, 'request_hash')},
                'phase': phase, 'snapshot': snapshot,
                'snapshot_hash': digest(snapshot) if snapshot is not None else '', 'code': code}

    def _reset_snapshot(self, db, request, highwater, running):
        peers = {}
        def peer(key):
            return peers.setdefault(key, {'source_watermark': 0, 'receiver_fence_id': '',
                                          'receiver_fence_version': 0, 'min_control_version': 1})
        inbound = db.execute('SELECT peer_id,source_seq FROM rx_inbound_event WHERE scope=?', (self.scope,)).fetchall()
        for row in inbound:
            entry = peer(row['peer_id'])
            if row['source_seq'] is not None:
                entry['source_watermark'] = max(entry['source_watermark'], row['source_seq'])
        for row in db.execute('SELECT peer_id,fence_id,version FROM rx_manual_fence WHERE scope=?', (self.scope,)):
            peer(row['peer_id']).update(receiver_fence_id=row['fence_id'], receiver_fence_version=row['version'])
        for row in db.execute('SELECT peer_id,min_version FROM rx_control WHERE scope=?', (self.scope,)):
            peer(row['peer_id'])['min_control_version'] = row['min_version']
        unresolved = []
        for row in db.execute('SELECT command_id,command_json,status FROM rx_command WHERE scope=?', (self.scope,)):
            command = json.loads(row['command_json'])
            peer(command['peer_id'])
            if row['status'] in ('unknown', 'pending', 'held'):
                unresolved.append({'command_id': row['command_id'], 'peer_id': command['peer_id'],
                                   'account_epoch': command.get('account_epoch', 0), 'status': row['status']})
        highwater = highwater() if callable(highwater) else highwater
        if type(highwater) is not int or highwater < 0:
            raise ProtocolError('SESSION_RESET_SOURCE_HIGHWATER_INVALID')
        if any(item['source_watermark'] > highwater for item in peers.values()):
            raise ProtocolError('SESSION_RESET_SOURCE_HIGHWATER_BEHIND')
        return {'account_epoch': request['account_epoch'], 'snapshot_at': self.clock(),
                'published_source_highwater': highwater, 'peers': peers, 'unresolved_history': unresolved,
                'old_native_tasks_running': int(running),
                'pending_inbound_count': sum(row['source_seq'] is None for row in inbound),
                'pending_echo_count': db.execute("SELECT COUNT(*) FROM rx_deferred WHERE scope=? AND state!='published'",
                                                 (self.scope,)).fetchone()[0]}

    def session_reset(self, payload, *, native_quiescent=None, published_source_highwater=0):
        """Install the account barrier before waiting; never invent native outcomes."""
        validate_session_reset(payload, self.binding)
        request = {**{key: payload[key] for key in (*RESET_FIELDS, 'request_hash')}, 'phase': 'request'}
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM rx_reset WHERE scope=? AND reset_id=?',
                             (self.scope, payload['reset_id'])).fetchone()
            if old and old['request_hash'] != payload['request_hash']:
                return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_ID_CONFLICT')
            if old is None:
                if payload['phase'] != 'request':
                    return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_REQUEST_NOT_FOUND')
                latest = self._latest_reset(db)
                current = latest['account_epoch'] if latest else 0
                if latest and latest['state'] != 'complete':
                    return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_IN_PROGRESS')
                if current != payload['expected_account_epoch']:
                    return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_EPOCH_CONFLICT')
                db.execute('INSERT INTO rx_reset VALUES(?,?,?,?,?,?,?,?,?)',
                           (self.scope, request['reset_id'], request['request_hash'], canonical(request).decode(),
                            request['expected_account_epoch'], request['account_epoch'], 'waiting_native', None, self.clock()))
                old = db.execute('SELECT * FROM rx_reset WHERE scope=? AND reset_id=?',
                                 (self.scope, request['reset_id'])).fetchone()
            snapshot = json.loads(old['snapshot_json']) if old['snapshot_json'] else None
            if payload['phase'] == 'applied':
                if old['state'] not in ('barrier_ready', 'complete') or snapshot is None:
                    return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_BARRIER_NOT_READY')
                if payload['snapshot_hash'] != digest(snapshot):
                    return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_SNAPSHOT_CONFLICT')
                db.execute("UPDATE rx_reset SET state='complete',updated=? WHERE scope=? AND reset_id=?",
                           (self.clock(), self.scope, request['reset_id']))
                return self._reset_ack(request, 'complete', snapshot, 'SESSION_RESET_COMPLETE')
            if old['state'] in ('barrier_ready', 'complete'):
                return self._reset_ack(request, old['state'], snapshot, 'SESSION_RESET_' + old['state'].upper())
        if not callable(native_quiescent):
            return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_NATIVE_QUIESCENCE_UNAVAILABLE')
        try:
            # A timed-out native future can still run after execute() released
            # this lock. Both the OS lock and the actual future must be quiet.
            with account_lock(self.path, self.binding, blocking=False):
                try:
                    quiet = native_quiescent()
                except Exception:
                    return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_NATIVE_QUIESCENCE_UNAVAILABLE')
                if type(quiet) is not bool:
                    return self._reset_ack(request, 'blocked', None, 'SESSION_RESET_NATIVE_QUIESCENCE_UNAVAILABLE')
                with self._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    old = db.execute('SELECT * FROM rx_reset WHERE scope=? AND reset_id=?',
                                     (self.scope, request['reset_id'])).fetchone()
                    if old['state'] in ('barrier_ready', 'complete'):
                        return self._reset_ack(request, old['state'], json.loads(old['snapshot_json']),
                                               'SESSION_RESET_' + old['state'].upper())
                    snapshot = self._reset_snapshot(db, request, published_source_highwater, not quiet)
                    phase = ('barrier_ready' if quiet and not snapshot['pending_inbound_count']
                             and not snapshot['pending_echo_count'] else 'waiting_native')
                    db.execute('UPDATE rx_reset SET state=?,snapshot_json=?,updated=? WHERE scope=? AND reset_id=?',
                               (phase, canonical(snapshot).decode() if phase == 'barrier_ready' else None,
                                self.clock(), self.scope, request['reset_id']))
                    return self._reset_ack(request, phase, snapshot, 'SESSION_RESET_' + phase.upper())
        except BlockingIOError:
            with self._db() as db:
                db.execute('BEGIN')
                snapshot = self._reset_snapshot(db, request, published_source_highwater, True)
            return self._reset_ack(request, 'waiting_native', snapshot, 'SESSION_RESET_WAITING_NATIVE')

    def _fingerprint(self, command):
        validate_command(command, self.binding)
        # Arrival route and volatile sender state do not change execution identity.
        return digest({k: v for k, v in command.items() if k not in ('transport', 'status', 'created_at')})

    @staticmethod
    def _check(row, fingerprint):
        if row and row['fingerprint'] != fingerprint:
            raise ProtocolError('同一命令ID与原正文或身份冲突')

    def _result(self,db,command,status,evidence):
        value=result(command,'pending' if status=='held' else status,evidence)
        native=db.execute('SELECT native_id FROM rx_native WHERE scope=? AND command_id=? AND peer_id=?',(self.scope,command['command_id'],command['peer_id'])).fetchone()
        if native:value['native_id']=native['native_id']
        return value

    def _record(self, db, command, fingerprint, status, evidence):
        cid = command['command_id']
        db.execute('''INSERT INTO rx_command VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,command_id)
                    DO UPDATE SET status=excluded.status,evidence=excluded.evidence,updated=excluded.updated''',
                   (self.scope, cid, fingerprint, canonical(command).decode(), status, evidence, self.clock()))
        db.execute('INSERT INTO rx_audit(scope,command_id,status,evidence,created) VALUES(?,?,?,?,?)', (self.scope, cid, status, evidence, self.clock()))
        return self._result(db,command,status,evidence)

    def _native_mapping(self, db, command, outcome):
        native_id = outcome.get('native_id') if isinstance(outcome, dict) else None
        if outcome.get('status') != 'sent' or native_id is None:
            return
        if not isinstance(native_id,str) or not native_id or len(native_id)>512 or '\x00' in native_id:
            raise ProtocolError('原生出站ID无效')
        old=db.execute('SELECT command_id,peer_id FROM rx_native WHERE scope=? AND native_id=?',(self.scope,native_id)).fetchone()
        if old and (old['command_id']!=command['command_id'] or old['peer_id']!=command['peer_id']):
            raise ProtocolError('同一原生出站ID关联了不同命令')
        db.execute('INSERT OR IGNORE INTO rx_native VALUES(?,?,?,?)',(self.scope,native_id,command['peer_id'],command['command_id']))

    def command_for_native(self,native_id:str,peer_id:str):
        """Exact native ID + peer mapping, scoped by this fixed account; never text/time."""
        with self._db() as db:
            row=db.execute('SELECT n.command_id,c.command_json FROM rx_native n JOIN rx_command c ON n.scope=c.scope AND n.command_id=c.command_id WHERE n.scope=? AND n.native_id=? AND n.peer_id=?',(self.scope,native_id,peer_id)).fetchone()
        return row['command_id'] if row and json.loads(row['command_json']).get('owner_user_id')==self.binding.owner_user_id else None

    def source_done(self, source: str, fingerprint: str):
        with self._db() as db:
            return db.execute('SELECT 1 FROM rx_source WHERE scope=? AND source=? AND fingerprint=?',(self.scope,source,fingerprint)).fetchone() is not None

    def mark_source(self, source: str, fingerprint: str, *, reason='receipt-queued'):
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO rx_source VALUES(?,?,?,?,?)',(self.scope,source,fingerprint,reason,self.clock()))

    def _peer_commands(self, db, peer):
        latest = self._latest_reset(db)
        epoch = latest['account_epoch'] if latest else 0
        return [(row,json.loads(row['command_json'])) for row in db.execute('SELECT * FROM rx_command WHERE scope=?',(self.scope,)) if json.loads(row['command_json'])['peer_id']==peer and json.loads(row['command_json']).get('account_epoch',0)==epoch]

    def _manual_barrier(self, db, peer, event_id):
        old=db.execute('SELECT 1 FROM rx_event_fence WHERE scope=? AND event_id=?',(self.scope,event_id)).fetchone()
        if old:return
        current=db.execute('SELECT version FROM rx_manual_fence WHERE scope=? AND peer_id=?',(self.scope,peer)).fetchone()
        fence_version=current['version']+1 if current else 1
        fence_id='manual-'+digest([self.scope,peer,event_id])
        db.execute('INSERT INTO rx_event_fence VALUES(?,?,?,?)',(self.scope,event_id,fence_id,fence_version))
        db.execute('INSERT INTO rx_manual_fence VALUES(?,?,?,?) ON CONFLICT(scope,peer_id) DO UPDATE SET fence_id=excluded.fence_id,version=excluded.version',(self.scope,peer,fence_id,fence_version))
        version=max((command['control_version'] for _,command in self._peer_commands(db,peer)),default=0)+1
        db.execute('INSERT INTO rx_control VALUES(?,?,?) ON CONFLICT(scope,peer_id) DO UPDATE SET min_version=MAX(min_version,excluded.min_version)',(self.scope,peer,version))

    def event_control(self,event_id):
        with self._db() as db:
            row=db.execute('SELECT fence_id,version FROM rx_event_fence WHERE scope=? AND event_id=?',(self.scope,event_id)).fetchone()
        return {'manual_fence_id':row['fence_id'],'manual_fence_version':row['version']} if row else {}

    def stage_out(self, data, wait_seconds):
        """Freeze a native out classification; pending rows hold subsequent auto sends."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM rx_deferred WHERE scope=? AND event_id=?',(self.scope,data['event_id'])).fetchone()
            if old:
                original=json.loads(old['data']); supplied=dict(data)
                original.pop('source_command_id',None);supplied.pop('source_command_id',None)
                if original!=supplied:raise ProtocolError('同一原生事件事实冲突')
                return old['state']
            human=data.get('role')=='human'
            active=any(row['status']=='pending' for row,_ in self._peer_commands(db,data['peer_id']))
            state='waiting' if active and not human and not data.get('source_command_id') else 'ready'
            if human:data.pop('source_command_id',None)
            if state=='ready' and not data.get('source_command_id'):
                self._manual_barrier(db,data['peer_id'],data['event_id'])
            db.execute('INSERT INTO rx_deferred VALUES(?,?,?,?,?,?,?)',(self.scope,data['event_id'],data['peer_id'],canonical(data).decode(),self.instance_id,self.clock()+wait_seconds,state))
            return state

    def deferred_ready(self):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM rx_deferred WHERE scope=? AND state!='published' ORDER BY rowid",(self.scope,)).fetchall()
            ready=[]
            for row in rows:
                data=json.loads(row['data'])
                if row['state']=='waiting':
                    native=data.get('ext_msg_id') or data.get('source_message_id')
                    mapped=db.execute('SELECT n.command_id,c.command_json FROM rx_native n JOIN rx_command c ON n.scope=c.scope AND n.command_id=c.command_id WHERE n.scope=? AND n.peer_id=? AND n.native_id=?',(self.scope,row['peer_id'],native)).fetchone()
                    if mapped and json.loads(mapped['command_json']).get('owner_user_id')!=self.binding.owner_user_id:mapped=None
                    expired=row['instance']!=self.instance_id or self.clock()>=row['deadline']
                    active=any(r['status']=='pending' for r,_ in self._peer_commands(db,row['peer_id']))
                    if mapped:data['source_command_id']=mapped['command_id']
                    elif active and not expired:continue
                    else:self._manual_barrier(db,row['peer_id'],row['event_id'])
                    db.execute("UPDATE rx_deferred SET data=?,state='ready' WHERE scope=? AND event_id=?",(canonical(data).decode(),self.scope,row['event_id']))
                ready.append(data)
            return ready

    def deferred_published(self, event_id):
        with self._db() as db:
            db.execute("UPDATE rx_deferred SET state='published' WHERE scope=? AND event_id=?",(self.scope,event_id))

    def _hold(self, db, command, fingerprint):
        row=db.execute('SELECT * FROM rx_command WHERE scope=? AND command_id=?',(self.scope,command['command_id'])).fetchone()
        self._check(row,fingerprint)
        if row and row['status']!='held':return None
        blocked=self._unexecuted_failure(db,command,fingerprint)
        if blocked:return blocked
        waiting=db.execute("SELECT 1 FROM rx_deferred WHERE scope=? AND peer_id=? AND state='waiting'",(self.scope,command['peer_id'])).fetchone()
        if waiting and command.get('kind')!='manual_send':
            self._record(db,command,fingerprint,'held','native-echo-classification-held')
            return result(command,'pending','native-echo-classification-held')
        return None

    def begin_inbound(self,event_id,peer_id):
        # Record native receipt BEFORE publication/network I/O; even an event
        # whose source journal write fails must block an obsolete auto reply.
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO rx_inbound_event(scope,event_id,peer_id,source_seq) VALUES(?,?,?,NULL)',(self.scope,event_id,peer_id))
            row=db.execute('SELECT peer_id FROM rx_inbound_event WHERE scope=? AND event_id=?',(self.scope,event_id)).fetchone()
            if row['peer_id']!=peer_id:raise ProtocolError('Native event cannot change counterparty')
    def finish_inbound_journal(self,event_id,peer_id,sequence):
        if type(sequence) is not int or sequence<1:raise ProtocolError('Invalid native source sequence')
        with self._db() as db:
            row=db.execute('SELECT * FROM rx_inbound_event WHERE scope=? AND event_id=?',(self.scope,event_id)).fetchone()
            if not row or row['peer_id']!=peer_id:raise ProtocolError('Native intake not registered')
            if row['source_seq'] is not None and row['source_seq']!=sequence:raise ProtocolError('Native source sequence changed')
            db.execute('UPDATE rx_inbound_event SET source_seq=? WHERE scope=? AND event_id=?',(sequence,self.scope,event_id))
    def _watermark_failure(self,db,command,fingerprint):
        if command.get('business_version')!='2.2' or command.get('kind')=='manual_send':return None
        expected=command.get('source_watermark')
        if type(expected) is not int or expected<0:raise ProtocolError('V2.2 command missing native-source watermark')
        pending=db.execute('SELECT 1 FROM rx_inbound_event WHERE scope=? AND peer_id=? AND source_seq IS NULL LIMIT 1',(self.scope,command['peer_id'])).fetchone()
        newest=db.execute('SELECT MAX(source_seq) AS n FROM rx_inbound_event WHERE scope=? AND peer_id=?',(self.scope,command['peer_id'])).fetchone()['n']
        if pending or newest is not None and newest>expected:
            return self._record(db,command,fingerprint,'cancelled','newer-native-message-awaiting-analysis')
        return None

    def permission(self, command):
        """Read-only guard for Bus/UI immediately before the native send action.

        execute() is waiting with the account file lock held, so this callback
        MUST NOT acquire that lock. No transaction spans the native action.
        A later race cannot be claimed atomically cancelled across OSS/IM.
        """
        fingerprint=self._fingerprint(command)
        with self._db() as db:
            db.execute('BEGIN')
            if not self._epoch_permitted(db, command):return False
            row=db.execute('SELECT * FROM rx_command WHERE scope=? AND command_id=?',(self.scope,command['command_id'])).fetchone()
            if not row or row['fingerprint']!=fingerprint or row['status']!='pending':return False
            if command['expires_at']<=self.clock():return False
            if db.execute('SELECT 1 FROM rx_tombstone WHERE scope=? AND command_id=?',(self.scope,command['command_id'])).fetchone():return False
            control=db.execute('SELECT min_version FROM rx_control WHERE scope=? AND peer_id=?',(self.scope,command['peer_id'])).fetchone()
            if control and command['control_version']<control['min_version']:return False
            fence=db.execute('SELECT fence_id,version FROM rx_manual_fence WHERE scope=? AND peer_id=?',(self.scope,command['peer_id'])).fetchone()
            if fence and (command.get('receiver_fence_id')!=fence['fence_id'] or command.get('receiver_fence_version')!=fence['version']):return False
            if command.get('kind')!='manual_send' and db.execute("SELECT 1 FROM rx_deferred WHERE scope=? AND peer_id=? AND state='waiting' LIMIT 1",(self.scope,command['peer_id'])).fetchone():return False
            if command.get('business_version')=='2.2' and command.get('kind')!='manual_send':
                pending=db.execute('SELECT 1 FROM rx_inbound_event WHERE scope=? AND peer_id=? AND source_seq IS NULL LIMIT 1',(self.scope,command['peer_id'])).fetchone()
                newest=db.execute('SELECT MAX(source_seq) AS n FROM rx_inbound_event WHERE scope=? AND peer_id=?',(self.scope,command['peer_id'])).fetchone()['n']
                expected=command.get('source_watermark')
                if type(expected) is not int or pending or newest is not None and newest>expected:return False
            return True
    def _unexecuted_failure(self,db,command,fingerprint):
        """Only call for absent/held records: a native claim cannot be disproved by expiry."""
        tomb=db.execute('SELECT * FROM rx_tombstone WHERE scope=? AND command_id=?',(self.scope,command['command_id'])).fetchone()
        self._check(tomb,fingerprint)
        control=db.execute('SELECT min_version FROM rx_control WHERE scope=? AND peer_id=?',(self.scope,command['peer_id'])).fetchone()
        fence=db.execute('SELECT fence_id,version FROM rx_manual_fence WHERE scope=? AND peer_id=?',(self.scope,command['peer_id'])).fetchone()
        evidence=None
        if not self._epoch_permitted(db, command):evidence='account-epoch-not-active'
        elif tomb:evidence='durable-cancellation-tombstone'
        elif control and command['control_version']<control['min_version']:evidence='stale-control-version'
        elif fence and (command.get('receiver_fence_id')!=fence['fence_id'] or command.get('receiver_fence_version')!=fence['version']):evidence='manual-fence-not-observed'
        if evidence:return self._record(db,command,fingerprint,'cancelled',evidence)
        watermark=self._watermark_failure(db,command,fingerprint)
        if watermark:return watermark
        if command['expires_at']<=self.clock():return self._record(db,command,fingerprint,'failed','command-expired-before-execution')
        return None

    def execute(self, command: dict, native_send):
        fingerprint = self._fingerprint(command)
        # This preflight must not wait behind an in-flight native callback.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            held=self._hold(db,command,fingerprint)
            if held:return held
        with self._account_lock():
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM rx_command WHERE scope=? AND command_id=?', (self.scope, command['command_id'])).fetchone()
                tomb = db.execute('SELECT * FROM rx_tombstone WHERE scope=? AND command_id=?', (self.scope, command['command_id'])).fetchone()
                self._check(row, fingerprint); self._check(tomb, fingerprint)
                if row and row['status']!='held':
                    if row['status'] in ('sent', 'failed', 'cancelled', 'unknown'):
                        return self._result(db,command,row['status'],row['evidence'])
                    return self._record(db, command, fingerprint, 'unknown', 'durable-pending-requires-reconciliation')
                if not self._epoch_permitted(db, command):
                    return self._record(db, command, fingerprint, 'cancelled', 'account-epoch-not-active')
                control = db.execute('SELECT min_version FROM rx_control WHERE scope=? AND peer_id=?', (self.scope,command['peer_id'])).fetchone()
                if control and command['control_version'] < control['min_version']:
                    return self._record(db, command, fingerprint, 'cancelled', 'stale-control-version')
                if tomb:
                    return self._record(db, command, fingerprint, 'cancelled', 'durable-cancellation-tombstone')
                if command['expires_at'] <= self.clock():
                    return self._record(db, command, fingerprint, 'failed', 'command-expired-before-execution')
                held=self._hold(db,command,fingerprint)
                if held:return held
                watermark=self._watermark_failure(db,command,fingerprint)
                if watermark:return watermark
                self._record(db, command, fingerprint, 'pending', 'native-execution-claimed')
            try:
                outcome = native_send(dict(command))
                status = outcome.get('status') if isinstance(outcome, dict) else None
                evidence = outcome.get('evidence') if isinstance(outcome, dict) else None
                if status not in ('sent', 'failed', 'cancelled', 'unknown') or not isinstance(evidence, str) or not evidence:
                    status, evidence = 'unknown', 'native-result-without-execution-evidence'
            except Exception:
                outcome = {}
                status, evidence = 'unknown', 'native-call-outcome-unknown'
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                if status=='sent' and isinstance(outcome,dict):self._native_mapping(db,command,outcome)
                return self._record(db, command, fingerprint, status, evidence)

    def reconcile(self, command: dict, native_reconcile):
        fingerprint = self._fingerprint(command)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM rx_command WHERE scope=? AND command_id=?',(self.scope,command['command_id'])).fetchone()
            self._check(row,fingerprint)
            if row and row['status']=='held':
                blocked=self._unexecuted_failure(db,command,fingerprint)
                return blocked or result(command,'pending','native-echo-classification-held')
        with self._account_lock():
            with self._db() as db:
                row = db.execute('SELECT * FROM rx_command WHERE scope=? AND command_id=?', (self.scope, command['command_id'])).fetchone()
                self._check(row, fingerprint)
                if row and row['status'] in ('sent', 'failed', 'cancelled'):
                    return self._result(db,command,row['status'],row['evidence'])
            try:
                outcome = native_reconcile(dict(command))
            except Exception:
                outcome = None
            status, evidence = 'unknown', 'no-conclusive-native-evidence'
            if isinstance(outcome, dict) and outcome.get('status') in ('sent', 'failed', 'cancelled') and isinstance(outcome.get('evidence'), str) and outcome['evidence']:
                status, evidence = outcome['status'], outcome['evidence']
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                if status=='sent' and isinstance(outcome,dict):self._native_mapping(db,command,outcome)
                return self._record(db, command, fingerprint, status, evidence)

    def cancel(self, command: dict):
        fingerprint = self._fingerprint(command)
        try:
            with account_lock(self.path,self.binding,blocking=False):
                return self._cancel_record(command,fingerprint)
        except BlockingIOError:
            # The already-claimed native action cannot be interrupted. Persist the
            # tombstone now; return unknown, never an invented successful cancel.
            return self._cancel_record(command,fingerprint)

    def _cancel_record(self, command, fingerprint):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM rx_command WHERE scope=? AND command_id=?', (self.scope, command['command_id'])).fetchone()
            tomb = db.execute('SELECT * FROM rx_tombstone WHERE scope=? AND command_id=?', (self.scope, command['command_id'])).fetchone()
            self._check(row, fingerprint); self._check(tomb, fingerprint)
            db.execute('INSERT OR IGNORE INTO rx_tombstone VALUES(?,?,?,?)', (self.scope, command['command_id'], fingerprint, self.clock()))
            if self._epoch_permitted(db, command):
                db.execute('INSERT INTO rx_control VALUES(?,?,?) ON CONFLICT(scope,peer_id) DO UPDATE SET min_version=MAX(min_version,excluded.min_version)', (self.scope, command['peer_id'], command['control_version']+1))
            if row and row['status'] in ('sent', 'failed', 'cancelled'):
                return self._result(db,command,row['status'],row['evidence'])
            if row and row['status']!='held':
                return self._record(db, command, fingerprint, 'unknown', 'cancellation-awaits-native-reconciliation')
            return self._record(db, command, fingerprint, 'cancelled', 'durable-cancellation-tombstone')

    def receive(self, blob: bytes, codec: Codec, native_send, native_reconcile):
        data = codec.unpack(blob)
        command = data['payload']
        if data['kind'] == 'command':
            return self.execute(command, native_send)
        if data['kind'] == 'cancel':
            return self.cancel(command)
        if data['kind'] == 'query':
            return self.reconcile(command, native_reconcile)
        raise ProtocolError('该入口只接受执行或控制信封')
