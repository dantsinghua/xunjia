"""Independent acceptance: Qidian quoted-reply context (R6-117, 2026-10-28).

Oracle = docs/implementation/2026-10-28-qidian-reply-context.md plus the
controller rulings DECISION-2 appendix A-D (T1-T13, N1-N7, C1-C3, D1-D3).
Written from the specification only; the batch implementation bodies and the
developer tests were not read. Pre-batch behaviour is taken from git HEAD.
All data is synthetic; no service, network, real Dify or IM is touched.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime
import functools
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import types
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests' / 'cutover'))

from dify.negotiation_v22 import node_code, prompts  # noqa: E402
from dify.negotiation_v22.build import build  # noqa: E402
from server.bond_tools import BondTools  # noqa: E402
from server.fresh_state import clear_workspace  # noqa: E402
from server.health import schema_health  # noqa: E402
from server.negotiation.application_store import ApplicationStore  # noqa: E402
from server.negotiation.engine import Engine  # noqa: E402
from server.negotiation.prices import InvalidFact, LimitPolicy  # noqa: E402
from server.negotiation.runtime import Inbox  # noqa: E402
from server.negotiation.schema import statements  # noqa: E402
from server.negotiation.service import Inquiry  # noqa: E402
from server.negotiation.store import Store, encoded  # noqa: E402

SH = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 10, 17, 10, tzinfo=SH).timestamp()
DAY = '2026-10-17'
OWNER = 'synthetic-reply-owner'
ACCOUNT = 'synthetic-reply-account'
WORKBENCH = 'synthetic-reply-workbench'
PEER = 'synthetic-reply-peer'
SEVEN = ('state', 'text', 'source_text', 'source_direction', 'sender_id', 'ts', 'source_ext_msg_id')
RAW_KEYS = {'source_text', 'source_direction', 'source_ext_msg_id', 'sender_id', 'reply_json'}
SENDER_MARK = 'synthetic-sender-zz9'
VERSIONS = (('0.15.3', 'chatflow_0153'), ('1.17.1', 'chatflow_1171'))
MISSING = object()

# Contract section 7, verbatim.
A_RULE = ('消息若带reply，表示该条引用了一条原消息：reply.text是原消息内容，text_is_summary=true时只是客户端摘要、可能不完整；'
          'from=out为我方发出、in为对方发出；reply.message_id若给出，即该原消息在本轮上下文中的ID。引用是上下文，不是本条新报价；'
          '引用中的价格、券名、数量归属原发送者，不能当作本条发送者的报价。判断本条报价对象时可结合引用确定指代。'
          '若本条明确采用被引用的我方已发报价（from=out且给出reply.message_id），按上文窗口内指代规则选该原消息自身的价格候选；'
          '其余情况报价数值只取本条text。slot候选、evidence和segments只来自各消息自身text，不来自reply对象。')
B_RULE = ('消息若带reply，表示该条引用了一条原消息：reply.text是原消息内容，text_is_summary=true时只是客户端摘要、可能不完整；'
          'from=out为我方发出、in为对方发出。引用是上下文，不是本条新报价；引用中的价格、券名、数量归属原发送者，'
          '只代表原消息发出时的报价，不自动成为当前报价。理解本条所说的券和价时可结合引用确定指代；'
          '对方引用我方报价并表示认可时，按上文接受语义与已发报价核验规则判断。不在回复中复述引用原文。')

# Contract section 4.1, verbatim.
MIGRATION_004 = """-- Explicit OceanBase MySQL migration; execute only in the verified xunjia_* schema.
-- Preserve a backup and SHOW CREATE TABLE xj2_message before any DDL.
-- On repeat runs, skip this ADD COLUMN if INFORMATION_SCHEMA.COLUMNS already has it:
--   SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
--   WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='xj2_message' AND COLUMN_NAME='reply_json';
-- NULL means no captured quote (no quote, legacy row, or row stored before this column). Never backfill.
ALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT NULL;"""


def reply(**changes):
    value = {'state': 'ok', 'text': 'SYN-Q 摘要3.05…', 'source_text': 'SYN-Q 3.05出800万',
             'source_direction': 'out', 'sender_id': SENDER_MARK, 'ts': '2026-10-17T09:30:00+08:00',
             'source_ext_msg_id': 'qd:100001'}
    value.update(changes)
    return value


UNPARSED = {key: None for key in SEVEN} | {'state': 'unparsed'}


# --------------------------------------------------------------------------- helpers

def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True, timeout=60).stdout


@functools.lru_cache(maxsize=None)
def head_module(relative, name):
    """Pre-batch module executed from git HEAD as a byte oracle."""
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / relative)
    exec(compile(git('show', 'HEAD:' + relative), 'HEAD:' + relative, 'exec'), module.__dict__)
    return module


def head_node_code():
    return head_module('dify/negotiation_v22/node_code.py', 'head_node_code_r6117')


def head_prompts():
    return head_module('dify/negotiation_v22/prompts.py', 'head_prompts_r6117')


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def all_keys(value):
    return {key for item in walk(value) for key in item}


def without_reply(value):
    """Remove every message-row reply (raw seven-key, None, or projected)."""
    if isinstance(value, dict):
        return {k: without_reply(v) for k, v in value.items()
                if not (k == 'reply' and (v is None or (isinstance(v, dict) and ('state' in v or 'text_is_summary' in v))))}
    if isinstance(value, list):
        return [without_reply(item) for item in value]
    return value


def assert_model_view_clean(view):
    """Section 6.1/6.3: no seven-key raw object anywhere; every reply is the projection."""
    assert not (all_keys(view) & RAW_KEYS), all_keys(view) & RAW_KEYS
    assert SENDER_MARK not in json.dumps(view, ensure_ascii=False)
    for row in walk(view):
        if 'reply' in row and isinstance(row['reply'], dict) and 'reply_requirements' not in row:
            keys = set(row['reply'])
            assert keys in ({'text', 'text_is_summary', 'from', 'ts'},
                            {'text', 'text_is_summary', 'from', 'ts', 'message_id'}), keys


def projected_rows(view):
    return [row for row in walk(view) if isinstance(row.get('reply'), dict) and 'text_is_summary' in row['reply']]


def generated(stage, version):
    doc = build(stage, version=version, provider='synthetic', model='synthetic')
    nodes = {node['id']: node['data'] for node in doc['workflow']['graph']['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    return projection['main'], contract['main']


class Desk:
    """Isolated SQLite world driven through the production Inbox path."""

    def __init__(self, path, *, reply_column=True):
        self.now = NOW
        self.path = path
        self.url = 'sqlite:///' + str(path)
        store = ApplicationStore(self.url, clock=lambda: self.now, initialize=True)
        store.bind_account(OWNER, ACCOUNT, WORKBENCH)
        if not reply_column:
            with sqlite3.connect(str(path)) as con:
                con.execute('ALTER TABLE xj2_message DROP COLUMN reply_json')
            store = ApplicationStore(self.url, clock=lambda: self.now, initialize=False)
        self.store = store
        self.group = store.create_group(OWNER, bond_code='SYN-Q', our_side='BUY', target_volume='10000',
                                        trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
        self.member, self.thread = store.add_member(OWNER, self.group, account_uid=ACCOUNT,
                                                    peer_uid=PEER, workbench_id=WORKBENCH)
        self.service = Inquiry(store)
        self.flows = SimpleNamespace(analyze=None, decide=None)
        self.engine = Engine(self.service, self.flows, BondTools(None))
        self.inbox = Inbox(self.service, None)
        self.binding = SimpleNamespace(owner_user_id=OWNER, account_uid=ACCOUNT, workbench_id=WORKBENCH, channel='qidian')
        self.seq = 0
        self.snapshots = []

    def payload(self, text='SYN-Q 早', *, value=MISSING, peer=PEER, direction='in', native=None, **extra):
        self.seq += 1
        body = {'peer_id': peer, 'event_id': 'synthetic-evt-%d' % self.seq, 'source_seq': self.seq,
                'text': text, 'direction': direction, 'ext_msg_id': native or 'qd:2000%d' % self.seq,
                'occurred_at': NOW + self.seq, 'session': {'kind': 'private'}}
        if value is not MISSING:
            body['reply'] = value
        body.update(extra)
        return body

    def envelope(self, body):
        return {'owner_user_id': OWNER, 'account_uid': ACCOUNT, 'workbench_id': WORKBENCH, 'channel': 'qidian',
                'kind': 'event', 'peer_id': body['peer_id'], 'created_at': NOW, 'payload': body}

    def apply(self, body):
        return self.inbox.apply(self.binding, self.envelope(body))

    def event(self, text='SYN-Q 早', **kwargs):
        return self.apply(self.payload(text, **kwargs))

    def row(self, message_id):
        with self.store.db.transaction(write=False) as tx:
            return dict(tx.execute('SELECT * FROM xj2_message WHERE message_id=?', (message_id,)).fetchone())

    def thread_row(self, thread=None):
        with self.store.db.transaction(write=False) as tx:
            return dict(tx.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (thread or self.thread,)).fetchone())

    def stored(self, message_id):
        raw = self.row(message_id)['reply_json']
        return None if raw is None else json.loads(raw)

    def analyze(self, observations=(), *, scopes=None, unresolved=None, corrections=None):
        def model(snapshot):
            self.snapshots.append(deepcopy(snapshot))
            value = {'schema_version': '2.2',
                     'scopes': {m['message_id']: list((scopes or {}).get(m['message_id'], [self.member]))
                                for m in snapshot['messages']},
                     'unresolved': deepcopy(unresolved or {}), 'observations': deepcopy(list(observations))}
            if corrections is not None:
                value['corrections'] = deepcopy(corrections)
            return value
        self.flows.analyze = model
        return self.engine.analyze_thread(OWNER, self.thread, force=True)

    def observation(self, ref, price, volume):
        changes = {'price': {'kind': 'YIELD', 'value': price}, 'volume_wan': volume,
                   'assertion': 'QUOTE', 'validity': 'ACTIVE', 'confirmed_day': DAY}
        return {'member_id': self.member, 'side': 'ofr', 'changes': changes,
                'evidence': {field: [ref] for field in changes}}

    def reply_context(self):
        with self.store.db.transaction(write=False) as tx:
            return self.engine._reply_context(tx, OWNER, self.member,
                                              self.service.commands_core._basis(tx, OWNER, self.member, True))


@pytest.fixture
def desk(tmp_path):
    return Desk(tmp_path / 'reply.sqlite')


# --------------------------------------------------------------------------- section 3: Inbox (T6/T7)

def test_inbox_thread_is_the_seeded_member_thread(desk):
    mid = desk.event('SYN-Q 早')
    assert desk.row(mid)['thread_id'] == desk.thread


@pytest.mark.parametrize('value', [MISSING, None, [], 'reply', 7, True, {},
                                   {k: v for k, v in reply().items() if k != 'state'},
                                   reply(state='OK'), reply(state=None), reply(state=True), reply(state=1),
                                   reply(state='parsed')],
                         ids=['missing', 'null', 'list', 'str', 'int', 'bool', 'empty', 'no_state',
                              'state_upper', 'state_null', 'state_bool', 'state_int', 'state_other'])
def test_whole_reply_is_null_when_absent_nonobject_or_bad_state(desk, value):
    """Section 3 rule 1 / T7: state missing or outside enum -> whole reply None; message still stored."""
    mid = desk.event('SYN-Q 3.10出500万', value=deepcopy(value))
    row = desk.row(mid)
    assert row['reply_json'] is None
    assert row['text'] == 'SYN-Q 3.10出500万'


def test_extra_keys_are_ignored_and_seven_keys_kept(desk):
    """Section 3 rule 2 / T7: only seven keys read; others dropped, not stored."""
    value = {**reply(), 'resolved_message_id': 'syn-resolved', 'source_msg_seq': 9, 'source_msg_uid': 'u',
             'sender': {'id': 'x', 'name': 'syn-name'}, 'eighth': 'future'}
    mid = desk.event(value=value)
    assert desk.stored(mid) == reply()
    assert desk.row(mid)['reply_json'] == encoded(reply())


@pytest.mark.parametrize('dropped', [k for k in SEVEN if k != 'state'])
def test_missing_nonstate_key_becomes_null(desk, dropped):
    value = reply()
    value.pop(dropped)
    mid = desk.event(value=value)
    assert desk.stored(mid) == {**reply(), dropped: None}


BAD_SINGLE = [
    ('text', 'x' * 2001), ('text', True), ('text', 5), ('text', ['a']),
    ('source_text', 'y' * 4001), ('source_text', 1.5), ('source_text', False),
    ('sender_id', 's' * 129), ('sender_id', 12345), ('sender_id', True),
    ('source_ext_msg_id', 'q' * 129), ('source_ext_msg_id', 100001), ('source_ext_msg_id', False),
    ('ts', 't' * 65), ('ts', 1792641600), ('ts', True),
    ('source_direction', 'IN'), ('source_direction', 'self'), ('source_direction', ''), ('source_direction', 1),
]


@pytest.mark.parametrize('key,bad', BAD_SINGLE, ids=[k + '-' + type(v).__name__ + str(len(v) if isinstance(v, str) else '')
                                                    for k, v in BAD_SINGLE])
def test_single_bad_key_is_nulled_and_the_rest_survive(desk, key, bad):
    """Section 3 rules 4/5, T6, T7: only that key None; type(x) is str; no exception."""
    mid = desk.event(value=reply(**{key: bad}))
    assert desk.stored(mid) == {**reply(), key: None}


@pytest.mark.parametrize('key,size', [('text', 2000), ('source_text', 4000), ('sender_id', 128),
                                      ('source_ext_msg_id', 128), ('ts', 64)])
def test_boundary_lengths_are_kept_verbatim_without_truncation(desk, key, size):
    value = reply(**{key: 'z' * size})
    mid = desk.event(value=value)
    assert desk.stored(mid) == value


def test_unparsed_forces_other_six_keys_null(desk):
    """Section 3 rule 3."""
    mid = desk.event(value=reply(state='unparsed'))
    assert desk.stored(mid) == UNPARSED


@pytest.mark.parametrize('value', [reply(), UNPARSED, reply(source_text=None, source_direction=None),
                                   reply(text=None, sender_id=None, ts=None, source_ext_msg_id=None),
                                   reply(source_direction='in')])
def test_valid_reply_is_stored_exactly_as_seven_key_encoded_json(desk, value):
    """Sections 3 output and 4.1: verbatim, seven keys, store.encoded() text."""
    mid = desk.event(value=deepcopy(value))
    row = desk.row(mid)
    assert row['reply_json'] == encoded(value)
    assert tuple(sorted(json.loads(row['reply_json']))) == tuple(sorted(SEVEN))
    assert value['source_text'] is None or value['source_text'] not in row['text']


def test_bad_shapes_never_quarantine_during_poll(tmp_path, monkeypatch):
    """Section 12: no isolation, no rejection, no INBOUND_QUARANTINED."""
    desk = Desk(tmp_path / 'poll.sqlite')
    shapes = [None, 'x', [1], {}, reply(state='nope'), reply(text=True), reply(ts='t' * 65),
              {**reply(), 'extra': 1}, reply(state='unparsed'), reply()]
    items = [{'envelope': desk.envelope(desk.payload('SYN-Q 第%d条' % i, value=shape)), 'source_token': 'tok-%d' % i}
             for i, shape in enumerate(shapes)]
    acked, codes = [], []

    class Adapter:
        binding = desk.binding

        def __init__(self):
            self.items = list(items)

        def poll(self, limit=50):
            out, self.items = self.items, []
            return out

        def ack(self, token):
            acked.append(token)

    original = desk.service.diagnostics.record

    def record(*args, **kwargs):
        codes.append(kwargs.get('code'))
        return original(*args, **kwargs)
    monkeypatch.setattr(desk.service.diagnostics, 'record', record)
    desk.inbox.transport = SimpleNamespace(adapters={'a': Adapter()})
    assert desk.inbox.poll_once() == len(items)
    assert acked == [item['source_token'] for item in items]
    assert 'INBOUND_QUARANTINED' not in codes and 'BUSINESS_INGEST_RETRY' not in codes
    with desk.store.db.transaction(write=False) as tx:
        assert tx.execute("SELECT COUNT(*) AS n FROM xj2_audit WHERE kind='inbound_quarantine'").fetchone()['n'] == 0
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_message WHERE thread_id=?', (desk.thread,)).fetchone()['n'] == len(items)


# --------------------------------------------------------------------------- section 4.3: content_hash, replay, T9

def head_digest(body):
    """Pre-batch content_hash formula (git HEAD store.ingest) - the reply must not enter it."""
    payload = dict(event_id=body['event_id'], source_seq=body['source_seq'], text=body['text'], trade_day=DAY,
                   occurred_at=body['occurred_at'], direction=body.get('direction', 'in'), historical=False,
                   native_id=body['ext_msg_id'], kind='message', target_event_id=None)
    return hashlib.sha256(encoded(payload).encode()).hexdigest()


def test_content_hash_ignores_reply_presence_and_content(desk):
    base = desk.payload('SYN-Q 3.10出500万')
    variants = [dict(base), {**base, 'reply': None}, {**base, 'reply': reply()},
                {**base, 'reply': reply(source_text='SYN-Q 9.99出1万')}, {**base, 'reply': UNPARSED}]
    hashes = []
    for index, body in enumerate(variants):
        peer = 'synthetic-hash-peer-%d' % index
        mid = desk.apply({**body, 'peer_id': peer})
        hashes.append(desk.row(mid)['content_hash'])
    assert len(set(hashes)) == 1
    assert hashes[0] == head_digest(base)


def test_null_reply_is_backfilled_on_same_event_replay_without_other_changes(desk):
    """Section 4.3 / T9: content_hash-equal branch backfills NULL only; no rescan, no thread change."""
    body = desk.payload('SYN-Q 3.10出500万')
    mid = desk.apply(dict(body))
    assert desk.row(mid)['reply_json'] is None
    before_row, before_thread = desk.row(mid), desk.thread_row()
    with desk.store.db.transaction(write=False) as tx:
        analyses = tx.execute('SELECT COUNT(*) AS n FROM xj2_analysis').fetchone()['n']
    assert desk.apply({**body, 'reply': reply()}) == mid
    after = desk.row(mid)
    assert json.loads(after['reply_json']) == reply()
    assert {k: v for k, v in after.items() if k != 'reply_json'} == {k: v for k, v in before_row.items() if k != 'reply_json'}
    assert desk.thread_row() == before_thread
    with desk.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_analysis').fetchone()['n'] == analyses


@pytest.mark.parametrize('second', [None, MISSING, reply(source_text='SYN-Q 其它原文'), UNPARSED])
def test_non_null_reply_is_never_rewritten_on_replay(desk, second):
    body = desk.payload('SYN-Q 3.10出500万', value=reply())
    mid = desk.apply(dict(body))
    assert desk.stored(mid) == reply()
    before_row, before_thread = desk.row(mid), desk.thread_row()
    replay = {k: v for k, v in body.items() if k != 'reply'}
    if second is not MISSING:
        replay['reply'] = second
    assert desk.apply(replay) == mid
    assert desk.row(mid) == before_row
    assert desk.thread_row() == before_thread


def test_replay_differing_only_in_reply_never_raises_different_native_content(desk):
    body = desk.payload('SYN-Q 3.10出500万', value=reply())
    mid = desk.apply(dict(body))
    for value in (reply(text='另一摘要'), None, 'garbage', reply(state='bad')):
        assert desk.apply({**body, 'reply': value}) == mid


def test_changed_native_content_still_raises_and_does_not_backfill(desk):
    body = desk.payload('SYN-Q 3.10出500万')
    mid = desk.apply(dict(body))
    before = desk.row(mid)
    with pytest.raises(InvalidFact, match='different native content'):
        desk.apply({**body, 'text': 'SYN-Q 3.11出500万', 'reply': reply()})
    assert desk.row(mid) == before


def test_outbound_echo_by_native_id_does_not_backfill(desk):
    """T9: echo branch (native_id hit on an out row) never writes reply."""
    first = desk.store.ingest(OWNER, desk.thread, event_id='synthetic-own-out', source_seq=900, text='SYN-Q 3.18收',
                              trade_day=DAY, occurred_at=NOW + 900, direction='out', native_id='qd:echo-1')
    again = desk.event('SYN-Q 3.18收', direction='out', native='qd:echo-1', value=reply())
    assert again == first
    assert desk.row(first)['reply_json'] is None


def test_retired_history_equivalent_replay_backfills_only_reply(tmp_path):
    """T9 second branch: historical equivalent replay after workspace retirement."""
    desk = Desk(tmp_path / 'retired.sqlite')
    desk.store.save_preferences(OWNER, {'context_id': 'old-context', 'context_started_at': NOW - 100})
    body = desk.payload('SYN-Q 不变原文')
    mid = desk.apply(dict(body))
    with desk.store.write(OWNER) as tx:
        clear_workspace(tx, OWNER, clock=desk.store.clock)
        tx.execute('UPDATE xj2_binding SET version=version+1,config_json=? WHERE owner_id=?',
                   (json.dumps({'context_id': 'new-context', 'context_started_at': NOW - 1}), OWNER))
    before_row, before_thread = desk.row(mid), desk.thread_row()
    assert before_row['reply_json'] is None
    replay = {**body, 'source_seq': body['source_seq'] + 100, 'historical': True, 'reply': reply()}
    assert desk.apply(replay) == mid
    after = desk.row(mid)
    assert json.loads(after['reply_json']) == reply()
    assert {k: v for k, v in after.items() if k != 'reply_json'} == {k: v for k, v in before_row.items() if k != 'reply_json'}
    assert desk.thread_row() == before_thread


@pytest.mark.parametrize('value', ['{"state":"ok"}', ['state'], 5, True])
def test_store_ingest_treats_nondict_reply_as_none(desk, value):
    mid = desk.store.ingest(OWNER, desk.thread, event_id='synthetic-direct-%s' % type(value).__name__, source_seq=700,
                            text='SYN-Q direct', trade_day=DAY, occurred_at=NOW, reply=value)
    assert desk.row(mid)['reply_json'] is None


# --------------------------------------------------------------------------- section 4.2: missing column

DEGRADED_SCRIPT = r'''
import json, logging, sqlite3, sys, io
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
root = logging.getLogger(); root.setLevel(logging.DEBUG)
records = []
class Grab(logging.Handler):
    def emit(self, record): records.append((record.levelno, record.name, record.getMessage()))
root.addHandler(Grab())
from server.negotiation.application_store import ApplicationStore
from server.negotiation.service import Inquiry
from server.negotiation.runtime import Inbox
from server.negotiation.prices import LimitPolicy
url = 'sqlite:///' + sys.argv[1]
s = ApplicationStore(url, initialize=True, clock=lambda: 1792641600.0)
s.bind_account('o', 'acc', 'wb')
with sqlite3.connect(sys.argv[1]) as con: con.execute('ALTER TABLE xj2_message DROP COLUMN reply_json')
def run(phase, count):
    store = ApplicationStore(url, initialize=False, clock=lambda: 1792641600.0)
    box = Inbox(Inquiry(store), None)
    b = SimpleNamespace(owner_user_id='o', account_uid='acc', workbench_id='wb', channel='qidian')
    ids = []
    for i in range(count):
        p = {'peer_id': 'p1', 'event_id': phase + str(i), 'source_seq': len(phase) * 100 + i, 'text': 'SYN plain',
             'ext_msg_id': 'qd:' + phase + str(i), 'occurred_at': 1792641600.0 + i,
             'reply': {'state': 'ok', 'text': 'SYN-SECRET-QUOTE', 'source_text': 'SYN-SECRET-SOURCE', 'source_direction': 'in',
                       'sender_id': 'SYN-SECRET-SENDER', 'ts': None, 'source_ext_msg_id': None}}
        ids.append(box.apply(b, {'owner_user_id': 'o', 'account_uid': 'acc', 'workbench_id': 'wb', 'channel': 'qidian',
                                 'kind': 'event', 'peer_id': 'p1', 'created_at': 1792641600.0, 'payload': p}))
    return store, ids
def reply_warnings():
    return [r for r in records if r[0] >= logging.WARNING and 'reply' in (r[1] + r[2]).lower()]
store1, ids1 = run('a', 3)
first = len(reply_warnings())
store2, ids2 = run('bb', 2)
with store2.db.transaction(write=False) as tx:
    n = tx.execute('SELECT COUNT(*) AS n FROM xj2_message').fetchone()['n']
    cols = [r['name'] for r in tx.execute('PRAGMA table_info(xj2_message)').fetchall()]
print(json.dumps({'first': first, 'total': len(reply_warnings()), 'rows': n, 'ids': ids1 + ids2,
                  'has_column': 'reply_json' in cols,
                  'leak': any('SECRET' in r[2] for r in records)}))
'''


@functools.lru_cache(maxsize=None)
def degraded_run(folder):
    db = Path(folder) / 'degraded.sqlite'
    result = subprocess.run([sys.executable, '-c', DEGRADED_SCRIPT, str(db), str(ROOT)], cwd=ROOT,
                            capture_output=True, text=True, timeout=180,
                            env={k: v for k, v in os.environ.items() if not k.startswith('XUNJIA_')})
    assert result.returncode == 0, result.stderr[-3000:]
    assert 'SECRET' not in result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_missing_column_ingest_succeeds_without_rollback_and_alerts_once(tmp_path_factory):
    """Section 4.2: inbound continues, nothing stored, alert once, alert has no quote content."""
    value = degraded_run(str(tmp_path_factory.mktemp('degraded')))
    assert value['has_column'] is False
    assert value['rows'] == 5 and all(value['ids'])
    assert value['first'] == 1
    assert value['leak'] is False


def test_missing_column_alert_is_once_per_process(tmp_path_factory):
    """Section 4.2 '进程内告警一次': a second Store instance in the same process does not re-alert."""
    value = degraded_run(str(tmp_path_factory.mktemp('degraded')))
    assert value['total'] == 1, value


def test_missing_column_reads_as_no_reply_and_projection_has_no_reply(tmp_path):
    desk = Desk(tmp_path / 'noreply.sqlite', reply_column=False)
    mid = desk.event('SYN-Q 3.10出500万', value=reply())
    assert 'reply_json' not in desk.row(mid)
    desk.analyze([])
    snapshot = desk.snapshots[-1]
    rows = [m for m in snapshot['messages'] if m['message_id'] == mid]
    assert rows and rows[0].get('reply') is None and 'reply_json' not in rows[0]
    view = node_code.compact_context({**snapshot, 'schema_version': '2.2', 'stage': 'analysis',
                                      'context_policy_revision': 'latest_quote_confirmation_v1'})
    assert 'reply' not in all_keys(view)


def test_adding_column_then_new_process_store_starts_saving(tmp_path):
    """README/section 4.2: manual SQLite ADD COLUMN TEXT, then restart (new Store) saves quotes."""
    desk = Desk(tmp_path / 'late.sqlite', reply_column=False)
    desk.event(value=reply())
    with sqlite3.connect(str(desk.path)) as con:
        con.execute('ALTER TABLE xj2_message ADD COLUMN reply_json TEXT;')
    desk.store = ApplicationStore(desk.url, clock=lambda: desk.now, initialize=False)
    desk.service = Inquiry(desk.store)
    desk.inbox = Inbox(desk.service, None)
    mid = desk.event(value=reply())
    assert desk.stored(mid) == reply()
    assert schema_health(desk.store)['code'] == 'SCHEMA_OK'


# --------------------------------------------------------------------------- section 4.4: health T1/N1/N3

def drop_reply_column(path):
    with sqlite3.connect(str(path)) as con:
        con.execute('ALTER TABLE xj2_message DROP COLUMN reply_json')


def fresh_repo(path):
    return ApplicationStore('sqlite:///' + str(path), initialize=True)


def test_full_schema_is_ok_and_warnings_key_is_always_present(tmp_path):
    report = schema_health(fresh_repo(tmp_path / 'full.db'))
    assert report['ready'] and report['code'] == 'SCHEMA_OK'
    assert 'warnings' in report  # C2
    assert 'reply_json' not in json.dumps(report['warnings'])


def test_missing_reply_column_is_only_a_warning(tmp_path):
    """T1: warning only, not missing_columns, not SCHEMA_INCOMPLETE."""
    repo = fresh_repo(tmp_path / 'missing.db')
    drop_reply_column(tmp_path / 'missing.db')
    report = schema_health(repo)
    assert report['ready'] is True and report['code'] == 'SCHEMA_OK'
    assert 'xj2_message.reply_json' not in report['missing_columns']
    assert report['missing_columns'] == [] and report['invalid_columns'] == []
    assert 'xj2_message.reply_json' in json.dumps(report['warnings'])


@pytest.mark.parametrize('column_type', ['INTEGER', 'LONGTEXT', 'VARCHAR(64)'])
def test_present_reply_column_with_wrong_type_is_still_incomplete(tmp_path, column_type):
    """N1 (and T2 note: SQLite LONGTEXT is invalid)."""
    path = tmp_path / 'wrongtype.db'
    repo = fresh_repo(path)
    drop_reply_column(path)
    with sqlite3.connect(str(path)) as con:
        con.execute('ALTER TABLE xj2_message ADD COLUMN reply_json ' + column_type)
    report = schema_health(repo)
    assert report['ready'] is False and report['code'] == 'SCHEMA_INCOMPLETE'
    assert 'xj2_message.reply_json' in report['invalid_columns']


@pytest.mark.parametrize('also_drop_reply', [False, True])
def test_other_missing_column_is_still_incomplete(tmp_path, also_drop_reply):
    path = tmp_path / 'other.db'
    repo = fresh_repo(path)
    with sqlite3.connect(str(path)) as con:
        con.execute('ALTER TABLE xj2_message RENAME COLUMN received_at TO obsolete_received_at')
    if also_drop_reply:
        drop_reply_column(path)
    report = schema_health(repo)
    assert report['ready'] is False and report['code'] == 'SCHEMA_INCOMPLETE'
    assert 'xj2_message.received_at' in report['missing_columns']
    assert 'xj2_message.reply_json' not in report['missing_columns']


def test_http_ready_is_200_and_warning_not_in_reasons(tmp_path):
    """T1 + N3."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'ready.db'), enable_background=False,
                     auth_config=AuthConfig(sso_secret='synthetic-portal',
                                            session_secret='synthetic-session-secret-at-least-32-bytes'))
    with app.state.repository.db.transaction() as tx:
        tx.execute('ALTER TABLE xj2_message DROP COLUMN reply_json')
    with TestClient(app) as client:
        response = client.get('/api/health/ready')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['ready'] and data['database_ready']
    assert 'xj2_message.reply_json' in json.dumps(data['schema']['warnings'])
    assert 'reply_json' not in json.dumps(data['reasons'])
    assert data['reasons'] == []


