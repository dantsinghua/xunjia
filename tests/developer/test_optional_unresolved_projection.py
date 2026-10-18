"""Omitting an unused mapping does not relax the existing wire type gate."""
from copy import deepcopy

import pytest

from dify.negotiation_v22.node_code import decode_stage_result
from test_dify_slot_wire import sample


def test_unused_unresolved_mapping_can_be_omitted_without_changing_any_fact():
    context, response = sample()
    expected = decode_stage_result(response, context, 'analysis')
    response.pop('unresolved')
    actual = decode_stage_result(response, context, 'analysis')
    assert actual['scopes'] == expected['scopes']
    assert actual['observations'] == expected['observations']


@pytest.mark.parametrize('invalid', [[], None, '', 0, True, ['a']])
def test_explicit_wrong_unresolved_type_is_still_rejected_and_not_normalized(invalid):
    context, response = sample()
    response['unresolved'] = invalid
    before = deepcopy(response)
    with pytest.raises(ValueError, match='Invalid unresolved associations'):
        decode_stage_result(response, context, 'analysis')
    assert response == before
