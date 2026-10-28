"""A SENT daily question awaits a real quote, not another mandatory question.

All accounts, clocks, model responses and SQLite data are synthetic. Production
Engine, Store, Commands and source-bound rendering are exercised without IM.
"""
from copy import deepcopy
import json

import pytest

from server.negotiation.reply_render import render_reply_plan, reply_requirements
from test_reference_window_policy import DAY, OWNER, Desk


NEXT_DAY = '2026-10-18'
QUESTION = '{bond} 请确认今天的报价和可做数量？'


def candidate(context, *, kind='RECONFIRM', text=QUESTION, question='ASK_CURRENT_QUOTE'):
    return {'schema_version': '2.2', 'action': 'SEND', 'proposal': render_reply_plan(context,
        {'kind': kind, 'text': text, 'question': question})['proposal']}


def command_count(case):
    with case.store.db.transaction(write=False) as tx:
        return tx.execute('SELECT COUNT(*) AS n FROM xj2_command WHERE thread_id=?',
                          (case.thread,)).fetchone()['n']


def current_peer(case):
    with case.store.db.transaction(write=False) as tx:
        return dict(case.store._member(tx, OWNER, case.member))


def daily_question(tmp_path, *, state='SENT', first_kind='RECONFIRM'):
    case = Desk(tmp_path / 'reconfirmation-ack.sqlite')
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
                   (json.dumps({'code': 'SYN-A', 'name': 'SYN-A', 'volume_wan': '10000',
                                'opening_yield_pct': '3.20', 'step_bp': '2'}), case.group))
    original = case.inbound('SYN-A 3.10出500万')
    saved = case.analyze([case.observation(original, price={'kind': 'YIELD', 'value': '3.10'},
        volume_wan='500', assertion='QUOTE', validity='ACTIVE', finality='NEGOTIABLE', confirmed_day=DAY)])
    assert saved['accepted'] and not saved['rejected']
    case.now += 86400
    case.store.rollover(OWNER, case.group, trade_day=NEXT_DAY)
    case.store.set_sending(OWNER, True)
    case.flows.decide = lambda context: candidate(context, kind=first_kind)
    first = case.engine.decide_member(OWNER, case.member)
    assert first['state'] == 'QUEUED', first
    command_id = first['command_id']
    if state != 'QUEUED':
        commands = case.service.commands_core
        assert commands.claim(OWNER, command_id)
        command = commands.get(OWNER, command_id)
        case.now += 1
        assert commands.receipt(OWNER, command_id, account_uid='synthetic-account', peer_uid='synthetic-peer',
            content_hash=command['content_hash'], status='SENT', evidence='synthetic-daily-native-confirmation',
            native_id='synthetic-daily-'+command_id, occurred_at=case.now) == 'SENT'
        if state == 'UNKNOWN':
            assert commands.receipt(OWNER, command_id, account_uid='synthetic-account', peer_uid='synthetic-peer',
                content_hash=command['content_hash'], status='FAILED', evidence='synthetic-conflicting-receipt',
                occurred_at=case.now) == 'UNKNOWN'
        elif state == 'OLD_EPOCH':
            with case.store.write(OWNER) as tx:
                tx.execute('UPDATE xj2_command SET account_epoch=-1 WHERE command_id=?', (command_id,))
        elif state == 'OLD_DAY':
            case.now += 86400
            case.store.rollover(OWNER, case.group, trade_day='2026-10-19')
    assert case.quote()['confirmed_day'] is None
    return case, command_id


def analyzed_message(case, text):
    case.now += 1
    incoming = case.inbound(text, occurred_at=case.now)
    result = case.analyze([])
    assert result['facts_committed'] and not result['accepted'] and not result['rejected']
    return incoming


