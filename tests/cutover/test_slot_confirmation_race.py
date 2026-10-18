"""Quote facts survive a lost confirmation receipt; explicit false dates do not."""
from copy import deepcopy

import pytest

from dify.negotiation_v22.node_code import decode_stage_result
from server.negotiation.prices import InvalidFact
from server.negotiation.slots import collect_candidates
from test_reference_window_policy import DAY, OWNER, Desk


TODAY = '2026-10-18'


@pytest.fixture
def rolled(tmp_path):
    case = Desk(tmp_path / 'slot-day-race.sqlite')
    first = case.inbound('SYN-A 3.10出500万')
    saved = case.analyze([case.observation(first, price={'kind': 'YIELD', 'value': '3.10'},
        volume_wan='500', assertion='QUOTE', validity='ACTIVE', confirmed_day=DAY)])
    assert saved['accepted'] and not saved['rejected']
    case.store.set_sending(OWNER, True)
    case.now += 86400
    case.store.rollover(OWNER, case.group, trade_day=TODAY)
    assert case.quote()['confirmed_day'] is None and case.quote()['validity'] == 'UNCONFIRMED'
    return case


def question(case, *, sent=True):
    context = case.reply_context()
    commands = case.service.commands_core
    value = commands.propose(OWNER, case.member, basis=context['basis'],
        request_id='synthetic-confirmation-question',
        proposal={'kind': 'RECONFIRM', 'text': 'SYN-A 请确认今日报价与可做数量。'},
        validator=lambda body: case.engine._validate_reply(context, body), expires_at=case.now+30)
    command_id = value['command_id']
    if sent:
        assert commands.claim(OWNER, command_id)
        assert receipt(case, command_id, 'SENT') == 'SENT'
    return command_id


def receipt(case, command_id, status):
    commands = case.service.commands_core
    command = commands.get(OWNER, command_id)
    return commands.receipt(OWNER, command_id, account_uid='synthetic-account',
        peer_uid='synthetic-peer', content_hash=command['content_hash'],
        status=status, evidence='synthetic-'+status,
        native_id='synthetic-question-native' if status == 'SENT' else None,
        occurred_at=case.now)


def decode_quote(case, snapshot, incoming):
    context = {**snapshot, 'schema_version': '2.2', 'stage': 'analysis'}
    candidates = [row for row in collect_candidates(context) if row['message_id'] == incoming]
    def selected(value, role):
        return {'candidate_id': next(row['id'] for row in candidates
                                     if row['value'] == value and role in row['roles']), 'role': role}
    observation = {'member_id': case.member, 'source_ids': [incoming], 'side': 'ofr',
        'slots': {'price': selected('3.15', 'YIELD'), 'volume_wan': selected('500万', 'QUANTITY')},
        'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE', 'negotiation_space': 'FINAL',
                    'reason': '本人当前出价出量，明确不能再改善。'}}
    return decode_stage_result(case.envelope(context, [observation]), context, 'analysis')


def current_member(case):
    with case.store.db.transaction(write=False) as tx:
        return case.store._member(tx, OWNER, case.member)


def test_sent_snapshot_then_conflicting_receipt_preserves_quote_without_confirming_day(rolled):
    case = rolled
    command_id = question(case)
    case.now += .1
    incoming = case.inbound('SYN-A 3.15出500万，不能再改善了')
    seen = []
    def model(snapshot):
        seen.append(deepcopy(snapshot))
        peer = next(row for row in snapshot['members'] if row['member_id'] == case.member)
        assert peer['reconfirm_sent_at'] is not None
        decoded = decode_quote(case, snapshot, incoming)
        # Precise synchronization point: the actual command changes after the
        # immutable A snapshot, but before the fact-commit transaction.
        assert receipt(case, command_id, 'FAILED') == 'UNKNOWN'
        return decoded
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(seen) == 1 and result['facts_committed'] and result['accepted'], result
    assert not result['rejected'] and not result['repair']['attempted']
    quote = case.quote()
    assert quote['price']['value'] == '3.15' and quote['volume_wan'] == '500'
    assert quote['finality'] == 'FINAL' and quote['confirmed_day'] is None
    assert current_member(case)['reconfirm_day'] == TODAY
    assert case.service.commands_core.get(OWNER, command_id)['state'] == 'UNKNOWN'
    summary = case.store.group(OWNER, case.group)['summary']
    assert summary['price_eligible_volume_wan'] == summary['final_confirmed_volume_wan'] == '0'
    assert not case.store.snapshot(OWNER)['threads'][0]['analysis_pending']


def test_actual_sent_question_followed_by_new_quote_is_confirmed_in_fact_commit(rolled):
    case = rolled
    command_id = question(case)
    case.now += .1
    incoming = case.inbound('SYN-A 3.15出500万，不能再改善了')
    calls = []
    case.flows.analyze = lambda snapshot: calls.append(deepcopy(snapshot)) or decode_quote(case, snapshot, incoming)
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(calls) == 1 and result['accepted'] and not result['rejected'], result
    quote = case.quote()
    assert quote['price']['value'] == '3.15' and quote['volume_wan'] == '500'
    assert quote['finality'] == 'FINAL' and quote['confirmed_day'] == TODAY
    assert quote['evidence']['confirmed_day'] == [incoming]
    assert current_member(case)['reconfirm_day'] is None
    assert case.service.commands_core.get(OWNER, command_id)['state'] == 'SENT'
    summary = case.store.group(OWNER, case.group)['summary']
    assert summary['price_eligible_volume_wan'] == summary['final_confirmed_volume_wan'] == '500'


