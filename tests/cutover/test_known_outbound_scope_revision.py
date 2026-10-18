"""Only server-proven current outbound scopes may fill a model omission.

Synthetic SQLite and real Store/Commands/generated Code; no external model,
HTTP server, transport send, or real account is used.
"""
from copy import deepcopy
from functools import lru_cache
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.prices import InvalidFact
from test_reference_window_policy import OWNER
from test_settlement_followup_revision import (
    add_second_cash_member, ask, cash_desk, receipt, wire,
)


def scene(tmp_path, *, mode='sent', text='T+1', second_member=False):
    case = cash_desk(tmp_path)
    if second_member:
        add_second_cash_member(case)
    commands = case.service.commands_core
    if mode == 'manual':
        command = commands.propose(OWNER, case.member,
            basis=commands.basis(OWNER, case.member), request_id='synthetic-manual-scope',
            proposal={'kind': 'MANUAL', 'text': 'SYN-A 人工结算询问'}, validator=lambda body: body,
            expires_at=case.now+120)
        command_id = command['command_id']
        assert command['state'] == 'QUEUED' and commands.claim(OWNER, command_id)
        case.now += 1
        assert receipt(case, command_id, 'SENT') == 'SENT'
    elif mode == 'forged_native':
        command_id = None
    else:
        command_id = ask(case, {'sent': 'SENT', 'queued': 'QUEUED', 'unknown': 'CONFLICT'}[mode])
    if mode in ('queued', 'forged_native'):
        case.now += 1
        outbound = case.inbound('SYN-A 结算是T+0还是T+1？', direction='out',
                                native_id='synthetic-unmatched-native', occurred_at=case.now)
    else:
        messages = case.store.messages(OWNER, case.thread)
        outbound = next(row['message_id'] for row in messages if row['event_id'] == 'command:'+command_id)
    if mode in ('queued', 'manual', 'forged_native'):
        # Deliberately provide an existing classified MENTION so the negative
        # cannot pass merely because scope was absent. It still lacks eligible
        # automated SENT-command evidence. This is this test's private SQLite.
        with case.store.write(OWNER) as tx:
            tx.execute("INSERT INTO xj2_scope(message_id,member_id,relation) VALUES(?,?,'MENTION')",
                       (outbound, case.member))
    case.now += 1
    incoming = case.inbound(text, occurred_at=case.now)
    job = case.store.claim_analysis(OWNER, case.thread, force=True, include_known_outbound_scopes=True)
    assert job
    context = {**job['snapshot'], 'schema_version': '2.2', 'stage': 'analysis'}
    return case, command_id, outbound, incoming, job, context


@lru_cache(maxsize=2)
def generated(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    namespace = {}
    exec(next(node['data']['code'] for node in graph['nodes'] if node['id'] == 'contract'), namespace)
    return namespace['main']


def decode(raw, context, version='0.15.3'):
    return json.loads(generated(version)(json.dumps(raw), json.dumps(context))['result'])


def empty_result(context, member):
    return {'schema_version': '2.2',
            'scopes': {row['message_id']: [member] for row in context['messages']}, 'observations': []}


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('response', ['settlement', 'wait'])
def test_only_omitted_proven_outbound_scope_is_filled_and_the_real_fact_transaction_commits(tmp_path, version, response):
    case, command_id, outbound, incoming, job, context = scene(tmp_path,
        text='T+1' if response == 'settlement' else 'SYN-A 稍等')
    assert context.get('known_outbound_scopes') == {outbound: [case.member]}
    command = case.service.commands_core.get(OWNER, command_id)
    sent = next(row for row in context['messages'] if row['message_id'] == outbound)
    assert command['state'] == 'SENT' and sent['direction'] == 'out' and not sent['historical']
    assert sent['thread_id'] == command['thread_id'] and sent['native_id'] == command['native_id']
    assert sent['text'] == json.loads(command['body_json'])['text']
    assert sent['content_hash'] == command['content_hash']
    raw = wire(case, context, incoming) if response == 'settlement' else empty_result(context, case.member)
    full = decode(deepcopy(raw), context, version)
    del raw['scopes'][outbound]
    before = deepcopy((raw, context))
    decoded = decode(raw, context, version)
    assert decoded == full, 'Completing a known scope must not change observations or any supplied semantic result'
    assert (raw, context) == before
    prior = case.quote()
    scopes, observations, unresolved = case.engine._normalize(context, decoded)
    saved = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
        scopes=scopes, observations=observations, unresolved=unresolved, reconfirm_current_quotes=True)
    assert saved['facts_committed'] and not saved['rejected'], saved
    if response == 'settlement':
        assert saved['accepted'] and case.quote()['price']['settlement_date'] == '2026-10-20'
    else:
        assert not saved['accepted'] and case.quote() == prior


