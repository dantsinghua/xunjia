"""Proven input aliases must render exactly as existing fact macros."""
from copy import deepcopy
import json

import pytest

from server.negotiation.engine import Engine
from server.negotiation.reply_render import render_reply_plan
from dify.negotiation_v22.build import build
from dify.negotiation_v22.node_code import compact_context
from test_rendered_reply_plan_revision import context


def current():
    ctx=context();ctx['quote_constraints']=Engine._quote_constraints(ctx)
    ctx['quotes']['ofr']['evidence']['volume_wan']=['peer-a-message']
    return ctx


def render(ctx,plan):
    original=deepcopy((ctx,plan));result=render_reply_plan(ctx,plan)
    assert (ctx,plan)==original
    return result,Engine.__new__(Engine)._validate_reply(ctx,result['proposal'])


@pytest.mark.parametrize('literal',['500万','500.0万','500 万','0.05亿','0.0500亿'])
def test_exact_current_whole_lot_alias_has_identical_parts_and_native_body(literal):
    ctx=current();text='我这边{own_quote}，'+literal+'，你再看看？'
    raw={'kind':'REPLY','quote_value':'3.15','text':text}
    bound,body=render(ctx,raw)
    canonical,canonical_body=render(ctx,{**raw,'text':'我这边{own_quote}，{peer_volume}，你再看看？'})
    assert bound['parts']==canonical['parts'] and bound['economic_text']==canonical['economic_text']
    assert body==canonical_body
    assert bound['proposal']['render_plan']['text']==text


@pytest.mark.parametrize('kind,label',[('NET_PRICE','净价'),('DIRTY_PRICE','全价')])
def test_only_complete_exact_current_cash_reference_alias_matches_the_existing_macro(kind,label):
    ctx=current();ctx['quotes']['ofr']['price']={'kind':kind,'value':'97.00'}
    ctx['context'][0]['text']='SYN-A '+label+'97.00出500万'
    text='你报的'+label+'97.00出500万，结算是哪天？'
    plan={'kind':'CLARIFY','question':'ASK_SETTLEMENT','text':text}
    bound,body=render(ctx,plan)
    canonical,other=render(ctx,{**plan,'text':'你报的{peer_quote}{peer_volume}，结算是哪天？'})
    assert bound['parts']==canonical['parts'] and bound['economic_text']==canonical['economic_text']
    assert body==other and body['text']==text and body.get('settlement_question')
    assert bound['proposal']['render_plan']['text']==text


@pytest.mark.parametrize('literal',['净价NaN出','净价Infinity出'])
def test_nonfinite_cash_declaration_is_not_treated_as_free_social_wording(literal):
    ctx=current();ctx['quotes']['ofr']['price']={'kind':'NET_PRICE','value':'97.00'}
    with pytest.raises(ValueError):
        render(ctx,{'kind':'CLARIFY','question':'ASK_SETTLEMENT','text':'你报'+literal+'，结算是哪天？'})


@pytest.mark.parametrize('text',['{own_quote}，501万','{own_quote}，500万元','{own_quote}，500万亿',
                                '{own_quote}，NaN万','{own_quote}，Infinity万'])
def test_unproven_or_nonfinite_quantity_declarations_remain_rejected(text):
    with pytest.raises(ValueError):render(current(),{'kind':'REPLY','quote_value':'3.15','text':text})


@pytest.mark.parametrize('violation',['question','missing_evidence','stale_day'])
def test_only_source_proven_current_quote_lot_has_a_literal_alias(violation):
    ctx=current()
    if violation=='question':ctx['quotes']['ofr']['assertion']='QUESTION'
    elif violation=='missing_evidence':ctx['quotes']['ofr']['evidence']['volume_wan']=[]
    else:ctx['quotes']['ofr']['confirmed_day']='2026-10-16'
    with pytest.raises(ValueError):render(ctx,{'kind':'REPLY','quote_value':'3.15','text':'{own_quote}，500万'})


@pytest.mark.parametrize('policy',['latest_quote_confirmation_v1','explicit_anchors_v0'])
def test_production_view_hides_only_mechanical_ids_without_mutating_the_original_context(policy):
    ctx=current();ctx.update(schema_version='2.2',stage='reply',context_policy_revision=policy)
    ctx['our_quote'].update(command_id='synthetic-command',message_id='synthetic-out',verified_sent=True)
    ctx['context'][0].update(native_id='synthetic-native',source_seq=9)
    original=deepcopy(ctx);view=compact_context(ctx)
    assert ctx==original and [r['text'] for r in view['context']]==[r['text'] for r in ctx['context']]
    assert view['quotes']==ctx['quotes']
    if policy=='latest_quote_confirmation_v1':
        assert 'evidence_index' not in view and 'member_id' not in view and 'thread_id' not in view
        assert not {'message_id','native_id','source_seq'}&set(view['context'][0])
        assert not {'command_id','message_id','native_id'}&set(view['our_quote'])
        assert view['our_quote']['verified_sent'] is True and view['our_quote']['price']==ctx['our_quote']['price']
    else:
        assert view['evidence_index'] and view['member_id']==ctx['member_id']
        assert view['context'][0]['message_id']==ctx['context'][0]['message_id']


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_generated_code_still_uses_the_original_context_for_source_bound_aliases(version):
    ctx=current();ctx.update(schema_version='2.2',stage='reply',context_policy_revision='latest_quote_confirmation_v1')
    raw={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'3.15','text':'{own_quote}，500万，可以报价吗？'}}
    graph=build('reply',version=version,provider='synthetic',model='synthetic')['workflow']['graph']
    nodes={n['id']:n['data'] for n in graph['nodes']};scope={};exec(nodes['contract']['code'],scope)
    result=json.loads(scope['main'](json.dumps(raw),json.dumps(ctx))['result'])
    assert result['proposal']['text']=='3.15%收，500万，可以报价吗？'
    assert result['proposal']['render_plan']['text']==raw['reply_plan']['text']
