"""Explicit price requests cannot be committed as executable quotations.

These gates exercise generated Code and the real normalization/fact reducer.
They open no app, database, external model, or transport.
"""
from copy import deepcopy
from functools import lru_cache
import json
from types import SimpleNamespace

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.engine import Engine
from server.negotiation.facts import Quote, apply_patch, group_summary
from server.negotiation.slots import collect_candidates


DAY = '2026-10-17'
QUESTIONS = (
    'SYN-A 能3.10出500万吗？',
    'SYN-A 可以3.10出500万吗？',
    'SYN-A 能否3.10出500万？',
)


def context(text):
    message = {'message_id': 'current', 'direction': 'in', 'text': text,
               'trade_day': DAY, 'ingest_revision': 1, 'historical': False}
    return {'schema_version': '2.2', 'stage': 'analysis', 'from_revision': 0,
            'through_revision': 1, 'messages': [message],
            'members': [{'member_id': 'a', 'bond_code': 'SYN-A', 'context': [],
                         'quotes': {'bid': Quote('bid').dump(), 'ofr': Quote('ofr').dump()}}]}


@lru_cache(maxsize=2)
def code(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    namespace = {}
    exec(next(node['data']['code'] for node in graph['nodes'] if node['id'] == 'contract'), namespace)
    return namespace['main']


def decoded(snapshot, intent, version):
    candidates = collect_candidates(snapshot)
    prices = [row['id'] for row in candidates if row['roles'] == ['YIELD'] and row['value'] == '3.10']
    assert len(prices) == 1, 'The explicit 3.10 price must survive candidate extraction: ' + repr(candidates)
    price = prices[0]
    volume = next(row['id'] for row in candidates if row['roles'] == ['QUANTITY'] and row['value'] == '500万')
    wire = {'schema_version': '2.2', 'scopes': {'current': ['a']}, 'observations': [{
        'member_id': 'a', 'side': 'ofr', 'source_ids': ['current'],
        'slots': {'price': price, 'volume_wan': volume},
        'meaning': {'intent': intent, 'validity': 'ACTIVE', 'negotiation_space': 'UNKNOWN',
                    'reason': snapshot['messages'][0]['text']}}]}
    return json.loads(code(version)(json.dumps(wire), json.dumps(snapshot))['result'])


def normalize(snapshot, result):
    service = SimpleNamespace(store=None, commands_core=None, reference_provider=None, calendar=None)
    return Engine(service, None, None)._normalize(snapshot, result)[1]


def accepted_quote(observations):
    assert len(observations) == 1 and 'validation_error' not in observations[0], observations
    row = observations[0]
    return apply_patch(Quote('ofr'), row['changes'], evidence=row['evidence'])


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('text', QUESTIONS)
def test_modal_before_price_question_rejects_a_quote_claim_without_rewriting_intent(text, version):
    snapshot = context(text)
    result = decoded(snapshot, 'QUOTE', version)
    before = deepcopy(result['observations'])
    observations = normalize(snapshot, result)
    assert len(observations) == 1
    assert observations[0].get('validation_error'), 'Explicit asking price was accepted as a current executable quote'
    assert observations[0]['member_id'] == 'a' and observations[0]['side'] == 'ofr'
    assert result['observations'] == before, 'Reject the contradiction; do not silently rewrite model QUOTE to QUESTION'


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('text', QUESTIONS)
def test_legal_question_keeps_price_quantity_and_evidence_without_confirming_executable_volume(text, version):
    snapshot = context(text)
    before = deepcopy(snapshot)
    observations = normalize(snapshot, decoded(snapshot, 'QUESTION', version))
    quote = accepted_quote(observations)
    assert quote.assertion == 'QUESTION' and quote.finality == 'UNKNOWN'
    assert quote.price.value == '3.10' and quote.volume_wan == '500'
    assert quote.confirmed_day is None
    assert quote.evidence['price'] == ['current'] and quote.evidence['volume_wan'] == ['current']
    summary = group_summary(our_side='BUY', target_volume='1000', trade_day=DAY,
                            boundary={'kind': 'YIELD', 'value': '3.00'}, quotes=[('a', quote)])
    assert summary['reported_volume_wan'] == '0'
    assert summary['price_eligible_volume_wan'] == summary['final_confirmed_volume_wan'] == '0'
    assert snapshot == before


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('text', [
    'SYN-A 3.10出500万',
    'SYN-A 3.10出500万。你还能收吗？',
    'SYN-A3.10出500万。你还能收吗？',
    'SYN-A 我可以3.10出500万。',
])
def test_real_declaration_is_not_rejected_for_a_separate_question_or_modal_word(text, version):
    snapshot = context(text)
    quote = accepted_quote(normalize(snapshot, decoded(snapshot, 'QUOTE', version)))
    assert quote.assertion == 'QUOTE' and quote.price.value == '3.10' and quote.volume_wan == '500'
    assert quote.confirmed_day == DAY
    summary = group_summary(our_side='BUY', target_volume='1000', trade_day=DAY,
                            boundary={'kind': 'YIELD', 'value': '3.00'}, quotes=[('a', quote)])
    assert summary['reported_volume_wan'] == summary['price_eligible_volume_wan'] == '500'


def test_cropped_segment_cannot_hide_the_explicit_question_in_the_actual_bond_clause():
    snapshot = context(QUESTIONS[0])
    result = decoded(snapshot, 'QUOTE', '0.15.3')
    result['observations'][0]['segments'] = [{'message_id': 'current', 'text': '3.10出500万'}]
    observations = normalize(snapshot, result)
    assert observations[0].get('validation_error'), 'A literal excerpt must not erase the actual asking-price context'
