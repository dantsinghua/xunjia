from copy import deepcopy
from decimal import Decimal
import importlib
import pytest


@pytest.fixture
def api():
    spec = importlib.util.find_spec('server.bond_tools')
    assert spec is not None, '四个只读债券工具尚未实现'
    return importlib.import_module('server.bond_tools').BondTools


def fact(value):
    return dict(value=value, quality='verified', source_ref='synthetic', as_of='2026-10-13', missing_reason=None)


def records():
    return [dict(bond_id=str(i), code=f'00000{i}.IB', name='示例券', market='IB', aliases=[],
                 master={'issuer_id': fact(issuer), 'maturity_date': fact('2027-10-13'),
                         'bond_type': fact('fixed'), 'balance': fact(None)},
                 valuation={'yield_pct': fact('2.10'), 'valuation_date': fact('2026-10-12')}, quotes=[])
            for i, issuer in [(1, 'i1'), (2, 'i1'), (3, None), (4, 'i2')]]


def provider(bonds=None, terms=None):
    from server.reference_data import ReferenceSnapshot
    class Synthetic:
        def snapshot(self):
            return ReferenceSnapshot(snapshot_id='fixture-v1', as_of='2026-10-13',
                sources=[dict(id='synthetic', origin='fixture', transport='local', dataset_version='1', as_of='2026-10-13')],
                bonds=deepcopy(records() if bonds is None else bonds), issuers=[], terms=terms or {})
    return Synthetic()


def invoke(api, name, **kwargs):
    return api().invoke(name, dict(schema_version='1', **kwargs))


def test_unconfigured_is_not_empty_success(api):
    r = invoke(api, 'bond_info', mode='search', query='000001')
    assert r['status'] == 'unavailable'
    assert r['warnings'][0]['code'] == 'NOT_CONFIGURED'
    assert r['usage'] == 'analysis_only'


@pytest.mark.parametrize('kwargs', [dict(mode='get', bond_id='1', query='x'), dict(mode='search', query='x', limit=True),
    dict(mode='search', query='x', sql='SELECT *'), dict(mode='search', query='x', include=['instructions'])])
def test_reject_invalid_schema(api, kwargs):
    assert invoke(api, 'bond_info', **kwargs)['status'] == 'invalid_input'


def test_search_ambiguity_is_not_hidden_by_limit(api):
    r = api(provider()).invoke('bond_info', dict(schema_version='1', mode='search', query='示例券', limit=1))
    assert r['status'] == 'ambiguous'
    assert r['data']['total'] == 4
    assert r['data']['resolved_bond_id'] is None
    assert r['data']['has_more'] is True
    page = api(provider()).invoke('bond_info', dict(schema_version='1', mode='search', query='示例券', limit=1, cursor=r['data']['cursor']))
    assert page['data']['candidates'][0]['bond_id'] == '2'


def test_exact_code_preserves_leading_zero_and_missing_master(api):
    r = api(provider()).invoke('bond_info', dict(schema_version='1', mode='search', query='000001.IB'))
    assert r['data']['resolved_bond_id'] == '1'
    r = api(provider()).invoke('bond_info', dict(schema_version='1', mode='get', bond_id='1'))
    assert r['status'] == 'partial'
    assert r['data']['identity']['code'] == '000001.IB'
    assert r['data']['master']['balance']['value'] is None
    assert r['data']['valuation']['valuation_date']['value'] == '2026-10-12'


def test_internal_data_never_exposed(api):
    rows = records()
    rows[0].update(instructions=[{'secret': 1}], ib='2.5', raw_json={'password':'secret'})
    rows[0]['master']['internal_credit'] = fact('secret')
    r = api(provider(rows)).invoke('bond_info', dict(schema_version='1', mode='get', bond_id='1'))
    assert 'secret' not in str(r)
    assert 'raw_json' not in str(r)


