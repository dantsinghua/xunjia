"""A corrected bond cannot borrow another raw fragment's cash-price basis."""
from copy import deepcopy

import pytest

from server.negotiation.prices import InvalidFact
from server.negotiation.validation import validate_observation


def corrected_fragments():
    old = {'message_id': 'old', 'text': 'SYN-A 净价99.32出500万；SYN-C 全价99.32出800万',
           'direction': 'in', 'ingest_revision': 1}
    now = {'message_id': 'current', 'text': '上一条SYN-A实际上是SYN-B，价格不变；SYN-C没变',
           'direction': 'in', 'ingest_revision': 2}
    snapshot = {'messages': [now], 'members': [
        {'member_id': member, 'context': [old] if member == 'b' else [], 'quotes': {}}
        for member in ('a', 'b', 'c')],
        'corrections': [{'message_id': 'old', 'member_ids': ['b', 'c'], 'evidence': ['current']}]}
    observation = {'member_id': 'b', 'side': 'ofr',
        'changes': {'price': {'kind': 'NET_PRICE', 'value': '99.32'}},
        'evidence': {'price': ['current', 'old']},
        'segments': [{'message_id': 'current', 'text': 'SYN-B，价格不变'},
                     {'message_id': 'old', 'text': '99.32出500万'}]}
    return snapshot, observation, {'syn-a': {'a'}, 'syn-b': {'b'}, 'syn-c': {'c'}}


def test_correction_keeps_the_explicit_cash_basis_of_the_actually_cited_raw_fragment():
    args = corrected_fragments()
    before = deepcopy(args)
    result = validate_observation(*args)
    assert result['changes']['price'] == {'kind': 'NET_PRICE', 'value': '99.32'}
    assert args == before


@pytest.mark.parametrize('also_cite_conflicting_fragment', [False, True])
def test_corrected_fragment_cannot_borrow_same_numeric_dirty_basis_from_another_bond(also_cite_conflicting_fragment):
    args = corrected_fragments()
    args[1]['changes']['price']['kind'] = 'DIRTY_PRICE'
    if also_cite_conflicting_fragment:
        args[1]['segments'].append({'message_id': 'old', 'text': '99.32出800万'})
    with pytest.raises(InvalidFact):
        validate_observation(*args)
