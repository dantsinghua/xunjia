"""Deterministic public-text checks on the production reply validator."""
import pytest
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact, Price
from server.negotiation.validation import validate_observation


def context():
    return {'member_id': 'p', 'bond_code': 'SYN-A', 'our_side': 'BUY',
            'reconfirm_day': None, 'boundary': {'kind': 'YIELD', 'value': '3.12'},
            'our_quote': {'price': {'kind': 'YIELD', 'value': '3.20'}},
            'plan': {'opening_yield_pct': '3.20', 'step_bp': '2', 'volume_wan': '1000'},
            'group_summary': {'reported_shortfall_wan': '500'},
            'quotes': {'ofr': {'volume_wan': '500'}, 'bid': {'volume_wan': None}}}


@pytest.mark.parametrize('text', ['3.19%收，9000万', '3.19%收，500万，成交锁定',
                                  '3.19%收，也可以3.10收500万'])
def test_public_reply_rejects_unapproved_amount_commitment_and_second_price(text):
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(context(), {'kind': 'REPLY', 'text': text,
                                               'our_price': Price('YIELD', '3.19').dump()})


@pytest.mark.parametrize('kind', ['SOCIAL', 'CLARIFY', 'ASK_VOLUME', 'RECONFIRM'])
def test_nonquote_action_cannot_smuggle_an_unvalidated_price(kind):
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(context(), {'kind': kind, 'text': '3.01收，1000万'})


def test_valid_bounded_reply_remains_available():
    result = Engine.__new__(Engine)._validate_reply(context(),
        {'kind': 'REPLY', 'text': '3.19%收，500万，价格还能再给一点吗？',
         'our_price': Price('YIELD', '3.19').dump()})
    assert result['our_price']['value'] == '3.19'


def test_valid_quote_cannot_disclose_internal_pricing_policy():
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(context(),
            {'kind':'REPLY','text':'3.19%收，500万，我们内部底价就是3.19%。',
             'our_price':Price('YIELD','3.19').dump()})


def test_ask_volume_respects_shared_remaining_amount():
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(context(), {'kind': 'ASK_VOLUME', 'text': '再找1000万可以吗？'})


@pytest.mark.parametrize('text,offset,yield_value,side', [
    ('SYN-A 中债估值减5BP收', '-5', '2.53', 'bid'),
    ('SYN-A 中债估值加5BP出', '5', '3.04', 'ofr'),
])
def test_bp_evidence_preserves_chinese_offset_sign(text, offset, yield_value, side):
    snapshot = {'members': [{'member_id': 'p', 'context': [], 'quotes': {'bid': {}, 'ofr': {}}}],
                'messages': [{'message_id': 'm', 'text': text, 'direction': 'in', 'ingest_revision':1}]}
    observation = {'member_id': 'p', 'side': side,
                   'changes': {'price': Price('YIELD', yield_value).dump()}, 'evidence': {'price': ['m']}}
    result = validate_observation(snapshot, observation, {'syn-a': {'p'}},
                                 raw_price={'kind': 'YIELD', 'notation': 'CHINABOND_BP', 'value': offset})
    assert result['changes']['price']['value'] == yield_value


def test_reference_to_our_bid_does_not_override_counterpartys_explicit_offer_side():
    sent={'message_id':'sent','text':'SYN-A 3.20%收。请报可做价格和数量。','direction':'out','ingest_revision':1}
    incoming={'message_id':'new','text':'SYN-A 按你刚才那个3.20收的价，我出500万，不能再改善了','direction':'in','ingest_revision':2}
    snapshot={'members':[{'member_id':'p','context':[sent],'quotes':{'bid':{},'ofr':{}}}], 'messages':[incoming]}
    changes={'price':Price('YIELD','3.20').dump(),'volume_wan':'500','finality':'FINAL'}
    observation={'member_id':'p','side':'ofr','changes':changes,
        'evidence':{'price':['sent','new'],'volume_wan':['new'],'finality':['new']},
        'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUOTE','negotiation_space':'FINAL',
                          'reason':'对手明确我出，引用双方实际已发价格并明确无进一步空间'}}
    result=validate_observation(snapshot,observation,{'syn-a':{'p'}},raw_price=changes['price'])
    assert result['side']=='ofr' and result['changes']['volume_wan']=='500'