@pytest.mark.parametrize('mode', ['queued', 'unknown', 'manual', 'forged_native'])
def test_mention_and_native_spelling_without_eligible_automated_sent_command_are_not_known(tmp_path, mode):
    case, _, outbound, _, _, context = scene(tmp_path, mode=mode)
    with case.store.db.transaction(write=False) as tx:
        assert tx.execute("SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?",
                          (outbound, case.member)).fetchone()['relation'] == 'MENTION'
    assert outbound not in context.get('known_outbound_scopes', {})


@pytest.mark.parametrize('missing', ['inbound', 'unknown_outbound'])
def test_missing_inbound_or_unknown_outbound_scope_is_never_silently_filled(tmp_path, missing):
    case, _, outbound, incoming, _, context = scene(tmp_path,
        mode='sent' if missing == 'inbound' else 'forged_native')
    raw = empty_result(context, case.member)
    del raw['scopes'][incoming if missing == 'inbound' else outbound]
    with pytest.raises(ValueError):
        decode(raw, context)


@pytest.mark.parametrize('conflict', ['empty', 'another_current_member'])
def test_explicit_model_scope_cannot_override_the_proven_outbound_association(tmp_path, conflict):
    case, _, outbound, _, _, context = scene(tmp_path, second_member=conflict != 'empty')
    raw = empty_result(context, case.member)
    raw['scopes'][outbound] = [] if conflict == 'empty' else [next(
        peer['member_id'] for peer in context['members'] if peer['member_id'] != case.member)]
    with pytest.raises(ValueError):
        decode(raw, context)


@pytest.mark.parametrize('forgery', ['inbound', 'unknown_member', 'historical_flag', 'past_context'])
def test_known_map_cannot_promote_inbound_unknown_member_or_history_to_trusted_current_outbound(tmp_path, forgery):
    case, _, outbound, incoming, _, context = scene(tmp_path)
    context = deepcopy(context)
    context['known_outbound_scopes'] = {outbound: [case.member]}
    if forgery == 'inbound':
        context['known_outbound_scopes'] = {incoming: [case.member]}
    elif forgery == 'unknown_member':
        context['known_outbound_scopes'][outbound] = ['not-current-member']
    elif forgery == 'historical_flag':
        next(row for row in context['messages'] if row['message_id'] == outbound)['historical'] = True
    else:
        sent = next(row for row in context['messages'] if row['message_id'] == outbound)
        context['messages'] = [row for row in context['messages'] if row['message_id'] != outbound]
        context['members'][0]['context'].append(sent)
    raw = empty_result(context, case.member)
    with pytest.raises(ValueError):
        decode(raw, context)


@pytest.mark.parametrize('violation', ['explicit_override', 'receipt_became_unknown'])
def test_commit_rechecks_the_persisted_snapshot_binding_without_trusting_code_output(tmp_path, violation):
    case, command_id, outbound, _, job, context = scene(tmp_path)
    assert context.get('known_outbound_scopes') == {outbound: [case.member]}
    scopes = empty_result(context, case.member)['scopes']
    if violation == 'explicit_override':
        scopes[outbound] = []
    else:
        assert receipt(case, command_id, 'FAILED') == 'UNKNOWN'
    before = case.quote()
    if violation == 'explicit_override':
        with pytest.raises(InvalidFact):
            case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], scopes=scopes, observations=[])
    else:
        # Association of the already recorded immutable out is not permission
        # to confirm a day or supplement settlement. Their existing live SENT
        # checks must remain separate, so unrelated A facts do not retry forever.
        saved = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'], scopes=scopes, observations=[])
        assert saved['facts_committed'] and not saved['accepted'] and not saved['rejected']
        assert case.service.commands_core.get(OWNER, command_id)['state'] == 'UNKNOWN'
    assert case.quote() == before


