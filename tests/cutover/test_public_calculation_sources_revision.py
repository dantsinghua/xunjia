"""A successful public calculation cannot declassify private operand sources."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from server.bond_tools import BondTools
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import public_information, render_reply_plan
from server.reference_data import ReferenceSnapshot
from tests.cutover.test_rendered_reply_plan_revision import context, plan, public_record, DAY


def desk():
    value = context()
    value['context'][0].update(thread_id=value['thread_id'])
    value['context'].insert(0, {'message_id': 'our-sent', 'thread_id': value['thread_id'],
        'direction': 'out', 'historical': False, 'native_id': 'synthetic-native-sent',
        'ingest_revision': 20, 'trade_day': DAY, 'text': 'SYN-A 3.20%收，请报可做价量'})
    value['our_quote'].update(message_id='our-sent', native_id='synthetic-native-sent', sent_at=1000)
    return value


def calculation(left='3.20', right='3.10', *, hypothetical=True, refs=None):
    arguments = {'schema_version': '1', 'operation': 'yield_gap', 'inputs': {
        'hypothetical': hypothetical, 'our_side': 'BUY', 'peer_side': 'ofr',
        'same_basis': True, 'quote_type': 'yield', 'yield_basis': 'maturity',
        'peer_yield_basis': 'maturity', 'limit_yield_pct': left, 'peer_yield_pct': right,
        **(refs or {})}}
    result = BondTools().invoke('bond_calc', arguments)
    assert result['status'] == 'ok', result
    assert result['data']['inputs_used'] == arguments['inputs']
    assert Decimal(result['data']['result']['raw_gap_bp']) == (Decimal(left)-Decimal(right))*100
    return {'request': {'name': 'bond_calc', 'arguments': arguments}, 'result': result}


def valuation(verified=True):
    def fact(value):
        return {'value': value, 'quality': 'verified',
                'source_ref': 'public-valuation-source' if verified else 'self-declared-missing-source',
                'as_of': DAY, 'missing_reason': None}
    snapshot = ReferenceSnapshot('synthetic-public-valuation', DAY,
        sources=[{'id': 'public-valuation-source', 'origin': 'explicit synthetic public fixture'}],
        bonds=[{'bond_id': 'SYN-A', 'code': 'SYN-A', 'name': 'SYN-A',
            'valuation': {'yield_pct': fact('2.9872'), 'provider': fact('synthetic'),
                          'basis': fact('maturity'), 'valuation_date': fact(DAY)}}])
    arguments = {'schema_version': '1', 'mode': 'get', 'bond_id': 'SYN-A', 'include': ['valuation']}
    response = BondTools(SimpleNamespace(snapshot=lambda: snapshot)).invoke('bond_info', arguments)
    assert response['status'] in ('ok', 'partial'), response
    assert response['data']['valuation']['yield_pct']['quality'] == ('verified' if verified else 'source_unverified')
    return {'request': {'name': 'bond_info', 'arguments': arguments}, 'result': response}


def calc_rows(value):
    return [row for row in public_information(value) if row['source']['tool'] == 'bond_calc']


def publish(value, rows):
    before = deepcopy(value)
    rendered = render_reply_plan(value, plan(information_ids=[row['id'] for row in rows]))
    body = Engine.__new__(Engine)._validate_reply(value, deepcopy(rendered['proposal']))
    assert value == before
    assert body['our_price']['value'] == '3.18'
    assert all(row['text'] in body['text'] for row in rows)
    return rendered, body


@pytest.mark.parametrize('hypothetical', [False, True])
@pytest.mark.parametrize('inherited', [False, True])
def test_current_public_quote_and_actual_sent_price_keep_the_correct_ten_bp_answer(hypothetical, inherited):
    value = desk()
    value['analysis_tool_results' if inherited else 'tool_results'] = [calculation(hypothetical=hypothetical)]
    rows = calc_rows(value)
    assert len(rows) == 1
    _, body = publish(value, rows)
    assert '10.00BP' in body['text'] and '3.20' not in body['text'] and '3.10' not in body['text']


@pytest.mark.parametrize('private_source,secret', [('boundary', '3.12'), ('plan', '3.16'), ('other_lot', '3.13')])
@pytest.mark.parametrize('position', ['left', 'right'])
@pytest.mark.parametrize('hypothetical', [False, True])
def test_neither_calculation_operand_may_be_sourced_only_from_private_context(private_source, secret, position, hypothetical):
    value = desk()
    if private_source == 'boundary': value['boundary']['value'] = secret
    elif private_source == 'plan': value['plan']['opening_yield_pct'] = secret
    else:
        value['group_summary']['whole_lots'] = [{'member_id': 'different-counterparty',
            'quote_revision': 1, 'volume_wan': secret,
            'comparison': {'raw_gap': '-1.00', 'remaining_gap': '0', 'unit': 'BP', 'price_eligible': True}}]
    left, right = (secret, '3.10') if position == 'left' else ('3.20', secret)
    entry = calculation(left, right, hypothetical=hypothetical)
    value['tool_results'] = [entry]
    before = deepcopy(value)
    rows = calc_rows(value)
    assert not rows, 'A successful calculator response must not turn a private operand into a publishable BP answer'
    assert value == before


@pytest.mark.parametrize('refs', [
    {'source_ref': 'peer-a-message'},
    {'source_ref': 'synthetic-native-sent', 'authorization_ref': 'user-said-public'},
])
@pytest.mark.parametrize('hypothetical', [False, True])
def test_self_reported_source_reference_or_hypothetical_flag_does_not_prove_operand_publicity(refs, hypothetical):
    value = desk()
    value['tool_results'] = [calculation('3.12', '3.10', hypothetical=hypothetical, refs=refs)]
    assert calc_rows(value) == []


def test_public_operand_coinciding_with_private_boundary_is_allowed_by_source_not_blacklisted_by_value():
    value = desk()
    value['context'][-1]['text'] += '。另外假设同一收益率口径，3.12和3.10的差是多少BP？'
    assert value['boundary']['value'] == '3.12'
    value['tool_results'] = [calculation('3.12', '3.10')]
    rows = calc_rows(value)
    assert len(rows) == 1
    _, body = publish(value, rows)
    assert '2.00BP' in body['text']


@pytest.mark.parametrize('inherited', [False, True])
def test_yield_from_an_actual_verified_public_tool_is_a_valid_calculation_source(inherited):
    value = desk()
    public = valuation()
    value['analysis_tool_results' if inherited else 'tool_results'] = [public]
    value['tool_results'].append(calculation('3.10', '2.9872'))
    rows = calc_rows(value)
    assert len(rows) == 1
    _, body = publish(value, rows)
    assert '11.2800BP' in body['text']


def test_public_tool_source_ref_without_verified_source_is_not_public_numeric_evidence():
    value = desk()
    value['tool_results'] = [valuation(False), calculation('3.10', '2.9872')]
    assert calc_rows(value) == []


def test_unsent_opening_plan_is_not_an_actual_public_outbound_price():
    value = desk()
    value['context'] = [value['context'][-1]]
    value['our_quote'] = {'price': {'kind': 'YIELD', 'value': '3.20'}}
    value['tool_results'] = [calculation()]
    assert calc_rows(value) == []


def test_engine_rebuild_rejects_a_previously_public_calculation_when_operand_source_is_missing():
    value = desk()
    value['context'][-1]['text'] += '。假设收益率3.12和3.10，求BP差。'
    value['tool_results'] = [calculation('3.12', '3.10')]
    rows = calc_rows(value)
    assert len(rows) == 1
    rendered, _ = publish(value, rows)
    value['context'][-1]['text'] = 'SYN-A 3.10出500万'
    with pytest.raises((ValueError, InvalidFact)):
        Engine.__new__(Engine)._validate_reply(value, deepcopy(rendered['proposal']))


def test_rejecting_private_calculation_does_not_remove_the_four_correct_tool_answers():
    value = desk()
    value['tool_results'] = [public_record(tool) for tool in
                            ('bond_info', 'issuer_info', 'similar_bonds', 'bond_calc')]
    value['tool_results'].append(calculation('3.12', '3.10'))
    rows = public_information(value)
    assert {row['source']['tool'] for row in rows} == {'bond_info', 'issuer_info', 'similar_bonds', 'bond_calc'}
    assert len([row for row in rows if row['source']['tool'] == 'bond_calc']) == 1
    _, body = publish(value, rows)
    for content in ('SYN-ISSUER', 'Synthetic Issuer', 'AAA', 'SYN-B', '2.9872', '335', '365', '10.00BP'):
        assert content in body['text'], 'Do not satisfy the privacy gate by deleting legitimate tool answers'
    assert '2.00BP' not in body['text']


def test_eight_decimal_public_operands_keep_the_existing_real_calculator_domain():
    value = desk()
    value['context'][-1]['text'] += '。假设同一收益率口径，3.12345678和3.10相差多少BP？'
    value['tool_results'] = [calculation('3.12345678', '3.10')]
    before = deepcopy(value)
    rows = calc_rows(value)
    assert len(rows) == 1
    _, body = publish(value, rows)
    assert '2.34567800BP' in body['text']
    assert value == before


def test_nine_decimal_operand_remains_the_real_tools_existing_invalid_input_not_a_public_candidate():
    value = desk()
    value['context'][-1]['text'] += '。假设同一收益率口径，3.123456789和3.10相差多少BP？'
    arguments = deepcopy(calculation()['request']['arguments'])
    arguments['inputs']['limit_yield_pct'] = '3.123456789'
    response = BondTools().invoke('bond_calc', arguments)
    assert response['status'] == 'invalid_input'
    assert any(warning['code'] == 'INVALID_INPUT' for warning in response['warnings'])
    value['tool_results'] = [{'request': {'name': 'bond_calc', 'arguments': arguments}, 'result': response}]
    assert calc_rows(value) == []
