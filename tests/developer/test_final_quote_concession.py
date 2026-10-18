"""A shared quantity shortfall does not justify worsening an agreed final price."""
from copy import deepcopy

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact


def context(our_side):
    peer_side = 'ofr' if our_side == 'BUY' else 'bid'
    value = '3.20' if our_side == 'BUY' else '3.00'
    price = {'kind': 'YIELD', 'value': value}
    return {
        'our_side': our_side, 'bond_code': 'SYN-A', 'trade_day': '2026-10-17',
        'unresolved_messages': [], 'reconfirm_day': None,
        'boundary': {'kind': 'YIELD', 'value': '3.12'},
        'our_quote': {'price': price, 'native_id': 'actual-synthetic-sent'},
        'plan': {'opening_yield_pct': value, 'step_bp': '2', 'volume_wan': '20000'},
        'group_summary': {'reported_shortfall_wan': '19500', 'goal_ready': False},
        'quotes': {peer_side: {'price': deepcopy(price), 'volume_wan': '500',
                              'assertion': 'QUOTE', 'validity': 'ACTIVE',
                              'finality': 'FINAL', 'confirmed_day': '2026-10-17'}},
    }


def proposal(side, value):
    return {'kind': 'REPLY', 'our_price': {'kind': 'YIELD', 'value': value},
            'text': 'SYN-A ' + value + '% ' + ('收' if side == 'BUY' else '出')}


@pytest.mark.parametrize('side,worse', [('BUY', '3.18'), ('SELL', '3.02')])
def test_final_accepted_peer_price_cannot_be_worsened_for_shared_shortfall(side, worse):
    engine = object.__new__(Engine)
    with pytest.raises(InvalidFact):
        engine._validate_reply(context(side), proposal(side, worse))


@pytest.mark.parametrize('side,same', [('BUY', '3.20'), ('SELL', '3.00')])
def test_same_final_price_and_nonquote_quantity_question_remain_permitted(side, same):
    engine = object.__new__(Engine)
    assert engine._validate_reply(context(side), proposal(side, same))['our_price']['value'] == same
    question = {'kind': 'ASK_VOLUME', 'text': 'SYN-A 还能增加一些量吗？'}
    assert engine._validate_reply(context(side), question) == question


@pytest.mark.parametrize('changes', [
    {'confirmed_day': '2026-10-16'}, {'validity': 'WITHDRAWN'},
    {'assertion': 'REPORTED'}, {'assertion': 'QUESTION'},
])
def test_stale_or_nonquote_peer_prices_do_not_become_new_price_constraints(changes):
    engine = object.__new__(Engine)
    ctx = context('BUY')
    ctx['quotes']['ofr'].update(changes)
    assert engine._validate_reply(ctx, proposal('BUY', '3.18'))['our_price']['value'] == '3.18'


def test_final_but_below_boundary_quote_does_not_block_a_better_authorized_counter():
    engine = object.__new__(Engine)
    ctx = context('BUY')
    ctx['quotes']['ofr']['price']['value'] = '3.10'
    assert engine._validate_reply(ctx, proposal('BUY', '3.18'))['our_price']['value'] == '3.18'
