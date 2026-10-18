"""The artifact supplement must reject a call-only or fabricated answer."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from dify.negotiation_v22.cases import reference_snapshot
from dify.negotiation_v22.prompts import CALC_EXAMPLE, SIMILAR_EXAMPLE
from server.bond_tools import BondTools
from tool_answer_artifacts import CASES, check_record


def synthetic_record(case_id):
    tools = BondTools(SimpleNamespace(snapshot=lambda: reference_snapshot(case_id)))
    if case_id == 'tool-bond-info':
        request = {'name': 'bond_info', 'arguments': {'schema_version': '1', 'mode': 'get', 'bond_id': 'SYN-A'}}
        text, private = 'SYN-A 3.19%收500万；发行主体是SYN-ISSUER。', {'issuer_id': 'SYN-ISSUER'}
    elif case_id == 'tool-issuer-info':
        request = {'name': 'issuer_info', 'arguments': {'schema_version': '1', 'mode': 'get', 'issuer_id': 'SYN-ISSUER'}}
        text, private = 'SYN-A 3.19%收500万；主体为Synthetic Issuer。', {'registered_region': 'Synthetic Region'}
    elif case_id == 'tool-similar-bonds':
        request = deepcopy(SIMILAR_EXAMPLE)
        text = 'SYN-A 3.19%收500万；有同发行人期限更短、估值一致的候选券。'
        private = {'candidate': 'SYN-B', 'remaining_days': 335, 'valuation_comparison': '估值与锚券一致'}
    else:
        request = deepcopy(CALC_EXAMPLE)
        text, private = 'SYN-A 3.19%收500万；所问价差已核算。', {'hypothetical_gap': '10.00 BP'}
    result = tools.invoke(request['name'], request['arguments'])
    assert result['status'] == 'ok'
    entry = {'request': request, 'result': result}
    return {'case_id': case_id, 'success': True,
        'fact_commit': {'through_revision': 21, 'public_tool_results': [deepcopy(entry)]},
        'reply_inputs': [{'analysis_tool_source': {'analysis_id': 'synthetic-analysis', 'through_revision': 21},
                          'analysis_tool_results': [deepcopy(entry)], 'tool_results': []}],
        'evidence': [{'tool': request['name'], 'arguments': request['arguments'], 'result': deepcopy(result)}],
        'reply': {'action': 'SEND', 'proposal': {'kind': 'REPLY', 'text': text,
                   'our_price': {'kind': 'YIELD', 'value': '3.19'}}, 'trader_view': private},
        'decision_commit': {'state': 'QUEUED'}}


@pytest.mark.parametrize('case_id', CASES)
def test_actual_tool_data_supports_substantive_public_or_private_answer(case_id):
    result = check_record(synthetic_record(case_id), case_id)
    assert result['passed'], result


@pytest.mark.parametrize('case_id', CASES)
def test_merely_calling_the_tool_and_then_only_quoting_is_not_completion(case_id):
    row = synthetic_record(case_id)
    row['reply']['proposal']['text'] = 'SYN-A 3.19%收500万，资料已查询。'
    row['reply']['trader_view'] = {}
    assert not check_record(row, case_id)['passed']


@pytest.mark.parametrize('failure', ['missing_delivery', 'wrong_watermark', 'forged_result', 'missing_receipt'])
def test_a_fluent_answer_cannot_replace_actual_result_delivery(failure):
    row = synthetic_record('tool-bond-info')
    if failure == 'missing_delivery': row['reply_inputs'][0]['analysis_tool_results'] = []
    elif failure == 'wrong_watermark': row['reply_inputs'][0]['analysis_tool_source']['through_revision'] = 20
    elif failure == 'missing_receipt': row['evidence'] = []
    else: row['reply_inputs'][0]['analysis_tool_results'][0]['result']['snapshot_id'] = 'forged-snapshot'
    assert not check_record(row, row['case_id'])['passed']


def test_issuer_search_identity_does_not_prove_the_returned_details_were_obtained():
    row = synthetic_record('tool-issuer-info')
    request = {'name': 'issuer_info', 'arguments': {'schema_version': '1', 'mode': 'search', 'query': 'SYN-ISSUER'}}
    result = BondTools(SimpleNamespace(snapshot=lambda: reference_snapshot('tool-issuer-info'))).invoke(
        request['name'], request['arguments'])
    entry = {'request': request, 'result': result}
    row['fact_commit']['public_tool_results'] = [deepcopy(entry)]
    row['reply_inputs'][0]['analysis_tool_results'] = [deepcopy(entry)]
    row['evidence'] = [{'tool': request['name'], 'arguments': request['arguments'], 'result': result}]
    result = check_record(row, row['case_id'])
    assert not result['passed'] and 'ISSUER_SEARCH_IDENTITY_IS_NOT_ISSUER_DETAILS' in result['errors']


@pytest.mark.parametrize('case_id', ['tool-similar-bonds', 'tool-bond-calc'])
def test_wrong_comparison_or_gap_cannot_pass_as_a_tool_answer(case_id):
    row = synthetic_record(case_id)
    if case_id == 'tool-similar-bonds': row['reply']['trader_view']['remaining_days'] = 999
    else: row['reply']['trader_view']['hypothetical_gap'] = '2 BP'
    assert not check_record(row, case_id)['passed']


def test_b_own_fresh_get_results_are_valid_without_any_a_tool_call():
    row = synthetic_record('tool-bond-info')
    context = row['reply_inputs'][0]
    context['tool_results'] = context['analysis_tool_results']
    context['analysis_tool_results'] = []
    context['analysis_tool_source'] = None
    row['fact_commit']['public_tool_results'] = []
    assert check_record(row, row['case_id'])['passed']
