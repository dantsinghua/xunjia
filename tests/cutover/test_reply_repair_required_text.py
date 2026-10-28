"""A hidden rejected draft does not make the new final reply text optional."""
from copy import deepcopy
import json

import pytest

from server.negotiation.engine import Engine
from test_reply_output_repair_projection import context_with_feedback, generated
from test_dify_output_contract_recovery import successful_stage


def test_output_contract_requires_fresh_nonempty_final_text_but_retains_full_private_input():
    for version in ['0.15.3','1.17.1']:
        context=context_with_feedback();before=deepcopy(context)
        project,decode=generated(version)
        result=project(json.dumps(context));system=result['system_prompt']
        assert any('reply_plan' in line and 'text' in line and '非空' in line
            and any(word in line for word in ('必须','必填'))
            and any(word in line for word in ('新生成','重新生成','重写','重新组织'))
            for line in system.splitlines()), 'Final reply_plan must explicitly require newly generated nonempty text'
        feedback=deepcopy(before['validation_feedback']);del feedback['previous_output']['reply_plan']['text']
        assert json.loads(result['context'])['validation_feedback']==feedback
        missing={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'1.55','question':'NONE'}}
        with pytest.raises(ValueError,match='Natural reply text must contain'):
            decode(json.dumps(missing),json.dumps(context))
        valid=deepcopy(missing);valid['trader_view']={};valid['reply_plan']['text']='我再靠近些，{own_quote}，你再看看？'
        answer=json.loads(decode(json.dumps(valid),json.dumps(context))['result'])
        expected,_raw=successful_stage(context)
        assert answer==expected
        assert Engine.__new__(Engine)._validate_reply(context,answer['proposal'])['our_price']['value']=='1.55'
        assert context==before and context['validation_feedback']['previous_output']['reply_plan']['text']



def test_required_final_text_does_not_remove_tools_wait_handoff_or_change_other_prompt_roles():
    from test_prompt_modes import ordinary, repairing, generated as generated_mode
    from dify.negotiation_v22 import prompts
    for version in ['0.15.3','1.17.1']:
        context=context_with_feedback();before=deepcopy(context)
        project,decode=generated(version)
        output=project(json.dumps(context));system=output['system_prompt']
        assert all(token in system for token in ['requests-only','WAIT','HANDOFF'])
        request={'schema_version':'2.2','tool_requests':[{'name':'issuer_info',
            'arguments':{'schema_version':'1','mode':'get','issuer_id':'SYN-ISSUER'}}]}
        assert json.loads(decode(json.dumps(request),json.dumps(context))['result'])==request
        for action in ['WAIT','HANDOFF']:
            result=json.loads(decode(json.dumps({'schema_version':'2.2','action':action}),json.dumps(context))['result'])
            assert result['action']==action and not result.get('proposal')
        assert context==before
        for stage,other in [('analysis',ordinary('analysis')),('analysis',repairing('analysis')),
                ('reply',ordinary('reply')),('reply',repairing('reply'))]:
            prior=deepcopy(other)
            _doc,_nodes,select,_contract=generated_mode(stage,version)
            selected=select(json.dumps(other))
            if stage=='reply' and other.get('validation_feedback',{}).get('mode')=='TEXT_ONLY':
                assert selected['system_prompt']==prompts.REPLY_REPAIR
            elif stage=='reply':
                native=prompts.select_system_prompt(other,'reply')
                assert selected['system_prompt'].startswith(native)
                assert all(anchor in selected['system_prompt'] for anchor in
                    ('你负责B谈价与自然聊天','reply_plan','{own_quote}'))
            else:
                assert 'OUTPUT_CONTRACT' not in selected['system_prompt']
            assert other==prior
