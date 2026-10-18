"""A final eligible peer price survives a shared quantity shortfall."""
from copy import deepcopy

import pytest

from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from test_production_flow import OWNER, desk, draft, confirm, item, raw
from test_acceptance_contract import observation, parser, current


@pytest.mark.parametrize('side,peer_side,value,worse', [
    ('BUY', 'ofr', '3.20', '3.18'), ('SELL', 'bid', '3.00', '3.02')])
def test_production_rejects_worse_reply_after_final_fact_without_losing_that_fact(desk, side, peer_side, value, worse):
    app, client, flows, native, _ = desk
    request = item('SYN-A', ['peer-a'])
    request.update(our_side=side, opening_yield_pct=value, volume_wan='20000')
    confirm(client, draft(client, [request]))
    app.state.dispatcher.run_once()
    peer = app.state.repository.group_rows(OWNER)[0]['peers'][0]
    flows.analyze = parser(lambda snap, msg: [observation(peer, msg, price=value, volume='500',
        side=peer_side, finality='FINAL', segment=msg['text'], assertion='QUOTE')])
    raw(app, peer['thread_id'], 'SYN-A '+value+('出' if peer_side=='ofr' else '收')+'500万，这是最好条件，不能再改善了')
    saved = app.state.negotiation_engine.analyze_thread(OWNER, peer['thread_id'])
    assert saved['facts_committed'] and not saved['rejected']
    before = current(app, peer, peer_side).dump()
    summary = app.state.repository.group_rows(OWNER)[0]['summary']
    assert summary['final_shortfall_wan'] == '19500' and not summary['goal_ready']
    flows.decide = lambda context: {'schema_version': '2.2', 'action': 'SEND', 'proposal': {
        'kind': 'REPLY', 'text': 'SYN-A '+worse+'%'+('收' if side=='BUY' else '出'),
        'our_price': {'kind': 'YIELD', 'value': worse}}}
    outcome = app.state.negotiation_engine.decide_member(OWNER, peer['member_id'])
    assert outcome['state'] == 'REJECTED' and outcome['command_id'] is None
    assert current(app, peer, peer_side).dump() == before
    app.state.dispatcher.run_once()
    assert len(native.sent) == 1


def context(side='BUY'):
    peer_side = 'ofr' if side=='BUY' else 'bid'
    price = {'kind': 'YIELD', 'value': '3.20' if side=='BUY' else '3.00'}
    return {'our_side': side, 'bond_code': 'SYN-A', 'trade_day': '2026-10-17',
        'unresolved_messages': [], 'reconfirm_day': None, 'boundary': {'kind': 'YIELD', 'value': '3.12'},
        'our_quote': {'price': deepcopy(price), 'native_id': 'synthetic-native'},
        'plan': {'opening_yield_pct': price['value'], 'step_bp': '2', 'volume_wan': '20000'},
        'group_summary': {'reported_shortfall_wan': '19500', 'goal_ready': False},
        'quotes': {peer_side: {'price': deepcopy(price), 'volume_wan': '500', 'assertion': 'QUOTE',
            'validity': 'ACTIVE', 'finality': 'FINAL', 'confirmed_day': '2026-10-17'}}}


def proposal(side='BUY'):
    value = '3.18' if side=='BUY' else '3.02'
    return {'kind': 'REPLY', 'text': 'SYN-A '+value+'%'+('收' if side=='BUY' else '出'),
            'our_price': {'kind': 'YIELD', 'value': value}}


@pytest.mark.parametrize('change', [{'finality': 'NEGOTIABLE'}, {'validity': 'UNCONFIRMED'}, {'assertion': 'INDICATIVE'}])
def test_nonfinal_or_not_confirmed_peer_fact_does_not_create_final_price_guard(change):
    value = context()
    value['quotes']['ofr'].update(change)
    assert Engine.__new__(Engine)._validate_reply(value, proposal())['our_price']['value'] == '3.18'


def test_counterparty_other_side_cannot_constrain_this_reply_direction():
    value = context()
    value['quotes']['bid'] = deepcopy(value['quotes']['ofr'])
    value['quotes']['ofr']['finality'] = 'UNKNOWN'
    assert Engine.__new__(Engine)._validate_reply(value, proposal())['our_price']['value'] == '3.18'


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_verified_comparable_cash_fact_uses_its_yield_for_final_price_guard(side):
    value = context(side)
    peer_side = 'ofr' if side=='BUY' else 'bid'
    quoted_yield = value['quotes'][peer_side]['price']['value']
    value['quotes'][peer_side]['price'] = {'kind': 'NET_PRICE', 'value': '97',
        'settlement_date': '2026-10-17', 'computed_yield': quoted_yield,
        'calculation_ref': 'synthetic-verified-calculation'}
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(value, proposal(side))
