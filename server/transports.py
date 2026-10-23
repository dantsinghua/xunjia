"""Authenticated, durable file-gateway transport core. No implicit failover.

The file gateway is implemented by FileGatewayTransport. Only ack() after the caller commits advances
transport cursors. A submission is never evidence that the native IM sent it.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .receiver_ledger import account_lock
from relay.protocol import (Binding, Codec, ProtocolError, canonical, digest, envelope,
                            result, token, validate_command, validate_session_reset)


class TransportUnavailable(RuntimeError):
    pass


class _Store:
    def __init__(self, path, binding):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        identity=[binding.owner_user_id,binding.workbench_id,binding.account_uid]
        if binding.channel != 'qidian':identity.append(binding.channel)
        self.scope = 'xunjia:'+digest(identity)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS tx_packet(scope TEXT,id TEXT,fingerprint TEXT,blob BLOB,expires REAL,replay INTEGER DEFAULT 0,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS tx_publication(scope TEXT,id TEXT,route TEXT,data TEXT,uploaded INTEGER DEFAULT 0,PRIMARY KEY(scope,id,route));
            CREATE TABLE IF NOT EXISTS tx_outbox(scope TEXT,id TEXT,op TEXT,route TEXT,fingerprint TEXT,status TEXT,evidence TEXT,created REAL,PRIMARY KEY(scope,id,op));
            CREATE TABLE IF NOT EXISTS tx_message(scope TEXT,kind TEXT,id TEXT,fingerprint TEXT,data TEXT,acked INTEGER DEFAULT 0,PRIMARY KEY(scope,kind,id));
            CREATE TABLE IF NOT EXISTS tx_source(token TEXT PRIMARY KEY,scope TEXT,stream TEXT,epoch TEXT,position INTEGER,kind TEXT,id TEXT,acked INTEGER,reason TEXT,UNIQUE(scope,stream,epoch,position));
            CREATE TABLE IF NOT EXISTS tx_cursor(scope TEXT,stream TEXT,epoch TEXT,position INTEGER,PRIMARY KEY(scope,stream,epoch));
            CREATE TABLE IF NOT EXISTS tx_event_route(scope TEXT,id TEXT,route TEXT,uploaded INTEGER DEFAULT 0,PRIMARY KEY(scope,id,route));
            CREATE TABLE IF NOT EXISTS tx_event(scope TEXT,id TEXT,seq INTEGER,original_hash TEXT,data TEXT,uploaded INTEGER DEFAULT 0,replay INTEGER DEFAULT 0,PRIMARY KEY(scope,id),UNIQUE(scope,seq));
            CREATE TABLE IF NOT EXISTS tx_event_control(scope TEXT,id TEXT,data TEXT,PRIMARY KEY(scope,id));
            ''')

    @contextmanager
    def db(self):
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

    def packet(self, identity, data, codec):
        fingerprint = digest({k:v for k,v in data.items() if k not in ('created_at','expires_at','transport_replay')})
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tx_packet WHERE scope=? AND id=?',(self.scope,identity)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ProtocolError('同一信封ID内容冲突')
                blob=bytes(row['blob'])
                data=dict(data)
                expired=row['expires']<=codec.clock()
                if data['kind']=='event' and (expired or row['replay']):data['transport_replay']=True
                replay_upgrade=data['kind']=='event' and data.get('transport_replay') is True and not row['replay']
                if replay_upgrade or expired and data['kind'] in ('query','cancel','receipt','catalog','contacts','manifest','event'):
                    blob=codec.pack(data)
                    db.execute('UPDATE tx_packet SET blob=?,expires=?,replay=? WHERE scope=? AND id=?',(blob,data['expires_at'],int(data.get('transport_replay') is True),self.scope,identity))
                else:codec.unpack(blob)
                return blob
            blob = codec.pack(data)
            db.execute('INSERT INTO tx_packet VALUES(?,?,?,?,?,?)',(self.scope,identity,fingerprint,blob,data['expires_at'],int(data.get('transport_replay') is True)))
            return blob

    def claim(self, command, op, route, now):
        fp=digest({k:v for k,v in command.items() if k not in ('status','created_at')})
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM tx_outbox WHERE scope=? AND id=? AND op=?',(self.scope,command['command_id'],op)).fetchone()
            if row:
                if row['fingerprint']!=fp or row['route']!=route:
                    raise ProtocolError('已有命令不能换正文或静默换路')
                return result(command,row['status'],row['evidence'])
            db.execute('INSERT INTO tx_outbox VALUES(?,?,?,?,?,?,?,?)',(self.scope,command['command_id'],op,route,fp,'pending','submission-claimed',now))
        return None

    def outcome(self, command, op, status, evidence):
        with self.db() as db:
            db.execute('UPDATE tx_outbox SET status=?,evidence=? WHERE scope=? AND id=? AND op=?',(status,evidence,self.scope,command['command_id'],op))
        return result(command,status,evidence)

    def message(self, data, db=None):
        if db is None:
            with self.db() as connection:
                return self.message(data,connection)
        kind, identity = data['kind'], data['message_id']
        fp=digest({k:v for k,v in data.items() if k not in ('created_at','expires_at','transport_replay')})
        old=db.execute('SELECT fingerprint,acked,data FROM tx_message WHERE scope=? AND kind=? AND id=?',(self.scope,kind,identity)).fetchone()
        if old and old['fingerprint']!=fp:
            raise ProtocolError('同一消息身份内容冲突')
        db.execute('INSERT OR IGNORE INTO tx_message(scope,kind,id,fingerprint,data) VALUES(?,?,?,?,?)',(self.scope,kind,identity,fp,canonical(data).decode()))
        if old:
            previous=json.loads(old['data'])
            if previous.get('transport_replay') is True:data={**data,'transport_replay':True}
            replay_upgrade=data.get('transport_replay') is True and previous.get('transport_replay') is not True
            if replay_upgrade or data['created_at']>=previous['created_at'] and data['expires_at']>previous['expires_at']:
                db.execute('UPDATE tx_message SET data=? WHERE scope=? AND kind=? AND id=?',(canonical(data).decode(),self.scope,kind,identity))
        return bool(old and old['acked'])

    def cursor(self, stream, epoch):
        with self.db() as db:
            row=db.execute('SELECT position FROM tx_cursor WHERE scope=? AND stream=? AND epoch=?',(self.scope,stream,epoch)).fetchone()
            return row['position'] if row else 0

    def _advance(self, db, stream, epoch):
        rows=db.execute('SELECT position,acked FROM tx_source WHERE scope=? AND stream=? AND epoch=? ORDER BY position',(self.scope,stream,epoch)).fetchall()
        position=0
        for row in rows:
            if not row['acked']:break
            position=row['position']
        db.execute('INSERT INTO tx_cursor VALUES(?,?,?,?) ON CONFLICT(scope,stream,epoch) DO UPDATE SET position=MAX(position,excluded.position)',(self.scope,stream,epoch,position))

    def stage(self, stream, epoch, position, data=None, reason=''):
        source='src_'+hashlib.sha256(f'{self.scope}|{stream}|{epoch}|{position}'.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT kind,id FROM tx_source WHERE token=?',(source,)).fetchone()
            if existing and data and (existing['kind']!=data['kind'] or existing['id']!=data['message_id']):raise ProtocolError('同一源位置事件身份冲突')
            acked=self.message(data,db) if data else True
            db.execute('INSERT OR IGNORE INTO tx_source VALUES(?,?,?,?,?,?,?,?,?)',(source,self.scope,stream,epoch,position,data['kind'] if data else 'quarantine',data['message_id'] if data else '',int(acked),reason))
            self._advance(db,stream,epoch)
        return source

    def pending(self, stream, *, limit=None, prioritize_control=False):
        order = "s.position"
        if prioritize_control:
            # Controls must be selected before the batch bound: an uncommitted
            # image can depend on a manifest received behind a full event batch.
            order = "(m.kind='event'), CASE WHEN m.kind='event' THEN json_extract(m.data,'$.payload.source_seq') END, s.position"
        query = f'''SELECT s.token,m.kind,m.id,m.data FROM tx_source s JOIN tx_message m
            ON s.scope=m.scope AND s.kind=m.kind AND s.id=m.id
            WHERE s.scope=? AND s.stream=? AND m.acked=0
            GROUP BY m.kind,m.id ORDER BY {order}'''
        parameters = [self.scope, stream]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.db() as db:
            rows=db.execute(query, parameters).fetchall()
        seen=set();out=[]
        for row in rows:
            key=(row['kind'],row['id'])
            if key in seen:continue
            seen.add(key);out.append({'source_token':row['token'],'kind':row['kind'],'envelope':json.loads(row['data'])})
        return out

    def ack(self, source):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM tx_source WHERE token=? AND scope=?',(source,self.scope)).fetchone()
            if not row:raise ProtocolError('游标确认不属于当前绑定')
            db.execute('UPDATE tx_message SET acked=1 WHERE scope=? AND kind=? AND id=?',(self.scope,row['kind'],row['id']))
            db.execute('UPDATE tx_source SET acked=1 WHERE scope=? AND kind=? AND id=?',(self.scope,row['kind'],row['id']))
            streams=db.execute('SELECT DISTINCT stream,epoch FROM tx_source WHERE scope=?',(self.scope,)).fetchall()
            for item in streams:self._advance(db,item['stream'],item['epoch'])

    def receipt(self, command):
        with self.db() as db:
            rows=db.execute("SELECT data FROM tx_message WHERE scope=? AND kind='receipt'",(self.scope,)).fetchall()
        found=[]
        for row in rows:
            env=json.loads(row['data'])
            if env['command_id']!=command['command_id']:continue
            body=env['payload']
            if env.get('control_version')!=command['control_version']:raise ProtocolError('回执控制版本不符')
            for key in ('command_id','account_uid','peer_id','content_hash'):
                if env.get(key)!=command[key] or body.get(key)!=command[key]:
                    raise ProtocolError('回执与原命令身份或摘要不符')
            value=result(command,body.get('status'),body.get('evidence'))
            if body.get('native_id'):value['native_id']=body['native_id']
            found.append(value)
        terminal={r['status'] for r in found if r['status'] in ('sent','failed','cancelled')}
        if len(terminal)>1:return result(command,'unknown','conflicting-native-receipts')
        if terminal:return next(r for r in reversed(found) if r['status'] in terminal)
        return found[-1] if found else None

    def event(self, event, control=None):
        event={k:v for k,v in event.items() if k not in ('transport','source_token','mail_uid','uidvalidity','object_key','received_at','transport_replay')}
        source_seq=event.pop('source_seq',None)
        identity=token(event.get('event_id'));token(event.get('peer_id'));fp=digest(event)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM tx_event WHERE scope=? AND id=?',(self.scope,identity)).fetchone()
            if old:
                if old['original_hash']!=fp:raise ProtocolError('原生事件ID正文冲突')
                if control is not None:
                    saved=db.execute('SELECT data FROM tx_event_control WHERE scope=? AND id=?',(self.scope,identity)).fetchone()
                    if (json.loads(saved['data']) if saved else {})!=control:raise ProtocolError('原生事件控制屏障冲突')
                value=json.loads(old['data'])
                if source_seq is not None and source_seq!=value['source_seq']:raise ProtocolError('事件源序号冲突')
                return value
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM tx_event WHERE scope=?',(self.scope,)).fetchone()[0]
            if source_seq is not None and source_seq!=seq:raise ProtocolError('事件源序号不连续')
            value={**event,'source_seq':seq}
            db.execute('INSERT INTO tx_event(scope,id,seq,original_hash,data) VALUES(?,?,?,?,?)',(self.scope,identity,seq,fp,canonical(value).decode()))
            db.execute('INSERT INTO tx_event_control VALUES(?,?,?)',(self.scope,identity,canonical(control or {}).decode()))
            return value

    def event_control(self,identity):
        with self.db() as db:
            row=db.execute('SELECT data FROM tx_event_control WHERE scope=? AND id=?',(self.scope,identity)).fetchone()
        return json.loads(row['data']) if row else {}

    def source_highwater(self):
        with self.db() as db:
            return db.execute('SELECT COALESCE(MAX(seq),0) FROM tx_event WHERE scope=?',
                              (self.scope,)).fetchone()[0]


class _Transport:
    route=''
    def __init__(self,binding,path,*,clock=time.time,ack_timeout=90,envelope_ttl=604800):
        if ack_timeout<=0 or envelope_ttl<=0:raise TransportUnavailable('TRANSPORT_TIMEOUT_CONFIGURATION_INVALID')
        self.binding,self.clock,self.ack_timeout,self.envelope_ttl=binding,clock,ack_timeout,envelope_ttl
        self.codec=Codec(binding,clock=clock);self.store=_Store(path,binding)

    def _command(self,command,kind):
        validate_command(command,self.binding)
        if command['transport']!=self.route:raise ProtocolError('命令与适配器主通道不符')
        now=self.clock()
        identity=command['command_id'] if kind=='command' else kind+'-'+command['command_id']
        if kind=='query':identity+='-'+str(int(now//30))
        return envelope(self.binding,kind,command,message_id=identity,
                        command_id=command['command_id'],peer_id=command['peer_id'],content_hash=command['content_hash'],
                        control_version=command['control_version'],created_at=now,
                        expires_at=command['expires_at'] if kind=='command' else now+self.envelope_ttl)

    def _submit(self,command,kind):
        data=self._command(command,kind)
        operation=kind if kind=='command' else kind+':'+str(int(self.clock()//(30 if kind=='query' else self.envelope_ttl)))
        old=self.store.claim(command,operation,self.route,self.clock())
        if old:
            receipt=self.store.receipt(command)
            if receipt:return receipt
            return self._waiting(command) if kind=='command' and old['status']=='pending' else old
        if kind=='command' and command['expires_at']<=self.clock():
            return self.store.outcome(command,operation,'failed','expired-before-submission')
        try:
            blob=self.store.packet(data['message_id'],data,self.codec)
        except ProtocolError:
            return self.store.outcome(command,operation,'failed','protocol-preflight-rejected-before-network')
        try:
            self._publish(data,blob)
        except Exception:
            return self.store.outcome(command,operation,'unknown','transport-submission-outcome-unknown')
        self.store.outcome(command,operation,'pending','transport-accepted-awaiting-native-evidence')
        return result(command,'accepted','transport-accepted-awaiting-native-evidence')

    def send(self,command):return self._submit(command,'command')
    def cancel(self,command):
        self._submit(command,'cancel')
        return self.reconcile(command)
    def ack(self,source_token):self.store.ack(source_token)
    def _waiting(self,command):
        with self.store.db() as db:
            row=db.execute("SELECT * FROM tx_outbox WHERE scope=? AND id=? AND op='command'",(self.store.scope,command['command_id'])).fetchone()
        if not row:return result(command,'unknown','submission-record-unavailable')
        if row and (row['status']=='unknown' or self.clock()-row['created']>=self.ack_timeout):return result(command,'unknown','awaiting-native-reconciliation')
        return result(command,'pending','awaiting-native-receipt')
    def _incoming(self,blob):return self.codec.unpack(blob)
    def _pending(self,stream,limit):
        rows=self.store.pending(stream)[:limit]
        for row in rows:self.codec._validate(row['envelope'])
        return rows
    def publish_catalog(self,payload):return self._publish_document('catalog',payload)
    def publish_contacts(self,payload):return self._publish_document('contacts',payload)

    def _queue_reset_control(self, kind, payload):
        validate_session_reset(payload, self.binding, ack=kind == 'session_reset_ack')
        if payload['transport'] != self.route:
            raise ProtocolError('SESSION_RESET_TRANSPORT_MISMATCH')
        # Only these idempotent controls receive distinct retry envelopes.
        # Ordinary uncertain text uploads retain their existing protection.
        bucket = int(self.clock() // 30)
        snapshot = payload.get('snapshot_hash') if payload['phase'] in ('applied', 'barrier_ready', 'complete') else ''
        identity = kind + '-' + digest([payload['reset_id'], payload['request_hash'], payload['phase'],
                                       snapshot, payload.get('code', ''), bucket])[:48]
        with account_lock(self.store.path, self.binding):
            with self.store.db() as db:
                old = db.execute('SELECT data FROM tx_publication WHERE scope=? AND id=? AND route=?',
                                 (self.store.scope, identity, self.route)).fetchone()
            if old:
                return identity
            data = envelope(self.binding, kind, payload, message_id=identity,
                            created_at=self.clock(), expires_at=self.clock() + self.envelope_ttl)
            self._queue_publication(data)
        return identity

    def queue_session_reset(self, payload):
        return self._queue_reset_control('session_reset', payload)

    def queue_session_reset_ack(self, payload):
        return self._queue_reset_control('session_reset_ack', payload)
    def _publish_document(self,kind,payload):
        data=envelope(self.binding,kind,payload,message_id=kind+'-'+digest(payload)[:32],created_at=self.clock(),expires_at=self.clock()+self.envelope_ttl)
        self._publish_durable(data)
        return data['message_id']

    def queue_receipt(self,command,outcome):
        validate_command(command,self.binding)
        for key in ('command_id','account_uid','peer_id','content_hash'):
            if outcome.get(key)!=command[key]:raise ProtocolError('不能发布不匹配的回执')
        result(command,outcome.get('status'),outcome.get('evidence'))
        outcome={key:value for key,value in outcome.items() if key in ('command_id','account_uid','peer_id','content_hash','status','evidence','native_id','executed_at','reason','code')}
        data=envelope(self.binding,'receipt',outcome,message_id='receipt-'+command['command_id']+'-'+digest(outcome)[:16],
                      command_id=command['command_id'],peer_id=command['peer_id'],content_hash=command['content_hash'],
                      control_version=command['control_version'],created_at=self.clock(),expires_at=self.clock()+self.envelope_ttl)
        self._queue_publication(data)
        return data['message_id']

    def publish_receipt(self,command,outcome):
        identity=self.queue_receipt(command,outcome)
        self.flush_publications()
        return identity

    def _queue_publication(self,data):
        self.store.packet(data['message_id'],data,self.codec)
        with self.store.db() as db:
            db.execute('INSERT INTO tx_publication(scope,id,route,data) VALUES(?,?,?,?) ON CONFLICT(scope,id,route) DO UPDATE SET uploaded=0,data=excluded.data',(self.store.scope,data['message_id'],self.route,canonical(data).decode()))

    def _publish_durable(self,data):
        if data['kind'] not in ('catalog','contacts'):
            self._queue_publication(data)
            self.flush_publications()
            return
        # Full directory snapshots have independent identities. An older unknown
        # snapshot must remain auditable without suppressing a new observation.
        with account_lock(self.store.path,self.binding):
            with self.store.db() as db:
                old=db.execute('SELECT data,uploaded FROM tx_publication WHERE scope=? AND id=? AND route=?',
                               (self.store.scope,data['message_id'],self.route)).fetchone()
            if old:
                saved=json.loads(old['data'])
                data={**data,'created_at':saved['created_at'],'expires_at':saved['expires_at']}
            # Reusing a snapshot may neither renew its report time nor re-sign an
            # expired packet. A later observation needs its own snapshot payload.
            self.codec._validate(data)
            if not old:self._queue_publication(data)
            blob=self.store.packet(data['message_id'],data,self.codec)
            if old and old['uploaded']:return
            self._publish(data,blob)
            with self.store.db() as db:
                db.execute('UPDATE tx_publication SET uploaded=1 WHERE scope=? AND id=? AND route=?',
                           (self.store.scope,data['message_id'],self.route))

    def flush_publications(self,limit=50):
        if not 1<=limit<=200:raise ValueError('发布批量须为1至200')
        with account_lock(self.store.path,self.binding):
            # Handshake retries supersede only older envelopes for the same
            # logical reset. An uncertain text/receipt is never reissued here.
            with self.store.db() as db:
                controls=db.execute('''SELECT p.id,p.data FROM tx_publication p
                    WHERE p.scope=? AND p.route=? AND p.uploaded=0
                    AND json_extract(p.data,'$.kind') IN ('session_reset','session_reset_ack')
                    AND p.rowid=(SELECT MAX(q.rowid) FROM tx_publication q WHERE q.scope=p.scope AND q.route=p.route
                        AND json_extract(q.data,'$.kind')=json_extract(p.data,'$.kind')
                        AND json_extract(q.data,'$.payload.reset_id')=json_extract(p.data,'$.payload.reset_id'))
                    ORDER BY p.rowid LIMIT ?''',(self.store.scope,self.route,limit)).fetchall()
            control_error=None
            for row in controls:
                try:
                    data=json.loads(row['data'])
                    self._publish(data,self.store.packet(row['id'],data,self.codec))
                    with self.store.db() as db:db.execute('UPDATE tx_publication SET uploaded=1 WHERE scope=? AND id=? AND route=?',(self.store.scope,row['id'],self.route))
                except Exception as exc:
                    control_error=exc
            with self.store.db() as db:rows=db.execute("SELECT id,data FROM tx_publication WHERE scope=? AND route=? AND uploaded=0 AND json_extract(data,'$.kind') NOT IN ('session_reset','session_reset_ack') ORDER BY rowid LIMIT ?",(self.store.scope,self.route,limit-len(controls))).fetchall()
            for row in rows:
                data=json.loads(row['data'])
                if data['expires_at']<=self.clock():
                    data['created_at']=self.clock();data['expires_at']=self.clock()+self.envelope_ttl
                self._publish(data,self.store.packet(row['id'],data,self.codec))
                with self.store.db() as db:db.execute('UPDATE tx_publication SET uploaded=1 WHERE scope=? AND id=? AND route=?',(self.store.scope,row['id'],self.route))
            if control_error is not None:raise control_error
            return len(rows)+len(controls)

    def _event_envelope(self,value,control=None):
        data=envelope(self.binding,'event',value,message_id=value['event_id'],peer_id=token(value['peer_id']),created_at=self.clock(),expires_at=self.clock()+self.envelope_ttl)
        control=self.store.event_control(value['event_id']) if control is None else control
        if not isinstance(control,dict) or set(control)-{'manual_fence_id','manual_fence_version'}:raise ProtocolError('事件控制元数据无效')
        return {**data,**control}

    def queue_event(self,event,*,routes=None,control=None):
        # The caller may enqueue every authorized route before touching network.
        routes=tuple(routes) if routes is not None else (self.route,)
        if not routes or not set(routes)<={'oss','mail'}:raise ProtocolError('上行路由集合无效')
        self.codec._validate(self._event_envelope(event,control))
        with account_lock(self.store.path,self.binding):
            value=self.store.event(event,control)
            data=self._event_envelope(value)
            self.store.packet('event-'+value['event_id'],data,self.codec)
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                for route in routes:db.execute('INSERT INTO tx_event_route(scope,id,route) VALUES(?,?,?) ON CONFLICT(scope,id,route) DO UPDATE SET uploaded=0',(self.store.scope,value['event_id'],route))
            return value['source_seq']

    def publish_event(self,event):
        seq=self.queue_event(event)
        self.flush_events(limit=200)
        return seq

    def queue_replay(self,first_seq,last_seq,*,routes=None):
        if type(first_seq) is not int or type(last_seq) is not int or first_seq<1 or last_seq<first_seq or last_seq-first_seq+1>200:
            raise ValueError('管理员重放范围须为连续1至200条源记录')
        routes=tuple(routes) if routes is not None else (self.route,)
        if not routes or not set(routes)<={'oss','mail'}:raise ProtocolError('上行路由集合无效')
        with account_lock(self.store.path,self.binding), self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT id,seq FROM tx_event WHERE scope=? AND seq>=? AND seq<=? ORDER BY seq',(self.store.scope,first_seq,last_seq)).fetchall()
            if [row['seq'] for row in rows]!=list(range(first_seq,last_seq+1)):raise ProtocolError('源账本缺口不能凭空补造')
            for row in rows:
                db.execute('UPDATE tx_event SET replay=1 WHERE scope=? AND id=?',(self.store.scope,row['id']))
                for route in routes:db.execute('INSERT INTO tx_event_route(scope,id,route) VALUES(?,?,?) ON CONFLICT(scope,id,route) DO UPDATE SET uploaded=0',(self.store.scope,row['id'],route))
        return [row['seq'] for row in rows]

    def flush_events(self,limit=50):
        if not 1<=limit<=200:raise ValueError('发布批量须为1至200')
        with account_lock(self.store.path,self.binding):return self._flush_events_locked(limit)

    def _flush_events_locked(self,limit):
        with self.store.db() as db:rows=db.execute('SELECT e.id,e.data,e.replay FROM tx_event e JOIN tx_event_route r ON e.scope=r.scope AND e.id=r.id WHERE e.scope=? AND r.route=? AND r.uploaded=0 ORDER BY e.seq LIMIT ?',(self.store.scope,self.route,limit)).fetchall()
        for row in rows:
            value=json.loads(row['data']);data=self._event_envelope(value)
            if row['replay']:data['transport_replay']=True
            blob=self.store.packet('event-'+value['event_id'],data,self.codec)
            self._publish_event(data,blob)
            with self.store.db() as db:db.execute('UPDATE tx_event_route SET uploaded=1 WHERE scope=? AND id=? AND route=?',(self.store.scope,value['event_id'],self.route))
        self._after_events()
        return len(rows)

    def _after_events(self):pass

    def quarantined(self,limit=50):
        with self.store.db() as db:
            rows=db.execute("SELECT token,stream,epoch,position,reason FROM tx_source WHERE scope=? AND kind='quarantine' ORDER BY rowid DESC LIMIT ?",(self.store.scope,min(max(int(limit),1),200))).fetchall()
        return [dict(row) for row in rows]


class TransportRouter:
    """Use exact frozen command route. Missing routes never trigger fallback."""
    def __init__(self,adapters):self.adapters=adapters
    def _adapter(self,command):
        route=command.get('transport');adapter=self.adapters.get((command.get('owner_user_id'),command.get('account_uid'),route)) or self.adapters.get(route)
        if adapter is None:raise TransportUnavailable('NOT_CONFIGURED: TRANSPORT')
        binding=adapter.binding
        if any(command.get(k)!=getattr(binding,k) for k in ('owner_user_id','account_uid','workbench_id')):raise ProtocolError('传输绑定身份不符')
        return adapter
    def send(self,command):return self._adapter(command).send(command)
    def reconcile(self,command):return self._adapter(command).reconcile(command)
    def cancel(self,command):return self._adapter(command).cancel(command)