@pytest.mark.parametrize('violation', ['wrong_day', 'question_not_sent'])
def test_explicit_illegal_confirmed_day_is_still_rejected_without_automatic_repair(rolled, violation):
    case = rolled
    question(case, sent=violation == 'wrong_day')
    case.now += .1
    incoming = case.inbound('SYN-A 3.15出500万，不能再改善了')
    before = case.quote()
    def model(snapshot):
        value = decode_quote(case, snapshot, incoming)
        observation = value['observations'][0]
        observation['changes']['confirmed_day'] = '2026-10-19' if violation == 'wrong_day' else TODAY
        observation['evidence']['confirmed_day'] = [incoming]
        return value
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['facts_committed'] and not result['accepted'] and result['rejected']
    assert 'confirmation' in result['rejected'][0]['reason'].lower()
    assert not result['repair']['attempted'] and case.quote() == before
    assert current_member(case)['reconfirm_day'] == TODAY


def test_direct_legacy_store_commit_does_not_opt_in_to_automatic_day_confirmation(rolled):
    case = rolled
    question(case)
    case.now += .1
    incoming = case.inbound('SYN-A 3.15出500万，不能再改善了')
    job = case.store.claim_analysis(OWNER, case.thread, force=True)
    canonical = decode_quote(case, job['snapshot'], incoming)
    observation = canonical['observations'][0]
    observation['changes'].pop('confirmed_day', None)
    observation['evidence'].pop('confirmed_day', None)
    scopes, observations, unresolved = case.engine._normalize(job['snapshot'], canonical)
    result = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
        scopes=scopes, observations=observations, unresolved=unresolved)
    assert result['accepted'] and not result['rejected']
    assert case.quote()['price']['value'] == '3.15' and case.quote()['confirmed_day'] is None
    assert current_member(case)['reconfirm_day'] == TODAY


@pytest.mark.parametrize('opt_in', [False, True])
def test_confirmation_policy_is_part_of_commit_replay_identity_and_does_not_mutate_input(rolled, opt_in):
    case = rolled
    command_id = question(case)
    case.now += .1
    incoming = case.inbound('SYN-A 3.15出500万，不能再改善了')
    job = case.store.claim_analysis(OWNER, case.thread, force=True)
    canonical = decode_quote(case, job['snapshot'], incoming)
    assert 'confirmed_day' not in canonical['observations'][0]['changes']
    scopes, observations, unresolved = case.engine._normalize(job['snapshot'], canonical)
    arguments = {'scopes': scopes, 'observations': observations, 'unresolved': unresolved,
                 'reconfirm_current_quotes': opt_in}
    original = deepcopy(arguments)
    saved = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], **arguments)
    assert saved['accepted'] and not saved['rejected'] and arguments == original
    before = case.quote()
    assert before['confirmed_day'] == (TODAY if opt_in else None)
    # A replay returns the already committed fact result, not a second inference
    # from a different receipt state or a different version of the policy.
    assert receipt(case, command_id, 'FAILED') == 'UNKNOWN'
    assert case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], **arguments) == saved
    assert case.quote() == before and arguments == original
    with pytest.raises(InvalidFact, match='replay'):
        case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
                                   **{**arguments, 'reconfirm_current_quotes': not opt_in})
    assert case.quote() == before


def test_explicit_null_confirmation_is_preserved_even_when_native_question_is_sent(rolled):
    case = rolled
    question(case)
    case.now += .1
    incoming = case.inbound('SYN-A 3.15出500万，不能再改善了')
    def model(snapshot):
        value = decode_quote(case, snapshot, incoming)
        value['observations'][0]['changes']['confirmed_day'] = None
        value['observations'][0]['evidence']['confirmed_day'] = [incoming]
        return value
    case.flows.analyze = model
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['accepted'] and not saved['rejected']
    assert case.quote()['price']['value'] == '3.15' and case.quote()['confirmed_day'] is None
    assert current_member(case)['reconfirm_day'] == TODAY


def test_snapshot_sent_metadata_cannot_authorize_a_question_that_is_only_queued(rolled):
    case = rolled
    command_id = question(case, sent=False)
    case.now += .1
    incoming = case.inbound('SYN-A 3.15出500万，不能再改善了')
    def model(snapshot):
        supplied = deepcopy(snapshot)
        member = next(row for row in supplied['members'] if row['member_id'] == case.member)
        assert member['reconfirm_sent_at'] is None
        member['reconfirm_sent_at'] = case.now - .1
        value = decode_quote(case, supplied, incoming)
        assert 'confirmed_day' not in value['observations'][0]['changes']
        return value
    case.flows.analyze = model
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert saved['accepted'] and not saved['rejected']
    assert case.quote()['price']['value'] == '3.15' and case.quote()['confirmed_day'] is None
    assert current_member(case)['reconfirm_day'] == TODAY
    assert case.service.commands_core.get(OWNER, command_id)['state'] != 'SENT'
