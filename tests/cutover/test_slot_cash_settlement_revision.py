"""Independent cash settlement selection gates through actual Code and Engine.

The calendar algorithm and public calculator are production implementations;
their source snapshots are explicit in-memory fixtures. No DB/network opens.
"""
from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
import json
from types import SimpleNamespace

import pytest

from dify.negotiation_v22.build import build
from server.bond_tools import BondTools
from server.negotiation.engine import Engine
from server.negotiation.facts import Quote, apply_patch
from server.negotiation.reference import HTTPReferenceProvider
from server.negotiation.reply_render import reply_requirements
from server.negotiation.slots import collect_candidates
from server.reference_data import ReferenceSnapshot


DAY = '2026-10-16'
NEXT_SETTLEMENT = '2026-10-20'


class Calendar(HTTPReferenceProvider):
    """Use the real settlement_date method with an explicit supplied calendar."""
    def __init__(self):
        self.reads = []

    def _read(self, key):
        self.reads.append(key)
        assert key == 'calendar_url'
        # Friday goes to Tuesday in this published synthetic holiday calendar.
        return {'settlement_dates': {DAY: {'T+1': NEXT_SETTLEMENT}}}


class Terms:
    def __init__(self, settlement):
        self.settlement = settlement

    def snapshot(self):
        return ReferenceSnapshot('synthetic-settlement-snapshot', DAY,
            sources=[{'id': 'synthetic-source'}], bonds=[{'bond_id': 'SYN-A', 'code': 'SYN-A'}],
            terms={'SYN-A': {'terms_snapshot_id': 'synthetic-cashflows', 'quality': 'verified',
                'source_ref': 'synthetic-source', 'bond_type': 'zero_coupon',
                'convention': 'ACT/365F-compounded-v1', 'compounding_frequency': 1,
                'yield_basis': 'maturity', 'settlement_cashflow': 'exclude',
                'cashflows': [{'payment_date': '2027-10-20', 'amount_per_100': '100'}],
                'accrued_interest_per_100': '0',
                'accrued_interest_settlement_date': self.settlement}})


class Tools(BondTools):
    def __init__(self, provider):
        super().__init__(provider)
        self.calls = []

    def invoke(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return super().invoke(name, arguments)


def fixture(label, term=None, *, extra_messages=(), extra_members=()):
    text = 'SYN-A ' + label + '97.00出500万' + (('，结算 ' + term) if term else '')
    current = {'message_id': 'current', 'direction': 'in', 'ingest_revision': 2,
               'historical': False, 'trade_day': DAY, 'text': text}
    member = {'member_id': 'a', 'bond_code': 'SYN-A', 'context': list(extra_messages),
              'quotes': {'bid': Quote('bid').dump(), 'ofr': Quote('ofr').dump()}}
    context = {'schema_version': '2.2', 'stage': 'analysis', 'from_revision': 1,
               'through_revision': 2, 'messages': [current], 'members': [member, *extra_members]}
    candidates = collect_candidates(context)
    price = next(row for row in candidates if row['message_id'] == 'current'
                 and set(row['roles']) & {'NET_PRICE', 'DIRTY_PRICE'})
    volume = next(row for row in candidates if row['message_id'] == 'current' and row['roles'] == ['QUANTITY'])
    slots = {'price': price['id'], 'volume_wan': volume['id']}
    if term:
        slots['settlement'] = next(row['id'] for row in candidates
            if row['message_id'] == 'current' and set(row['roles']) & {'DATE', 'SETTLEMENT'})
    result = {'schema_version': '2.2', 'scopes': {'current': ['a']}, 'observations': [{
        'member_id': 'a', 'source_ids': ['current'], 'side': 'ofr', 'slots': slots,
        'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE',
                    'negotiation_space': 'UNKNOWN', 'reason': '当前本人报价及结算'}}]}
    return context, result


