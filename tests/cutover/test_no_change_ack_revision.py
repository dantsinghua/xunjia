"""A closed acknowledgement prompt never manufactures or erases a fact.

Synthetic contexts and real generated Code/Engine/Store/Commands only.
No HTTP server, model, external transport or real account is used.
"""
from copy import deepcopy
from functools import lru_cache
import json

import pytest

from dify.negotiation_v22 import node_code, prompts
from dify.negotiation_v22.build import build
from server.negotiation import slots
from tests.cutover.test_direct_calculation_route_revision import context as full_context
from tests.cutover.test_reference_window_policy import OWNER
from tests.cutover.test_settlement_followup_revision import ask, cash_desk, receipt


def context(text='SYN-A 稍等', *, outbound=True):
    value = full_context(text)
    current = value['messages'][0]
    current.update(kind='message', ingest_revision=22)
    value.update(from_revision=21, through_revision=22)
    if outbound:
        value['messages'].insert(0, {'message_id': 'question-out', 'ingest_revision': 21,
            'direction': 'out', 'kind': 'message', 'historical': False,
            'text': 'SYN-A 你刚才报的净价97.00，结算是T+0、T+1还是具体日期？',
            'trade_day': '2026-10-17', 'native_id': 'synthetic-question-native'})
        value['known_outbound_scopes'] = {'question-out': ['a']}
    return value


def eligible(value):
    method = getattr(slots, 'no_change_acknowledgement', None)
    assert callable(method), 'The bounded acknowledgement route predicate is missing'
    before = deepcopy(value)
    result = method(value)
    assert type(result) is bool and value == before
    return result


def ack_system():
    system = getattr(prompts, 'ANALYSIS_ACK', None)
    assert isinstance(system, str) and system.strip(), 'The acknowledgement prompt is missing'
    assert system != prompts.ANALYSIS and len(system) < len(prompts.ANALYSIS)
    return system