def manage_check(path):
    env = {k: v for k, v in os.environ.items() if not k.startswith('XUNJIA_')}
    env['XUNJIA_DATABASE_URL'] = 'sqlite:///' + str(path)
    return subprocess.run([sys.executable, '-m', 'server.manage', 'check'], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_manage_check_passes_without_reply_column_and_fails_on_other_gap(tmp_path):
    path = tmp_path / 'manage.db'
    fresh_repo(path)
    drop_reply_column(path)
    ok = manage_check(path)
    assert ok.returncode == 0, ok.stderr[-2000:]
    with sqlite3.connect(str(path)) as con:
        con.execute('ALTER TABLE xj2_message RENAME COLUMN received_at TO obsolete_received_at')
    bad = manage_check(path)
    assert bad.returncode != 0 and 'SCHEMA_INCOMPLETE' in (bad.stderr + bad.stdout)


# --------------------------------------------------------------------------- section 4.1: DDL, 004, 001, rollback

def message_ddl(mysql):
    return next(s for s in statements(mysql) if 'CREATE TABLE IF NOT EXISTS xj2_message ' in s or
                'CREATE TABLE IF NOT EXISTS xj2_message(' in s)


@pytest.mark.parametrize('mysql,kind', [(False, 'TEXT'), (True, 'LONGTEXT')])
def test_schema_declares_nullable_reply_column_before_unique(mysql, kind):
    ddl = message_ddl(mysql)
    start = ddl.index('reply_json')
    clause = ddl[start:ddl.index(',', start)]
    assert clause.split() == ['reply_json', kind]
    unique = ddl.find('UNIQUE')
    assert unique == -1 or start < unique


def test_fresh_sqlite_column_is_nullable_text_without_default(tmp_path):
    fresh_repo(tmp_path / 'fresh.db')
    with sqlite3.connect(str(tmp_path / 'fresh.db')) as con:
        cols = {r[1]: r for r in con.execute('PRAGMA table_info(xj2_message)').fetchall()}
    assert cols['reply_json'][2].upper() == 'TEXT' and cols['reply_json'][3] == 0 and cols['reply_json'][4] is None


def test_migration_004_matches_contract_verbatim():
    text = (ROOT / 'sql' / 'oceanbase' / '004_message_reply.sql').read_text(encoding='utf8')
    assert text.strip() == MIGRATION_004.strip()


def test_schema_001_regenerated_and_contains_longtext_reply():
    sql = (ROOT / 'sql' / 'oceanbase' / '001_xunjia_schema_mysql.sql').read_text(encoding='utf8')
    assert 'reply_json LONGTEXT' in sql
    result = subprocess.run([sys.executable, 'scripts/export_oceanbase_schema.py', '--check'], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]


def test_rollback_clear_statement_empties_values_and_keeps_column(desk):
    """Section 10.3 / T13 / N6."""
    ids = [desk.event('SYN-Q %d' % i, value=reply()) for i in range(3)]
    before = [{k: v for k, v in desk.row(i).items() if k != 'reply_json'} for i in ids]
    with desk.store.db.transaction() as tx:
        tx.execute('UPDATE xj2_message SET reply_json = NULL WHERE reply_json IS NOT NULL;')
    with desk.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_message WHERE reply_json IS NOT NULL;').fetchone()['n'] == 0
    assert [{k: v for k, v in desk.row(i).items() if k != 'reply_json'} for i in ids] == before
    assert all('reply_json' in desk.row(i) for i in ids)
    assert schema_health(desk.store)['code'] == 'SCHEMA_OK'


# --------------------------------------------------------------------------- section 5 + C1: read points

def message_row_in(value, message_id):
    return [row for row in walk(value) if row.get('message_id') == message_id and 'text' in row]


def assert_parsed(value, message_id, expected):
    rows = message_row_in(value, message_id)
    assert rows, 'message row is missing from this read point'
    for row in rows:
        assert row.get('reply') == expected
        assert 'reply_json' not in row


@pytest.fixture
def quoted(desk):
    origin = desk.event('SYN-Q 3.05出800万', native='qd:300001')
    quote = reply(source_ext_msg_id='qd:300001', source_direction='in', source_text='SYN-Q 3.05出800万')
    current = desk.event('SYN-Q 改3.06出800万', value=quote)
    result = desk.analyze([desk.observation(current, '3.06', '800')])
    assert result['accepted'] and not result['rejected'], result
    desk.origin, desk.current, desk.quote = origin, current, quote
    return desk


def test_snapshot_messages_carry_parsed_reply_and_persisted_snapshot_has_no_raw(quoted):
    desk = quoted
    snapshot = desk.snapshots[0]
    assert_parsed(snapshot['messages'], desk.current, desk.quote)
    assert [r for r in snapshot['messages'] if r['message_id'] == desk.origin][0].get('reply') is None
    assert 'reply_json' not in all_keys(snapshot)
    raw = desk.row(desk.current)['reply_json']
    assert raw not in json.dumps(snapshot, ensure_ascii=False)
    with desk.store.db.transaction(write=False) as tx:
        stored = [json.loads(r['snapshot_json']) for r in tx.execute('SELECT snapshot_json FROM xj2_analysis').fetchall()]
    assert stored and all('reply_json' not in all_keys(s) for s in stored)
    assert any(message_row_in(s, desk.current) and message_row_in(s, desk.current)[0].get('reply') == desk.quote
               for s in stored)


def test_member_context_history_and_b_context_carry_parsed_reply(quoted):
    desk = quoted
    desk.event('SYN-Q 再看看')
    desk.analyze([])
    assert_parsed([m['context'] for m in desk.snapshots[-1]['members']], desk.current, desk.quote)
    context = desk.reply_context()
    assert_parsed(context['context'], desk.current, desk.quote)
    assert 'reply_json' not in all_keys(context)
    history = desk.store.context(OWNER, desk.member, anchors=(desk.current,))
    assert_parsed(history, desk.current, desk.quote)
    assert 'reply_json' not in all_keys(history)


def test_unresolved_messages_carry_parsed_reply(quoted):
    desk = quoted
    # Single-member thread: a scoped priced message without an observation is
    # recorded as INVALID, which B reads through the same unresolved query.
    pending = desk.event('SYN-Q 3.20出100万', value=reply(source_ext_msg_id=None))
    result = desk.analyze([])
    assert result['facts_committed'], result
    with desk.store.db.transaction(write=False) as tx:
        relation = tx.execute('SELECT relation FROM xj2_scope WHERE message_id=?', (pending,)).fetchone()['relation']
    assert relation in ('UNRESOLVED', 'INVALID'), relation
    context = desk.reply_context()
    assert_parsed(context['unresolved_messages'], pending, reply(source_ext_msg_id=None))
    assert 'reply_json' not in all_keys(context)


def test_handoff_evidence_uses_message_row_c1(quoted):
    """C1: commands._handoff_evidence goes through Store.message_row; no raw reply_json."""
    desk = quoted
    context = desk.reply_context()
    assert_parsed(context['handoff_evidence'], desk.current, desk.quote)
    with desk.store.db.transaction(write=False) as tx:
        thread = desk.store._thread(tx, OWNER, desk.thread)
        quotes = {side: desk.store._quote(tx, desk.member, side).dump() for side in ('bid', 'ofr')}
        rows = desk.service.commands_core._handoff_evidence(tx, desk.member, quotes, {}, thread['analyzed_revision'])
    assert_parsed(rows, desk.current, desk.quote)
    assert all('reply_json' not in row for row in rows)


def test_correction_context_rows_are_parsed(quoted, monkeypatch):
    desk = quoted
    clarification = desk.event('SYN-Q 刚才那条不是这只')
    seen = []
    original = desk.engine._normalize

    def spy(snapshot, *args, **kwargs):
        seen.append(deepcopy(snapshot.get('correction_context')))
        return original(snapshot, *args, **kwargs)
    monkeypatch.setattr(desk.engine, '_normalize', spy)
    try:
        desk.analyze([], corrections=[{'message_id': desk.current, 'member_ids': [desk.member],
                                       'evidence': [clarification]}])
    except Exception:
        pass
    hydrated = [rows for rows in seen if rows]
    assert hydrated, 'correction anchor was not hydrated'
    assert_parsed(hydrated[0], desk.current, desk.quote)


@pytest.mark.parametrize('raw', [None, 'not json', '[]', '"x"', '{}', json.dumps({k: None for k in SEVEN[:6]}),
                                 json.dumps({**reply(), 'eighth': 1})])
def test_message_row_rejects_malformed_stored_text_as_none(raw):
    row = {'message_id': 'syn', 'text': 'SYN', 'reply_json': raw}
    result = Store.message_row(row)
    assert result['reply'] is None and 'reply_json' not in result and result['text'] == 'SYN'


def test_message_row_parses_valid_and_handles_missing_column():
    assert Store.message_row({'message_id': 'syn', 'reply_json': encoded(reply())})['reply'] == reply()
    missing = Store.message_row({'message_id': 'syn', 'text': 'SYN'})
    assert missing.get('reply') is None and 'reply_json' not in missing


# --------------------------------------------------------------------------- section 6: Dify projection

def row(mid, text, *, direction='in', native=None, revision=1, kind='message', value=MISSING, historical=False):
    item = {'message_id': mid, 'direction': direction, 'kind': kind, 'text': text,
            'native_id': native or 'qd:9' + mid, 'ingest_revision': revision, 'historical': historical}
    if value is not MISSING:
        item['reply'] = deepcopy(value)
    return item


def a_context(current_reply=MISSING, members_rows=None, history=None, correction=None):
    ctx = {'schema_version': '2.2', 'stage': 'analysis', 'context_policy_revision': 'latest_quote_confirmation_v1',
           'messages': [row('syn-cur', 'SYN-Q 按这个再谈', native='qd:500009', revision=9, value=current_reply)],
           'members': [{'member_id': 'syn-member', 'context': members_rows if members_rows is not None else
                        [row('syn-hit', 'SYN-Q 3.05出800万', direction='out', native='qd:100001', revision=1)]}]}
    if history is not None:
        ctx['history_results'] = history
    if correction is not None:
        ctx['correction_context'] = correction
    return ctx


def b_context(policy, current_reply=MISSING, unresolved=None, history=None, extra=None):
    ctx = {'schema_version': '2.2', 'stage': 'reply', 'member_id': 'syn-member', 'our_side': 'BUY', 'quotes': {},
           'context_policy_revision': policy, 'tool_results': [], 'history_results': history or [],
           'context': [row('syn-hit', 'SYN-Q 3.05出800万', direction='out', native='qd:100001', revision=1)] +
                      (extra or []) +
                      [row('syn-cur', 'SYN-Q 按这个再谈', native='qd:500009', revision=9, value=current_reply)],
           'unresolved_messages': unresolved or []}
    return ctx


PROD, LEGACY = 'latest_quote_confirmation_v1', 'explicit_anchors_v0'


def view_row(view, path_rows, index):
    return path_rows(view)[index]


@pytest.mark.parametrize('source_text,summary,want_text,want_summary', [
    ('SYN-Q 3.05出800万', 'SYN-Q 摘要', 'SYN-Q 3.05出800万', False),
    ('SYN-Q 3.05出800万', None, 'SYN-Q 3.05出800万', False),
    (None, 'SYN-Q 摘要', 'SYN-Q 摘要', True),
    (None, None, None, False)])
@pytest.mark.parametrize('stage', ['analysis', 'reply-prod', 'reply-legacy'])
def test_projection_text_precedence_and_summary_flag(stage, source_text, summary, want_text, want_summary):
    value = reply(source_text=source_text, text=summary)
    ctx = a_context(value) if stage == 'analysis' else b_context(PROD if stage == 'reply-prod' else LEGACY, value)
    view = node_code.compact_context(ctx)
    rows = projected_rows(view)
    assert rows
    for item in rows:
        assert item['reply']['text'] == want_text and item['reply']['text_is_summary'] is want_summary
    assert_model_view_clean(view)


@pytest.mark.parametrize('direction', ['out', 'in', None])
@pytest.mark.parametrize('stage', ['analysis', 'reply-prod'])
def test_projection_from_and_ts(stage, direction):
    value = reply(source_direction=direction, ts='2026-10-16T15:01:02+08:00')
    ctx = a_context(value) if stage == 'analysis' else b_context(PROD, value)
    rows = projected_rows(node_code.compact_context(ctx))
    assert rows and all(r['reply']['from'] == direction and r['reply']['ts'] == '2026-10-16T15:01:02+08:00' for r in rows)
    value = reply(ts=None)
    ctx = a_context(value) if stage == 'analysis' else b_context(PROD, value)
    assert all(r['reply']['ts'] is None for r in projected_rows(node_code.compact_context(ctx)))


@pytest.mark.parametrize('stage', ['analysis', 'reply-prod', 'reply-legacy'])
def test_unparsed_projection(stage):
    ctx = a_context(UNPARSED) if stage == 'analysis' else b_context(PROD if stage == 'reply-prod' else LEGACY, UNPARSED)
    rows = projected_rows(node_code.compact_context(ctx))
    assert rows and all(r['reply'] == {'text': None, 'text_is_summary': False, 'from': None, 'ts': None} for r in rows)


def alias_namespace(alias):
    assert isinstance(alias, str) and alias.startswith('m') and '_' in alias
    return alias.rsplit('_', 1)[0]


def test_a_alias_hits_member_context_row_in_same_namespace():
    view = node_code.compact_context(a_context(reply()))
    current = view['messages'][0]
    hit = view['members'][0]['context'][0]
    assert hit['message_id'] != 'syn-hit'
    assert current['reply']['message_id'] == hit['message_id']
    assert alias_namespace(current['reply']['message_id']) == alias_namespace(current['message_id'])
    assert_model_view_clean(view)


@pytest.mark.parametrize('case', ['null_id', 'zero', 'two_messages', 'edit_only'])
def test_a_alias_absent_for_zero_or_ambiguous_hits(case):
    members = [row('syn-hit', 'SYN-Q 3.05出800万', direction='out', native='qd:100001', revision=1)]
    value = reply()
    if case == 'null_id':
        value = reply(source_ext_msg_id=None)
    elif case == 'zero':
        value = reply(source_ext_msg_id='qd:not-in-context')
    elif case == 'two_messages':
        members.append(row('syn-hit-2', 'SYN-Q 3.05出800万 again', direction='out', native='qd:100001', revision=2))
    else:
        members = [row('syn-edit', 'SYN-Q 3.05出800万 改', direction='out', native='qd:100001', revision=2, kind='edit')]
    view = node_code.compact_context(a_context(value, members_rows=members))
    projected = view['messages'][0]['reply']
    assert 'message_id' not in projected
    assert projected['text'] == value['source_text']


def test_a_alias_ignores_edit_rows_sharing_native_id():
    """T11: only kind=message counts; an edit row on the same native_id does not make it ambiguous."""
    members = [row('syn-hit', 'SYN-Q 3.05出800万', direction='out', native='qd:100001', revision=1),
               row('syn-edit', 'SYN-Q 3.05出800万 改', direction='out', native='qd:100001', revision=2, kind='edit')]
    view = node_code.compact_context(a_context(reply(), members_rows=members))
    assert view['messages'][0]['reply']['message_id'] == view['members'][0]['context'][0]['message_id']


def test_a_alias_same_message_in_two_places_is_one_hit_and_history_is_covered():
    hit = row('syn-hit', 'SYN-Q 3.05出800万', direction='out', native='qd:100001', revision=1)
    anchored = row('syn-anchor', 'SYN-Q 老原文', native='qd:100777', revision=0, value=reply(), historical=True)
    history = [{'member_id': 'syn-member', 'message_ids': ['syn-hit'], 'messages': [deepcopy(hit)]},
               {'member_id': 'syn-member', 'message_ids': ['syn-anchor'], 'messages': [anchored]}]
    view = node_code.compact_context(a_context(reply(), history=history))
    alias = view['members'][0]['context'][0]['message_id']
    assert view['messages'][0]['reply']['message_id'] == alias
    projected_history = view['history_results'][1]['messages'][0]
    assert projected_history['reply']['message_id'] == alias
    assert_model_view_clean(view)


def test_a_alias_hit_in_history_only_and_correction_context():
    history_hit = row('syn-old', 'SYN-Q 3.01出100万', direction='in', native='qd:100555', revision=0, historical=True)
    view = node_code.compact_context(a_context(reply(source_ext_msg_id='qd:100555'),
                                               history=[{'member_id': 'syn-member', 'message_ids': ['syn-old'],
                                                         'messages': [history_hit]}]))
    assert view['messages'][0]['reply']['message_id'] == view['history_results'][0]['messages'][0]['message_id']
    corr = row('syn-corr', 'SYN-Q 被纠正的', native='qd:100666', revision=3, value=reply())
    ctx = a_context(reply(source_ext_msg_id='qd:100666'), correction=[corr])
    view = node_code.compact_context(ctx)
    assert 'correction_context' in view
    assert view['messages'][0]['reply']['message_id'] == view['correction_context'][0]['message_id']
    assert view['correction_context'][0]['reply']['message_id'] == view['members'][0]['context'][0]['message_id']
    assert_model_view_clean(view)


def test_a_reply_does_not_change_any_alias_or_other_view_bytes():
    """Section 6.2: reply is not hashed into the namespace; removing projected reply = HEAD view."""
    ctx = a_context(reply(), history=[{'member_id': 'syn-member', 'message_ids': ['syn-hit'],
                                       'messages': [row('syn-hit', 'SYN-Q 3.05出800万', direction='out',
                                                        native='qd:100001', revision=1, value=reply())]}])
    new = node_code.compact_context(deepcopy(ctx))
    old = head_node_code().compact_context(without_reply(deepcopy(ctx)))
    assert without_reply(new) == old


def test_production_b_has_no_message_ids_and_legacy_b_has_canonical_id():
    prod = node_code.compact_context(b_context(PROD, reply()))
    rows = projected_rows(prod)
    assert rows and all('message_id' not in r['reply'] for r in rows)
    assert all('message_id' not in r and 'native_id' not in r for r in prod['context'])
    assert_model_view_clean(prod)
    legacy = node_code.compact_context(b_context(LEGACY, reply()))
    assert projected_rows(legacy)[0]['reply']['message_id'] == 'syn-hit'
    assert_model_view_clean(legacy)


@pytest.mark.parametrize('policy', [PROD, LEGACY])
def test_b_projection_covers_unresolved_current_inbound_and_history(policy):
    anchor = row('syn-anchor', 'SYN-Q 老原文', native='qd:100777', revision=0, value=reply(), historical=True)
    unresolved = [row('syn-unres', 'SYN-Q 哪只？', native='qd:500010', revision=10, value=reply(source_direction='in'))]
    ctx = b_context(policy, reply(), unresolved=unresolved,
                    history=[{'member_id': 'syn-member', 'message_ids': ['syn-anchor'], 'messages': [anchor]}])
    view = node_code.compact_context(ctx)
    assert_model_view_clean(view)
    assert view['current_inbound_message'] is not None
    assert view['current_inbound_message']['reply']['from'] == 'in'
    assert view['unresolved_messages'][0]['reply']['text'] == reply()['source_text']
    assert view['history_results'][0]['messages'][0]['reply']['text'] == reply()['source_text']
    if policy == LEGACY:
        assert view['history_results'][0]['messages'][0]['reply']['message_id'] == 'syn-hit'
    else:
        assert all('message_id' not in r['reply'] for r in projected_rows(view))


@pytest.mark.parametrize('stage', ['analysis', PROD, LEGACY])
def test_no_reply_or_null_reply_view_is_byte_identical_to_head(stage):
    """N5 + section 12: reply None/absent -> no reply key, identical to the pre-change projection."""
    ctx = a_context(None) if stage == 'analysis' else b_context(stage, None)
    plain = without_reply(deepcopy(ctx))
    old = head_node_code().compact_context(deepcopy(plain))
    for variant in (ctx, plain):
        new = node_code.compact_context(deepcopy(variant))
        assert json.dumps(new, ensure_ascii=False, sort_keys=False) == json.dumps(old, ensure_ascii=False, sort_keys=False)
        assert 'reply' not in all_keys(new)


def test_existing_samples_without_reply_are_unchanged_from_head():
    from test_model_wire_adapter import sample
    from test_rendered_reply_plan_revision import context as reply_sample
    for ctx in (sample()[0], {**reply_sample(), 'schema_version': '2.2', 'stage': 'reply'}):
        assert node_code.compact_context(deepcopy(ctx)) == head_node_code().compact_context(deepcopy(ctx))


@pytest.mark.parametrize('stage', ['analysis', PROD, LEGACY])
def test_projection_equals_head_view_once_reply_removed(stage):
    ctx = a_context(reply()) if stage == 'analysis' else b_context(
        stage, reply(), unresolved=[row('syn-unres', 'SYN-Q 哪只？', native='qd:500010', revision=10, value=reply())])
    new = node_code.compact_context(deepcopy(ctx))
    old = head_node_code().compact_context(without_reply(deepcopy(ctx)))
    assert without_reply(new) == old


def test_slot_candidates_never_come_from_reply_text():
    """Section 6.3 last bullet."""
    ctx = a_context(reply(source_text='SYN-Z 7.77出4321万', text='SYN-Z 7.77'),
                    members_rows=[row('syn-other', 'SYN-Q 稍后', direction='out', native='qd:1', revision=1)])
    view = node_code.compact_context(ctx)
    tokens = json.dumps(view.get('slot_candidates', []), ensure_ascii=False)
    assert '7.77' not in tokens and '4321' not in tokens


def test_raw_reply_json_string_in_a_row_never_reaches_the_model_view():
    """C1 second line of defence: a stray reply_json string is not passed to the model."""
    stray = row('syn-cur', 'SYN-Q 按这个再谈', native='qd:500009', revision=9)
    stray['reply_json'] = encoded(reply())
    for ctx in ({**a_context(), 'messages': [stray]}, {**b_context(PROD), 'context': [stray]},
                {**b_context(LEGACY), 'context': [stray]}):
        view = node_code.compact_context(deepcopy(ctx))
        assert 'reply_json' not in all_keys(view)
        assert SENDER_MARK not in json.dumps(view, ensure_ascii=False)


def test_end_to_end_inbox_snapshot_to_generated_a_and_b_views(quoted):
    desk = quoted
    snapshot = desk.snapshots[0]
    a = {**snapshot, 'schema_version': '2.2', 'stage': 'analysis', 'context_policy_revision': PROD}
    b = {**desk.reply_context(), 'schema_version': '2.2', 'stage': 'reply'}
    for version, _ in VERSIONS:
        project_a, _ = generated('analysis', version)
        view_a = json.loads(project_a(json.dumps(a, ensure_ascii=False))['context'])
        assert view_a == node_code.compact_context(a)
        assert_model_view_clean(view_a)
        index = [m['message_id'] for m in snapshot['messages']]
        cur = view_a['messages'][index.index(desk.current)]
        origin = view_a['messages'][index.index(desk.origin)]
        assert cur['reply'] == {'text': 'SYN-Q 3.05出800万', 'text_is_summary': False, 'from': 'in',
                                'ts': desk.quote['ts'], 'message_id': origin['message_id']}
        assert 'reply' not in origin
        project_b, _ = generated('reply', version)
        view_b = json.loads(project_b(json.dumps(b, ensure_ascii=False))['context'])
        assert view_b == node_code.compact_context(b)
        assert_model_view_clean(view_b)
        rows = projected_rows(view_b)
        assert rows and all('message_id' not in r['reply'] for r in rows)


# --------------------------------------------------------------------------- section 7: prompts

def removed_once(text, rule):
    return {text.replace(joined, '', 1) for joined in ('\n' + rule, rule + '\n', rule) if joined in text}


def test_a_rule_in_analysis_and_direct_only():
    assert prompts.ANALYSIS.count(A_RULE) == 1 and prompts.ANALYSIS_DIRECT.count(A_RULE) == 1
    for name in ('ANALYSIS_ACK', 'ANALYSIS_REPAIR', 'REPLY', 'REPLY_NONQUOTE', 'REPLY_REPAIR'):
        assert A_RULE not in getattr(prompts, name), name
    head = head_prompts()
    assert head.ANALYSIS in removed_once(prompts.ANALYSIS, A_RULE)
    assert head.ANALYSIS_DIRECT in removed_once(prompts.ANALYSIS_DIRECT, A_RULE)
    assert prompts.ANALYSIS_ACK == head.ANALYSIS_ACK and prompts.ANALYSIS_REPAIR == head.ANALYSIS_REPAIR


def test_b_rule_in_reply_and_nonquote_only():
    assert prompts.REPLY.count(B_RULE) == 1 and prompts.REPLY_NONQUOTE.count(B_RULE) == 1
    assert B_RULE in prompts.B_PROTOCOL_NOTES
    for name in ('ANALYSIS', 'ANALYSIS_DIRECT', 'ANALYSIS_ACK', 'ANALYSIS_REPAIR', 'REPLY_REPAIR'):
        assert B_RULE not in getattr(prompts, name), name
    head = head_prompts()
    assert head.REPLY in removed_once(prompts.REPLY, B_RULE)
    assert head.REPLY_NONQUOTE in removed_once(prompts.REPLY_NONQUOTE, B_RULE)
    assert prompts.REPLY_REPAIR == head.REPLY_REPAIR


def test_a_rule_quoted_own_offer_uses_source_candidates_and_slots_stay_on_own_text():
    """Decision-2 appendix F: A rule revision."""
    head = head_prompts()
    for phrase in ('按上文窗口内指代规则选该原消息自身的价格候选',
                   'slot候选、evidence和segments只来自各消息自身text，不来自reply对象'):
        assert phrase in A_RULE
        for name in ('ANALYSIS', 'ANALYSIS_DIRECT'):
            assert getattr(prompts, name).count(phrase) == getattr(head, name).count(phrase) + 1, (name, phrase)


def test_b_rule_drops_old_price_wording_and_defers_to_acceptance_rules():
    """Decision-2 appendix F: B rule revision."""
    head = head_prompts()
    assert '旧价' not in B_RULE
    assert '按上文接受语义与已发报价核验规则判断' in B_RULE
    for name in ('B_PROTOCOL_NOTES', 'REPLY', 'REPLY_NONQUOTE'):
        value, before = getattr(prompts, name), getattr(head, name)
        assert value.count('旧价') == before.count('旧价'), name
        assert '引用里的旧价' not in value, name
        assert value.count('按上文接受语义与已发报价核验规则判断') == \
            before.count('按上文接受语义与已发报价核验规则判断') + 1, name


def test_system_routes_carry_rules_except_ack_and_repair():
    for key, value in prompts._SYSTEMS['analysis'].items():
        assert (A_RULE in value) is (key not in ('acknowledgement', 'EVIDENCE_ONLY')), key
        assert B_RULE not in value
    for key, value in prompts._SYSTEMS['reply'].items():
        assert (B_RULE in value) is (key != 'TEXT_ONLY'), key
        assert A_RULE not in value


@pytest.mark.parametrize('version,folder', VERSIONS)
def test_generated_and_saved_dsl_select_rules_per_route(version, folder):
    from test_prompt_modes import ordinary, repairing
    for stage, rule, other in (('analysis', A_RULE, B_RULE), ('reply', B_RULE, A_RULE)):
        document = yaml.safe_load((ROOT / 'dify' / folder / ('v22-' + stage + '.yml')).read_text(encoding='utf8'))
        nodes = {n['id']: n['data'] for n in document['workflow']['graph']['nodes']}
        namespace = {}
        exec(nodes['context']['code'], namespace)
        plain = namespace['main'](json.dumps(ordinary(stage), ensure_ascii=False))['system_prompt']
        repair = namespace['main'](json.dumps(repairing(stage), ensure_ascii=False))['system_prompt']
        assert plain.count(rule) == 1 and other not in plain
        assert rule not in repair and other not in repair


# --------------------------------------------------------------------------- section 8: evidence only from text

def rejected(call):
    try:
        out = call()
    except ValueError:
        return True
    observations = out.get('observations', []) if isinstance(out, dict) else []
    return any('validation_error' in o for o in observations)


@pytest.mark.parametrize('version', [v for v, _ in VERSIONS])
def test_dify_contract_rejects_segment_found_only_in_reply(version):
    from test_model_wire_adapter import sample
    context, canonical = sample()
    context['messages'][0]['reply'] = reply(source_text='SYN-C 2.95出900万', text='SYN-C 2.95出900万')
    _, contract = generated('analysis', version)
    decode = lambda value: json.loads(contract(json.dumps(value, ensure_ascii=False),
                                               json.dumps(context, ensure_ascii=False))['result'])
    assert decode(canonical) == canonical  # control: own-text evidence still passes with a reply present
    bad = deepcopy(canonical)
    bad['observations'][0]['segments'] = [{'message_id': 'in', 'text': 'SYN-C 2.95出900万'}]
    bad['observations'][0]['changes']['price'] = {'kind': 'YIELD', 'value': '2.95'}
    assert rejected(lambda: decode(bad))
    assert rejected(lambda: node_code.validate_result(deepcopy(bad), deepcopy(context), 'analysis'))


def test_dify_contract_evidence_on_quoted_original_follows_its_own_direction():
    """Section 8: citing the quoted original (an out row) grants no extra permission."""
    from test_model_wire_adapter import sample
    context, canonical = sample()
    context['messages'][0]['reply'] = reply(source_ext_msg_id='synthetic-native', source_text='SYN-A 3.10%收')
    bad = deepcopy(canonical)
    bad['observations'][0]['evidence'] = {field: ['old'] for field in bad['observations'][0]['changes']}
    bad['observations'][0]['segments'] = [{'message_id': 'old', 'text': 'SYN-A 3.10%收'}]
    _, contract = generated('analysis', '1.17.1')
    assert rejected(lambda: json.loads(contract(json.dumps(bad, ensure_ascii=False),
                                                json.dumps(context, ensure_ascii=False))['result']))


def test_server_rejects_old_price_that_only_appears_in_quote(desk):
    """Section 8: validation.py still only trusts text; quoted old price is not evidence."""
    quoted = desk.event('SYN-Q 就按这个', value=reply(source_text='SYN-Q 3.05出800万', text='SYN-Q 3.05出800万',
                                                     source_direction='out'))
    result = desk.analyze([desk.observation(quoted, '3.05', '800')])
    assert result['rejected'] and not result['accepted'], result
    assert desk.store.quote(OWNER, desk.member, 'ofr').dump().get('price') is None
    own = desk.event('SYN-Q 3.05出800万')
    result = desk.analyze([desk.observation(own, '3.05', '800')])
    assert result['accepted'] and not result['rejected'], result


# --------------------------------------------------------------------------- build artifacts, README, wheel

@pytest.mark.parametrize('version,folder', VERSIONS)
@pytest.mark.parametrize('stage', ['analysis', 'reply'])
def test_saved_yml_equals_build_and_executes_reply_projection(version, folder, stage):
    document = yaml.safe_load((ROOT / 'dify' / folder / ('v22-' + stage + '.yml')).read_text(encoding='utf8'))
    assert document == build(stage, version=version, provider='CONFIGURE_MODEL_PROVIDER', model='CONFIGURE_MODEL_NAME')
    nodes = {n['id']: n['data'] for n in document['workflow']['graph']['nodes']}
    namespace = {}
    exec(nodes['context']['code'], namespace)
    ctx = a_context(reply()) if stage == 'analysis' else b_context(PROD, reply())
    view = json.loads(namespace['main'](json.dumps(ctx, ensure_ascii=False))['context'])
    assert view == node_code.compact_context(ctx)
    assert projected_rows(view)
    assert_model_view_clean(view)


def test_readme_and_contract_carry_operator_steps():
    readme = (ROOT / 'dify' / 'negotiation_v22' / 'README.md').read_text(encoding='utf8')
    contract = (ROOT / 'docs' / 'implementation' / '2026-10-28-qidian-reply-context.md').read_text(encoding='utf8')
    statement = 'ALTER TABLE xj2_message ADD COLUMN reply_json TEXT;'
    assert statement in readme and statement in contract  # T2 / N7 (voided)
    clear = 'UPDATE xj2_message SET reply_json = NULL WHERE reply_json IS NOT NULL;'
    assert clear in readme  # T13
    # T12 / N5: import new A/B before upgrading the server (004 either side).
    rollout = next(line for line in readme.splitlines() if '上线顺序' in line)
    assert rollout.index('A/B 导入') < rollout.index('升级询价端服务端')
    # N4: stop service -> DBA clear -> deploy old server -> roll back Dify.
    back = readme[readme.index('回退（'):]
    steps = [back.index('停询价端服务'), back.index(clear), back.index('部署旧服务端代码'), back.index('再回退 Dify')]
    assert steps == sorted(steps), steps
    assert "build(stage, version=version, provider='CONFIGURE_MODEL_PROVIDER', model='CONFIGURE_MODEL_NAME')" in readme
    assert 'build_dify0153_dsl.py' not in readme  # T4
    assert '004_message_reply.sql' in readme and '重启询价端' in readme


def wheel_files():
    tree = ast.parse((ROOT / 'scripts' / 'build_collector_runtime.py').read_text(encoding='utf8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'FILES' for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError('FILES not found')


def test_changed_files_do_not_touch_the_18_wheel_files():
    files = wheel_files()
    assert len(files) == 18 and len(set(files)) == 18
    changed = set(git('diff', '--name-only', 'HEAD').split()) | set(git('ls-files', '--others', '--exclude-standard').split())
    assert not (changed & set(files)), sorted(changed & set(files))
    assert subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', *files], cwd=ROOT, timeout=60).returncode == 0
    assert subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', 'relay/protocol.py'], cwd=ROOT, timeout=60).returncode == 0
