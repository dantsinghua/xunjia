"""Durable V2 quote facts. No LLM/network/native I/O inside transactions.
All writers take a short per-owner row lock before touching threads/groups.
This deliberately favors simple, deterministic ordering for a small trader
workbench over a distributed lock service. The legacy schema is never touched.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import math
import time
import uuid
from ..db import Database
from .facts import Quote, apply_patch, group_summary
from .prices import InvalidFact, LimitPolicy, Price, StaleDecision, Valuation, iso_day, number
from .schema import statements
from .settings import Aggregation, Settings
def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
def identity(value, label="identifier", size=160):
    if not isinstance(value, str) or not value.strip() or len(value) > size or "\x00" in value:
        raise InvalidFact("Invalid " + label)
    return value
def new_id():
    return uuid.uuid4().hex
class Store:
    def __init__(self, url, *, clock=time.time, settings=None, initialize=False):
        self.db = Database(url)
        self.clock = clock
        self.settings = settings or Settings()
        if initialize:
            self.initialize()
    def initialize(self):
        """Explicit new-schema provisioning; never called by legacy startup."""
        self.db.initialize(statements(mysql=self.db.mysql))
    @contextmanager
    def write(self, owner):
        identity(owner, "owner")
        with self.db.transaction() as tx:
            query = ("INSERT IGNORE INTO xj2_owner(owner_id,control_version,send_allowed) VALUES(?,1,1)"
                     if tx.mysql else
                     "INSERT INTO xj2_owner(owner_id,control_version,send_allowed) VALUES(?,1,1) ON CONFLICT(owner_id) DO NOTHING")
            tx.execute(query, (owner,))
            lock = " FOR UPDATE" if tx.mysql else ""
            tx.execute("SELECT owner_id FROM xj2_owner WHERE owner_id=?" + lock, (owner,)).fetchone()
            yield tx
            tx.execute("UPDATE xj2_owner SET projection_version=projection_version+1 WHERE owner_id=?", (owner,))
    def _group(self, tx, owner, group_id):
        row = tx.execute("SELECT * FROM xj2_group WHERE group_id=? AND owner_id=?", (group_id, owner)).fetchone()
        if row is None:
            raise InvalidFact("Unknown group for this owner")
        return dict(row)
    def _thread(self, tx, owner, thread_id):
        row = tx.execute("SELECT * FROM xj2_thread WHERE thread_id=? AND owner_id=?", (thread_id, owner)).fetchone()
        if row is None:
            raise InvalidFact("Unknown counterparty thread for this owner")
        return dict(row)
    def _member(self, tx, owner, member_id):
        row = tx.execute("SELECT p.* FROM xj2_peer p JOIN xj2_group g ON g.group_id=p.group_id WHERE p.member_id=? AND g.owner_id=?", (member_id, owner)).fetchone()
        if row is None:
            raise InvalidFact("Unknown bond/counterparty member")
        return dict(row)
    def _quote(self, tx, member_id, side):
        if side not in ("bid", "ofr"):
            raise InvalidFact("Quote side must be bid or ofr")
        row = tx.execute("SELECT state_json FROM xj2_quote WHERE member_id=? AND side=? ORDER BY revision DESC LIMIT 1", (member_id, side)).fetchone()
        return Quote.load(json.loads(row["state_json"])) if row else Quote(side)
    def _save_quote(self, tx, member_id, quote, analysis_id):
        tx.execute("INSERT INTO xj2_quote(quote_id,member_id,side,revision,analysis_id,state_json,created_at) VALUES(?,?,?,?,?,?,?)",
                   (new_id(), member_id, quote.side, quote.revision, analysis_id, encoded(quote.dump()), self.clock()))
    def _evidence_dependents(self, tx, thread_id, message_id):
        # A later round can cite an earlier round's actual chat without copying
        # its scope. Discover dependencies from current field evidence itself.
        members = tx.execute('SELECT * FROM xj2_peer WHERE thread_id=?', (thread_id,)).fetchall()
        return [member for member in members if any(
            message_id in refs for side in ('bid', 'ofr')
            for refs in self._quote(tx, member['member_id'], side).evidence.values())]
    def _recompute_evidence(self, tx, member_id, *, reason, withdrawn=False):
        """Replay affected fields from still-valid evidence, never add quantities.

        Full snapshots retain field references, so a revoked size correction
        can recover the earlier size without erasing an unrelated valid price.
        """
        changed = False
        through = tx.execute('SELECT t.ingest_revision FROM xj2_thread t JOIN xj2_peer p ON p.thread_id=t.thread_id WHERE p.member_id=?',
                             (member_id,)).fetchone()['ingest_revision']
        def valid(refs):
            if not refs:
                return False
            for ref in refs:
                scope = self._history_message(tx, member_id, ref, through, allow_invalid=True)
                replaced = tx.execute("SELECT message_id FROM xj2_message WHERE target_id=? AND historical=0 LIMIT 1", (ref,)).fetchone()
                if not scope or replaced:
                    return False
            return True
        for side in ('bid', 'ofr'):
            previous = self._quote(tx, member_id, side)
            invalid = [field for field, refs in previous.evidence.items() if not valid(refs)]
            if not invalid:
                continue
            history = [Quote.load(json.loads(r['state_json'])).dump() for r in tx.execute(
                'SELECT state_json FROM xj2_quote WHERE member_id=? AND side=? ORDER BY revision DESC', (member_id, side)).fetchall()]
            data = previous.dump(); defaults = Quote(side).dump()
            for field in invalid:
                found = next((q for q in history if valid(q['evidence'].get(field, []))), None)
                data[field] = found[field] if found else defaults[field]
                if found:
                    data['evidence'][field] = found['evidence'][field]
                else:
                    data['evidence'].pop(field, None)
            if any(data[k] != previous.dump()[k] for k in ('price', 'volume_wan', 'assertion')):
                data['finality'] = 'UNKNOWN'; data['evidence'].pop('finality', None)
            if data['price'] is None:
                data.update(validity='WITHDRAWN' if withdrawn else 'UNCONFIRMED', confirmed_day=None, finality='UNKNOWN')
            data['revision'] = previous.revision + 1
            self._save_quote(tx, member_id, Quote.load(data), reason)
            changed = True
        return changed

    def _correct_association(self, tx, owner, thread_id, message_id, member_ids, reason):
        if not isinstance(member_ids, list) or len(member_ids) > 100 or len(set(member_ids)) != len(member_ids):
            raise InvalidFact('Correction needs unique member IDs')
        original = tx.execute('SELECT * FROM xj2_message WHERE message_id=? AND thread_id=?', (message_id, thread_id)).fetchone()
        if not original:
            raise InvalidFact('Correction target is outside this thread')
        for mid in member_ids:
            if self._member(tx, owner, mid)['thread_id'] != thread_id:
                raise InvalidFact('Correction cannot cross counterparty threads')
        old = {r['member_id']: r['relation'] for r in tx.execute('SELECT * FROM xj2_scope WHERE message_id=?', (message_id,)).fetchall()}
        for mid in old:
            if mid not in member_ids:
                tx.execute("UPDATE xj2_scope SET relation='REMOVED' WHERE message_id=? AND member_id=?", (message_id, mid))
        for mid in member_ids:
            if mid in old:
                tx.execute("UPDATE xj2_scope SET relation='MENTION' WHERE message_id=? AND member_id=?", (message_id, mid))
            else:
                tx.execute("INSERT INTO xj2_scope(message_id,member_id,relation) VALUES(?,?,'MENTION')", (message_id, mid))
        groups, touched = [], set()
        for mid in set(old) | set(member_ids):
            if old.get(mid) == ('MENTION' if mid in member_ids else 'REMOVED'):
                continue
            member = self._member(tx, owner, mid)
            self._recompute_evidence(tx, mid, reason=reason)
            tx.execute('UPDATE xj2_peer SET version=version+1 WHERE member_id=?', (mid,))
            self._cancel_commands(tx, member_id=mid, reason='ASSOCIATION_CORRECTED')
            groups.append(member['group_id'])
            touched.add(mid)
        for member in self._evidence_dependents(tx, thread_id, message_id):
            mid = member['member_id']
            if mid not in touched and self._recompute_evidence(tx, mid, reason=reason):
                tx.execute('UPDATE xj2_peer SET version=version+1 WHERE member_id=?', (mid,))
                self._cancel_commands(tx, member_id=mid, reason='ASSOCIATION_CORRECTED')
                groups.append(member['group_id'])
        self._refresh_groups(tx, owner, groups)
        tx.execute('INSERT INTO xj2_audit(audit_id,owner_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)',
                   (new_id(),owner,'association_corrected',message_id,self.clock(),encoded({'previous':old,'members':member_ids,'reason':reason})))

    def correct_association(self, owner, thread_id, message_id, member_ids, expected_revision, reason):
        identity(reason, 'correction reason', 1000)
        with self.write(owner) as tx:
            self._thread(tx, owner, thread_id)
            current = tx.execute('SELECT projection_version FROM xj2_owner WHERE owner_id=?', (owner,)).fetchone()['projection_version']
            if type(expected_revision) is not int or current != expected_revision:
                raise InvalidFact('Projection changed; refresh before correcting association')
            self._correct_association(tx, owner, thread_id, message_id, member_ids, 'human:'+reason)
        return {'message_id':message_id, 'member_ids':member_ids, 'facts_recomputed':True}
    def create_group(self, owner, *, bond_code, our_side, target_volume, trade_day,
                     policy: LimitPolicy, valuation: Valuation | None = None,
                     expected_valuation_date=None, send_allowed=False):
        identity(bond_code, "bond code", 32)
        iso_day(trade_day)
        if our_side not in ("BUY", "SELL") or type(send_allowed) is not bool:
            raise InvalidFact("Explicit group direction and sending choice required")
        target = number(target_volume, "target volume")
        if target <= 0 or not isinstance(policy, LimitPolicy):
            raise InvalidFact("Positive target quantity and explicit limit policy required")
        boundary = policy.resolve(bond_code=bond_code, expected_valuation_date=expected_valuation_date, valuation=valuation)
        with self.write(owner) as tx:
            return self._create_group(tx, owner, bond_code=bond_code, our_side=our_side,
                target_volume=format(target, "f"), trade_day=trade_day, policy=policy,
                boundary=boundary, valuation=valuation, send_allowed=send_allowed)
    def _create_group(self, tx, owner, *, bond_code, our_side, target_volume, trade_day,
                      policy, boundary, valuation=None, send_allowed=False, plan=None):
        group_id = new_id()
        tx.execute("INSERT INTO xj2_group(group_id,owner_id,bond_code,our_side,target_volume,trade_day,policy_json,boundary_json,valuation_json,version,policy_version,control_version,send_allowed,state,review_needed,handoff_json,plan_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,1,1,?,'OPEN',0,'{}',?,?)",
                   (group_id,owner,bond_code,our_side,target_volume,trade_day,
                    encoded(asdict(policy)),encoded(boundary),encoded(asdict(valuation) if valuation else None),
                    int(send_allowed),encoded(plan or {}),self.clock()))
        return group_id
    def add_member(self, owner, group_id, *, account_uid, peer_uid):
        identity(account_uid, "account", 80)
        identity(peer_uid, "counterparty")
        with self.write(owner) as tx:
            return self._add_member(tx,owner,group_id,account_uid=account_uid,peer_uid=peer_uid)
    def _add_member(self,tx,owner,group_id,*,account_uid,peer_uid):
        self._group(tx, owner, group_id)
        thread = tx.execute("SELECT thread_id FROM xj2_thread WHERE owner_id=? AND account_uid=? AND peer_uid=?", (owner, account_uid, peer_uid)).fetchone()
        if thread:
            tid = thread["thread_id"]
        else:
            tid = new_id()
            tx.execute("INSERT INTO xj2_thread(thread_id,owner_id,account_uid,peer_uid,ingest_revision,analyzed_revision) VALUES(?,?,?,?,0,0)", (tid, owner, account_uid, peer_uid))
        existing = tx.execute("SELECT member_id FROM xj2_peer WHERE group_id=? AND thread_id=?", (group_id, tid)).fetchone()
        if existing:
            return existing["member_id"], tid
        mid = new_id()
        tx.execute("INSERT INTO xj2_peer(member_id,group_id,thread_id,version,control_version,send_allowed,response_state,our_quote_json) VALUES(?,?,?,0,1,1,'NONE','{}')", (mid, group_id, tid))
        return mid, tid
    def ingest(self, owner, thread_id, *, event_id, source_seq, text, trade_day,
               occurred_at=None, direction="in", historical=False, native_id=None, kind="message", target_event_id=None):
        """Commit raw text immediately, independently of model/command state."""
        identity(event_id, "source event")
        if kind not in ("message", "edit", "retract"):
            raise InvalidFact("Invalid message event kind")
        if kind != "message":identity(target_event_id, "target event")
        iso_day(trade_day)
        if type(source_seq) is not int or source_seq < 0:
            raise InvalidFact("Invalid source sequence")
        if not isinstance(text, str) or len(text) > 20000 or type(historical) is not bool:
            raise InvalidFact("Invalid raw message")
        if direction not in ("in", "out"):
            raise InvalidFact("Invalid message direction")
        if native_id is not None:
            identity(native_id, "native id")
        stamp = self.clock() if occurred_at is None else occurred_at
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise InvalidFact("Invalid message timestamp")
        payload = dict(event_id=event_id, source_seq=source_seq, text=text, trade_day=trade_day,
                       occurred_at=occurred_at, direction=direction, historical=historical, native_id=native_id, kind=kind,target_event_id=target_event_id)
        digest = hashlib.sha256(encoded(payload).encode()).hexdigest()
        with self.write(owner) as tx:
            thread = self._thread(tx, owner, thread_id)
            previous = tx.execute("SELECT message_id,content_hash FROM xj2_message WHERE thread_id=? AND event_id=?", (thread_id, event_id)).fetchone()
            if previous:
                if previous["content_hash"] != digest:
                    raise InvalidFact("Replayed event ID has different native content")
                return previous["message_id"]
            if direction == "out" and native_id:
                echo = tx.execute("SELECT message_id,text FROM xj2_message WHERE thread_id=? AND direction='out' AND native_id=?", (thread_id, native_id)).fetchone()
                if echo:
                    if echo["text"] != text:
                        raise InvalidFact("Native echo conflicts with the confirmed sent text")
                    return echo["message_id"]
            target_id=None
            if target_event_id:
                target=tx.execute("SELECT message_id FROM xj2_message WHERE thread_id=? AND event_id=?",(thread_id,target_event_id)).fetchone()
                if not target:raise InvalidFact("Correction target is not in this counterparty thread")
                target_id=target['message_id']
            message_id = new_id()
            revision = thread["ingest_revision"] + 1
            tx.execute("INSERT INTO xj2_message(message_id,thread_id,event_id,content_hash,source_seq,ingest_revision,direction,kind,text,native_id,occurred_at,received_at,trade_day,historical,target_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (message_id, thread_id, event_id, digest, source_seq, revision, direction, kind, text,
                        native_id, stamp, self.clock(), trade_day, int(historical),target_id))
            first = thread["pending_since"] if thread["pending_since"] is not None else self.clock()
            tx.execute("UPDATE xj2_thread SET ingest_revision=?,pending_since=?,last_ingest_at=? WHERE thread_id=?", (revision, first, self.clock(), thread_id))
            if direction == 'in' and not historical:
                tx.execute("UPDATE xj2_thread SET source_watermark=CASE WHEN source_watermark<? THEN ? ELSE source_watermark END WHERE thread_id=?",(source_seq,source_seq,thread_id))
            if target_id and not historical:
                touched=[]
                members=self._evidence_dependents(tx,thread_id,target_id)
                for member in members:
                    changed=self._recompute_evidence(tx,member['member_id'],reason='correction-'+message_id,withdrawn=kind=='retract')
                    if changed:
                        tx.execute('UPDATE xj2_peer SET version=version+1 WHERE member_id=?',(member['member_id'],))
                        touched.append(member['group_id'])
                        self._cancel_commands(tx,member_id=member['member_id'],reason='SOURCE_CORRECTED')
                self._refresh_groups(tx,owner,touched)
            return message_id
    def messages(self, owner, thread_id, *, after_revision=0, limit=200):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InvalidFact("Raw message page size must be 1..500")
        with self.db.transaction(write=False) as tx:
            self._thread(tx, owner, thread_id)
            return [dict(row) for row in tx.execute("SELECT * FROM xj2_message WHERE thread_id=? AND ingest_revision>? ORDER BY ingest_revision LIMIT ?", (thread_id, after_revision, limit)).fetchall()]
    def quote(self, owner, member_id, side):
        with self.db.transaction(write=False) as tx:
            self._member(tx, owner, member_id)
            return self._quote(tx, member_id, side)
    def _summary(self, tx, group):
        members = tx.execute("SELECT member_id FROM xj2_peer WHERE group_id=? ORDER BY member_id", (group["group_id"],)).fetchall()
        quotes=[]
        for member in members:
            mid=member['member_id']
            blocked=tx.execute("SELECT 1 FROM xj2_scope WHERE member_id=? AND relation IN ('INVALID','UNRESOLVED') LIMIT 1",(mid,)).fetchone()
            for side in ('bid','ofr'):
                quote=self._quote(tx,mid,side)
                if blocked:quote=replace(quote,validity='UNCONFIRMED')
                quotes.append((mid,quote))
        return group_summary(our_side=group["our_side"], target_volume=group["target_volume"],
                             trade_day=group["trade_day"], boundary=json.loads(group["boundary_json"]), quotes=quotes)
    def group(self, owner, group_id):
        with self.db.transaction(write=False) as tx:
            group = self._group(tx, owner, group_id)
            return {**group, "summary": self._summary(tx, group)}
    def _cancel_commands(self, tx, *, group_id=None, member_id=None, owner=None, reason):
        if group_id:
            condition, values = '(c.group_id=? OR participant.group_id=?)', (group_id,group_id)
        elif member_id:
            condition, values = '(c.member_id=? OR cm.member_id=?)', (member_id,member_id)
        else:
            condition, values = 'g.owner_id=?', (owner,)
        rows=tx.execute("SELECT DISTINCT c.* FROM xj2_command c JOIN xj2_group g ON c.group_id=g.group_id LEFT JOIN xj2_command_member cm ON cm.command_id=c.command_id LEFT JOIN xj2_peer participant ON participant.member_id=cm.member_id WHERE "+condition+" AND c.state IN ('QUEUED','PENDING','UNKNOWN')",values).fetchall()
        for row in rows:
            if row["state"] == "QUEUED":
                tx.execute("UPDATE xj2_command SET state='CANCELLED',result_json=? WHERE command_id=?", (encoded({"reason": reason}), row["command_id"]))
                tx.execute("UPDATE xj2_peer SET pending_command_id=NULL WHERE pending_command_id=?", (row["command_id"],))
            else:
                tx.execute("UPDATE xj2_command SET cancel_requested=1 WHERE command_id=?", (row["command_id"],))
    def set_sending(self, owner, allowed, *, group_id=None, member_id=None):
        """Sending controls never erase raw messages or disable analysis jobs."""
        if type(allowed) is not bool or (group_id and member_id):
            raise InvalidFact("Choose exactly one sending scope")
        with self.write(owner) as tx:
            if member_id:
                self._member(tx, owner, member_id)
                tx.execute("UPDATE xj2_peer SET send_allowed=?,control_version=control_version+1 WHERE member_id=?", (int(allowed), member_id))
            elif group_id:
                group = self._group(tx, owner, group_id)
                if allowed and group["state"] == "HANDOFF":
                    raise InvalidFact("Completed handoff needs an explicit new round, not a toggle")
                tx.execute("UPDATE xj2_group SET send_allowed=?,control_version=control_version+1 WHERE group_id=?", (int(allowed), group_id))
            else:
                tx.execute("UPDATE xj2_owner SET send_allowed=?,control_version=control_version+1 WHERE owner_id=?", (int(allowed), owner))
            if not allowed:
                self._cancel_commands(tx, group_id=group_id, member_id=member_id, owner=owner, reason="sending_paused")
    def _refresh_groups(self, tx, owner, group_ids):
        for gid in sorted(set(group_ids)):
            group = self._group(tx, owner, gid)
            summary = self._summary(tx, group)
            if group["state"] == "HANDOFF":
                tx.execute("UPDATE xj2_group SET version=version+1,review_needed=1 WHERE group_id=?", (gid,))
            elif summary["goal_ready"]:
                tx.execute("UPDATE xj2_group SET version=version+1,state='HANDOFF',send_allowed=0,control_version=control_version+1,handoff_json=?,review_needed=0 WHERE group_id=?", (encoded(summary), gid))
                self._cancel_commands(tx, group_id=gid, reason="group_goal_handoff")
            else:
                tx.execute("UPDATE xj2_group SET version=version+1 WHERE group_id=?", (gid,))

    def _context(self, tx, member_id, through_revision):
        # A new authorization round does not erase actual chat history. Scope
        # history by owner + physical thread + bond, while facts stay per round.
        rows = tx.execute("""SELECT m.* FROM xj2_message m
            JOIN xj2_peer cp ON cp.member_id=? JOIN xj2_group cg ON cg.group_id=cp.group_id
            WHERE m.thread_id=cp.thread_id AND m.ingest_revision<=?
            AND (m.direction='in' OR m.native_id IS NOT NULL)
            AND EXISTS (SELECT 1 FROM xj2_scope s JOIN xj2_peer hp ON hp.member_id=s.member_id
                JOIN xj2_group hg ON hg.group_id=hp.group_id WHERE s.message_id=m.message_id
                AND hp.thread_id=cp.thread_id AND hg.owner_id=cg.owner_id AND hg.bond_code=cg.bond_code
                AND s.relation IN ('MENTION','UNRESOLVED','INVALID'))
            ORDER BY m.occurred_at DESC,m.ingest_revision DESC LIMIT ?""",
                          (member_id, through_revision, self.settings.context_messages)).fetchall()
        return [dict(row) for row in reversed(rows)]
    def _history_message(self, tx, member_id, message_id, through_revision, *, allow_invalid=False):
        return tx.execute("""SELECT m.* FROM xj2_message m
            JOIN xj2_peer cp ON cp.member_id=? JOIN xj2_group cg ON cg.group_id=cp.group_id
            WHERE m.message_id=? AND m.thread_id=cp.thread_id AND m.ingest_revision<=?
            AND (m.direction='in' OR m.native_id IS NOT NULL)
            AND EXISTS (SELECT 1 FROM xj2_scope s JOIN xj2_peer hp ON hp.member_id=s.member_id
                JOIN xj2_group hg ON hg.group_id=hp.group_id WHERE s.message_id=m.message_id
                AND hp.thread_id=cp.thread_id AND hg.owner_id=cg.owner_id AND hg.bond_code=cg.bond_code
                AND (s.relation='MENTION' OR (?=1 AND s.relation='INVALID')))""",
                          (member_id,message_id,through_revision,int(allow_invalid))).fetchone()
    def context(self, owner, member_id, *, anchors=(), through_revision=None):
        """One global last-N window; explicit references are added, not guessed."""
        with self.db.transaction(write=False) as tx:
            member = self._member(tx, owner, member_id)
            thread = self._thread(tx, owner, member["thread_id"])
            through=thread['ingest_revision'] if through_revision is None else through_revision
            if type(through) is not int or not 0<=through<=thread['ingest_revision']:
                raise InvalidFact('Invalid fixed history watermark')
            rows = self._context(tx, member_id, through)
            seen = {m["message_id"] for m in rows}
            if len(anchors) > 30:
                raise InvalidFact("Too many explicit context anchors")
            for anchor in anchors:
                row = self._history_message(tx,member_id,anchor,through)
                if not row:
                    raise InvalidFact("Context anchor belongs to a different bond/counterparty or lacks native send evidence")
                if anchor not in seen:
                    rows.append(dict(row)); seen.add(anchor)
            return sorted(rows, key=lambda r: (r["occurred_at"], r["ingest_revision"]))
    def claim_analysis(self, owner, thread_id, *, aggregation: Aggregation | None = None,
                       force=False, lease_seconds=60):
        """A manual explicit force is for tests/recovery, not a latency default.
        Lease recovery reuses the SAME immutable raw-message snapshot. A new
        intake is never skipped when an older model request finishes late.
        """
        if type(force) is not bool or type(lease_seconds) is not int or not 1 <= lease_seconds <= 600:
            raise InvalidFact("Invalid analysis execution options")
        if aggregation is None and not force:
            raise InvalidFact("No measured aggregation policy; automatic scheduling is not configured")
        now = self.clock()
        with self.write(owner) as tx:
            thread = self._thread(tx, owner, thread_id)
            if thread["analyzed_revision"] >= thread["ingest_revision"]:
                return None
            old = tx.execute("SELECT * FROM xj2_analysis WHERE thread_id=? AND state IN ('RUNNING','RETRY') ORDER BY through_revision LIMIT 1", (thread_id,)).fetchone()
            if old:
                if old["lease_until"] > now:
                    return None
                token = new_id()
                tx.execute("UPDATE xj2_analysis SET state='RUNNING',lease_token=?,lease_until=?,attempts=attempts+1 WHERE analysis_id=?", (token, now + lease_seconds, old["analysis_id"]))
                return {"analysis_id": old["analysis_id"], "lease_token": token,
                        "snapshot": json.loads(old["snapshot_json"]), "attempt": old["attempts"] + 1}
            if not force and not aggregation.ready(now=now, first_pending=thread["pending_since"], last_received=thread["last_ingest_at"]):
                return None
            raw = [dict(r) for r in tx.execute("SELECT * FROM xj2_message WHERE thread_id=? AND ingest_revision>? ORDER BY ingest_revision LIMIT 100", (thread_id, thread["analyzed_revision"])).fetchall()]
            through = raw[-1]["ingest_revision"]
            peers = [dict(r) for r in tx.execute("""SELECT p.*,g.bond_code,g.trade_day,g.plan_json,g.our_side
                FROM xj2_peer p JOIN xj2_group g ON g.group_id=p.group_id
                WHERE p.thread_id=? AND g.archived=0 AND NOT EXISTS (
                    SELECT 1 FROM xj2_peer np JOIN xj2_group ng ON ng.group_id=np.group_id
                    WHERE np.thread_id=p.thread_id AND ng.owner_id=g.owner_id AND ng.bond_code=g.bond_code
                    AND ng.archived=0 AND ((ng.state='OPEN' AND g.state<>'OPEN') OR
                        (ng.state=g.state AND (ng.created_at>g.created_at OR
                            (ng.created_at=g.created_at AND ng.group_id>g.group_id)))))
                ORDER BY p.member_id""", (thread_id,)).fetchall()]
            for peer in peers:
                peer["plan"]=json.loads(peer.pop("plan_json"))
                peer["context"] = self._context(tx, peer["member_id"], thread["analyzed_revision"])
                peer["quotes"] = {side: self._quote(tx, peer["member_id"], side).dump() for side in ("bid", "ofr")}
            snapshot = {"thread_id": thread_id, "from_revision": thread["analyzed_revision"],
                        "through_revision": through, "messages": raw, "members": peers,
                        "context_messages": self.settings.context_messages}
            analysis_id, token = new_id(), new_id()
            tx.execute("INSERT INTO xj2_analysis(analysis_id,thread_id,from_revision,through_revision,state,lease_token,lease_until,attempts,snapshot_json,result_json,created_at) VALUES(?,?,?,?,'RUNNING',?,?,1,?,'{}',?)",
                       (analysis_id, thread_id, thread["analyzed_revision"], through, token, now + lease_seconds, encoded(snapshot), now))
            return {"analysis_id": analysis_id, "lease_token": token, "snapshot": snapshot, "attempt": 1}
    def _analysis(self, tx, owner, analysis_id, token):
        row = tx.execute("SELECT a.* FROM xj2_analysis a JOIN xj2_thread t ON t.thread_id=a.thread_id WHERE a.analysis_id=? AND t.owner_id=?", (analysis_id, owner)).fetchone()
        if not row or row["lease_token"] != token:
            raise StaleDecision("Analysis lease was replaced or belongs to another thread")
        if row["state"] != "COMMITTED" and (row["state"] != "RUNNING" or row["lease_until"] <= self.clock()):
            raise StaleDecision("Analysis lease is not active")
        return dict(row)
    def fail_analysis(self, owner, analysis_id, token, *, reason, retry_after_seconds=1):
        if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
            raise InvalidFact("A bounded failure reason is required")
        if type(retry_after_seconds) is not int or not 0 <= retry_after_seconds <= 600:
            raise InvalidFact("Invalid retry delay")
        with self.write(owner) as tx:
            row = self._analysis(tx, owner, analysis_id, token)
            if row["state"] == "COMMITTED":
                raise StaleDecision("Cannot fail an already committed analysis")
            tx.execute("UPDATE xj2_analysis SET state='RETRY',lease_until=?,result_json=? WHERE analysis_id=?",
                       (self.clock() + retry_after_seconds, encoded({"failure": reason}), analysis_id))
    def _evidence(self, tx, member, analysis, changes, evidence, previous):
        for key, refs in evidence.items():
            messages = []
            for ref in refs:
                row = self._history_message(tx,member['member_id'],ref,analysis['through_revision'])
                if row is None:
                    raise InvalidFact("Field evidence is not scoped to this bond/counterparty snapshot")
                messages.append(dict(row))
            fresh = [m for m in messages if m["direction"] == "in" and not m["historical"] and m["ingest_revision"] > analysis["from_revision"]]
            for message in messages:
                # A frozen analysis can use older input, but it cannot revive a
                # source already edited or withdrawn while the model was running.
                newer=tx.execute("SELECT message_id FROM xj2_message WHERE target_id=? AND historical=0 LIMIT 1",(message['message_id'],)).fetchone()
                if newer:raise InvalidFact('Evidence has been edited or withdrawn')
            if not fresh:
                raise InvalidFact("Current counterpart fields require a new inbound message, not history or our draft")
            if key == "confirmed_day":
                day = changes[key]
                group = tx.execute("SELECT trade_day FROM xj2_group WHERE group_id=?", (member["group_id"],)).fetchone()
                if day is not None and (day != group["trade_day"] or not any(m["trade_day"] == day for m in fresh)):
                    raise InvalidFact("Quote confirmation must match this member's current trading date")
                if day is not None and member["reconfirm_day"]:
                    command = tx.execute("SELECT * FROM xj2_command WHERE command_id=? AND member_id=? AND state='SENT'", (member["reconfirm_command_id"], member["member_id"])).fetchone()
                    if not command or not any(m["received_at"] >= command["sent_at"] for m in fresh):
                        raise InvalidFact("Cross-day confirmation requires an actually sent reconfirmation question and a new reply")
            previous_sequence = -1
            for ref in previous.evidence.get(key, []):
                old = tx.execute("SELECT source_seq FROM xj2_message WHERE message_id=? AND thread_id=?", (ref, member["thread_id"])).fetchone()
                if old:
                    previous_sequence = max(previous_sequence, old["source_seq"])
            if max(m["source_seq"] for m in fresh) < previous_sequence:
                raise InvalidFact("Late older evidence cannot overwrite a newer field value")
    def commit_analysis(self, owner, analysis_id, token, *, scopes, observations, unresolved=None, corrections=None,
                        public_tool_results=None):
        """Transaction A: valid facts survive independently of any later reply.
        A malformed observation is reported without discarding valid observations
        for other bonds/sides. The Chatflow's semantic interpretation is NOT
        proven merely by accepting this structural/evidence contract.
        """
        unresolved = {} if unresolved is None else unresolved
        if not isinstance(scopes, dict) or not isinstance(unresolved, dict) or not isinstance(observations, list) or len(observations) > 100:
            raise InvalidFact("Invalid multi-bond analysis envelope")
        corrections = [] if corrections is None else corrections
        if not isinstance(corrections,list) or len(corrections)>100:
            raise InvalidFact('Invalid association corrections')
        public_tool_results=[] if public_tool_results is None else public_tool_results
        if (not isinstance(public_tool_results,list) or len(public_tool_results)>6
                or any(not isinstance(entry,dict) or set(entry)!={'request','result'}
                    or not isinstance(entry['request'],dict) or not isinstance(entry['result'],dict)
                    for entry in public_tool_results)):
            raise InvalidFact('Invalid captured public tool results')
        public_tool_results=json.loads(encoded(public_tool_results))
        inputs=[scopes,observations,unresolved,corrections]
        # Retain the existing replay digest for analyses without public calls.
        if public_tool_results:inputs.append(public_tool_results)
        digest = hashlib.sha256(encoded(inputs).encode()).hexdigest()
        with self.write(owner) as tx:
            analysis = self._analysis(tx, owner, analysis_id, token)
            if analysis["state"] == "COMMITTED":
                saved = json.loads(analysis["result_json"])
                if saved["input_hash"] != digest:
                    raise InvalidFact("Analysis replay differs from committed facts")
                return saved
            snapshot = json.loads(analysis["snapshot_json"])
            raw_ids = {m["message_id"] for m in snapshot["messages"]}
            raw_messages={m['message_id']:m for m in snapshot['messages']}
            current_members={p['member_id'] for p in snapshot['members']}
            if set(scopes) != raw_ids or set(unresolved) - raw_ids:
                raise InvalidFact("Each raw message needs an explicit bond association (possibly empty)")
            thread = self._thread(tx, owner, analysis["thread_id"])
            if thread["analyzed_revision"] != analysis["from_revision"]:
                raise StaleDecision("This analysis no longer follows the committed message watermark")
            touched = {}
            for message_id, members in scopes.items():
                unclear = unresolved.get(message_id, [])
                if (not isinstance(members, list) or not isinstance(unclear, list)
                        or len(members) + len(unclear) > 100 or set(members) & set(unclear)):
                    raise InvalidFact("Invalid message-to-bond associations")
                for member_id, relation in [(m, "MENTION") for m in set(members)] + [(m, "UNRESOLVED") for m in set(unclear)]:
                    message=raw_messages[message_id]
                    if member_id not in current_members and (message['direction']!='out' or not message['native_id']):
                        raise InvalidFact('Current inbound associations cannot target a retired round')
                    member = self._member(tx, owner, member_id)
                    if member["thread_id"] != analysis["thread_id"]:
                        raise InvalidFact("Message cannot be assigned to a different counterparty")
                    current = tx.execute("SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?", (message_id, member_id)).fetchone()
                    if current and current["relation"] != relation:
                        raise InvalidFact("Cannot silently rewrite an existing message association")
                    if current is None:
                        tx.execute("INSERT INTO xj2_scope(message_id,member_id,relation) VALUES(?,?,?)", (message_id, member_id, relation))
                    touched[member_id] = member["group_id"]
            for correction in corrections:
                if not isinstance(correction,dict) or set(correction)!={'message_id','member_ids','evidence'}:
                    raise InvalidFact('Invalid correction envelope')
                refs=correction['evidence']
                if not isinstance(refs,list) or not refs or any(ref not in raw_ids for ref in refs):
                    raise InvalidFact('Correction needs fresh clarification in this snapshot')
                for ref in refs:
                    msg=next(m for m in snapshot['messages'] if m['message_id']==ref)
                    if msg['direction']!='in' or msg['historical']:
                        raise InvalidFact('Correction requires actual inbound clarification')
                self._correct_association(tx,owner,analysis['thread_id'],correction['message_id'],correction['member_ids'],'analysis:'+analysis_id)
            accepted, rejected, invalid_members = [], [], set()
            for index, observation in enumerate(observations):
                try:
                    if not isinstance(observation, dict) or set(observation) != {"member_id", "side", "changes", "evidence"}:
                        raise InvalidFact("Invalid quote observation fields")
                    mid = observation["member_id"]
                    if mid not in current_members:
                        raise InvalidFact('Current quote facts cannot update a retired round')
                    member = self._member(tx, owner, mid)
                    if member["thread_id"] != analysis["thread_id"]:
                        raise InvalidFact("Quote cannot cross counterparty threads")
                    previous = self._quote(tx, mid, observation["side"])
                    updated = apply_patch(previous, observation["changes"], evidence=observation["evidence"])
                    self._evidence(tx, member, analysis, observation["changes"], observation["evidence"], previous)
                except (InvalidFact, TypeError, KeyError) as exc:
                    mid=observation.get('member_id') if isinstance(observation,dict) else None
                    side=observation.get('side') if isinstance(observation,dict) else None
                    rejected.append({'index':index,'reason':str(exc),'member_id':mid,'side':side})
                    affected=set(observation.get('affected_members',[])) if isinstance(observation,dict) else set()
                    if mid:affected.add(mid)
                    for bad_member in affected & set(touched):
                        group=self._group(tx,owner,touched[bad_member])
                        relevant='ofr' if group['our_side']=='BUY' else 'bid'
                        if side not in (None,relevant):continue
                        invalid_members.add(bad_member)
                    continue
                self._save_quote(tx, mid, updated, analysis_id)
                if 'price' in observation['changes'] or observation['changes'].get('validity')=='WITHDRAWN':
                    tx.execute("UPDATE xj2_scope SET relation='MENTION' WHERE member_id=? AND relation='INVALID' AND message_id IN (SELECT message_id FROM xj2_message WHERE thread_id=? AND ingest_revision<=?)",(mid,analysis['thread_id'],analysis['from_revision']))
                touched[mid] = member["group_id"]
                if "confirmed_day" in observation["changes"] and updated.confirmed_day is not None:
                    tx.execute("UPDATE xj2_peer SET reconfirm_day=NULL WHERE member_id=?", (mid,))
                accepted.append({"index": index, "member_id": mid, "side": updated.side, "revision": updated.revision})
            # Close automation only after every sibling observation has checked
            # its own evidence. A bad A fragment cannot destroy a valid B fact.
            for bad_member in invalid_members:
                for source in raw_ids:
                    tx.execute("UPDATE xj2_scope SET relation='INVALID' WHERE message_id=? AND member_id=? AND relation='MENTION'",(source,bad_member))
            for mid in touched:
                tx.execute("UPDATE xj2_peer SET version=version+1 WHERE member_id=?", (mid,))
                answered = tx.execute("SELECT m.message_id FROM xj2_message m JOIN xj2_scope s ON s.message_id=m.message_id JOIN xj2_peer p ON p.member_id=s.member_id WHERE p.member_id=? AND m.message_id IN (SELECT message_id FROM xj2_message WHERE thread_id=? AND ingest_revision>? AND ingest_revision<=?) AND s.relation='MENTION' AND m.direction='in' AND m.historical=0 AND p.waiting_since IS NOT NULL AND m.received_at>=p.waiting_since LIMIT 1",
                                      (mid, analysis["thread_id"], analysis["from_revision"], analysis["through_revision"])).fetchone()
                if answered:
                    tx.execute("UPDATE xj2_peer SET response_state='REPLIED',waiting_command_id=NULL,waiting_since=NULL WHERE member_id=?", (mid,))
            self._refresh_groups(tx, owner, touched.values())
            following = tx.execute("SELECT MIN(received_at) AS t FROM xj2_message WHERE thread_id=? AND ingest_revision>?", (analysis["thread_id"], analysis["through_revision"])).fetchone()["t"]
            tx.execute("UPDATE xj2_thread SET analyzed_revision=?,pending_since=? WHERE thread_id=?", (analysis["through_revision"], following, analysis["thread_id"]))
            result = {"input_hash": digest, "accepted": accepted, "rejected": rejected, "unresolved": unresolved,
                      "through_revision": analysis["through_revision"], "facts_committed": True}
            if public_tool_results:
                result['public_tool_results']=public_tool_results
            tx.execute("UPDATE xj2_analysis SET state='COMMITTED',result_json=?,committed_at=? WHERE analysis_id=?", (encoded(result), self.clock(), analysis_id))
            return result
    def rollover(self, owner, group_id, *, trade_day, valuation=None, expected_valuation_date=None):
        """All peers need actual reconfirmation; market data cannot substitute."""
        iso_day(trade_day)
        with self.write(owner) as tx:
            group = self._group(tx, owner, group_id)
            if trade_day <= group["trade_day"]:
                raise InvalidFact("Rollover must advance the trading date")
            policy = LimitPolicy(**json.loads(group["policy_json"]))
            boundary = policy.resolve(bond_code=group["bond_code"], valuation=valuation, expected_valuation_date=expected_valuation_date)
            tx.execute("UPDATE xj2_group SET trade_day=?,boundary_json=?,valuation_json=?,policy_version=policy_version+1,control_version=control_version+1 WHERE group_id=?", (trade_day, encoded(boundary), encoded(asdict(valuation) if valuation else None), group_id))
            for member in tx.execute("SELECT member_id FROM xj2_peer WHERE group_id=?", (group_id,)).fetchall():
                mid = member["member_id"]
                for side in ("bid", "ofr"):
                    previous = self._quote(tx, mid, side)
                    if previous.revision:
                        evidence = {k: v for k, v in previous.evidence.items() if k not in ("confirmed_day", "finality", "validity")}
                        updated = replace(previous, validity="UNCONFIRMED", confirmed_day=None, finality="UNKNOWN", revision=previous.revision+1, evidence=evidence)
                        self._save_quote(tx, mid, updated, "rollover-" + trade_day)
                tx.execute("UPDATE xj2_peer SET version=version+1,reconfirm_day=?,reconfirm_command_id=NULL,waiting_since=NULL,waiting_command_id=NULL,response_state='NONE' WHERE member_id=?", (trade_day, mid))
            self._cancel_commands(tx, group_id=group_id, reason="trading_day_changed")
            self._refresh_groups(tx, owner, [group_id])
