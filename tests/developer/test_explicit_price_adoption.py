"""A stated adopted price cannot be replaced by an unrelated old peer quote."""
import pytest

from server.negotiation.prices import InvalidFact
from server.negotiation.validation import validate_observation


def validate(text, selected='3.10', side='ofr'):
    own_word, peer_word = ('收', '出') if side == 'ofr' else ('出', '收')
    rows = [
        {'message_id': 'old-peer', 'direction': 'in', 'ingest_revision': 1, 'text': 'SYN-A 3.10'+peer_word+'500万'},
        {'message_id': 'our-quote', 'direction': 'out', 'native_id': 'synthetic-native',
         'ingest_revision': 2, 'text': 'SYN-A 3.20%'+own_word},
    ]
    current = {'message_id': 'current', 'direction': 'in', 'ingest_revision': 3, 'text': text}
    snapshot = {'members': [{'member_id': 'a', 'context': rows}], 'messages': [current]}
    source = 'our-quote' if selected == '3.20' else 'old-peer'
    observation = {'member_id': 'a', 'side': side, 'changes': {'price': {'kind': 'YIELD', 'value': selected}},
                   'evidence': {'price': ['current', source]}}
    return validate_observation(snapshot, observation, {'syn-a': {'a'}})


@pytest.mark.parametrize('text,side', [
    ('SYN-A 按你刚才那个3.20收的价，我出500万，不能再改善了', 'ofr'),
    ('SYN-A 就按贵方之前3.20%的报价，本方收0.5亿', 'bid'),
])
def test_explicit_current_adoption_rejects_an_unrelated_old_price(text, side):
    with pytest.raises(InvalidFact, match='explicitly adopted'):
        validate(text, side=side)


@pytest.mark.parametrize('text,side', [
    ('SYN-A 按你刚才那个3.20收的价，我出500万，不能再改善了', 'ofr'),
    ('SYN-A 就按贵方之前3.20%的报价，本方收0.5亿', 'bid'),
])
def test_actual_loaded_adopted_price_remains_valid(text, side):
    assert validate(text, '3.20', side)['changes']['price']['value'] == '3.20'


@pytest.mark.parametrize('prefix', ['不是', '不要', '如果', '客户说'])
def test_negated_conditional_or_reported_adoption_does_not_override_a_real_current_quote(prefix):
    text = 'SYN-A '+prefix+'按你刚才3.20收的价，我出500万，目前3.10出'
    assert validate(text)['changes']['price']['value'] == '3.10'


def test_a_later_explicit_correction_is_not_overridden_by_an_earlier_adoption():
    assert validate('SYN-A 按你刚才3.20收的价，我出500万，不过现在改为3.10出')['changes']['price']['value'] == '3.10'


def test_a_separate_later_quote_without_a_correction_keyword_is_not_erased():
    assert validate('SYN-A 按你刚才3.20收的价，我出500万。SYN-A 我方3.10出500万')['changes']['price']['value'] == '3.10'
