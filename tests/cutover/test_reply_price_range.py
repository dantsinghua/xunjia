"""Displayed quote bounds must equal the existing decimal economic guards."""
from copy import deepcopy
from decimal import Decimal

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from test_production_flow import OWNER, desk
from test_final_price_preservation import context as final_context
from test_reply_text_repair import reply_case, unchanged_facts


FIELDS = {'status', 'price_kind', 'tick_size', 'min_yield_pct', 'max_yield_pct',
          'base_yield_pct', 'base_source', 'max_step_bp', 'reason'}


def constraints(context):
    engine = Engine.__new__(Engine)
    method = getattr(engine, '_quote_constraints', None)
    assert callable(method), 'The quote_constraints projection helper is missing'
    before = deepcopy(context)
    result = method(context)
    assert context == before, 'Computing a quote interval must not mutate source facts'
    assert set(result) == FIELDS
    assert result['price_kind'] == 'YIELD' and result['tick_size'] == '0.01'
    return result


def plain(side='BUY'):
    value = final_context(side)
    value['quotes']['ofr' if side == 'BUY' else 'bid']['finality'] = 'NEGOTIABLE'
    return value


def proposal(side, price):
    return {'kind': 'REPLY', 'our_price': {'kind': 'YIELD', 'value': price},
            'text': 'SYN-A '+price+'%'+('收' if side == 'BUY' else '出')}


@pytest.mark.parametrize('side,final,boundary,step,minimum,maximum', [
    ('BUY', False, '3.12', '2', '3.18', '3.22'),
    ('SELL', False, '3.12', '2', '2.98', '3.02'),
    ('BUY', True, '3.12', '2', '3.20', '3.22'),
    ('SELL', True, '3.12', '2', '2.98', '3.00'),
    ('BUY', False, '3.195', '2', '3.20', '3.22'),
    ('SELL', False, '3.005', '2', '2.98', '3.00'),
    ('BUY', False, '3.12', '0.5', '3.20', '3.20'),
    ('SELL', False, '3.12', '0.5', '3.00', '3.00'),
])
def test_decimal_bounds_are_exactly_the_existing_guard_on_the_two_decimal_grid(
        side, final, boundary, step, minimum, maximum):
    value = final_context(side) if final else plain(side)
    value['boundary']['value'] = boundary
    value['plan']['step_bp'] = step
    result = constraints(value)
    assert result['status'] == 'AVAILABLE' and result['reason'] is None
    assert (result['min_yield_pct'], result['max_yield_pct']) == (minimum, maximum)
    assert result['base_source'] == 'LAST_SENT'
    assert Decimal(result['max_step_bp']) == Decimal(step)
    engine = Engine.__new__(Engine)
    # One closed finite neighborhood covers inside, exact endpoints and one
    # tick beyond each endpoint; test against the real existing validator.
    lower, upper = Decimal(minimum), Decimal(maximum)
    candidate = lower - Decimal('0.01')
    while candidate <= upper + Decimal('0.01'):
        price = format(candidate, '.2f')
        if lower <= candidate <= upper:
            assert engine._validate_reply(value, proposal(side, price))['our_price']['value'] == price
        else:
            with pytest.raises(InvalidFact): engine._validate_reply(value, proposal(side, price))
        candidate += Decimal('0.01')


@pytest.mark.parametrize('side,boundary', [('BUY', '3.25'), ('SELL', '2.95')])
def test_empty_intersection_never_suggests_an_out_of_step_or_out_of_limit_price(side, boundary):
    value = plain(side)
    value['boundary']['value'] = boundary
    result = constraints(value)
    assert result['status'] == 'EMPTY' and result['reason']
    assert result['min_yield_pct'] is result['max_yield_pct'] is None
    assert result['base_source'] == 'LAST_SENT'
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(value, proposal(side, boundary))


@pytest.mark.parametrize('missing', ['boundary', 'cash_boundary', 'step', 'no_base', 'uncomparable_actual_cash'])
def test_unknown_critical_inputs_do_not_invent_an_authorized_interval(missing):
    value = plain()
    if missing == 'boundary': value['boundary'] = None
    elif missing == 'cash_boundary': value['boundary'] = {'kind': 'NET_PRICE', 'value': '98'}
    elif missing == 'step': value['plan'].pop('step_bp')
    elif missing == 'no_base': value['our_quote'] = {}; value['plan'].pop('opening_yield_pct')
    else:
        value['our_quote']['price'] = {'kind': 'NET_PRICE', 'value': '98'}
        # This valid opening must not be substituted for an actual sent cash
        # quote whose comparable yield is currently unavailable.
        value['plan']['opening_yield_pct'] = '3.20'
    result = constraints(value)
    assert result['status'] == 'UNAVAILABLE' and result['reason']
    assert result['min_yield_pct'] is result['max_yield_pct'] is None


