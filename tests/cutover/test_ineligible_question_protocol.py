"""A QUESTION amount is not itself permission to use requested_volume."""
from copy import deepcopy
import json
import re

import pytest

from test_requested_quantity_offer import asked_case,offer
from test_requested_quantity_model_protocol import request700_case,mode_context
from test_reply_output_repair_projection import generated
from server.negotiation.engine import Engine


def denial(system):
    return [line for line in system.splitlines() if '{requested_volume}' in line
        and any(word in line for word in ('本轮','当前','本次'))
        and re.search(r'未授予|不可用|不允许|不可引用|未通过.*核验',line)]


def ineligible(path,reason='direction'):
    if reason=='direction':
        _case,_source,saved,context,_=asked_case(path,'SYN-A 能3.10出500万吗？',side='ofr')
        assert saved['accepted'] and not saved['rejected']
        return context
    _case,context=request700_case(path,'我能3.10收700万吗' if reason=='self' else '你能3.10收700万吗')
    if reason=='old':context['context'][-1]['trade_day']='2026-10-16'
    elif reason=='missing':context['quotes']['bid']['evidence']['volume_wan']=[]
    return context


@pytest.mark.parametrize('reason',['direction','self','old','missing'])
@pytest.mark.parametrize('mode',['ordinary','OUTPUT_CONTRACT'])
def test_current_question_without_request_permission_has_explicit_unavailability_guidance(tmp_path,mode,reason):
    context=mode_context(ineligible(tmp_path/'ineligible.sqlite',reason),mode);before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        project,decode=generated(version);system=project(json.dumps(context))['system_prompt']
        lines=denial(system)
        assert lines, 'A source-validated QUESTION amount must explicitly state that requested_volume was not granted in this frame'
        assert any('QUESTION' in line and '裸' in line and '{peer_volume}' in line for line in lines)
        with pytest.raises(ValueError):
            decode(json.dumps({'schema_version':'2.2','reply_plan':offer('按您询问的{requested_volume}，这边{own_quote}。')}),json.dumps(context))
        question=next(q for q in context['quotes'].values() if q.get('assertion')=='QUESTION' and q.get('volume_wan') is not None)
        with pytest.raises(ValueError):
            decode(json.dumps({'schema_version':'2.2','reply_plan':offer('这边{own_quote}，'+question['volume_wan']+'万再看看？')}),json.dumps(context))
        own={'schema_version':'2.2','reply_plan':offer('这边{own_quote}，请再看看？')}
        body=Engine.__new__(Engine)._validate_reply(context,json.loads(decode(json.dumps(own),json.dumps(context))['result'])['proposal'])
        assert body['our_price']['value']=='3.15' and '万' not in body['text']
        peer={'schema_version':'2.2','reply_plan':offer('您报{peer_volume}，这边{own_quote}，请再看看？')}
        if context['quotes']['ofr'].get('assertion')=='QUOTE':
            body=Engine.__new__(Engine)._validate_reply(context,json.loads(decode(json.dumps(peer),json.dumps(context))['result'])['proposal'])
            assert '500万' in body['text'] and '700万' not in body['text']
        else:
            with pytest.raises(ValueError):decode(json.dumps(peer),json.dumps(context))
        assert context==before



def test_qualified700_and_inputs_without_question_quantity_do_not_receive_the_negative_task(tmp_path):
    from test_verified_literal_fact_binding import quantity_context
    _case,qualified=request700_case(tmp_path/'qualified.sqlite')
    ordinary=quantity_context();ordinary.update(schema_version='2.2',stage='reply')
    no_quantity=deepcopy(ordinary);no_quantity['quotes']['ofr'].update(assertion='QUESTION',volume_wan=None,confirmed_day=None)
    no_quantity['quotes']['ofr']['evidence']['volume_wan']=[]
    for version in ['0.15.3','1.17.1']:
        project,decode=generated(version)
        for base in [qualified,ordinary,no_quantity]:
            for mode in ['ordinary','OUTPUT_CONTRACT']:
                context=mode_context(base,mode);before=deepcopy(context)
                system=project(json.dumps(context))['system_prompt']
                assert not denial(system)
                if base is qualified:
                    good={'schema_version':'2.2','reply_plan':offer('按您询问的{requested_volume}，这边{own_quote}，请再看看？')}
                    body=Engine.__new__(Engine)._validate_reply(context,json.loads(decode(json.dumps(good),json.dumps(context))['result'])['proposal'])
                    assert '700万' in body['text']
                assert context==before


def test_analysis_and_text_only_keep_their_own_protocols_without_the_new_denial(tmp_path):
    from test_prompt_modes import ordinary,repairing,generated as modes
    from dify.negotiation_v22 import prompts
    for version in ['0.15.3','1.17.1']:
        for stage,context in [('analysis',ordinary('analysis')),('analysis',repairing('analysis')),('reply',repairing('reply'))]:
            before=deepcopy(context);_doc,_nodes,project,_decode=modes(stage,version)
            result=project(json.dumps(context))
            assert not denial(result['system_prompt'])
            if stage=='reply':assert result['system_prompt']==prompts.REPLY_REPAIR
            assert context==before
