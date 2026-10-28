"""One-sided hard price limits and a soft initial concession reference."""
from copy import deepcopy
from decimal import Decimal

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from test_production_flow import OWNER, desk
from test_final_price_preservation import context as final_context
from test_reply_text_repair import reply_case, unchanged_facts


FIELDS = {'status', 'price_kind', 'tick_size', 'min_yield_pct', 'max_yield_pct',
          'base_yield_pct', 'base_source', 'reference_step_bp', 'reason'}


def constraints(context):
    before = deepcopy(context)
    result = Engine._quote_constraints(context)
    assert context == before, 'Projecting a domain cannot mutate facts'
    assert set(result) == FIELDS
    assert result['price_kind'] == 'YIELD' and result['tick_size'] == '0.01'
    return result


def plain(side='BUY'):
    value = final_context(side)
    # This fixture isolates the private limit and soft step, without a current peer cap.
    value['quotes']['ofr' if side == 'BUY' else 'bid'].update(finality='NEGOTIABLE', validity='UNCONFIRMED')
    return value


def proposal(side, price):
    return {'kind': 'REPLY', 'our_price': {'kind': 'YIELD', 'value': price},
            'text': 'SYN-A '+price+'%'+('收' if side == 'BUY' else '出')}


@pytest.mark.parametrize('side,final,boundary,step,minimum,maximum', [
    ('BUY', False, '3.12', '2', '3.12', None),
    ('SELL', False, '3.12', '2', None, '3.12'),
    ('BUY', True, '3.12', '2', '3.20', None),
    ('SELL', True, '3.12', '2', None, '3.00'),
    ('BUY', False, '3.195', '2', '3.20', None),
    ('SELL', False, '3.005', '2', None, '3.00'),
    ('BUY', False, '3.12', '0.5', '3.12', None),
    ('SELL', False, '3.12', '0.5', None, '3.12'),
])
def test_projection_preserves_hard_boundary_and_final_peer_without_turning_reference_into_a_cap(
        side, final, boundary, step, minimum, maximum):
    value = final_context(side) if final else plain(side)
    value['boundary']['value'] = boundary
    value['plan']['step_bp'] = step
    result = constraints(value)
    assert result['status'] == 'AVAILABLE' and result['reason'] is None
    assert (result['min_yield_pct'], result['max_yield_pct']) == (minimum, maximum)
    assert result['base_source'] == 'LAST_SENT'
    assert Decimal(result['reference_step_bp']) == Decimal(step)
    bound = Decimal(minimum if side == 'BUY' else maximum)
    for delta in ('-0.01', '0', '0.01', '0.05', '0.50'):
        candidate = bound+Decimal(delta)*(1 if side == 'BUY' else -1)
        price = format(candidate, '.2f')
        if Decimal(delta) >= 0:
            assert Engine.__new__(Engine)._validate_reply(value, proposal(side, price))['our_price']['value'] == price
        else:
            with pytest.raises(InvalidFact):
                Engine.__new__(Engine)._validate_reply(value, proposal(side, price))


@pytest.mark.parametrize('side,boundary', [('BUY', '3.25'), ('SELL', '2.95')])
def test_previous_sent_price_does_not_create_an_unapproved_second_hard_boundary(side, boundary):
    value = plain(side)
    value['boundary']['value'] = boundary
    result = constraints(value)
    assert result['status'] == 'AVAILABLE'
    assert result['min_yield_pct' if side == 'BUY' else 'max_yield_pct'] == boundary
    assert Engine.__new__(Engine)._validate_reply(value, proposal(side, boundary))


@pytest.mark.parametrize('missing', ['boundary', 'cash_boundary', 'step', 'no_base', 'uncomparable_actual_cash'])
def test_unknown_critical_inputs_do_not_invent_an_authorized_domain(missing):
    value = plain()
    if missing == 'boundary': value['boundary'] = None
    elif missing == 'cash_boundary': value['boundary'] = {'kind': 'NET_PRICE', 'value': '98'}
    elif missing == 'step': value['plan'].pop('step_bp')
    elif missing == 'no_base': value['our_quote'] = {}; value['plan'].pop('opening_yield_pct')
    else:
        value['our_quote']['price'] = {'kind': 'NET_PRICE', 'value': '98'}
        value['plan']['opening_yield_pct'] = '3.20'
    result = constraints(value)
    assert result['status'] == 'UNAVAILABLE' and result['reason']
    assert result['min_yield_pct'] is result['max_yield_pct'] is None


