"""Independent local acceptance for a SENT cash-settlement question and answer.

Only synthetic SQLite, production Code/Engine/Store/Commands, the published
calendar algorithm and real local BondTools are used. No transport is sent.
"""
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import hashlib
import json
from zoneinfo import ZoneInfo

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.reference import HTTPReferenceProvider
from server.negotiation.reply_render import render_reply_plan
from server.negotiation.runtime import Dispatcher
from server.negotiation.slots import collect_candidates
from server.negotiation.store import encoded
from test_reference_window_policy import DAY, OWNER, Desk
from test_slot_cash_settlement_revision import Terms, Tools


SETTLEMENT = '2026-10-20'


class PublishedCalendar(HTTPReferenceProvider):
    def __init__(self, case):
        self.case = case
        self.reads = []

    def _read(self, key):
        assert key == 'calendar_url'
        self.reads.append(key)
        day = datetime.fromtimestamp(self.case.now, ZoneInfo('Asia/Shanghai')).date().isoformat()
        return {'trading_day': day, 'latest_chinabond_trading_day': '2026-10-16',
                'settlement_dates': {DAY: {'T+1': SETTLEMENT}}}


def cash_desk(tmp_path, kind='NET_PRICE', expected_settlement=SETTLEMENT):
    case = Desk(tmp_path / 'settlement-followup.sqlite')
    provider = Terms(expected_settlement)
    case.service.reference_provider = provider
    case.service.calendar = PublishedCalendar(case)
    case.engine.tools = Tools(provider)
    label = '净价' if kind == 'NET_PRICE' else '全价'
    source = case.inbound('SYN-A '+label+'97.00出500万，最终价不能再改善')
    observation = case.observation(source, price={'kind': kind, 'value': '97.00'}, volume_wan='500',
        assertion='QUOTE', validity='ACTIVE', finality='FINAL', confirmed_day=DAY)
    observation.update(segments=[{'message_id': source, 'text': 'SYN-A '+label+'97.00出500万，最终价不能再改善'}],
        interpretation={'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                        'negotiation_space': 'FINAL', 'reason': '本人明确不能改善'})
    saved = case.analyze([observation])
    assert saved['accepted'] and not saved['rejected'], saved
    assert case.quote()['price']['settlement_date'] is None
    case.store.set_sending(OWNER, True)
    return case


def receipt(case, command_id, status):
    command = case.service.commands_core.get(OWNER, command_id)
    return case.service.commands_core.receipt(OWNER, command_id,
        account_uid='synthetic-account', peer_uid='synthetic-peer', content_hash=command['content_hash'],
        status=status, evidence='synthetic-settlement-'+status,
        native_id='synthetic-native-'+command_id if status == 'SENT' else None,
        occurred_at=case.now)


def ask(case, state='SENT'):
    case.flows.decide = lambda context: {'schema_version': '2.2', 'action': 'SEND',
        'proposal': render_reply_plan(context,
            {'kind': 'CLARIFY', 'question': 'ASK_SETTLEMENT', 'tone': 'NEUTRAL', 'information_ids': []})['proposal']}
    result = case.engine.decide_member(OWNER, case.member)
    assert result['state'] == 'QUEUED', result
    command_id = result['command_id']
    if state != 'QUEUED':
        assert case.service.commands_core.claim(OWNER, command_id)
        case.now += 1
        if state in ('SENT', 'CONFLICT'):
            assert receipt(case, command_id, 'SENT') == 'SENT'
            if state == 'CONFLICT':
                assert receipt(case, command_id, 'FAILED') == 'UNKNOWN'
        elif state != 'PENDING':
            assert receipt(case, command_id, state) == state
    return command_id


@lru_cache(maxsize=2)
def contract(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    namespace = {}
    exec(next(node['data']['code'] for node in graph['nodes'] if node['id'] == 'contract'), namespace)
    return namespace['main']


def wire(case, context, incoming, *, mutation=None):
    candidates = collect_candidates(context)
    candidate = next(row for row in candidates if row['message_id'] == incoming
                     and set(row['roles']) & {'DATE', 'SETTLEMENT'})
    observation = {'member_id': case.member, 'side': 'ofr', 'source_ids': [incoming],
        'slots': {'settlement': candidate['id']},
        'meaning': {'intent': 'SETTLEMENT', 'reason': '本人回复结算'}}
    if mutation == 'extra_binding':
        observation['settlement_binding'] = {'command_id': 'model-forged', 'quote_revision': 999}
    elif mutation == 'wrong_side':
        observation['side'] = 'bid'
    elif mutation in ('ordinary_settlement_only', 'replay_old_price'):
        observation['meaning'] = {'intent': 'QUOTE', 'validity': 'ACTIVE',
                                  'negotiation_space': 'UNKNOWN', 'reason': '仅回复结算'}
        if mutation == 'replay_old_price':
            price = next(row for row in candidates if row['message_id'] != incoming
                         and set(row['roles']) & {'NET_PRICE', 'DIRTY_PRICE'})
            observation['slots']['price'] = price['id']
    return case.envelope(context, [observation])


def answer(case, text='T+1', *, version='0.15.3', occurred_at=None,
           mutation=None, mutate_context=None, during=None):
    case.now += 1
    incoming = case.inbound(text, occurred_at=case.now if occurred_at is None else occurred_at)
    seen = []
    def model(snapshot):
        context = {**snapshot, 'schema_version': '2.2', 'stage': 'analysis'}
        seen.append(deepcopy(context))
        code_context = deepcopy(context)
        if mutate_context:
            mutate_context(code_context)
        raw = wire(case, code_context, incoming, mutation=mutation)
        for ref in code_context.get('known_outbound_scopes', {}):
            raw['scopes'].pop(ref, None)
        result = json.loads(contract(version)(json.dumps(raw), json.dumps(code_context))['result'])
        if during:
            during()
        return result
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(seen) == 1, 'A settlement answer must not create another model/repair round'
    return result, incoming, seen[0]


def question_in(snapshot, case):
    return next(row for row in snapshot['members'] if row['member_id'] == case.member).get('settlement_question')


def assert_same_economics(before, after):
    assert (after['price']['kind'], after['price']['value']) == (before['price']['kind'], before['price']['value'])
    for field in ('volume_wan', 'assertion', 'validity', 'confirmed_day'):
        assert after[field] == before[field], field


def test_service_owned_question_metadata_is_hashed_but_never_part_of_transport_payload(tmp_path):
    case = cash_desk(tmp_path)
    before = case.quote()
    command_id = ask(case, 'QUEUED')
    commands = case.service.commands_core
    row = commands.get(OWNER, command_id)
    body = json.loads(row['body_json'])
    assert body['settlement_question'] == {'side': 'ofr', 'quote_revision': before['revision']}
    assert row['content_hash'] == hashlib.sha256(encoded(body).encode()).hexdigest()
    claim = commands.claim(OWNER, command_id)
    outgoing = Dispatcher(case.service, None)._wire(OWNER, claim)
    assert outgoing['text'] == body['text'] and outgoing['content_hash'] == row['content_hash']
    assert 'settlement_question' not in outgoing and 'quote_revision' not in outgoing
    assert 'settlement_question' not in outgoing['text']
    assert set(outgoing) <= {'business_version', 'command_id', 'owner_user_id', 'account_uid',
        'peer_id', 'workbench_id', 'kind', 'op', 'text', 'content_hash', 'control_version',
        'expires_at', 'transport', 'source_watermark', 'dispatch_watermark',
        'receiver_fence_id', 'receiver_fence_version', 'account_epoch'}
    assert outgoing['account_epoch'] == 0
    assert commands.get(OWNER, command_id)['state'] == 'PENDING', 'No native execution occurs in this test'


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('kind', ['NET_PRICE', 'DIRTY_PRICE'])
@pytest.mark.parametrize('text,expected', [('T+0', DAY), ('T+1', SETTLEMENT), ('2026-10-20', SETTLEMENT)])
def test_sent_question_accepts_settlement_only_without_reconfirming_price_quantity_or_finality(tmp_path, version, kind, text, expected):
    case = cash_desk(tmp_path, kind, expected)
    before = case.quote()
    command_id = ask(case)
    result, incoming, snapshot = answer(case, text, version=version)
    assert result['facts_committed'] and result['accepted'] and not result['rejected'], result
    binding = question_in(snapshot, case)
    assert binding['command_id'] == command_id and binding['quote_revision'] == before['revision']
    after = case.quote()
    assert_same_economics(before, after)
    assert after['price']['settlement_date'] == expected
    assert after['price']['computed_yield'] is not None and after['price']['calculation_ref'].startswith('calc-')
    assert after['finality'] == 'UNKNOWN' and after['revision'] == before['revision'] + 1
    assert after['evidence']['confirmed_day'] == before['evidence']['confirmed_day']
    assert incoming in after['evidence']['price']
    assert len(case.engine.tools.calls) == 1
    assert case.engine.tools.calls[0][1]['inputs']['settlement_date'] == expected


def test_wait_a_moment_clears_waiting_pointer_but_not_same_version_sent_question(tmp_path):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    case.now += 1
    case.inbound('SYN-A 稍等')
    assert case.analyze([])['facts_committed']
    with case.store.db.transaction(write=False) as tx:
        assert case.store._member(tx, OWNER, case.member)['waiting_command_id'] is None
    before = case.quote()
    result, _, snapshot = answer(case)
    assert result['accepted'] and not result['rejected'], result
    assert question_in(snapshot, case)['command_id'] == command_id
    assert_same_economics(before, case.quote())
    assert case.quote()['price']['settlement_date'] == SETTLEMENT


@pytest.mark.parametrize('state', ['QUEUED', 'PENDING', 'UNKNOWN', 'FAILED', 'CONFLICT'])
def test_no_valid_sent_question_never_authorizes_a_settlement_update(tmp_path, state):
    case = cash_desk(tmp_path)
    ask(case, state)
    before = case.quote()
    result, _, snapshot = answer(case)
    assert not question_in(snapshot, case)
    assert not result.get('accepted') and case.quote() == before
    assert not case.engine.tools.calls


def test_old_question_cannot_bind_after_a_new_quote_revision_even_when_price_is_unchanged(tmp_path):
    case = cash_desk(tmp_path)
    ask(case)
    case.now += 1
    incoming = case.inbound('SYN-A 量改600万')
    changed = case.analyze([case.observation(incoming, volume_wan='600')])
    assert changed['accepted'] and not changed['rejected']
    before = case.quote()
    result, _, snapshot = answer(case)
    assert not question_in(snapshot, case)
    assert not result.get('accepted') and case.quote() == before
    assert not case.engine.tools.calls


def test_next_day_term_does_not_reuse_old_question_or_clear_day_gate(tmp_path):
    case = cash_desk(tmp_path)
    ask(case)
    case.now += 86400
    case.store.rollover(OWNER, case.group, trade_day='2026-10-18')
    before = case.quote()
    result, _, snapshot = answer(case)
    assert not question_in(snapshot, case)
    assert not result.get('accepted') and case.quote() == before
    with case.store.db.transaction(write=False) as tx:
        assert case.store._member(tx, OWNER, case.member)['reconfirm_day'] == '2026-10-18'
    assert not case.engine.tools.calls


def test_late_received_message_that_occurred_before_question_is_not_an_answer(tmp_path):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    sent_at = case.service.commands_core.get(OWNER, command_id)['sent_at']
    before = case.quote()
    result, _, _ = answer(case, occurred_at=sent_at-1)
    assert not result.get('accepted') and case.quote() == before
    assert not case.engine.tools.calls


def test_t_plus_one_without_calendar_never_guesses_a_settlement_date(tmp_path):
    case = cash_desk(tmp_path)
    ask(case)
    case.service.calendar = None
    before = case.quote()
    result, _, _ = answer(case)
    after = case.quote()
    assert_same_economics(before, after)
    assert after['price']['settlement_date'] is None and after['price']['computed_yield'] is None
    assert after['price']['calculation_ref'] is None and not case.engine.tools.calls
    if result.get('accepted'):
        assert after['finality'] == 'UNKNOWN'


@pytest.mark.parametrize('field,value', [('command_id', 'not-owned-command'), ('quote_revision', 999)])
def test_forged_code_question_binding_is_rejected_against_server_state(tmp_path, field, value):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    before = case.quote()
    def forged(snapshot):
        member = next(row for row in snapshot['members'] if row['member_id'] == case.member)
        assert member.get('settlement_question'), 'The actual SENT question must first exist in the trusted snapshot'
        member['settlement_question'][field] = value
    result, _, snapshot = answer(case, mutate_context=forged)
    assert question_in(snapshot, case)['command_id'] == command_id, 'A fixture callback failure is not proof of binding rejection'
    assert not result.get('accepted') and case.quote() == before


@pytest.mark.parametrize('mutation', ['extra_binding', 'wrong_side', 'ordinary_settlement_only', 'replay_old_price'])
def test_new_intent_does_not_relax_old_wire_or_allow_model_copied_bindings(tmp_path, mutation):
    case = cash_desk(tmp_path)
    ask(case)
    before = case.quote()
    result, _, _ = answer(case, mutation=mutation)
    assert not result.get('accepted') and case.quote() == before


def test_receipt_conflict_during_analysis_is_rechecked_in_fact_commit(tmp_path):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    before = case.quote()
    def conflicting_receipt():
        assert receipt(case, command_id, 'FAILED') == 'UNKNOWN'
    result, _, snapshot = answer(case, during=conflicting_receipt)
    assert question_in(snapshot, case)['command_id'] == command_id
    assert not result.get('accepted') and case.quote() == before
    assert case.service.commands_core.get(OWNER, command_id)['state'] == 'UNKNOWN'


@pytest.mark.parametrize('text', ['不是 T+1', 'T+1吗？', '评级日期为2026-10-20'])
def test_model_settlement_intent_cannot_turn_denial_question_or_rating_date_into_confirmation(tmp_path, text):
    case = cash_desk(tmp_path)
    ask(case)
    before = case.quote()
    result, incoming, snapshot = answer(case, text)
    assert any(row['message_id'] == incoming and set(row['roles']) & {'DATE', 'SETTLEMENT'}
               for row in collect_candidates(snapshot)), 'The negative must reach Code, not pass from a fixture StopIteration'
    assert not result.get('accepted'), 'Model SETTLEMENT must not override the actual meaning of this inbound'
    assert case.quote() == before
    assert not case.engine.tools.calls, 'Reject unsupported settlement meaning before deriving an executable yield'


def test_corrected_affirmative_term_is_not_rejected_just_because_another_term_is_negated(tmp_path):
    case = cash_desk(tmp_path)
    ask(case)
    case.now += 1
    incoming = case.inbound('不是 T+0，是 T+1', occurred_at=case.now)
    def model(snapshot):
        context = {**snapshot, 'schema_version': '2.2', 'stage': 'analysis'}
        raw = wire(case, context, incoming)
        chosen = next(row['id'] for row in collect_candidates(context)
                      if row['message_id'] == incoming and row['token'] == 'T+1')
        raw['observations'][0]['slots']['settlement'] = chosen
        return json.loads(contract('0.15.3')(json.dumps(raw), json.dumps(context))['result'])
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['accepted'] and not result['rejected'], result
    assert case.quote()['price']['settlement_date'] == SETTLEMENT


def test_repeated_sent_questions_for_the_same_binding_choose_the_latest_equivalent(tmp_path):
    case = cash_desk(tmp_path)
    first = ask(case)
    case.now += 1
    case.inbound('SYN-A 请再问一次结算')
    assert case.analyze([])['facts_committed']
    before = case.quote()
    second = ask(case)
    assert first != second
    result, _, snapshot = answer(case)
    assert result['accepted'] and not result['rejected'], result
    assert question_in(snapshot, case)['command_id'] == second
    assert question_in(snapshot, case)['quote_revision'] == before['revision']
    assert_same_economics(before, case.quote())


def add_second_cash_member(case):
    from server.negotiation.prices import LimitPolicy
    group = case.store.create_group(OWNER, bond_code='SYN-B', our_side='BUY',
        target_volume='10000', trade_day=DAY, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
    member, thread = case.store.add_member(OWNER, group,
        account_uid='synthetic-account', peer_uid='synthetic-peer')
    assert thread == case.thread
    case.now += 1
    incoming = case.inbound('SYN-B 净价96.00出800万')
    observation = {'member_id': member, 'side': 'ofr',
        'changes': {'price': {'kind': 'NET_PRICE', 'value': '96.00'}, 'volume_wan': '800',
                    'assertion': 'QUOTE', 'validity': 'ACTIVE', 'confirmed_day': DAY},
        'evidence': {field: [incoming] for field in ('price', 'volume_wan', 'assertion', 'validity', 'confirmed_day')}}
    def model(snapshot):
        scopes = {row['message_id']: [peer['member_id'] for peer in snapshot['members']
                  if peer['bond_code'] in row['text']] for row in snapshot['messages']}
        return {'schema_version': '2.2', 'scopes': scopes, 'observations': [deepcopy(observation)]}
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['accepted'] and not result['rejected'], result
    return group, member


@pytest.mark.parametrize('other_state', ['SENT', 'PENDING', 'UNKNOWN'])
@pytest.mark.parametrize('explicit', [False, True])
def test_another_bond_question_blocks_bare_date_but_not_an_explicit_member_answer(tmp_path, other_state, explicit):
    case = cash_desk(tmp_path)
    group_b, member_b = add_second_cash_member(case)
    ask(case)
    assert case.analyze([])['facts_committed']  # Process the actual A out message; no price changes.
    original_member, original_group = case.member, case.group
    try:
        case.member, case.group = member_b, group_b
        ask(case, other_state)
    finally:
        case.member, case.group = original_member, original_group
    before_a = case.quote()
    before_b = case.store.quote(OWNER, member_b, 'ofr').dump()
    result, _, snapshot = answer(case, 'SYN-A T+1' if explicit else 'T+1')
    assert question_in(snapshot, case), 'A genuine A question must be present before checking competition'
    if explicit:
        assert result['accepted'] and not result['rejected'], result
        assert case.quote()['price']['settlement_date'] == SETTLEMENT
        assert_same_economics(before_a, case.quote())
    else:
        assert not result.get('accepted') and case.quote() == before_a
        assert not case.engine.tools.calls
    assert case.store.quote(OWNER, member_b, 'ofr').dump() == before_b


def test_pausing_sending_does_not_disable_a_valid_settlement_fact_update(tmp_path):
    case = cash_desk(tmp_path)
    ask(case)
    case.store.set_sending(OWNER, False)
    before = case.quote()
    result, _, _ = answer(case)
    assert result['accepted'] and not result['rejected'], result
    assert_same_economics(before, case.quote())
    with case.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT send_allowed FROM xj2_owner WHERE owner_id=?', (OWNER,)).fetchone()['send_allowed'] == 0
        assert tx.execute("SELECT COUNT(*) AS n FROM xj2_command WHERE state IN ('QUEUED','PENDING')").fetchone()['n'] == 0


def change_original_source(case, command_id, target, kind):
    case.now += 1
    event = 'command:'+command_id if target == 'question' else 'synthetic-in-1'
    text = '撤回原文' if kind == 'retract' else ('SYN-A 改为核对券名' if target == 'question' else 'SYN-A 净价98.00出500万')
    return case.inbound(text, direction='out' if target == 'question' else 'in',
                        kind=kind, target_event_id=event, occurred_at=case.now)


@pytest.mark.parametrize('target', ['question', 'quote'])
@pytest.mark.parametrize('kind', ['edit', 'retract'])
def test_edited_or_retracted_price_or_question_source_cannot_bind_a_followup(tmp_path, target, kind):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    change_original_source(case, command_id, target, kind)
    before = case.quote()
    result, _, snapshot = answer(case)
    assert not question_in(snapshot, case)
    assert not result.get('accepted') and case.quote() == before
    assert not case.engine.tools.calls


@pytest.mark.parametrize('target', ['question', 'quote'])
def test_source_edit_while_model_runs_is_rechecked_inside_commit(tmp_path, target):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    expected = []
    def edit_during_analysis():
        change_original_source(case, command_id, target, 'edit')
        expected.append(deepcopy(case.quote()))
    result, _, snapshot = answer(case, during=edit_during_analysis)
    assert question_in(snapshot, case)['command_id'] == command_id
    assert expected and not result.get('accepted')
    assert case.quote() == expected[0]


@pytest.mark.parametrize('replace_lease', [False, True])
def test_settlement_commit_is_idempotent_and_stale_lease_cannot_write(tmp_path, replace_lease):
    from server.negotiation.prices import StaleDecision
    case = cash_desk(tmp_path)
    ask(case)
    before = case.quote()
    case.now += 1
    incoming = case.inbound('T+1', occurred_at=case.now)
    job = case.store.claim_analysis(OWNER, case.thread, force=True, lease_seconds=2)
    context = {**job['snapshot'], 'schema_version': '2.2', 'stage': 'analysis'}
    raw = wire(case, context, incoming)
    decoded = json.loads(contract('0.15.3')(json.dumps(raw), json.dumps(context))['result'])
    scopes, observations, unresolved = case.engine._normalize(context, decoded)
    assert len(observations) == 1 and not observations[0].get('validation_error'), observations
    arguments = {'scopes': scopes, 'observations': observations, 'unresolved': unresolved,
                 'reconfirm_current_quotes': True}
    if replace_lease:
        case.now += 3
        renewed = case.store.claim_analysis(OWNER, case.thread, force=True, lease_seconds=30)
        assert renewed['analysis_id'] == job['analysis_id'] and renewed['lease_token'] != job['lease_token']
        assert renewed['snapshot'] == job['snapshot']
        with pytest.raises(StaleDecision):
            case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], **arguments)
        assert case.quote() == before
        job = renewed
    first = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], **arguments)
    assert first['accepted'] and not first['rejected'], first
    after = case.quote()
    second = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], **arguments)
    assert second == first and case.quote() == after
    assert after['revision'] == before['revision'] + 1


