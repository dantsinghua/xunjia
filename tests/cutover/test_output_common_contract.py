"""Every B OUTPUT correction returns a complete public quotation protocol."""
from copy import deepcopy
import json

import pytest

from server.negotiation.engine import Engine
from test_reply_output_repair_projection import context_with_feedback,generated


def controlled_context(reason):
    context=context_with_feedback()
    if reason=='UNAVAILABLE':
        context['validation_feedback']['reason_code']='DIFY_MODEL_CONTRACT_UNAVAILABLE_FACT_SLOT'
        context['validation_feedback']['previous_output']['reply_plan']['text']='这边{own_quote}，{info:synthetic-unavailable}。'
    return context


def complete_final_object_task(system):
    return 'reply_plan' in system and any('完整' in line
        and any(word in line for word in ('协议对象','JSON对象','完整对象'))
        and any(word in line for word in ('patch','补丁','改动字段')) for line in system.splitlines())


@pytest.mark.parametrize('reason',['SLOTS','UNAVAILABLE'])
def test_generated_output_requires_a_complete_nested_reply_object_and_public_nonexecution(reason):
    context=controlled_context(reason);before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        project,_decode=generated(version);system=project(json.dumps(context))['system_prompt']
        assert complete_final_object_task(system), 'OUTPUT must explicitly require the complete final object, not a changed-fields patch'
        assert any('reply_plan' in line and '内部' in line and all(key in line for key in ('kind','text','quote_value'))
            for line in system.splitlines())
        assert '报价确认' in system and all(word in system for word in ('成交','锁量'))
        assert any(word in system for word in ('内部策略','私策','底价'))
        valid={'schema_version':'2.2','reply_plan':{'kind':'REPLY','text':'这边{own_quote}，请再看看？','quote_value':'1.55','question':'NONE'}}
        for change in ['missing','top_level','numeric','object']:
            bad=deepcopy(valid)
            if change=='missing':bad['reply_plan'].pop('quote_value')
            elif change=='top_level':bad['quote_value']=bad['reply_plan'].pop('quote_value')
            elif change=='numeric':bad['reply_plan']['quote_value']=1.55
            else:bad['reply_plan']['quote_value']={'kind':'YIELD','value':'1.55'}
            with pytest.raises(ValueError):_decode(json.dumps(bad),json.dumps(context))
        result=json.loads(_decode(json.dumps(valid),json.dumps(context))['result'])
        body=Engine.__new__(Engine)._validate_reply(context,result['proposal'])
        assert body['our_price']['value']=='1.55' and result['proposal']['render_plan']['quote_value']=='1.55'
        for public_text in ['这边{own_quote}，可以考虑成交，请确认。','这边{own_quote}，我给您锁量。',
                '这边{own_quote}，内部底价就到这。']:
            bad=deepcopy(valid);bad['reply_plan']['text']=public_text
            with pytest.raises(ValueError):_decode(json.dumps(bad),json.dumps(context))
        assert context==before



def test_boundary_correction_nonquote_and_intermediate_protocols_keep_their_original_permissions():
    from test_dify_output_contract_recovery import failed_candidate,REASONS
    for version in ['0.15.3','1.17.1']:
        project,decode=generated(version)
        boundary=controlled_context('SLOTS')
        boundary['validation_feedback'].update(reason_code='DIFY_MODEL_CONTRACT_BOUNDARY',
            previous_output=failed_candidate(REASONS[1][0]))
        before=deepcopy(boundary)
        assert project(json.dumps(boundary))['system_prompt']
        good={'schema_version':'2.2','reply_plan':{'kind':'REPLY','question':'NONE','quote_value':'1.55','text':'这边{own_quote}，请再看看？'}}
        corrected=json.loads(decode(json.dumps(good),json.dumps(boundary))['result'])
        assert Engine.__new__(Engine)._validate_reply(boundary,corrected['proposal'])['our_price']['value']=='1.55'
        assert boundary['validation_feedback']['previous_output']['reply_plan']['quote_value']=='1.39'
        bad=deepcopy(good);bad['reply_plan']['quote_value']='1.39'
        with pytest.raises(ValueError):decode(json.dumps(bad),json.dumps(boundary))
        assert boundary==before
        context=controlled_context('SLOTS')
        context['validation_feedback']['previous_output']['reply_plan']={'kind':'CLARIFY','question':'NONE','text':'请确认501万是否为整量？'}
        before=deepcopy(context)
        nonquote={'schema_version':'2.2','reply_plan':{'kind':'CLARIFY','question':'NONE','text':'请确认您本人当前的报价和整量？'}}
        result=json.loads(decode(json.dumps(nonquote),json.dumps(context))['result'])
        assert result['proposal']['kind']=='CLARIFY' and not result['proposal'].get('our_price')
        bad=deepcopy(nonquote);bad['reply_plan']['quote_value']='1.55'
        with pytest.raises(ValueError):decode(json.dumps(bad),json.dumps(context))
        request={'schema_version':'2.2','tool_requests':[{'name':'issuer_info','arguments':{
            'schema_version':'1','mode':'get','issuer_id':'SYN-ISSUER'}}]}
        assert json.loads(decode(json.dumps(request),json.dumps(context))['result'])==request
        for action in ['WAIT','HANDOFF']:
            result=json.loads(decode(json.dumps({'schema_version':'2.2','action':action}),json.dumps(context))['result'])
            assert result['action']==action and not result.get('proposal')
        assert context==before


def test_common_output_task_does_not_reassign_other_prompt_roles_or_mutate_policy_globals():
    from test_prompt_modes import ordinary,repairing,generated as modes
    from dify.negotiation_v22 import prompts
    globals_before={'systems':deepcopy(prompts._SYSTEMS),'reply':prompts.REPLY}
    for version in ['0.15.3','1.17.1']:
        for stage,context in [('analysis',ordinary('analysis')),('analysis',repairing('analysis')),
                ('reply',ordinary('reply')),('reply',repairing('reply'))]:
            before=deepcopy(context);_doc,_nodes,project,_decode=modes(stage,version)
            output=project(json.dumps(context))
            assert output['system_prompt']==prompts.select_system_prompt(context,stage)
            assert not complete_final_object_task(output['system_prompt'])
            assert context==before and {'systems':prompts._SYSTEMS,'reply':prompts.REPLY}==globals_before
