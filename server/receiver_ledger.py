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

from relay.protocol import Binding, Codec, ProtocolError, canonical, digest, result, validate_command


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
        return [(row,json.loads(row['command_json'])) for row in db.execute('SELECT * FROM rx_command WHERE scope=?',(self.scope,)) if json.loads(row['command_json'])['peer_id']==peer]

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
        if tomb:evidence='durable-cancellation-tombstone'
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
