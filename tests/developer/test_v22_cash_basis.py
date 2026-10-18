"""An explicit cash-price basis belongs to the actual source, not an excerpt."""
from copy import deepcopy

import pytest

from server.negotiation.prices import InvalidFact
from server.negotiation.validation import validate_observation


def fixture(text, kind, *, segment=None, side='ofr', price='99.32', history=None):
    message = {'message_id': 'current', 'text': text, 'direction': 'in', 'ingest_revision': 2}
    member = {'member_id': 'member-a', 'context': history or [], 'quotes': {}}
    snapshot = {'messages': [message], 'members': [member]}
    observation = {'member_id': 'member-a', 'side': side,
                   'changes': {'price': {'kind': kind, 'value': price}},
                   'evidence': {'price': ['current']},
                   'segments': [{'message_id': 'current', 'text': segment or text}]}
    return snapshot, observation, {'syn-a': {'member-a'}}


@pytest.mark.parametrize('label,wrong', [('净价', 'DIRTY_PRICE'), ('全价', 'NET_PRICE')])
@pytest.mark.parametrize('cropped', [False, True])
def test_explicit_cash_basis_rejects_other_cash_basis_even_when_marker_is_cropped(label, wrong, cropped):
    args = fixture('SYN-A '+label+'99.32出500万', wrong,
                   segment='99.32出500万' if cropped else None)
    with pytest.raises(InvalidFact):
        validate_observation(*args)


@pytest.mark.parametrize('label', ['净价', '全价'])
def test_cash_marker_cannot_be_hidden_to_accept_a_yield(label):
    args = fixture('SYN-A '+label+'99.32出500万', 'YIELD', segment='99.32出500万')
    with pytest.raises(InvalidFact):
        validate_observation(*args)


@pytest.mark.parametrize('label,kind', [('净价', 'NET_PRICE'), ('全价', 'DIRTY_PRICE')])
@pytest.mark.parametrize('cropped', [False, True])
def test_correct_cash_basis_stays_valid_and_does_not_rewrite_facts(label, kind, cropped):
    args = fixture('SYN-A '+label+'99.32出500万', kind,
                   segment='99.32出500万' if cropped else None)
    before = deepcopy(args)
    result = validate_observation(*args)
    assert result['changes'] == before[1]['changes']
    assert args == before


@pytest.mark.parametrize('label,wrong', [('净价', 'DIRTY_PRICE'), ('全价', 'NET_PRICE')])
def test_direct_label_after_number_also_rejects_wrong_basis(label, wrong):
    with pytest.raises(InvalidFact):
        validate_observation(*fixture('SYN-A 99.32'+label+'出500万', wrong))


@pytest.mark.parametrize('kind', ['NET_PRICE', 'DIRTY_PRICE'])
def test_without_an_explicit_label_contextual_cash_basis_is_not_invented_or_rejected(kind):
    validate_observation(*fixture('SYN-A 99.32出500万', kind))


@pytest.mark.parametrize('member,kind,segment', [
    ('member-a', 'NET_PRICE', 'SYN-A 净价99.32出500万'),
    ('member-b', 'DIRTY_PRICE', 'SYN-B 全价99.32出800万'),
])
def test_another_bonds_basis_does_not_contaminate_this_bond(member, kind, segment):
    args = fixture('SYN-A 净价99.32出500万；SYN-B 全价99.32出800万', kind, segment=segment)
    snapshot, observation, aliases = args
    snapshot['members'].append({'member_id': 'member-b', 'context': [], 'quotes': {}})
    aliases['syn-b'] = {'member-b'}
    observation['member_id'] = member
    validate_observation(*args)


@pytest.mark.parametrize('side,kind,price', [('ofr', 'NET_PRICE', '99.32'), ('bid', 'DIRTY_PRICE', '100.12')])
def test_same_bond_two_sides_keep_their_own_explicit_basis(side, kind, price):
    validate_observation(*fixture('SYN-A 净价99.32出500万；全价100.12收800万', kind, side=side, price=price))


@pytest.mark.parametrize('label,kind,old_label,old_kind', [
    ('全价', 'DIRTY_PRICE', '净价', 'NET_PRICE'),
    ('净价', 'NET_PRICE', '全价', 'DIRTY_PRICE'),
])
def test_latest_explicit_inbound_basis_wins_over_old_same_numeric_price(label, kind, old_label, old_kind):
    history = [{'message_id': 'old', 'text': 'SYN-A '+old_label+'99.32出500万',
                'direction': 'in', 'ingest_revision': 1}]
    args = fixture('SYN-A 现在改报'+label+'99.32出500万', old_kind, history=history)
    args[1]['evidence']['price'].append('old')
    with pytest.raises(InvalidFact):
        validate_observation(*args)
    args[1]['changes']['price']['kind'] = kind
    validate_observation(*args)


@pytest.mark.parametrize('label,kind', [('净价', 'NET_PRICE'), ('全价', 'DIRTY_PRICE')])
def test_referenced_unchanged_cash_price_keeps_real_history_basis(label, kind):
    history = [{'message_id': 'old', 'text': 'SYN-A '+label+'99.32出500万',
                'direction': 'in', 'ingest_revision': 1}]
    args = fixture('SYN-A 价格不变', kind, history=history)
    args[1]['evidence']['price'].append('old')
    validate_observation(*args)


@pytest.mark.parametrize('label,kind,wrong', [('净价', 'NET_PRICE', 'DIRTY_PRICE'), ('全价', 'DIRTY_PRICE', 'NET_PRICE')])
def test_explicit_bond_correction_preserves_the_original_cash_basis(label, kind, wrong):
    old = {'message_id': 'old', 'text': 'SYN-A '+label+'99.32出500万',
           'direction': 'in', 'ingest_revision': 1}
    snapshot, observation, aliases = fixture('上一条实际上是SYN-B，价格不变', kind, history=[old])
    snapshot['members'].append({'member_id': 'member-b', 'context': [], 'quotes': {}})
    aliases['syn-b'] = {'member-b'}
    snapshot['corrections'] = [{'message_id': 'old', 'member_ids': ['member-b'], 'evidence': ['current']}]
    observation['member_id'] = 'member-b'
    observation['evidence']['price'].append('old')
    observation['segments'].append({'message_id': 'old', 'text': '99.32出500万'})
    validate_observation(snapshot, observation, aliases)
    observation['changes']['price']['kind'] = wrong
    with pytest.raises(InvalidFact):
        validate_observation(snapshot, observation, aliases)


def test_ambiguous_repeated_correction_fragment_cannot_choose_a_cash_basis():
    old = {'message_id': 'old', 'text': 'SYN-A 净价99.32出500万；SYN-C 全价99.32出800万',
           'direction': 'in', 'ingest_revision': 1}
    snapshot, observation, aliases = fixture('SYN-B，价格不变', 'NET_PRICE', history=[old])
    snapshot['members'].extend([{'member_id': 'member-b', 'context': [], 'quotes': {}},
                                {'member_id': 'member-c', 'context': [], 'quotes': {}}])
    aliases.update({'syn-b': {'member-b'}, 'syn-c': {'member-c'}})
    snapshot['corrections'] = [{'message_id': 'old', 'member_ids': ['member-b', 'member-c'], 'evidence': ['current']}]
    observation['member_id'] = 'member-b'
    observation['evidence']['price'].append('old')
    observation['segments'].append({'message_id': 'old', 'text': '99.32'})
    with pytest.raises(InvalidFact, match='not unique'):
        validate_observation(snapshot, observation, aliases)
