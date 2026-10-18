"""Independent source-to-fact gates for Unicode quotation spellings.

Only pure generated Code, Engine normalization and the fact reducer run here.
No application, database, model, network, or account state is opened.
"""
from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
import json
from types import SimpleNamespace

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.engine import Engine
from server.negotiation.facts import Quote, apply_patch
from server.negotiation.slots import collect_candidates
from server.negotiation.validation import economics


def snapshot(token):
    message = {'message_id': 'in', 'direction': 'in', 'ingest_revision': 1,
               'historical': False, 'trade_day': '2026-10-16',
               'text': '🙂 SYN-A ' + token + '出500万'}
    return {'schema_version': '2.2', 'stage': 'analysis', 'from_revision': 0,
            'through_revision': 1, 'messages': [message],
            'members': [{'member_id': 'a', 'bond_code': 'SYN-A', 'context': [],
                         'quotes': {'bid': Quote('bid').dump(), 'ofr': Quote('ofr').dump()}}]}


def response(context):
    candidates = collect_candidates(context)
    price = next(row for row in candidates if row['roles'] == ['YIELD'])
    volume = next(row for row in candidates if row['roles'] == ['QUANTITY'])
    return {'schema_version': '2.2', 'scopes': {'in': ['a']}, 'observations': [{
        'member_id': 'a', 'source_ids': ['in'], 'side': 'ofr',
        'slots': {'price': price['id'], 'volume_wan': volume['id']},
        'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE',
                    'negotiation_space': 'UNKNOWN', 'reason': '本人明确报盘'}}]}


@lru_cache(maxsize=2)
def contract(version):
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    namespace = {}
    exec(next(node['data']['code'] for node in graph['nodes'] if node['id'] == 'contract'), namespace)
    return namespace['main']


def decode(context, version='0.15.3'):
    raw = response(context)
    return json.loads(contract(version)(json.dumps(raw), json.dumps(context))['result'])


def normalize(context, result):
    service = SimpleNamespace(store=None, commands_core=None, reference_provider=None, calendar=None)
    engine = Engine(service, None, None)
    return engine._normalize(context, result)[1]


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('token,expected', [
    ('3．10', '3.10'), ('３．１０', '3.10'),
    ('−0.05', '-0.05'), ('－0.05', '-0.05'), ('−０．０５', '-0.05'),
])
def test_unicode_candidate_generated_code_engine_and_fact_reducer_preserve_price(version, token, expected):
    context = snapshot(token)
    original = deepcopy(context)
    decoded = decode(context, version)
    assert decoded['observations'][0]['changes']['price']['value'] == expected
    segments = decoded['observations'][0]['segments']
    assert segments and all(segment['message_id'] == 'in'
                            and segment['text'] in context['messages'][0]['text']
                            and token in segment['text'] for segment in segments)
    observations = normalize(context, decoded)
    assert len(observations) == 1 and 'validation_error' not in observations[0], observations
    row = observations[0]
    quote = apply_patch(Quote('ofr'), row['changes'], evidence=row['evidence'])
    assert quote.price.value == expected
    assert quote.volume_wan == '500'
    assert quote.evidence['price'] == ['in']
    assert context == original, 'Normalization must not rewrite the source text or its offsets'


@pytest.mark.parametrize('token,expected', [('3．10', '3.10'), ('−0.05', '-0.05'), ('－0.05', '-0.05')])
def test_economic_evidence_preserves_unicode_sign_and_original_character_positions(token, expected):
    context = snapshot(token)
    text = context['messages'][0]['text']
    candidates = collect_candidates(context)
    for candidate in candidates:
        assert candidate['token'] == text[candidate['start']:candidate['end']]
    prices, quantities = economics(text, bond_codes=('SYN-A',))
    assert len(prices) == 1
    literal, start, end = prices[0]
    assert text[start:end] == token, 'Evidence offsets must include the complete original price and sign'
    assert Decimal(literal) == Decimal(expected)
    assert len(quantities) == 1 and quantities[0][0] == Decimal('500')


@pytest.mark.parametrize('token', ['-0.05', '−0.05', '－0.05'])
def test_negative_source_cannot_support_a_positive_canonical_value(token):
    context = snapshot(token)
    decoded = decode(context)
    decoded['observations'][0]['changes']['price']['value'] = '0.05'
    observations = normalize(context, decoded)
    assert observations[0].get('validation_error'), 'A lost minus sign must never become an accepted positive fact'


@pytest.mark.parametrize('token,replacement', [('3．10', '3.10'), ('−0.05', '-0.05')])
def test_normalized_text_cannot_masquerade_as_an_original_source_segment(token, replacement):
    context = snapshot(token)
    decoded = decode(context)
    decoded['observations'][0]['segments'][0]['text'] = context['messages'][0]['text'].replace(token, replacement)
    observations = normalize(context, decoded)
    assert observations[0].get('validation_error'), 'The numeric normalizer must not relax exact segment evidence'


def test_unicode_normalization_cannot_authorize_a_different_price():
    context = snapshot('3．10')
    decoded = decode(context)
    decoded['observations'][0]['changes']['price']['value'] = '3.11'
    assert normalize(context, decoded)[0].get('validation_error')
