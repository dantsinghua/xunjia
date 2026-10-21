"""Durable its-relay transport with isolated download failures.
Downloads consume files. Preserve uncertain outcomes for diagnostics without
blocking later files; native receipt and command deduplication remain authoritative.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time
from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth
from relay.protocol import ProtocolError, canonical, digest
from .receiver_ledger import account_lock
from .transports import _Transport, TransportUnavailable

UPLINK = frozenset(("event", "receipt", "catalog", "contacts", "session_reset_ack"))
DOWNLINK = frozenset(("command", "cancel", "query", "session_reset"))

class FileGatewayTransport(_Transport):
    route = "oss"  # Business route unchanged; only the file carrier changes.
    polls_downlink = True

    def __init__(self, binding, inbound, outbound, path, *, role, allow_shared_test_queue=False, **kwargs):
        if role not in ("service", "collector") or type(allow_shared_test_queue) is not bool:
            raise FileGatewayError("FILE_GATEWAY_ROLE_INVALID")
        if inbound.queue_identity == outbound.queue_identity and not allow_shared_test_queue:
            raise FileGatewayError("FILE_GATEWAY_SEPARATE_QUEUES_REQUIRED")
        self.inbound, self.outbound, self.role = inbound, outbound, role
        self.incoming_kinds = UPLINK if role == "service" else DOWNLINK
        self.outgoing_kinds = DOWNLINK if role == "service" else UPLINK
        self.stream = "file-gateway-" + role
        self.allow_shared_test_queue = allow_shared_test_queue
        super().__init__(binding, path, **kwargs)
        self.store.path.chmod(0o600)
        identity = canonical([role, inbound.queue_identity, outbound.queue_identity]).decode()
        with self.store.db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS fg_identity(scope TEXT PRIMARY KEY,identity TEXT NOT NULL); CREATE TABLE IF NOT EXISTS fg_upload(scope TEXT,id TEXT,state TEXT,file_key TEXT,code TEXT,PRIMARY KEY(scope,id)); CREATE TABLE IF NOT EXISTS fg_download(seq INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT,state TEXT,name TEXT,blob BLOB,code TEXT);")
            columns={r[1] for r in db.execute("PRAGMA table_info(fg_download)")}
            if "received_at" not in columns:db.execute("ALTER TABLE fg_download ADD COLUMN received_at REAL")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT identity FROM fg_identity WHERE scope=?", (self.store.scope,)).fetchone()
            if row and row[0] != identity:
                raise FileGatewayError("FILE_GATEWAY_STATE_ROUTE_CHANGED")
            db.execute("INSERT OR IGNORE INTO fg_identity VALUES(?,?)", (self.store.scope, identity))

    @classmethod
    def from_config(cls, binding, config, path, *, role, config_dir):
        if isinstance(config, dict) and config.get("contract") == "its-relay-path-v1":
            from .institution_transport import InstitutionTransport
            return InstitutionTransport.from_institution_config(binding, config, path, role=role, config_dir=config_dir)
        allowed = {"uplink_url", "downlink_url", "auth_file", "allow_http", "timeout_seconds", "max_bytes"}
        if not isinstance(config, dict) or set(config) - allowed or not {"uplink_url", "downlink_url", "auth_file"} <= config.keys():
            raise FileGatewayError("FILE_GATEWAY_CONFIG_INVALID")
        auth_path = Path(config["auth_file"])
        if not auth_path.is_absolute():
            auth_path = Path(config_dir) / auth_path
        auth = GatewayAuth.from_file(auth_path)
        options = dict(allow_http=config.get("allow_http", False), timeout=config.get("timeout_seconds", 15), max_bytes=config.get("max_bytes", 4*1024*1024))
        up = FileGatewayClient(config["uplink_url"], auth, **options)
        down = FileGatewayClient(config["downlink_url"], auth, **options)
        return cls(binding, up if role == "service" else down, down if role == "service" else up, path, role=role)

    def _publish(self, data, blob):
        if data["kind"] not in self.outgoing_kinds:
            raise ProtocolError("FILE_GATEWAY_OUTBOUND_DIRECTION_INVALID")
        identity = hashlib.sha256(blob).hexdigest()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM fg_upload WHERE scope=? AND id=?", (self.store.scope, identity)).fetchone()
            if row and row[0] == "UPLOADED":
                return
            if row and row[0] in ("UPLOADING", "UNKNOWN"):
                raise FileGatewayError("FILE_UPLOAD_OUTCOME_UNKNOWN", ambiguous=True)
            db.execute("INSERT INTO fg_upload VALUES(?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET state=excluded.state,code=excluded.code", (self.store.scope, identity, "UPLOADING", "", ""))
        try:
            result = self.outbound.upload("xunjia-v22-" + data["kind"] + "-" + identity[:32] + ".bin", blob)
        except Exception as exc:
            ambiguous = not isinstance(exc, FileGatewayError) or exc.ambiguous
            code = exc.code if isinstance(exc, FileGatewayError) else "FILE_UPLOAD_OUTCOME_UNKNOWN"
            with self.store.db() as db:
                db.execute("UPDATE fg_upload SET state=?,code=? WHERE scope=? AND id=?", ("UNKNOWN" if ambiguous else "REJECTED", code, self.store.scope, identity))
            raise FileGatewayError(code, status=getattr(exc,"status",None), ambiguous=ambiguous) from None
        with self.store.db() as db:
            db.execute("UPDATE fg_upload SET state=?,file_key=?,code=? WHERE scope=? AND id=?", ("UPLOADED", result["file_key"], "", self.store.scope, identity))

    def _publish_event(self, data, blob):
        self._publish(data, blob)

    def receive_ready(self):
        # Historical file outcomes do not disable the account intake.
        return True

    def _stage_downloads(self):
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM fg_download WHERE scope=? AND state=? ORDER BY seq", (self.store.scope, "STORED")).fetchall()
        for row in rows:
            try:
                data = self.codec.unpack(bytes(row["blob"]))
                if data["kind"] not in self.incoming_kinds:
                    raise ProtocolError("FILE_QUEUE_DIRECTION_MISMATCH")
                self.store.stage(self.stream, "v1", row["seq"], data)
            except ProtocolError:
                with self.store.db() as db:
                    db.execute("UPDATE fg_download SET state=?,code=? WHERE seq=? AND scope=?", ("QUARANTINED", "FILE_QUEUE_ENVELOPE_REJECTED", row["seq"], self.store.scope))
                continue
            with self.store.db() as db:
                db.execute("UPDATE fg_download SET state=? WHERE seq=? AND scope=?", ("STAGED", row["seq"], self.store.scope))

    def _poll_pending(self, limit, control_intake):
        if not control_intake:
            return self._pending(self.stream, limit)
        rows = self.store.pending(self.stream)
        rows.sort(key=lambda row: row['kind'] != 'session_reset')
        rows = rows[:limit]
        for row in rows:
            # These bytes were authenticated before durable staging. The
            # Bridge still rejects an expired command before execution; allow
            # that rejection to retire the old input without hiding a reset.
            self.codec._validate(row['envelope'], allow_expired=row['kind'] in DOWNLINK)
        return rows

    def poll(self, limit=50, *, control_intake=False):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Invalid file-gateway poll limit")
        if type(control_intake) is not bool or control_intake and self.role != 'collector':
            raise ValueError('Reset control intake requires the collector role')
        # Separate from the publication lock: the native guard only reads local state.
        with account_lock(str(self.store.path) + ".download", self.binding):
            self._stage_downloads()
            pending = self._poll_pending(limit, control_intake)
            if pending and not control_intake:
                return pending
            for _ in range(limit):
                with self.store.db() as db:
                    seq = db.execute("INSERT INTO fg_download(scope,state,name,blob,code) VALUES(?,?,?,?,?)", (self.store.scope, "RECEIVING", "", None, "")).lastrowid
                try:
                    value = self.inbound.download()
                except Exception as exc:
                    ambiguous = not isinstance(exc, FileGatewayError) or exc.ambiguous
                    code = exc.code if isinstance(exc, FileGatewayError) else "FILE_DOWNLOAD_OUTCOME_UNKNOWN"
                    with self.store.db() as db:
                        db.execute("UPDATE fg_download SET state=?,code=? WHERE seq=? AND scope=?", ("UNKNOWN" if ambiguous else "REJECTED", code, seq, self.store.scope))
                    raise FileGatewayError(code, status=getattr(exc,"status",None), ambiguous=ambiguous) from None
                with self.store.db() as db:
                    db.execute("UPDATE fg_download SET state=?,name=?,blob=?,received_at=? WHERE seq=? AND scope=?", ("EMPTY" if value is None else "STORED", value.name if value else "", value.content if value else None, self.clock() if value else None, seq, self.store.scope))
                if value is None:
                    break
                self._stage_downloads()
            return self._poll_pending(limit, control_intake)

    def reconcile(self, command):
        self._command(command, "query")
        self.poll()
        receipt = self.store.receipt(command)
        if receipt and receipt["status"] in ("sent", "failed", "cancelled"):
            return receipt
        self._submit(command, "query")
        return receipt or self._waiting(command)

    def _book(self, kind):
        self.poll()
        with self.store.db() as db:
            row = db.execute("SELECT data FROM tx_message WHERE scope=? AND kind=? ORDER BY rowid DESC LIMIT 1", (self.store.scope, kind)).fetchone()
        if row is None:
            raise TransportUnavailable("NOT_CONFIGURED: FILE_GATEWAY_DIRECTORY")
        value = json.loads(row[0]); self.codec._validate(value)
        return value["payload"]

    def catalog(self):
        return self._book("catalog")

    def contacts(self):
        return self._book("contacts")

    def delivery_diagnostics(self):
        with self.store.db() as db:
            downloads = db.execute("SELECT seq,state,code FROM fg_download WHERE scope=? AND state IN (?,?,?) ORDER BY seq LIMIT 50", (self.store.scope, "RECEIVING", "UNKNOWN", "QUARANTINED")).fetchall()
            uploads = db.execute("SELECT id,state,code FROM fg_upload WHERE scope=? AND state IN (?,?) LIMIT 50", (self.store.scope, "UPLOADING", "UNKNOWN")).fetchall()
        return {"carrier": "its-relay", "role": self.role, "independent_queues": self.inbound.queue_identity != self.outbound.queue_identity, "download_replay_supported": False, "download_blockers": [], "download_issues": [dict(r) for r in downloads], "upload_unknown": [dict(r) for r in uploads]}
