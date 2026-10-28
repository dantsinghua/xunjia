"""No trusted requested lot means no new request-specific high-priority task."""
from copy import deepcopy
import json
import re

import pytest

from test_verified_literal_fact_binding import quantity_context
from test_requested_quantity_model_protocol import mode_context
from test_requested_quantity_offer import offer
from test_reply_output_repair_projection import generated
from server.negotiation.engine import Engine


def base_context():
    value=quantity_context();value.update(schema_version='2.2',stage='reply')
    assert value['quotes']['ofr']['assertion']=='QUOTE'
    return value


def quantity_instruction(system,mode):
    if mode=='ordinary':
        return next(line for line in system.splitlines() if '字面格式摘要' in line)
    return next(part for part in re.split(r'[；。\n]',system) if 'peer_volume' in part and '已含万' in part)


@pytest.mark.parametrize('mode',['ordinary','OUTPUT_CONTRACT'])
def test_ineligible_request_does_not_override_production_quantity_role_or_repair_shape(mode):
    context=mode_context(base_context(),mode);before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        project,decode=generated(version);result=project(json.dumps(context));system=result['system_prompt']
        directive=quantity_instruction(system,mode)
        assert '{peer_volume}' in directive
        assert '{requested_volume}' not in directive, 'Without an eligible source, the highest-priority quantity instruction must not offer the new request role'
        if mode=='OUTPUT_CONTRACT':
            valid={'schema_version':'2.2','reply_plan':offer('这边{own_quote}{peer_volume}，请再看看？')}
            for invalid_type in ['missing','top_level','numeric','object']:
                invalid=deepcopy(valid)
                if invalid_type=='missing':invalid['reply_plan'].pop('quote_value')
                elif invalid_type=='top_level':invalid['quote_value']=invalid['reply_plan'].pop('quote_value')
                elif invalid_type=='numeric':invalid['reply_plan']['quote_value']=3.15
                else:invalid['reply_plan']['quote_value']={'kind':'YIELD','value':'3.15'}
                with pytest.raises(ValueError):decode(json.dumps(invalid),json.dumps(context))
            accepted=json.loads(decode(json.dumps(valid),json.dumps(context))['result'])
            assert Engine.__new__(Engine)._validate_reply(context,accepted['proposal'])['our_price']['value']=='3.15'
        with pytest.raises(ValueError):
            decode(json.dumps({'schema_version':'2.2','reply_plan':offer('按您询问的{requested_volume}，这边{own_quote}。')}),json.dumps(context))
        assert context==before



@pytest.mark.parametrize('mode',['ordinary','OUTPUT_CONTRACT'])
def test_no_source_wrong_actor_and_old_source_keep_existing_code_roles_and_input_immutable(tmp_path,mode):
    from test_requested_quantity_model_protocol import request700_case
    _case,wrong_actor=request700_case(tmp_path/'wrong-actor.sqlite','我能3.10收700万吗')
    _case,current=request700_case(tmp_path/'old-source.sqlite')
    old=deepcopy(current);old['context'][-1]['trade_day']='2026-10-16'
    missing=deepcopy(current);missing['quotes']['bid']['evidence']['volume_wan']=[]
    for base in [base_context(),wrong_actor,old,missing]:
        context=mode_context(base,mode);before=deepcopy(context)
        for version in ['0.15.3','1.17.1']:
            project,decode=generated(version);output=project(json.dumps(context));system=output['system_prompt']
            with pytest.raises(ValueError):
                decode(json.dumps({'schema_version':'2.2','reply_plan':offer('按您询问的{requested_volume}，这边{own_quote}。')}),json.dumps(context))
            for text,amount in [('您报{peer_volume}，这边{own_quote}，请再看看？','500万'),
                    ('这边{own_quote}，请再看看？',None)]:
                decoded=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':offer(text)}),json.dumps(context))['result'])
                body=Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])
                assert body['our_price']['value']=='3.15'
                if amount is not None:assert amount in body['text']
                else:assert '万' not in body['text']
            if mode=='OUTPUT_CONTRACT':
                expected=deepcopy(context['validation_feedback']);del expected['previous_output']['reply_plan']['text']
                assert json.loads(output['context'])['validation_feedback']==expected
            assert context==before
            assert '{requested_volume}' not in quantity_instruction(system,mode)



@pytest.mark.parametrize('sequence',[(True,False,True),(False,True,False)])
def test_alternating_request_eligibility_keeps_each_namespace_and_policy_globals_immutable(tmp_path,sequence):
    from dify.negotiation_v22 import prompts
    from test_requested_quantity_model_protocol import request700_case,declares_binding
    _case,eligible=request700_case(tmp_path/'alternating-source.sqlite')
    ineligible=base_context()
    native_before={'systems':deepcopy(prompts._SYSTEMS),'reply':prompts.REPLY}
    for version in ['0.15.3','1.17.1']:
        project,_decode=generated(version)
        scope=project.__globals__
        generated_before={name:deepcopy(scope[name]) for name in ['_SYSTEMS','REPLY'] if name in scope}
        assert '_SYSTEMS' in generated_before
        for mode in ['ordinary','OUTPUT_CONTRACT']:
            results={}
            for qualification in sequence:
                context=mode_context(eligible if qualification else ineligible,mode)
                before=deepcopy(context)
                output=project(json.dumps(context));system=output['system_prompt']
                assert system==prompts.select_system_prompt(context,'reply')
                if qualification:
                    assert declares_binding(system,'{requested_volume}','700')
                    assert declares_binding(system,'{peer_volume}','500')
                else:
                    assert '{requested_volume}' not in quantity_instruction(system,mode)
                if qualification in results:assert output==results[qualification]
                results[qualification]=deepcopy(output)
                assert context==before
                assert {name:scope[name] for name in generated_before}==generated_before
                assert {'systems':prompts._SYSTEMS,'reply':prompts.REPLY}==native_before
