"""Independent dynamic-negotiation acceptance through production contracts.

SQLite fixtures and model responses are synthetic. No external model, real
account, network, deployment, or IM send is used by these tests.
"""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import node_code
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from test_reference_window_policy import DAY, OWNER, Desk
from test_rendered_reply_plan_revision import context as sourced_context
from test_rendered_reply_plan_revision import with_public
from test_production_flow import OWNER as HTTP_OWNER, desk
from test_reply_text_repair import reply_case


def dynamic_context(side='BUY'):
    context = sourced_context()
    context['our_side'] = side
    context['boundary'] = {'kind': 'YIELD', 'value': '1.40' if side == 'BUY' else '1.80'}
    context['plan'].update(opening_yield_pct='1.60', step_bp='2')
    context['our_quote'].update(price={'kind': 'YIELD', 'value': '1.60'},
                                native_id='synthetic-sent-native', message_id='our-sent-message',
                                command_id='synthetic-sent-command', verified_sent=True)
    peer = context['quotes'].pop('ofr')
    peer.update(side='ofr' if side == 'BUY' else 'bid',
                price={'kind': 'YIELD', 'value': '1.50' if side == 'BUY' else '1.70'},
                finality='NEGOTIABLE')
    context['quotes']['ofr' if side == 'BUY' else 'bid'] = peer
    context['context'][0]['text'] = 'SYN-A '+peer['price']['value']+('出' if side == 'BUY' else '收')+'500万'
    context['context'].append({'message_id': 'our-sent-message', 'direction': 'out',
        'text': 'SYN-A 1.60%'+('收' if side == 'BUY' else '出'), 'native_id': 'synthetic-sent-native',
        'thread_id': context['thread_id'], 'trade_day': DAY, 'ingest_revision': 20, 'historical': False})
    context['quote_constraints'] = Engine._quote_constraints(context)
    return context


def natural_plan(value='1.55', text='你这次让到{peer_quote}我看到了，我也往前走些，{own_quote}，这个位置再看看？'):
    return {'kind': 'REPLY', 'quote_value': value, 'text': text, 'question': 'NONE'}


def validate_plan(context, plan):
    before = deepcopy((context, plan))
    rendered = render_reply_plan(context, plan)
    assert (context, plan) == before, 'Rendering cannot mutate economic facts or source evidence'
    result = Engine.__new__(Engine)._validate_reply(context, deepcopy(rendered['proposal']))
    assert context == before[0]
    return result


@pytest.mark.parametrize('side,value', [('BUY', '1.55'), ('SELL', '1.65')])
def test_dynamic_concession_may_exceed_two_bp_reference_without_extra_hidden_boundary(side, value):
    context = dynamic_context(side)
    body = validate_plan(context, natural_plan(value))
    assert body['our_price']['value'] == value
    assert value+'%'+('收' if side == 'BUY' else '出') in body['text']
    assert '你这次让到' in body['text'] and '这个位置再看看？' in body['text']
    assert '请问这个报价还能调整吗' not in body['text']


@pytest.mark.parametrize('side,value', [('BUY', '1.39'), ('SELL', '1.81')])
def test_dynamic_reference_never_overrides_the_final_user_boundary(side, value):
    with pytest.raises((InvalidFact, ValueError)):
        validate_plan(dynamic_context(side), natural_plan(value))


@pytest.mark.parametrize('text', ['SYN-A 1.55收500万', 'SYN-A 1.56%收500万'])
def test_low_level_percent_consistency_guard_remains_a_hard_rejection(text):
    with pytest.raises(InvalidFact, match='Public yield text differs'):
        Engine.__new__(Engine)._validate_reply(dynamic_context(),
            {'kind': 'REPLY', 'text': text, 'our_price': {'kind': 'YIELD', 'value': '1.55'}})


