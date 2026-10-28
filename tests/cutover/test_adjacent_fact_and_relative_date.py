"""Minimal same-field formatting and explicit relative-date regressions."""
from copy import deepcopy

from test_settlement_question_semantics import context
from test_verified_literal_fact_binding import assert_same_macro, settlement, validated


def test_identical_adjacent_current_peer_field_equals_the_original_single_source_macro_body():
    assert_same_macro(context(),
        settlement('您好，您报的{peer_quote}净价34.19出{peer_volume}万，请问结算日期是哪一天？'),
        settlement('您好，您报的{peer_quote}{peer_volume}，请问结算日期是哪一天？'))


def test_explicit_tomorrow_cash_question_keeps_its_time_dimension_without_fabricating_a_date():
    value=context();plan=settlement('请问现金交割明天可以吗？');before=deepcopy((value,plan))
    _rendered,body=validated(value,plan)
    assert body['text']==plan['text'] and body['kind']=='CLARIFY' and body.get('our_price') is None
    assert value['quotes']['ofr']['price']['settlement_date'] is None and (value,plan)==before
