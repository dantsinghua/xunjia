"""Only verified adjacent duplicate components canonicalize; no free rewrite."""
from copy import deepcopy
import json
import traceback

import pytest

from server.negotiation.reply_render import render_reply_plan
from server.negotiation.engine import Engine
from server.negotiation.chatflow import _http_error_code
from dify.negotiation_v22.prompts import select_system_prompt
from dify.negotiation_v22.build import build
from test_literal_fact_binding_impl import current
from test_rendered_reply_plan_revision import with_public


def equivalent(ctx,raw,canonical):
    plan={'kind':'REPLY','quote_value':'3.15','text':raw}
    left=render_reply_plan(ctx,plan);right=render_reply_plan(ctx,{**plan,'text':canonical})
    assert left['parts']==right['parts'] and left['economic_text']==right['economic_text']
    assert Engine.__new__(Engine)._validate_reply(ctx,left['proposal'])==Engine.__new__(Engine)._validate_reply(ctx,right['proposal'])
    assert left['proposal']['render_plan']['text']==raw


@pytest.mark.parametrize('raw,canonical',[
    ('这边{own_quote}收，你再看看？','这边{own_quote}，你再看看？'),
    ('这边{own_quote}%收，你再看看？','这边{own_quote}，你再看看？'),
    ('你报{peer_quote}出，我这边{own_quote}。','你报{peer_quote}，我这边{own_quote}。'),
    ('{own_quote}，{peer_volume}万可以吗？','{own_quote}，{peer_volume}可以吗？'),
])
def test_verified_adjacent_duplicate_field_component_matches_the_existing_canonical_plan(raw,canonical):
    equivalent(current(),raw,canonical)


def test_verified_info_sentence_and_one_same_period_do_not_duplicate_punctuation():
    ctx,infos=with_public('bond_info');ctx['quote_constraints']=Engine._quote_constraints(ctx)
    actual=next(x['id'] for x in infos if x['source']['field']=='master.issuer_id')
    equivalent(ctx,'已查到{info:'+actual+'}。我这边{own_quote}。',
                   '已查到{info:'+actual+'}我这边{own_quote}。')


@pytest.mark.parametrize('text',['{own_quote}出，看看？','{own_quote}%出，看看？','{own_quote}，{peer_volume}亿可以吗？',
                                '{own_quote}，{peer_volume}万万可以吗？'])
def test_conflicting_direction_or_multiple_units_cannot_be_trimmed_into_permission(text):
    with pytest.raises(ValueError):render_reply_plan(current(),{'kind':'REPLY','quote_value':'3.15','text':text})


def test_separated_natural_words_are_never_absorbed_as_affixes():
    text='{own_quote}，{peer_volume}，收到消息后再聊，出差也没关系，万一有变化再说。'
    result=render_reply_plan(current(),{'kind':'REPLY','quote_value':'3.15','text':text})
    assert '收到消息' in result['proposal']['text'] and '出差' in result['proposal']['text'] and '万一' in result['proposal']['text']


def correction_context(reason='DIFY_MODEL_CONTRACT_SLOTS',*,cash=False):
    ctx=current();ctx.update(schema_version='2.2',stage='reply')
    plan={'kind':'REPLY','quote_value':'3.15','text':'{own_quote}，10BP'}
    if cash:
        ctx['quotes']['ofr']['price']={'kind':'NET_PRICE','value':'97.00'}
        plan={'kind':'CLARIFY','question':'ASK_SETTLEMENT','text':'净价97.00出，结算是T+0还是T+1？'}
    ctx['validation_feedback']={'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,
        'reason_code':reason,'previous_output':{'schema_version':'2.2','reply_plan':plan}}
    return ctx


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_output_correction_is_a_narrow_task_not_another_round_of_negotiation(version):
    ctx=correction_context();before=deepcopy(ctx);system=select_system_prompt(ctx,'reply')
    assert '不是新一轮谈价' in system and '你负责B谈价与自然聊天' not in system
    assert '保留原kind' in system and '保留原quote_value' in system
    graph=build('reply',version=version,provider='synthetic',model='synthetic')['workflow']['graph']
    scope={};exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='context'),scope)
    assert scope['main'](json.dumps(ctx))['system_prompt']==system and ctx==before


def test_focused_cash_repair_asks_settlement_without_numbers_or_requoting():
    system=select_system_prompt(correction_context(cash=True),'reply')
    assert '只自然询问结算安排' in system and '不复述价量' in system
    assert '不列T日期或数字选项' in system


def test_nonquote_own_value_rejection_can_carry_the_actual_invalid_draft_without_granting_it_permission():
    ctx=current();ctx.update(schema_version='2.2',stage='reply')
    candidate={'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','quote_value':'3.15','text':'暂时{own_quote}，再聊？'}}
    graph=build('reply',version='0.15.3',provider='synthetic',model='synthetic')['workflow']['graph']
    scope={};exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='contract'),scope)
    try:scope['main'](json.dumps(candidate),json.dumps(ctx))
    except ValueError:
        body={'code':'invalid_param','status':400,'message':'Run failed: '+traceback.format_exc()+'error: exit status 255\n'}
        assert _http_error_code(400,json.dumps(body).encode())=='DIFY_MODEL_CONTRACT_OWN_QUOTE'
    else:pytest.fail('Nonquote cannot become an actual quote through a private error envelope')
