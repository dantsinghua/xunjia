"""Observed reference aliases and bounded unavailable-source recovery only."""
from copy import deepcopy
import json
import traceback

import pytest

from dify.negotiation_v22.build import build
from dify.negotiation_v22.prompts import select_system_prompt
from server.negotiation.reply_render import render_reply_plan
from server.negotiation.engine import Engine
from server.negotiation.chatflow import _http_error_code
from test_literal_fact_binding_impl import current
from test_rendered_reply_plan_revision import with_public


@pytest.mark.parametrize('value',['3.10','-0.05'])
def test_complete_current_peer_yield_alias_is_the_existing_peer_macro(value):
    ctx=current();ctx['quotes']['ofr']['price']['value']=value
    ctx['context'][0]['text']='SYN-A '+value+'%出500万'
    literal='你刚才'+value+'%出，我这边{own_quote}，再看看？'
    left=render_reply_plan(ctx,{'kind':'REPLY','quote_value':'3.15','text':literal})
    right=render_reply_plan(ctx,{'kind':'REPLY','quote_value':'3.15','text':'你刚才{peer_quote}，我这边{own_quote}，再看看？'})
    assert left['parts']==right['parts'] and left['economic_text']==right['economic_text']
    assert Engine.__new__(Engine)._validate_reply(ctx,left['proposal'])==Engine.__new__(Engine)._validate_reply(ctx,right['proposal'])
    assert left['proposal']['render_plan']['text']==literal


def test_only_an_actual_public_information_id_gets_the_existing_namespace_alias():
    ctx,infos=with_public('bond_info');ctx['quote_constraints']=Engine._quote_constraints(ctx)
    actual=next(row['id'] for row in infos if row['source']['field']=='master.issuer_id')
    literal='资料查到了：{'+actual+'}我这边{own_quote}，你再看看？'
    left=render_reply_plan(ctx,{'kind':'REPLY','quote_value':'3.15','text':literal})
    right=render_reply_plan(ctx,{'kind':'REPLY','quote_value':'3.15','text':literal.replace('{'+actual+'}','{info:'+actual+'}')})
    assert left['parts']==right['parts']
    assert Engine.__new__(Engine)._validate_reply(ctx,left['proposal'])==Engine.__new__(Engine)._validate_reply(ctx,right['proposal'])
    assert left['proposal']['render_plan']['text']==literal


@pytest.mark.parametrize('withdrawn',[True,False])
def test_unavailable_source_errors_have_only_the_new_fixed_controlled_enum(withdrawn):
    ctx=current();ctx.update(schema_version='2.2',stage='reply')
    if withdrawn:ctx['quotes']['ofr']['validity']='WITHDRAWN'
    text='你刚才{peer_quote}，现在还方便报价吗？' if withdrawn else '请核实{not_an_available_source}。'
    graph=build('reply',version='0.15.3',provider='synthetic',model='synthetic')['workflow']['graph']
    scope={};exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='contract'),scope)
    try:scope['main'](json.dumps({'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','text':text}}),json.dumps(ctx))
    except ValueError:
        body={'code':'invalid_param','status':400,'message':'Run failed: '+traceback.format_exc()+'error: exit status 255\n'}
        assert _http_error_code(400,json.dumps(body).encode())=='DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT'
    else:pytest.fail('Unavailable facts must remain rejected')


def calculation_context():
    ctx=current();ctx.update(schema_version='2.2',stage='reply')
    ctx['context'][-1].update(direction='in',historical=False,ingest_revision=21,
        text='SYN-A 3.10出500万，另外请核算：假设同一到期收益率口径3.20和3.10相差多少BP？回复不重复这两个原始收益率，不作成交。')
    return ctx


def test_existing_narrow_calculation_intent_prioritizes_the_existing_tool_protocol_only_once():
    ctx=calculation_context();before=deepcopy(ctx);system=select_system_prompt(ctx,'reply')
    assert system.startswith('本轮明确计算咨询且尚未尝试bond_calc')
    assert system.index('requests-only')<system.index('本轮受信执行摘要')
    assert ctx==before
    ctx['tool_results']=[{'request':{'name':'bond_calc','arguments':{}},'result':{'status':'unavailable'}}]
    assert not select_system_prompt(ctx,'reply').startswith('本轮明确计算咨询且尚未尝试bond_calc')


def test_tool_first_hint_never_replaces_an_actual_text_only_frozen_repair():
    ctx=calculation_context();ctx['validation_feedback']={'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
                                                        'original_response':{'schema_version':'2.2'}}
    assert not select_system_prompt(ctx,'reply').startswith('本轮明确计算咨询且尚未尝试bond_calc')