def test_scope_completion_keeps_bad_observation_slot_and_valid_sibling_unchanged(tmp_path):
    case, _, outbound, incoming, job, context = scene(tmp_path)
    raw = wire(case, context, incoming)
    bad = deepcopy(raw['observations'][0])
    bad['member_id'] = 'not-current-member'
    raw['observations'].insert(0, bad)
    full = decode(deepcopy(raw), context)
    del raw['scopes'][outbound]
    decoded = decode(raw, context)
    assert decoded == full
    assert len(decoded['observations']) == 2 and decoded['observations'][0].get('validation_error')
    assert decoded['observations'][1]['member_id'] == case.member
    scopes, observations, unresolved = case.engine._normalize(context, decoded)
    saved = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
        scopes=scopes, observations=observations, unresolved=unresolved, reconfirm_current_quotes=True)
    assert [row['index'] for row in saved['accepted']] == [1]
    assert [row['index'] for row in saved['rejected']] == [0]
    assert case.quote()['price']['settlement_date'] == '2026-10-20'


def test_default_direct_store_retains_legacy_empty_out_scope_without_erasing_existing_mention(tmp_path):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    before = case.quote()
    job = case.store.claim_analysis(OWNER, case.thread, force=True)
    assert 'known_outbound_scopes' not in job['snapshot']
    outbound = next(row['message_id'] for row in job['snapshot']['messages']
                    if row['event_id'] == 'command:'+command_id)
    scopes = {row['message_id']: [] for row in job['snapshot']['messages']}
    saved = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
                                      scopes=scopes, observations=[])
    assert saved['facts_committed'] and not saved['accepted'] and not saved['rejected']
    assert case.quote() == before
    with case.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?',
                          (outbound, case.member)).fetchone()['relation'] == 'MENTION'
        assert case.store._member(tx, OWNER, case.member)['waiting_command_id'] == command_id


def test_current_engine_explicitly_selects_known_outbound_contract_for_the_actual_model_context(tmp_path):
    case = cash_desk(tmp_path)
    command_id = ask(case)
    case.now += 1
    incoming = case.inbound('SYN-A 稍等', occurred_at=case.now)
    before = case.quote()
    seen = []
    def model(context):
        context = {**context, 'schema_version': '2.2', 'stage': 'analysis'}
        seen.append(deepcopy(context))
        raw = {'schema_version': '2.2', 'scopes': {incoming: [case.member]}, 'observations': []}
        return decode(raw, context)
    case.flows.analyze = model
    saved = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(seen) == 1
    outbound = next(row['message_id'] for row in seen[0]['messages'] if row['event_id'] == 'command:'+command_id)
    assert seen[0].get('known_outbound_scopes') == {outbound: [case.member]}
    assert saved['facts_committed'] and not saved['accepted'] and not saved['rejected']
    assert case.quote() == before
    assert not case.store.snapshot(OWNER)['threads'][0]['analysis_pending']