def test_authorized_opening_is_used_only_without_a_last_sent_price():
    value = plain()
    value['our_quote'] = {}
    result = constraints(value)
    assert result['status'] == 'AVAILABLE' and result['base_source'] == 'AUTHORIZED_OPENING'
    assert result['base_yield_pct'] == '3.20'
    value['our_quote'] = {'price': {'kind': 'YIELD', 'value': '3.22'}, 'native_id': 'actual-synthetic'}
    result = constraints(value)
    assert result['base_source'] == 'LAST_SENT' and result['base_yield_pct'] == '3.22'
    assert (result['min_yield_pct'], result['max_yield_pct']) == ('3.20', '3.24')


@pytest.mark.parametrize('change', [
    {'finality': 'NEGOTIABLE'}, {'finality': 'UNKNOWN'}, {'validity': 'UNCONFIRMED'},
    {'validity': 'WITHDRAWN'}, {'assertion': 'INDICATIVE'}, {'assertion': 'QUESTION'},
    {'assertion': 'REPORTED'}, {'confirmed_day': '2026-10-16'},
])
def test_noneligible_peer_fact_does_not_add_a_final_price_floor(change):
    value = final_context()
    value['quotes']['ofr'].update(change)
    result = constraints(value)
    assert (result['min_yield_pct'], result['max_yield_pct']) == ('3.18', '3.22')
    assert Engine.__new__(Engine)._validate_reply(value, proposal('BUY', '3.18'))


def test_other_side_and_noncomparable_final_cash_do_not_create_an_extra_boundary():
    value = plain()
    value['quotes']['bid'] = deepcopy(value['quotes']['ofr'])
    value['quotes']['bid']['finality'] = 'FINAL'
    value['quotes']['ofr'].update(finality='FINAL', price={'kind': 'NET_PRICE', 'value': '98'})
    result = constraints(value)
    assert (result['min_yield_pct'], result['max_yield_pct']) == ('3.18', '3.22')
    assert Engine.__new__(Engine)._validate_reply(value, proposal('BUY', '3.18'))


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_verified_cash_final_price_uses_the_same_comparable_yield_as_existing_guard(side):
    value = final_context(side)
    peer = value['quotes']['ofr' if side == 'BUY' else 'bid']
    peer['price'] = {'kind': 'NET_PRICE', 'value': '98', 'settlement_date': '2026-10-17',
                     'computed_yield': peer['price']['value'], 'calculation_ref': 'verified-synthetic'}
    result = constraints(value)
    expected = ('3.20', '3.22') if side == 'BUY' else ('2.98', '3.00')
    assert (result['min_yield_pct'], result['max_yield_pct']) == expected


@pytest.mark.parametrize('final', [False, True])
def test_actual_reply_stage_receives_the_interval_but_still_rejects_an_eight_bp_move(desk, final):
    case = reply_case(desk, final=final)
    observed = []
    def decide(context):
        observed.append(deepcopy(context))
        # The projection is an internal explanation, never trusted input from
        # a model. Even a fabricated model constraint cannot authorize 8 BP.
        return {'schema_version': '2.2', 'action': 'SEND',
                'proposal': proposal('BUY', '3.12'),
                'quote_constraints': {'status': 'AVAILABLE', 'min_yield_pct': '3.12'}}
    case.flows.decide = decide
    result = case.app.state.negotiation_engine.decide_member(OWNER, case.peer['member_id'])
    assert len(observed) == 1
    interval = observed[0].get('quote_constraints')
    assert isinstance(interval, dict), 'The real B context is not wired to the interval projection'
    expected = ('3.20', '3.22') if final else ('3.18', '3.22')
    assert (interval['min_yield_pct'], interval['max_yield_pct']) == expected
    assert result['state'] == 'REJECTED' and result['command_id'] is None
    assert not result['repair']['attempted'], 'An economic failure cannot become text repair'
    unchanged_facts(case)
    case.app.state.dispatcher.run_once()
    assert len(case.native.sent) == 1


@pytest.mark.parametrize('reason', ['unresolved', 'cross_day'])
def test_existing_control_rules_still_reject_an_economic_reply_inside_the_interval(reason):
    value = plain()
    assert constraints(value)['status'] == 'AVAILABLE'
    if reason == 'unresolved': value['unresolved_messages'] = [{'message_id': 'synthetic-ambiguous'}]
    else: value['reconfirm_day'] = '2026-10-17'
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(value, proposal('BUY', '3.20'))
