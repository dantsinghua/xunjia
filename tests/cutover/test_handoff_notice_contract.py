"""Independent quote-acceptance contracts, with isolated SQLite and scripted A/B.

These tests exercise production parsers, validators, persistence and receipts.
They do not call a real model, send IM, execute trades, or restart services.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import json

import pytest
import yaml

from dify.negotiation_v22 import node_code, prompts
from dify.negotiation_v22.build import build
from server.negotiation.application_store import ApplicationStore, status_label
from server.negotiation.commands import Commands
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact, LimitPolicy
from server.negotiation.reply_render import render_reply_plan, reply_requirements
from server.negotiation.service import Inquiry
from server.negotiation.slots import collect_candidates
from server.negotiation.store import encoded


ROOT = Path(__file__).resolve().parents[2]
DAY = '2026-10-27'
OWNER = 'contract-owner'
ACCOUNT = 'contract-account'
ACCEPTANCE = {'meaning': 'ACCEPTED_CURRENT_QUOTE', 'source': 'LATEST_INBOUND'}
SOURCES = ['native', 'generated-0153', 'generated-1171', 'stored-0153', 'stored-1171']


def notice(text='收到，我跟进。'):
    return {'kind': 'HANDOFF_NOTICE', 'text': text, 'acceptance': dict(ACCEPTANCE)}


def envelope(context, stage):
    return {**deepcopy(context), 'schema_version': '2.2', 'stage': stage,
            'context_policy_revision': 'latest_quote_confirmation_v1'}


def endpoints(source, stage='reply'):
    if source == 'native':
        def project(raw):
            value = json.loads(raw)
            return {'system_prompt': prompts.select_system_prompt(value, stage),
                    'context': json.dumps(node_code.compact_context(value))}

        def decode(raw, context):
            return {'result': json.dumps(node_code.decode_stage_result(
                json.loads(raw), json.loads(context), stage))}
        return project, decode
    suffix = source.split('-')[1]
    version = {'0153': '0.15.3', '1171': '1.17.1'}[suffix]
    if source.startswith('generated'):
        document = build(stage, version=version, provider='synthetic', model='synthetic')
    else:
        path = ROOT / 'dify' / ('chatflow_' + suffix) / ('v22-' + stage + '.yml')
        document = yaml.safe_load(path.read_text(encoding='utf8'))
    nodes = {row['id']: row['data'] for row in document['workflow']['graph']['nodes']}
    scopes = []
    for key in ('context', 'contract'):
        scope = {}
        exec(compile(nodes[key]['code'], '<' + source + '-' + stage + '-' + key + '>', 'exec'), scope)
        scopes.append(scope['main'])
    return tuple(scopes)


class ContractDesk:
    """Construct facts through the real analysis path; model choices are scripted."""

    def __init__(self, tmp_path):
        self.now = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.day = DAY
        self.url = 'sqlite:///' + str(tmp_path / 'acceptance.sqlite')
        self.store = ApplicationStore(self.url, clock=lambda: self.now, initialize=True)
        self.store.bind_account(OWNER, ACCOUNT, 'contract-workbench')
        self.store.set_sending(OWNER, True)
        self.group, self.member, self.thread = self.add_scope()
        self.other, _ = self.store.add_member(OWNER, self.group, account_uid=ACCOUNT, peer_uid='peer-b')
        self.seq = 0
        self.last_analysis_context = None
        self.reconnect()

    def reconnect(self):
        self.store = ApplicationStore(self.url, clock=lambda: self.now)
        self.commands = Commands(self.store, current_day=lambda: self.day)
        self.flows = SimpleNamespace(analyze=None, decide=None)
        service = SimpleNamespace(store=self.store, commands_core=self.commands, reference_provider=None,
                                  diagnostics=SimpleNamespace(record=lambda *a, **kw: None))
        self.engine = Engine(service, self.flows, None)

    def add_scope(self, *, owner=OWNER, account=ACCOUNT, bond='SYN-A', side='SELL', peer='peer-a'):
        group = self.store.create_group(owner, bond_code=bond, our_side=side,
            target_volume='2000', trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '2.12'), send_allowed=True)
        member, thread = self.store.add_member(owner, group, account_uid=account, peer_uid=peer)
        with self.store.write(owner) as tx:
            tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
                       (encoded({'opening_yield_pct': '1.98' if side == 'SELL' else '2.26',
                                 'step_bp': '1', 'volume_wan': '2000'}), group))
        return group, member, thread

    def analyze(self, *, incoming=None, price=False, quantity=False, intent='QUOTE', finality='UNKNOWN'):
        def model(snapshot):
            context = envelope(snapshot, 'analysis')
            self.last_analysis_context = deepcopy(context)
            wire = {'schema_version': '2.2', 'scopes': {}, 'observations': []}
            for row in snapshot['messages']:
                wire['scopes'][row['message_id']] = snapshot.get('known_outbound_scopes', {}).get(
                    row['message_id'], [self.member])
            if incoming is not None and (price or quantity or intent == 'WITHDRAW'):
                candidates = collect_candidates(context)
                slots = {}
                for selected, field, role in ((price, 'price', 'YIELD'), (quantity, 'volume_wan', 'QUANTITY')):
                    if selected:
                        matches = [c for c in candidates if c['message_id'] == incoming and role in c['roles']]
                        assert len(matches) == 1, matches
                        slots[field] = {'candidate_id': matches[0]['id'], 'role': role}
                wire['observations'] = [{'member_id': self.member, 'source_ids': [incoming],
                    'side': 'bid', 'slots': slots, 'meaning': {'intent': intent,
                    'validity': 'WITHDRAWN' if intent == 'WITHDRAW' else 'ACTIVE',
                    'negotiation_space': finality, 'reason': '当前原文明确报盘或变更'}}]
            return node_code.decode_stage_result(wire, context, 'analysis')
        self.flows.analyze = model
        result = self.engine.analyze_thread(OWNER, self.thread, force=True)
        assert result and result['facts_committed'] and not result['rejected'], json.dumps(result, ensure_ascii=False)
        return result

    def inbound(self, text, **selection):
        self.seq += 1
        self.now += 1
        message = self.store.ingest(OWNER, self.thread, event_id='contract-in-' + str(self.seq),
            source_seq=self.seq, text=text, trade_day=self.day, occurred_at=self.now)
        self.analyze(incoming=message, **selection)
        return message

    def context(self, member=None):
        with self.store.db.transaction(write=False) as tx:
            member = member or self.member
            basis = self.commands._basis(tx, OWNER, member, True)
            return envelope(self.engine._reply_context(tx, OWNER, member, basis), 'reply')

    def send_own(self, value='2.12'):
        context = self.context()
        proposal = render_reply_plan(context, {'kind': 'REPLY', 'quote_value': value,
                                               'text': '{bond}，{own_quote}，请看看。'})['proposal']
        queued = self.commands.propose(OWNER, self.member, basis=context['basis'],
            request_id='initial-own-' + value, proposal=proposal,
            validator=lambda body: self.engine._validate_reply(context, body), expires_at=self.now + 120)
        assert queued['state'] == 'QUEUED', queued
        cid = queued['command_id']
        assert self.commands.claim(OWNER, cid)
        assert self.commands.execution_permission(OWNER, cid)
        assert self.receipt(cid, 'SENT', 'contract-own-' + value) == 'SENT'
        self.analyze()
        return cid

    def prime(self):
        self.send_own()
        self.quantity_anchor = self.inbound('SYN-A 2.10收1000万', price=True, quantity=True)
        self.acceptance_anchor = self.inbound('SYN-A 2.12可以', price=True)
        assert 'HANDOFF_NOTICE' in reply_requirements(self.context())['allowed_kinds']
        return self

    def queue(self, plan=None):
        value = deepcopy(plan if plan is not None else notice())
        self.flows.decide = lambda context: node_code.decode_stage_result(
            {'schema_version': '2.2', 'reply_plan': deepcopy(value)}, envelope(context, 'reply'), 'reply')
        result = self.engine.decide_member(OWNER, self.member)
        assert result and result['state'] == 'QUEUED', result
        return result['command_id']

    def receipt(self, cid, status, native=None, **overrides):
        row = self.commands.get(OWNER, cid)
        args = {'account_uid': ACCOUNT, 'peer_uid': 'peer-a', 'content_hash': row['content_hash'],
                'status': status, 'evidence': 'contract-receipt-' + status,
                'native_id': native, 'occurred_at': self.now}
        args.update(overrides)
        return self.commands.receipt(OWNER, cid, **args)

    def quote(self):
        return self.store.quote(OWNER, self.member, 'bid').dump()

    def projection(self, member=None):
        member = member or self.member
        for group in self.store.group_rows(OWNER):
            for row in group['peers']:
                if row['member_id'] == member:
                    return group, row
        raise AssertionError('Missing member projection: ' + member)

    def decisions(self):
        with self.store.db.transaction(write=False) as tx:
            return [dict(r) for r in tx.execute(
                "SELECT * FROM xj2_decision WHERE member_id=? AND state='HANDOFF'", (self.member,)).fetchall()]


@pytest.fixture
def desk(tmp_path):
    return ContractDesk(tmp_path).prime()


@pytest.mark.parametrize('finality', ['UNKNOWN', 'NEGOTIABLE', 'FINAL'])
def test_peer_210_prevents_unconditional_212_even_within_sell_limit(tmp_path, finality):
    case = ContractDesk(tmp_path)
    case.send_own('1.98')
    qualifier = {'UNKNOWN': '', 'NEGOTIABLE': '，还能再谈', 'FINAL': '，本人最优且不能再改善'}[finality]
    case.inbound('SYN-A 2.10收1000万' + qualifier, price=True, quantity=True, finality=finality)
    context = case.context()
    before = case.quote()
    assert context['quote_constraints']['max_yield_pct'] == '2.10'
    with pytest.raises(InvalidFact, match='worsens this counterparty'):
        case.engine._validate_reply(context, {'kind': 'REPLY', 'text': '2.12%出',
                                            'our_price': {'kind': 'YIELD', 'value': '2.12'}})
    rendered = render_reply_plan(context, {'kind': 'REPLY', 'quote_value': '2.10', 'text': '{own_quote}，请看看。'})
    assert case.engine._validate_reply(context, rendered['proposal'])['our_price']['value'] == '2.10'
    assert case.quote() == before


def test_absent_quantity_survives_two_instances_explicit_change_and_withdrawal(desk):
    case = desk
    before = case.quote()
    assert before['volume_wan'] == '1000' and before['price']['value'] == '2.12'
    assert before['evidence']['volume_wan'] == [case.quantity_anchor]
    assert before['finality'] == 'UNKNOWN'
    case.reconnect()
    assert case.quote() == before
    case.inbound('SYN-A 改成700万', quantity=True)
    assert case.quote()['volume_wan'] == '700'
    case.reconnect()
    assert case.quote()['volume_wan'] == '700'
    case.inbound('SYN-A 撤回我方报价', intent='WITHDRAW')
    assert case.quote()['validity'] == 'WITHDRAWN'
    assert 'HANDOFF_NOTICE' not in reply_requirements(case.context())['allowed_kinds']
    assert case.projection()[0]['summary']['reported_volume_wan'] == '0'
    case.reconnect()
    assert case.quote()['validity'] == 'WITHDRAWN'


@pytest.mark.parametrize('late', [False, True])
def test_native_sent_only_moves_one_member_and_preserves_unfilled_group(desk, late):
    case = desk
    other_group, other_member, _ = case.add_scope(bond='SYN-B')
    buy_group, buy_member, _ = case.add_scope(side='BUY')
    case.store.bind_account('other-owner', 'other-account', 'other-workbench')
    case.add_scope(owner='other-owner', account='other-account')
    other_owner_before = deepcopy(case.store.group_rows('other-owner'))
    untouched = {mid: deepcopy(case.projection(mid)[1]) for mid in (case.other, other_member, buy_member)}
    facts, own = case.quote(), deepcopy(case.context()['our_quote'])
    cid = case.queue()
    assert not case.decisions() and not case.projection()[1]['member_handoff_pending']
    assert case.commands.claim(OWNER, cid) and case.commands.execution_permission(OWNER, cid)
    if late:
        assert case.receipt(cid, 'UNKNOWN') == 'UNKNOWN'
        assert not case.decisions() and case.projection()[1]['send_allowed'] == 1
        case.reconnect()
    assert case.receipt(cid, 'SENT', 'contract-notice') == 'SENT'
    case.reconnect()
    group, peer = case.projection()
    assert peer['member_handoff_pending'] and peer['send_allowed'] == 0
    assert status_label(group, peer) == '待人工确认'
    assert group['state'] == 'OPEN' and group['send_allowed'] == 1
    assert group['summary']['reported_shortfall_wan'] == '1000'
    assert group['summary']['final_confirmed_volume_wan'] == '0'
    assert group['summary']['final_shortfall_wan'] == '2000'
    assert not group['summary']['goal_ready'] and not group['summary']['quantity_covered']
    assert case.quote() == facts and case.context()['our_quote'] == own
    assert case.store.group_rows('other-owner') == other_owner_before
    for mid, previous in untouched.items():
        current = case.projection(mid)[1]
        if current['thread_id'] == case.thread:
            # The native echo advances the shared conversation watermark,
            # while each sibling's business facts and controls stay intact.
            shared_projection = {'ingest_revision', 'analysis_pending'}
            assert {k: v for k, v in current.items() if k not in shared_projection} == {
                k: v for k, v in previous.items() if k not in shared_projection}
        else:
            assert current == previous
        assert current['send_allowed'] == 1
    sessions = {row['session_id']: row for row in Inquiry(case.store).sessions(OWNER)}
    assert sessions[case.member]['state'] == 'HANDOFF'
    assert sessions[case.other]['state'] == sessions[other_member]['state'] == sessions[buy_member]['state'] == 'NEGOTIATING'
    assert len(case.decisions()) == 1
    snapshot = deepcopy(case.projection())
    assert case.receipt(cid, 'SENT', 'contract-notice') == 'SENT'
    assert case.projection() == snapshot and len(case.decisions()) == 1


@pytest.mark.parametrize('control', ['member', 'owner', 'group', 'next-day', 'context-epoch'])
def test_delayed_sent_cannot_override_new_authorization(desk, control):
    case = desk
    cid = case.queue()
    assert case.commands.claim(OWNER, cid)
    assert case.receipt(cid, 'UNKNOWN') == 'UNKNOWN'
    if control in ('member', 'owner', 'group'):
        scope = {'member_id': case.member} if control == 'member' else {'group_id': case.group} if control == 'group' else {}
        case.store.set_sending(OWNER, False, **scope)
        case.store.set_sending(OWNER, True, **scope)
    elif control == 'next-day':
        case.day = '2026-10-28'
        case.now += 86400
    else:
        case.store.save_preferences(OWNER, {'context_id': 'new-contract-context', 'context_epoch': 1})
    before = case.projection()[1]['control_version']
    assert case.receipt(cid, 'SENT', 'delayed-contract-notice') == 'SENT'
    result = json.loads(case.commands.get(OWNER, cid)['result_json'])
    assert result['handoff_notice']['applied'] is False
    assert not case.decisions() and case.projection()[1]['control_version'] == before
    assert not case.projection()[1]['member_handoff_pending']


def test_expired_undispatched_notice_cannot_claim_handoff(desk):
    case = desk
    cid = case.queue()
    case.now = case.commands.get(OWNER, cid)['expires_at'] + 1
    assert case.commands.claim(OWNER, cid) is None
    assert case.commands.get(OWNER, cid)['state'] == 'CANCELLED'
    assert not case.commands.execution_permission(OWNER, cid)
    assert not case.decisions() and not case.projection()[1]['member_handoff_pending']


@pytest.mark.parametrize('field,value', [('account_uid', 'wrong-account'), ('peer_uid', 'wrong-peer'), ('content_hash', '0' * 64)])
def test_native_receipt_identity_mismatch_is_rejected_without_handoff(desk, field, value):
    cid = desk.queue()
    assert desk.commands.claim(OWNER, cid)
    with pytest.raises(InvalidFact, match='identity/content mismatch'):
        desk.receipt(cid, 'SENT', 'wrong-source', **{field: value})
    assert desk.commands.get(OWNER, cid)['state'] == 'PENDING'
    assert not desk.decisions()
    with pytest.raises((InvalidFact, ValueError)):
        desk.commands.get('another-owner', cid)


def test_manual_resume_then_pause_does_not_resurrect_old_handoff(desk):
    cid = desk.queue()
    assert desk.commands.claim(OWNER, cid)
    desk.receipt(cid, 'SENT', 'notice-before-resume')
    desk.store.set_sending(OWNER, True, member_id=desk.member)
    desk.store.set_sending(OWNER, False, member_id=desk.member)
    desk.reconnect()
    group, peer = desk.projection()
    assert not peer['member_handoff_pending'] and status_label(group, peer) == '已暂停'
    desk.receipt(cid, 'SENT', 'notice-before-resume')
    assert desk.projection()[1]['member_handoff_pending'] is False


@pytest.mark.parametrize('text', ['收到。', '谢谢。', '稍等。', '这个报价可以。'])
def test_equal_price_and_inbound_words_do_not_replace_explicit_model_action(desk, text):
    desk.inbound(text)
    assert 'HANDOFF_NOTICE' in reply_requirements(desk.context())['allowed_kinds']
    cid = desk.queue({'kind': 'SOCIAL', 'text': '收到。'})
    assert desk.commands.claim(OWNER, cid)
    desk.receipt(cid, 'SENT', 'social-receipt')
    assert not desk.decisions() and not desk.projection()[1]['member_handoff_pending']


@pytest.mark.parametrize('text', ['收到1000万。', '按2.12确认。', '目标2000万。', '{peer_volume}收到。',
                                '成交。', '给你锁量。', '先执行。', '我现在执行这笔交易。', '收到，可以吗？'])
def test_notice_rejects_private_quantity_economics_and_trade_commitment(desk, text):
    before = desk.quote()
    with pytest.raises((ValueError, InvalidFact)):
        candidate = render_reply_plan(desk.context(), notice(text))['proposal']
        desk.engine._validate_reply(desk.context(), candidate)
    assert desk.quote() == before and not desk.decisions()


def test_execution_promise_never_reaches_notice_outbox(desk):
    desk.flows.decide = lambda context: node_code.decode_stage_result(
        {'schema_version': '2.2', 'reply_plan': notice('我现在执行这笔交易。')},
        envelope(context, 'reply'), 'reply')
    result = desk.engine.decide_member(OWNER, desk.member)
    assert result and result.get('state') != 'QUEUED', json.dumps(result, ensure_ascii=False)
    with desk.store.db.transaction(write=False) as tx:
        rows = tx.execute("SELECT body_json FROM xj2_command WHERE member_id=? AND state='QUEUED'",
                          (desk.member,)).fetchall()
    assert not rows


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('mode', ['ordinary', 'nonquote', 'OUTPUT_CONTRACT'])
def test_actual_prompt_paths_declare_semantic_notice_without_forced_final(desk, source, mode):
    context = desk.context()
    if mode == 'ordinary':
        # Without verified own delivery, the normal quoting prompt applies;
        # equal prices alone still cannot grant the notice action.
        context['our_quote']['verified_sent'] = False
    elif mode == 'OUTPUT_CONTRACT':
        context['validation_feedback'] = {'mode': mode, 'attempt': 1, 'max_attempts': 1,
            'reason_code': 'DIFY_MODEL_CONTRACT_SLOTS',
            'previous_output': {'schema_version': '2.2', 'reply_plan': notice('{peer_volume}收到。')}}
    before = deepcopy(context)
    project, decode = endpoints(source)
    output = project(json.dumps(context))
    system = output['system_prompt']
    for required in ('HANDOFF_NOTICE', 'ACCEPTED_CURRENT_QUOTE', 'LATEST_INBOUND', '价格相等本身不是接受', '真正SENT'):
        assert required in system
    assert '优先CLARIFY/NONE自然询问对方本人当前报价及整量是否为最终可做报价' not in system
    projected = json.loads(output['context'])
    assert 'handoff_evidence' not in projected and 'message_id' not in projected['current_inbound_message']
    if mode != 'ordinary':
        assert 'HANDOFF_NOTICE' in projected['reply_requirements']['allowed_kinds']
        value = json.loads(decode(json.dumps({'schema_version': '2.2', 'reply_plan': notice()}),
                                  json.dumps(context))['result'])
        body = desk.engine._validate_reply(context, value['proposal'])
        assert body['kind'] == 'HANDOFF_NOTICE' and body.get('our_price') is None
        assert body['text'] == '收到，我跟进。' and '2000' not in body['text']
    else:
        assert 'HANDOFF_NOTICE' not in projected['reply_requirements']['allowed_kinds']
    assert context == before


@pytest.mark.parametrize('source', SOURCES)
def test_output_repair_cannot_create_acceptance_or_change_frozen_notice(desk, source):
    context = desk.context()
    context['validation_feedback'] = {'mode': 'OUTPUT_CONTRACT', 'attempt': 1, 'max_attempts': 1,
        'reason_code': 'DIFY_MODEL_CONTRACT_SLOTS',
        'previous_output': {'schema_version': '2.2', 'reply_plan': notice('{peer_volume}收到。')}}
    _, decode = endpoints(source)
    for changed in ({'kind': 'SOCIAL', 'text': '收到。'},
                    {**notice(), 'acceptance': {'meaning': 'ACCEPTED_CURRENT_QUOTE', 'source': 'OLD_INBOUND'}},
                    {**notice(), 'quote_value': '2.12'}):
        with pytest.raises(ValueError, match='frozen reply fields'):
            decode(json.dumps({'schema_version': '2.2', 'reply_plan': changed}), json.dumps(context))
    context['validation_feedback']['previous_output']['reply_plan'] = {'kind': 'SOCIAL', 'text': '{peer_volume}收到。'}
    with pytest.raises(ValueError, match='frozen reply fields'):
        decode(json.dumps({'schema_version': '2.2', 'reply_plan': notice()}), json.dumps(context))


@pytest.mark.parametrize('source', SOURCES)
def test_analysis_prompt_distinguishes_acceptance_finality_and_omitted_quantity(desk, source):
    context = deepcopy(desk.last_analysis_context)
    project, _ = endpoints(source, stage='analysis')
    system = project(json.dumps(context))['system_prompt']
    assert '明确认可当前报价与明确不能再改善是两件事' in system
    assert '省略volume_wan并保留既有量' in system
    assert '不为转人工伪造FINAL' in system


@pytest.mark.parametrize('suffix,version', [('0153', '0.15.3'), ('1171', '1.17.1')])
@pytest.mark.parametrize('stage', ['analysis', 'reply'])
def test_stored_dsl_code_and_prompts_match_current_source_build(suffix, version, stage):
    path = ROOT / 'dify' / ('chatflow_' + suffix) / ('v22-' + stage + '.yml')
    stored = yaml.safe_load(path.read_text(encoding='utf8'))
    fresh = build(stage, version=version, provider='synthetic', model='synthetic')
    stored_nodes = {r['id']: r['data'] for r in stored['workflow']['graph']['nodes']}
    fresh_nodes = {r['id']: r['data'] for r in fresh['workflow']['graph']['nodes']}
    for key in ('context', 'contract'):
        assert stored_nodes[key]['code'] == fresh_nodes[key]['code'], str(path) + ':' + key
    assert stored_nodes['model']['prompt_template'] == fresh_nodes['model']['prompt_template']
