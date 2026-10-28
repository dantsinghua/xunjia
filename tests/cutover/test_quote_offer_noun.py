"""Offer noun 出盘 reuses the same ofr direction, never market-close 收盘."""
from copy import deepcopy
import json

import pytest

from server.negotiation.prices import InvalidFact
from server.negotiation.engine import Engine
from test_verified_literal_fact_binding import quantity_context,reply,assert_same_macro,validated,cash_context,settlement
from test_reply_output_repair_projection import generated


@pytest.mark.parametrize('ending',['为参考。','，目前先看看。','。'])
def test_clean_counterparty_offer_noun_keeps_exact_bound_fragment_and_source(ending):
    context=quantity_context();context.update(schema_version='2.2',stage='reply')
    literal=reply('我方{own_quote}，当前参考对方{peer_quote}出盘'+ending)
    macro=reply('我方{own_quote}，当前参考对方{peer_quote}盘'+ending)
    before=deepcopy((context,literal,macro))
    _rendered,expected=assert_same_macro(context,literal,macro)
    for version in ['0.15.3','1.17.1']:
        _project,decode=generated(version)
        result=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':literal}),json.dumps(context))['result'])
        assert Engine.__new__(Engine)._validate_reply(context,result['proposal'])==expected
    assert (context,literal,macro)==before



def test_close_opposite_direction_and_unknown_offer_continuations_remain_unavailable():
    context=quantity_context();before=deepcopy(context)
    for text in ['我方{own_quote}收盘为参考。','对方{peer_quote}收盘为参考，我方{own_quote}。',
            '我方{own_quote}出盘为参考。','对方{peer_quote}出盘价为参考，我方{own_quote}。',
            '对方{peer_quote}出盘foo，我方{own_quote}。','对方{peer_quote}出盘与其他条件一起看，我方{own_quote}。',
            '对方{peer_quote}出差后再看，我方{own_quote}。']:
        with pytest.raises((ValueError,InvalidFact)):validated(context,reply(text))
    assert context==before


def test_offer_noun_does_not_forgive_incompatible_cash_percent_type():
    context=cash_context();before=deepcopy(context)
    with pytest.raises((ValueError,InvalidFact)):
        validated(context,settlement('参考对方{peer_quote}%出盘，结算日期是哪一天？'))
    assert context==before


@pytest.mark.parametrize('side,text',[('BUY','我方{peer_quote}出盘为参考，我现在{own_quote}。'),
    ('SELL','对方{own_quote}出盘为参考，后面再看看？')])
def test_new_offer_noun_cannot_attribute_a_valid_fragment_to_the_wrong_party(side,text):
    context=quantity_context(side);before=deepcopy(context)
    with pytest.raises((ValueError,InvalidFact)):validated(context,reply(text))
    assert context==before


def test_correct_subject_polite_perception_and_local_negation_still_bind_the_same_field():
    for side,literal,macro in [
        ('BUY','对方{peer_quote}出盘为参考，我现在{own_quote}。','对方{peer_quote}盘为参考，我现在{own_quote}。'),
        ('SELL','我方{own_quote}出盘为参考，您再看看？','我方{own_quote}盘为参考，您再看看？'),
        ('SELL','你看看{own_quote}出盘为参考，这个报价再聊？','你看看{own_quote}盘为参考，这个报价再聊？'),
        ('BUY','不是我方{peer_quote}出盘为参考，我现在{own_quote}。','不是我方{peer_quote}盘为参考，我现在{own_quote}。')]:
        context=quantity_context(side)
        assert_same_macro(context,reply(literal),reply(macro))


# Exact observed unsafe draft is a negative oracle, never a normalization success.
UNSAFE_PLAN = {'kind': 'REPLY', 'question': 'NONE', 'quote_value': '3.12', 'text': 'SYN-A 本轮我方按{own_quote}收，当前以对方{peer_quote}出盘为参考，具体成交与锁量后续再沟通'}


def test_original_unsafe_draft_remains_a_public_execution_rejection_after_lexing():
    context=quantity_context();before=deepcopy((context,UNSAFE_PLAN))
    with pytest.raises(ValueError,match='Reply promises execution or reserves a counterparty lot'):
        validated(context,UNSAFE_PLAN)
    assert (context,UNSAFE_PLAN)==before