def test_sent_question_then_plain_ack_does_not_force_the_constraint_obeying_model_to_repeat_it(tmp_path):
    case, first = daily_question(tmp_path)
    analyzed_message(case, '稍等')
    seen = []
    def constraint_obeying_model(context):
        seen.append(deepcopy(context))
        if reply_requirements(context)['question'] == 'ASK_CURRENT_QUOTE':
            return candidate(context)
        return {'schema_version': '2.2', 'action': 'WAIT'}
    case.flows.decide = constraint_obeying_model
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'WAIT', 'A daily question already SENT must no longer be a mandatory repeat after ACK'
    assert len(seen) == 1 and seen[0].get('reconfirmation_waiting') is True
    assert command_count(case) == 1
    assert current_peer(case)['reconfirm_command_id'] == first
    assert current_peer(case)['reconfirm_day'] == NEXT_DAY and case.quote()['confirmed_day'] is None


def test_sent_question_then_nonguiding_chitchat_permits_a_natural_nonquote_response(tmp_path):
    case, first = daily_question(tmp_path)
    analyzed_message(case, 'QA 连通测试 SYN-ACK-0001')
    case.flows.decide = lambda context: candidate(context, kind='SOCIAL', question='NONE',
        text='收到，我这边等你确认。')
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED', result
    body = json.loads(case.service.commands_core.get(OWNER, result['command_id'])['body_json'])
    assert body['kind'] == 'SOCIAL' and body['text'] == '收到，我这边等你确认。'
    assert body.get('our_price') is None and command_count(case) == 2
    assert current_peer(case)['reconfirm_command_id'] == first
    assert current_peer(case)['reconfirm_day'] == NEXT_DAY and case.quote()['confirmed_day'] is None


def test_public_information_question_is_not_replaced_with_another_daily_quote_question(tmp_path):
    case, _ = daily_question(tmp_path)
    analyzed_message(case, 'SYN-A 的发行主体是谁？')
    case.flows.decide = lambda context: candidate(context, kind='CLARIFY', question='NONE',
        text='你问的是发行主体全称，还是主体评级？')
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED', result
    body = json.loads(case.service.commands_core.get(OWNER, result['command_id'])['body_json'])
    assert body['kind'] == 'CLARIFY' and '评级' in body['text'] and body.get('our_price') is None
    assert current_peer(case)['reconfirm_day'] == NEXT_DAY and case.quote()['confirmed_day'] is None


def test_explicit_request_to_repeat_remains_servable_after_a_real_sent_confirmation(tmp_path):
    case, _ = daily_question(tmp_path)
    analyzed_message(case, '刚才没收到，请再问我一次今天的报价')
    case.flows.decide = lambda context: candidate(context)
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED' and command_count(case) == 2
    assert current_peer(case)['reconfirm_day'] == NEXT_DAY and case.quote()['confirmed_day'] is None


@pytest.mark.parametrize('state', ['QUEUED', 'UNKNOWN', 'OLD_EPOCH', 'OLD_DAY'])
def test_only_current_real_sent_proof_can_unlock_a_nonquote_social_response(tmp_path, state):
    case, _ = daily_question(tmp_path, state=state)
    analyzed_message(case, '稍等')
    context = case.reply_context()
    assert not context.get('reconfirmation_waiting')
    case.flows.decide = lambda ctx: candidate(ctx, kind='SOCIAL', question='NONE', text='收到，我先等等。')
    result = case.engine.decide_member(OWNER, case.member)
    assert result is None or result['state'] != 'QUEUED'
    assert command_count(case) == 1 and case.quote()['confirmed_day'] is None


@pytest.mark.parametrize('text', ['我按{own_quote}收。', '我就收{peer_volume}，先等你。',
                                 '我就按{our_last_quote}做。', '收到，这笔就成交。'])
def test_waiting_does_not_let_social_smuggle_economic_or_execution_authority(tmp_path, text):
    case, _ = daily_question(tmp_path)
    analyzed_message(case, '稍等')
    case.flows.decide = lambda context: candidate(context, kind='SOCIAL', question='NONE', text=text)
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] != 'QUEUED' and command_count(case) == 1
    assert current_peer(case)['reconfirm_day'] == NEXT_DAY and case.quote()['confirmed_day'] is None


