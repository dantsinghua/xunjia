"""R6-117 Qidian quote: inbound bounds, storage, replay, context reads, health, SQL.

External oracle: docs/implementation/2026-10-28-qidian-reply-context.md sections
2-5, 10.3, 12 and DECISION-2 appendices A/B/C. All data is synthetic; only
isolated temporary SQLite files and in-memory doubles are used.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from relay.protocol import Binding, Codec, envelope
from server.app import create_app
from server.auth import AuthConfig
from server.fresh_state import clear_workspace
from server.health import contract, schema_health
from server.negotiation import store as store_module
from server.negotiation.application_store import ApplicationStore
from server.negotiation.commands import Commands
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact, LimitPolicy, Price
from server.negotiation.runtime import Inbox, _reply_wire
from server.negotiation.schema import statements
from server.negotiation.service import Inquiry
from server.negotiation.store import Store, encoded

ROOT = Path(__file__).resolve().parents[2]
NOW = 1792641600.0
DAY = '2026-10-22'
OWNER = 'synthetic-quote-owner'
KEYS = ('state', 'text', 'source_text', 'source_direction', 'sender_id', 'ts', 'source_ext_msg_id')
SECRET_TEXT = 'SYNTHETIC-QUOTE-BODY-NEVER-LOGGED'
FULL = {'state': 'ok', 'text': '摘要 3.10出', 'source_text': 'SYN-A 3.10出500万 '+SECRET_TEXT,
        'source_direction': 'in', 'sender_id': 'synthetic-uin-0001',
        'ts': '2026-10-22T09:30:00+08:00', 'source_ext_msg_id': 'qd:900001'}
NONE = object()

# Contract section 4.1, copied verbatim.
MIGRATION_004 = """-- Explicit OceanBase MySQL migration; execute only in the verified xunjia_* schema.
-- Preserve a backup and SHOW CREATE TABLE xj2_message before any DDL.
-- On repeat runs, skip this ADD COLUMN if INFORMATION_SCHEMA.COLUMNS already has it:
--   SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
--   WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='xj2_message' AND COLUMN_NAME='reply_json';
-- NULL means no captured quote (no quote, legacy row, or row stored before this column). Never backfill.
ALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT NULL;
"""
ROLLBACK = 'UPDATE xj2_message SET reply_json = NULL WHERE reply_json IS NOT NULL;'


def full(**changes):
    value = dict(FULL)
    value.update(changes)
    return value


def nulls(**changes):
    value = dict.fromkeys(KEYS)
    value.update(changes)
    return value


@pytest.fixture(autouse=True)
def fresh_warning_flag(monkeypatch):
    monkeypatch.setattr(store_module, '_reply_column_warned', False)


class Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return '\n'.join(record.getMessage() for record in self.records)


@contextmanager
def runtime_log():
    logger = logging.getLogger('xunjia.runtime')
    handler, level = Captured(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def drop_reply_column(path):
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE xj2_message DROP COLUMN reply_json')


def columns(path):
    with sqlite3.connect(path) as connection:
        return [row[1] for row in connection.execute('PRAGMA table_info(xj2_message)')]


def stored(store, message_id):
    with store.db.transaction(write=False) as tx:
        return dict(tx.execute('SELECT * FROM xj2_message WHERE message_id=?', (message_id,)).fetchone())


def stored_reply(store, message_id):
    raw = stored(store, message_id).get('reply_json')
    return None if raw is None else json.loads(raw)


def thread_row(store, thread):
    with store.db.transaction(write=False) as tx:
        return dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (thread,)).fetchone())


# ---------------------------------------------------------------- wire bounds

WIRE_CASES = [
    ('missing-key', NONE, None),
    ('null', None, None),
    ('string', json.dumps(FULL), None),
    ('list', [FULL], None),
    ('number', 7, None),
    ('bool', True, None),
    ('state-missing', {k: v for k, v in FULL.items() if k != 'state'}, None),
    ('state-null', full(state=None), None),
    ('state-uppercase', full(state='OK'), None),
    ('state-unknown', full(state='parsed'), None),
    ('state-number', full(state=1), None),
    ('valid-ok', FULL, FULL),
    ('ok-all-null', nulls(state='ok'), nulls(state='ok')),
    ('valid-unparsed', nulls(state='unparsed'), nulls(state='unparsed')),
    ('unparsed-with-values', full(state='unparsed'), nulls(state='unparsed')),
    ('extra-keys', {**FULL, 'resolved_message_id': 'internal', 'source_msg_seq': 3, 'sender': {'name': 'x'}}, FULL),
    ('missing-text', {k: v for k, v in FULL.items() if k != 'text'}, full(text=None)),
    ('missing-several', {'state': 'ok', 'ts': FULL['ts']}, nulls(state='ok', ts=FULL['ts'])),
    ('text-2000', full(text='摘'*2000), full(text='摘'*2000)),
    ('text-2001', full(text='摘'*2001), full(text=None)),
    ('source-4000', full(source_text='原'*4000), full(source_text='原'*4000)),
    ('source-4001', full(source_text='原'*4001), full(source_text=None)),
    ('sender-128', full(sender_id='u'*128), full(sender_id='u'*128)),
    ('sender-129', full(sender_id='u'*129), full(sender_id=None)),
    ('native-128', full(source_ext_msg_id='q'*128), full(source_ext_msg_id='q'*128)),
    ('native-129', full(source_ext_msg_id='q'*129), full(source_ext_msg_id=None)),
    ('ts-64', full(ts='t'*64), full(ts='t'*64)),
    ('ts-65', full(ts='t'*65), full(ts=None)),
    ('text-bool', full(text=True), full(text=None)),
    ('text-number', full(text=310), full(text=None)),
    ('source-object', full(source_text={'text': 'x'}), full(source_text=None)),
    ('sender-number', full(sender_id=10001), full(sender_id=None)),
    ('native-bool', full(source_ext_msg_id=False), full(source_ext_msg_id=None)),
    ('ts-number', full(ts=1792641600.0), full(ts=None)),
    ('direction-out', full(source_direction='out'), full(source_direction='out')),
    ('direction-null', full(source_direction=None), full(source_direction=None)),
    ('direction-upper', full(source_direction='IN'), full(source_direction=None)),
    ('direction-other', full(source_direction='self'), full(source_direction=None)),
    ('direction-bool', full(source_direction=True), full(source_direction=None)),
]


@pytest.mark.parametrize('label,value,expected', WIRE_CASES, ids=[case[0] for case in WIRE_CASES])
def test_wire_bound_function_degrades_without_raising(label, value, expected):
    argument = None if value is NONE else value
    original = deepcopy(argument)
    result = _reply_wire(argument)
    assert result == expected
    if result is not None:
        assert set(result) == set(KEYS)
    assert argument == original


def test_wire_bound_keeps_qualified_values_unchanged():
    value = full(text='  前后空白\n保持  ', source_text='\t原文\n')
    assert _reply_wire(value) == value


# ---------------------------------------------------------------- inbox end to end


class InboxDesk:
    def __init__(self, tmp_path, *, column=True):
        self.path = tmp_path/'inbox.db'
        bootstrap = ApplicationStore('sqlite:///'+str(self.path), initialize=True, clock=lambda: NOW)
        if not column:
            drop_reply_column(self.path)
        self.store = ApplicationStore('sqlite:///'+str(self.path), clock=lambda: NOW)
        self.store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
        self.binding = Binding(OWNER, 'synthetic-account', 'synthetic-workbench', b'k'*32, b's'*32)
        self.codec = Codec(self.binding, clock=lambda: NOW)
        self.inbox = Inbox(Inquiry(self.store), None)
        self.seq = 0
        del bootstrap

    def payload(self, reply=NONE, **changes):
        self.seq += 1
        value = {'event_id': 'synthetic-event-'+str(self.seq), 'source_seq': self.seq, 'text': 'SYN-A 按这个500万',
                 'direction': 'in', 'event_kind': 'message', 'ext_msg_id': 'qd:'+str(800000+self.seq),
                 'peer_id': 'synthetic-peer', 'occurred_at': NOW-100+self.seq, 'trade_day': DAY}
        if reply is not NONE:
            value['reply'] = reply
        value.update(changes)
        return value

    def apply(self, value):
        data = envelope(self.binding, 'event', value, message_id=value['event_id'], peer_id='synthetic-peer',
                        created_at=NOW, expires_at=NOW+120)
        return self.inbox.apply(self.binding, self.codec.unpack(self.codec.pack(data)))


@pytest.mark.parametrize('label,value,expected', WIRE_CASES, ids=[case[0] for case in WIRE_CASES])
def test_inbox_accepts_every_shape_and_stores_bounded_seven_keys(tmp_path, label, value, expected):
    desk = InboxDesk(tmp_path)
    message_id = desk.apply(desk.payload(value))
    assert isinstance(message_id, str) and message_id
    row = stored(desk.store, message_id)
    assert row['text'] == 'SYN-A 按这个500万'
    if expected is None:
        assert row['reply_json'] is None
    else:
        assert json.loads(row['reply_json']) == expected
        assert set(json.loads(row['reply_json'])) == set(KEYS)


def test_inbox_group_event_with_quote_still_opens_no_thread(tmp_path):
    desk = InboxDesk(tmp_path)
    group = desk.payload(FULL, peer_id='g_synthetic', session={'kind': 'group'})
    data = envelope(desk.binding, 'event', group, message_id=group['event_id'], peer_id='g_synthetic',
                    created_at=NOW, expires_at=NOW+120)
    assert desk.inbox.apply(desk.binding, desk.codec.unpack(desk.codec.pack(data))) is None
    with desk.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_message').fetchone()['n'] == 0


def test_inbox_without_column_stores_message_and_warns_once_without_quote_content(tmp_path):
    desk = InboxDesk(tmp_path, column=False)
    with runtime_log() as log:
        first = desk.apply(desk.payload(FULL))
        second = desk.apply(desk.payload(full(text='另一条')))
        third = desk.apply(desk.payload())
    for message_id in (first, second, third):
        row = stored(desk.store, message_id)
        assert 'reply_json' not in row and row['text'] == 'SYN-A 按这个500万'
    warnings = [r for r in log.records if 'REPLY_COLUMN_UNAVAILABLE' in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno >= logging.WARNING
    for secret in (SECRET_TEXT, FULL['sender_id'], FULL['source_ext_msg_id'], '摘要'):
        assert secret not in log.text()
    assert thread_row(desk.store, stored(desk.store, third)['thread_id'])['ingest_revision'] == 3


# ---------------------------------------------------------------- store ingest


class World:
    def __init__(self, path, *, column=True):
        self.path = path
        Store('sqlite:///'+str(path), clock=lambda: NOW, initialize=True)
        if not column:
            drop_reply_column(path)
        self.store = Store('sqlite:///'+str(path), clock=lambda: NOW)
        self.group = self.store.create_group(OWNER, bond_code='SYN-A', our_side='BUY', target_volume='1000',
                                             trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'))
        self.member, self.thread = self.store.add_member(OWNER, self.group, account_uid='synthetic-account',
                                                         peer_uid='synthetic-peer')

    def ingest(self, event='e-1', *, text='SYN-A 按这个500万', seq=1, reply=None, **kwargs):
        return self.store.ingest(OWNER, self.thread, event_id=event, source_seq=seq, text=text, trade_day=DAY,
                                 occurred_at=NOW-50+seq, reply=reply, **kwargs)


def test_content_hash_ignores_presence_and_content_of_quote(tmp_path):
    hashes = []
    for index, reply in enumerate([None, FULL, full(text='完全不同', source_text=None), nulls(state='unparsed')]):
        world = World(tmp_path/('hash-%d.db' % index))
        message_id = world.ingest(reply=reply)
        row = stored(world.store, message_id)
        hashes.append(row['content_hash'])
        assert (row['reply_json'] is None) == (reply is None)
    assert len(set(hashes)) == 1


def test_store_ingest_bounds_nonobject_quote_itself(tmp_path):
    world = World(tmp_path/'bounds.db')
    for seq, value in enumerate(['string', 3, [FULL], full(state='bad'), full(text=True)], start=1):
        message_id = world.ingest('e-%d' % seq, seq=seq, reply=value)
        expected = full(text=None) if isinstance(value, dict) and value.get('state') == 'ok' else None
        assert stored_reply(world.store, message_id) == expected


def test_replay_fills_null_once_and_changes_nothing_else(tmp_path):
    world = World(tmp_path/'fill.db')
    message_id = world.ingest(reply=None)
    job = world.store.claim_analysis(OWNER, world.thread, force=True)
    world.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
                                scopes={message_id: [world.member]}, observations=[])
    before, thread_before = stored(world.store, message_id), thread_row(world.store, world.thread)
    with world.store.db.transaction(write=False) as tx:
        analyses = tx.execute('SELECT COUNT(*) AS n FROM xj2_analysis').fetchone()['n']
    assert world.ingest(seq=1, reply=FULL) == message_id
    after = stored(world.store, message_id)
    assert json.loads(after.pop('reply_json')) == FULL
    before.pop('reply_json')
    assert after == before
    assert thread_row(world.store, world.thread) == thread_before
    assert world.store.claim_analysis(OWNER, world.thread, force=True) is None
    with world.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_analysis').fetchone()['n'] == analyses
    # Non-NULL is never rewritten, and a later NULL does not erase it.
    assert world.ingest(seq=1, reply=full(text='其他引用')) == message_id
    assert world.ingest(seq=1, reply=None) == message_id
    assert stored_reply(world.store, message_id) == FULL


def test_replay_with_different_quote_and_changed_sequence_never_raises(tmp_path):
    world = World(tmp_path/'seq.db')
    message_id = world.ingest(reply=FULL)
    assert world.ingest(seq=1, reply=nulls(state='unparsed')) == message_id
    assert stored_reply(world.store, message_id) == FULL


def test_rejected_replay_does_not_fill_quote(tmp_path):
    world = World(tmp_path/'reject.db')
    message_id = world.ingest(reply=None)
    with pytest.raises(InvalidFact, match='different native content'):
        world.ingest(text='SYN-A 不同正文', reply=FULL)
    assert stored(world.store, message_id)['reply_json'] is None


def test_outbound_echo_branch_does_not_fill_quote(tmp_path):
    world = World(tmp_path/'echo.db')
    original = world.ingest('e-out', direction='out', native_id='qd:700001', text='SYN-A 3.10%收', reply=None)
    echoed = world.ingest('e-echo', seq=2, direction='out', native_id='qd:700001', text='SYN-A 3.10%收', reply=FULL)
    assert echoed == original
    assert stored(world.store, original)['reply_json'] is None


def test_store_without_column_degrades_for_insert_and_replay(tmp_path):
    world = World(tmp_path/'nocolumn.db', column=False)
    with runtime_log() as log:
        message_id = world.ingest(reply=FULL)
        assert world.ingest(seq=1, reply=full(text='补写')) == message_id
        other = Store('sqlite:///'+str(world.path), clock=lambda: NOW)
        other.ingest(OWNER, world.thread, event_id='e-2', source_seq=2, text='第二条', trade_day=DAY,
                     occurred_at=NOW, reply=FULL)
    assert 'reply_json' not in columns(world.path)
    assert stored(world.store, message_id)['text'] == 'SYN-A 按这个500万'
    assert len([r for r in log.records if 'REPLY_COLUMN_UNAVAILABLE' in r.getMessage()]) == 1
    assert SECRET_TEXT not in log.text()
    job = world.store.claim_analysis(OWNER, world.thread, force=True)
    assert [row['reply'] for row in job['snapshot']['messages']] == [None, None]
    assert all('reply_json' not in row for row in job['snapshot']['messages'])


def test_mysql_column_probe_uses_show_columns(tmp_path):
    store = Store('sqlite:///'+str(tmp_path/'probe.db'), initialize=True)

    class Tx:
        mysql = True

        def __init__(self, rows):
            self.rows, self.sql = rows, []

        def execute(self, sql, args=()):
            self.sql.append(sql)
            return SimpleNamespace(fetchall=lambda: self.rows, fetchone=lambda: self.rows[0] if self.rows else None)

    present = Tx([{'Field': 'reply_json', 'Type': 'longtext'}])
    assert store._reply_available(present) is True
    assert present.sql == ["SHOW COLUMNS FROM xj2_message LIKE 'reply_json'"]
    missing_store = Store('sqlite:///'+str(tmp_path/'probe.db'))
    with runtime_log() as log:
        assert missing_store._reply_available(Tx([])) is False
        assert missing_store._reply_available(Tx([])) is False
    assert len([r for r in log.records if 'REPLY_COLUMN_UNAVAILABLE' in r.getMessage()]) == 1


# ---------------------------------------------------------------- historical-equivalent replay


def retired_fixture(tmp_path, reply):
    store = ApplicationStore('sqlite:///'+str(tmp_path/'retired.db'), initialize=True, clock=lambda: NOW)
    store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
    store.save_preferences(OWNER, {'context_id': 'old-context', 'context_started_at': NOW-100})
    group = store.create_group(OWNER, bond_code='SYN-HISTORY', our_side='BUY', target_volume='1000',
                               trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'))
    store.add_member(OWNER, group, account_uid='synthetic-account', peer_uid='synthetic-peer')
    binding = Binding(OWNER, 'synthetic-account', 'synthetic-workbench', b'k'*32, b's'*32)
    codec = Codec(binding, clock=lambda: NOW)
    inbox = Inbox(Inquiry(store), None)
    payload = {'event_id': 'native-replayed', 'source_seq': 9, 'text': 'immutable native text', 'direction': 'in',
               'event_kind': 'message', 'ext_msg_id': 'qd:600009', 'peer_id': 'synthetic-peer',
               'occurred_at': NOW-3, 'trade_day': DAY, 'historical': False, 'reply': reply}

    def apply(value):
        data = envelope(binding, 'event', value, message_id=value['event_id'], peer_id='synthetic-peer',
                        created_at=NOW, expires_at=NOW+120)
        return inbox.apply(binding, codec.unpack(codec.pack(data)))

    original = apply(payload)
    with store.write(OWNER) as tx:
        clear_workspace(tx, OWNER, clock=store.clock)
        tx.execute('UPDATE xj2_binding SET version=version+1,config_json=? WHERE owner_id=?',
                   (json.dumps({'context_id': 'new-context', 'context_started_at': NOW-1}), OWNER))
    return store, apply, payload, original


def test_historical_equivalent_replay_fills_null_quote_only(tmp_path):
    store, apply, payload, original = retired_fixture(tmp_path, None)
    before = stored(store, original)
    thread_before = thread_row(store, before['thread_id'])
    replay = {**payload, 'source_seq': 109, 'historical': True, 'reply': FULL}
    assert apply(replay) == original
    after = stored(store, original)
    assert json.loads(after.pop('reply_json')) == FULL
    before.pop('reply_json')
    assert after == before
    assert thread_row(store, before['thread_id']) == thread_before
    assert apply({**replay, 'reply': full(text='另一个')}) == original
    assert stored_reply(store, original) == FULL


def test_historical_equivalent_replay_never_overwrites_existing_quote(tmp_path):
    store, apply, payload, original = retired_fixture(tmp_path, nulls(state='unparsed'))
    assert apply({**payload, 'source_seq': 109, 'historical': True, 'reply': FULL}) == original
    assert stored_reply(store, original) == nulls(state='unparsed')


def test_historical_changed_content_still_rejected_and_not_filled(tmp_path):
    store, apply, payload, original = retired_fixture(tmp_path, None)
    with pytest.raises(InvalidFact, match='different native content'):
        apply({**payload, 'source_seq': 109, 'historical': True, 'text': 'changed', 'reply': FULL})
    assert stored(store, original)['reply_json'] is None


# ---------------------------------------------------------------- reads that reach Dify


def assert_context_row(row, expected):
    assert 'reply_json' not in row
    assert row['reply'] == expected
    assert SECRET_TEXT not in json.dumps({k: v for k, v in row.items() if k != 'reply'}, ensure_ascii=False)


@pytest.mark.parametrize('raw,expected', [
    (None, None),
    (encoded(FULL), FULL),
    (encoded(nulls(state='unparsed')), nulls(state='unparsed')),
    ('not json', None),
    ('[1, 2]', None),
    ('"text"', None),
    (encoded({k: v for k, v in FULL.items() if k != 'ts'}), None),
    (encoded({**FULL, 'extra': 1}), None),
])
def test_message_row_parses_reply_json_into_reply(raw, expected):
    row = {'message_id': 'm', 'text': 't', 'reply_json': raw}
    converted = Store.message_row(row)
    assert converted == {'message_id': 'm', 'text': 't', 'reply': expected}
    assert row['reply_json'] == raw
    assert Store.message_row({'message_id': 'm', 'text': 't'}) == {'message_id': 'm', 'text': 't', 'reply': None}


def test_snapshot_messages_member_context_and_saved_snapshot_carry_parsed_reply(tmp_path):
    world = World(tmp_path/'reads.db')
    old = world.ingest('e-old', seq=1, text='SYN-A 3.10出500万', reply=FULL)
    plain = world.ingest('e-plain', seq=2, text='SYN-A 普通', reply=None)
    job = world.store.claim_analysis(OWNER, world.thread, force=True)
    messages = {row['message_id']: row for row in job['snapshot']['messages']}
    assert_context_row(messages[old], FULL)
    assert_context_row(messages[plain], None)
    world.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
                                scopes={old: [world.member], plain: [world.member]}, observations=[])
    current = world.ingest('e-cur', seq=3, text='SYN-A 按这个', reply=nulls(state='unparsed'))
    job = world.store.claim_analysis(OWNER, world.thread, force=True)
    assert_context_row(job['snapshot']['messages'][0], nulls(state='unparsed'))
    context = {row['message_id']: row for row in job['snapshot']['members'][0]['context']}
    assert_context_row(context[old], FULL)
    assert_context_row(context[plain], None)
    with world.store.db.transaction(write=False) as tx:
        saved = tx.execute('SELECT snapshot_json FROM xj2_analysis WHERE analysis_id=?',
                           (job['analysis_id'],)).fetchone()['snapshot_json']
    assert 'reply_json' not in saved
    restored = json.loads(saved)
    assert restored['messages'][0]['reply'] == nulls(state='unparsed')
    assert {row['message_id']: row['reply'] for row in restored['members'][0]['context']}[old] == FULL
    assert current == restored['messages'][0]['message_id']


def test_history_context_and_anchor_rows_carry_parsed_reply(tmp_path):
    world = World(tmp_path/'history.db')
    anchor = world.ingest('e-anchor', seq=1, text='SYN-A 3.10%收', direction='out', native_id='qd:500001',
                          reply=full(source_direction='out'))
    later = world.ingest('e-later', seq=2, text='SYN-A 普通', reply=None)
    job = world.store.claim_analysis(OWNER, world.thread, force=True)
    world.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
                                scopes={anchor: [world.member], later: [world.member]}, observations=[])
    rows = {row['message_id']: row for row in world.store.context(OWNER, world.member, anchors=[anchor])}
    assert_context_row(rows[anchor], full(source_direction='out'))
    assert_context_row(rows[later], None)
    with world.store.db.transaction(write=False) as tx:
        explicit = world.store._history_message(tx, world.member, anchor, 2)
    assert Store.message_row(explicit)['reply'] == full(source_direction='out')


class ReplyDesk:
    """B context with a quote on the current-quote evidence and an unresolved row."""
    def __init__(self, tmp_path):
        self.now = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.store = ApplicationStore('sqlite:///'+str(tmp_path/'reply.db'), clock=lambda: self.now, initialize=True)
        self.store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
        self.store.set_sending(OWNER, True)
        self.day = '2026-10-27'
        self.commands = Commands(self.store, current_day=lambda: self.day)
        self.group = self.store.create_group(OWNER, bond_code='SYN-A', our_side='SELL', target_volume='2000',
                                             trade_day=self.day, policy=LimitPolicy('ABSOLUTE', '2.12'),
                                             send_allowed=True)
        self.member, self.thread = self.store.add_member(OWNER, self.group, account_uid='synthetic-account',
                                                         peer_uid='peer-a')
        with self.store.write(OWNER) as tx:
            tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
                       (encoded({'opening_yield_pct': '1.98', 'step_bp': '1', 'volume_wan': '2000'}), self.group))
        self.engine = Engine(SimpleNamespace(store=self.store, commands_core=self.commands,
                                             diagnostics=SimpleNamespace(record=lambda *a, **k: None)), None, None)

    def inbound(self, text, *, reply=None, quote=False, unresolved=False):
        self.now += 1
        mid = self.store.ingest(OWNER, self.thread, event_id='event-'+str(self.now), source_seq=int(self.now),
                                text=text, trade_day=self.day, occurred_at=self.now, reply=reply)
        changes = {'price': Price('YIELD', '2.12').dump(), 'volume_wan': '1000', 'assertion': 'QUOTE',
                   'validity': 'ACTIVE', 'confirmed_day': self.day}
        observations = [{'member_id': self.member, 'side': 'bid', 'changes': changes,
                         'evidence': {key: [mid] for key in changes}}] if quote else []
        job = self.store.claim_analysis(OWNER, self.thread, force=True)
        scopes = {m['message_id']: ([] if unresolved and m['message_id'] == mid else [self.member])
                  for m in job['snapshot']['messages']}
        self.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], scopes=scopes,
                                   observations=observations,
                                   unresolved={mid: [self.member]} if unresolved else None)
        return mid

    def context(self):
        with self.store.db.transaction(write=False) as tx:
            basis = self.commands._basis(tx, OWNER, self.member, True)
            return self.engine._reply_context(tx, OWNER, self.member, basis)


def test_reply_context_unresolved_and_handoff_evidence_carry_parsed_reply(tmp_path):
    desk = ReplyDesk(tmp_path)
    quote_id = desk.inbound('SYN-A 2.12收1000万', reply=FULL, quote=True)
    open_id = desk.inbound('那个呢', reply=full(source_direction='out'), unresolved=True)
    context = desk.context()
    rows = {row['message_id']: row for row in context['context']}
    assert_context_row(rows[quote_id], FULL)
    unresolved = {row['message_id']: row for row in context['unresolved_messages']}
    assert_context_row(unresolved[open_id], full(source_direction='out'))
    evidence = {row['message_id']: row for row in context['handoff_evidence']}
    assert_context_row(evidence[quote_id], FULL)
    assert 'reply_json' not in json.dumps(context, ensure_ascii=False)
    with desk.store.db.transaction(write=False) as tx:
        raw = desk.commands._handoff_evidence(tx, desk.member, context['quotes'], {}, 10**9)
    assert [row['reply'] for row in raw] == [FULL] and all('reply_json' not in row for row in raw)


def test_correction_anchor_context_carries_parsed_reply(tmp_path, monkeypatch):
    clock = [1792202400.0]
    flows = SimpleNamespace(analyze=lambda context: None, decide=lambda context: None)
    app = create_app(submission_guard=lambda _owner: None, database_url='sqlite:///'+str(tmp_path/'corr.db'),
                     enable_background=False, auth_config=AuthConfig(clock=lambda: clock[0]), chatflows=flows)
    store = app.state.repository
    store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench', transport='oss')
    group = store.create_group(OWNER, bond_code='SYN-A', our_side='BUY', target_volume='1000', trade_day=DAY,
                               policy=LimitPolicy('ABSOLUTE', '3.12'))
    member, thread = store.add_member(OWNER, group, account_uid='synthetic-account', peer_uid='synthetic-peer',
                                      workbench_id='synthetic-workbench')
    store.set_sending(OWNER, False)
    anchor = store.ingest(OWNER, thread, event_id='corr-1', source_seq=1, trade_day=DAY, text='SYN-A 3.20出500万',
                          reply=FULL)
    current = store.ingest(OWNER, thread, event_id='corr-2', source_seq=2, trade_day=DAY, text='上一条说错了',
                           reply=None)
    seen, captured = [], []

    def analyze(context):
        seen.append(deepcopy(context))
        return {'schema_version': '2.2', 'scopes': {anchor: [member], current: [member]}, 'unresolved': {},
                'observations': [], 'corrections': [{'message_id': anchor, 'member_ids': [member],
                                                     'evidence': [current]}]}
    flows.analyze = analyze
    engine = app.state.negotiation_engine
    normalize = engine._normalize

    def capture(snapshot, *args, **kwargs):
        captured.append(deepcopy(snapshot.get('correction_context')))
        return normalize(snapshot, *args, **kwargs)
    monkeypatch.setattr(engine, '_normalize', capture)
    engine.analyze_thread(OWNER, thread, force=True)
    assert seen, 'analysis model was not called'
    messages = {row['message_id']: row for row in seen[0]['messages']}
    assert messages[anchor]['reply'] == FULL and 'reply_json' not in messages[anchor]
    assert captured and captured[0] and captured[0][0]['message_id'] == anchor
    assert_context_row(captured[0][0], FULL)


# ---------------------------------------------------------------- health


def health_app(tmp_path):
    return create_app(database_url='sqlite:///'+str(tmp_path/'schema.db'), enable_background=False,
                      auth_config=AuthConfig(sso_secret='synthetic-portal',
                                             session_secret='synthetic-session-secret-at-least-32-bytes'))


def test_fresh_schema_has_reply_column_and_no_warning(tmp_path):
    store = Store('sqlite:///'+str(tmp_path/'fresh.db'), initialize=True)
    result = schema_health(SimpleNamespace(db=store.db))
    assert result['ready'] and result['code'] == 'SCHEMA_OK' and result['warnings'] == []
    assert 'reply_json' in columns(tmp_path/'fresh.db')


def test_missing_reply_column_is_warning_only_and_ready_stays_200(tmp_path):
    app = health_app(tmp_path)
    with app.state.repository.db.transaction() as tx:
        tx.execute('ALTER TABLE xj2_message DROP COLUMN reply_json')
    with TestClient(app) as client:
        response = client.get('/api/health/ready')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['ready'] and data['schema']['code'] == 'SCHEMA_OK'
    assert data['schema']['missing_columns'] == [] and data['schema']['invalid_columns'] == []
    assert any('xj2_message.reply_json' in item for item in data['schema']['warnings'])
    assert not any('reply_json' in reason for reason in data['reasons']) and data['reasons'] == []


def test_manual_sqlite_text_column_restores_clean_health_and_storage(tmp_path):
    path = tmp_path/'manual.db'
    Store('sqlite:///'+str(path), initialize=True)
    drop_reply_column(path)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE xj2_message ADD COLUMN reply_json TEXT;')
    store = Store('sqlite:///'+str(path))
    result = schema_health(SimpleNamespace(db=store.db))
    assert result['ready'] and result['warnings'] == []


def test_reply_column_with_wrong_type_is_still_schema_incomplete(tmp_path):
    path = tmp_path/'wrongtype.db'
    Store('sqlite:///'+str(path), initialize=True)
    drop_reply_column(path)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT')
    result = schema_health(SimpleNamespace(db=Store('sqlite:///'+str(path)).db))
    assert not result['ready'] and result['code'] == 'SCHEMA_INCOMPLETE'
    assert 'xj2_message.reply_json' in result['invalid_columns']


def test_other_missing_column_still_fails_even_when_reply_column_is_missing(tmp_path):
    app = health_app(tmp_path)
    with app.state.repository.db.transaction() as tx:
        tx.execute('ALTER TABLE xj2_message DROP COLUMN reply_json')
        tx.execute('ALTER TABLE xj2_message RENAME COLUMN received_at TO obsolete_received')
    with TestClient(app) as client:
        response = client.get('/api/health/ready')
    assert response.status_code == 503
    schema = response.json()['data']['schema']
    assert schema['code'] == 'SCHEMA_INCOMPLETE'
    assert schema['missing_columns'] == ['xj2_message.received_at']
    assert 'SCHEMA_INCOMPLETE' in response.json()['data']['reasons']


def manage(database, *args):
    return subprocess.run([sys.executable, '-B', '-m', 'server.manage', *args], cwd=ROOT,
                          env={**os.environ, 'XUNJIA_DATABASE_URL': 'sqlite:///'+str(database)},
                          capture_output=True, text=True, timeout=60)


def test_manage_check_passes_without_reply_column_and_fails_on_other_gap(tmp_path):
    path = tmp_path/'check.sqlite'
    ApplicationStore('sqlite:///'+str(path), initialize=True)
    drop_reply_column(path)
    result = manage(path, 'check')
    assert result.returncode == 0, result.stdout+result.stderr
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE xj2_quote')
    result = manage(path, 'check')
    assert result.returncode != 0 and 'SCHEMA_INCOMPLETE' in result.stdout+result.stderr


class MysqlMetadata:
    """In-memory OceanBase metadata double built from the generated MySQL contract."""
    def __init__(self, *, reply_type='longtext', drop=None):
        self.spec = contract(True)
        self.reply_type, self.drop = reply_type, drop
        self.mysql = True

    @contextmanager
    def transaction(self, write=False):
        outer = self

        class Tx:
            def execute(self, sql, args=()):
                rows = []
                if sql == 'SHOW TABLES':
                    rows = [{'Tables_in_xunjia': name} for name in outer.spec]
                elif sql.startswith('SHOW COLUMNS FROM '):
                    table = sql.split()[-1]
                    for name, typ in outer.spec[table]['columns'].items():
                        if table+'.'+name == outer.drop:
                            continue
                        if table+'.'+name == 'xj2_message.reply_json':
                            if outer.reply_type is None:
                                continue
                            typ = outer.reply_type
                        rows.append({'Field': name, 'Type': typ})
                elif sql.startswith('SHOW INDEX FROM '):
                    table = sql.split()[-1]
                    for n, cols in enumerate(outer.spec[table]['unique']):
                        rows.extend({'Key_name': 'unique_'+str(n), 'Seq_in_index': i+1, 'Column_name': col,
                                     'Non_unique': 0} for i, col in enumerate(cols))
                    for name, cols in outer.spec[table]['secondary'].items():
                        rows.extend({'Key_name': name, 'Seq_in_index': i+1, 'Column_name': col,
                                     'Non_unique': 1} for i, col in enumerate(cols))
                return SimpleNamespace(fetchall=lambda: rows)
        yield Tx()


@pytest.mark.parametrize('reply_type,drop,ready,where', [
    ('longtext', None, True, None),
    (None, None, True, 'warnings'),
    ('text', None, False, 'invalid_columns'),
    (None, 'xj2_message.native_id', False, 'missing_columns'),
])
def test_mysql_schema_health_reply_column_is_optional_but_typed(reply_type, drop, ready, where):
    result = schema_health(SimpleNamespace(db=MysqlMetadata(reply_type=reply_type, drop=drop)))
    assert result['ready'] is ready
    assert result['code'] == ('SCHEMA_OK' if ready else 'SCHEMA_INCOMPLETE')
    assert 'xj2_message.reply_json' not in result['missing_columns']
    if where == 'warnings':
        assert any('xj2_message.reply_json' in item for item in result['warnings'])
    elif where == 'invalid_columns':
        assert result['invalid_columns'] == ['xj2_message.reply_json']
    elif where == 'missing_columns':
        assert result['missing_columns'] == ['xj2_message.native_id']
        assert any('xj2_message.reply_json' in item for item in result['warnings'])
    else:
        assert result['warnings'] == []


# ---------------------------------------------------------------- SQL artifacts and rollback


def test_migration_004_is_the_contract_text_and_only_adds_the_column():
    path = ROOT/'sql'/'oceanbase'/'004_message_reply.sql'
    data = path.read_bytes()
    assert data.decode('ascii') == MIGRATION_004
    assert b'\r' not in data
    executable = [line for line in MIGRATION_004.splitlines() if line.strip() and not line.startswith('--')]
    assert executable == ['ALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT NULL;']


def test_migration_004_column_matches_regenerated_001_and_schema_source():
    mysql = contract(True)['xj2_message']['columns']
    sqlite = contract(False)['xj2_message']['columns']
    assert mysql['reply_json'] == 'longtext' and sqlite['reply_json'] == 'text'
    added = re.search(r'ADD COLUMN reply_json (\w+) NULL;', MIGRATION_004).group(1)
    assert added.lower() == mysql['reply_json']
    generated = (ROOT/'sql'/'oceanbase'/'001_xunjia_schema_mysql.sql').read_text(encoding='utf8')
    message = re.search(r'CREATE TABLE IF NOT EXISTS xj2_message \((.*?)ENGINE=InnoDB', generated, re.S).group(1)
    assert 'reply_json LONGTEXT, UNIQUE(thread_id,event_id)' in message
    for ddl in (statements(False), statements(True)):
        text = next(item for item in ddl if 'CREATE TABLE IF NOT EXISTS xj2_message ' in item)
        clause = re.search(r'reply_json [A-Z]+[^,]*,', text).group(0)
        assert clause in ('reply_json TEXT,', 'reply_json LONGTEXT,')
        assert text.index('reply_json') < text.index('UNIQUE(thread_id,event_id)')
    result = subprocess.run([sys.executable, '-B', 'scripts/export_oceanbase_schema.py', '--check'], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout+result.stderr


def test_rollback_statement_clears_values_and_keeps_column(tmp_path):
    world = World(tmp_path/'rollback.db')
    quoted = world.ingest('e-1', seq=1, reply=FULL)
    plain = world.ingest('e-2', seq=2, reply=None)
    before = {mid: stored(world.store, mid) for mid in (quoted, plain)}
    with sqlite3.connect(world.path) as connection:
        connection.execute(ROLLBACK)
        assert connection.execute('SELECT COUNT(*) FROM xj2_message WHERE reply_json IS NOT NULL;').fetchone()[0] == 0
    assert 'reply_json' in columns(world.path)
    for mid, row in before.items():
        row['reply_json'] = None
        assert stored(world.store, mid) == row
