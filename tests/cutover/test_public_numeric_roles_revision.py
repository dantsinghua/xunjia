"""Numeric roles must prevent false rejection without hiding real quote amounts."""
from copy import deepcopy
from decimal import Decimal

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.validation import economics


def context():
    return {'our_side': 'BUY', 'bond_code': 'SYN-A', 'trade_day': '2026-10-17',
            'unresolved_messages': [], 'reconfirm_day': None,
            'boundary': {'kind': 'YIELD', 'value': '3.12'},
            'our_quote': {'price': {'kind': 'YIELD', 'value': '3.20'}, 'native_id': 'synthetic-sent'},
            'plan': {'opening_yield_pct': '3.20', 'step_bp': '2', 'volume_wan': '20000'},
            'group_summary': {'reported_shortfall_wan': '19500', 'goal_ready': False},
            'quotes': {'ofr': {'price': {'kind': 'YIELD', 'value': '3.10'}, 'volume_wan': '500',
                'assertion': 'QUOTE', 'validity': 'ACTIVE', 'finality': 'UNKNOWN',
                'confirmed_day': '2026-10-17'}}}


def proposal(text):
    return {'kind': 'REPLY', 'text': text, 'our_price': {'kind': 'YIELD', 'value': '3.18'}}


def check_reply(text):
    snapshot, body = context(), proposal(text)
    before = deepcopy((snapshot, body))
    validated = Engine.__new__(Engine)._validate_reply(snapshot, body)
    assert (snapshot, body) == before
    assert validated['text'] == text, 'Validation must not rewrite dates or delete public words'
    assert validated['our_price']['value'] == '3.18'
    return validated


def test_f10_actual_chinese_rating_date_does_not_turn_2026_into_peer_volume():
    # Verbatim synthetic public reply from the retained qwen-max F10 artifact.
    text = ('Synthetic Issuer（简称SYN-ISSUER）是一家位于Synthetic Region的发行主体，'
            '其行业分类为SYN-INDUSTRY。该发行主体的信用评级为AAA级，评级机构为Synthetic，'
            '评级日期为2026年10月17日。\nSYN-A 3.18%收，请问结算方式是T+0还是T+1？')
    check_reply(text)
    prices, quantities = economics(text, bond_codes=('SYN-A',))
    assert [value for value, _, _ in prices] == ['3.18']
    assert quantities == []


@pytest.mark.parametrize('date', [
    '2026年10月17日', '2026年1月7日', '2024年2月29日',
    '2026-10-17', '2024-02-29', '2026/10/17', '2024/2/29',
])
def test_real_calendar_dates_are_not_price_or_quantity_and_keep_original_offsets(date):
    text = '🙂评级日期'+date+'；SYN-A 3.18%收500万。'
    prices, quantities = economics(text, bond_codes=('SYN-A',))
    assert [value for value, _, _ in prices] == ['3.18']
    assert [value for value, _, _ in quantities] == [Decimal('500')]
    assert [text[start:end] for _, start, end in prices] == ['3.18']
    assert [text[start:end] for _, start, end in quantities] == ['500万']
    check_reply(text)


@pytest.mark.parametrize('description', [
    '剩余期限335天', '剩余期限2.5年', '剩余期限18个月',
    '信用评级A-1，短期评级P-1', '结算方式T+0或T+1',
])
def test_explicit_tenor_rating_and_settlement_roles_do_not_consume_trade_slots(description):
    text = description+'；SYN-A 3.18%收500万。'
    prices, quantities = economics(text, bond_codes=('SYN-A',))
    assert [value for value, _, _ in prices] == ['3.18']
    assert [value for value, _, _ in quantities] == [Decimal('500')]
    check_reply(text)


def test_known_numeric_code_is_separate_from_same_message_quote_and_amount():
    text = '债券代码123456.IB；评级日期2026年10月17日；123456.IB 3.18%收500万。'
    prices, quantities = economics(text, bond_codes=('123456.IB',))
    assert [value for value, _, _ in prices] == ['3.18']
    assert [value for value, _, _ in quantities] == [Decimal('500')]
    assert text[prices[0][1]:prices[0][2]] == '3.18'
    assert text[quantities[0][1]:quantities[0][2]] == '500万'


@pytest.mark.parametrize('invalid_date', [
    '2026-02-29', '2026-02-30', '2026-13-17', '2026-00-17',
    '2026/02/30', '2026年2月30日',
])
def test_invalid_calendar_date_is_not_an_exemption_that_hides_extra_numbers(invalid_date):
    with pytest.raises(InvalidFact):
        check_reply('评级日期'+invalid_date+'；SYN-A 3.18%收500万。')


@pytest.mark.parametrize('attached', [
    '2026-10-17万', '2026-10-17亿', '2026-10-17500万',
    '2026年10月17日万', '2026/10/17万', '335天万',
])
def test_date_or_tenor_spelling_cannot_hide_an_attached_trading_unit(attached):
    with pytest.raises(InvalidFact):
        check_reply('资料日期或期限'+attached+'；SYN-A 3.18%收500万。')


@pytest.mark.parametrize('extra', ['额外800万', '余额200000万', '资料估值2.9872%', '公开资料标注3.12%收'])
def test_free_text_public_info_labels_do_not_authorize_extra_economic_values(extra):
    # Verified PUBLIC_INFO spans are a separate planned contract. A model's
    # free-text label alone cannot exempt an amount/price from today's guard.
    with pytest.raises(InvalidFact):
        check_reply('评级日期2026-10-17；'+extra+'；SYN-A 3.18%收500万。')


@pytest.mark.parametrize('text,price,amount', [
    ('SYN-A 3.18%收500万', '3.18', Decimal('500')),
    ('SYN-A 20出2000万', '20', Decimal('2000')),
    ('SYN-A -0.05出0.5亿', '-0.05', Decimal('5000')),
])
def test_numeric_role_filter_preserves_legal_price_and_volume_tokens(text, price, amount):
    prices, quantities = economics(text, bond_codes=('SYN-A',))
    assert [value for value, _, _ in prices] == [price]
    assert [value for value, _, _ in quantities] == [amount]


@pytest.mark.parametrize('malformed_date_amount', ['2026/10/17.5万', '2026年10月17.5万'])
def test_fractional_day_prefix_cannot_mask_the_leading_digits_of_a_trading_amount(malformed_date_amount):
    with pytest.raises(InvalidFact):
        check_reply('评级日期'+malformed_date_amount+'；SYN-A 3.18%收500万。')


def test_hyphenated_credit_rating_does_not_become_a_price_next_to_a_valid_bare_amount():
    text = '信用评级A-1；SYN-A 3.18%收500'
    prices, quantities = economics(text, bond_codes=('SYN-A',))
    assert [value for value, _, _ in prices] == ['3.18']
    assert [value for value, _, _ in quantities] == [Decimal('500')]
    assert text[quantities[0][1]:quantities[0][2]] == '500'
    check_reply(text)