@lru_cache(maxsize=2)
def contract(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    namespace = {}
    exec(next(row['data']['code'] for row in graph['nodes'] if row['id'] == 'contract'), namespace)
    return namespace['main']


def normalize(context, result, settlement=NEXT_SETTLEMENT, *, calendar=True, version='0.15.3'):
    decoded = json.loads(contract(version)(json.dumps(result), json.dumps(context))['result'])
    provider = Terms(settlement)
    tools = Tools(provider)
    actual_calendar = Calendar() if calendar else None
    service = SimpleNamespace(store=None, commands_core=None, reference_provider=provider, calendar=actual_calendar)
    observations = Engine(service, None, tools)._normalize(context, decoded)[1]
    return decoded, observations, tools, actual_calendar


def accepted(observations):
    assert len(observations) == 1 and 'validation_error' not in observations[0], observations
    row = observations[0]
    return apply_patch(Quote('ofr'), row['changes'], evidence=row['evidence'])


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('label,kind', [('净价', 'NET_PRICE'), ('全价', 'DIRTY_PRICE')])
@pytest.mark.parametrize('term,expected', [
    ('2026-10-20', NEXT_SETTLEMENT), ('2026年10月20日', NEXT_SETTLEMENT),
    ('T+0', DAY), ('T+1', NEXT_SETTLEMENT),
])
def test_selected_cash_settlement_survives_code_calendar_and_verified_calculator(version, label, kind, term, expected):
    context, result = fixture(label, term)
    original = deepcopy((context, result))
    decoded, observations, tools, calendar = normalize(context, result, expected, version=version)
    quote = accepted(observations)
    assert quote.price.kind == kind and Decimal(quote.price.value) == Decimal('97')
    assert quote.price.settlement_date == expected
    assert quote.price.computed_yield is not None and quote.price.calculation_ref.startswith('calc-')
    assert len(tools.calls) == 1
    name, request = tools.calls[0]
    assert name == 'bond_calc' and request['operation'] == 'yield_from_price'
    assert request['inputs']['settlement_date'] == expected
    assert request['inputs']['price_type'] == ('clean' if kind == 'NET_PRICE' else 'dirty')
    assert request['inputs']['terms_snapshot_id'] == 'synthetic-cashflows'
    audit = decoded['_verified_calculations']
    assert len(audit) == 1 and audit[0]['status'] == 'VERIFIED'
    assert audit[0]['response']['data']['hypothetical'] is False
    assert audit[0]['response']['data']['result']['solver_status'] == 'converged'
    if term == 'T+1':
        assert calendar.reads and expected == '2026-10-20', 'The source holiday calendar must win over weekday guesses'
    assert quote.evidence['price'] == ['current']
    assert (context, result) == original


@pytest.mark.parametrize('label', ['净价', '全价'])
def test_missing_cash_settlement_preserves_raw_price_and_requires_settlement_question(label):
    context, result = fixture(label)
    _, observations, tools, _ = normalize(context, result)
    quote = accepted(observations)
    assert quote.price.settlement_date is None and quote.price.computed_yield is None
    assert quote.price.calculation_ref is None and tools.calls == []
    requirements = reply_requirements({'our_side': 'BUY', 'trade_day': DAY, 'quotes': {'ofr': quote.dump()}})
    assert requirements['allowed_kinds'] == ['CLARIFY']
    assert requirements['question'] == 'ASK_SETTLEMENT'


@pytest.mark.parametrize('label,kind', [('净价', 'NET_PRICE'), ('全价', 'DIRTY_PRICE')])
def test_existing_canonical_iso_price_still_uses_the_same_verified_fixture(label, kind):
    context, _ = fixture(label, NEXT_SETTLEMENT)
    text = context['messages'][0]['text']
    result = {'schema_version': '2.2', 'scopes': {'current': ['a']}, 'observations': [{
        'member_id': 'a', 'side': 'ofr',
        'changes': {'price': {'kind': kind, 'notation': 'ABSOLUTE', 'value': '97.00',
                              'settlement_date': NEXT_SETTLEMENT}},
        'evidence': {'price': ['current']},
        'segments': [{'message_id': 'current', 'text': text}],
        'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                           'negotiation_space': 'UNKNOWN', 'reason': '明确原文结算'}}]}
    _, observations, tools, _ = normalize(context, result)
    quote = accepted(observations)
    assert quote.price.settlement_date == NEXT_SETTLEMENT
    assert quote.price.computed_yield == '3.09' and quote.price.calculation_ref.startswith('calc-')
    assert len(tools.calls) == 1


def test_explicit_candidate_and_role_can_select_the_same_cash_settlement():
    context, result = fixture('净价', NEXT_SETTLEMENT)
    slots = result['observations'][0]['slots']
    slots['settlement'] = {'candidate_id': slots['settlement'], 'role': 'DATE'}
    _, observations, tools, _ = normalize(context, result)
    assert accepted(observations).price.settlement_date == NEXT_SETTLEMENT
    assert len(tools.calls) == 1


def test_t_plus_one_without_calendar_keeps_cash_fact_unconverted_and_pending_confirmation():
    context, result = fixture('净价', 'T+1')
    _, observations, tools, _ = normalize(context, result, calendar=False)
    quote = accepted(observations)
    assert Decimal(quote.price.value) == Decimal('97')
    assert quote.price.settlement_date is None and quote.price.computed_yield is None
    assert tools.calls == []
    assert reply_requirements({'our_side': 'BUY', 'trade_day': DAY, 'quotes': {'ofr': quote.dump()}})['question'] == 'ASK_SETTLEMENT'


@pytest.mark.parametrize('source', ['outbound', 'another_member', 'old_inbound'])
def test_settlement_selection_cannot_borrow_unconfirmed_or_other_member_date(source):
    old = {'message_id': 'other', 'direction': 'out' if source == 'outbound' else 'in',
           'historical': False, 'ingest_revision': 1, 'trade_day': DAY,
           'text': ('SYN-B' if source == 'another_member' else 'SYN-A') + ' 结算2026-10-20'}
    extra_members = [{'member_id': 'b', 'bond_code': 'SYN-B', 'context': [old], 'quotes': {}}] if source == 'another_member' else []
    context, result = fixture('净价', extra_messages=[] if extra_members else [old], extra_members=extra_members)
    candidate = next(row for row in collect_candidates(context) if row['roles'] == ['DATE'])
    result['observations'][0]['slots']['settlement'] = candidate['id']
    _, observations, tools, _ = normalize(context, result)
    assert tools.calls == []
    for row in observations:
        if not row.get('validation_error'):
            price = row['changes'].get('price') or {}
            assert price.get('settlement_date') is None and price.get('computed_yield') is None


@pytest.mark.parametrize('mutation', ['settlement_only', 'yield_price', 'literal_date', 'unknown_id', 'wrong_role'])
def test_settlement_slot_cannot_create_or_retype_a_price(mutation):
    context, result = fixture('净价', '2026-10-20')
    observation = result['observations'][0]
    if mutation == 'settlement_only':
        observation['slots'].pop('price')
    elif mutation == 'yield_price':
        context['messages'][0]['text'] = 'SYN-A 3.10出500万，结算2026-10-20'
        candidates = collect_candidates(context)
        observation['slots']['price'] = next(row['id'] for row in candidates if row['roles'] == ['YIELD'])
        observation['slots']['volume_wan'] = next(row['id'] for row in candidates if row['roles'] == ['QUANTITY'])
        observation['slots']['settlement'] = next(row['id'] for row in candidates if row['roles'] == ['DATE'])
    elif mutation == 'literal_date':
        observation['slots']['settlement'] = '2026-10-20'
    elif mutation == 'unknown_id':
        observation['slots']['settlement'] = 'not-loaded'
    else:
        observation['slots']['settlement'] = {'candidate_id': observation['slots']['settlement'], 'role': 'QUANTITY'}
    _, observations, tools, _ = normalize(context, result)
    assert any(row.get('validation_error') for row in observations)
    assert tools.calls == []
