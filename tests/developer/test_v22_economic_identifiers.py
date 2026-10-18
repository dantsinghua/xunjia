"""Message identifiers are evidence pointers, never economic quantities."""
from decimal import Decimal
import pytest
from server.negotiation.validation import economics


@pytest.mark.parametrize('identifier',[
    'abcdef1234567890123456789000000001',
    '12345678901234567890000000000000ab',
    '12345678-1234-1234-1234-123456789012',
])
def test_old_message_reference_does_not_become_price_or_volume(identifier):
    prices,quantities=economics('SYN-A 按消息 '+identifier+' 里你报的那个价，我出500万。',bond_codes=('SYN-A',))
    assert prices==[]
    assert [v for v,_a,_b in quantities]==[Decimal('500')]


def test_bond_identifier_matching_is_case_insensitive():
    prices,quantities=economics('SYN-1234567890.IB 3.10出500万',bond_codes=('syn-1234567890.ib',))
    assert [v for v,_a,_b in prices]==['3.10']
    assert [v for v,_a,_b in quantities]==[Decimal('500')]
