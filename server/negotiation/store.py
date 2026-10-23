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
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from ..db import Database
from .facts import Quote, apply_patch, group_summary
from .prices import InvalidFact, LimitPolicy, Price, StaleDecision, Valuation, iso_day, number
from .schema import statements
from .settings import Aggregation, Settings
from .text_roles import non_trading_spans
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
    def _account_reset(self,tx,owner,account_uid,*,workbench_id=None,applied_only=False):
        if workbench_id is None:
            binding=tx.execute('SELECT account_uid,workbench_id FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
            if not binding or binding['account_uid']!=account_uid:return None
            workbench_id=binding['workbench_id']
        if not workbench_id:return None # Unattributed legacy threads never inherit a selected collector.
        state=" AND r.state IN ('local_applied','complete')" if applied_only else ''
        rows=tx.execute('SELECT r.* FROM xj2_session_reset r WHERE r.owner_id=? AND r.account_uid=? AND r.workbench_id=?'+state+' ORDER BY r.account_epoch DESC',(owner,account_uid,workbench_id)).fetchall()
        binding=tx.execute('SELECT config_json FROM xj2_binding WHERE owner_id=? AND account_uid=? AND workbench_id=?',(owner,account_uid,workbench_id)).fetchone()
        context=json.loads(binding['config_json']).get('context_id') if binding else None
        for row in rows:
            if not context or json.loads(row['request_json']).get('context_id')==context:return dict(row)
        return None
    def account_epoch(self,tx,owner,account_uid,*,workbench_id=None,require_complete=False):
        reset=self._account_reset(tx,owner,account_uid,workbench_id=workbench_id)
        if reset and require_complete and reset['state']!='complete':
            raise InvalidFact('SESSION_RESET_IN_PROGRESS:'+reset['state'])
        if reset:return reset['account_epoch']
        binding=tx.execute('SELECT config_json FROM xj2_binding WHERE owner_id=? AND account_uid=?',(owner,account_uid)).fetchone()
        return int(json.loads(binding['config_json']).get('context_epoch',0)) if binding else 0
    @staticmethod
    def thread_is_current(tx,thread):
        binding=tx.execute('SELECT account_uid,workbench_id FROM xj2_binding WHERE owner_id=?',(thread['owner_id'],)).fetchone()
        if binding is None:
            return not tx.execute("SELECT 1 FROM xj2_audit WHERE owner_id=? AND kind='workspace_retired' LIMIT 1",(thread['owner_id'],)).fetchone()
        return thread.get('workbench_id') is not None and (thread['account_uid'],thread['workbench_id'])==(binding['account_uid'],binding['workbench_id'])
    @staticmethod
    def workspace_cut(tx,owner,thread_id):
        rows=tx.execute("SELECT data_json FROM xj2_audit WHERE owner_id=? AND entity_id=? AND kind='workspace_thread_retired'",(owner,thread_id)).fetchall()
        return max((json.loads(row['data_json'])['through_revision'] for row in rows),default=-1)
    @staticmethod
    def group_is_retired(tx,owner,group_id):
        return bool(tx.execute("SELECT 1 FROM xj2_audit WHERE owner_id=? AND entity_id=? AND kind='workspace_group_retired' LIMIT 1",(owner,group_id)).fetchone())
    def require_current_thread(self,tx,owner,thread_id):
        thread=self._thread(tx,owner,thread_id)
        if not self.thread_is_current(tx,thread):raise InvalidFact('Thread does not belong to the selected collector/account')
        return thread
    def _restore_thread_reset(self,tx,owner,account_uid,peer_uid,thread_id,workbench_id):
        reset=self._account_reset(tx,owner,account_uid,workbench_id=workbench_id or '',applied_only=True)
        if reset:peer=json.loads(reset['snapshot_json'])['peers'].get(peer_uid,{})
        else:
            binding=tx.execute('SELECT config_json FROM xj2_binding WHERE owner_id=? AND workbench_id=? AND account_uid=?',(owner,workbench_id,account_uid)).fetchone()
            if not binding:return
            peer=json.loads(binding['config_json']).get('context_peers',{}).get(peer_uid,{})
        tx.execute('UPDATE xj2_thread SET source_watermark=?,dispatch_version=?,receiver_fence_id=?,receiver_fence_version=? WHERE thread_id=?',
            (peer.get('source_watermark',0),max(0,peer.get('min_control_version',1)-1),peer.get('receiver_fence_id',''),peer.get('receiver_fence_version',0),thread_id))
    def _reset_message_filter(self,tx,thread,alias=''):
        prefix=alias+'.' if alias else ''
        workspace_cut=self.workspace_cut(tx,thread['owner_id'],thread['thread_id'])
        clause,args=(' AND '+prefix+'ingest_revision>? AND '+prefix+'historical=0',[workspace_cut]) if workspace_cut>=0 else ('',[])
        reset=self._account_reset(tx,thread['owner_id'],thread['account_uid'],workbench_id=thread.get('workbench_id') or '',applied_only=True)
        if not reset:return clause,args
        cut=json.loads(reset['local_cut_json'])
        return (clause+' AND '+prefix+'ingest_revision>? AND ('+prefix+'source_seq<=0 OR '+prefix+'source_seq>?) AND '+prefix+'historical=0',
            args+[cut.get('local_revisions',{}).get(thread['thread_id'],0),cut['source_highwater']])
    def _eligible_messages(self,tx,thread,after,limit=100):
        clause,args=self._reset_message_filter(tx,thread)
        return [dict(r) for r in tx.execute('SELECT * FROM xj2_message WHERE thread_id=? AND ingest_revision>?'+clause+' ORDER BY ingest_revision LIMIT ?',
            (thread['thread_id'],after,*args,limit)).fetchall()]
    def _analysis_pending(self,tx,thread):
        if not self.thread_is_current(tx,thread):return False
        reset=self._account_reset(tx,thread['owner_id'],thread['account_uid'],workbench_id=thread.get('workbench_id') or '')
        if not reset:return bool(self._eligible_messages(tx,thread,thread['analyzed_revision'],1))
        if reset['state']!='complete':return False
        return bool(self._eligible_messages(tx,thread,thread['analyzed_revision'],1))
    def source_before_reset(self,tx,thread,source_seq):
        reset=self._account_reset(tx,thread['owner_id'],thread['account_uid'],workbench_id=thread.get('workbench_id') or '',applied_only=True)
        return bool(reset and type(source_seq) is int and source_seq>0 and source_seq<=json.loads(reset['local_cut_json'])['source_highwater'])
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
        if original['ingest_revision']<=self.workspace_cut(tx,owner,thread_id):
            raise InvalidFact('Retired workspace history is read only')
        for mid in member_ids:
            member=self._member(tx, owner, mid)
            if self.group_is_retired(tx,owner,member['group_id']):raise InvalidFact('Retired workspace is read only')
            if member['thread_id'] != thread_id:
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
            self.require_current_thread(tx, owner, thread_id)
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
    def add_member(self, owner, group_id, *, account_uid, peer_uid, workbench_id=None):
        identity(account_uid, "account", 80)
        identity(peer_uid, "counterparty")
        with self.write(owner) as tx:
            return self._add_member(tx,owner,group_id,account_uid=account_uid,peer_uid=peer_uid,workbench_id=workbench_id)
    MAX_ACTIVE_COUNTERPARTIES = 50
    def _active_peers(self, tx, owner, account_uid):
        # A pause keeps analysis alive and therefore keeps the slot. Only a
        # closed/handoff/archived final group releases that peer's last slot.
        return {row['peer_uid'] for row in tx.execute("""SELECT DISTINCT t.peer_uid
            FROM xj2_thread t JOIN xj2_peer p ON p.thread_id=t.thread_id
            JOIN xj2_group g ON g.group_id=p.group_id
            WHERE t.owner_id=? AND t.account_uid=? AND g.owner_id=?
            AND g.state='OPEN' AND g.archived=0""",(owner,account_uid,owner)).fetchall()}
    def _ensure_peer_capacity(self, tx, owner, account_uid, incoming):
        existing = self._active_peers(tx, owner, account_uid)
        requested = set(incoming)
        total = len(existing | requested)
        if total > self.MAX_ACTIVE_COUNTERPARTIES:
            raise InvalidFact('ACTIVE_COUNTERPARTY_LIMIT: 同一企点账号最多维持50个活跃对手；'
                              f'当前{len(existing)}人，本次新增{len(requested-existing)}人。'
                              '同一对手多券只计一人；暂停仍占名额，请先完成人工交付或归档。')
        return {'active':len(existing),'requested_total':total,'limit':self.MAX_ACTIVE_COUNTERPARTIES}
    def counterparty_capacity(self, owner, account_uid):
        with self.db.transaction(write=False) as tx:
            active = len(self._active_peers(tx,owner,account_uid))
        return {'active':active,'limit':self.MAX_ACTIVE_COUNTERPARTIES,
                'available':max(0,self.MAX_ACTIVE_COUNTERPARTIES-active),
                'unit':'distinct_counterparties','paused_counts':True}
    def _add_member(self,tx,owner,group_id,*,account_uid,peer_uid,workbench_id=None):
        binding=tx.execute('SELECT account_uid,workbench_id FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
        if binding:
            if account_uid!=binding['account_uid'] or workbench_id is not None and workbench_id!=binding['workbench_id']:
                raise InvalidFact('New member must belong to the selected collector/account')
            workbench_id=binding['workbench_id']
        if workbench_id is not None:identity(workbench_id,'workbench',160)
        group = self._group(tx, owner, group_id)
        if self.group_is_retired(tx,owner,group_id):raise InvalidFact('Retired workspace is read only')
        existing_pairs=tx.execute('SELECT t.account_uid,t.workbench_id FROM xj2_peer p JOIN xj2_thread t ON t.thread_id=p.thread_id WHERE p.group_id=?',(group_id,)).fetchall()
        if any((p['account_uid'],p['workbench_id'])!=(account_uid,workbench_id) for p in existing_pairs):
            raise InvalidFact('A bond group cannot mix collector/account identities')
        if group["state"] == "OPEN" and not group["archived"]:
            self._ensure_peer_capacity(tx,owner,account_uid,[peer_uid])
        thread = tx.execute("SELECT thread_id FROM xj2_thread WHERE owner_id=? AND account_uid=? AND peer_uid=? AND (workbench_id=? OR (workbench_id IS NULL AND ? IS NULL))", (owner, account_uid, peer_uid,workbench_id,workbench_id)).fetchone()
        if thread:
            tid = thread["thread_id"]
        else:
            tid = new_id()
            tx.execute("INSERT INTO xj2_thread(thread_id,owner_id,workbench_id,account_uid,peer_uid,ingest_revision,analyzed_revision) VALUES(?,?,?,?,?,0,0)", (tid, owner,workbench_id, account_uid, peer_uid))
            self._restore_thread_reset(tx,owner,account_uid,peer_uid,tid,workbench_id)
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
            previous = tx.execute("SELECT * FROM xj2_message WHERE thread_id=? AND event_id=?", (thread_id, event_id)).fetchone()
            if previous:
                if previous["content_hash"] != digest:
                    # A new workspace may replay the same retired native event
                    # with a fresh transport sequence/historical classification.
                    # Accept only provably identical native facts; never rewrite
                    # their original hash, sequence, scope or financial evidence.
                    if historical and occurred_at is not None and previous['ingest_revision']<=self.workspace_cut(tx,owner,thread_id):
                        target=tx.execute('SELECT message_id FROM xj2_message WHERE thread_id=? AND event_id=?',(thread_id,target_event_id)).fetchone() if target_event_id else None
                        target_matches=(previous['target_id']==target['message_id']) if target is not None else target_event_id is None and previous['target_id'] is None
                        fields=('text','direction','kind','native_id','occurred_at','trade_day')
                        if target_matches and tuple(previous[key] for key in fields)==(text,direction,kind,native_id,stamp,trade_day):
                            return previous['message_id']
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
            if target_id and not historical and not self.source_before_reset(tx,thread,source_seq):
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
            self.require_current_thread(tx, owner, thread_id)
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
                member=self._member(tx, owner, member_id)
                if self.group_is_retired(tx,owner,member['group_id']):raise InvalidFact('Retired workspace is read only')
                self.require_current_thread(tx,owner,member['thread_id'])
                tx.execute("UPDATE xj2_peer SET send_allowed=?,control_version=control_version+1 WHERE member_id=?", (int(allowed), member_id))
            elif group_id:
                group = self._group(tx, owner, group_id)
                if self.group_is_retired(tx,owner,group_id):raise InvalidFact('Retired workspace is read only')
                for peer in tx.execute('SELECT thread_id FROM xj2_peer WHERE group_id=?',(group_id,)).fetchall():
                    self.require_current_thread(tx,owner,peer['thread_id'])
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

    @staticmethod
    def _context_thread(tx,member_id):
        return dict(tx.execute('SELECT t.* FROM xj2_peer p JOIN xj2_thread t ON t.thread_id=p.thread_id WHERE p.member_id=?',(member_id,)).fetchone())
    def _context(self, tx, member_id, through_revision):
        # A new authorization round does not erase actual chat history. Scope
        # history by owner + physical thread + bond, while facts stay per round.
        thread=self._context_thread(tx,member_id)
        clause,args=self._reset_message_filter(tx,thread,'m')
        rows = tx.execute("""SELECT m.* FROM xj2_message m
            JOIN xj2_peer cp ON cp.member_id=? JOIN xj2_group cg ON cg.group_id=cp.group_id
            WHERE m.thread_id=cp.thread_id AND m.ingest_revision<=?
            AND (m.direction='in' OR m.native_id IS NOT NULL)
            AND EXISTS (SELECT 1 FROM xj2_scope s JOIN xj2_peer hp ON hp.member_id=s.member_id
                JOIN xj2_group hg ON hg.group_id=hp.group_id WHERE s.message_id=m.message_id
                AND hp.thread_id=cp.thread_id AND hg.owner_id=cg.owner_id AND hg.bond_code=cg.bond_code
                AND s.relation IN ('MENTION','UNRESOLVED','INVALID'))
            """+clause+" ORDER BY m.occurred_at DESC,m.ingest_revision DESC LIMIT ?",
                          (member_id, through_revision, *args, self.settings.context_messages)).fetchall()
        return [dict(row) for row in reversed(rows)]
    def _history_message(self, tx, member_id, message_id, through_revision, *, allow_invalid=False):
        clause,args=self._reset_message_filter(tx,self._context_thread(tx,member_id),'m')
        return tx.execute("""SELECT m.* FROM xj2_message m
            JOIN xj2_peer cp ON cp.member_id=? JOIN xj2_group cg ON cg.group_id=cp.group_id
            WHERE m.message_id=? AND m.thread_id=cp.thread_id AND m.ingest_revision<=?
            AND (m.direction='in' OR m.native_id IS NOT NULL)
            AND EXISTS (SELECT 1 FROM xj2_scope s JOIN xj2_peer hp ON hp.member_id=s.member_id
                JOIN xj2_group hg ON hg.group_id=hp.group_id WHERE s.message_id=m.message_id
                AND hp.thread_id=cp.thread_id AND hg.owner_id=cg.owner_id AND hg.bond_code=cg.bond_code
                AND (s.relation='MENTION' OR (?=1 AND s.relation='INVALID')))"""+clause,
                          (member_id,message_id,through_revision,int(allow_invalid),*args)).fetchone()
    def context(self, owner, member_id, *, anchors=(), through_revision=None):
        """One global last-N window; explicit references are added, not guessed."""
        with self.db.transaction(write=False) as tx:
            member = self._member(tx, owner, member_id)
            thread = self.require_current_thread(tx, owner, member["thread_id"])
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
    def _settlement_question(self, tx, owner, member):
        """Read actual command evidence, independently of the generic waiting pointer."""
        group=self._group(tx,owner,member['group_id'])
        side='ofr' if group['our_side']=='BUY' else 'bid'
        quote=self._quote(tx,member['member_id'],side)
        if (member['reconfirm_day'] or group['archived'] or quote.price is None
                or quote.price.kind not in ('NET_PRICE','DIRTY_PRICE') or quote.validity!='ACTIVE'
                or quote.assertion!='QUOTE' or quote.confirmed_day!=group['trade_day']):
            return None
        # Search the current-day actual sends and unresolved executions only.
        # Cancelled/failed history cannot hide a current question at row 100.
        # Retain competing live questions: a bare T+1 must still be unambiguous.
        start = datetime.fromisoformat(group['trade_day']).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        rows=[dict(row) for row in tx.execute("""SELECT * FROM xj2_command
            WHERE thread_id=? AND (state IN ('PENDING','UNKNOWN')
              OR (state='SENT' AND sent_at>=? AND sent_at<?))
            ORDER BY created_at DESC,command_id DESC""",
            (member['thread_id'],start,start+86400)).fetchall()]
        target={'side':side,'quote_revision':quote.revision}
        candidates=[]; live=[]
        for row in rows:
            body=json.loads(row['body_json']);basis=json.loads(row['basis_json'])
            if basis.get('trade_day')!=group['trade_day'] or row['state'] not in ('SENT','PENDING','UNKNOWN'):
                continue
            row['body']=body;live.append(row)
            if (row['member_id']==member['member_id'] and body.get('settlement_question')==target
                    and row['state']=='SENT' and row['sent_at'] is not None and row['native_id']):
                sent=tx.execute("SELECT message_id FROM xj2_message WHERE thread_id=? AND direction='out' AND native_id=? AND text=?",
                                (member['thread_id'],row['native_id'],body['text'])).fetchone()
                if sent and not tx.execute("SELECT message_id FROM xj2_message WHERE target_id=? AND historical=0 LIMIT 1",
                                           (sent['message_id'],)).fetchone():
                    candidates.append(row)
        if not candidates:return None
        chosen=max(candidates,key=lambda row:(row['sent_at'],row['command_id']))
        ambiguous=False
        for row in live:
            marker=row['body'].get('settlement_question')
            if row['member_id']==member['member_id'] and marker==target:continue
            if row['body'].get('kind')=='SOCIAL':continue
            if row['state'] in ('PENDING','UNKNOWN'):
                ambiguous=True
            elif marker:
                other=self._member(tx,owner,row['member_id'])
                other_group=self._group(tx,owner,other['group_id'])
                other_quote=self._quote(tx,row['member_id'],marker['side'])
                if (not other_group['archived'] and other_quote.revision==marker['quote_revision']
                        and other_quote.validity=='ACTIVE' and other_quote.confirmed_day==group['trade_day']):
                    ambiguous=True
            elif row['sent_at'] is not None and row['sent_at']>=chosen['sent_at']:
                ambiguous=True
        return {'command_id':chosen['command_id'],**target,'trade_day':group['trade_day'],
                'sent_at':chosen['sent_at'],'ambiguous':ambiguous}

    @staticmethod
    def _settlement_sources(snapshot, observation, question):
        """Reused before calculation and inside commit; no calendar or network I/O."""
        binding=observation.get('settlement_binding')
        if (not question or not isinstance(binding,dict) or set(binding)!={'command_id','quote_revision'}
                or binding!={key:question[key] for key in ('command_id','quote_revision')}
                or observation.get('side')!=question['side']):
            raise InvalidFact('Settlement answer does not match an actual current sent question')
        changes=observation.get('changes',{});evidence=observation.get('evidence',{})
        if set(changes)!={'price','finality'} or changes['finality']!='UNKNOWN' or set(evidence)!=set(changes):
            raise InvalidFact('Settlement answer may only supplement conditions, not reconfirm price or quantity')
        refs=evidence.get('price')
        if not isinstance(refs,list) or not refs or len(refs)>30 or evidence.get('finality')!=refs:
            raise InvalidFact('Settlement answer requires current source references')
        current={row['message_id']:row for row in snapshot['messages']}
        for ref in refs:
            message=current.get(ref)
            if (not message or message['direction']!='in' or message['historical']
                    or message['trade_day']!=question['trade_day']
                    or not isinstance(message.get('occurred_at'),(int,float))
                    or not math.isfinite(message['occurred_at'])
                    or message['occurred_at']<question['sent_at'] or message['received_at']<message['occurred_at']):
                raise InvalidFact('Settlement reply must occur after the actual question on the same trading day')
            def named_in(text):
                return {member['member_id'] for member in snapshot['members'] if member.get('bond_code')
                        and re.search(r'(?<![a-zA-Z0-9_.-])'+re.escape(member['bond_code'])+r'(?![a-zA-Z0-9_.-])',text,re.I)}
            named=named_in(message['text'])
            if named and observation['member_id'] not in named or not named and question['ambiguous']:
                raise InvalidFact('Settlement reply does not uniquely identify its question')
            if len(named)>1 and not any(named_in(part)=={observation['member_id']}
                    and any(role in ('DATE','SETTLEMENT') for _start,_end,role in non_trading_spans(part))
                    for part in re.split(r'[,，;；。\n]',message['text'])):
                raise InvalidFact('Multiple named bonds require an unambiguous settlement clause for this member')

    @staticmethod
    def _known_outbound_scopes(tx, thread_id, messages, members):
        """Snapshot native-send associations, not current permission to send or confirm."""
        current={member['member_id'] for member in members}
        known={}
        for message in messages:
            if message['direction']!='out' or message['historical'] or not message['native_id']:
                continue
            commands=tx.execute("SELECT command_id,body_json,content_hash,native_id FROM xj2_command WHERE thread_id=? AND native_id=? AND state='SENT'",
                                (thread_id,message['native_id'])).fetchall()
            if len(commands)!=1:continue
            command=commands[0];body=json.loads(command['body_json'])
            if (body['kind']=='MANUAL' or body['text']!=message['text'] or command['native_id']!=message['native_id']
                    or hashlib.sha256(encoded(body).encode()).hexdigest()!=command['content_hash']):continue
            # Captured native events and command bodies hash different objects.
            # A receipt may reuse an earlier raw row; that raw hash stays intact.
            scoped=[row['member_id'] for row in tx.execute("""SELECT cm.member_id FROM xj2_command_member cm
                JOIN xj2_scope s ON s.member_id=cm.member_id AND s.message_id=? AND s.relation='MENTION'
                WHERE cm.command_id=? ORDER BY cm.member_id""",(message['message_id'],command['command_id'])).fetchall()]
            all_members=[row['member_id'] for row in tx.execute("SELECT member_id FROM xj2_command_member WHERE command_id=? ORDER BY member_id",
                                                               (command['command_id'],)).fetchall()]
            if scoped and scoped==all_members and set(scoped)<=current:
                known[message['message_id']]=scoped
        return known

    def claim_analysis(self, owner, thread_id, *, aggregation: Aggregation | None = None,
                       force=False, lease_seconds=60, include_known_outbound_scopes=False):
        """A manual explicit force is for tests/recovery, not a latency default.
        Lease recovery reuses the SAME immutable raw-message snapshot. A new
        intake is never skipped when an older model request finishes late.
        """
        if (type(force) is not bool or type(include_known_outbound_scopes) is not bool
                or type(lease_seconds) is not int or not 1 <= lease_seconds <= 600):
            raise InvalidFact("Invalid analysis execution options")
        if aggregation is None and not force:
            raise InvalidFact("No measured aggregation policy; automatic scheduling is not configured")
        now = self.clock()
        with self.write(owner) as tx:
            thread = self._thread(tx, owner, thread_id)
            if not self._analysis_pending(tx,thread):return None
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
            raw = self._eligible_messages(tx,thread,thread['analyzed_revision'])
            if not raw:return None
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
                peer['settlement_question']=self._settlement_question(tx,owner,peer)
                # Informational snapshot only: commit independently checks the
                # current receipt in its transaction before deriving a day.
                peer['reconfirm_sent_at'] = None
                if peer['reconfirm_day'] and peer['reconfirm_command_id']:
                    sent = tx.execute("SELECT sent_at FROM xj2_command WHERE command_id=? AND member_id=? AND state='SENT'",
                                      (peer['reconfirm_command_id'], peer['member_id'])).fetchone()
                    if sent:
                        peer['reconfirm_sent_at'] = sent['sent_at']
            snapshot = {"thread_id": thread_id, "from_revision": thread["analyzed_revision"],
                        "through_revision": through, "messages": raw, "members": peers,
                        "context_messages": self.settings.context_messages,
                        "account_epoch": self.account_epoch(tx,owner,thread['account_uid'],workbench_id=thread.get('workbench_id') or '',require_complete=True)}
            known=self._known_outbound_scopes(tx,thread_id,raw,peers) if include_known_outbound_scopes else {}
            if known:snapshot['known_outbound_scopes']=known
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
    def _reconfirmed_observation(self, tx, member, analysis, observation, raw_messages):
        changes, evidence = observation['changes'], observation['evidence']
        if (not member['reconfirm_day'] or not isinstance(changes, dict) or not isinstance(evidence, dict)
                or 'confirmed_day' in changes or not changes.get('price')
                or changes.get('assertion') != 'QUOTE' or changes.get('validity') != 'ACTIVE'):
            return observation
        refs = evidence.get('price')
        if not isinstance(refs, list):
            return observation  # The normal field validator will report malformed evidence.
        command = tx.execute("SELECT sent_at FROM xj2_command WHERE command_id=? AND member_id=? AND state='SENT'",
                             (member['reconfirm_command_id'], member['member_id'])).fetchone()
        if not command or command['sent_at'] is None:
            return observation
        fresh = [ref for ref in refs if isinstance(ref, str) and ref in raw_messages
                 and raw_messages[ref]['direction'] == 'in' and not raw_messages[ref]['historical']
                 and raw_messages[ref]['ingest_revision'] > analysis['from_revision']
                 and raw_messages[ref]['trade_day'] == member['reconfirm_day']
                 and raw_messages[ref]['received_at'] >= command['sent_at']]
        if not fresh:
            return observation
        return {**observation, 'changes': {**changes, 'confirmed_day': member['reconfirm_day']},
                'evidence': {**evidence, 'confirmed_day': list(dict.fromkeys(fresh))}}

    def commit_analysis(self, owner, analysis_id, token, *, scopes, observations, unresolved=None, corrections=None,
                        public_tool_results=None, reconfirm_current_quotes=False):
        """Transaction A: valid facts survive independently of any later reply.
        A malformed observation is reported without discarding valid observations
        for other bonds/sides. The Chatflow's semantic interpretation is NOT
        proven merely by accepting this structural/evidence contract.
        """
        unresolved = {} if unresolved is None else unresolved
        if type(reconfirm_current_quotes) is not bool:
            raise InvalidFact('Invalid automatic reconfirmation policy')
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
        if reconfirm_current_quotes:inputs.append({'reconfirm_current_quotes': True})
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
            for ref,expected in snapshot.get('known_outbound_scopes',{}).items():
                if (not isinstance(scopes[ref],list) or len(scopes[ref])!=len(expected)
                        or set(scopes[ref])!=set(expected) or unresolved.get(ref)):
                    raise InvalidFact('Cannot replace the application-owned outbound association')
            thread = self._thread(tx, owner, analysis["thread_id"])
            if not self.thread_is_current(tx,thread):raise StaleDecision('Analysis belongs to another selected collector/account')
            if self.account_epoch(tx,owner,thread['account_uid'],workbench_id=thread.get('workbench_id') or '',require_complete=True)!=snapshot.get('account_epoch',0):
                raise StaleDecision('Analysis belongs to a reset authorization round')
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
            accepted, rejected, invalid_members, reconfirmed_members = [], [], set(), set()
            for index, observation in enumerate(observations):
                try:
                    if isinstance(observation,dict) and isinstance(observation.get('validation_error'),str):
                        raise InvalidFact(observation['validation_error'])
                    bound=isinstance(observation,dict) and 'settlement_binding' in observation
                    fields={"member_id", "side", "changes", "evidence"}|({'settlement_binding'} if bound else set())
                    if not isinstance(observation, dict) or set(observation) != fields:
                        raise InvalidFact("Invalid quote observation fields")
                    mid = observation["member_id"]
                    if mid not in current_members:
                        raise InvalidFact('Current quote facts cannot update a retired round')
                    member = self._member(tx, owner, mid)
                    if member["thread_id"] != analysis["thread_id"]:
                        raise InvalidFact("Quote cannot cross counterparty threads")
                    if reconfirm_current_quotes and not bound:
                        observation = self._reconfirmed_observation(tx, member, analysis, observation, raw_messages)
                    previous = self._quote(tx, mid, observation["side"])
                    if bound:
                        self._settlement_sources(snapshot,observation,self._settlement_question(tx,owner,member))
                        price=Price.load(observation['changes']['price'])
                        if previous.price is None or (price.kind,price.value)!=(previous.price.kind,previous.price.value):
                            raise InvalidFact('Settlement answer changed the bound cash price')
                        for ref in previous.evidence.get('price',[]):
                            if tx.execute("SELECT message_id FROM xj2_message WHERE target_id=? AND historical=0 LIMIT 1",(ref,)).fetchone():
                                raise InvalidFact('Settlement question price source has been edited or withdrawn')
                    updated = apply_patch(previous, observation["changes"], evidence=observation["evidence"])
                    self._evidence(tx, member, analysis, observation["changes"], observation["evidence"], previous)
                    if bound:
                        updated=replace(updated,evidence={**updated.evidence,'price':list(dict.fromkeys(
                            previous.evidence.get('price',[])+updated.evidence['price']))})
                except (InvalidFact, TypeError, KeyError) as exc:
                    mid=observation.get('member_id') if isinstance(observation,dict) else None
                    side=observation.get('side') if isinstance(observation,dict) else None
                    rejected.append({'index':index,'reason':str(exc),'member_id':mid,'side':side})
                    if isinstance(observation,dict) and isinstance(observation.get('validation_path'),str):
                        rejected[-1]['validation_path']=observation['validation_path']
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
                    reconfirmed_members.add(mid)
                accepted.append({"index": index, "member_id": mid, "side": updated.side, "revision": updated.revision})
            # Both sides in one analysis must see the same confirmation gate.
            # Clearing it early can either miss a later valid side or let an
            # earlier, pre-question reply bypass the gate after its sibling.
            for mid in sorted(reconfirmed_members):
                tx.execute("UPDATE xj2_peer SET reconfirm_day=NULL WHERE member_id=?", (mid,))
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
    def refresh_valuation(self, owner, group_id, *, valuation, expected_valuation_date, expected_policy_version=None):
        """Same-day recovery/update of OUR BP boundary. Never edits peer facts.
        Repeated equal economics is a no-op. A competing refresh/round may not
        be overwritten by a snapshot fetched before that transaction committed.
        """
        def boundary_for(group):
            policy=LimitPolicy(**json.loads(group['policy_json']))
            if group['archived'] or policy.mode!='CHINABOND_BP':return None
            if expected_policy_version is not None and group['policy_version']!=expected_policy_version:return None
            boundary=policy.resolve(bond_code=group['bond_code'],valuation=valuation,expected_valuation_date=expected_valuation_date)
            if boundary is None:raise InvalidFact('A sourced ChinaBond valuation is required for refresh')
            previous=json.loads(group['valuation_json'])
            if (json.loads(group['boundary_json'])==boundary and previous
                    and Valuation(**previous).authorization_key()==valuation.authorization_key()):
                return None  # A new quote carrying identical valuation is not a new trading decision.
            return boundary

        # Stable polling is a read, not a UI revision or owner-row write.
        with self.db.transaction(write=False) as tx:
            if boundary_for(self._group(tx,owner,group_id)) is None:return False
        with self.write(owner) as tx:
            group=self._group(tx,owner,group_id)
            boundary=boundary_for(group)  # Recheck under the owner lock.
            if boundary is None:return False
            previous=json.loads(group['valuation_json'])
            tx.execute('UPDATE xj2_group SET boundary_json=?,valuation_json=?,policy_version=policy_version+1 WHERE group_id=?',
                       (encoded(boundary),encoded(asdict(valuation)),group_id))
            self._cancel_commands(tx,group_id=group_id,reason='CHINABOND_VALUATION_REFRESHED')
            self._refresh_groups(tx,owner,[group_id])
            tx.execute('INSERT INTO xj2_audit(audit_id,owner_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)',
                (new_id(),owner,'valuation_refreshed',group_id,self.clock(),encoded({'previous':previous,
                'valuation':asdict(valuation),'boundary':boundary})))
            return True
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