def test_naming_two_competing_bonds_in_a_question_does_not_make_the_last_one_explicit(tmp_path):
    case = cash_desk(tmp_path)
    group_b, member_b = add_second_cash_member(case)
    member_a = case.member
    ask(case)
    assert case.analyze([])['facts_committed']
    case.member, case.group = member_b, group_b
    ask(case)
    before_b = case.quote()
    before_a = case.store.quote(OWNER, member_a, 'ofr').dump()
    result, incoming, snapshot = answer(case, 'SYN-A还是SYN-B？ T+1')
    assert question_in(snapshot, case), 'Both real questions must be present before checking ambiguity'
    assert any(row['message_id'] == incoming and row['roles'] == ['SETTLEMENT']
               for row in collect_candidates(snapshot))
    assert not result.get('accepted'), 'Two explicitly named competing targets are not one unique settlement binding'
    assert case.quote() == before_b
    assert case.store.quote(OWNER, member_a, 'ofr').dump() == before_a


def test_other_bond_current_cash_price_cannot_authorize_ordinary_replay_for_settlement_only_member(tmp_path):
    case = cash_desk(tmp_path)
    _, member_b = add_second_cash_member(case)
    before_a = case.quote()
    old_a_source = before_a['evidence']['price'][0]
    before_b = case.store.quote(OWNER, member_b, 'ofr').dump()
    case.now += 1
    incoming = case.inbound('SYN-A T+1；SYN-B 净价95.00出800万', occurred_at=case.now)
    def model(snapshot):
        context = {**snapshot, 'schema_version': '2.2', 'stage': 'analysis'}
        candidates = collect_candidates(context)
        def selected(ref, value, role):
            return next(row['id'] for row in candidates
                        if row['message_id'] == ref and row['value'] == value and role in row['roles'])
        common = {'source_ids': [incoming], 'side': 'ofr',
                  'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE',
                              'negotiation_space': 'UNKNOWN', 'reason': '故意模拟模型错误沿用A旧价'}}
        observations = [
            {**deepcopy(common), 'member_id': case.member,
             'slots': {'price': selected(old_a_source, '97.00', 'NET_PRICE'),
                       'settlement': selected(incoming, 'T+1', 'SETTLEMENT')}},
            {**deepcopy(common), 'member_id': member_b,
             'slots': {'price': selected(incoming, '95.00', 'NET_PRICE'),
                       'volume_wan': selected(incoming, '800万', 'QUANTITY')}},
        ]
        scopes = {row['message_id']: [peer['member_id'] for peer in context['members']
                  if peer['bond_code'] in row['text']] for row in context['messages']}
        raw = {'schema_version': '2.2', 'scopes': scopes, 'observations': observations}
        return json.loads(contract('0.15.3')(json.dumps(raw), json.dumps(context))['result'])
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['facts_committed']
    assert [(row['member_id'], row['side']) for row in result['accepted']] == [(member_b, 'ofr')], result
    assert len(result['rejected']) == 1 and result['rejected'][0]['member_id'] == case.member
    assert case.quote() == before_a, 'B cash tokens cannot make A settlement-only text a fresh cash quotation'
    after_b = case.store.quote(OWNER, member_b, 'ofr').dump()
    assert after_b['price']['kind'] == 'NET_PRICE' and after_b['price']['value'] == '95.00'
    assert after_b['volume_wan'] == '800' and after_b['confirmed_day'] == DAY
    assert after_b['revision'] == before_b['revision'] + 1
    assert not case.engine.tools.calls