@pytest.mark.parametrize('text', [
    '你提出的条件我收到了，我再靠近些，{own_quote}，再帮忙看看？',
    '刚才是{our_last_quote}，你现在{peer_quote}，这次我调整到{own_quote}。',
    '你看看{own_quote}这个位置，我们再聊？',
    '不自动成交、不锁量，先按{own_quote}继续谈。',
])
def test_natural_ordinary_words_and_verified_opposite_or_historical_prices_are_not_rejected(text):
    context = dynamic_context()
    body = validate_plan(context, natural_plan(text=text))
    expected = text.replace('{our_last_quote}', '1.60%收').replace('{peer_quote}', '1.50%出').replace('{own_quote}', '1.55%收')
    assert body['text'] == expected, 'The model controls the sentence; only verified facts are bound'


def test_bound_whole_peer_lot_is_allowed_and_does_not_change_the_source_quote():
    context = dynamic_context()
    before = deepcopy(context['quotes'])
    body = validate_plan(context, natural_plan(text='{own_quote}，{peer_volume}整笔，这次再看看？'))
    assert '500万' in body['text'] and context['quotes'] == before


@pytest.mark.parametrize('text', [
    '{own_quote}，我出给你。', '{own_quote}，成交。', '{own_quote}，这笔替你锁量。',
    '{own_quote}，内部底价不能再说。', '{own_quote}，另外还有1.59%的条件。',
    '{own_quote}，先做200万。', '{own_quote}，再让5BP。', '{own_quote}，收益率一点五九。',
    '{own_quote}，{unknown_fact}。', '{own_quote}，{info:missing-source}。',
    '我报{peer_quote}，现在{own_quote}。', '你报{our_last_quote}，现在{own_quote}。',
    '{own_quote}，再让十个点。', '{own_quote}，我再退五厘。', '你报{own_quote}，这个位置再看看？',
    '我方底线就是{own_quote}。', '就这么定了，{own_quote}。', '按这个给你做了，{own_quote}。',
    '不会自动成交但这笔给你锁量，{own_quote}。',
])
def test_freedom_of_wording_does_not_authorize_wrong_side_execution_privacy_or_fabricated_economics(text):
    with pytest.raises((InvalidFact, ValueError)):
        validate_plan(dynamic_context(), natural_plan(text=text))


@pytest.mark.parametrize('change', ['no_native', 'wrong_native', 'wrong_owner_message', 'stale_peer_day', 'missing_peer_evidence'])
def test_price_reference_slots_require_current_owned_source_proof(change):
    context = dynamic_context()
    if change == 'no_native':
        context['our_quote'].pop('native_id')
    elif change == 'wrong_native':
        context['our_quote']['native_id'] = 'different-native'
    elif change == 'wrong_owner_message':
        context['context'][-1]['thread_id'] = 'another-thread'
    elif change == 'stale_peer_day':
        context['quotes']['ofr']['confirmed_day'] = '2026-10-16'
    else:
        context['quotes']['ofr']['evidence']['price'] = []
    with pytest.raises((InvalidFact, ValueError)):
        validate_plan(context, natural_plan(text='你这边{peer_quote}，我之前{our_last_quote}，现在{own_quote}。'))


def test_source_bound_public_information_can_be_inserted_without_rewriting_the_model_sentence():
    context, records = with_public('bond_info')
    context.update(quote_constraints=Engine._quote_constraints(context))
    info = next(row for row in records if row['source']['field'] == 'master.remaining_days')
    text = '我查到{info:'+info['id']+'}报价这边{own_quote}，你再考虑一下？'
    body = validate_plan(context, natural_plan('3.18', text))
    assert body['text'] == text.replace('{info:'+info['id']+'}', info['text']).replace('{own_quote}', '3.18%收')


