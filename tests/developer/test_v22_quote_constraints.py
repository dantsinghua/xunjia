"""Decimal grid bounds describe, but cannot replace, the existing price gates."""
from copy import deepcopy
from decimal import Decimal

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact


def context(side='BUY', base='3.20', step='2', limit='3.12'):
    return {'our_side': side, 'bond_code': 'SYN-A', 'trade_day': '2026-10-17',
        'unresolved_messages': [], 'reconfirm_day': None,
        'boundary': {'kind': 'YIELD', 'value': limit},
        'our_quote': {'price': {'kind': 'YIELD', 'value': base}, 'native_id': 'synthetic-native'},
        'plan': {'opening_yield_pct': base, 'step_bp': step, 'volume_wan': '1000'},
        'group_summary': {'reported_shortfall_wan': '500'},
        'quotes': {'bid': {'volume_wan': '500'}, 'ofr': {'volume_wan': '500'}}}


@pytest.mark.parametrize('side,base,step,limit,lower,upper', [
    ('BUY', '3.20', '1.5', '3.12', '3.19', '3.21'),
    ('SELL', '3.20', '1.5', '3.30', '3.19', '3.21'),
    ('BUY', '3.14', '2', '3.125', '3.13', '3.16'),
    ('SELL', '3.12', '2', '3.125', '3.10', '3.12'),
    ('BUY', '-0.10', '0.5', '-0.20', '-0.10', '-0.10'),
    ('SELL', '-0.10', '0.5', '0.00', '-0.10', '-0.10'),
])
def test_fractional_grid_is_inward_and_matches_original_economic_guard(side, base, step, limit, lower, upper):
    engine = object.__new__(Engine)
    data = context(side, base, step, limit)
    original = deepcopy(data)
    bounds = engine._quote_constraints(data)
    assert data == original
    assert bounds['status'] == 'AVAILABLE' and bounds['reason'] is None
    assert (bounds['min_yield_pct'], bounds['max_yield_pct']) == (lower, upper)
    assert bounds['tick_size'] == '0.01'
    middle = int(Decimal(base) * 100)
    for tick in range(middle-5, middle+6):
        value = Decimal(tick) / 100
        candidate = {'kind': 'REPLY', 'our_price': {'kind': 'YIELD', 'value': format(value, '.2f')},
            'text': 'SYN-A ' + format(value, '.2f') + '%' + ('收' if side == 'BUY' else '出') + '500万'}
        if Decimal(lower) <= value <= Decimal(upper):
            assert engine._validate_reply(data, candidate)['our_price']['value'] == format(value, '.2f')
        else:
            with pytest.raises(InvalidFact):
                engine._validate_reply(data, candidate)


def test_forged_projected_bounds_cannot_authorize_eight_bp_from_a_two_bp_base():
    engine = object.__new__(Engine)
    data = context()
    data['quote_constraints'] = {'status': 'AVAILABLE', 'min_yield_pct': '0.00', 'max_yield_pct': '99.00'}
    with pytest.raises(InvalidFact, match='maximum step'):
        engine._validate_reply(data, {'kind': 'REPLY', 'text': 'SYN-A 3.12%收500万',
                                     'our_price': {'kind': 'YIELD', 'value': '3.12'}})
