"""Owner-scoped trace projection over durable facts; never polls or replays input."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .store import identity


def message_trace(store, owner, message_id):
    identity(message_id, 'message id', 64)
    with store.db.transaction(write=False) as tx:
        row = tx.execute("""SELECT m.*,t.owner_id,t.workbench_id,t.account_uid,t.peer_uid
            FROM xj2_message m JOIN xj2_thread t ON t.thread_id=m.thread_id
            WHERE m.message_id=? AND t.owner_id=?""", (message_id, owner)).fetchone()
        if row is None:
            raise LookupError('未找到本人消息的持久化记录')
        message = dict(row)
        thread = message['thread_id']
        scopes = [dict(r) for r in tx.execute("""SELECT s.member_id,s.relation,g.group_id,g.bond_code
            FROM xj2_scope s JOIN xj2_peer p ON p.member_id=s.member_id
            JOIN xj2_group g ON g.group_id=p.group_id
            WHERE s.message_id=? AND g.owner_id=? AND p.thread_id=?
            ORDER BY s.member_id""", (message_id, owner, thread)).fetchall()]
        # Revision ranges alone are insufficient: reset/history exclusions can
        # leave holes. Require the actual message in the persisted A snapshot.
        analyses = []
        for r in tx.execute("""SELECT * FROM xj2_analysis WHERE thread_id=?
            AND from_revision<? AND through_revision>=? ORDER BY created_at,analysis_id""",
            (thread, message['ingest_revision'], message['ingest_revision'])).fetchall():
            item = dict(r)
            snapshot = json.loads(item.pop('snapshot_json'))
            if not any(m.get('message_id') == message_id for m in snapshot.get('messages', [])):
                continue
            item.pop('lease_token')
            item['result'] = json.loads(item.pop('result_json'))
            analyses.append(item)
        watermarks = {a['through_revision'] for a in analyses if a['state'] == 'COMMITTED'}
        members = {s['member_id'] for s in scopes}
        decisions, replies = [], []
        for table, key, target in (('xj2_decision', 'decision_id', decisions), ('xj2_reply_job', 'job_id', replies)):
            if not members or not watermarks:
                continue
            member_ids = sorted(members)
            for r in tx.execute(f"""SELECT d.* FROM {table} d
                JOIN xj2_peer p ON p.member_id=d.member_id JOIN xj2_group g ON g.group_id=p.group_id
                WHERE p.thread_id=? AND g.owner_id=? AND d.member_id IN ({','.join('?' for _ in member_ids)})
                ORDER BY d.created_at,d.{key}""", (thread, owner, *member_ids)).fetchall():
                item = dict(r)
                basis = json.loads(item['basis_json'])
                if not isinstance(basis, dict) or item['member_id'] not in members or basis.get('observed_watermark') not in watermarks:
                    continue
                item['basis'] = json.loads(item.pop('basis_json'))
                item['result'] = json.loads(item.pop('result_json'))
                item.pop('lease_token', None)
                target.append(item)
        command_ids = {d['command_id'] for d in decisions if d.get('command_id')}
        if message['direction'] == 'out' and message['event_id'].startswith('command:'):
            command_ids.add(message['event_id'][len('command:'):])
        predicates, parameters = [], [thread, owner]
        if command_ids:
            predicates.append('c.command_id IN (' + ','.join('?' for _ in command_ids) + ')')
            parameters.extend(sorted(command_ids))
        if message['direction'] == 'out' and message['native_id']:
            predicates.append('c.native_id=?')
            parameters.append(message['native_id'])
        commands = []
        for r in tx.execute("""SELECT c.* FROM xj2_command c JOIN xj2_group g ON g.group_id=c.group_id
            WHERE c.thread_id=? AND g.owner_id=? AND (""" + (' OR '.join(predicates) or '1=0') +
            ') ORDER BY c.created_at,c.command_id', tuple(parameters)).fetchall():
            item = dict(r)
            for field in ('body', 'basis', 'result'):
                item[field] = json.loads(item.pop(field + '_json'))
            commands.append(item)
        # Outbound records point to their generating decision via command_id,
        # even though A's input watermark precedes the outbound message itself.
        for command in commands:
            for r in tx.execute("""SELECT d.* FROM xj2_decision d JOIN xj2_peer p ON p.member_id=d.member_id
                JOIN xj2_group g ON g.group_id=p.group_id WHERE d.command_id=? AND p.thread_id=? AND g.owner_id=?""",
                (command['command_id'], thread, owner)).fetchall():
                item = dict(r)
                if any(d['decision_id'] == item['decision_id'] for d in decisions):
                    continue
                item['basis'] = json.loads(item.pop('basis_json'))
                item['result'] = json.loads(item.pop('result_json'))
                decisions.append(item)
        entities = {message_id}
        for rows, key in ((analyses, 'analysis_id'), (replies, 'job_id'), (decisions, 'decision_id'), (commands, 'command_id')):
            entities.update(r[key] for r in rows)
        audits = []
        for entity in sorted(entities):
            for r in tx.execute('SELECT * FROM xj2_audit WHERE owner_id=? AND entity_id=? ORDER BY created_at,audit_id', (owner, entity)).fetchall():
                item = dict(r)
                item['data'] = json.loads(item.pop('data_json'))
                audits.append(item)

    evidence = dict(message=message, scopes=scopes, analyses=analyses, replies=replies,
                    decisions=decisions, commands=commands, audits=audits)
    steps = []

    def stamp(value):
        return datetime.fromtimestamp(value, ZoneInfo('Asia/Shanghai')).isoformat() if value is not None else '未记录'

    def add(title, desc, at, **chips):
        steps.append({'title': title, 'desc': desc, 'at': at, 'cost': '未记录', 'reached': True,
                      'chips': [{'k': '时间', 'v': stamp(at)}] + [{'k': k, 'v': str(v)} for k, v in chips.items() if v is not None]})

    add('消息已落库', '原文及原生标识已写入服务端数据库；此节点不代表已分析或已回复。',
        message['received_at'], message_id=message_id, event_id=message['event_id'],
        native_id=message['native_id'], source_seq=message['source_seq'])
    for a in analyses:
        add('分析记录', '持久化输入快照包含此消息；状态：' + a['state'], a['created_at'],
            analysis_id=a['analysis_id'], attempts=a['attempts'], committed_at=stamp(a['committed_at']),
            result=json.dumps(a['result'], ensure_ascii=False))
    for reply in replies:
        add('回复任务', '此消息所属子会话、分析水位对应的回复任务；状态：' + reply['state'], reply['created_at'],
            job_id=reply['job_id'], result=json.dumps(reply['result'], ensure_ascii=False))
    for d in decisions:
        add('决策记录', '关联同一子会话与分析水位的持久化决策；状态：' + d['state'], d['created_at'],
            decision_id=d['decision_id'], member_id=d['member_id'], result=json.dumps(d['result'], ensure_ascii=False))
    for c in commands:
        add('发送命令', '持久化命令当前状态：' + c['state'] + '；排队或待回执不代表原生发送成功。',
            c['created_at'], command_id=c['command_id'], kind=c['body'].get('kind'), text=c['body'].get('text'))
        for receipt in c['result'].get('receipts', []):
            add('发送回执', '已保存原生执行证据；回执状态：' + receipt['status'],
                c['sent_at'] if receipt['status'] == 'SENT' else None,
                command_id=c['command_id'], native_id=receipt.get('native_id'), evidence=receipt.get('evidence'))
    for audit in audits:
        add('审计记录', audit['kind'], audit['created_at'], audit_id=audit['audit_id'],
            entity_id=audit['entity_id'], data=json.dumps(audit['data'], ensure_ascii=False))
    steps.sort(key=lambda s: (s['at'] is None, s['at'] if s['at'] is not None else 0))
    for step in steps:
        step.pop('at')
    return {'id': message_id, 'text': message['text'], 'status': '已持久化',
            'summary': '读取服务端持久化证据，共 ' + str(len(steps)) + ' 条记录；仅展示已记录阶段，未记录不代表已执行。',
            'dirLabel': '发送' if message['direction'] == 'out' else '接收', 'steps': steps,
            'meta': [{'k': k, 'v': str(v)} for k, v in (
                ('工作台', message['workbench_id']), ('账号', message['account_uid']), ('对手', message['peer_uid']),
                ('线程', thread), ('原生时间', stamp(message['occurred_at'])), ('入库时间', stamp(message['received_at'])),
                ('历史消息', '是' if message['historical'] else '否'),
                ('归属', '；'.join(s['bond_code'] + ' / ' + s['relation'] for s in scopes) or '尚无归属记录'))],
            'evidence': evidence}
