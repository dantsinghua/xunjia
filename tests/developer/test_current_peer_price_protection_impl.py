"""Current executable peer prices constrain replies without changing facts."""
from copy import deepcopy

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact


def context(side, finality='NEGOTIABLE'):
    peer_side = 'bid' if side == 'SELL' else 'ofr'
    return {'our_side':side, 'bond_code':'SYN-PRICE', 'trade_day':'2026-10-27',
            'unresolved_messages':[], 'reconfirm_day':None,
            'boundary':{'kind':'YIELD','value':'2.12'},
            'our_quote':{'price':{'kind':'YIELD','value':'1.98' if side=='SELL' else '2.26'}, 'native_id':'synthetic'},
            'plan':{'opening_yield_pct':'1.98' if side=='SELL' else '2.26','step_bp':'1','volume_wan':'20000'},
            'group_summary':{'reported_shortfall_wan':'19000','goal_ready':False},
            'quotes':{peer_side:{'price':{'kind':'YIELD','value':'2.10' if side=='SELL' else '2.14'},
                                 'volume_wan':'1000','assertion':'QUOTE','validity':'ACTIVE',
                                 'finality':finality,'confirmed_day':'2026-10-27'}}}


def proposal(side, value):
    return {'kind':'REPLY','text':'SYN-PRICE '+value+'%'+('出' if side=='SELL' else '收'),
            'our_price':{'kind':'YIELD','value':value}}


@pytest.mark.parametrize('side',['SELL','BUY'])
@pytest.mark.parametrize('finality',['UNKNOWN','NEGOTIABLE','FINAL'])
def test_current_peer_quote_protects_each_finality_and_preserves_facts(side, finality):
    value = context(side, finality)
    before = deepcopy(value)
    constraints = Engine._quote_constraints(value)
    assert constraints['max_yield_pct' if side=='SELL' else 'min_yield_pct'] == ('2.10' if side=='SELL' else '2.14')
    assert constraints['min_yield_pct' if side=='SELL' else 'max_yield_pct'] is None
    assert constraints['reference_step_bp'] == '1' and constraints['base_yield_pct'] == ('1.98' if side=='SELL' else '2.26')
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(value, proposal(side,'2.12'))
    for acceptable in (['2.10','2.05'] if side=='SELL' else ['2.14','2.19']):
        assert Engine.__new__(Engine)._validate_reply(value,proposal(side,acceptable))['our_price']['value'] == acceptable
    assert value == before


@pytest.mark.parametrize('side',['SELL','BUY'])
@pytest.mark.parametrize('change',[{'assertion':'QUESTION'},{'assertion':'REPORTED'},
    {'assertion':'INDICATIVE'},{'validity':'WITHDRAWN'},{'validity':'UNCONFIRMED'},
    {'confirmed_day':'2026-10-26'},{'price':None}])
def test_nonexecuting_or_old_peer_fact_does_not_add_an_acceptable_price(side, change):
    value = context(side)
    value['quotes']['bid' if side=='SELL' else 'ofr'].update(change)
    before = deepcopy(value)
    domain = Engine._quote_constraints(value)
    assert domain['max_yield_pct' if side=='SELL' else 'min_yield_pct'] == '2.12'
    assert Engine.__new__(Engine)._validate_reply(value,proposal(side,'2.12'))
    assert value == before


@pytest.mark.parametrize('side',['SELL','BUY'])
def test_wrong_side_or_outside_private_limit_cannot_set_peer_guard(side):
    value = context(side)
    wanted = 'bid' if side=='SELL' else 'ofr'
    value['quotes']['ofr' if side=='SELL' else 'bid'] = value['quotes'].pop(wanted)
    assert Engine._quote_constraints(value)['max_yield_pct' if side=='SELL' else 'min_yield_pct'] == '2.12'
    value = context(side)
    value['quotes'][wanted]['price']['value'] = '2.14' if side=='SELL' else '2.10'
    assert Engine._quote_constraints(value)['max_yield_pct' if side=='SELL' else 'min_yield_pct'] == '2.12'
    with pytest.raises(InvalidFact,match='internal limit'):
        Engine.__new__(Engine)._validate_reply(value,proposal(side,'2.13' if side=='SELL' else '2.11'))


@pytest.mark.parametrize('side',['SELL','BUY'])
@pytest.mark.parametrize('kind',['NET_PRICE','DIRTY_PRICE'])
@pytest.mark.parametrize('finality',['UNKNOWN','NEGOTIABLE','FINAL'])
def test_verified_comparable_cash_price_gets_same_current_quote_protection(side, kind, finality):
    value = context(side,finality)
    peer = value['quotes']['bid' if side=='SELL' else 'ofr']
    peer['price'] = {'kind':kind,'value':'99','settlement_date':'2026-10-28',
                     'computed_yield':peer['price']['value'],'calculation_ref':'calc-synthetic-verified'}
    before = deepcopy(value)
    assert Engine._quote_constraints(value)['max_yield_pct' if side=='SELL' else 'min_yield_pct'] == ('2.10' if side=='SELL' else '2.14')
    with pytest.raises(InvalidFact):Engine.__new__(Engine)._validate_reply(value,proposal(side,'2.12'))
    assert value == before


@pytest.mark.parametrize('side',['SELL','BUY'])
@pytest.mark.parametrize('kind',['NET_PRICE','DIRTY_PRICE'])
def test_cash_without_trusted_conversion_cannot_invent_acceptable_yield(side, kind):
    value = context(side)
    peer = value['quotes']['bid' if side=='SELL' else 'ofr']
    peer['price'] = {'kind':kind,'value':'99','settlement_date':'2026-10-28'}
    assert Engine._quote_constraints(value)['max_yield_pct' if side=='SELL' else 'min_yield_pct'] == '2.12'
    assert Engine.__new__(Engine)._validate_reply(value,proposal(side,'2.12'))
    peer['price']['computed_yield'] = '2.10' if side=='SELL' else '2.14'
    domain = Engine._quote_constraints(value)
    assert domain['status'] == 'UNAVAILABLE' and domain['min_yield_pct'] is domain['max_yield_pct'] is None
    with pytest.raises(InvalidFact):Engine.__new__(Engine)._validate_reply(value,proposal(side,'2.12'))
