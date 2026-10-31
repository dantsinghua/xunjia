"""R6-117 Qidian quote context in the Dify A/B model views; synthetic data only.

External oracles: contract docs/implementation/2026-10-28-qidian-reply-context.md
sections 6-8 and 10.1. Byte hashes of the no-quote views were produced from the
pre-R6-117 commit 1a248f2 compact_context on the same synthetic contexts.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import pytest
import yaml

from dify.negotiation_v22 import node_code, prompts
from dify.negotiation_v22.build import build
from server.negotiation.prices import InvalidFact, Price
from server.negotiation.validation import validate_observation
from test_prompt_modes import generated

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ('0.15.3', '1.17.1')
DAY = '2026-10-28'
RAW_KEYS = {'source_text', 'source_direction', 'sender_id', 'source_ext_msg_id'}
VIEW_KEYS = {'text', 'text_is_summary', 'from', 'ts', 'message_id'}
SENDER = 'synthetic-sender-uin-r6117'
ALIAS = re.compile(r'm([0-9a-f]{10})_[0-9]+')

# Contract section 7, copied verbatim (not from the implementation constant).
A_RULE = '消息若带reply，表示该条引用了一条原消息：reply.text是原消息内容，text_is_summary=true时只是客户端摘要、可能不完整；from=out为我方发出、in为对方发出；reply.message_id若给出，即该原消息在本轮上下文中的ID。引用是上下文，不是本条新报价；引用中的价格、券名、数量归属原发送者，不能当作本条发送者的报价。判断本条报价对象时可结合引用确定指代。若本条明确采用被引用的我方已发报价（from=out且给出reply.message_id），按上文窗口内指代规则选该原消息自身的价格候选；其余情况报价数值只取本条text。slot候选、evidence和segments只来自各消息自身text，不来自reply对象。'
B_RULE = '消息若带reply，表示该条引用了一条原消息：reply.text是原消息内容，text_is_summary=true时只是客户端摘要、可能不完整；from=out为我方发出、in为对方发出。引用是上下文，不是本条新报价；引用中的价格、券名、数量归属原发送者，只代表原消息发出时的报价，不自动成为当前报价。理解本条所说的券和价时可结合引用确定指代；对方引用我方报价并表示认可时，按上文接受语义与已发报价核验规则判断。不在回复中复述引用原文。'

# sha256 of json.dumps(compact_context(ctx), ensure_ascii=False, separators=(',',':'))
# computed with the 1a248f2 (pre-R6-117) node_code on the contexts below.
NO_QUOTE_VIEW_ORACLES = {
    'analysis': 'e13e2bd803a5ec4d3c6625b0154172aaf57779133b1af3cc87ef52141ee273d1',
    'reply_production': '6c2388873d4532fd2deb1cde324b4f5198a1b6f706328a82c92a4b2edf19da02',
    'reply_v0': 'cceb8c0f0b9e7c25a0763267c52f6a21e69119fb710049101901fb0a19cb2902',
}


def row(message_id, text, *, direction='in', revision=1, native=None, kind='message', target=None, historical=0):
    return {'message_id': message_id, 'thread_id': 'synthetic-thread', 'event_id': 'event-'+message_id,
            'content_hash': 'hash-'+message_id, 'source_seq': revision, 'ingest_revision': revision,
            'direction': direction, 'kind': kind, 'text': text, 'native_id': native, 'target_id': target,
            'occurred_at': 1800000000.0+revision, 'received_at': 1800000000.5+revision,
            'trade_day': DAY, 'historical': historical}


def analysis_context():
    current = row('a-cur', 'SYN-A 按这个出500万', revision=9, native='qd:8009')
    correction = row('a-corr', 'SYN-B 3.15出', revision=5, native='qd:8005')
    member_a = [row('a-old-out', 'SYN-A 3.10%收', direction='out', revision=2, native='qd:8002'),
                row('a-old-in', 'SYN-A 3.12出300万', revision=3, native='qd:8003'),
                row('a-edit', 'SYN-A 3.13出300万', revision=4, native='qd:8003', kind='edit', target='a-old-in')]
    member_b = [row('b-old', 'SYN-B 3.20出', revision=1, native='qd:8001')]
    history = row('a-hist', 'SYN-A 3.05%收', direction='out', revision=0, native='qd:8000', historical=1)
    members = [{'member_id': 'pa', 'bond_code': 'SYN-A', 'our_side': 'BUY', 'trade_day': DAY, 'version': 1,
                'control_version': 1, 'group_id': 'g-a', 'thread_id': 'synthetic-thread', 'quotes': {},
                'context': member_a},
               {'member_id': 'pb', 'bond_code': 'SYN-B', 'our_side': 'BUY', 'trade_day': DAY, 'version': 1,
                'control_version': 1, 'group_id': 'g-b', 'thread_id': 'synthetic-thread', 'quotes': {},
                'context': member_b}]
    return {'schema_version': '2.2', 'stage': 'analysis', 'thread_id': 'synthetic-thread',
            'account_epoch': 1, 'from_revision': 8, 'through_revision': 9,
            'messages': [current], 'correction_context': [correction], 'members': members,
            'history_results': [{'member_id': 'pa', 'message_ids': ['a-hist'], 'messages': [history]}]}


def reply_context(policy):
    own = row('b-own', 'SYN-Q 2.12%出', direction='out', revision=1, native='qd:7001')
    quote = row('b-quote', 'SYN-Q 2.12收1000万', revision=2, native='qd:7002')
    ask = row('b-ask', '这个位置还行吗', revision=4, native='qd:7004')
    unresolved = row('b-unres', '那个呢', revision=3, native='qd:7003')
    history = row('b-hist', 'SYN-Q 2.20出', direction='out', revision=0, native='qd:7000', historical=1)
    return {'schema_version': '2.2', 'stage': 'reply', 'context_policy_revision': policy,
            'member_id': 'member', 'thread_id': 'synthetic-thread', 'bond_code': 'SYN-Q', 'our_side': 'SELL',
            'trade_day': DAY, 'basis': {'observed_watermark': 4}, 'reconfirm_day': None,
            'boundary': {'kind': 'YIELD', 'value': '2.12'},
            'plan': {'opening_yield_pct': '1.98', 'step_bp': '1', 'volume_wan': '2000'},
            'group_summary': {'reported_shortfall_wan': '1000', 'goal_ready': False},
            'quote_constraints': {'status': 'AVAILABLE', 'price_kind': 'YIELD', 'tick_size': '0.01',
                                  'min_yield_pct': None, 'max_yield_pct': '2.12'},
            'our_quote': {'price': Price('YIELD', '2.12').dump(), 'verified_sent': True, 'command_id': 'own-command',
                          'message_id': 'b-own', 'native_id': 'qd:7001', 'sent_at': 10},
            'quotes': {'bid': {'side': 'bid', 'price': Price('YIELD', '2.12').dump(), 'volume_wan': '1000',
                               'assertion': 'QUOTE', 'validity': 'ACTIVE', 'confirmed_day': DAY,
                               'finality': 'UNKNOWN', 'revision': 1,
                               'evidence': {'price': ['b-quote'], 'volume_wan': ['b-quote']}}},
            'context': [own, quote, ask], 'unresolved_messages': [unresolved],
            'handoff_evidence': [deepcopy(own), deepcopy(quote)],
            'tool_results': [],
            'history_results': [{'member_id': 'member', 'message_ids': ['b-hist'], 'messages': [history]}]}


CONTEXTS = {'analysis': analysis_context,
            'reply_production': lambda: reply_context('latest_quote_confirmation_v1'),
            'reply_v0': lambda: reply_context('explicit_anchors_v0')}


def quote(**values):
    value = {'state': 'ok', 'text': None, 'source_text': None, 'source_direction': None,
             'sender_id': SENDER, 'ts': '2026-10-28T09:30:00+08:00', 'source_ext_msg_id': None}
    value.update(values)
    return value


def unparsed():
    return dict.fromkeys(('state', 'text', 'source_text', 'source_direction', 'sender_id', 'ts', 'source_ext_msg_id'),
                         None) | {'state': 'unparsed'}


def message_rows(context):
    """Every message row that the server may hand to the projection."""
    rows = list(context.get('messages', [])) + list(context.get('correction_context', []))
    for member in context.get('members', []):
        rows.extend(member.get('context', []))
    rows.extend(context.get('context', []))
    rows.extend(context.get('unresolved_messages', []))
    rows.extend(context.get('handoff_evidence', []))
    for history in context.get('history_results', []):
        rows.extend(history.get('messages', []))
    return rows


def dumped(view):
    return json.dumps(view, ensure_ascii=False, separators=(',', ':'))


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def keys(value):
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= keys(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= keys(item)
        return found
    return set()


def replies(value):
    """All projected reply sub-objects anywhere in a view."""
    found = []
    if isinstance(value, dict):
        if isinstance(value.get('reply'), dict):
            found.append(value['reply'])
        for key, item in value.items():
            found.extend(replies(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(replies(item))
    return found


def assert_no_raw_quote(view):
    assert not RAW_KEYS & keys(view), RAW_KEYS & keys(view)
    assert 'reply_json' not in keys(view)
    assert SENDER not in dumped(view)
    for projected in replies(view):
        assert set(projected) <= VIEW_KEYS and {'text', 'text_is_summary', 'from', 'ts'} <= set(projected)


def strip_reply(value):
    if isinstance(value, dict):
        return {key: strip_reply(item) for key, item in value.items() if key != 'reply'}
    if isinstance(value, list):
        return [strip_reply(item) for item in value]
    return value


def by_message(view_rows, context_rows, message_id):
    """View row at the same position as the canonical row (A aliases hide the ID)."""
    index = [item['message_id'] for item in context_rows].index(message_id)
    return view_rows[index]


# ---------------------------------------------------------------- no quote


@pytest.mark.parametrize('label', sorted(CONTEXTS))
def test_no_quote_view_is_byte_identical_to_pre_r6117_projection(label):
    context = CONTEXTS[label]()
    assert sha(dumped(node_code.compact_context(context))) == NO_QUOTE_VIEW_ORACLES[label]
    with_none = CONTEXTS[label]()
    for item in message_rows(with_none):
        item['reply'] = None
    before = deepcopy(with_none)
    assert sha(dumped(node_code.compact_context(with_none))) == NO_QUOTE_VIEW_ORACLES[label]
    assert with_none == before


@pytest.mark.parametrize('version', VERSIONS)
@pytest.mark.parametrize('label', sorted(CONTEXTS))
def test_generated_projection_node_keeps_no_quote_bytes(version, label):
    context = CONTEXTS[label]()
    for item in message_rows(context):
        item['reply'] = None
    _, _, project, _ = generated(context['stage'], version)
    assert sha(project(json.dumps(context, ensure_ascii=False))['context']) == NO_QUOTE_VIEW_ORACLES[label]


# ---------------------------------------------------------------- A stage


def a_view(context):
    view = node_code.compact_context(context)
    assert_no_raw_quote(view)
    return view


def target_alias(view, context, message_id):
    rows = context['members'][0]['context']
    return by_message(view['members'][0]['context'], rows, message_id)['message_id']


def test_a_reply_uses_full_source_and_same_namespace_alias_without_changing_other_aliases():
    plain = node_code.compact_context(analysis_context())
    context = analysis_context()
    context['messages'][0]['reply'] = quote(text='摘要被截断', source_text='SYN-A 3.10%收', source_direction='out',
                                            source_ext_msg_id='qd:8002')
    before = deepcopy(context)
    view = a_view(context)
    assert context == before
    projected = view['messages'][0]['reply']
    alias = target_alias(view, context, 'a-old-out')
    assert projected == {'text': 'SYN-A 3.10%收', 'text_is_summary': False, 'from': 'out',
                         'ts': '2026-10-28T09:30:00+08:00', 'message_id': alias}
    namespace = ALIAS.fullmatch(view['messages'][0]['message_id']).group(1)
    assert ALIAS.fullmatch(alias).group(1) == namespace
    assert alias != 'a-old-out'
    # The quote is context only: it does not take part in namespace hashing.
    assert strip_reply(view) == plain


@pytest.mark.parametrize('values,expected', [
    ({'source_text': 'SYN-A 3.10%收', 'text': '摘要'}, ('SYN-A 3.10%收', False)),
    ({'source_text': None, 'text': '摘要可能被截断'}, ('摘要可能被截断', True)),
    ({'source_text': None, 'text': None}, (None, False)),
    ({'source_text': '原文全文', 'text': None}, ('原文全文', False)),
])
def test_a_text_prefers_source_then_summary(values, expected):
    context = analysis_context()
    context['messages'][0]['reply'] = quote(**values)
    projected = a_view(context)['messages'][0]['reply']
    assert (projected['text'], projected['text_is_summary']) == expected
    assert 'message_id' not in projected


@pytest.mark.parametrize('direction', ['in', 'out', None])
def test_from_maps_to_message_direction_domain(direction):
    for context, rows in ((analysis_context(), 'messages'), (reply_context('explicit_anchors_v0'), 'context')):
        context[rows][-1]['reply'] = quote(source_text='原文', source_direction=direction)
        view = node_code.compact_context(context)
        assert_no_raw_quote(view)
        assert view[rows][-1]['reply']['from'] == direction


@pytest.mark.parametrize('ts', ['2026-10-27T16:01:02+08:00', None])
def test_ts_is_passed_through(ts):
    context = analysis_context()
    context['messages'][0]['reply'] = quote(text='摘要', ts=ts)
    assert a_view(context)['messages'][0]['reply']['ts'] == ts


@pytest.mark.parametrize('label', sorted(CONTEXTS))
def test_unparsed_projects_null_view_without_message_id(label):
    context = CONTEXTS[label]()
    rows = context['messages'] if label == 'analysis' else context['context']
    rows[-1]['reply'] = unparsed()
    view = node_code.compact_context(context)
    assert_no_raw_quote(view)
    target = view['messages'][-1] if label == 'analysis' else view['context'][-1]
    assert target['reply'] == {'text': None, 'text_is_summary': False, 'from': None, 'ts': None}


@pytest.mark.parametrize('native,hit', [
    ('qd:8002', 'a-old-out'),            # exactly one message row
    ('qd:8003', 'a-old-in'),             # one message row plus an edit row: edits do not count
    ('qd:8000', 'history'),              # target only in history_results
    ('qd:9999', None),                   # zero rows
    (None, None),                        # no native ID
])
def test_a_alias_requires_exactly_one_message_row(native, hit):
    context = analysis_context()
    context['messages'][0]['reply'] = quote(source_text='原文', source_ext_msg_id=native)
    view = a_view(context)
    projected = view['messages'][0]['reply']
    if hit is None:
        assert 'message_id' not in projected
    elif hit == 'history':
        assert projected['message_id'] == view['history_results'][0]['messages'][0]['message_id']
    else:
        assert projected['message_id'] == target_alias(view, context, hit)


def test_a_two_distinct_message_rows_with_same_native_give_no_alias():
    context = analysis_context()
    context['members'][1]['context'].append(row('b-dup', 'SYN-B 重复原生号', revision=6, native='qd:8002'))
    context['messages'][0]['reply'] = quote(source_text='原文', source_ext_msg_id='qd:8002')
    projected = a_view(context)['messages'][0]['reply']
    assert projected['text'] == '原文' and 'message_id' not in projected


def test_a_same_message_in_two_positions_is_still_one_target():
    context = analysis_context()
    context['history_results'][0]['messages'].append(deepcopy(context['members'][0]['context'][0]))
    context['history_results'][0]['message_ids'].append('a-old-out')
    context['messages'][0]['reply'] = quote(source_text='原文', source_ext_msg_id='qd:8002')
    view = a_view(context)
    assert view['messages'][0]['reply']['message_id'] == target_alias(view, context, 'a-old-out')


def test_a_every_message_position_is_projected():
    context = analysis_context()
    context['messages'][0]['reply'] = quote(source_text='当前引用', source_ext_msg_id='qd:8001')
    context['correction_context'][0]['reply'] = quote(text='纠错引用摘要', source_direction='in')
    context['members'][0]['context'][1]['reply'] = quote(source_text='成员引用', source_ext_msg_id='qd:8002')
    context['members'][0]['context'][2]['reply'] = unparsed()
    context['history_results'][0]['messages'][0]['reply'] = quote(source_text='历史引用', source_direction='out')
    view = a_view(context)
    assert view['messages'][0]['reply']['message_id'] == by_message(
        view['members'][1]['context'], context['members'][1]['context'], 'b-old')['message_id']
    assert view['correction_context'][0]['reply']['text'] == '纠错引用摘要'
    assert view['correction_context'][0]['reply']['text_is_summary'] is True
    assert view['members'][0]['context'][1]['reply']['message_id'] == target_alias(view, context, 'a-old-out')
    assert view['members'][0]['context'][2]['reply']['text'] is None
    assert view['history_results'][0]['messages'][0]['reply']['text'] == '历史引用'
    assert len(replies(view)) == 5


@pytest.mark.parametrize('version', VERSIONS)
def test_generated_a_node_projects_reply_and_decodes_aliases_unchanged(version):
    _, _, project, contract = generated('analysis', version)
    context = analysis_context()
    context['messages'][0]['reply'] = quote(source_text='SYN-A 3.10%收', source_direction='out',
                                            source_ext_msg_id='qd:8002')
    output = project(json.dumps(context, ensure_ascii=False))
    view = json.loads(output['context'])
    assert view == node_code.compact_context(context)
    assert_no_raw_quote(view)
    assert A_RULE in output['system_prompt'] and B_RULE not in output['system_prompt']
    # Decoding an alias taken from the quote returns the canonical ID.
    alias = view['messages'][0]['reply']['message_id']
    member = view['members'][0]['member_id']
    wire = {'schema_version': '2.2', 'history_requests': [{'member_id': member, 'message_ids': [alias]}]}
    decoded = json.loads(contract(json.dumps(wire), json.dumps(context, ensure_ascii=False))['result'])
    assert decoded['history_requests'] == [{'member_id': 'pa', 'message_ids': ['a-old-out']}]


# ---------------------------------------------------------------- B stage


def test_production_b_quotes_have_text_but_no_message_ids():
    context = reply_context('latest_quote_confirmation_v1')
    context['context'][2]['reply'] = quote(source_text='SYN-Q 2.12%出', source_direction='out',
                                           source_ext_msg_id='qd:7001')
    context['unresolved_messages'][0]['reply'] = quote(text='摘要', source_direction='in',
                                                       source_ext_msg_id='qd:7002')
    context['history_results'][0]['messages'][0]['reply'] = quote(source_text='历史原文', source_ext_msg_id='qd:7001')
    before = deepcopy(context)
    view = node_code.compact_context(context)
    assert context == before
    assert_no_raw_quote(view)
    assert 'handoff_evidence' not in view
    assert view['context'][2]['reply'] == {'text': 'SYN-Q 2.12%出', 'text_is_summary': False, 'from': 'out',
                                           'ts': '2026-10-28T09:30:00+08:00'}
    assert view['current_inbound_message']['reply'] == view['context'][2]['reply']
    assert 'message_id' not in view['current_inbound_message']
    assert view['unresolved_messages'][0]['reply'] == {'text': '摘要', 'text_is_summary': True, 'from': 'in',
                                                       'ts': '2026-10-28T09:30:00+08:00'}
    assert all('message_id' not in projected for projected in replies(view))
    assert len(replies(view)) == 4
    assert strip_reply(view) == node_code.compact_context(reply_context('latest_quote_confirmation_v1'))


def test_v0_b_quotes_keep_canonical_id_on_every_position():
    context = reply_context('explicit_anchors_v0')
    context['context'][2]['reply'] = quote(source_text='SYN-Q 2.12%出', source_direction='out',
                                           source_ext_msg_id='qd:7001')
    context['unresolved_messages'][0]['reply'] = quote(text='摘要', source_ext_msg_id='qd:7002')
    context['handoff_evidence'][1]['reply'] = quote(source_text='证据引用', source_ext_msg_id='qd:7003')
    context['history_results'][0]['messages'][0]['reply'] = quote(source_text='历史引用', source_ext_msg_id='qd:7000')
    view = node_code.compact_context(context)
    assert_no_raw_quote(view)
    assert view['context'][2]['reply']['message_id'] == 'b-own'
    assert view['current_inbound_message']['reply']['message_id'] == 'b-own'
    assert view['unresolved_messages'][0]['reply']['message_id'] == 'b-quote'
    assert view['handoff_evidence'][1]['reply']['message_id'] == 'b-unres'
    assert view['history_results'][0]['messages'][0]['reply']['message_id'] == 'b-hist'
    assert strip_reply(view) == node_code.compact_context(reply_context('explicit_anchors_v0'))


def test_b_alias_matching_counts_distinct_message_rows_only():
    context = reply_context('explicit_anchors_v0')
    context['context'].insert(0, row('b-dup', '同原生号另一行', revision=0, native='qd:7001', historical=1))
    context['context'][-1]['reply'] = quote(source_text='原文', source_ext_msg_id='qd:7001')
    assert 'message_id' not in node_code.compact_context(context)['context'][-1]['reply']
    context = reply_context('explicit_anchors_v0')
    context['context'].insert(0, row('b-edit', '改后', revision=0, native='qd:7001', kind='edit', target='b-own'))
    context['context'][-1]['reply'] = quote(source_text='原文', source_ext_msg_id='qd:7001')
    assert node_code.compact_context(context)['context'][-1]['reply']['message_id'] == 'b-own'


@pytest.mark.parametrize('label', sorted(CONTEXTS))
def test_stale_raw_reply_json_string_never_reaches_the_model_view(label):
    context = CONTEXTS[label]()
    raw = json.dumps(quote(source_text='旧形状原串', source_ext_msg_id='qd:7001'), ensure_ascii=False)
    for item in message_rows(context):
        item['reply_json'] = raw
    view = node_code.compact_context(context)
    assert_no_raw_quote(view)
    assert '旧形状原串' not in dumped(view)


@pytest.mark.parametrize('version', VERSIONS)
@pytest.mark.parametrize('label', ['reply_production', 'reply_v0'])
def test_generated_b_node_projects_reply(version, label):
    _, _, project, _ = generated('reply', version)
    context = CONTEXTS[label]()
    context['context'][2]['reply'] = quote(source_text='SYN-Q 2.12%出', source_ext_msg_id='qd:7001')
    output = project(json.dumps(context, ensure_ascii=False))
    view = json.loads(output['context'])
    assert view == node_code.compact_context(context)
    assert_no_raw_quote(view)
    assert B_RULE in output['system_prompt'] and A_RULE not in output['system_prompt']


# ---------------------------------------------------------------- evidence stays text-only


def a_decode_context(with_quote=True):
    old = row('orig', 'SYN-A 3.10出500万', revision=1, native='qd:6001')
    current = row('in', 'SYN-A 按这个出500万', revision=2, native='qd:6002')
    if with_quote:
        current['reply'] = quote(text='SYN-A 3.10出500万', source_text='SYN-A 3.10出500万',
                                 source_direction='in', source_ext_msg_id='qd:6001')
    return {'schema_version': '2.2', 'stage': 'analysis', 'messages': [current],
            'members': [{'member_id': 'a', 'context': [old]}]}


def observation(evidence, segments):
    changes = {'price': {'kind': 'YIELD', 'value': '3.10'}, 'volume_wan': '500', 'assertion': 'QUOTE',
               'validity': 'ACTIVE', 'finality': 'UNKNOWN', 'confirmed_day': DAY}
    return {'member_id': 'a', 'side': 'ofr', 'changes': changes,
            'evidence': {field: list(evidence) for field in changes}, 'segments': segments,
            'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                               'negotiation_space': 'UNKNOWN', 'reason': 'synthetic'}}


def decode(observations, context):
    value = {'schema_version': '2.2', 'scopes': {'in': ['a']}, 'unresolved': {}, 'observations': observations,
             'corrections': [], 'tool_requests': [], 'history_requests': []}
    return node_code.decode_stage_result(value, context, 'analysis')['observations']


def test_code_node_rejects_a_segment_found_only_in_the_quote():
    rejected = decode([observation(['in'], [{'message_id': 'in', 'text': '3.10出500万'}])], a_decode_context())
    assert rejected == [{'validation_error': 'Segment must be a literal continuous source substring'}]


@pytest.mark.parametrize('evidence,segments', [
    (['in'], [{'message_id': 'in', 'text': '3.10出500万'}]),
    (['in'], [{'message_id': 'in', 'text': '出500万'}]),
    (['orig'], [{'message_id': 'orig', 'text': '3.10出500万'}]),
    (['in', 'orig'], [{'message_id': 'orig', 'text': '3.10出500万'}]),
])
def test_quote_grants_no_extra_evidence_permission_in_code_node(evidence, segments):
    with_quote = decode([observation(evidence, deepcopy(segments))], a_decode_context(True))
    without = decode([observation(evidence, deepcopy(segments))], a_decode_context(False))
    assert with_quote == without
    if evidence == ['orig']:
        assert with_quote == [{'validation_error': 'Field requires current inbound evidence'}]


def server_snapshot(with_quote=True):
    old = {'message_id': 'peer-old', 'direction': 'in', 'ingest_revision': 1, 'text': 'SYN-A 3.20出500万',
           'native_id': 'qd:5001', 'reply': None}
    current = {'message_id': 'current', 'direction': 'in', 'ingest_revision': 2, 'text': 'SYN-A 那就这个500万',
               'native_id': 'qd:5002', 'reply': None}
    if with_quote:
        current['reply'] = quote(text='SYN-A 3.20出500万', source_text='SYN-A 3.20出500万',
                                 source_direction='in', source_ext_msg_id='qd:5001')
    return {'members': [{'member_id': 'a', 'context': [old]}], 'messages': [current]}


def server_observation(segment):
    changes = {'price': {'kind': 'YIELD', 'value': '3.20'}, 'assertion': 'QUOTE'}
    return {'member_id': 'a', 'side': 'ofr', 'changes': changes,
            'evidence': {'price': ['current'], 'assertion': ['current']},
            'segments': [{'message_id': 'current', 'text': segment}],
            'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                               'negotiation_space': 'UNKNOWN', 'reason': 'synthetic'}}


def test_server_validation_rejects_old_price_that_only_appears_in_the_quote():
    snapshot = server_snapshot()
    before = deepcopy(snapshot)
    with pytest.raises(InvalidFact):
        validate_observation(snapshot, server_observation('3.20出500万'), {'syn-a': {'a'}})
    with pytest.raises(InvalidFact):
        validate_observation(snapshot, server_observation('SYN-A 那就这个500万'), {'syn-a': {'a'}})
    assert snapshot == before


@pytest.mark.parametrize('segment', ['3.20出500万', 'SYN-A 那就这个500万', '那就这个'])
def test_server_validation_outcome_is_identical_with_or_without_quote(segment):
    outcomes = []
    for with_quote in (True, False):
        try:
            outcomes.append(('ok', validate_observation(server_snapshot(with_quote), server_observation(segment),
                                                         {'syn-a': {'a'}})))
        except InvalidFact as exc:
            outcomes.append(('error', str(exc)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == 'error'


# ---------------------------------------------------------------- prompts


def test_prompt_rules_are_the_verbatim_contract_sentences_in_the_right_systems():
    assert prompts.ANALYSIS.count(A_RULE) == 1 and prompts.ANALYSIS.endswith('\n'+A_RULE)
    assert prompts.ANALYSIS_DIRECT.count(A_RULE) == 1 and prompts.ANALYSIS_DIRECT.endswith('\n'+A_RULE)
    for text in (prompts.ANALYSIS_ACK, prompts.ANALYSIS_REPAIR, prompts.REPLY_REPAIR):
        assert A_RULE not in text and B_RULE not in text
    for text in (prompts.REPLY, prompts.REPLY_NONQUOTE, prompts.B_PROTOCOL_NOTES):
        assert text.count(B_RULE) == 1 and A_RULE not in text
    assert prompts.B_PROTOCOL_NOTES.endswith(B_RULE+'\n')
    for text in (prompts.ANALYSIS, prompts.ANALYSIS_DIRECT):
        assert B_RULE not in text
    systems = prompts._SYSTEMS
    assert {mode for mode, text in systems['analysis'].items() if A_RULE in text} == {'ordinary', 'direct_calculation'}
    assert {mode for mode, text in systems['reply'].items() if B_RULE in text} == {'ordinary', 'nonquote', '_protocol_notes'}


@pytest.mark.parametrize('version,folder', [('0.15.3', 'chatflow_0153'), ('1.17.1', 'chatflow_1171')])
@pytest.mark.parametrize('stage', ['analysis', 'reply'])
def test_saved_dsl_projection_runs_with_quotes_and_carries_the_rules(version, folder, stage):
    document = yaml.safe_load((ROOT/'dify'/folder/('v22-'+stage+'.yml')).read_text(encoding='utf8'))
    assert document == build(stage, version=version, provider='CONFIGURE_MODEL_PROVIDER', model='CONFIGURE_MODEL_NAME')
    nodes = {item['id']: item['data'] for item in document['workflow']['graph']['nodes']}
    namespace = {}
    exec(nodes['context']['code'], namespace)
    rule, other = (A_RULE, B_RULE) if stage == 'analysis' else (B_RULE, A_RULE)
    assert rule in nodes['context']['code'] and other not in nodes['context']['code']
    for label in (['analysis'] if stage == 'analysis' else ['reply_production', 'reply_v0']):
        context = CONTEXTS[label]()
        rows = context['messages'] if stage == 'analysis' else context['context']
        rows[-1]['reply'] = quote(source_text='原文', source_direction='in', source_ext_msg_id='qd:8002')
        output = namespace['main'](json.dumps(context, ensure_ascii=False))
        view = json.loads(output['context'])
        assert view == node_code.compact_context(context)
        assert_no_raw_quote(view)
        assert rule in output['system_prompt']


def test_readme_generation_command_reproduces_the_saved_artifacts_byte_for_byte(tmp_path, monkeypatch):
    readme = (ROOT/'dify'/'negotiation_v22'/'README.md').read_text(encoding='utf8')
    assert 'build_dify0153_dsl.py' not in readme
    blocks = re.findall(r"```bash\n\.venv/bin/python - <<'PY'\n(.*?)\nPY\n```", readme, re.S)
    assert len(blocks) == 1 and 'from dify.negotiation_v22.build import build' in blocks[0]
    for folder in ('chatflow_0153', 'chatflow_1171'):
        (tmp_path/'dify'/folder).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    exec(compile(blocks[0], 'README-generation', 'exec'), {})
    for folder in ('chatflow_0153', 'chatflow_1171'):
        for stage in ('analysis', 'reply'):
            name = 'v22-'+stage+'.yml'
            assert (tmp_path/'dify'/folder/name).read_bytes() == (ROOT/'dify'/folder/name).read_bytes()
    assert '../../sql/oceanbase/004_message_reply.sql' in readme
    assert 'UPDATE xj2_message SET reply_json = NULL WHERE reply_json IS NOT NULL;' in readme
