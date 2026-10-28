"""B protocol instructions through real generated Context and Contract."""
from copy import deepcopy
import json

import pytest

from server.negotiation.engine import Engine
from test_prompt_modes import generated as modes, ordinary, repairing
from test_reply_output_repair_projection import context_with_feedback
from test_verified_literal_fact_binding import cash_context, settlement, quantity_context


@pytest.mark.parametrize('role', ['ordinary', 'nonquote', 'OUTPUT'])
def test_actual_b_roles_require_nested_fields_permitted_slots_and_real_tool_completion(role):
    context = quantity_context() if role == 'ordinary' else cash_context() if role == 'nonquote' else context_with_feedback()
    context.update(schema_version='2.2', stage='reply')
    before = deepcopy(context)
    for version in ['0.15.3', '1.17.1']:
        _doc, _nodes, project, decode = modes('reply', version)
        system = project(json.dumps(context))['system_prompt']
        assert 'kind、question、quote_value、text都只在reply_plan内部' in system
        assert 'CLARIFY且question=ASK_SETTLEMENT' in system
        assert 'invalid_input不是已完成查询' in system
        assert '不含operation、mode或内部arguments字段' in system
        assert '后续成交、执行、锁量或占额度' in system
        if role != 'OUTPUT':
            assert 'TEXT_ONLY' not in system
        if role == 'nonquote':
            plan = settlement('请问结算日期是哪一天？')
        else:
            plan = {'kind': 'REPLY', 'quote_value': '3.15' if role == 'ordinary' else '1.55',
                    'question': 'NONE', 'text': '我方{own_quote}，您再看看？'}
        wire = {'schema_version': '2.2', 'reply_plan': plan}
        result = json.loads(decode(json.dumps(wire), json.dumps(context))['result'])
        assert Engine.__new__(Engine)._validate_reply(context, result['proposal'])['kind'] == plan['kind']
        for key in ['kind', 'question', 'text']:
            bad = deepcopy(wire)
            bad[key] = bad['reply_plan'][key]
            with pytest.raises(ValueError):
                decode(json.dumps(bad), json.dumps(context))
        bad = deepcopy(wire)
        bad['reply_plan']['context_ref'] = None
        with pytest.raises(ValueError):
            decode(json.dumps(bad), json.dumps(context))
        assert context == before


@pytest.mark.parametrize('boundary', [False, True])
def test_output_private_summary_pins_the_actual_candidate_except_boundary_repricing(boundary):
    from test_dify_output_contract_recovery import failed_candidate, REASONS
    context = context_with_feedback()
    if boundary:
        context['validation_feedback'].update(reason_code='DIFY_MODEL_CONTRACT_BOUNDARY',
                                             previous_output=failed_candidate(REASONS[1][0]))
    before = deepcopy(context)
    for version in ['0.15.3', '1.17.1']:
        _doc, _nodes, project, decode = modes('reply', version)
        system = project(json.dumps(context))['system_prompt']
        assert '必须省略reply_plan.context_ref整个字段' in system
        assert ('本次仅修文案的既定候选reply_plan.quote_value=' in system) == (not boundary)
        if not boundary:
            assert 'reply_plan.quote_value="1.55"' in system
        wire = {'schema_version': '2.2', 'reply_plan': {'kind': 'REPLY', 'quote_value': '1.55',
                 'question': 'NONE', 'text': '我方{own_quote}，您再看看？'}}
        result = json.loads(decode(json.dumps(wire), json.dumps(context))['result'])
        assert Engine.__new__(Engine)._validate_reply(context, result['proposal'])['our_price']['value'] == '1.55'
        if not boundary:
            clarify = {'schema_version': '2.2', 'reply_plan': {'kind': 'CLARIFY', 'question': 'NONE',
                       'text': '请确认您本人当前的报价？'}}
            conservative = json.loads(decode(json.dumps(clarify), json.dumps(context))['result'])
            assert conservative['proposal']['kind'] == 'CLARIFY'
            assert not conservative['proposal'].get('our_price')
        assert context == before


def test_a_and_text_only_do_not_inherit_the_new_b_protocol_notes():
    for version in ['0.15.3', '1.17.1']:
        for stage, context in [('analysis', ordinary('analysis')), ('analysis', repairing('analysis')),
                               ('reply', repairing('reply'))]:
            before = deepcopy(context)
            _doc, _nodes, project, _decode = modes(stage, version)
            assert '回复计划结构核对' not in project(json.dumps(context))['system_prompt']
            assert context == before
