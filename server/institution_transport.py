"""Account-isolated path transport; V2.2 execution and UNKNOWN semantics retained."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from pathlib import Path
from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth
from relay.file_gateway_connection import connection_client
from relay.gateway_paths import CONTRACT, InstitutionKeys, account_paths, HISTORY_SECONDS
from relay.institution_codec import InstitutionCodec, retarget
from relay.protocol import Codec, ProtocolError, canonical, digest, envelope, result, token
from .file_gateway_transport import FileGatewayTransport

class InstitutionTransport(FileGatewayTransport):
    def __init__(self, binding, inbound, outbound, path, *, role, institution_owner, clock=time.time):
        super().__init__(binding, inbound, outbound, path, role=role, clock=clock)
        self.institution_owner = institution_owner
        if role == "service":
            self.codec = InstitutionCodec(binding, institution_owner, clock=clock)
        self.incoming_kinds = self.incoming_kinds | {"manifest"}
        self.outgoing_kinds = self.outgoing_kinds | {"manifest"}
        self.background_errors = {}
        self.online_guard = None
        self.manifest_handler = None
        self.catalog_provider = None
        self.contacts_provider = None
        with self.store.db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS ip_delivery(scope TEXT,id TEXT,kind TEXT,fingerprint TEXT,blob BLOB,first_at REAL,last_at REAL,attempts INTEGER,acked INTEGER DEFAULT 0,PRIMARY KEY(scope,id)); CREATE TABLE IF NOT EXISTS ip_ack(scope TEXT,id TEXT,fingerprint TEXT,PRIMARY KEY(scope,id));")

    @classmethod
    def from_institution_config(cls, binding, config, path, *, role, config_dir):
        allowed = {"contract","base_url","auth_file","allow_http","timeout_seconds","max_bytes","hostname","addresses"}
        if set(config) - allowed or config.get("contract") != CONTRACT:
            raise FileGatewayError("FILE_GATEWAY_CONFIG_INVALID")
        auth_path = Path(config["auth_file"])
        if not auth_path.is_absolute(): auth_path = Path(config_dir) / auth_path
        auth = GatewayAuth.from_file(auth_path)
        keys = InstitutionKeys.from_auth(auth)
        if binding.seal_key != keys.seal_key or binding.sign_key != keys.sign_key:
            raise FileGatewayError("FILE_GATEWAY_INSTITUTION_KEYS_MISMATCH")
        paths = account_paths(binding.workbench_id, binding.account_uid)
        up = connection_client(config, auth, allow_http=config.get("allow_http",False), path=paths["uplink_path"])
        down = connection_client(config, auth, allow_http=config.get("allow_http",False), path=paths["downlink_path"])
        return cls(binding, up if role == "service" else down, down if role == "service" else up, path, role=role, institution_owner=keys.owner_id)

    def _fingerprint(self, data):
        wire = retarget(data, self.institution_owner)
        return digest({k:v for k,v in wire.items() if k not in ("created_at","expires_at","transport_replay")})

    def _safe_retry(self, data):
        return data["kind"] in ("event","receipt") or (data["kind"] == "manifest" and data["payload"].get("op") != "data_ack")

    def _publish(self, data, blob):
        if not self._safe_retry(data):
            return super()._publish(data, blob)
        fp = self._fingerprint(data)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint,acked FROM ip_delivery WHERE scope=? AND id=?", (self.store.scope,data["message_id"])).fetchone()
            if old and old["fingerprint"] != fp: raise ProtocolError("PATH_DELIVERY_ID_CONFLICT")
            if old and old["acked"]: return
            db.execute("INSERT OR IGNORE INTO ip_delivery(scope,id,kind,fingerprint,blob,first_at,last_at,attempts) VALUES(?,?,?,?,?,?,?,0)", (self.store.scope,data["message_id"],data["kind"],fp,blob,self.clock(),0))
        # Retain an ambiguous first attempt. Later retries are only immutable data,
        # never an execute-command. A native UNKNOWN is never reset here.
        try: return super()._publish(data,blob)
        finally:
            with self.store.db() as db: db.execute("UPDATE ip_delivery SET last_at=?,attempts=attempts+1 WHERE scope=? AND id=?",(self.clock(),self.store.scope,data["message_id"]))

    def _queue_ack(self, data):
        if not self._safe_retry(data): return
        with self.store.db() as db:
            db.execute("INSERT INTO ip_ack VALUES(?,?,?) ON CONFLICT(scope,id) DO UPDATE SET fingerprint=excluded.fingerprint",(self.store.scope,data["message_id"],self._fingerprint(data)))

    def ack(self, source_token):
        with self.store.db() as db:
            row = db.execute("SELECT m.data FROM tx_source s JOIN tx_message m ON m.scope=s.scope AND m.kind=s.kind AND m.id=s.id WHERE s.scope=? AND s.token=?",(self.store.scope,source_token)).fetchone()
        if row: self._queue_ack(json.loads(row["data"]))
        # Ack is called only after business persistence, as in the existing core.
        super().ack(source_token)

    def _stage_downloads(self):
        with self.store.db() as db:
            fresh = db.execute("SELECT blob FROM fg_download WHERE scope=? AND state=?", (self.store.scope, "STORED")).fetchall()
        super()._stage_downloads()
        # Only a newly downloaded duplicate re-acks; idle polls publish nothing.
        for row in fresh:
            try:
                data = self.codec.unpack(bytes(row["blob"]))
            except ProtocolError:
                continue
            with self.store.db() as db:
                saved = db.execute("SELECT acked FROM tx_message WHERE scope=? AND kind=? AND id=?", (self.store.scope,data["kind"],data["message_id"])).fetchone()
            if saved and saved["acked"] and self._safe_retry(data):
                self._queue_ack(data)

    def publish_manifest(self, payload, *, message_id=None):
        if not isinstance(payload,dict) or payload.get("contract") != CONTRACT: raise ProtocolError("PATH_MANIFEST_CONTRACT_REQUIRED")
        now = self.clock()
        data = envelope(self.binding,"manifest",payload,message_id=message_id or "ip-"+uuid.uuid4().hex,created_at=now,expires_at=now+HISTORY_SECONDS)
        blob = self.store.packet(data["message_id"],data,self.codec)
        self._publish(data,blob)
        return data["message_id"]

    def _flush_acks(self):
        with self.store.db() as db: rows = db.execute("SELECT id,fingerprint FROM ip_ack WHERE scope=? LIMIT 100",(self.store.scope,)).fetchall()
        if not rows:return
        payload = {"contract":CONTRACT,"op":"data_ack","items":[dict(row) for row in rows]}
        self.publish_manifest(payload)
        with self.store.db() as db:
            db.executemany("DELETE FROM ip_ack WHERE scope=? AND id=? AND fingerprint=?",[(self.store.scope,r["id"],r["fingerprint"]) for r in rows])

    def _accept_ack(self, payload):
        items = payload.get("items")
        if not isinstance(items,list) or len(items)>100:raise ProtocolError("PATH_ACK_INVALID")
        with self.store.db() as db:
            for item in items:
                if not isinstance(item,dict):raise ProtocolError("PATH_ACK_INVALID")
                token(item.get("id"))
                if not isinstance(item.get("fingerprint"),str) or len(item["fingerprint"])!=64:raise ProtocolError("PATH_ACK_INVALID")
                db.execute("UPDATE ip_delivery SET acked=1 WHERE scope=? AND id=? AND fingerprint=?",(self.store.scope,item["id"],item["fingerprint"]))

    def retry_data(self, limit=4):
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM ip_delivery WHERE scope=? AND acked=0 AND first_at>? AND last_at<? ORDER BY last_at LIMIT ?",(self.store.scope,self.clock()-HISTORY_SECONDS,self.clock()-30,limit)).fetchall()
        for row in rows:
            blob = bytes(row["blob"])
            try:
                data = self.codec.unpack(blob)
                if not self._safe_retry(data):continue
                self.outbound.upload("xj-data-"+hashlib.sha256(blob).hexdigest()[:32]+".bin",blob)
            finally:
                with self.store.db() as db:db.execute("UPDATE ip_delivery SET last_at=?,attempts=attempts+1 WHERE scope=? AND id=?",(self.clock(),self.store.scope,row["id"]))

    def poll(self, limit=50, *, control_intake=False):
        rows = super().poll(limit=limit,control_intake=control_intake)
        business = []
        for row in rows:
            data = row["envelope"]
            if data["kind"] != "manifest":
                business.append(row); continue
            payload = data["payload"]
            if payload.get("contract") != CONTRACT:raise ProtocolError("PATH_MANIFEST_VERSION_UNSUPPORTED")
            if payload.get("op") == "data_ack":self._accept_ack(payload)
            elif self.manifest_handler is not None:self.manifest_handler(data)
            else:raise ProtocolError("PATH_MANIFEST_HANDLER_UNAVAILABLE")
            self.ack(row["source_token"])
        for name,operation in (("data_ack",self._flush_acks),("data_retry",self.retry_data)):
            try:
                operation();self.background_errors.pop(name,None)
            except (OSError,FileGatewayError) as exc:
                self.background_errors[name]={"code":getattr(exc,"code",type(exc).__name__),"status":getattr(exc,"status",None),"at":self.clock()}
        return business

    def delivery_diagnostics(self):
        return {**super().delivery_diagnostics(),"background_errors":dict(self.background_errors)}

    def send(self, command):
        if self.role=="service" and (self.online_guard is None or not self.online_guard()):
            return result(command,"failed","collector-offline-before-submission")
        if str(command.get("peer_id","")).startswith("g_"):
            return result(command,"failed","group-auto-reply-not-supported")
        return super().send(command)

    def catalog(self):
        return self.catalog_provider() if self.catalog_provider else super().catalog()

    def contacts(self):
        return self.contacts_provider() if self.contacts_provider else super().contacts()
