"""Additional edge checks; synthetic messages and production validation only."""
from types import SimpleNamespace
import pytest
from server.negotiation.slots import collect_candidates
from server.negotiation.prices import InvalidFact
from server.reference_data import ReferenceSnapshot
from test_reference_window_policy import Desk, OWNER, DAY
from test_review_valuation_recovery import source_market
@pytest.mark.parametrize("text", ["SYN-A 估值-5%收", "估值-50万元", "估值加5000万", "估值-5.2%出"])
def test_explicit_non_bp_units_do_not_become_bp(text):
    candidates=collect_candidates({'bond_code':'SYN-A','messages':[{'message_id':'edge','text':text,'direction':'in'}]})
    assert not any('CHINABOND_BP' in row['roles'] for row in candidates)
@pytest.mark.parametrize('offset,accepted', [('-5',True), ('5',False)])
def test_bilateral_bp_offsets_cannot_cross_sides(tmp_path,offset,accepted):
    case=Desk(tmp_path/'bilateral.sqlite')
    case.service.reference_provider=SimpleNamespace(snapshot=lambda:ReferenceSnapshot('edge',DAY,
        bonds=[{'code':'SYN-A','valuation_date':DAY,'valuation_yield_pct':'2.5818'}]))
    case.service.calendar=SimpleNamespace(latest_valuation_day=lambda _:DAY)
    incoming=case.inbound('SYN-A 估值-5收5000万，估值+5出1000万')
    observation=case.observation(incoming,price={'kind':'YIELD','notation':'CHINABOND_BP','value':offset},
        assertion='QUOTE',validity='ACTIVE')
    observation['side']='bid'
    result=case.analyze([observation])
    assert bool(result['accepted']) is accepted,result
    if accepted:assert case.store.quote(OWNER,case.member,'bid').price.value=='2.53'
    else:assert case.store.quote(OWNER,case.member,'bid').price is None
def test_inno_same_revision_conflict_does_not_pick_the_more_favorable_value():
    market=source_market('2.60')
    market.source.book[1][2]={**market.source.book[1][1],'cnbd_yield_mat':'2.70'}
    with pytest.raises(InvalidFact,match='CONFLICT'):
        market.valuation('SYN-A')