def test_two_explicit_bond_clauses_can_each_supplement_their_own_settlement(tmp_path):
    from types import SimpleNamespace
    case = cash_desk(tmp_path)
    group_b, member_b = add_second_cash_member(case)
    member_a, group_a = case.member, case.group
    ask(case)
    assert case.analyze([])['facts_committed']
    case.member, case.group = member_b, group_b
    ask(case)
    case.member, case.group = member_a, group_a
    before = {mid: case.store.quote(OWNER, mid, 'ofr').dump() for mid in (member_a, member_b)}
    snapshot = Terms(SETTLEMENT).snapshot()
    snapshot.bonds.append({'bond_id': 'SYN-B', 'code': 'SYN-B'})
    snapshot.terms['SYN-B'] = deepcopy(snapshot.terms['SYN-A'])
    snapshot.terms['SYN-B'].update(terms_snapshot_id='synthetic-cashflows-b',
                                  accrued_interest_settlement_date=DAY)
    provider = SimpleNamespace(snapshot=lambda: deepcopy(snapshot))
    case.service.reference_provider = provider
    case.engine.tools = Tools(provider)
    case.now += 1
    incoming = case.inbound('SYN-A T+1；SYN-B T+0', occurred_at=case.now)
    def model(actual):
        context = {**actual, 'schema_version': '2.2', 'stage': 'analysis'}
        candidates = collect_candidates(context)
        observations = []
        for mid, term in ((member_a, 'T+1'), (member_b, 'T+0')):
            chosen = next(row['id'] for row in candidates
                          if row['message_id'] == incoming and row['token'] == term and row['roles'] == ['SETTLEMENT'])
            observations.append({'member_id': mid, 'side': 'ofr', 'source_ids': [incoming],
                'slots': {'settlement': chosen}, 'meaning': {'intent': 'SETTLEMENT', 'reason': '各券分句明确结算'}})
        scopes = {row['message_id']: [peer['member_id'] for peer in context['members']
                  if peer['bond_code'] in row['text']] for row in context['messages']}
        raw = {'schema_version': '2.2', 'scopes': scopes, 'observations': observations}
        return json.loads(contract('0.15.3')(json.dumps(raw), json.dumps(context))['result'])
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['facts_committed'] and not result['rejected'], result
    assert {row['member_id'] for row in result['accepted']} == {member_a, member_b}
    for mid, day in ((member_a, SETTLEMENT), (member_b, DAY)):
        after = case.store.quote(OWNER, mid, 'ofr').dump()
        assert_same_economics(before[mid], after)
        assert after['price']['settlement_date'] == day
        assert after['price']['computed_yield'] is not None and after['price']['calculation_ref'].startswith('calc-')
        assert after['evidence']['confirmed_day'] == before[mid]['evidence']['confirmed_day']
        assert after['finality'] == 'UNKNOWN' and after['revision'] == before[mid]['revision'] + 1
    assert len(case.engine.tools.calls) == 2
    assert {(request['inputs']['bond_id'], request['inputs']['settlement_date'])
            for _, request in case.engine.tools.calls} == {('SYN-A', SETTLEMENT), ('SYN-B', DAY)}