def test_generated_reply_code_and_application_validate_the_same_natural_contract():
    context = dynamic_context()
    context.update(schema_version='2.2', stage='reply', context_policy_revision='latest_quote_confirmation_v1')
    result = node_code.decode_stage_result({'schema_version': '2.2', 'reply_plan': natural_plan(), 'trader_view': {}}, context, 'reply')
    assert result['action'] == 'SEND'
    assert result['proposal'] == render_reply_plan(context, natural_plan())['proposal']
    expected = Engine.__new__(Engine)._validate_reply(context, result['proposal'])
    assert expected['text'] == validate_plan(context, natural_plan())['text']
    assert expected['our_price']['value'] == '1.55'


def test_real_reply_context_marks_history_only_after_proving_the_actual_sent_command(desk):
    case = reply_case(desk)
    engine = case.app.state.negotiation_engine
    store = case.app.state.repository
    with store.db.transaction(write=False) as tx:
        context = engine._reply_context(tx, HTTP_OWNER, case.peer['member_id'],
            engine.commands._basis(tx, HTTP_OWNER, case.peer['member_id'], True))
        own = context['our_quote']
        assert own.get('verified_sent') is True
        command = tx.execute('SELECT * FROM xj2_command WHERE command_id=?', (own['command_id'],)).fetchone()
        message = tx.execute('SELECT * FROM xj2_message WHERE message_id=?', (own['message_id'],)).fetchone()
    assert command['state'] == 'SENT'
    assert command['native_id'] == message['native_id'] == own['native_id']
    assert message['thread_id'] == case.peer['thread_id'] and message['direction'] == 'out'
    body = validate_plan(context, natural_plan('3.15', '之前{our_last_quote}，现在我调整到{own_quote}。'))
    assert '3.20%收' in body['text'] and '3.15%收' in body['text']


@pytest.mark.parametrize('policy', ['latest_quote_confirmation_v1', 'explicit_anchors_v0'])
def test_ordinary_engine_send_rejects_even_an_economically_valid_unbound_old_dify_body(desk, policy):
    case = reply_case(desk)
    engine = case.app.state.negotiation_engine
    engine.context_policy_revision = policy
    before = deepcopy(case.fact)
    calls = []
    def old_flow(context):
        calls.append(deepcopy(context))
        proposal = {'kind': 'REPLY', 'text': 'SYN-A 3.15%收500万',
                    'our_price': {'kind': 'YIELD', 'value': '3.15'}}
        assert engine._validate_reply(context, proposal)['our_price']['value'] == '3.15'
        return {'schema_version': '2.2', 'action': 'SEND', 'proposal': proposal}
    case.flows.decide = old_flow
    result = engine.decide_member(HTTP_OWNER, case.peer['member_id'])
    assert result['state'] == 'REJECTED' and result['command_id'] is None
    assert len(calls) == 1 and not result['repair']['attempted']
    assert case.app.state.repository.quote(HTTP_OWNER, case.peer['member_id'], 'ofr').dump() == before
    assert len(case.native.sent) == 1


@pytest.mark.parametrize('violation', ['unknown_receipt', 'forged_command_hash', 'previous_account_epoch'])
def test_real_context_never_promotes_unproven_sent_history_to_a_public_reference(desk, violation):
    case = reply_case(desk)
    engine = case.app.state.negotiation_engine
    store = case.app.state.repository
    with store.write(HTTP_OWNER) as tx:
        own = tx.execute("SELECT command_id FROM xj2_command WHERE thread_id=? AND state='SENT'",
                         (case.peer['thread_id'],)).fetchone()
        if violation == 'unknown_receipt':
            tx.execute("UPDATE xj2_command SET state='UNKNOWN' WHERE command_id=?", (own['command_id'],))
        elif violation == 'forged_command_hash':
            tx.execute('UPDATE xj2_command SET content_hash=? WHERE command_id=?', ('e'*64, own['command_id']))
        else:
            tx.execute('UPDATE xj2_command SET account_epoch=-1 WHERE command_id=?', (own['command_id'],))
    with store.db.transaction(write=False) as tx:
        context = engine._reply_context(tx, HTTP_OWNER, case.peer['member_id'],
            engine.commands._basis(tx, HTTP_OWNER, case.peer['member_id'], True))
    assert not (context.get('our_quote') or {}).get('verified_sent')
    with pytest.raises((InvalidFact, ValueError)):
        validate_plan(context, natural_plan('3.15', '之前{our_last_quote}，现在{own_quote}。'))


