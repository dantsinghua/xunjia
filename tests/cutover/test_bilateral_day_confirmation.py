"""Every observation in one A transaction must see the original day gate."""
from copy import deepcopy

import pytest

from dify.negotiation_v22.node_code import decode_stage_result
from server.negotiation.slots import collect_candidates
from test_reference_window_policy import OWNER
from test_slot_confirmation_race import TODAY, rolled, question, receipt, current_member


def decoded_sides(case, snapshot, specifications, *, explicit_pre_question=False):
    context = {**snapshot, 'schema_version': '2.2', 'stage': 'analysis'}
    candidates = collect_candidates(context)
    observations = []
    for side, ref, price, amount in specifications:
        def selected(value, role):
            return {'candidate_id': next(row['id'] for row in candidates
                if row['message_id'] == ref and row['value'] == value and role in row['roles']), 'role': role}
        observations.append({'member_id': case.member, 'source_ids': [ref], 'side': side,
            'slots': {'price': selected(price, 'YIELD'), 'volume_wan': selected(amount, 'QUANTITY')},
            'meaning': {'intent': 'QUOTE', 'validity': 'ACTIVE', 'negotiation_space': 'FINAL',
                        'reason': '本人明确此方向价量且不能再改善'}})
    result = decode_stage_result(case.envelope(context, observations), context, 'analysis')
    if explicit_pre_question:
        for observation, (_side, ref, _price, _amount) in zip(result['observations'], specifications):
            if observation['side'] == 'bid':
                observation['changes']['confirmed_day'] = TODAY
                observation['evidence']['confirmed_day'] = [ref]
    return result


@pytest.mark.parametrize('order', [('bid', 'ofr'), ('ofr', 'bid')])
def test_both_sides_of_the_same_new_reply_are_confirmed_regardless_of_observation_order(rolled, order):
    case = rolled
    command_id = question(case)
    case.now += .1
    incoming = case.inbound('SYN-A 3.05收400万，3.15出500万，双边均为本人最终报价，不能再改善了')
    values = {'bid': ('3.05', '400万'), 'ofr': ('3.15', '500万')}
    calls = []
    def model(snapshot):
        calls.append(deepcopy(snapshot))
        return decoded_sides(case, snapshot, [(side, incoming, *values[side]) for side in order])
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(calls) == 1 and result['facts_committed'] and len(result['accepted']) == 2
    assert not result['rejected'] and not result['repair']['attempted']
    for side, (price, amount) in values.items():
        quote = case.store.quote(OWNER, case.member, side)
        assert quote.price.value == price and quote.volume_wan == amount[:-1]
        assert quote.finality == 'FINAL' and quote.confirmed_day == TODAY, (order, side, quote.dump())
        assert quote.evidence['confirmed_day'] == [incoming]
    assert current_member(case)['reconfirm_day'] is None
    assert case.service.commands_core.get(OWNER, command_id)['state'] == 'SENT'
    assert case.store.group(OWNER, case.group)['summary']['final_confirmed_volume_wan'] == '500'


@pytest.mark.parametrize('order', [('ofr', 'bid'), ('bid', 'ofr')])
def test_valid_post_question_sibling_cannot_authorize_explicit_pre_question_confirmation(rolled, order):
    case = rolled
    command_id = question(case, sent=False)
    assert case.service.commands_core.claim(OWNER, command_id)
    case.now += .1
    before_question = case.inbound('SYN-A 3.05收400万，不能再改善了')
    case.now += .1
    assert receipt(case, command_id, 'SENT') == 'SENT'
    case.now += .1
    after_question = case.inbound('SYN-A 3.15出500万，不能再改善了')
    before_bid = case.store.quote(OWNER, case.member, 'bid').dump()
    values = {'bid': (before_question, '3.05', '400万'), 'ofr': (after_question, '3.15', '500万')}
    calls = []
    def model(snapshot):
        calls.append(deepcopy(snapshot))
        sent_at = next(row['reconfirm_sent_at'] for row in snapshot['members'] if row['member_id'] == case.member)
        messages = {row['message_id']: row for row in snapshot['messages']}
        assert messages[before_question]['received_at'] < sent_at <= messages[after_question]['received_at']
        return decoded_sides(case, snapshot, [(side, *values[side]) for side in order], explicit_pre_question=True)
    case.flows.analyze = model
    result = case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert len(calls) == 1 and result['facts_committed'] and not result['repair']['attempted']
    assert [(row['side'], row['index']) for row in result['accepted']] == [('ofr', order.index('ofr'))], result
    assert len(result['rejected']) == 1 and result['rejected'][0]['side'] == 'bid'
    assert result['rejected'][0]['index'] == order.index('bid')
    assert 'actually sent reconfirmation question and a new reply' in result['rejected'][0]['reason']
    assert case.store.quote(OWNER, case.member, 'bid').dump() == before_bid
    valid = case.store.quote(OWNER, case.member, 'ofr')
    assert valid.price.value == '3.15' and valid.volume_wan == '500'
    assert valid.confirmed_day == TODAY and valid.finality == 'FINAL'
    assert current_member(case)['reconfirm_day'] is None
    assert case.store.group(OWNER, case.group)['summary']['final_confirmed_volume_wan'] == '500'
