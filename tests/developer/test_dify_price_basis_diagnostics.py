"""Cash spelling and local error provenance from the recorded qwen-max failures."""
from copy import deepcopy

import pytest

from dify.negotiation_v22.node_code import decode_stage_result


def sample(basis):
    ctx = {'schema_version': '2.2', 'stage': 'analysis',
           'messages': [{'message_id': 'in', 'direction': 'in', 'text': 'SYN-A 净价34.19出500万'}],
           'members': [{'member_id': 'a', 'context': []}]}
    observation = {'member_id': 'a', 'side': 'ofr',
                   'fields': [{'field': 'price', 'value': {'basis': basis, 'value': '34.19'}, 'evidence': ['in']}],
                   'segments': [{'message_id': 'in', 'text': ctx['messages'][0]['text']}],
                   'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                                      'negotiation_space': 'UNKNOWN', 'reason': '本人报盘'}}
    result = {'schema_version': '2.2', 'scopes': {'in': ['a']}, 'unresolved': {}, 'observations': [observation]}
    return ctx, result


@pytest.mark.parametrize('basis', ['NET_PRICE', 'DIRTY_PRICE'])
def test_explicit_internal_cash_enum_is_a_lossless_spelling(basis):
    ctx, value = sample(basis)
    before = deepcopy(value)
    row = decode_stage_result(value, ctx, 'analysis')['observations'][0]
    assert row['changes']['price'] == {'kind': basis, 'value': '34.19', 'notation': 'ABSOLUTE'}
    assert row['evidence']['price'] == ['in']
    assert value == before


def test_price_error_keeps_member_side_and_original_field_path():
    ctx, value = sample('not-a-basis')
    row = decode_stage_result(value, ctx, 'analysis')['observations'][0]
    assert row['validation_error'] == 'Unknown explicit price basis'
    assert row['member_id'] == 'a' and row['side'] == 'ofr'
    assert row['validation_path'] == 'observations[0].fields[0].value'