@pytest.mark.parametrize('field,value', [('quote_index', 1), ('lead', '旧开场'), ('tail', '旧结尾'), ('tone', 'POLITE'), ('include_volume', True)])
def test_replaced_fixed_sentence_and_grid_inputs_are_rejected_instead_of_compatibly_used(field, value):
    with pytest.raises((InvalidFact, ValueError)):
        validate_plan(dynamic_context(), {**natural_plan(), field: value})


def sent_outbound_case(tmp_path, *, kind='CLARIFY', state='SENT'):
    case = Desk(tmp_path / 'dynamic-acceptance.sqlite')
    source = case.inbound('SYN-A 3.10出500万')
    saved = case.analyze([case.observation(source, price={'kind': 'YIELD', 'value': '3.10'},
        volume_wan='500', assertion='QUOTE', validity='ACTIVE', confirmed_day=DAY)])
    assert saved['facts_committed'] and saved['accepted']
    case.store.set_sending(OWNER, True)
    commands = case.service.commands_core
    created = commands.propose(OWNER, case.member, basis=commands.basis(OWNER, case.member),
        request_id='synthetic-dynamic-outbound', proposal={'kind': kind, 'text': 'SYN-A 请再确认当前条件'},
        validator=lambda value: value, expires_at=case.now+120)
    command_id = created['command_id']
    assert commands.claim(OWNER, command_id)
    command = commands.get(OWNER, command_id)
    case.now += 1
    assert commands.receipt(OWNER, command_id, account_uid='synthetic-account', peer_uid='synthetic-peer',
        content_hash=command['content_hash'], status='SENT', evidence='synthetic-acceptance-receipt',
        native_id='synthetic-dynamic-native', occurred_at=case.now) == 'SENT'
    if state == 'UNKNOWN':
        assert commands.receipt(OWNER, command_id, account_uid='synthetic-account', peer_uid='synthetic-peer',
            content_hash=command['content_hash'], status='FAILED', evidence='synthetic-conflicting-receipt',
            occurred_at=case.now) == 'UNKNOWN'
    outbound = next(row for row in case.store.messages(OWNER, case.thread) if row['event_id'] == 'command:'+command_id)
    return case, command_id, outbound


def count_model_calls(case):
    calls = []
    case.flows.analyze = lambda snapshot: calls.append(deepcopy(snapshot)) or case.envelope(snapshot, [])
    return calls


def test_complete_proven_sent_only_batch_commits_without_a_remote_analysis_call(tmp_path):
    case, _, outbound = sent_outbound_case(tmp_path)
    prior = deepcopy(case.quote())
    calls = count_model_calls(case)
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['facts_committed'] and not saved['accepted'] and not saved['rejected']
    assert calls == [], 'A complete proven SENT-only snapshot must not spend a model round'
    assert case.quote() == prior
    page = case.store.raw_page(OWNER, case.thread)
    assert page['thread']['analyzed_revision'] == page['thread']['ingest_revision']
    with case.store.db.transaction(write=False) as tx:
        scope = tx.execute('SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?',
                           (outbound['message_id'], case.member)).fetchone()
    assert scope['relation'] == 'MENTION'


