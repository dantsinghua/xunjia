"""A redundant bound lot unit before a clear question keeps the original fact."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.prices import InvalidFact
from server.negotiation.engine import Engine
from test_verified_literal_fact_binding import quantity_context, assert_same_macro, validated


@pytest.mark.parametrize('question', ['是否', '是不是', '能否', '可否'])
def test_verified_whole_lot_unit_before_a_question_is_the_same_authorized_macro(question):
    context = quantity_context()
    context.update(schema_version='2.2', stage='reply')
    prefix = '感谢贵方的报价。请确认您当前的报价{peer_quote}{peer_volume}'
    suffix = question+'为最终可做价格？'
    literal = {'kind':'CLARIFY', 'question':'NONE', 'text':prefix+'万'+suffix}
    canonical = {**literal, 'text':prefix+suffix}
    before = deepcopy((context, literal, canonical))
    _rendered, expected = assert_same_macro(context, literal, canonical)
    for version in ['0.15.3', '1.17.1']:
        nodes = {row['id']:row['data'] for row in build('reply', version=version,
            provider='synthetic', model='synthetic')['workflow']['graph']['nodes']}
        projection, contract = {}, {}
        exec(nodes['context']['code'], projection)
        exec(nodes['contract']['code'], contract)
        projected = projection['main'](json.dumps(context))
        assert projected['system_prompt']
        decoded = json.loads(contract['main'](json.dumps({'schema_version':'2.2',
            'reply_plan':literal}), json.dumps(context))['result'])
        assert decoded['proposal']['render_plan']['text'] == literal['text']
        assert Engine.__new__(Engine)._validate_reply(context, decoded['proposal']) == expected
    assert (context, literal, canonical) == before


def test_question_boundary_cannot_authorize_ambiguous_units_or_unavailable_peer_volume():
    context = quantity_context()
    before = deepcopy(context)
    for unit in ['万一', '万万', '万元', '万亿', '亿']:
        plan = {'kind':'CLARIFY','question':'NONE',
            'text':'请确认{peer_quote}{peer_volume}'+unit+'是否为最终可做价格？'}
        with pytest.raises((ValueError, InvalidFact)):
            validated(context, plan)
    assert context == before
    for change in ['QUESTION', 'missing_source']:
        unavailable = deepcopy(context)
        if change == 'QUESTION': unavailable['quotes']['ofr']['assertion'] = change
        else: unavailable['quotes']['ofr']['evidence']['volume_wan'] = []
        before = deepcopy(unavailable)
        for volume in ['{peer_volume}万', '20000万']:
            plan = {'kind':'CLARIFY','question':'NONE',
                'text':'请确认'+volume+'是否为最终可做整量？'}
            with pytest.raises((ValueError, InvalidFact)):
                validated(unavailable, plan)
        assert unavailable == before



def test_verified_whole_lot_before_genitive_settlement_question_keeps_the_original_macro_body():
    from test_verified_literal_fact_binding import cash_context, settlement
    context = cash_context()
    context.update(schema_version='2.2', stage='reply')
    context['quotes']['ofr']['price']['value'] = '34.19'
    context['context'][0]['text'] = 'SYN-A净价34.19出500万'
    literal = settlement('请确认一下，您提到的SYN-A净价34.19出{peer_volume}万的结算日是哪一天？')
    canonical = settlement('请确认一下，您提到的SYN-A{peer_quote}{peer_volume}的结算日是哪一天？')
    before = deepcopy((context, literal, canonical))
    _rendered, expected = assert_same_macro(context, literal, canonical)
    assert expected['kind'] == 'CLARIFY' and expected.get('our_price') is None
    for version in ['0.15.3', '1.17.1']:
        nodes = {row['id']:row['data'] for row in build('reply', version=version,
            provider='synthetic', model='synthetic')['workflow']['graph']['nodes']}
        projection, contract = {}, {}
        exec(nodes['context']['code'], projection)
        exec(nodes['contract']['code'], contract)
        assert projection['main'](json.dumps(context))['system_prompt']
        decoded = json.loads(contract['main'](json.dumps({'schema_version':'2.2',
            'reply_plan':literal}), json.dumps(context))['result'])
        assert decoded['proposal']['render_plan']['text'] == literal['text']
        assert Engine.__new__(Engine)._validate_reply(context, decoded['proposal']) == expected
    assert (context, literal, canonical) == before
