"""Reply-only final formatting reminder preserves graph modes and A input."""
import json

import pytest

from dify.negotiation_v22 import prompts
from dify.negotiation_v22.build import build


ORIGINAL_USER='本轮完整业务context_json（仅省数据库机械字段，原文/证据未裁剪）：\n{{#context.context#}}'


def nodes(stage,version):
    return {n['id']:n['data'] for n in build(stage,version=version,
        provider='synthetic-provider',model='synthetic-model')['workflow']['graph']['nodes']}


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_analysis_keeps_exact_original_user_prompt_and_model_parameters(version):
    model=nodes('analysis',version)['model']
    assert model['prompt_template']==[{'role':'system','text':'{{#context.system_prompt#}}'},
                                     {'role':'user','text':ORIGINAL_USER}]
    assert model['model']=={'provider':'synthetic-provider','name':'synthetic-model',
                           'mode':'chat','completion_params':{'temperature':0}}


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_reply_footer_follows_all_context_and_does_not_expand_or_force_an_action(version):
    model=nodes('reply',version)['model']
    text=model['prompt_template'][1]['text']
    assert text.startswith(ORIGINAL_USER+'\n')
    footer=text[len(ORIGINAL_USER):]
    assert all(part in footer for part in ['当前模式','未展开','{peer_volume}','可不提量',
                                          '技术ID','裸数字','成交','锁量'])
    assert '必须SEND' not in footer and '只输出reply_plan' not in footer
    assert model['prompt_template'][0]=={'role':'system','text':'{{#context.system_prompt#}}'}
    assert model['model']['completion_params']=={'temperature':0}


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
@pytest.mark.parametrize('mode',['ordinary','clarify','text_only'])
def test_generated_context_still_selects_the_existing_mode_without_footer_side_effects(version,mode):
    ctx={'schema_version':'2.2','stage':'reply','member_id':'synthetic-member','our_side':'BUY',
         'context':[],'quote_constraints':{'status':'AVAILABLE','min_yield_pct':'3.12','max_yield_pct':None}}
    if mode=='clarify':ctx['reference_confirmation_required']=True
    if mode=='text_only':
        ctx['validation_feedback']={'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
                                    'original_response':{'schema_version':'2.2'}}
    data=nodes('reply',version)
    scope={}
    exec(data['context']['code'],scope)
    result=scope['main'](json.dumps(ctx))
    assert result['system_prompt']==prompts.select_system_prompt(ctx,'reply')
    projected=json.loads(result['context'])
    assert projected['stage']=='reply' and projected['member_id']=='synthetic-member'
    if mode=='clarify':assert projected['reply_requirements']['allowed_kinds']==['CLARIFY']
    if mode=='text_only':assert projected['validation_feedback']==ctx['validation_feedback']
