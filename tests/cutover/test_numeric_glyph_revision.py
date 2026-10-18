"""Independent glyph checks preserve source meaning and public economic gates."""
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from server.negotiation.prices import InvalidFact
from server.negotiation.engine import Engine
from server.negotiation.slots import collect_candidates
from server.negotiation.text_roles import numeric_spelling
from server.negotiation.validation import validate_observation, validate_public_text
from tests.cutover.test_explicit_adoption_revision import adoption
from tests.cutover.test_public_numeric_roles_revision import context, proposal
from tests.cutover.test_slot_unicode_evidence_revision import snapshot, decode, normalize, contract


@pytest.mark.parametrize('token', ['+3.20', '＋3.20', '＋３．２０'])
def test_explicit_positive_sign_cannot_hide_current_adoption_and_restore_old_price(token):
    snapshot, observation = adoption('SYN-A 按你'+token+'收的价，我出500万')
    before = deepcopy((snapshot, observation))
    with pytest.raises(InvalidFact, match='explicitly adopted'):
        validate_observation(snapshot, observation, {'syn-a': {'a'}})
    assert (snapshot, observation) == before


@pytest.mark.parametrize('token', ['+3.20', '＋3.20', '＋３．２０'])
def test_explicit_positive_adoption_keeps_the_correct_selected_price(token):
    snapshot, observation = adoption('SYN-A 按你'+token+'收的价，我出500万', selected='3.20')
    before = deepcopy((snapshot, observation))
    result = validate_observation(snapshot, observation, {'syn-a': {'a'}})
    assert result['changes']['price']['value'] == '3.20'
    assert (snapshot, observation) == before


@pytest.mark.parametrize('spelling,offset', [('−５', '-5'), ('－５', '-5'), ('＋５', '5')])
@pytest.mark.parametrize('wrong_sign', [False, True])
def test_bp_evidence_sign_is_checked_without_rewriting_the_original_message(spelling, offset, wrong_sign):
    text = 'SYN-A 中债估值'+spelling+'BP出500万'
    snapshot = {'members': [{'member_id': 'a', 'context': []}], 'messages': [
        {'message_id': 'in', 'direction': 'in', 'ingest_revision': 1, 'text': text}]}
    observation = {'member_id': 'a', 'side': 'ofr',
        'changes': {'price': {'kind': 'YIELD', 'value': '3.15'}},
        'evidence': {'price': ['in']}, 'segments': [{'message_id': 'in', 'text': text}]}
    raw = {'kind': 'YIELD', 'notation': 'CHINABOND_BP',
           'value': str(-int(offset)) if wrong_sign else offset}
    before = deepcopy((snapshot, observation, raw))
    if wrong_sign:
        with pytest.raises(InvalidFact, match='not supported'):
            validate_observation(snapshot, observation, {'syn-a': {'a'}}, raw_price=raw)
    else:
        result = validate_observation(snapshot, observation, {'syn-a': {'a'}}, raw_price=raw)
        assert result['changes'] == observation['changes']
    assert (snapshot, observation, raw) == before


@pytest.mark.parametrize('extra', ['另报２．９８７２％', '另报−３．１８％'])
def test_unicode_spelling_does_not_hide_an_additional_public_price(extra):
    with pytest.raises(InvalidFact, match='All public price tokens'):
        validate_public_text(context(), proposal('SYN-A 3.18%收500万；'+extra))


def test_supported_numeric_glyph_translation_preserves_every_source_offset():
    original = '🙂 SYN-A −０．０５％出５００万，＋３．１８，－１'
    normalized = numeric_spelling(original)
    assert normalized == '🙂 SYN-A -0.05%出500万，+3.18，-1'
    assert len(normalized) == len(original)
    for index, glyph in enumerate(original):
        assert numeric_spelling(glyph) == normalized[index:index+1]


@pytest.mark.parametrize('token', ['+3.10', '＋３．１０'])
def test_explicit_positive_yield_survives_generated_code_and_real_engine(token):
    source = snapshot(token)
    before = deepcopy(source)
    observations = normalize(source, decode(source))
    assert len(observations) == 1 and 'validation_error' not in observations[0], observations
    assert Decimal(observations[0]['changes']['price']['value']) == Decimal('3.10')
    assert observations[0]['changes']['volume_wan'] == '500'
    assert source == before


@pytest.mark.parametrize('token,expected', [('估值−５BP', '3.05'), ('估值＋５BP', '3.15')])
def test_unicode_bp_candidate_keeps_sign_through_code_and_real_valuation(token, expected):
    source = snapshot(token)
    before = deepcopy(source)
    candidates = collect_candidates(source)
    bp = [row for row in candidates if row['roles'] == ['CHINABOND_BP']]
    assert len(bp) == 1, candidates
    assert source['messages'][0]['text'][bp[0]['start']:bp[0]['end']] == token
    volume = next(row for row in candidates if row['roles'] == ['QUANTITY'])
    raw = {'schema_version': '2.2', 'scopes': {'in': ['a']}, 'observations': [{
        'member_id': 'a', 'source_ids': ['in'], 'side': 'ofr',
        'slots': {'price': bp[0]['id'], 'volume_wan': volume['id']},
        'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE',
                    'negotiation_space': 'UNKNOWN', 'reason': '当前估值偏移报价'}}]}
    decoded = json.loads(contract('0.15.3')(json.dumps(raw), json.dumps(source))['result'])
    service = SimpleNamespace(store=None, commands_core=None, reference_provider=None, calendar=None,
        valuation=lambda code: (SimpleNamespace(yield_pct='3.10'), '2026-10-16'))
    observations = Engine(service, None, None)._normalize(source, decoded)[1]
    assert len(observations) == 1 and 'validation_error' not in observations[0], observations
    assert Decimal(observations[0]['changes']['price']['value']) == Decimal(expected)
    assert source == before


@pytest.mark.parametrize('token', ['３．１８', '＋３．１８'])
def test_public_unicode_equivalent_yield_retains_full_price_direction_and_quantity_checks(token):
    source, body = context(), proposal('SYN-A '+token+'％收５００万')
    before = deepcopy((source, body))
    result = Engine.__new__(Engine)._validate_reply(source, body)
    assert result['text'] == body['text'] and result['our_price']['value'] == '3.18'
    assert (source, body) == before


@pytest.mark.parametrize('token', ['+3.10', '＋３．１０'])
def test_positive_evidence_offsets_include_the_entire_original_sign(token):
    from server.negotiation.validation import economics
    source = '🙂 SYN-A '+token+'出500万'
    prices, _ = economics(source, bond_codes=('SYN-A',))
    assert len(prices) == 1
    literal, start, end = prices[0]
    assert source[start:end] == token
    assert Decimal(literal) == Decimal('3.10')
