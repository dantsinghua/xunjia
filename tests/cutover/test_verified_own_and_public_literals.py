"""Literal spellings may bind only to the same already-authorized macro P*."""
import pytest

from server.negotiation.prices import InvalidFact
from test_verified_literal_fact_binding import quantity_context, reply, assert_same_macro, validated
from test_rendered_reply_plan_revision import with_public


@pytest.mark.parametrize('side,literal', [('BUY','3.15%收'),('BUY','3.15收'),('SELL','3.15%出')])
def test_complete_same_value_own_offer_equals_the_original_verified_macro(side,literal):
    assert_same_macro(quantity_context(side),
        reply('这边'+literal+'，你再看看？'), reply('这边{own_quote}，你再看看？'))


@pytest.mark.parametrize('literal', ['3.15收500万','3.15 % 收500万'])
def test_complete_own_price_followed_by_the_same_verified_lot_equals_both_original_macros(literal):
    assert_same_macro(quantity_context(),reply('这边'+literal+'，你再看看？'),
        reply('这边{own_quote}{peer_volume}，你再看看？'))


@pytest.mark.parametrize('modal', ['可以考虑','可以商量','可以吗'])
def test_redundant_whole_lot_unit_before_a_complete_modal_word_keeps_the_original_fact_body(modal):
    from server.negotiation.engine import Engine
    context=quantity_context('SELL')
    context['boundary']['value']='3.12'
    context['quotes']['bid']['volume_wan']='2200'
    context['context'][0]['text']=context['context'][0]['text'].replace('500万','2200万')
    context['quote_constraints']=Engine._quote_constraints(context)
    assert context['quote_constraints']['max_yield_pct']=='3.12'
    literal={**reply('这边{own_quote}出，{peer_volume}万'+modal+'。'),'quote_value':'3.12'}
    macro={**reply('这边{own_quote}，{peer_volume}'+modal+'。'),'quote_value':'3.12'}
    assert_same_macro(context,literal,macro)


def test_complete_actual_public_calculation_text_equals_its_original_source_bound_macro():
    context, records = with_public('bond_calc')
    record = records[0]
    assert_same_macro(context,
        reply(record['text']+'这边{own_quote}，你再看看？'),
        reply('{info:'+record['id']+'}这边{own_quote}，你再看看？'))


def test_actual_public_fact_and_own_offer_both_keep_their_separate_source_roles():
    context, records = with_public('bond_calc')
    record = records[0]
    assert_same_macro(context,reply(record['text']+'这边3.15收，你再看看？'),
        reply('{info:'+record['id']+'}这边{own_quote}，你再看看？'))


@pytest.mark.parametrize('literal', ['3.14%收','3.15%出','净价3.15收','全价3.15%收'])
def test_wrong_value_side_or_cash_label_cannot_be_discarded_to_create_an_own_offer(literal):
    with pytest.raises((ValueError,InvalidFact)):
        validated(quantity_context(),reply('这边'+literal+'，你再看看？'))


def test_adjacent_received_or_travel_words_cannot_be_reinterpreted_as_an_own_offer():
    for literal in ['3.15收到你的意思了','3.15出差再说']:
        with pytest.raises((ValueError,InvalidFact)):
            validated(quantity_context(),reply(literal))


def test_binding_never_supplies_missing_structured_price_or_authorizes_nonquote_or_bare_send():
    from dify.negotiation_v22.node_code import decode_model_output
    context=quantity_context(); context.update(schema_version='2.2',stage='reply')
    missing=reply('这边3.15%收，你再看看？');missing.pop('quote_value')
    for plan in [missing,{'kind':'CLARIFY','question':'NONE','text':'这边3.15%收，请确认？'}]:
        with pytest.raises((ValueError,InvalidFact)):
            validated(context,plan)
    with pytest.raises(ValueError):
        decode_model_output({'schema_version':'2.2','action':'SEND','proposal':{
            'kind':'REPLY','text':'这边3.15%收','our_price':{'kind':'YIELD','value':'3.15'}}},context,'reply')
    for plan in [reply('这边3.11%收，你再看看？'),reply('3.15%收，'+'嗯'*1500)]:
        with pytest.raises((ValueError,InvalidFact)):validated(context,plan)


def test_partial_changed_or_unexecuted_public_calculation_is_never_a_verified_fact_fragment():
    context, records=with_public('bond_calc');record=records[0]
    for text in ['10BP',record['text'].replace('10.00','11.00'),record['text'][1:]]:
        assert text!=record['text']
        with pytest.raises((ValueError,InvalidFact)):
            validated(context,reply(text+'这边{own_quote}，你再看看？'))
    context['analysis_tool_results']=[];context['tool_results']=[]
    with pytest.raises((ValueError,InvalidFact)):
        validated(context,reply(record['text']+'这边{own_quote}，你再看看？'))
