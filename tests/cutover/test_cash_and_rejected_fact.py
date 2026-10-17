"""Real deterministic calculator and field-failure coverage through production A."""
from decimal import Decimal
from test_production_flow import OWNER,desk,raw
from test_acceptance_contract import setup_groups,observation,parser,current
from server.bond_tools import BondTools
from server.reference_data import ReferenceSnapshot
class Terms:
    def snapshot(self):
        return ReferenceSnapshot('synthetic-terms-snapshot','2026-10-17',
            sources=[{'id':'synthetic','origin':'fixture','transport':'local','dataset_version':'1','as_of':'2026-10-17'}],
            bonds=[{'bond_id':'SYN-A','code':'SYN-A'}],
            terms={'SYN-A':{'terms_snapshot_id':'terms-v1','quality':'verified','source_ref':'synthetic',
                'bond_type':'zero_coupon','cashflows':[{'payment_date':'2027-10-17','amount_per_100':'100'}],
                'convention':'ACT/365F-compounded-v1','compounding_frequency':1,'yield_basis':'maturity',
                'settlement_cashflow':'exclude','accrued_interest_per_100':'0',
                'accrued_interest_settlement_date':'2026-10-17'}})
def install_terms(app):
    source=Terms();tools=BondTools(provider=source)
    app.state.inquiry.reference_provider=source
    app.state.negotiation_engine.tools=tools
def cash_observation(peer,message,date):
    value=observation(peer,message)
    value['changes']['price']={'kind':'NET_PRICE','value':'34.19','settlement_date':date}
    return value
def test_net_price_uses_actual_settlement_and_real_verified_cashflow_solver(desk):
    app,_,flows,_,_=desk;peer=setup_groups(desk)['SYN-A']['peers'][0]
    install_terms(app)
    flows.analyze=parser(lambda snapshot,msg:[cash_observation(peer,msg,'2026-10-17')])
    raw(app,peer['thread_id'],'SYN-A 净价34.19出500万，T+0结算')
    app.state.negotiation_engine.run_once()
    result=current(app,peer)
    assert result.price.kind=='NET_PRICE' and result.price.value=='34.19'
    assert result.price.settlement_date=='2026-10-17'
    assert abs(Decimal(result.price.computed_yield)-Decimal('192.48'))<Decimal('.01')
    assert result.price.calculation_ref.startswith('calc-')
    with app.state.repository.db.transaction(write=False) as tx:
        row=tx.execute("SELECT data_json FROM xj2_audit WHERE owner_id=? AND kind='cash_conversion_evidence'",(OWNER,)).fetchone()
    assert row and 'terms-v1' in row['data_json']
def test_fabricated_settlement_cannot_authorize_conversion_but_raw_cash_fact_survives(desk):
    app,_,flows,_,_=desk;peer=setup_groups(desk)['SYN-A']['peers'][0]
    install_terms(app)
    flows.analyze=parser(lambda snapshot,msg:[cash_observation(peer,msg,'2026-10-17')])
    raw(app,peer['thread_id'],'SYN-A 净价34.19出500万')
    app.state.negotiation_engine.run_once()
    result=current(app,peer)
    assert result.price.kind=='NET_PRICE' and result.price.value=='34.19'
    assert result.price.settlement_date is None and result.price.computed_yield is None
def test_rejected_new_price_marks_previous_quote_uncertain_in_group_cover(desk):
    app,client,flows,_,_=desk;group=setup_groups(desk)['SYN-A'];peer=group['peers'][0]
    flows.analyze=parser(lambda snapshot,msg:[observation(peer,msg,price='3.15',finality='FINAL',segment=msg['text'])])
    raw(app,peer['thread_id'],'SYN-A 3.15出500万，已让至最终价')
    app.state.negotiation_engine.run_once()
    assert app.state.repository.group(OWNER,group['group_id'])['summary']['final_confirmed_volume_wan']=='500'
    flows.analyze=parser(lambda snapshot,msg:[observation(peer,msg,price='9.99',finality='FINAL',segment=msg['text'])])
    message=raw(app,peer['thread_id'],'SYN-A 3.11出500万，价格变了',seq=2)
    app.state.negotiation_engine.run_once()
    assert current(app,peer).price.value=='3.15','Rejected parse must not overwrite the old durable fact'
    assert app.state.repository.group(OWNER,group['group_id'])['summary']['final_confirmed_volume_wan']=='0'
    raw_view=client.get('/api/negotiation/messages/'+peer['thread_id']).json()['data']['messages']
    assert next(row for row in raw_view if row['message_id']==message)['analysis_state']=='NEEDS_REVIEW'
def test_updated_context_can_confirm_final_after_an_earlier_indicative_quote(desk):
    app,_,flows,_,_=desk;group=setup_groups(desk)['SYN-A'];peer=group['peers'][0]
    flows.analyze=parser(lambda snapshot,msg:[observation(peer,msg,price='3.15',assertion='INDICATIVE')])
    earlier=raw(app,peer['thread_id'],'SYN-A 3.15出500万，先报个意向价')
    app.state.negotiation_engine.run_once()
    def updated(snapshot,msg):
        obs=observation(peer,msg,price='3.15',assertion='QUOTE',finality='FINAL',segment=msg['text'])
        obs['evidence']['finality']=[earlier,msg['message_id']]
        return [obs]
    flows.analyze=parser(updated)
    raw(app,peer['thread_id'],'SYN-A 现在不是意向了，3.15出500万就是最终价格，不能再让',seq=2)
    app.state.negotiation_engine.run_once()
    quote=current(app,peer)
    assert quote.finality=='FINAL' and quote.assertion=='QUOTE'
    assert app.state.repository.group(OWNER,group['group_id'])['summary']['final_confirmed_volume_wan']=='500'
