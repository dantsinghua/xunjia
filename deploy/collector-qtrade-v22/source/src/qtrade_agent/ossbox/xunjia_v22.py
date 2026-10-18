"""Opt-in V2.2 collector host. It never starts an IM account or a native sender.

The existing Agent owns this worker and its event hook. Protocol dependencies
are imported only when V2.2 is explicitly configured; no legacy fallback occurs.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

log = logging.getLogger('qtrade.ossbox.xunjia_v22')


def collector_version(env=None):
    value = (os.environ if env is None else env).get('XUNJIA_COLLECTOR_VERSION', '1')
    if value not in ('1', '2.2'):
        raise ValueError('xunjia_collector_version_invalid')
    return value


class _ObjectStore:
    def __init__(self, box):
        self.box = box

    def list(self, prefix):
        return self.box.list_prefix(prefix)

    def get(self, name):
        return self.box.get(name)

    def request(self, method, name, body=None):
        if method == 'GET':
            value = self.box.get(name)
            return (404, b'') if value is None else (200, value)
        if method == 'PUT':
            self.box.put(name, body)
            return 200, b''
        raise ValueError('unsupported_collector_oss_operation')


class XunjiaV22Host:
    def __init__(self, *, store, bus, loop, binding, bridge, port, state_path,
                 legacy_inbox_disabled, allow_native_send=False, contacts_provider=None):
        if type(allow_native_send) is not bool or not callable(legacy_inbox_disabled):
            raise ValueError('invalid_collector_execution_policy')
        if not legacy_inbox_disabled():
            raise RuntimeError('legacy_xunjia_inbox_must_be_disabled')
        if getattr(bus, 'supports_xunjia_v22_guard', False) is not True:
            raise RuntimeError('qtrade_v22_native_guard_not_installed')
        if bridge.binding != binding or port.binding != binding:
            raise ValueError('collector_binding_mismatch')
        self.store, self.bus, self.loop = store, bus, loop
        self.binding, self.bridge, self.port = binding, bridge, port
        self.ledger = bridge.ledger
        self.legacy_inbox_disabled = legacy_inbox_disabled
        self.allow_native_send = allow_native_send
        self.contacts_provider = contacts_provider
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active = False
        self._intake_ok = True
        self._worker = None
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._last_catalog = 0.0
        self.port.allow_native_send = False
        self.port.execution_permission = self.permission
        self.bridge.native_send = self._send
        self.bridge.native_reconcile = self.port.native_reconcile
        identity = json.dumps([binding.owner_user_id, binding.account_uid, binding.workbench_id])
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS host_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS host_event (event_id TEXT PRIMARY KEY, body TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS host_native (native_key TEXT PRIMARY KEY, original_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL, revoked INTEGER NOT NULL, outbox_seq INTEGER NOT NULL DEFAULT 0);
            ''')
            old = db.execute("SELECT value FROM host_meta WHERE key='binding'").fetchone()
            if old is not None and old['value'] != identity:
                raise ValueError('collector_state_belongs_to_another_binding')
            db.execute("INSERT OR IGNORE INTO host_meta VALUES('binding',?)", (identity,))
            if db.execute("SELECT 1 FROM host_meta WHERE key='installation_seq'").fetchone() is None:
                bounds = self.store.outbox_seq_bounds() if hasattr(self.store, 'outbox_seq_bounds') else (None, None)
                db.execute("INSERT INTO host_meta VALUES('installation_seq',?)", (str(bounds[1] or 0),))
            db.execute("INSERT OR IGNORE INTO host_meta VALUES('replayed_seq','0')")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.state_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @classmethod
    def from_env(cls, *, store, bus, loop, box, data_dir, legacy_inbox_disabled, contacts_provider=None):
        if collector_version() != '2.2':
            return None
        if not legacy_inbox_disabled():
            raise RuntimeError('legacy_xunjia_inbox_must_be_disabled')
        config_path = os.environ.get('XUNJIA_COLLECTOR_CONFIG_FILE', '')
        if not config_path or box is None:
            raise ValueError('NOT_CONFIGURED: XUNJIA_COLLECTOR_CONFIG_FILE or OSS')
        # Production QTrade boxes must use STS. Synthetic boxes are explicit test injection.
        if hasattr(box, '_credentials_provider') and box._credentials_provider is None:
            raise ValueError('NOT_CONFIGURED: collector_requires_OSS_STS')
        try:
            path = Path(config_path)
            if path.stat().st_size > 16384:
                raise ValueError
            config = json.loads(path.read_text(encoding='utf-8'))
            required = {'owner_user_id', 'account_uid', 'workbench_id', 'seal_key_file', 'sign_key_file'}
            if not isinstance(config, dict) or not required <= config.keys() or set(config) - required - {'allow_native_send'}:
                raise ValueError
            if type(config.get('allow_native_send', False)) is not bool:
                raise ValueError
            def key(name):
                target = Path(config[name])
                if not target.is_absolute():
                    target = path.parent / target
                if target.stat().st_size > 4096:
                    raise ValueError
                return bytes.fromhex(target.read_text(encoding='ascii').strip())
            seal, sign = key('seal_key_file'), key('sign_key_file')
        except Exception:
            raise ValueError('NOT_CONFIGURED: invalid_private_collector_configuration') from None
        try:
            from relay.protocol import Binding
            from relay.collector_bridge import CollectorBridge
            from relay.qtrade_native_port import QTradeNativePort
            from server.receiver_ledger import ReceiverLedger
            from server.transports import OSSTransport
        except ImportError:
            raise RuntimeError('NOT_CONFIGURED: xunjia_v22_protocol_package_not_installed') from None
        binding = Binding(config['owner_user_id'], config['account_uid'], config['workbench_id'], seal, sign)
        workbench = os.environ.get('OSS_WORKBENCH_ID')
        if workbench and workbench != binding.workbench_id:
            raise ValueError('collector_workbench_binding_mismatch')
        root = Path(data_dir) / 'xunjia-v22'
        ledger = ReceiverLedger(root / 'receiver.sqlite3', binding)
        objects = _ObjectStore(box)
        transport = OSSTransport(binding, objects, root / 'publication.sqlite3')
        port = QTradeNativePort(binding=binding, store=store, bus=bus, loop=loop,
            legacy_inbox_disabled=legacy_inbox_disabled, execution_permission=ledger.permission)
        bridge = CollectorBridge(ledger=ledger, adapters={'oss': transport}, object_store=objects,
            native_send=port.native_send, native_reconcile=port.native_reconcile)
        return cls(store=store, bus=bus, loop=loop, binding=binding, bridge=bridge, port=port,
            state_path=root / 'host.sqlite3', legacy_inbox_disabled=legacy_inbox_disabled,
            allow_native_send=config.get('allow_native_send', False), contacts_provider=contacts_provider)

    def permission(self, command):
        return bool(self._active and self._intake_ok and self.allow_native_send
                    and self.legacy_inbox_disabled() and self.ledger.permission(command) is True)

    def _send(self, command):
        if command.get('business_version') != '2.2':
            return {'status': 'failed', 'evidence': 'collector-requires-business-version-2.2'}
        if not self._active or not self.allow_native_send or not self._intake_ok or not self.legacy_inbox_disabled():
            return {'status': 'failed', 'evidence': 'qtrade-v2-native-disabled'}
        return self.port.native_send(command)

    def observe(self, *, event, payload, account_id=None, event_id='', channel=None,
                trace_id=None, now_ms=None, historical=False, source_seq=0):
        if event != 'message' or channel != 'qidian':
            return
        try:
            # Event payload is already the committed QTrade message snapshot.
            row = dict(payload)
            row['account_id'] = account_id or row.get('account_id')
            row['session_id'] = (row.get('session') or {}).get('id', row.get('session_id'))
            if 'ts_ms' not in row:
                row['ts_ms'] = datetime.fromisoformat(row['ts'].replace('Z', '+00:00')).timestamp() * 1000
            bound = self.port._account()
            if not bound or row['account_id'] != bound['id']:
                return
            data = self.port.normalize_native_message(row, live=not historical)
            native_key = json.dumps([data['peer_id'], data['direction'], data['ext_msg_id']])
            text_hash = hashlib.sha256(data['text'].encode()).hexdigest()
            revoked = int(bool(row.get('revoked')))
            if not source_seq and event_id and hasattr(self.store, 'con'):
                position = self.store.con.execute("SELECT seq FROM events_outbox WHERE target='ws' AND event_id=?", (event_id,)).fetchone()
                source_seq = position['seq'] if position else 0
            with self._lock, self._db() as db:
                previous = db.execute('SELECT * FROM host_native WHERE native_key=?', (native_key,)).fetchone()
                if previous and source_seq and source_seq <= previous['outbox_seq']:
                    return
                if previous and previous['text_hash'] == text_hash and previous['revoked'] == revoked:
                    db.execute('UPDATE host_native SET outbox_seq=MAX(outbox_seq,?) WHERE native_key=?', (source_seq, native_key))
                    return
                original = previous['original_id'] if previous else data['event_id']
                if revoked or previous:
                    kind = 'retract' if revoked else 'edit'
                    data.update(kind=kind, target_event_id=original)
                    data['event_id'] = 'qtrade-' + hashlib.sha256(json.dumps(
                        [original, kind, event_id or text_hash], separators=(',', ':')).encode()).hexdigest()
                # This barrier is synchronous and local: never queue network work first.
                if data['direction'] == 'in' and not data['historical']:
                    self.ledger.begin_inbound(data['event_id'], data['peer_id'])
                elif data['direction'] == 'out':
                    self.ledger.stage_out(data, self.bridge.echo_wait_seconds)
                db.execute('INSERT OR IGNORE INTO host_event(event_id,body) VALUES(?,?)',
                    (data['event_id'], json.dumps(data, ensure_ascii=False)))
                db.execute('INSERT INTO host_native VALUES(?,?,?,?,?) ON CONFLICT(native_key) DO UPDATE SET text_hash=excluded.text_hash,revoked=excluded.revoked,outbox_seq=excluded.outbox_seq',
                    (native_key, original, text_hash, revoked, source_seq))
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self._wake.set)
        except Exception:
            self._intake_ok = False
            log.error('V2.2 native intake failed; automatic native sending blocked')
            raise

    def _replay(self):
        if not hasattr(self.store, 'con'):
            return
        with self._db() as db:
            cursor = int(db.execute("SELECT value FROM host_meta WHERE key='replayed_seq'").fetchone()['value'])
            installed = int(db.execute("SELECT value FROM host_meta WHERE key='installation_seq'").fetchone()['value'])
        rows = self.store.con.execute("SELECT seq,event_id,event,account_id,channel,payload_json,ts_ms FROM events_outbox WHERE target='ws' AND seq>? ORDER BY seq LIMIT 200", (cursor,)).fetchall()
        for row in rows:
            self.observe(event=row['event'], payload=json.loads(row['payload_json']), account_id=row['account_id'],
                channel=row['channel'], event_id=row['event_id'], now_ms=row['ts_ms'], historical=row['seq'] <= installed, source_seq=row['seq'])
            with self._db() as db:
                db.execute("UPDATE host_meta SET value=? WHERE key='replayed_seq'", (str(row['seq']),))

    def _flush_native(self):
        self._replay()
        with self._db() as db:
            rows = db.execute('SELECT event_id,body FROM host_event WHERE published=0 ORDER BY rowid LIMIT 200').fetchall()
        for row in rows:
            data = json.loads(row['body'])
            self.bridge.publish_native(data, historical=data.get('historical', False))
            with self._db() as db:
                db.execute('UPDATE host_event SET published=1 WHERE event_id=?', (row['event_id'],))

    def _publish_directory(self):
        account = self.port._account()
        if account is None:
            return
        payload = {'account_uid': self.binding.account_uid, 'contacts': account.get('contacts', []),
                   'org': account.get('org', {}), 'updated_at': self.ledger.clock()}
        if self.contacts_provider is not None:
            try:
                book = self.contacts_provider(account)
                if not isinstance(book, dict) or not isinstance(book.get('contacts'), list):
                    raise ValueError('collector_contacts_unavailable')
                payload.update(contacts=book['contacts'], org=book.get('org', {}))
            except Exception:
                log.warning('V2.2 contacts unavailable; preserving previous published directory')
        if payload['contacts'] or payload['org']:
            for adapter in self.bridge.adapters.values():
                adapter.publish_contacts(payload)
        self.bridge.publish_capabilities()

    async def run_once(self):
        async with self._tick_lock:
            await asyncio.to_thread(self._flush_native)
            result = await asyncio.to_thread(self.bridge.run_once)
            if self._active and self._intake_ok and self.legacy_inbox_disabled() and time.monotonic() - self._last_catalog >= 60:
                await asyncio.to_thread(self._publish_directory)
                self._last_catalog = time.monotonic()
            return result

    async def _run(self):
        while self._active:
            try:
                await self.run_once()
            except Exception:
                log.warning('V2.2 collector work remains durable; retry on next tick')
            if not self._active:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def start(self):
        if self._worker is not None and not self._worker.done():
            return
        if not self.legacy_inbox_disabled():
            raise RuntimeError('legacy_xunjia_inbox_must_be_disabled')
        self._active = True
        self.port.allow_native_send = self.allow_native_send
        self._worker = asyncio.create_task(self._run(), name='xunjia-v22-collector')

    async def stop(self):
        self._active = False
        self.port.allow_native_send = False
        self._wake.set()
        if self._worker is not None:
            # Do not cancel a to_thread native invocation and pretend it never ran.
            await asyncio.wait_for(asyncio.shield(self._worker), timeout=120)
            self._worker = None
