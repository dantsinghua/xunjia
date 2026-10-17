"""Deterministic user-approved quotation rules; no model or external services."""
from dataclasses import replace
from decimal import Decimal
import pytest
from server.negotiation.facts import Quote, apply_patch, group_summary
from server.negotiation.prices import (
    AmbiguousPrice, InvalidFact, LimitPolicy, Price, Valuation,
    comparison, resolve_yield_suffix, valuation_quote, volume_wan,
)
from server.negotiation.settings import Aggregation, Settings
@pytest.mark.parametrize('valuation,bp,expected', [
    ('2.5818','-5','2.53'), ('2.9872','5','3.04'),
    ('2.9949','1','3.00'), ('3.0021','-1','2.99'), ('2.5250','0','2.53'),
])
def test_full_precision_before_two_decimal_rounding(valuation,bp,expected):
    assert str(valuation_quote(valuation,bp))==expected
@pytest.mark.parametrize('suffix,anchor,expected', [('53','2.5818','2.53'),('04','2.9872','3.04'),('20','2.2','2.20')])
def test_suffix_uses_explicit_context_not_internal_goal(suffix,anchor,expected):
    assert str(resolve_yield_suffix(suffix,contextual_anchor=anchor))==expected
def test_tied_suffix_and_missing_volume_unit_are_not_guessed():
    with pytest.raises(AmbiguousPrice):
        resolve_yield_suffix('50',contextual_anchor='3.00')
    with pytest.raises(InvalidFact):
        volume_wan('5000')
    assert volume_wan('5000',contextual_unit='w')==Decimal('5000')
    assert volume_wan('0.5亿')==Decimal('5000')
def test_settlement_required_for_cash_price_comparison_and_conversion():
    cash=Price('NET_PRICE','34.19')
    assert comparison(cash,{'kind':'NET_PRICE','value':'35'},our_side='BUY') is None
    with pytest.raises(InvalidFact):
        Price('NET_PRICE','34.19',computed_yield='25',calculation_ref='missing-settlement')
    settled=Price('NET_PRICE','34.19',settlement_date='2026-10-19')
    assert comparison(settled,{'kind':'NET_PRICE','value':'35'},our_side='BUY')['price_eligible']
def quote(price='3.12',volume='500',finality='FINAL',day='2026-10-17',side='ofr'):
    return Quote(side,Price('YIELD',price),volume,finality=finality,confirmed_day=day)
def summary(rows,target='1000'):
    return group_summary(our_side='BUY',target_volume=target,trade_day='2026-10-17',
                         boundary={'kind':'YIELD','value':'3.12'},quotes=rows)
def test_equal_target_is_goal_and_better_price_is_not_penalized():
    result=summary([('a',quote()),('b',quote('3.15'))])
    assert result['goal_ready'] and result['final_confirmed_volume_wan']=='1000'
    assert result['allocation_is_manual']
    assert sum(Decimal(lot['volume_wan']) for lot in result['whole_lots'])==1000
def test_no_splitting_counterparty_lots():
    result=summary([('a',quote(volume='1500'))])
    assert result['goal_ready']
    assert result['whole_lots'][0]['volume_wan']=='1500'
def test_amount_enough_price_not_enough_keeps_goal_false():
    result=summary([('a',quote('3.05','1000')),('b',quote('3.10','500'))])
    assert result['reported_volume_wan']=='1500'
    assert result['final_confirmed_volume_wan']=='0' and not result['goal_ready']
def test_price_met_but_more_negotiable_is_not_final_handoff():
    result=summary([('a',quote(volume='1500',finality='NEGOTIABLE'))])
    assert result['price_eligible_volume_wan']=='1500'
    assert not result['goal_ready']
@pytest.mark.parametrize('change',[
    {'validity':'WITHDRAWN'}, {'confirmed_day':'2026-10-16'},
    {'assertion':'INDICATIVE'}, {'assertion':'REPORTED'}, {'assertion':'QUESTION'},
])
def test_invalid_or_not_self_quoted_quantity_excluded(change):
    result=summary([('a',replace(quote(volume='1500'),**change))])
    assert result['reported_volume_wan']=='0'
    assert not result['goal_ready']
def test_partial_patch_preserves_price_changes_quantity_not_addition_and_resets_finality():
    old=quote(volume='500')
    new=apply_patch(old,{'volume_wan':'800'},evidence={'volume_wan':['incoming-2']})
    assert new.price==old.price and new.volume_wan=='800' and new.finality=='UNKNOWN'
    assert old.volume_wan=='500' and old.finality=='FINAL'
def test_new_day_or_reactivation_does_not_inherit_old_finality():
    old=quote(day='2026-10-16')
    refreshed=apply_patch(old,{'confirmed_day':'2026-10-17'},evidence={'confirmed_day':['new-inbound']})
    assert refreshed.finality=='UNKNOWN'
    reactivated=apply_patch(replace(old,validity='WITHDRAWN'),{'validity':'ACTIVE'},evidence={'validity':['new-inbound']})
    assert reactivated.finality=='UNKNOWN'
def test_cross_day_absolute_stable_bp_recalculates_from_new_chinabond_snapshot():
    absolute=LimitPolicy('ABSOLUTE','3.12')
    assert absolute.resolve(bond_code='SYN-A')=={'kind':'YIELD','value':'3.12'}
    policy=LimitPolicy('CHINABOND_BP','5')
    assert policy.resolve(bond_code='SYN-A') is None
    value=Valuation('SYN-A','2026-10-16','2.9872','source-v1')
    assert policy.resolve(bond_code='SYN-A',expected_valuation_date='2026-10-16',valuation=value)['value']=='3.04'
    with pytest.raises(InvalidFact):
        policy.resolve(bond_code='SYN-B',expected_valuation_date='2026-10-16',valuation=value)
def test_global_context_settings_and_measured_latency_gate():
    assert Settings.from_env({}).context_messages==20
    assert Settings.from_env({'XUNJIA_BOND_CONTEXT_MESSAGES':'30'}).context_messages==30
    with pytest.raises(InvalidFact):Settings(31)
    with pytest.raises(InvalidFact):Aggregation(1500,3000,800,'fast-measurement')
    a=Aggregation(1500,3000,8000,'test-evidence-not-a-production-measurement')
    assert not a.ready(now=1,first_pending=0,last_received=0)
    assert a.ready(now=3,first_pending=0,last_received=2.8)