def test_similar_does_not_match_unknown_issuer_or_relax(api):
    r = api(provider()).invoke('similar_bonds', dict(schema_version='1', anchor_bond_id='1',
        tenor_basis='maturity', remaining_days={'min':350,'max':380}, issuer_scope='same', compare_fields=['valuation_yield_pct']))
    assert [c['bond_id'] for c in r['data']['candidates']] == ['2']
    assert r['data']['unknown_counts']['issuer_id'] == 1
    assert r['data']['excluded_counts']['issuer_id'] == 1
    assert r['data']['criteria_applied']['issuer_scope'] == 'same'


def calc(api, operation, **inputs):
    return invoke(api, 'bond_calc', operation=operation, inputs=inputs)


@pytest.mark.parametrize('side,peer,boundary,value,gap', [('SELL','bid','2.10','2.40','30'),('BUY','ofr','-0.05','-0.10','5'),('SELL','bid','2.10','2.100001','0.0001')])
def test_signed_gap(api, side, peer, boundary, value, gap):
    r = calc(api,'yield_gap',our_side=side,peer_side=peer,limit_yield_pct=boundary,peer_yield_pct=value,same_basis=True)
    assert r['status'] == 'ok'
    assert Decimal(r['data']['result']['raw_gap_bp']) == Decimal(gap)


def test_gap_direction_mismatch_is_incomparable(api):
    r = calc(api,'yield_gap',our_side='SELL',peer_side='ofr',limit_yield_pct='2.1',peer_yield_pct='2.4',same_basis=True)
    assert r['data']['result']['raw_gap_bp'] is None
    assert any(w['code'] == 'INCOMPARABLE' for w in r['warnings'])


def base_terms():
    return dict(hypothetical=True, bond_type='zero_coupon', settlement_date='2025-01-01',
        cashflows=[{'payment_date':'2027-01-01','amount_per_100':'100'}], convention='ACT/365F-compounded-v1',
        compounding_frequency=1,yield_basis='maturity',settlement_cashflow='exclude',
        accrued_interest_per_100='0',accrued_interest_settlement_date='2025-01-01')


def test_explicit_zero_coupon_golden_round_trip(api):
    r = calc(api,'price_from_yield',yield_pct='5',**base_terms())
    assert r['status'] == 'ok'
    assert abs(Decimal(r['data']['result']['dirty_price_per_100'])-Decimal('90.7029478458')) < Decimal('0.00000001')
    back = calc(api,'yield_from_price',price_type='dirty',price_per_100=r['data']['result']['dirty_price_per_100'],**base_terms())
    assert abs(Decimal(back['data']['result']['yield_pct']) - Decimal('5')) < Decimal('0.000001')


def test_negative_yield_and_settlement_date_payment_rule(api):
    args=base_terms()
    args['cashflows'].insert(0, {'payment_date':'2025-01-01','amount_per_100':'2'})
    a=calc(api,'price_from_yield',yield_pct='-1',**args)
    args['settlement_cashflow']='include'
    b=calc(api,'price_from_yield',yield_pct='-1',**args)
    assert Decimal(b['data']['result']['dirty_price_per_100']) - Decimal(a['data']['result']['dirty_price_per_100']) == 2


def test_missing_cashflows_never_invents_schedule(api):
    r=calc(api,'price_from_yield',hypothetical=True,yield_pct='3',settlement_date='2026-10-13')
    assert r['status'] == 'partial'
    assert 'cashflows' in r['data']['missing_inputs']


def test_clean_dirty_requires_same_settlement_accrual(api):
    r=calc(api,'clean_dirty',hypothetical=True,clean_price_per_100='99.2',accrued_interest_per_100='1.3',
        settlement_date='2026-10-13',accrued_interest_settlement_date='2026-10-13')
    assert r['data']['result']['dirty_price_per_100'] == '100.5'
    r=calc(api,'clean_dirty',hypothetical=True,clean_price_per_100='99.2',settlement_date='2026-10-13')
    assert 'accrued_interest_per_100' in r['data']['missing_inputs']


