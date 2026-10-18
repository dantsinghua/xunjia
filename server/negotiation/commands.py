"""Reply transaction and durable command outcomes, isolated from peer facts.
No network I/O is performed here. The host MUST check execution_permission
immediately before native dispatch and carry dispatch_watermark to its receiver.
This module does not replace the Chatflow semantic/text validator.
"""
from __future__ import annotations
import hashlib
import json
import math
from .prices import InvalidFact, Price, StaleDecision
from .store import encoded, identity, new_id
class Commands:
    def __init__(self, store, *, current_day=None):
        self.store = store
        self.current_day = current_day
    def basis(self, owner, member_id, *, depends_on_group_volume=False):
        if type(depends_on_group_volume) is not bool:
            raise InvalidFact("Explicit group-volume dependency required")
        with self.store.db.transaction(write=False) as tx:
            return self._basis(tx, owner, member_id, depends_on_group_volume)
    def _basis(self, tx, owner, member_id, depends):
        member = self.store._member(tx, owner, member_id)
        group = self.store._group(tx, owner, member["group_id"])
        thread = self.store._thread(tx, owner, member["thread_id"])
        control = tx.execute("SELECT control_version FROM xj2_owner WHERE owner_id=?", (owner,)).fetchone()
        binding=tx.execute('SELECT version FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
        return {"binding_version":binding['version'] if binding else 0,
                "member_id": member_id, "member_version": member["version"],
                "member_control": member["control_version"], "group_id": group["group_id"],
                "group_policy": group["policy_version"], "group_control": group["control_version"],
                "group_volume_version": group["version"] if depends else None,
                "owner_control": control["control_version"], "trade_day": group["trade_day"],
                "observed_watermark": thread["analyzed_revision"]}
    def _command(self, tx, owner, command_id):
        row = tx.execute("SELECT c.* FROM xj2_command c JOIN xj2_group g ON g.group_id=c.group_id WHERE c.command_id=? AND g.owner_id=?", (command_id, owner)).fetchone()
        if not row:
            raise InvalidFact("Unknown command for this owner")
        return dict(row)
    def get(self, owner, command_id):
        with self.store.db.transaction(write=False) as tx:
            return self._command(tx, owner, command_id)
    def _gate(self, tx, owner, member_id, basis, *, command_id=None, kind="REPLY"):
        member = self.store._member(tx, owner, member_id)
        group = self.store._group(tx, owner, member["group_id"])
        thread = self.store._thread(tx, owner, member["thread_id"])
        control = tx.execute("SELECT * FROM xj2_owner WHERE owner_id=?", (owner,)).fetchone()
        current = self._basis(tx, owner, member_id, basis.get("group_volume_version") is not None)
        if kind!='MANUAL' and self.current_day is not None and group['trade_day']!=self.current_day():
            return 'DAILY_ROLLOVER_REQUIRED'
        if set(basis) != set(current):
            return "INVALID_BASIS"
        for key in current:
            if key != "observed_watermark" and basis[key] != current[key]:
                return "STALE_BASIS"
        if (type(basis["observed_watermark"]) is not int or
                not 0 <= basis["observed_watermark"] <= thread["analyzed_revision"]):
            return "INVALID_WATERMARK"
        if kind != "MANUAL" and (not control["send_allowed"] or not group["send_allowed"] or not member["send_allowed"] or group["state"] != "OPEN"):
            return "SENDING_PAUSED"
        if kind != "MANUAL" and thread["analyzed_revision"] != thread["ingest_revision"]:
            return "WAITING_ANALYSIS"
        if kind not in ("MANUAL","CLARIFY") and tx.execute("SELECT message_id FROM xj2_scope WHERE member_id=? AND relation IN ('UNRESOLVED','INVALID') LIMIT 1", (member_id,)).fetchone():
            return "UNRESOLVED_BOND_REFERENCE"
        if member["reconfirm_day"] and kind not in ("RECONFIRM","CLARIFY","MANUAL"):
            return "DAILY_RECONFIRMATION_REQUIRED"
        if member["pending_command_id"] and member["pending_command_id"] != command_id:
            return "MEMBER_HAS_PENDING_COMMAND"
        return None
    @staticmethod
    def _body(value):
        if not isinstance(value, dict) or set(value) - {"kind", "text", "our_price", "scope_members", "scope_prices", "scope_bases"}:
            raise InvalidFact("Invalid validated reply body")
        if value.get("kind") not in ("REPLY", "RECONFIRM", "ASK_VOLUME", "CLARIFY", "SOCIAL", "INITIAL", "MANUAL"):
            raise InvalidFact("Invalid reply action")
        if not isinstance(value.get("text"), str) or not 1 <= len(value["text"].strip()) <= 1500:
            raise InvalidFact("Reply text is empty or too long")
        result = dict(value)
        if result.get("our_price") is not None:
            result["our_price"] = Price.load(result["our_price"]).dump()
        if 'scope_members' in result:
            scope=result['scope_members']
            if not isinstance(scope,list) or not 1<=len(scope)<=50 or len(set(scope))!=len(scope):
                raise InvalidFact('Invalid multi-bond command scope')
            if result['kind'] not in ('INITIAL','MANUAL'):
                raise InvalidFact('Only an authorized initial or human message may span bonds')
            for key in ('scope_prices','scope_bases'):
                if not isinstance(result.get(key,{}),dict) or set(result.get(key,{}))-set(scope):
                    raise InvalidFact('Invalid scoped command metadata')
            result['scope_prices']={mid:Price.load(p).dump() for mid,p in result.get('scope_prices',{}).items()}
        return result
    def _scope_gate(self,tx,owner,member_id,basis,body,command_id=None):
        reason=self._gate(tx,owner,member_id,basis,command_id=command_id,kind=body['kind'])
        if reason:return reason
        first=self.store._member(tx,owner,member_id)
        for mid in body.get('scope_members',[]):
            member=self.store._member(tx,owner,mid)
            if member['thread_id']!=first['thread_id']:return 'CROSS_COUNTERPARTY_COMMAND'
            if mid==member_id:continue
            other=body.get('scope_bases',{}).get(mid)
            if other is None:return 'MISSING_SCOPED_BASIS'
            reason=self._gate(tx,owner,mid,other,command_id=command_id,kind=body['kind'])
            if reason:return reason
        return None
    def _insert(self,tx,owner,member_id,basis,request_id,body,expires_at):
        member=self.store._member(tx,owner,member_id)
        digest=hashlib.sha256(encoded(body).encode()).hexdigest()
        command_id=new_id()
        tx.execute("INSERT INTO xj2_command(command_id,request_id,member_id,group_id,thread_id,state,content_hash,body_json,basis_json,result_json,cancel_requested,created_at,expires_at) VALUES(?,?,?,?,?,'QUEUED',?,?,?,'{}',0,?,?)",
                   (command_id,request_id,member_id,member['group_id'],member['thread_id'],digest,encoded(body),encoded(basis),self.store.clock(),expires_at))
        for mid in set(body.get('scope_members',[]))|{member_id}:
            tx.execute('INSERT INTO xj2_command_member(command_id,member_id) VALUES(?,?)',(command_id,mid))
            tx.execute('UPDATE xj2_peer SET pending_command_id=? WHERE member_id=?',(command_id,mid))
        return command_id
    def _require_reply_lease(self, tx, owner, member_id, request_id, job_id, token):
        """Fence asynchronous B work inside the transaction that writes its outcome."""
        row=tx.execute("""SELECT j.state,j.lease_token,j.lease_until FROM xj2_reply_job j
            JOIN xj2_peer p ON p.member_id=j.member_id JOIN xj2_group g ON g.group_id=p.group_id
            WHERE j.job_id=? AND j.member_id=? AND j.request_id=? AND g.owner_id=?""",
            (job_id,member_id,request_id,owner)).fetchone()
        if (not row or row['state']!='RUNNING' or row['lease_token']!=token
                or row['lease_until']<=self.store.clock()):
            raise StaleDecision('Reply job lease is expired, replaced, or outside this request')

    def propose(self, owner, member_id, *, basis, request_id, proposal, validator, expires_at,
                reply_job_id=None, reply_lease_token=None):
        """Transaction B. A server-supplied validator runs BEFORE the DB lock.
        A failure creates an inspectable decision record, not a rollback of
        transaction A. Tests may supply a synthetic validator; production must
        provide actual semantic/price/quantity/text validation before wiring.
        """
        identity(request_id, "request id")
        if reply_job_id is not None or reply_lease_token is not None:
            identity(reply_job_id, 'reply job id')
            identity(reply_lease_token, 'reply lease token')
        if not callable(validator) or not isinstance(basis, dict):
            raise InvalidFact("A server-side validator and fixed decision basis are required")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at) or expires_at <= self.store.clock():
            raise InvalidFact("Command expiry must be in the future")
        body, error = None, None
        try:
            body = self._body(validator(proposal))
        except Exception as exc:
            error = {"reason": "REPLY_VALIDATION_FAILED", "error_type": type(exc).__name__}
        with self.store.write(owner) as tx:
            member = self.store._member(tx, owner, member_id)
            if reply_job_id is not None:
                self._require_reply_lease(tx,owner,member_id,request_id,reply_job_id,reply_lease_token)
            digest = hashlib.sha256(encoded(body).encode()).hexdigest() if body else None
            previous = tx.execute("SELECT command_id,content_hash,basis_json,state FROM xj2_command WHERE member_id=? AND request_id=?", (member_id, request_id)).fetchone()
            if previous:
                if digest != previous["content_hash"] or encoded(basis) != previous["basis_json"]:
                    raise InvalidFact("Idempotent request changed its text or factual basis")
                return {"state": previous["state"], "command_id": previous["command_id"], "replayed": True}
            reason = self._scope_gate(tx, owner, member_id, basis, body) if body else None
            if error is None and reason:
                error = {"reason": reason}
            decision_id, command_id = new_id(), None
            if error is None:
                command_id=self._insert(tx,owner,member_id,basis,request_id,body,expires_at)
            result = {"state": "REJECTED" if error else "QUEUED", "decision_id": decision_id,
                      "command_id": command_id, "error": error}
            tx.execute("INSERT INTO xj2_decision(decision_id,member_id,basis_json,state,result_json,created_at,command_id) VALUES(?,?,?,?,?,?,?)",
                       (decision_id, member_id, encoded(basis), result["state"], encoded(result), self.store.clock(), command_id))
            return result
    def _cancel_unsubmitted(self, tx, row, reason):
        tx.execute("UPDATE xj2_command SET state='CANCELLED',result_json=? WHERE command_id=? AND state='QUEUED'", (encoded({"reason": reason}), row["command_id"]))
        tx.execute("UPDATE xj2_peer SET pending_command_id=NULL WHERE pending_command_id=?", (row["command_id"],))
    def claim(self, owner, command_id):
        with self.store.write(owner) as tx:
            row = self._command(tx, owner, command_id)
            if row["state"] != "QUEUED":
                return None
            if row["expires_at"] <= self.store.clock():
                self._cancel_unsubmitted(tx, row, "EXPIRED_BEFORE_DISPATCH")
                return None
            body, basis = json.loads(row["body_json"]), json.loads(row["basis_json"])
            reason = self._scope_gate(tx, owner, row["member_id"], basis, body, command_id=command_id)
            if reason == "WAITING_ANALYSIS":
                return None
            if reason:
                self._cancel_unsubmitted(tx, row, reason)
                return None
            busy = tx.execute("SELECT command_id FROM xj2_command WHERE thread_id=? AND state IN ('PENDING','UNKNOWN') AND command_id<>? LIMIT 1", (row["thread_id"], command_id)).fetchone()
            if busy:
                return None
            thread = self.store._thread(tx, owner, row["thread_id"])
            dispatch_version=thread['dispatch_version']+1
            tx.execute('UPDATE xj2_thread SET dispatch_version=? WHERE thread_id=?',(dispatch_version,row['thread_id']))
            result = {"dispatch_watermark": thread["ingest_revision"],"source_watermark":thread['source_watermark'],"dispatch_version":dispatch_version}
            tx.execute("UPDATE xj2_command SET state='PENDING',result_json=? WHERE command_id=?", (encoded(result), command_id))
            return {**row, "state": "PENDING", "body": body, "basis": basis,
                    "dispatch_watermark": thread["ingest_revision"], "source_watermark":thread["source_watermark"], "dispatch_version":dispatch_version,
                    "account_uid": thread["account_uid"], "peer_uid": thread["peer_uid"]}
    def execution_permission(self, owner, command_id):
        """Host calls immediately before send; no false cancelled receipt on uncertainty."""
        with self.store.write(owner) as tx:
            row = self._command(tx, owner, command_id)
            if row["state"] != "PENDING" or row["cancel_requested"]:
                return False
            reason = self._scope_gate(tx, owner, row["member_id"], json.loads(row["basis_json"]), json.loads(row["body_json"]), command_id=command_id)
            if reason or row["expires_at"] <= self.store.clock():
                tx.execute("UPDATE xj2_command SET cancel_requested=1 WHERE command_id=?", (command_id,))
                return False
            return True

    def _record_native_send(self, tx, owner, row, native_id, occurred_at):
        """Only an actual native receipt can become 'our last sent quote'."""
        thread = self.store._thread(tx, owner, row["thread_id"])
        member = self.store._member(tx, owner, row["member_id"])
        group = self.store._group(tx, owner, row["group_id"])
        body, basis = json.loads(row["body_json"]), json.loads(row["basis_json"])
        existing = tx.execute("SELECT message_id,text FROM xj2_message WHERE thread_id=? AND direction='out' AND native_id=?", (row["thread_id"], native_id)).fetchone()
        if existing and existing["text"] != body["text"]:
            raise InvalidFact("Native ID collides with a different sent text")
        if existing:
            message_id = existing["message_id"]
        else:
            message_id = "out_" + row["command_id"]
            revision = thread["ingest_revision"] + 1
            tx.execute("INSERT INTO xj2_message(message_id,thread_id,event_id,content_hash,source_seq,ingest_revision,direction,kind,text,native_id,occurred_at,received_at,trade_day,historical) VALUES(?,?,?,?,?,?,'out','message',?,?,?,?,?,0)",
                       (message_id, row["thread_id"], "command:" + row["command_id"], row["content_hash"], -1, revision,
                        body["text"], native_id, occurred_at, self.store.clock(), basis["trade_day"]))
            first = thread["pending_since"] if thread["pending_since"] is not None else self.store.clock()
            tx.execute("UPDATE xj2_thread SET ingest_revision=?,pending_since=?,last_ingest_at=? WHERE thread_id=?", (revision, first, self.store.clock(), row["thread_id"]))
        for mid in set(body.get('scope_members',[]))|{row['member_id']}:
            member=self.store._member(tx,owner,mid)
            group=self.store._group(tx,owner,member['group_id'])
            if body['kind']!='MANUAL':
                linked=tx.execute("SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?",(message_id,mid)).fetchone()
                if not linked:tx.execute("INSERT INTO xj2_scope(message_id,member_id,relation) VALUES(?,?,'MENTION')",(message_id,mid))
            price=body.get('scope_prices',{}).get(mid) or (body.get('our_price') if mid==row['member_id'] else None)
            actual=json.loads(member['our_quote_json'])
            if price and occurred_at>=actual.get('sent_at',-1):
                actual={'price':price,'command_id':row['command_id'],'message_id':message_id,'native_id':native_id,'sent_at':occurred_at}
                tx.execute('UPDATE xj2_peer SET our_quote_json=?,version=version+1 WHERE member_id=?',(encoded(actual),mid))
            if body['kind']!='SOCIAL' and (member['waiting_since'] is None or occurred_at>=member['waiting_since']):
                tx.execute("UPDATE xj2_peer SET waiting_command_id=?,waiting_since=?,response_state='WAITING' WHERE member_id=?",(row['command_id'],occurred_at,mid))
            if body['kind']=='RECONFIRM' and basis['trade_day']==group['trade_day'] and member['reconfirm_day']==group['trade_day']:
                tx.execute('UPDATE xj2_peer SET reconfirm_command_id=? WHERE member_id=?',(row['command_id'],mid))
    def receipt(self, owner, command_id, *, account_uid, peer_uid, content_hash,
                status, evidence, native_id=None, occurred_at=None):
        """A native result is evidence, never permission to unpause automation."""
        if status not in ("SENT", "FAILED", "CANCELLED", "UNKNOWN"):
            raise InvalidFact("Unsupported delivery result")
        identity(evidence, "delivery evidence", 2000)
        if status == "SENT":
            identity(native_id, "native message id")
        stamp = self.store.clock() if occurred_at is None else occurred_at
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise InvalidFact("Invalid native send timestamp")
        with self.store.write(owner) as tx:
            row = self._command(tx, owner, command_id)
            thread = self.store._thread(tx, owner, row["thread_id"])
            if (thread["account_uid"], thread["peer_uid"], row["content_hash"]) != (account_uid, peer_uid, content_hash):
                raise InvalidFact("Native receipt identity/content mismatch")
            result = json.loads(row["result_json"])
            receipt = {"status": status, "evidence": evidence, "native_id": native_id}
            receipts = list(result.get("receipts", []))
            if receipt in receipts:
                return row["state"]
            native_conflict = status == "SENT" and row["native_id"] is not None and row["native_id"] != native_id
            if status == "SENT" and row["native_id"] is None:
                self._record_native_send(tx, owner, row, native_id, stamp)
                tx.execute("UPDATE xj2_command SET native_id=?,sent_at=? WHERE command_id=?", (native_id, stamp, command_id))
            terminal = {"SENT", "FAILED", "CANCELLED"}
            known = set(result.get("terminal_statuses", []))
            if row["state"] in terminal:
                known.add(row["state"])
            if status in terminal:
                known.add(status)
            conflict = bool(result.get("conflict")) or len(known) > 1 or native_conflict
            if conflict:
                final = "UNKNOWN"
            elif row["state"] in terminal and status == "UNKNOWN":
                final = row["state"]
            else:
                final = status
            receipts.append(receipt)
            result.update({"receipts": receipts[-64:], "terminal_statuses": sorted(known),
                           "conflict": conflict, "native_id_conflict": bool(result.get("native_id_conflict")) or native_conflict})
            tx.execute("UPDATE xj2_command SET state=?,result_json=? WHERE command_id=?", (final, encoded(result), command_id))
            if final in terminal:
                tx.execute("UPDATE xj2_peer SET pending_command_id=NULL WHERE pending_command_id=?", (command_id,))
            if conflict:
                tx.execute("UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE member_id=?", (row["member_id"],))
                self.store._cancel_commands(tx, member_id=row["member_id"], reason="CONFLICTING_NATIVE_EVIDENCE")
            return final
    def mark_unresponsive(self, owner):
        """One hour since an actual outbound question; never infers finality."""
        changed = []
        with self.store.write(owner) as tx:
            rows = tx.execute("SELECT p.member_id,p.thread_id,p.waiting_since FROM xj2_peer p JOIN xj2_group g ON g.group_id=p.group_id WHERE g.owner_id=? AND p.response_state='WAITING' AND p.waiting_since<=?", (owner, self.store.clock() - 3600)).fetchall()
            for row in rows:
                thread = self.store._thread(tx, owner, row["thread_id"])
                if thread["analyzed_revision"] != thread["ingest_revision"]:
                    continue
                tx.execute("UPDATE xj2_peer SET response_state='UNRESPONSIVE' WHERE member_id=? AND response_state='WAITING'", (row["member_id"],))
                changed.append(row["member_id"])
        return changed
