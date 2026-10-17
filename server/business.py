"""Transaction boundaries for user-confirmed inquiry batches and execution facts."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .domain import decimal, opening_text, policy
from .repository import Repository


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def identity(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _counterparty_stop_intent(text: str) -> bool:
    # Do not treat negated deal status (e.g. not yet concluded) as a command
    # to cancel the conversation. A separate explicit stop still wins.
    direct = r'停止|别发|不要再|不用了|不询了|不谈了|不出了|不卖了|不买了|先不做|转人工|人工联系|撤价|\bdone\b|按这个价做|我们要了'
    if re.search(direct, text, re.I):
        return True
    stripped = re.sub(r'(?:还没|没有|尚未|未|没|不)成交', '', text)
    return bool(re.search(r'成交(?:了|啦|吧|！|!|。|，|,|$)', stripped))


class InquiryService:
    def __init__(self, repository: Repository, *, clock=time.time, reference_provider=None):
        self.repo = repository
        self.clock = clock
        self.reference_provider = reference_provider
        from .diagnostics import Diagnostics
        self.diagnostics = Diagnostics(clock)

    def _binding(self, tx, owner: str, *, lock: bool = True):
        suffix = " FOR UPDATE" if lock and self.repo.db.mysql else ""
        row = tx.execute("SELECT * FROM xj_binding WHERE owner_user_id=?" + suffix, (owner,)).fetchone()
        if not row:
            raise PermissionError("尚未配置本人企点账号")
        return row

    def _audit(self, tx, owner, kind, entity, data):
        tx.execute("INSERT INTO xj_audit(audit_id,owner_user_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)",
                   (identity("ev_"), owner, kind, entity, self.clock(), encoded(data)))

    def _reference_snapshot(self):
        from .reference_data import ReferenceSnapshot
        if self.reference_provider is None:
            raise ValueError('BP 授权需要服务端可信估值，当前尚未接入')
        try:
            snapshot = self.reference_provider.snapshot()
        except Exception:
            raise ValueError('可信估值暂不可用，不允许根据浏览器估值确认 BP') from None
        if (not isinstance(snapshot, ReferenceSnapshot) or not snapshot.snapshot_id
                or not snapshot.as_of or not isinstance(snapshot.sources, list)):
            raise ValueError('可信估值快照缺少完整来源')
        return snapshot

    def _normalized_authorization(self, item: dict, preferences: dict, snapshot=None) -> dict:
        from .reference_data import ReferenceSnapshot
        result = policy(item)
        basis = item.get('pricing_basis', 'absolute')
        if basis not in ('absolute', 'bp'):
            raise ValueError('价格授权口径须为 absolute 或 bp')
        result['pricing_basis'] = basis
        if basis == 'absolute':
            # The trader explicitly accepts these yields; never imply the UI
            # derived them from a verified valuation source.
            return result
        if not isinstance(snapshot, ReferenceSnapshot):
            raise ValueError('相对估值 BP 不允许在缺少可信快照时确认')
        saved = (preferences.get('floors') or {}).get(result['code'])
        strategy = preferences.get('strategy')
        if not isinstance(saved, dict) or saved.get('kind') != 'bp' or not isinstance(strategy, dict):
            raise ValueError('BP 授权须有本人保存的 BP 底价与策略')
        matches = [bond for bond in snapshot.bonds
                   if isinstance(bond, dict) and bond.get('code') == result['code']]
        if len(matches) != 1:
            raise ValueError('没有可唯一归属的服务端可信债券估值')
        fact = matches[0].get('valuation')
        if not isinstance(fact, dict):
            raise ValueError('可信估值缺失')
        source_ids = {src.get('id') for src in snapshot.sources
                      if isinstance(src, dict) and isinstance(src.get('id'), str)}
        values = {}
        for key in ('provider', 'basis', 'yield_pct', 'valuation_date'):
            field = fact.get(key)
            if (not isinstance(field, dict) or field.get('quality') != 'verified'
                    or field.get('source_ref') not in source_ids):
                raise ValueError('估值字段没有经过可信来源验证')
            values[key] = field.get('value')
        if values['provider'] not in ('中债', 'CNBD', 'ChinaBond') or values['basis'] != 'maturity':
            raise ValueError('估值须为中债到期收益率口径')
        date_text = values['valuation_date']
        if not isinstance(date_text, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_text):
            raise ValueError('估值日格式无效')
        try:
            age = (datetime.fromtimestamp(self.clock(), ZoneInfo('Asia/Shanghai')).date()
                   - datetime.fromisoformat(date_text).date()).days
        except (ValueError, OverflowError):
            raise ValueError('估值日期不可验证') from None
        if not 0 <= age <= 3:
            raise ValueError('可信估值日已失鲜，必须重新确认')
        val = decimal(values['yield_pct'], '可信中债估值')
        try:
            floor_bp = decimal(saved['value'], '保存的 BP 底价')
            start_bp = decimal(strategy['start_offset_bp'], '起始偏移 BP')
            step_bp = decimal(strategy['step_bp'], '策略步幅')
        except (KeyError, TypeError, ValueError):
            raise ValueError('保存的 BP 底价或策略格式无效') from None
        if start_bp < floor_bp or step_bp <= 0:
            raise ValueError('BP 授权的边界或步幅无效')
        sign = 1 if result['our_side'] == 'BUY' else -1
        opening = val + sign * start_bp / 100
        boundary = val + sign * floor_bp / 100
        tolerance = decimal('0.000001')
        if (abs(decimal(result['opening_yield_pct']) - opening) > tolerance
                or abs(decimal(result['limit_yield_pct']) - boundary) > tolerance
                or decimal(result['step_bp']) != step_bp
                or result['strict_match'] != strategy.get('strict_match', True)):
            raise ValueError('浏览器 BP 换算与服务端可信值或策略不一致')
        result['opening_yield_pct'] = format(opening, 'f')
        result['limit_yield_pct'] = format(boundary, 'f')
        result['valuation_snapshot'] = {
            'snapshot_id': snapshot.snapshot_id,
            'as_of': snapshot.as_of,
            'provider': values['provider'],
            'basis': values['basis'],
            'valuation_date': date_text,
            'yield_pct': format(val, 'f'),
            'source_ref': fact['yield_pct']['source_ref']}
        result['bp_terms'] = {
            'floor_bp': format(floor_bp, 'f'),
            'opening_offset_bp': format(start_bp, 'f'),
            'floor_revision': saved.get('revision'),
            'strategy_digest': digest(strategy)}
        return result

    def preview(self, owner: str, request_id: str, items: list[dict]) -> dict:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 160:
            raise ValueError("须提供稳定的请求ID")
        if not isinstance(items, list) or not items or len(items) > 500:
            raise ValueError("本批须有1至500条明确询价明细")
        with self.repo.db.transaction(write=False) as tx:
            saved_binding = self._binding(tx, owner, lock=False)
            preferences = json.loads(saved_binding['config_json'])
        need_bp = any(isinstance(v, dict) and v.get('pricing_basis') == 'bp' for v in items)
        reference = self._reference_snapshot() if need_bp else None
        normalized, texts, seen = [], {}, set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("询价明细无效")
            value = self._normalized_authorization(item, preferences, reference)
            peers = item.get("peer_ids")
            if not isinstance(peers, list) or not peers or len(peers) > 100:
                raise ValueError("须明确本批收件人")
            if any(not isinstance(peer, str) for peer in peers):
                raise ValueError("收件人标识无效")
            for peer in dict.fromkeys(peers):
                if not isinstance(peer, str) or not peer.strip() or len(peer) > 160:
                    raise ValueError("收件人标识无效")
                key = (peer, value["code"], value["our_side"])
                if key in seen:
                    raise ValueError("同批存在重复询价明细")
                seen.add(key)
                # Free-form manual messages use the separate manual-send path.
                rendered = opening_text(value)
                if item.get("text") not in (None, "", rendered):
                    raise ValueError("编辑后的首句须与结构化授权一致；自由消息请使用人工发送")
                normalized.append({**value, "peer_id": peer, "text": rendered})
                texts.setdefault(peer, []).append(rendered)
        merged = {peer: "\n".join(parts) for peer, parts in texts.items()}
        if any(len(text) > 1500 for text in merged.values()):
            raise ValueError("拼合后超过1500字，没有发送")
        data = {"items": normalized, "commands_preview": [{"peer_id": p, "text": t} for p, t in merged.items()]}
        fingerprint = digest(data)
        with self.repo.db.transaction() as tx:
            binding = self._binding(tx, owner)
            if need_bp and (binding['version'] != saved_binding['version']
                    or digest(json.loads(binding['config_json'])) != digest(preferences)):
                raise ValueError('保存的 BP 底价或策略已发生变化，请重新预览')
            previous = tx.execute("SELECT * FROM xj_batch WHERE owner_user_id=? AND request_id=?", (owner, request_id)).fetchone()
            if previous:
                if previous["content_hash"] != fingerprint:
                    raise ValueError("请求ID与原批次内容冲突")
                return self._batch(previous)
            batch_id = identity("batch_")
            tx.execute("INSERT INTO xj_batch(batch_id,owner_user_id,request_id,content_hash,version,status,binding_version,data_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (batch_id, owner, request_id, fingerprint, 1, "DRAFT", binding["version"], encoded(data), self.clock()))
            self._audit(tx, owner, "batch_preview", batch_id, {"count": len(normalized)})
        return {"batch_id": batch_id, "version": 1, "status": "DRAFT", **data}

    @staticmethod
    def _batch(row):
        return {"batch_id": row["batch_id"], "version": row["version"], "status": row["status"], **json.loads(row["data_json"])}

    def _conversation(self, tx, owner, binding, peer):
        row = tx.execute("SELECT * FROM xj_conversation WHERE owner_user_id=? AND peer_id=? AND kind='private'", (owner, peer)).fetchone()
        if row:
            # Explicit takeover is a durable consent barrier, independent of the
            # transient HUMAN/STOPPING/STOPPED mode. Only resume() may clear it.
            if row['human_lock']:
                raise ValueError('This conversation is under explicit human control; resume it first')
            initial_unapproved=(row['mode']=='HUMAN' and not row['social_authorized'] and not row['human_lock'])
            if row["mode"] in ("HUMAN", "STOPPING") and not initial_unapproved:
                raise ValueError("该对话正在接管或对账，不能重新启动自动询价")
            if initial_unapproved:
                pending=tx.execute("SELECT command_id FROM xj_command WHERE conversation_id=? AND status IN ('queued','pending','unknown') LIMIT 1",(row['conversation_id'],)).fetchone()
                if pending:raise ValueError('初次授权前须处理在途消息')
                high=tx.execute('SELECT MAX(source_seq) AS n FROM xj_message WHERE conversation_id=?',(row['conversation_id'],)).fetchone()['n'] or 0
                tx.execute('UPDATE xj_conversation SET resume_seq=? WHERE conversation_id=?',(high,row['conversation_id']))
            return dict(row)
        cid = identity("conv_")
        tx.execute("INSERT INTO xj_conversation(conversation_id,owner_user_id,account_uid,peer_id,kind,mode,control_version,resume_seq,social_authorized,memory_revision,memory_json,ambiguity_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, owner, binding["account_uid"], peer, "private", "AUTO", 1, 0, 1, 1, "{}", "{}"))
        return {"conversation_id": cid, "control_version": 1, "mode": "AUTO"}

    def confirm(self, owner: str, batch_id: str, expected_version: int, request_id: str) -> dict:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
            raise ValueError("须提供确认请求ID")
        # Fetch the live trusted source outside the write transaction, then
        # recheck the frozen basis and DB preferences under the account lock.
        with self.repo.db.transaction(write=False) as tx:
            draft = tx.execute("SELECT status,data_json FROM xj_batch WHERE batch_id=? AND owner_user_id=?",
                               (batch_id, owner)).fetchone()
        need_bp = bool(draft and draft['status'] == 'DRAFT' and any(
            x.get('pricing_basis') == 'bp' for x in json.loads(draft['data_json'])['items']))
        reference = self._reference_snapshot() if need_bp else None
        with self.repo.db.transaction() as tx:
            binding = self._binding(tx, owner)
            batch = tx.execute("SELECT * FROM xj_batch WHERE batch_id=? AND owner_user_id=?", (batch_id, owner)).fetchone()
            if not batch:
                raise PermissionError("批次不存在或无权访问")
            if batch["status"] == "AUTHORIZED":
                if batch["confirm_request_id"] != request_id:
                    raise ValueError("该批次已确认，请查询原请求结果")
                return {"batch_id": batch_id, "commands": self._commands(tx, owner, batch_id)}
            if batch["version"] != expected_version or batch["binding_version"] != binding["version"]:
                raise ValueError("预览版本已失效，请重新核对")
            if batch["status"] != "DRAFT" or self.clock() - batch["created_at"] > 900:
                raise ValueError("预览已失效，请重新核对")
            data = json.loads(batch["data_json"])
            if need_bp:
                preferences = json.loads(binding['config_json'])
                for item in data['items']:
                    if item.get('pricing_basis') != 'bp':
                        continue
                    now = self._normalized_authorization(item, preferences, reference)
                    for key in ('pricing_basis', 'opening_yield_pct', 'limit_yield_pct',
                                'step_bp', 'strict_match', 'valuation_snapshot', 'bp_terms'):
                        if now.get(key) != item.get(key):
                            raise ValueError('BP 估值快照或策略已变化，必须重新预览')
            by_peer = {}
            for item in data["items"]:
                by_peer.setdefault(item["peer_id"], []).append(item)
            for peer, items in by_peer.items():
                conv = self._conversation(tx, owner, binding, peer)
                cid = conv["conversation_id"]
                tx.execute("UPDATE xj_conversation SET mode='AUTO',social_authorized=1 WHERE conversation_id=? AND owner_user_id=?", (cid, owner))
                command_id = "cmd_" + hashlib.sha256(f"{binding['account_uid']}|{batch_id}|{peer}".encode()).hexdigest()[:32]
                session_ids = []
                for item in items:
                    existing = tx.execute("SELECT session_id FROM xj_session WHERE owner_user_id=? AND conversation_id=? AND bond_code=? AND archived=0 AND state NOT IN ('STOPPED','CLOSED')", (owner,cid,item['code'])).fetchone()
                    if existing:
                        raise ValueError("该对手的该券仍在询价，请先处理原会话")
                    sid = identity("session_")
                    session_ids.append(sid)
                    tx.execute("INSERT INTO xj_session(session_id,owner_user_id,batch_id,conversation_id,bond_code,state,revision,policy_json,peer_quote_json,our_quote_json,pending_command_id,archived,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (sid, owner, batch_id, cid, item["code"], "WAITING_SEND", 1, encoded(item), "{}", "{}", command_id, 0, self.clock()))
                body = {"batch_id": batch_id, "workbench_id": binding["workbench_id"], "transport": binding["transport"],
                        "binding_version": binding["version"], "receiver_fence_id": conv.get("receiver_fence_id") or "", "receiver_fence_version": conv.get("receiver_fence_version",0), "text": "\n".join(item["text"] for item in items),
                        "session_ids": session_ids, "session_revisions": {sid: 1 for sid in session_ids}}
                tx.execute("INSERT INTO xj_command(command_id,owner_user_id,batch_id,account_uid,peer_id,conversation_id,kind,request_id,content_hash,body_json,status,control_version,created_at,expires_at,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (command_id, owner, batch_id, binding["account_uid"], peer, cid, "send_text", command_id, digest(body), encoded(body), "queued", conv["control_version"], self.clock(), self.clock() + 300, "{}"))
            tx.execute("UPDATE xj_batch SET status='AUTHORIZED',approved_at=?,confirm_request_id=? WHERE batch_id=? AND owner_user_id=?", (self.clock(), request_id, batch_id, owner))
            tx.execute("UPDATE xj_binding SET bot_enabled=1 WHERE owner_user_id=?", (owner,))
            self._audit(tx, owner, "batch_authorized", batch_id, {"request_id": request_id, "version": expected_version})
            return {"batch_id": batch_id, "commands": self._commands(tx, owner, batch_id)}

    def _commands(self, tx, owner, batch_id=None):
        sql="SELECT * FROM xj_command WHERE owner_user_id=?"
        parameters=[owner]
        if batch_id:
            sql+=' AND batch_id=?';parameters.append(batch_id)
        rows=tx.execute(sql+' ORDER BY created_at,command_id',tuple(parameters)).fetchall()
        out = []
        for row in rows:
            body = json.loads(row["body_json"])
            if batch_id and body.get("batch_id") != batch_id:
                continue
            out.append({"command_id": row["command_id"], "cmd_id": row["command_id"], "status": row["status"],
                        "account_uid": row["account_uid"], "peer_id": row["peer_id"], "conversation_id": row["conversation_id"], "content_hash": row["content_hash"], "control_version": row["control_version"], **body})
        return out

    def commands(self, owner):
        with self.repo.db.transaction(write=False) as tx:
            return self._commands(tx, owner)

    def status_commands(self,owner):
        with self.repo.db.transaction(write=False) as tx:
            rows=tx.execute("SELECT * FROM xj_command WHERE owner_user_id=? AND (status='unknown' OR command_id IN (SELECT pending_command_id FROM xj_session WHERE owner_user_id=? AND archived=0 AND pending_command_id IS NOT NULL))",(owner,owner)).fetchall()
        return [{"command_id":r['command_id'],"status":r['status'],"conversation_id":r['conversation_id'],**json.loads(r['body_json'])} for r in rows]

    def sessions(self, owner):
        with self.repo.db.transaction(write=False) as tx:
            rows = tx.execute("SELECT * FROM xj_session WHERE owner_user_id=? AND archived=0 ORDER BY created_at,session_id", (owner,)).fetchall()
        return [{**dict(row), "policy": json.loads(row["policy_json"]), "peer_quote": json.loads(row["peer_quote_json"]),
                 "our_quote": json.loads(row["our_quote_json"])} for row in rows]

    def set_bot(self, owner: str, enabled: bool):
        with self.repo.db.transaction() as tx:
            binding = self._binding(tx, owner)
            pending = tx.execute("SELECT command_id FROM xj_command WHERE owner_user_id=? AND status IN ('pending','unknown')", (owner,)).fetchall()
            if enabled and pending:
                raise ValueError("存在未决命令，须先对账")
            if bool(binding['bot_enabled']) == enabled:
                return {"status": "正常" if enabled else "停止中" if pending else "停止"}
            tx.execute("UPDATE xj_binding SET bot_enabled=?,version=version+1 WHERE owner_user_id=?", (int(enabled), owner))
            if enabled:
                # Resume only after a new per-conversation acknowledgement. A
                # global switch never replays messages received while stopped.
                pass
            if not enabled:
                tx.execute("UPDATE xj_command SET next_check_at=0 WHERE owner_user_id=? AND status IN ('pending','unknown')",(owner,))
                tx.execute("UPDATE xj_message SET processing='ignored' WHERE owner_user_id=? AND processing IN ('queued','running')",(owner,))
                tx.execute("UPDATE xj_turn SET state='cancelled' WHERE owner_user_id=? AND state='running'",(owner,))
                tx.execute("UPDATE xj_conversation SET mode=?,control_version=control_version+1 WHERE owner_user_id=?", ("STOPPING" if pending else "STOPPED", owner))
                tx.execute("UPDATE xj_command SET status='cancelled' WHERE owner_user_id=? AND status='queued'", (owner,))
                tx.execute("UPDATE xj_session SET state='STOPPED',revision=revision+1 WHERE owner_user_id=? AND state<>'CLOSED'", (owner,))
            self._audit(tx, owner, "bot_control", binding["account_uid"], {"enabled": enabled, "pending_ids": [r["command_id"] for r in pending]})
        return {"status": "正常" if enabled else "停止中" if pending else "停止"}

    def _record_sent_message(self,tx,command,evidence,native_id):
        body=json.loads(command['body_json']);mid='out_'+command['command_id']
        contexts=[]
        for sid in body.get('session_ids',[]):
            session=tx.execute('SELECT bond_code,policy_json FROM xj_session WHERE session_id=? AND owner_user_id=?',(sid,command['owner_user_id'])).fetchone()
            if session:contexts.append({'code':session['bond_code'],'name':json.loads(session['policy_json'])['name'],'session_id':sid})
        if native_id:
            echoes=tx.execute("SELECT message_id,text FROM xj_message WHERE account_uid=? AND conversation_id=? AND native_message_id=? AND direction='out' AND message_id<>? AND processing<>'duplicate'",(command['account_uid'],command['conversation_id'],native_id,mid)).fetchall()
            for echo in echoes:
                if echo['text']!=body.get('text',''):raise ValueError('原生回执ID对应的正文不一致')
                tx.execute("UPDATE xj_message SET processing='duplicate' WHERE message_id=?",(echo['message_id'],))
                self._audit(tx,command['owner_user_id'],'native_echo_correlated',command['command_id'],{'native_id':native_id,'merged_message_id':echo['message_id']})
        exists=tx.execute('SELECT message_id FROM xj_message WHERE message_id=?',(mid,)).fetchone()
        if not exists:
            tx.execute('INSERT INTO xj_message(message_id,owner_user_id,conversation_id,account_uid,source_event_id,source_seq,kind,direction,text,occurred_at,processing,data_json,native_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(mid,command['owner_user_id'],command['conversation_id'],command['account_uid'],command['command_id'],0,'message','out',body.get('text',''),self.clock(),'done',encoded({'command_id':command['command_id'],'evidence':evidence,'role':body.get('role','bot'),'contexts':contexts}),native_id))
        elif native_id:tx.execute('UPDATE xj_message SET native_message_id=? WHERE message_id=?',(native_id,mid))

    def receipt(self, command_id: str, *, account_uid: str, peer_id: str, status: str, evidence: str, native_id: str | None = None):
        if status not in ("sent", "failed", "cancelled", "unknown") or not evidence:
            raise ValueError("回执状态或证据无效")
        if native_id is not None and (not isinstance(native_id,str) or not native_id or len(native_id)>160):raise ValueError('原生消息ID无效')
        with self.repo.db.transaction() as tx:
            found = tx.execute("SELECT owner_user_id FROM xj_command WHERE command_id=?", (command_id,)).fetchone()
            if not found:
                raise ValueError("未知命令")
            owner = found["owner_user_id"]
            self._binding(tx, owner)
            command = tx.execute("SELECT * FROM xj_command WHERE command_id=?", (command_id,)).fetchone()
            if command["account_uid"] != account_uid or command["peer_id"] != peer_id:
                raise PermissionError("回执身份不匹配")
            if status=="sent":self._record_sent_message(tx,command,evidence,native_id)
            previous = command["status"]
            old_result=json.loads(command['result_json'])
            receipt_hash=digest([status,evidence])
            if old_result.get('adjudicated') and receipt_hash in old_result.get('seen_receipts',[]):return
            if previous in ('sent','failed','cancelled') and status=='unknown':
                self._audit(tx,owner,'inconclusive_late_receipt',command_id,{'evidence':evidence,'preserved_status':previous})
                return
            if previous == status:
                return
            old_result=json.loads(command['result_json'])
            terminal={'sent','failed','cancelled'}
            conflict=bool(old_result.get('conflict')) or previous in terminal and status in terminal and previous != status
            seen=list(old_result.get('seen_receipts',[]))
            if old_result.get('evidence'):seen.append(digest([old_result.get('status'),old_result['evidence']]))
            seen.append(receipt_hash)
            result = {"status": status, "evidence": evidence, "conflict": conflict, "seen_receipts":list(dict.fromkeys(seen))[-64:]}
            if conflict:
                result['conflicting_statuses']=sorted(set(old_result.get('conflicting_statuses',[])) | ({previous} if previous in terminal else set()) | ({status} if status in terminal else set()))
                tx.execute("UPDATE xj_conversation SET mode='HUMAN',control_version=control_version+1 WHERE conversation_id=? AND mode='AUTO'",(command['conversation_id'],))
                tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND state NOT IN ('STOPPED','CLOSED')",(command['conversation_id'],))
                tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued' AND command_id<>?",(command['conversation_id'],command_id))
            final = "unknown" if conflict else previous if status=='unknown' and previous in terminal else status
            if conflict:
                tx.execute("UPDATE xj_message SET processing='uncertain' WHERE message_id=?",('out_'+command_id,))
            tx.execute("UPDATE xj_command SET status=?,result_json=? WHERE command_id=?", (final, encoded(result), command_id))
            self._audit(tx, owner, "command_receipt", command_id, {**result, "previous": previous, "resolved_status": final})
            conv = tx.execute("SELECT control_version,mode FROM xj_conversation WHERE conversation_id=?", (command["conversation_id"],)).fetchone()
            if final != "sent" or not conv:
                return
            body = json.loads(command["body_json"])
            if conv["control_version"] != command["control_version"] or conv["mode"] != "AUTO":
                # Keep stopped/human session snapshots unchanged. The sent
                # command and out-message are durable; explicit resume()
                # rehydrates the price baseline from this immutable evidence.
                self._audit(tx, owner, 'late_sent_quote_fact', command_id, {
                    'session_ids': list(body.get('session_revisions', {}))})
                return
            for sid, revision in body.get("session_revisions", {}).items():
                row = tx.execute("SELECT policy_json FROM xj_session WHERE session_id=? AND owner_user_id=? AND revision=? AND pending_command_id=?", (sid, owner, revision, command_id)).fetchone()
                if row:
                    saved = json.loads(row["policy_json"])
                    quote = {"yield_pct": body.get("quote_updates", {}).get(sid, saved["opening_yield_pct"]), "source_command_id": command_id}
                    tx.execute("UPDATE xj_session SET state='NEGOTIATING',our_quote_json=?,pending_command_id=NULL,revision=revision+1 WHERE session_id=? AND owner_user_id=? AND revision=? AND pending_command_id=?",
                               (encoded(quote), sid, owner, revision, command_id))

    def conversations(self, owner):
        with self.repo.db.transaction(write=False) as tx:
            rows=tx.execute('SELECT * FROM xj_conversation WHERE owner_user_id=? ORDER BY conversation_id',(owner,)).fetchall()
        return [{**dict(row),'memory':json.loads(row['memory_json']),'ambiguity':json.loads(row['ambiguity_json'])} for row in rows]

    def ingest(self, owner, event, *, force_historical=False, receiver_fence_id=None, receiver_fence_version=0):
        if not isinstance(event,dict):raise ValueError('消息事件无效')
        if receiver_fence_id is not None and (not isinstance(receiver_fence_id,str) or not 1<=len(receiver_fence_id)<=160 or type(receiver_fence_version) is not int or receiver_fence_version<=0):raise ValueError('采集端人工屏障格式无效')
        if receiver_fence_id is None and receiver_fence_version!=0:raise ValueError('采集端人工屏障缺少标识')
        for key,maxlen in [('event_id',160),('account_uid',80),('peer_id',160)]:
            if not isinstance(event.get(key),str) or not event[key] or len(event[key])>maxlen:raise ValueError('消息标识无效')
        if type(event.get('source_seq')) is not int or event['source_seq']<0:raise ValueError('源消息序号无效')
        if not isinstance(event.get('text'),str) or len(event['text'])>20000:raise ValueError('消息正文无效')
        if type(event.get('occurred_at')) not in (int,float) or not math.isfinite(event['occurred_at']):raise ValueError('消息时间无效')
        if event.get('kind') == 'private':event = {**event,'kind':'message'}
        if event.get('kind') not in ('message','retract','edit'):raise ValueError('不支持的消息事件')
        if type(event.get('historical',False)) is not bool:raise ValueError('历史标记无效')
        native_id=event.get('native_message_id')
        if native_id is not None and (not isinstance(native_id,str) or not native_id or len(native_id)>160):raise ValueError('原生消息ID无效')
        direction=event.get('direction','in')
        if direction not in ('in','out'):raise ValueError('消息方向无效')
        with self.repo.db.transaction() as tx:
            binding=self._binding(tx,owner)
            if event['account_uid']!=binding['account_uid']:raise PermissionError('消息账号不属于本人')
            old=tx.execute('SELECT message_id,processing,data_json FROM xj_message WHERE account_uid=? AND source_event_id=?',(event['account_uid'],event['event_id'])).fetchone()
            conv=tx.execute("SELECT * FROM xj_conversation WHERE owner_user_id=? AND peer_id=? AND kind='private'",(owner,event['peer_id'])).fetchone()
            if not conv:
                cid=identity('conv_')
                tx.execute('INSERT INTO xj_conversation(conversation_id,owner_user_id,account_uid,peer_id,kind,mode,control_version,resume_seq,social_authorized,memory_revision,memory_json,ambiguity_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(cid,owner,binding['account_uid'],event['peer_id'],'private','HUMAN',1,event['source_seq'],0,1,'{}','{}'))
                conv=tx.execute('SELECT * FROM xj_conversation WHERE conversation_id=?',(cid,)).fetchone()
            cid=conv['conversation_id']
            if receiver_fence_id is not None:
                if receiver_fence_version==conv['receiver_fence_version'] and receiver_fence_id!=conv['receiver_fence_id']:raise ValueError('同版采集端人工屏障身份冲突')
                if receiver_fence_version>conv['receiver_fence_version']:
                    tx.execute("UPDATE xj_conversation SET receiver_fence_id=?,receiver_fence_version=?,mode='HUMAN',human_lock=CASE WHEN social_authorized=1 THEN 1 ELSE human_lock END,control_version=control_version+1 WHERE conversation_id=?",(receiver_fence_id,receiver_fence_version,cid))
                    tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
                    tx.execute("UPDATE xj_command SET next_check_at=0 WHERE conversation_id=? AND status IN ('pending','unknown')",(cid,))
                    tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing IN ('queued','running')",(cid,))
                    tx.execute("UPDATE xj_turn SET state='cancelled' WHERE conversation_id=? AND state='running'",(cid,))
                    tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND archived=0 AND state NOT IN ('CLOSED','STOPPED')",(cid,))
                    self._audit(tx,owner,'receiver_manual_fence',cid,{'fence_id':receiver_fence_id,'fence_version':receiver_fence_version,'source_event_id':event['event_id']})
                    conv=tx.execute('SELECT * FROM xj_conversation WHERE conversation_id=?',(cid,)).fetchone()
            if old:
                prior=json.loads(old['data_json'])
                if prior.get('event_hash')!=digest(event):raise ValueError('同一源消息ID的内容冲突，须以修订事件上报')
                return {'message_id':old['message_id'],'duplicate':True,'processing':old['processing']}
            if native_id and event['kind']=='message':
                prior_native=tx.execute("SELECT message_id,text,direction,processing FROM xj_message WHERE account_uid=? AND conversation_id=? AND native_message_id=? AND direction=? AND processing<>'duplicate'",(binding['account_uid'],cid,native_id,direction)).fetchone()
                if prior_native:
                    if prior_native['text']!=event['text']:raise ValueError('同一原生消息ID出现不同正文，须以修订事件上报')
                    return {'message_id':prior_native['message_id'],'duplicate':True,'processing':prior_native['processing']}
            high=tx.execute('SELECT MAX(source_seq) AS n FROM xj_message WHERE conversation_id=?',(cid,)).fetchone()['n'] or 0
            runnable=(direction=='in' and binding['bot_enabled'] and conv['mode']=='AUTO' and conv['social_authorized'] and not event.get('historical',False) and not force_historical and event['source_seq']>max(conv['resume_seq'],high) and event['kind']=='message')
            first_authorization=tx.execute("SELECT MIN(created_at) AS at FROM xj_session WHERE conversation_id=? AND archived=0 AND state NOT IN ('CLOSED','STOPPED')",(cid,)).fetchone()['at']
            if first_authorization is not None and event['occurred_at']<first_authorization:runnable=False
            active=tx.execute("SELECT bond_code,policy_json,created_at FROM xj_session WHERE conversation_id=? AND archived=0 AND state NOT IN ('CLOSED','STOPPED')",(cid,)).fetchall()
            mentioned=[s for s in active if s['bond_code'] in event['text'] or json.loads(s['policy_json'])['name'] in event['text']]
            relevant=mentioned if mentioned else active
            if relevant and event['occurred_at']<max(s['created_at'] for s in relevant):runnable=False
            if event['occurred_at']>self.clock()+60:runnable=False
            mid=identity('msg_');processing='queued' if runnable else 'ignored'
            tx.execute('INSERT INTO xj_message(message_id,owner_user_id,conversation_id,account_uid,source_event_id,source_seq,kind,direction,text,occurred_at,processing,data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (mid,owner,cid,binding['account_uid'],event['event_id'],event['source_seq'],event['kind'],direction,event['text'],event['occurred_at'],processing,encoded({'event_hash':digest(event),'historical':event.get('historical',False) or force_historical,'target_event_id':event.get('target_event_id')})))
            if native_id and event['kind']=='message':tx.execute('UPDATE xj_message SET native_message_id=? WHERE message_id=?',(native_id,mid))
            if runnable and _counterparty_stop_intent(event['text']):
                tx.execute("UPDATE xj_conversation SET mode='HUMAN',control_version=control_version+1 WHERE conversation_id=?",(cid,))
                tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
                tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND archived=0 AND state NOT IN ('CLOSED','STOPPED')",(cid,))
                processing='ignored'
                tx.execute("UPDATE xj_message SET processing='ignored' WHERE message_id=?",(mid,))
                self._audit(tx,owner,'counterparty_stop',cid,{'message_id':mid})
            fresh_out=(direction=='out' and not event.get('historical',False) and not force_historical and event['source_seq']>max(conv['resume_seq'],high) and (first_authorization is None or event['occurred_at']>=first_authorization) and event['occurred_at']<=self.clock()+60)
            if fresh_out and conv['mode']=='AUTO' and not (receiver_fence_version and receiver_fence_version<conv['receiver_fence_version']):
                tx.execute("UPDATE xj_conversation SET mode='HUMAN',human_lock=1,control_version=control_version+1,resume_seq=? WHERE conversation_id=?",(event['source_seq'],cid))
                tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
                tx.execute("UPDATE xj_turn SET state='cancelled' WHERE conversation_id=? AND state='running'",(cid,))
                tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing IN ('queued','running')",(cid,))
                tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND state NOT IN ('STOPPED','CLOSED')",(cid,))
                self._audit(tx,owner,'native_manual_takeover',cid,{'message_id':mid})
            if event['kind'] in ('edit','retract'):
                target=event.get('target_event_id')
                if not isinstance(target,str) or not target:raise ValueError('修订须指定原始消息ID')
                tx.execute("UPDATE xj_message SET processing='revoked' WHERE conversation_id=? AND source_event_id=?",(cid,target))
                tx.execute("UPDATE xj_conversation SET mode='HUMAN',control_version=control_version+1 WHERE conversation_id=?",(cid,))
                tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
                tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND archived=0",(cid,))
            self._audit(tx,owner,'message_ingested',mid,{'conversation_id':cid,'historical':event.get('historical',False) or force_historical,'processing':processing})
            return {'message_id':mid,'duplicate':False,'processing':processing}

    def takeover(self,owner,conversation_id):
        return self._control_conversation(owner,conversation_id,False)

    def resume(self,owner,conversation_id):
        return self._control_conversation(owner,conversation_id,True)

    def _latest_verified_sent_quote(self, tx, session):
        # Last confirmed native send for this precise session, even when its
        # ACK arrived after control-version changes. Never use model drafts,
        # sent-pending, or conflicted receipts as the next concession baseline.
        rows = tx.execute(
            "SELECT command_id,body_json,result_json FROM xj_command "
            "WHERE owner_user_id=? AND conversation_id=? AND status='sent' "
            "ORDER BY created_at DESC,command_id DESC",
            (session['owner_user_id'], session['conversation_id'])).fetchall()
        for command in rows:
            if json.loads(command['result_json']).get('conflict'):
                continue
            body = json.loads(command['body_json'])
            if session['session_id'] not in body.get('session_ids', []):
                continue
            value = body.get('quote_updates', {}).get(
                session['session_id'], json.loads(session['policy_json'])['opening_yield_pct'])
            return {'yield_pct': value, 'source_command_id': command['command_id']}
        return None

    def _control_conversation(self,owner,cid,resume):
        with self.repo.db.transaction() as tx:
            binding=self._binding(tx,owner)
            conv=tx.execute('SELECT * FROM xj_conversation WHERE conversation_id=? AND owner_user_id=?',(cid,owner)).fetchone()
            if not conv:raise PermissionError('对话不存在或无权访问')
            pending=tx.execute("SELECT command_id FROM xj_command WHERE conversation_id=? AND status IN ('pending','unknown') LIMIT 1",(cid,)).fetchone()
            if resume and pending:raise ValueError('该对话存在未决指令，须先对账')
            if resume and not binding['bot_enabled']:raise ValueError('请先明确开启机器人')
            high=tx.execute('SELECT MAX(source_seq) AS n FROM xj_message WHERE conversation_id=?',(cid,)).fetchone()['n'] or 0
            if resume:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                from .domain import goal
                sessions=tx.execute("SELECT * FROM xj_session WHERE conversation_id=? AND archived=0 AND state='HANDOFF'",(cid,)).fetchall()
                day=datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).date()
                for session in sessions:
                    if datetime.fromtimestamp(session['created_at'],ZoneInfo('Asia/Shanghai')).date()!=day:raise ValueError('跨日须重新确认本批询价策略')
                    policy=json.loads(session['policy_json']);peer=json.loads(session['peer_quote_json'])
                    if goal(policy['our_side'],policy['limit_yield_pct'],peer.get('side'),peer.get('yield_pct'))['target_reached']:raise ValueError('已达到价格目标，请人工处理，不恢复自动谈价')
                for session in sessions:
                    sent_quote=self._latest_verified_sent_quote(tx,session)
                    if sent_quote and json.loads(session['our_quote_json'])!=sent_quote:
                        tx.execute("UPDATE xj_session SET our_quote_json=?,pending_command_id=NULL "
                                   "WHERE session_id=? AND owner_user_id=?",
                                   (encoded(sent_quote),session['session_id'],owner))
                        self._audit(tx,owner,'resume_sent_quote_baseline',session['session_id'],sent_quote)
                tx.execute("UPDATE xj_session SET state='NEGOTIATING',revision=revision+1 WHERE conversation_id=? AND archived=0 AND state='HANDOFF'",(cid,))
            tx.execute("UPDATE xj_command SET next_check_at=0 WHERE conversation_id=? AND status IN ('pending','unknown')",(cid,))
            mode='AUTO' if resume else 'HUMAN'
            tx.execute('UPDATE xj_conversation SET mode=?,human_lock=?,control_version=control_version+1,resume_seq=? WHERE conversation_id=?',(mode,int(not resume),high,cid))
            tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing IN ('queued','running')",(cid,))
            tx.execute("UPDATE xj_turn SET state='cancelled' WHERE conversation_id=? AND state='running'",(cid,))
            tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
            if not resume:
                tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND archived=0 AND state NOT IN ('CLOSED','STOPPED')",(cid,))
            self._audit(tx,owner,'conversation_control',cid,{'mode':mode,'watermark':high,'unresolved':bool(pending)})
            return {'conversation_id':cid,'mode':mode,'control_version':conv['control_version']+1,'resume_seq':high,'pending':bool(pending)}

    def manual_send(self,owner,peer_id,text,request_id):
        from .public_text import SECRET_TEXT,DEAL_TEXT
        if not isinstance(peer_id,str) or not peer_id.strip() or len(peer_id)>160:raise ValueError('对手标识无效')
        if not isinstance(text,str) or not text.strip() or len(text)>1500:raise ValueError('人工消息须1至1500字')
        if not isinstance(request_id,str) or not request_id or len(request_id)>160:raise ValueError('须提供稳定请求ID')
        if SECRET_TEXT.search(text) or DEAL_TEXT.search(text):raise ValueError('本系统仅询价，不发送敏感信息或成交确认')
        fingerprint=digest({'peer_id':peer_id,'text':text})
        with self.repo.db.transaction() as tx:
            binding=self._binding(tx,owner)
            prior=tx.execute('SELECT * FROM xj_command WHERE owner_user_id=? AND request_id=?',(owner,request_id)).fetchone()
            if prior:
                body=json.loads(prior['body_json'])
                if body.get('manual_fingerprint')!=fingerprint:raise ValueError('请求ID与原人工消息冲突')
                return {'cmd_id':prior['command_id'],'status':prior['status']}
            conv=tx.execute("SELECT * FROM xj_conversation WHERE owner_user_id=? AND peer_id=? AND kind='private'",(owner,peer_id)).fetchone()
            if not conv:raise ValueError('没有已接入的本人对话')
            pending=tx.execute("SELECT command_id FROM xj_command WHERE conversation_id=? AND status IN ('pending','unknown') LIMIT 1",(conv['conversation_id'],)).fetchone()
            if pending:raise ValueError('该对话有未决命令，须先接管并完成对账')
            cid=conv['conversation_id'];version=conv['control_version']+1
            high=tx.execute('SELECT MAX(source_seq) AS n FROM xj_message WHERE conversation_id=?',(cid,)).fetchone()['n'] or 0
            tx.execute("UPDATE xj_conversation SET mode='HUMAN',human_lock=1,control_version=?,resume_seq=? WHERE conversation_id=?",(version,high,cid))
            tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
            tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing IN ('queued','running')",(cid,))
            tx.execute("UPDATE xj_turn SET state='cancelled' WHERE conversation_id=? AND state='running'",(cid,))
            tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND state NOT IN ('STOPPED','CLOSED')",(cid,))
            command_id=identity('cmd_')
            body={'workbench_id':binding['workbench_id'],'binding_version':binding['version'],'transport':binding['transport'],'receiver_fence_id':conv['receiver_fence_id'] or '', 'receiver_fence_version':conv['receiver_fence_version'],'role':'human','text':text,'manual_fingerprint':fingerprint,'session_ids':[],'session_revisions':{}}
            tx.execute('INSERT INTO xj_command(command_id,owner_user_id,account_uid,peer_id,conversation_id,kind,request_id,content_hash,body_json,status,control_version,created_at,expires_at,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(command_id,owner,binding['account_uid'],peer_id,cid,'manual_send',request_id,digest(body),encoded(body),'queued',version,self.clock(),self.clock()+120,'{}'))
            self._audit(tx,owner,'manual_send_authorized',command_id,{'conversation_id':cid,'request_id':request_id})
            return {'cmd_id':command_id,'status':'queued'}

    def stop_sessions(self,owner,codes):
        with self.repo.db.transaction() as tx:
            self._binding(tx,owner)
            selected=[]
            for code in codes:
                rows=tx.execute('SELECT session_id,conversation_id FROM xj_session WHERE owner_user_id=? AND bond_code=? AND archived=0',(owner,code)).fetchall()
                selected.extend(rows)
            # A merged command may mention several bonds; cancel the containing
            # conversation epoch, never send a stale partial merged quotation.
            for cid in {r['conversation_id'] for r in selected}:
                tx.execute("UPDATE xj_command SET next_check_at=0 WHERE conversation_id=? AND status IN ('pending','unknown')",(cid,))
                tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing IN ('queued','running')",(cid,))
                tx.execute("UPDATE xj_turn SET state='cancelled' WHERE conversation_id=? AND state='running'",(cid,))
                tx.execute("UPDATE xj_conversation SET mode='HUMAN',control_version=control_version+1 WHERE conversation_id=?",(cid,))
                tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
                tx.execute("UPDATE xj_session SET state='STOPPED',revision=revision+1 WHERE conversation_id=? AND archived=0 AND state<>'CLOSED'",(cid,))
                self._audit(tx,owner,'stop_sessions',cid,{'requested_codes':codes})
            return {'stopped':list(codes),'count':len(selected)}

    def reconcile_manually(self,owner,command_id,status,evidence,expected_result_hash,confirmed):
        if status not in ('sent','failed','cancelled') or confirmed is not True:raise ValueError('须明确核对结果并确认已经查验采集端原始记录')
        if not isinstance(evidence,str) or not 8<=len(evidence.strip())<=1000:raise ValueError('须提供8至1000字的原生记录或对账凭证引用')
        if not isinstance(expected_result_hash,str):raise ValueError('须提供当前回执版本')
        with self.repo.db.transaction() as tx:
            self._binding(tx,owner)
            row=tx.execute('SELECT * FROM xj_command WHERE command_id=? AND owner_user_id=?',(command_id,owner)).fetchone()
            if not row:raise PermissionError('命令不存在或无权访问')
            previous=json.loads(row['result_json'])
            if row['status']!='unknown' or digest(previous)!=expected_result_hash:raise ValueError('回执已变化，请刷新后重新核对')
            cid=row['conversation_id']
            high=tx.execute('SELECT MAX(source_seq) AS n FROM xj_message WHERE conversation_id=?',(cid,)).fetchone()['n'] or 0
            tx.execute("UPDATE xj_conversation SET mode=CASE WHEN mode='STOPPED' THEN 'STOPPED' ELSE 'HUMAN' END,human_lock=1,control_version=control_version+1,resume_seq=? WHERE conversation_id=?",(high,cid))
            tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing IN ('queued','running')",(cid,))
            tx.execute("UPDATE xj_turn SET state='cancelled' WHERE conversation_id=? AND state='running'",(cid,))
            tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(cid,))
            tx.execute("UPDATE xj_session SET state=CASE WHEN state IN ('STOPPED','CLOSED') THEN state ELSE 'HANDOFF' END,revision=revision+1 WHERE conversation_id=? AND archived=0",(cid,))
            resolved={**previous,'status':status,'conflict':False,'adjudicated':True,'adjudicated_by':owner,'adjudicated_at':self.clock(),'reconciliation_evidence':evidence.strip()}
            tx.execute('UPDATE xj_command SET status=?,result_json=? WHERE command_id=?',(status,encoded(resolved),command_id))
            tx.execute("UPDATE xj_session SET pending_command_id=NULL,revision=revision+1 WHERE owner_user_id=? AND pending_command_id=?",(owner,command_id))
            if status=='sent':
                mid='out_'+command_id
                existing=tx.execute('SELECT message_id FROM xj_message WHERE message_id=?',(mid,)).fetchone()
                if existing:tx.execute("UPDATE xj_message SET processing='done' WHERE message_id=?",(mid,))
                else:
                    body=json.loads(row['body_json'])
                    tx.execute('INSERT INTO xj_message(message_id,owner_user_id,conversation_id,account_uid,source_event_id,source_seq,kind,direction,text,occurred_at,processing,data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mid,owner,row['conversation_id'],row['account_uid'],command_id,0,'message','out',body.get('text',''),self.clock(),'done',encoded({'command_id':command_id,'adjudicated_by':owner,'evidence':evidence.strip()})))
            else:tx.execute("UPDATE xj_message SET processing='revoked' WHERE message_id=?",('out_'+command_id,))
            # Human reconciliation never resumes negotiation or resubmits text.
            self._audit(tx,owner,'manual_reconciliation',command_id,{'status':status,'evidence_ref':evidence.strip(),'previous_result_hash':expected_result_hash})
            return {'cmd_id':command_id,'status':status,'adjudicated':True}