@pytest.mark.parametrize('bad', ['NaN','Infinity','0',0,True])
def test_invalid_prices_rejected(api,bad):
    r=calc(api,'yield_from_price',price_type='dirty',price_per_100=bad,**base_terms())
    assert r['status'] == 'invalid_input'


def test_unsupported_terms_do_not_use_fixed_formula(api):
    terms=base_terms(); terms['bond_type']='callable'
    assert calc(api,'price_from_yield',yield_pct='2',**terms)['status']=='unsupported'


def test_real_terms_cannot_be_completed_by_untrusted_cashflows(api):
    terms={'1':dict(terms_snapshot_id='t1',quality='verified',source_ref='synthetic',bond_type='zero_coupon')}
    args=base_terms();args.pop('hypothetical');args.update(bond_id='1',terms_snapshot_id='t1',yield_pct='5')
    r=api(provider(terms=terms)).invoke('bond_calc',dict(schema_version='1',operation='price_from_yield',inputs=args))
    assert r['status'] == 'partial'
    assert 'cashflows' in r['data']['missing_inputs']
    assert r['data']['result'] is None


def test_quotes_keep_each_side_time_and_never_compare_stale_side(api):
    rows=records()
    base=dict(quote_type='yield',raw_value='2.4',yield_pct='2.4',yield_basis='maturity',quality='verified',
        settlement_date='2026-10-14',source_ref='synthetic',status='active',freshness='fresh',
        source_updated_at='2026-10-13T09:30:00+08:00',timestamp_scope='side')
    rows[0]['quotes']=[dict(base,side='bid'),dict(base,side='ofr',freshness='stale',source_updated_at='2026-10-12T09:30:00+08:00')]
    r=api(provider(rows)).invoke('bond_info',dict(schema_version='1',mode='get',bond_id='1',include=['quotes']))
    assert r['data']['best']['bid']['yield_pct']=='2.4'
    assert r['data']['best']['ofr'] is None
    assert r['data']['quotes'][1]['source_updated_at']=='2026-10-12T09:30:00+08:00'


def test_provider_cannot_inject_nested_private_quote_or_metadata(api):
    rows=records()
    rows[0]['quotes']=[dict(side='bid',raw_value={'password':'secret'},broker_name={'password':'secret'})]
    rows[0]['master']['balance']=fact({'password':'secret','value':{'password':'secret'}})
    r=api(provider(rows)).invoke('bond_info',dict(schema_version='1',mode='get',bond_id='1'))
    assert 'secret' not in str(r)


def test_real_verified_terms_work_but_override_is_rejected(api):
    terms=base_terms();terms.pop('hypothetical');terms.update(terms_snapshot_id='t1',quality='verified',source_ref='synthetic')
    p=provider(terms={'1':terms})
    args=dict(bond_id='1',terms_snapshot_id='t1',yield_pct='5')
    r=api(p).invoke('bond_calc',dict(schema_version='1',operation='price_from_yield',inputs=args))
    assert r['status']=='ok'
    assert Decimal(r['data']['result']['dirty_price_per_100'])>90
    args['cashflows']=[dict(payment_date='2027-01-01',amount_per_100='200')]
    assert api(p).invoke('bond_calc',dict(schema_version='1',operation='price_from_yield',inputs=args))['status']=='invalid_input'


def test_issuer_search_and_get_retain_source_facts(api):
    from server.reference_data import ReferenceSnapshot
    class Source:
        def snapshot(self):
            return ReferenceSnapshot(snapshot_id='s1',as_of='2026-10-13',
                sources=[dict(id='synthetic')],issuers=[dict(issuer_id='001',full_name=fact('合成发行人'),short_name=fact('合成'))])
    service=api(Source())
    found=service.invoke('issuer_info',dict(schema_version='1',mode='search',query='合成发行人'))
    assert found['data']['resolved_issuer_id']=='001'
    assert found['data']['candidates'][0]['full_name']=='合成发行人'
    got=service.invoke('issuer_info',dict(schema_version='1',mode='get',issuer_id='001'))
    assert got['data']['full_name']['source_ref']=='synthetic'
    assert got['data']['uscc']['value'] is None