def test_native_out_arriving_before_sent_receipt_keeps_raw_hash_and_gets_the_same_known_scope(tmp_path):
    import hashlib
    from server.negotiation.store import encoded
    case = cash_desk(tmp_path)
    command_id = ask(case, 'QUEUED')
    commands = case.service.commands_core
    original_command = commands.get(OWNER, command_id)
    body = json.loads(original_command['body_json'])
    assert commands.claim(OWNER, command_id)
    case.now += 1
    sent_at = case.now
    native_id = 'synthetic-out-before-ack-'+command_id
    outbound = case.inbound(body['text'], direction='out', native_id=native_id, occurred_at=sent_at)
    original_raw = next(row for row in case.store.messages(OWNER, case.thread) if row['message_id'] == outbound)
    assert original_raw['content_hash'] != original_command['content_hash'], 'These hashes describe a raw event and a command body respectively'
    assert original_command['content_hash'] == hashlib.sha256(encoded(body).encode()).hexdigest()
    case.now += 1
    assert commands.receipt(OWNER, command_id, account_uid='synthetic-account', peer_uid='synthetic-peer',
        content_hash=original_command['content_hash'], status='SENT', evidence='synthetic-native-first-receipt',
        native_id=native_id, occurred_at=sent_at) == 'SENT'
    received = commands.get(OWNER, command_id)
    matches = [row for row in case.store.messages(OWNER, case.thread) if row['native_id'] == native_id]
    assert len(matches) == 1 and matches[0]['message_id'] == outbound
    assert matches[0]['content_hash'] == original_raw['content_hash'], 'Receipt must not rewrite the immutable raw-event hash'
    assert received['native_id'] == native_id and received['thread_id'] == matches[0]['thread_id']
    assert matches[0]['text'] == body['text'] and received['content_hash'] == original_command['content_hash']
    with case.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT relation FROM xj2_scope WHERE message_id=? AND member_id=?',
                          (outbound, case.member)).fetchone()['relation'] == 'MENTION'
    case.now += 1
    incoming = case.inbound('T+1', occurred_at=case.now)
    job = case.store.claim_analysis(OWNER, case.thread, force=True, include_known_outbound_scopes=True)
    context = {**job['snapshot'], 'schema_version': '2.2', 'stage': 'analysis'}
    assert context.get('known_outbound_scopes') == {outbound: [case.member]}, {
        'raw_message_hash': original_raw['content_hash'], 'command_body_hash': original_command['content_hash'],
        'command_state': received['state'], 'same_native_text_thread': True}
    raw = wire(case, context, incoming)
    del raw['scopes'][outbound]
    decoded = decode(raw, context)
    assert decoded['scopes'][outbound] == [case.member]
    scopes, observations, unresolved = case.engine._normalize(context, decoded)
    saved = case.store.commit_analysis(OWNER, job['analysis_id'], job['lease_token'],
        scopes=scopes, observations=observations, unresolved=unresolved, reconfirm_current_quotes=True)
    assert saved['accepted'] and not saved['rejected'], saved
    assert case.quote()['price']['settlement_date'] == '2026-10-20'
    after = next(row for row in case.store.messages(OWNER, case.thread) if row['message_id'] == outbound)
    assert after['content_hash'] == original_raw['content_hash']


def test_command_body_must_match_its_own_hash_even_when_native_text_and_scope_match(tmp_path):
    import hashlib
    from server.negotiation.store import encoded
    case = cash_desk(tmp_path)
    command_id = ask(case)
    commands = case.service.commands_core
    original = commands.get(OWNER, command_id)
    body = json.loads(original['body_json'])
    outbound = next(row for row in case.store.messages(OWNER, case.thread)
                    if row['event_id'] == 'command:'+command_id)
    assert outbound['content_hash'] == original['content_hash']
    assert outbound['text'] == body['text'] and outbound['native_id'] == original['native_id']
    tampered = deepcopy(body)
    tampered['settlement_question']['quote_revision'] += 1
    assert tampered['text'] == body['text']
    assert hashlib.sha256(encoded(tampered).encode()).hexdigest() != original['content_hash']
    # Fault injection touches this test's private command row only; the actual
    # native message and both stored hashes deliberately remain unchanged.
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_command SET body_json=? WHERE command_id=?', (encoded(tampered), command_id))
    case.now += 1
    case.inbound('T+1', occurred_at=case.now)
    job = case.store.claim_analysis(OWNER, case.thread, force=True, include_known_outbound_scopes=True)
    context = {**job['snapshot'], 'schema_version': '2.2', 'stage': 'analysis'}
    assert outbound['message_id'] not in context.get('known_outbound_scopes', {})
    matches = [row for row in case.store.messages(OWNER, case.thread) if row['native_id'] == original['native_id']]
    assert len(matches) == 1 and matches[0] == outbound
    assert commands.get(OWNER, command_id)['content_hash'] == original['content_hash']
    raw = empty_result(context, case.member)
    del raw['scopes'][outbound['message_id']]
    with pytest.raises(ValueError):
        decode(raw, context)
