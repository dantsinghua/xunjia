"""A cash quote is convertible only with peer settlement and sourced terms."""
from types import SimpleNamespace
from server.bond_tools import BondTools
from server.reference_data import ReferenceSnapshot
from server.negotiation.engine import Engine


def normalize(text, *, verified=True):
    terms={'terms_snapshot_id':'synthetic-terms','quality':'verified' if verified else 'unverified',
           'source_ref':'synthetic-source','bond_type':'zero_coupon','convention':'ACT/365F-compounded-v1',
           'compounding_frequency':1,'yield_basis':'maturity','settlement_cashflow':'exclude',
           'cashflows':[{'payment_date':'2027-10-17','amount_per_100':'100'}],
           'accrued_interest_per_100':'0','accrued_interest_settlement_date':'2026-10-17'}
    class Source:
        def snapshot(self):
            return ReferenceSnapshot('synthetic-snapshot','2026-10-17',sources=[{'id':'synthetic-source'}],
                bonds=[{'bond_id':'SYN-A','code':'SYN-A'}],terms={'SYN-A':terms})
    provider=Source()
    service=SimpleNamespace(store=None,commands_core=None,reference_provider=provider,calendar=None)
    engine=Engine(service,None,BondTools(provider=provider))
    snapshot={'members':[{'member_id':'p','bond_code':'SYN-A','context':[],
                         'quotes':{'bid':{},'ofr':{}},'plan':{}}],
              'messages':[{'message_id':'m','direction':'in','text':text,'trade_day':'2026-10-17','ingest_revision':1}]}
    output={'scopes':{'m':['p']},'observations':[{'member_id':'p','side':'ofr',
        'changes':{'price':{'kind':'NET_PRICE','value':'97','settlement_date':'2026-10-17'}},
        'evidence':{'price':['m']}}]}
    _,observations,_=engine._normalize(snapshot,output)
    return observations[0]['changes']['price'],output


def test_confirmed_cash_settlement_uses_actual_public_calculator_and_terms():
    price,output=normalize('SYN-A 净价97出500万，确认2026-10-17结算')
    assert price['computed_yield']=='3.09'
    assert price['calculation_ref'].startswith('calc-')
    assert output['_verified_calculations'][0]['response']['data']['hypothetical'] is False


def test_model_settlement_without_counterparty_confirmation_keeps_raw_cash_price():
    price,_=normalize('SYN-A 净价97出500万')
    assert price['kind']=='NET_PRICE' and price['value']=='97'
    assert price['settlement_date'] is None and price['computed_yield'] is None


def test_unverified_cashflows_never_produce_derived_yield():
    price,_=normalize('SYN-A 净价97出500万，确认2026-10-17结算',verified=False)
    assert price['value']=='97' and price['computed_yield'] is None