@lru_cache(maxsize=2)
def generated(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    nodes = {node['id']: node['data'] for node in graph['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'], projection)
    exec(nodes['contract']['code'], contract)
    return nodes, projection['main'], contract['main']


@pytest.mark.parametrize('text', ['稍等', '等一下', 'SYN-A稍等', 'SYN-A：等一下。', ' SYN-A, 稍等！ \n'])
def test_only_closed_no_change_acknowledgements_select_the_short_prompt(text):
    value = context(text)
    assert eligible(value)
    selected = prompts.select_system_prompt(value, 'analysis')
    assert ack_system() in selected and prompts.ANALYSIS not in selected


def test_all_current_inbound_acknowledgements_may_share_one_prompt_without_a_prior_outbound():
    value = context('稍等', outbound=False)
    second = deepcopy(value['messages'][0])
    second.update(message_id='second', ingest_revision=23, text='SYN-A 等一下。')
    value['messages'].append(second)
    value['through_revision'] = 23
    assert eligible(value)
    assert ack_system() in prompts.select_system_prompt(value, 'analysis')


@pytest.mark.parametrize('text', [
    'SYN-A 稍等，3.10出500万', 'SYN-A 等一下，量改800万', 'SYN-A 稍等，撤了',
    'SYN-A 稍等，不能再让', 'SYN-A 不要等一下', 'SYN-A 稍等？', 'SYN-A 能等一下吗？',
    '那只稍等', 'SYN-A 如果可以就稍等', 'SYN-B 稍等', 'XSYN-A 稍等',
    'SYN-A.EXTRA 稍等', 'SYN-A0 稍等', 'SYN-A 2026年10月20日', 'SYN-A T+1',
    'SYN-A 另一只等一下', 'SYN-A 收到', '',
])
def test_economic_change_question_reference_condition_or_unknown_content_falls_back(text):
    value = context(text)
    assert eligible(value) is False
    assert ack_system() not in prompts.select_system_prompt(value, 'analysis')


@pytest.mark.parametrize('change', [
    'two-members', 'empty-members', 'no-inbound', 'historical-inbound', 'edited-inbound',
    'unknown-direction', 'second-economic-inbound', 'missing-outbound-map', 'empty-outbound-map',
    'wrong-outbound-member', 'additional-outbound-member', 'second-unmapped-outbound',
    'tools', 'history', 'correction-context', 'feedback',
])
def test_route_requires_every_current_message_and_trusted_outbound_to_match(change):
    value = context()
    current = value['messages'][-1]
    if change == 'two-members':
        member = deepcopy(value['members'][0]); member.update(member_id='b', bond_code='SYN-B')
        value['members'].append(member)
    elif change == 'empty-members': value['members'] = []
    elif change == 'no-inbound': value['messages'] = value['messages'][:1]
    elif change == 'historical-inbound': current['historical'] = True
    elif change == 'edited-inbound': current.update(kind='edit', target_id='old-message')
    elif change == 'unknown-direction': current['direction'] = 'sideways'
    elif change == 'second-economic-inbound':
        value['messages'].append({**deepcopy(current), 'message_id': 'second', 'text': 'SYN-A 量改800万'})
    elif change == 'missing-outbound-map': value.pop('known_outbound_scopes')
    elif change == 'empty-outbound-map': value['known_outbound_scopes'] = {'question-out': []}
    elif change == 'wrong-outbound-member': value['known_outbound_scopes'] = {'question-out': ['b']}
    elif change == 'additional-outbound-member': value['known_outbound_scopes'] = {'question-out': ['a', 'b']}
    elif change == 'second-unmapped-outbound':
        value['messages'].append({**deepcopy(value['messages'][0]), 'message_id': 'unmapped-out'})
    elif change == 'tools': value['tool_results'] = [{'result': {'status': 'ok', 'data': {'text': '稍等'}}}]
    elif change == 'history': value['history_results'] = [{'member_id': 'a', 'messages': []}]
    elif change == 'correction-context': value['correction_context'] = [deepcopy(current)]
    else: value['validation_feedback'] = None
    assert eligible(value) is False


def test_untrusted_flags_do_not_select_ack_and_valid_repair_keeps_its_original_route():
    value = context('SYN-A 量改800万')
    value.update(no_change_acknowledgement=True, analysis_route='acknowledgement')
    assert eligible(value) is False
    repair = context()
    repair['validation_feedback'] = {'mode': 'EVIDENCE_ONLY', 'attempt': 1, 'max_attempts': 1,
        'targets': [], 'original_response': {'schema_version': '2.2', 'scopes': {}, 'observations': []}}
    assert eligible(repair) is False
    assert prompts.select_system_prompt(repair, 'analysis') == prompts.ANALYSIS_REPAIR
    repair['validation_feedback'] = None
    with pytest.raises(ValueError):
        prompts.select_system_prompt(repair, 'analysis')


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_short_route_keeps_raw_twenty_context_candidates_and_one_model(version):
    value = context()
    before = deepcopy(value)
    nodes, project, decode = generated(version)
    result = project(json.dumps(value, ensure_ascii=False))
    assert ack_system() in result['system_prompt'] and prompts.ANALYSIS not in result['system_prompt']
    projected = json.loads(result['context'])
    assert projected == node_code.compact_context(value)
    assert projected['messages'] == value['messages']
    assert projected['members'][0]['context'] == value['members'][0]['context']
    assert len(projected['members'][0]['context']) == 20
    candidates = slots.collect_candidates(value)
    expected = [row for row in candidates if row['message_id'] in {'current', 'question-out'}
                or set(row['roles']) & {'YIELD', 'YIELD_SUFFIX', 'CHINABOND_BP', 'NET_PRICE', 'DIRTY_PRICE', 'QUANTITY'}]
    assert {row['id'] for row in projected['slot_candidates']} == {row['id'] for row in expected}
    for row in projected['slot_candidates']:
        original = next(item for item in candidates if item['id'] == row['id'])
        # The existing model projection omits lexical/value; its retained
        # token, identity, roles and offsets must remain exact in this route.
        assert all(row[key] == original[key] for key in ('message_id', 'token', 'start', 'end', 'roles'))
        assert 'lexical' not in row and 'value' not in row
    assert len([node for node in nodes.values() if node.get('type') == 'llm']) == 1
    assert nodes['model']['prompt_template'][0]['text'] == '{{#context.system_prompt#}}'
    assert 'observations' not in projected and value == before
    raw = {'schema_version': '2.2', 'scopes': {'current': ['a']}, 'observations': []}
    decoded = json.loads(decode(json.dumps(raw), json.dumps(value))['result'])
    assert decoded['observations'] == [] and decoded['scopes'] == {'current': ['a'], 'question-out': ['a']}
    assert raw['scopes'] == {'current': ['a']}, 'Only the decoder copy may complete the trusted outbound scope'


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_window_reference_still_precedes_acknowledgement_and_malformed_model_scope_is_not_fixed(version):
    _, project, decode = generated(version)
    value = context('SYN-A 稍等。按消息 outside-anchor-123 的报价出500万')
    assert ack_system() not in project(json.dumps(value))['system_prompt']
    assert 'confirmation_requests' in project(json.dumps(value))['system_prompt']
    ordinary = context()
    raw = {'schema_version': '2.2', 'scopes': {}, 'observations': []}
    with pytest.raises(ValueError, match='scope'):
        decode(json.dumps(raw), json.dumps(ordinary))


def analyze_ack(case, version, *, bad_outbound_observation=False, receipt_changes=False):
    case.now += 1
    incoming = case.inbound('SYN-A 稍等', occurred_at=case.now)
    seen = []
    def model(snapshot):
        value = {**deepcopy(snapshot), 'schema_version': '2.2', 'stage': 'analysis'}
        raw = {'schema_version': '2.2', 'scopes': {row['message_id']: [case.member]
               for row in value['messages']}, 'observations': []}
        if bad_outbound_observation:
            outgoing = next(row for row in value['messages'] if row['direction'] == 'out')
            candidate = next(row for row in slots.collect_candidates(value) if row['message_id'] == outgoing['message_id']
                             and 'SETTLEMENT' in row['roles'])
            raw['observations'] = [{'member_id': case.member, 'source_ids': [outgoing['message_id']],
                'side': 'ofr', 'slots': {'settlement': candidate['id']},
                'meaning': {'intent': 'SETTLEMENT', 'reason': '本人回复结算'}}]
        saved_raw = deepcopy(raw)
        decoded = json.loads(generated(version)[2](json.dumps(raw), json.dumps(value))['result'])
        assert raw == saved_raw
        seen.append({'context': value, 'raw': saved_raw, 'decoded': deepcopy(decoded)})
        if receipt_changes:
            assert receipt(case, case.question_id, 'FAILED') == 'UNKNOWN'
        return decoded
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(seen) == 1 and not result['repair']['attempted']
    return result, incoming, seen[0]


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('state', ['SENT', 'CONFLICT'])
def test_real_empty_ack_preserves_cash_facts_and_never_upgrades_unknown_or_sends(tmp_path, version, state):
    case = cash_desk(tmp_path)
    case.question_id = ask(case, state)
    before = case.quote()
    result, _, seen = analyze_ack(case, version)
    assert eligible(seen['context']) is (state == 'SENT')
    assert result['facts_committed'] and result['accepted'] == [] and result['rejected'] == []
    assert case.quote() == before
    assert case.quote()['finality'] == 'FINAL' and case.quote()['price']['settlement_date'] is None
    assert case.service.commands_core.get(OWNER, case.question_id)['state'] == ('SENT' if state == 'SENT' else 'UNKNOWN')
    with case.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n'] == 1
        member = case.store._member(tx, OWNER, case.member)
        assert member['waiting_command_id'] is None and member['waiting_since'] is None


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_live_sent_to_unknown_does_not_turn_no_change_analysis_into_economic_confirmation(tmp_path, version):
    case = cash_desk(tmp_path)
    case.question_id = ask(case)
    before = case.quote()
    result, _, seen = analyze_ack(case, version, receipt_changes=True)
    assert eligible(seen['context'])
    assert result['facts_committed'] and not result['accepted'] and not result['rejected']
    assert case.quote() == before and case.quote()['price']['settlement_date'] is None
    assert case.service.commands_core.get(OWNER, case.question_id)['state'] == 'UNKNOWN'


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_native_wrong_outbound_settlement_observation_still_rejects_and_preserves_all_old_facts(tmp_path, version):
    case = cash_desk(tmp_path)
    case.question_id = ask(case)
    before = case.quote()
    result, _, seen = analyze_ack(case, version, bad_outbound_observation=True)
    assert len(seen['raw']['observations']) == len(seen['decoded']['observations']) == 1
    rejection = seen['decoded']['observations'][0]
    assert rejection['validation_error'] == 'Slot facts need loaded same-member evidence and current inbound source'
    assert result['facts_committed'] and result['accepted'] == [] and len(result['rejected']) == 1
    assert result['rejected'][0]['reason'] == rejection['validation_error']
    assert case.quote() == before, 'A short prompt must never erase the bad slot or repair it from the outbound question'


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_ack_prompt_ends_with_real_snapshot_ids_that_the_actual_contract_accepts(version):
    value = context()
    current = value['messages'][-1]
    current['message_id'] = 'actual-inbound-7d8c9e'
    second = {**deepcopy(current), 'message_id': 'actual-inbound-2a3b4c',
              'ingest_revision': 23, 'text': 'SYN-A 等一下。'}
    value['messages'].append(second)
    value['through_revision'] = 23
    before = deepcopy(value)
    _, project, decode = generated(version)
    projected = project(json.dumps(value, ensure_ascii=False))
    bound = json.loads(projected['system_prompt'].splitlines()[-1])
    assert bound == {'schema_version': '2.2',
                     'scopes': {'actual-inbound-7d8c9e': ['a'], 'actual-inbound-2a3b4c': ['a']},
                     'observations': []}
    raw_before = deepcopy(bound)
    decoded = json.loads(decode(json.dumps(bound), json.dumps(value))['result'])
    assert decoded == {**bound, 'scopes': {**bound['scopes'], 'question-out': ['a']}}
    assert bound == raw_before and value == before
    assert json.loads(projected['context']) == node_code.compact_context(before)


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_chinese_placeholder_scope_from_model_is_still_rejected_without_repair(version):
    value = context()
    raw = {'schema_version': '2.2', 'scopes': {'当前入站ID': ['a']}, 'observations': []}
    before = deepcopy((value, raw))
    with pytest.raises(ValueError, match='scope'):
        generated(version)[2](json.dumps(raw, ensure_ascii=False), json.dumps(value))
    assert (value, raw) == before