def test_invalid_cursor_is_input_error(api):
    r=api(provider()).invoke('bond_info',dict(schema_version='1',mode='search',query='示例券',cursor='!'))
    assert r['status']=='invalid_input'


def test_current_yield_name_cannot_claim_ytm(api):
    r=calc(api,'current_yield',hypothetical=True,annual_coupon_per_100='3',clean_price_per_100='100',coupon_basis='fixed_annual')
    assert r['data']['result']=={'current_yield_pct':'3.00','name':'当期收益率'}


def test_leap_year_uses_actual_366_days_not_one_year(api):
    args=base_terms(); args.update(settlement_date='2024-01-01',accrued_interest_settlement_date='2024-01-01',
        cashflows=[dict(payment_date='2025-01-01',amount_per_100='100')])
    r=calc(api,'price_from_yield',yield_pct='5',**args)
    # Independently evaluated 100 / 1.05 ** (366/365).
    assert abs(Decimal(r['data']['result']['dirty_price_per_100'])-Decimal('95.2253654525'))<Decimal('0.00000001')


def test_fact_nested_metadata_and_numeric_type_are_not_public_facts(api):
    rows=records()
    rows[0]['master']['balance']=dict(value=123,quality='verified',source_ref='synthetic',as_of={'password':'secret'},missing_reason={'password':'secret'})
    r=api(provider(rows)).invoke('bond_info',dict(schema_version='1',mode='get',bond_id='1'))
    assert 'secret' not in str(r)
    assert r['data']['master']['balance']['value'] is None


def test_similar_balance_requires_same_unit_and_effective_date(api):
    rows=records()
    for row,unit in zip(rows[:2],['CNY','CNY_10000']):
        row['master'].update(balance=fact('100'),balance_unit=fact(unit),balance_date=fact('2026-10-13'))
    r=api(provider(rows)).invoke('similar_bonds',dict(schema_version='1',anchor_bond_id='1',tenor_basis='maturity',
        remaining_days={'min':350,'max':380},issuer_scope='same',compare_fields=['balance']))
    assert r['data']['candidates'][0]['comparisons']['balance']['comparable'] is False


def test_comparable_fresh_quotes_compute_signed_bp_difference(api):
    rows=records()
    for row,y in zip(rows[:2],['2.40','2.45']):
        row['quotes']=[dict(side='bid',quote_type='yield',raw_value=y,yield_pct=y,yield_basis='maturity',quality='verified',
            settlement_date='2026-10-14',source_ref='synthetic',status='active',freshness='fresh',
            source_updated_at='2026-10-13T09:30:00+08:00',timestamp_scope='side')]
    r=api(provider(rows)).invoke('similar_bonds',dict(schema_version='1',anchor_bond_id='1',tenor_basis='maturity',
        remaining_days={'min':350,'max':380},issuer_scope='same',compare_fields=['bid_yield_pct']))
    result=r['data']['candidates'][0]['comparisons']['bid_yield_pct']
    assert result['comparable'] is True
    assert Decimal(result['difference_bp']) == 5


@pytest.mark.parametrize('extra', [dict(quote_type='invented'),dict(yield_basis=['maturity'],peer_yield_basis=['maturity']),
    dict(settlement_date='2026-02-30',peer_settlement_date='2026-02-30')])
def test_gap_rejects_malformed_comparability_fields(api,extra):
    r=calc(api,'yield_gap',our_side='SELL',peer_side='bid',limit_yield_pct='2.10',peer_yield_pct='2.40',same_basis=True,**extra)
    assert r['status']=='invalid_input'