def test_authorized_opening_is_reference_only_when_no_actual_sent_quote_exists():
    value = plain()
    value['our_quote'] = {}
    result = constraints(value)
    assert result['status'] == 'AVAILABLE' and result['base_source'] == 'AUTHORIZED_OPENING'
    assert result['base_yield_pct'] == '3.20'
    value['our_quote'] = {'price': {'kind': 'YIELD', 'value': '3.22'}, 'native_id': 'actual-synthetic'}
    result = constraints(value)
    assert result['base_source'] == 'LAST_SENT' and result['base_yield_pct'] == '3.22'
    assert (result['min_yield_pct'], result['max_yield_pct']) == ('3.12', None)


@pytest.mark.parametrize('change', [
    {'validity': 'UNCONFIRMED'},
    {'validity': 'WITHDRAWN'}, {'assertion': 'INDICATIVE'}, {'assertion': 'QUESTION'},
    {'assertion': 'REPORTED'}, {'confirmed_day': '2026-10-16'},
])
def test_noneligible_peer_fact_does_not_add_a_final_price_floor(change):
    value = final_context()
    value['quotes']['ofr'].update(change)
    assert (constraints(value)['min_yield_pct'], constraints(value)['max_yield_pct']) == ('3.12', None)
    assert Engine.__new__(Engine)._validate_reply(value, proposal('BUY', '3.15'))


def test_other_side_and_noncomparable_final_cash_do_not_create_an_extra_boundary():
    value = plain()
    value['quotes']['bid'] = deepcopy(value['quotes']['ofr'])
    value['quotes']['bid']['finality'] = 'FINAL'
    value['quotes']['ofr'].update(finality='FINAL', price={'kind': 'NET_PRICE', 'value': '98'})
    assert (constraints(value)['min_yield_pct'], constraints(value)['max_yield_pct']) == ('3.12', None)
    assert Engine.__new__(Engine)._validate_reply(value, proposal('BUY', '3.15'))


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_verified_cash_final_price_uses_the_same_comparable_yield_as_existing_guard(side):
    value = final_context(side)
    peer = value['quotes']['ofr' if side == 'BUY' else 'bid']
    peer['price'] = {'kind': 'NET_PRICE', 'value': '98', 'settlement_date': '2026-10-17',
                     'computed_yield': peer['price']['value'], 'calculation_ref': 'verified-synthetic'}
    expected = ('3.20', None) if side == 'BUY' else (None, '3.00')
    result = constraints(value)
    assert (result['min_yield_pct'], result['max_yield_pct']) == expected


def test_actual_reply_stage_receives_soft_reference_and_can_queue_a_five_bp_concession(desk):
    case = reply_case(desk)
    seen = []
    def decide(context):
        seen.append(deepcopy(context))
        return {'schema_version': '2.2', 'action': 'SEND', 'proposal': render_reply_plan(context,
            {'kind': 'REPLY', 'quote_value': '3.15', 'text': 'SYN-A {own_quote}，你再看看？'})['proposal']}
    case.flows.decide = decide
    result = case.app.state.negotiation_engine.decide_member(OWNER, case.peer['member_id'])
    assert result['state'] == 'QUEUED' and len(seen) == 1
    domain = seen[0]['quote_constraints']
    assert (domain['min_yield_pct'], domain['max_yield_pct']) == ('3.12', None)
    assert domain['reference_step_bp'] == '2'
    unchanged_facts(case)
    case.app.state.dispatcher.run_once()
    assert len(case.native.sent) == 2 and '3.15%收' in case.native.sent[-1]['text']


@pytest.mark.parametrize('reason', ['unresolved', 'cross_day'])
def test_existing_control_rules_still_reject_an_economic_reply_inside_the_domain(reason):
    value = plain()
    assert constraints(value)['status'] == 'AVAILABLE'
    if reason == 'unresolved': value['unresolved_messages'] = [{'message_id': 'synthetic-ambiguous'}]
    else: value['reconfirm_day'] = '2026-10-17'
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(value, proposal('BUY', '3.20'))