def test_new_actual_quote_still_commits_and_allows_the_normal_economic_reply(tmp_path):
    case, _ = daily_question(tmp_path)
    case.now += 1
    incoming = case.inbound('SYN-A 3.15出600万，还可以谈', occurred_at=case.now)
    saved = case.analyze([case.observation(incoming, price={'kind': 'YIELD', 'value': '3.15'},
        volume_wan='600', assertion='QUOTE', validity='ACTIVE', finality='NEGOTIABLE', confirmed_day=NEXT_DAY)])
    assert saved['accepted'] and not saved['rejected']
    assert current_peer(case)['reconfirm_day'] is None and case.quote()['confirmed_day'] == NEXT_DAY
    case.flows.decide = lambda context: {'schema_version': '2.2', 'action': 'SEND',
        'proposal': render_reply_plan(context, {'kind': 'REPLY', 'quote_value': '3.19',
            'text': '{bond} {own_quote}{peer_volume}，你再看看？'})['proposal']}
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED' and command_count(case) == 2


def test_bound_current_day_clarify_question_has_canonical_native_reconfirm_kind_and_original_audit(tmp_path):
    case, first = daily_question(tmp_path, first_kind='CLARIFY')
    row = case.service.commands_core.get(OWNER, first)
    body = json.loads(row['body_json'])
    assert body['kind'] == 'RECONFIRM', 'A valid daily confirmation must use the same tracked native semantics'
    assert row['state'] == 'SENT' and row['native_id']
    with case.store.db.transaction(write=False) as tx:
        job = tx.execute('SELECT result_json FROM xj2_reply_job WHERE member_id=? ORDER BY created_at DESC LIMIT 1',
                         (case.member,)).fetchone()
    original = json.loads(job['result_json'])['repair']['initial_response']['proposal']
    assert original['kind'] == original['render_plan']['kind'] == 'CLARIFY'
    assert original['render_plan']['question'] == 'ASK_CURRENT_QUOTE'
    assert body['text'] == original['text'] and body.get('our_price') is None
    assert current_peer(case)['reconfirm_command_id'] == first


def test_initial_clarify_confirmation_is_not_forgotten_after_a_real_sent_and_ack(tmp_path):
    case, first = daily_question(tmp_path, first_kind='CLARIFY')
    analyzed_message(case, '稍等')
    assert case.reply_context().get('reconfirmation_waiting') is True
    case.flows.decide = lambda context: (candidate(context)
        if reply_requirements(context)['question'] == 'ASK_CURRENT_QUOTE'
        else {'schema_version': '2.2', 'action': 'WAIT'})
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'WAIT' and command_count(case) == 1
    assert current_peer(case)['reconfirm_command_id'] == first
    assert current_peer(case)['reconfirm_day'] == NEXT_DAY and case.quote()['confirmed_day'] is None


def test_unresolved_bond_clarification_is_not_reclassified_as_a_daily_quote_confirmation(tmp_path):
    case, first = daily_question(tmp_path)
    case.now += 1
    incoming = case.inbound('你指的是哪只券？', occurred_at=case.now)
    case.flows.analyze = lambda snapshot: {'schema_version': '2.2',
        'scopes': {row['message_id']: [] if row['message_id'] == incoming else [case.member]
                   for row in snapshot['messages']},
        'unresolved': {incoming: [case.member]}, 'observations': []}
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['facts_committed'] and not saved['rejected']
    case.flows.decide = lambda context: candidate(context, kind='CLARIFY', question='NONE', text='你问的是哪只券？')
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED'
    body = json.loads(case.service.commands_core.get(OWNER, result['command_id'])['body_json'])
    assert body['kind'] == 'CLARIFY' and current_peer(case)['reconfirm_command_id'] == first


def test_cash_settlement_clarification_keeps_its_original_kind_and_binding(tmp_path):
    from test_settlement_followup_revision import cash_desk
    case = cash_desk(tmp_path)
    case.flows.decide = lambda context: candidate(context, kind='CLARIFY', question='ASK_SETTLEMENT',
        text='{bond} 你刚才{peer_quote}，结算是{settlement_options}？')
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED'
    body = json.loads(case.service.commands_core.get(OWNER, result['command_id'])['body_json'])
    assert body['kind'] == 'CLARIFY' and body.get('settlement_question')
    assert current_peer(case)['reconfirm_command_id'] is None and body.get('our_price') is None