@pytest.mark.parametrize('violation', ['mixed_inbound', 'unknown', 'manual', 'missing_scope', 'bad_hash', 'wrong_epoch', 'historical', 'missing_native', 'edit', 'retract'])
def test_incomplete_or_noncurrent_outbound_evidence_cannot_bypass_normal_analysis(tmp_path, violation):
    case, command_id, outbound = sent_outbound_case(tmp_path,
        kind='MANUAL' if violation == 'manual' else 'CLARIFY',
        state='UNKNOWN' if violation == 'unknown' else 'SENT')
    if violation == 'mixed_inbound':
        case.now += 1
        case.inbound('SYN-A 再等一下', occurred_at=case.now)
    elif violation in ('missing_scope', 'bad_hash', 'wrong_epoch', 'historical', 'missing_native', 'edit', 'retract'):
        with case.store.write(OWNER) as tx:
            if violation == 'missing_scope':
                tx.execute('DELETE FROM xj2_scope WHERE message_id=?', (outbound['message_id'],))
            elif violation == 'bad_hash':
                tx.execute('UPDATE xj2_command SET content_hash=? WHERE command_id=?', ('f'*64, command_id))
            elif violation == 'wrong_epoch':
                tx.execute('UPDATE xj2_command SET account_epoch=-1 WHERE command_id=?', (command_id,))
            elif violation == 'historical':
                tx.execute('UPDATE xj2_message SET historical=1 WHERE message_id=?', (outbound['message_id'],))
            elif violation in ('edit', 'retract'):
                source = tx.execute("SELECT message_id FROM xj2_message WHERE thread_id=? AND direction='in' ORDER BY ingest_revision LIMIT 1", (case.thread,)).fetchone()
                tx.execute('UPDATE xj2_message SET kind=?,target_id=? WHERE message_id=?',
                           (violation, source['message_id'], outbound['message_id']))
            else:
                tx.execute('UPDATE xj2_message SET native_id=NULL WHERE message_id=?', (outbound['message_id'],))
    calls = count_model_calls(case)
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['facts_committed']
    assert len(calls) == 1, 'Unproven/mixed/manual/UNKNOWN evidence must retain ordinary model analysis'


def test_new_inbound_arriving_before_fast_commit_remains_pending_without_losing_its_watermark(tmp_path, monkeypatch):
    case, _, _ = sent_outbound_case(tmp_path)
    calls = count_model_calls(case)
    original = case.store.commit_analysis
    arrivals = []
    def with_new_inbound(*args, **kwargs):
        case.now += 1
        arrivals.append(case.inbound('SYN-A 新条件稍后提供', occurred_at=case.now))
        return original(*args, **kwargs)
    monkeypatch.setattr(case.store, 'commit_analysis', with_new_inbound)
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['facts_committed'] and not calls
    page = case.store.raw_page(OWNER, case.thread)
    assert page['thread']['ingest_revision'] > page['thread']['analyzed_revision']
    assert arrivals[0] in {row['message_id'] for row in page['messages']}


@pytest.mark.parametrize('violation', ['expired_lease', 'replaced_token', 'account_epoch'])
def test_fast_path_still_obeys_the_original_commit_fences(tmp_path, monkeypatch, violation):
    case, _, _ = sent_outbound_case(tmp_path)
    calls = count_model_calls(case)
    original = case.store.commit_analysis
    def revoke_claim(*args, **kwargs):
        if violation == 'expired_lease':
            case.now += case.engine.lease_seconds+1
        else:
            with case.store.write(OWNER) as tx:
                if violation == 'replaced_token':
                    tx.execute('UPDATE xj2_analysis SET lease_token=? WHERE analysis_id=?', ('replacement-worker-token', args[1]))
                else:
                    binding = tx.execute('SELECT config_json FROM xj2_binding WHERE owner_id=?', (OWNER,)).fetchone()
                    config = json.loads(binding['config_json'])
                    config['context_epoch'] = config.get('context_epoch', 0)+1
                    tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?', (json.dumps(config), OWNER))
        return original(*args, **kwargs)
    monkeypatch.setattr(case.store, 'commit_analysis', revoke_claim)
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert not calls
    assert not saved.get('facts_committed'), 'Fast-path association cannot bypass lease/epoch CAS'
    page = case.store.raw_page(OWNER, case.thread)
    assert page['thread']['ingest_revision'] > page['thread']['analyzed_revision']
