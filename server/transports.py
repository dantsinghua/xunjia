"""Authenticated, durable OSS/mail adapters. No implicit route failover.

Network factories are injectable; defaults use STS-backed OSSHTTP and TLS SMTP/
IMAP. poll() stages verified input; only ack() after the caller commits advances
transport cursors. A submission is never evidence that the native IM sent it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import time

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from imlink.oss_http import OSSHTTP
from .receiver_ledger import account_lock
from relay.protocol import Binding, Codec, ProtocolError, canonical, digest, envelope, result, token, validate_command


class TransportUnavailable(RuntimeError):
    pass


class _Store:
    def __init__(self, path, binding):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = 'xunjia:'+digest([binding.owner_user_id,binding.workbench_id,binding.account_uid])
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

    def pending(self, stream):
        with self.db() as db:
            rows=db.execute('''SELECT s.token,m.kind,m.id,m.data FROM tx_source s JOIN tx_message m
                ON s.scope=m.scope AND s.kind=m.kind AND s.id=m.id
                WHERE s.scope=? AND s.stream=? AND m.acked=0 ORDER BY s.position''',(self.scope,stream)).fetchall()
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
        self._queue_publication(data)
        self.flush_publications()

    def flush_publications(self,limit=50):
        if not 1<=limit<=200:raise ValueError('发布批量须为1至200')
        with account_lock(self.store.path,self.binding):
            with self.store.db() as db:rows=db.execute('SELECT id,data FROM tx_publication WHERE scope=? AND route=? AND uploaded=0 ORDER BY rowid LIMIT ?',(self.store.scope,self.route,limit)).fetchall()
            for row in rows:
                data=json.loads(row['data'])
                if data['expires_at']<=self.clock():
                    data['created_at']=self.clock();data['expires_at']=self.clock()+self.envelope_ttl
                self._publish(data,self.store.packet(row['id'],data,self.codec))
                with self.store.db() as db:db.execute('UPDATE tx_publication SET uploaded=1 WHERE scope=? AND id=? AND route=?',(self.store.scope,row['id'],self.route))
            return len(rows)

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


class OSSTransport(_Transport):
    route='oss'
    def __init__(self,binding:Binding,client:OSSHTTP,path,**kwargs):
        if client is None:raise TransportUnavailable('NOT_CONFIGURED: OSS')
        super().__init__(binding,path,**kwargs);self.client=client
        self.root=f'wb/{token(binding.workbench_id)}'
        self.out=f'{self.root}/out/xunjia/{token(binding.account_uid)}'

    def _get(self,name):
        status,body=self.client.request('GET',name)
        if status==404:return None
        if status!=200:raise TransportUnavailable('OSS_READ_UNAVAILABLE')
        return body
    def _put(self,name,blob):
        status,_=self.client.request('PUT',name,blob)
        if status not in (200,201,204):raise TransportUnavailable('OSS_WRITE_UNAVAILABLE')
    def _publish(self,data,blob):
        kind=data['kind'];cid=data['command_id']
        if kind=='catalog':name=f'{self.out}/catalog.bin'
        elif kind=='contacts':name=f'{self.out}/contacts.bin'
        elif kind=='receipt':name=f'{self.root}/ack/{cid}.bin'
        elif kind in ('command','cancel','query'):name=f'{self.root}/in/xunjia/{data["message_id"]}.bin'
        else:raise ProtocolError('不支持的OSS指令对象')
        self._put(name,blob)

    def reconcile(self,command):
        self._command(command,'query')
        raw=self._get(f'{self.root}/ack/{token(command["command_id"])}.bin')
        if raw:
            try:data=self._incoming(raw)
            except ProtocolError:
                self._submit(command,'query')
                raise
            if data['kind']!='receipt' or data['command_id']!=command['command_id']:raise ProtocolError('ACK对象身份不匹配')
            self.store.message(data)
        receipt=self.store.receipt(command)
        if receipt and receipt['status'] in ('sent','failed','cancelled'):return receipt
        self._submit(command,'query')
        return receipt or self._waiting(command)

    def _publish_event(self,data,blob):
        self._put(f'{self.out}/{data["payload"]["source_seq"]:020d}.bin',blob)

    def _after_events(self):
        with self.store.db() as db:
            rows=db.execute('SELECT e.seq,COALESCE(r.uploaded,0) AS uploaded FROM tx_event e LEFT JOIN tx_event_route r ON e.scope=r.scope AND e.id=r.id AND r.route=? WHERE e.scope=? ORDER BY e.seq',(self.route,self.store.scope)).fetchall()
        last=0
        for row in rows:
            if not row['uploaded']:break
            last=row['seq']
        manifest=envelope(self.binding,'manifest',{'first_seq':1,'last_seq':last},message_id='manifest-'+str(last),created_at=self.clock(),expires_at=self.clock()+self.envelope_ttl)
        self._put(f'{self.out}/manifest.bin',self.codec.pack(manifest))

    def poll(self,limit=50):
        if not 1<=limit<=200:raise ValueError('读取批量须为1至200')
        raw=self._get(f'{self.out}/manifest.bin')
        if not raw:return self._pending('oss-events',limit)
        manifest=self._incoming(raw)
        if manifest['kind']!='manifest':raise ProtocolError('消息清单种类不符')
        first,last=manifest['payload'].get('first_seq'),manifest['payload'].get('last_seq')
        if type(first) is not int or type(last) is not int or first<1 or last<0:raise ProtocolError('消息清单水位无效')
        cursor=self.store.cursor('oss-events','v2')
        if last<cursor or first>cursor+1:raise ProtocolError(f'消息清单缺口: cursor={cursor}, first_seq={first}, last_seq={last}')
        for seq in range(cursor+1,min(last,cursor+limit)+1):
            body=self._get(f'{self.out}/{seq:020d}.bin')
            if body is None:
                if self.store.pending('oss-events'):break
                raise ProtocolError(f'消息清单对象缺口: source_seq={seq}')
            try:data=self._incoming(body)
            except ProtocolError:raise ProtocolError(f'源事件认证或有效期失败: source_seq={seq}; 请核对源账本后有界重放') from None
            if data['kind']!='event' or data['payload'].get('source_seq')!=seq or data['message_id']!=data['payload'].get('event_id'):
                raise ProtocolError('消息身份或源序号不匹配')
            self.store.stage('oss-events','v2',seq,data)
        return self._pending('oss-events',limit)

    def catalog(self):
        return self._book(f'{self.out}/catalog.bin','catalog')
    def contacts(self):
        return self._book(f'{self.out}/contacts.bin','contacts')
    def _book(self,name,kind):
        raw=self._get(name)
        if not raw:raise TransportUnavailable('NOT_CONFIGURED: DIRECTORY')
        data=self._incoming(raw)
        if data['kind']!=kind:raise ProtocolError('目录种类不符')
        return data['payload']


@dataclass(frozen=True)
class MailConfig:
    smtp_host:str
    smtp_user:str
    smtp_password:str=field(repr=False)
    imap_host:str
    imap_user:str
    imap_password:str=field(repr=False)
    outbound_from:str
    outbound_to:tuple[str,...]
    inbound_from:tuple[str,...]
    inbound_to:tuple[str,...]
    smtp_port:int=465
    imap_port:int=993
    smtp_tls:str='ssl'
    mailbox:str='INBOX'
    timeout:float=20

    def __post_init__(self):
        if not all((self.smtp_host,self.smtp_user,self.smtp_password,self.imap_host,self.imap_user,self.imap_password,self.outbound_from,self.outbound_to,self.inbound_from,self.inbound_to)):
            raise TransportUnavailable('NOT_CONFIGURED: MAIL')
        if self.smtp_tls not in ('ssl','starttls') or any(c in self.mailbox for c in '\r\n\x00'):
            raise TransportUnavailable('MAIL_CONFIGURATION_INVALID')
        for addr in (self.outbound_from,*self.outbound_to,*self.inbound_from,*self.inbound_to):
            if not isinstance(addr,str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+',addr):
                raise TransportUnavailable('MAIL_ALLOWLIST_INVALID')

    @classmethod
    def from_env(cls,env=None):
        e=os.environ if env is None else env
        try:
            return cls(smtp_host=e['XUNJIA_SMTP_HOST'],smtp_user=e['XUNJIA_SMTP_USER'],smtp_password=e['XUNJIA_SMTP_PASSWORD'],
                       imap_host=e['XUNJIA_IMAP_HOST'],imap_user=e['XUNJIA_IMAP_USER'],imap_password=e['XUNJIA_IMAP_PASSWORD'],
                       outbound_from=e['XUNJIA_MAIL_FROM'],outbound_to=tuple(e['XUNJIA_MAIL_TO'].split(',')),
                       inbound_from=tuple(e['XUNJIA_MAIL_ALLOWED_FROM'].split(',')),inbound_to=tuple(e['XUNJIA_MAIL_ALLOWED_TO'].split(',')),
                       smtp_port=int(e.get('XUNJIA_SMTP_PORT','465')),imap_port=int(e.get('XUNJIA_IMAP_PORT','993')),smtp_tls=e.get('XUNJIA_SMTP_TLS','ssl'),mailbox=e.get('XUNJIA_IMAP_MAILBOX','INBOX'))
        except (KeyError,ValueError):raise TransportUnavailable('NOT_CONFIGURED: MAIL') from None


def _smtp(config):
    context=ssl.create_default_context()
    if config.smtp_tls=='ssl':return smtplib.SMTP_SSL(config.smtp_host,config.smtp_port,timeout=config.timeout,context=context)
    connection=smtplib.SMTP(config.smtp_host,config.smtp_port,timeout=config.timeout)
    try:connection.ehlo();connection.starttls(context=context);connection.ehlo()
    except BaseException:connection.close();raise
    return connection


def _imap(config):
    return imaplib.IMAP4_SSL(config.imap_host,config.imap_port,ssl_context=ssl.create_default_context(),timeout=config.timeout)


class MailTransport(_Transport):
    route='mail'
    MAX_MAIL_BYTES=8*1024*1024
    def __init__(self,binding:Binding,config:MailConfig,path,*,smtp_factory=None,imap_factory=None,**kwargs):
        if config is None:raise TransportUnavailable('NOT_CONFIGURED: MAIL')
        super().__init__(binding,path,**kwargs);self.config=config
        self.smtp_factory=smtp_factory or _smtp;self.imap_factory=imap_factory or _imap
        self.stream='imap-'+digest([config.imap_host,config.imap_user,config.mailbox])[:20]

    def _mail(self,data,blob):
        msg=EmailMessage()
        msg['From']=self.config.outbound_from;msg['To']=', '.join(self.config.outbound_to)
        msg['Subject']=('[xunjia-v2] '+data['kind']+' '+token(data['message_id']))[:200]
        msg['Message-ID']='<xj.'+hashlib.sha256(data['message_id'].encode()).hexdigest()+'@xunjia.invalid>'
        msg['X-Xunjia-Version']='2'
        msg.set_content('Authenticated xunjia protocol attachment. No action is authorized by this visible body.')
        msg.add_attachment(blob,maintype='application',subtype='octet-stream',filename='xunjia-v2.bin')
        return msg
    def _publish(self,data,blob):
        connection=self.smtp_factory(self.config)
        try:
            connection.login(self.config.smtp_user,self.config.smtp_password)
            refused=connection.send_message(self._mail(data,blob),from_addr=self.config.outbound_from,to_addrs=list(self.config.outbound_to))
            if refused:raise TransportUnavailable('SMTP_RECIPIENT_REJECTED')
        finally:
            try:connection.quit()
            except Exception:pass

    def _parse(self,raw):
        try:return self._parse_checked(raw)
        except ProtocolError:raise
        except Exception:raise ProtocolError('邮件解析失败') from None

    def _parse_checked(self,raw):
        if not isinstance(raw,bytes) or len(raw)>self.MAX_MAIL_BYTES:raise ProtocolError('邮件大小超限')
        msg=BytesParser(policy=policy.default).parsebytes(raw)
        if msg.defects or len(msg.get_all('From',[]))!=1 or len(msg.get_all('To',[]))!=1:raise ProtocolError('邮件头无效')
        senders=[address.lower() for _,address in getaddresses(msg.get_all('From',[]))]
        recipients=[address.lower() for _,address in getaddresses(msg.get_all('To',[]))]
        if len(senders)!=1 or senders[0] not in {x.lower() for x in self.config.inbound_from} or not recipients or not set(recipients)<={x.lower() for x in self.config.inbound_to}:
            raise ProtocolError('邮件不在固定收发白名单')
        parts=[]
        for part in msg.walk():
            parts.append(part)
            if len(parts)>12:raise ProtocolError('邮件结构过深或部件过多')
        if len(parts)>12 or any(p.defects or p.get_content_type() in ('text/html','message/rfc822') for p in parts):raise ProtocolError('邮件结构不受支持')
        attachments=[p for p in parts if p.get_content_disposition()=='attachment']
        if len(attachments)!=1 or attachments[0].get_filename()!='xunjia-v2.bin' or attachments[0].get_content_type()!='application/octet-stream':
            raise ProtocolError('邮件协议附件无效')
        return self._incoming(attachments[0].get_payload(decode=True))

    def poll(self,limit=50):
        if not 1<=limit<=200:raise ValueError('读取批量须为1至200')
        connection=self.imap_factory(self.config)
        try:
            connection.login(self.config.imap_user,self.config.imap_password)
            status,_=connection.select(self.config.mailbox,readonly=True)
            if status!='OK':raise TransportUnavailable('IMAP_MAILBOX_UNAVAILABLE')
            _,values=connection.response('UIDVALIDITY')
            if not values or not re.fullmatch(rb'[0-9]+',values[0]):raise TransportUnavailable('IMAP_UIDVALIDITY_UNAVAILABLE')
            epoch=values[0].decode('ascii');cursor=self.store.cursor(self.stream,epoch)
            status,found=connection.uid('search',None,f'UID {cursor+1}:*')
            if status!='OK':raise TransportUnavailable('IMAP_SEARCH_UNAVAILABLE')
            ids=sorted({int(x) for x in (found[0] or b'').split() if x.isdigit() and int(x)>cursor})[:limit]
            for uid in ids:
                status,metadata=connection.uid('fetch',str(uid),'(RFC822.SIZE)')
                size=re.search(rb'RFC822.SIZE\s+(\d+)',b' '.join(x for x in metadata if isinstance(x,bytes)))
                if status!='OK' or not size or int(size[1])>self.MAX_MAIL_BYTES:
                    self.store.stage(self.stream,epoch,uid,reason='mail-size-unavailable-or-too-large');continue
                status,body=connection.uid('fetch',str(uid),'(BODY.PEEK[])')
                raw=next((x[1] for x in body if isinstance(x,tuple) and len(x)==2 and isinstance(x[1],bytes)),None)
                if status!='OK' or raw is None:raise TransportUnavailable('IMAP_BODY_UNAVAILABLE')
                try:data=self._parse(raw)
                except ProtocolError:
                    self.store.stage(self.stream,epoch,uid,reason='mail-authentication-or-attachment-rejected:'+hashlib.sha256(raw).hexdigest());continue
                self.store.stage(self.stream,epoch,uid,data)
        finally:
            try:connection.logout()
            except Exception:pass
        return self._pending(self.stream,limit)

    def reconcile(self,command):
        self._command(command,'query');self.poll()
        receipt=self.store.receipt(command)
        if receipt and receipt['status'] in ('sent','failed','cancelled'):return receipt
        self._submit(command,'query')
        return receipt or self._waiting(command)
    def _publish_event(self,data,blob):self._publish(data,blob)
    def _latest_book(self,kind):
        self.poll()
        with self.store.db() as db:rows=db.execute('SELECT data FROM tx_message WHERE scope=? AND kind=?',(self.store.scope,kind)).fetchall()
        if not rows:raise TransportUnavailable('NOT_CONFIGURED: DIRECTORY')
        data=max((json.loads(row['data']) for row in rows),key=lambda item:item['created_at'])
        self.codec._validate(data)
        return data['payload']
    def catalog(self):return self._latest_book('catalog')
    def contacts(self):return self._latest_book('contacts')


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
