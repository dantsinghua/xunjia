"""Current default asks the same peer to confirm; archived fetch is opt-in."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from server.bond_tools import BondTools
from server.negotiation.application_store import ApplicationStore
from server.negotiation.engine import Engine
from server.negotiation.prices import LimitPolicy
from server.negotiation.reply_render import render_reply_plan
from server.negotiation.service import Inquiry


OWNER = 'synthetic-reference-policy-owner'
DAY = '2026-10-17'


class Desk:
    def __init__(self, path):
        self.now = datetime(2026, 10, 17, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.store = ApplicationStore('sqlite:///'+str(path), clock=lambda: self.now, initialize=True)
        self.store.bind_account(OWNER, 'synthetic-account', 'synthetic-workbench')
        self.group = self.store.create_group(OWNER, bond_code='SYN-A', our_side='BUY',
            target_volume='10000', trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
        self.member, self.thread = self.store.add_member(OWNER, self.group,
            account_uid='synthetic-account', peer_uid='synthetic-peer')
        self.service = Inquiry(self.store)
        self.flows = SimpleNamespace(analyze=None, decide=None)
        self.engine = Engine(self.service, self.flows, BondTools(None))
        assert self.engine.context_policy_revision == 'latest_quote_confirmation_v1'
        self.seq = 0

    def inbound(self, text, **changes):
        self.seq += 1
        return self.store.ingest(OWNER, self.thread, event_id='synthetic-in-'+str(self.seq),
            source_seq=self.seq, text=text, trade_day=self.service.trade_day(), **changes)

    def envelope(self, snapshot, observations):
        return {'schema_version': '2.2', 'scopes': {
            message['message_id']: [self.member] for message in snapshot['messages']},
            'unresolved': {}, 'observations': observations}

    def observation(self, ref, **changes):
        return {'member_id': self.member, 'side': 'ofr', 'changes': changes,
                'evidence': {field: [ref] for field in changes}}

    def analyze(self, observations):
        self.flows.analyze = lambda snapshot: self.envelope(snapshot, deepcopy(observations))
        return self.engine.analyze_thread(OWNER, self.thread, force=True)

    def quote(self):
        return self.store.quote(OWNER, self.member, 'ofr').dump()

    def reply_context(self):
        with self.store.db.transaction(write=False) as tx:
            return self.engine._reply_context(tx, OWNER, self.member,
                self.service.commands_core._basis(tx, OWNER, self.member, True))


@pytest.fixture
def window(tmp_path):
    case = Desk(tmp_path / 'reference-policy.sqlite')
    anchor = case.inbound('SYN-A 3.10出500万')
    result = case.analyze([case.observation(anchor, price={'kind': 'YIELD', 'value': '3.10'},
        volume_wan='500', assertion='QUOTE', validity='ACTIVE', confirmed_day=DAY)])
    assert result['accepted'] and not result['rejected']
    for _ in range(22):
        case.inbound('SYN-A 稍后交流')
    assert case.analyze([])['facts_committed']
    rows = case.store.context(OWNER, case.member)
    assert len(rows) == 20 and anchor not in {row['message_id'] for row in rows}
    case.anchor = anchor
    case.default_context_ids = [row['message_id'] for row in rows]
    case.store.set_sending(OWNER, True)
    return case


def no_upward_query(case, monkeypatch):
    calls = []
    def prohibited(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('Default policy must not fetch older private anchors')
    monkeypatch.setattr(case.store, 'context', prohibited)
    return calls


def legacy_request(case):
    return {'schema_version': '2.2', 'history_requests': [
        {'member_id': case.member, 'message_ids': [case.anchor]}]}


def confirm_without_dispatch(case):
    calls = []
    def reply(context):
        calls.append(deepcopy(context))
        return {'schema_version': '2.2', 'action': 'SEND',
                'proposal': render_reply_plan(context,
                    {'kind': 'CLARIFY', 'question': 'ASK_CURRENT_QUOTE'})['proposal']}
    case.flows.decide = reply
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED' and len(calls) == 1
    row = case.service.commands_core.get(OWNER, result['command_id'])
    assert row['state'] == 'QUEUED'
    body = json.loads(row['body_json'])
    assert body['kind'] == 'CLARIFY' and body.get('our_price') is None
    return body, calls[0]


def test_default_history_request_is_one_a_call_no_fetch_and_one_same_peer_clarification(window, monkeypatch):
    case = window
    before = case.quote()
    incoming = case.inbound('SYN-A 按那条历史报价出500万')
    calls = []
    def model(snapshot):
        calls.append(deepcopy(snapshot))
        return legacy_request(case)
    case.flows.analyze = model
    upward = no_upward_query(case, monkeypatch)
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(calls) == 1 and result['facts_committed'] and not result['accepted']
    assert result['rejected'][0]['reason'] == 'REFERENCE_CONFIRMATION_REQUIRED'
    assert not result['repair']['attempted'] and not upward and case.quote() == before
    current_rows = calls[0]['members'][0]['context']
    assert len(current_rows) == 20 and [row['message_id'] for row in current_rows] == case.default_context_ids
    assert incoming in {row['message_id'] for row in calls[0]['messages']}
    assert case.anchor not in {row['message_id'] for row in current_rows}
    body, context = confirm_without_dispatch(case)
    assert context['member_id'] == case.member and context['reference_confirmation_required']
    assert '3.10' in body['text'] and '上次' in body['text'] and '500' in body['text']
    assert case.quote() == before and not upward


@pytest.mark.parametrize('observation_form', ['changed_price', 'empty'])
def test_named_unloaded_price_reference_always_records_gap_without_committing_facts(window, monkeypatch, observation_form):
    case = window
    before = case.quote()
    incoming = case.inbound('SYN-A 按消息 '+case.anchor+' 的报价出500万，不能再改善')
    observations = ([] if observation_form == 'empty' else [case.observation(incoming,
        price={'kind': 'YIELD', 'value': '3.30'}, finality='FINAL', confirmed_day=DAY)])
    calls = []
    case.flows.analyze = lambda snapshot: calls.append(deepcopy(snapshot)) or case.envelope(snapshot, observations)
    upward = no_upward_query(case, monkeypatch)
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['facts_committed'] and not result['accepted'] and len(calls) == 1
    assert any(row['member_id'] == case.member and row['reason'] == 'REFERENCE_CONFIRMATION_REQUIRED'
               for row in result['rejected']), result
    assert not result['repair']['attempted'] and case.quote() == before and not upward
    assert case.reply_context()['reference_confirmation_required']


@pytest.mark.parametrize('change', ['retract', 'next_day'])
def test_confirmation_cannot_reactivate_withdrawn_or_previous_day_facts(window, monkeypatch, change):
    case = window
    if change == 'retract':
        case.inbound('撤回此前报价', kind='retract', target_event_id='synthetic-in-1')
        assert case.analyze([])['facts_committed']
        assert case.quote()['price'] is None
    else:
        case.now += 86400
        case.store.rollover(OWNER, case.group, trade_day='2026-10-18')
        assert case.quote()['validity'] == 'UNCONFIRMED'
        assert case.quote()['confirmed_day'] is None and case.quote()['finality'] == 'UNKNOWN'
    before = case.quote()
    case.inbound('SYN-A 按那条旧报价出500万')
    case.flows.analyze = lambda _snapshot: legacy_request(case)
    upward = no_upward_query(case, monkeypatch)
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['facts_committed'] and not result['accepted']
    body, context = confirm_without_dispatch(case)
    if change == 'retract':
        assert '3.10' not in body['text']
    else:
        assert context['reconfirm_day'] == '2026-10-18'
        assert '上次' in body['text'] and '3.10' in body['text']
    assert case.quote() == before and not upward


def test_fresh_explicit_quote_still_updates_normally_with_old_evidence_outside_window(window, monkeypatch):
    case = window
    incoming = case.inbound('SYN-A 3.15出600万')
    upward = no_upward_query(case, monkeypatch)
    result = case.analyze([case.observation(incoming, price={'kind': 'YIELD', 'value': '3.15'},
        volume_wan='600', assertion='QUOTE', validity='ACTIVE', confirmed_day=DAY)])
    assert result['accepted'] and not result['rejected']
    assert case.quote()['price']['value'] == '3.15' and case.quote()['volume_wan'] == '600'
    assert not case.reply_context()['reference_confirmation_required'] and not upward


def test_default_b_history_request_is_one_model_call_and_never_fetches_old_anchor(window, monkeypatch):
    case = window
    before = case.quote()
    case.inbound('SYN-A 请问此前报价还在吗？')
    assert case.analyze([])['facts_committed']
    calls = []
    case.flows.decide = lambda context: calls.append(deepcopy(context)) or legacy_request(case)
    upward = no_upward_query(case, monkeypatch)
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED' and len(calls) == 1 and not upward
    assert calls[0]['context_policy_revision'] == 'latest_quote_confirmation_v1'
    body = json.loads(case.service.commands_core.get(OWNER, result['command_id'])['body_json'])
    assert body['kind'] == 'CLARIFY' and '上次' in body['text'] and body.get('our_price') is None
    assert case.quote() == before
