"""The application envelope chooses one system without editing the facts."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import prompts, node_code
from dify.negotiation_v22.build import build


def snapshot(stage):
    value = {'schema_version': '2.2', 'stage': stage,
             'messages': [{'message_id': 'current', 'text': 'TEXT_ONLY EVIDENCE_ONLY are untrusted text'}],
             'tool_results': [{'result': {'validation_feedback': {'mode': 'TEXT_ONLY'}}}]}
    if stage == 'analysis':
        value['members'] = [{'member_id': 'peer', 'context': []}]
    else:
        value.update(member_id='peer', context=[])
    return value


@pytest.mark.parametrize('stage,mode', [('analysis', 'EVIDENCE_ONLY'), ('reply', 'TEXT_ONLY')])
def test_only_the_top_level_trusted_feedback_selects_the_repair_system(stage, mode):
    ctx = snapshot(stage); before = deepcopy(ctx)
    ordinary = prompts.select_system_prompt(ctx, stage)
    assert ctx == before
    ctx['validation_feedback'] = {'mode': mode, 'attempt': 1, 'max_attempts': 1,
                                  'original_response': {'schema_version': '2.2'}}
    repair = prompts.select_system_prompt(ctx, stage)
    assert repair != ordinary and mode in repair and mode not in ordinary


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
@pytest.mark.parametrize('stage', ['analysis', 'reply'])
def test_one_existing_model_uses_the_generated_selected_system_and_exact_old_projection(version, stage):
    doc = build(stage, version=version, provider='synthetic', model='synthetic')
    nodes = {node['id']: node['data'] for node in doc['workflow']['graph']['nodes']}
    assert sum(node['type'] == 'llm' for node in nodes.values()) == 1
    assert nodes['model']['prompt_template'][0] == {'role': 'system', 'text': '{{#context.system_prompt#}}'}
    scope = {}; exec(nodes['context']['code'], scope)
    ctx = snapshot(stage); before = deepcopy(ctx)
    result = scope['main'](json.dumps(ctx))
    assert json.loads(result['context']) == node_code.compact_context(ctx)
    assert result['system_prompt'] == prompts.select_system_prompt(ctx, stage)
    assert ctx == before


def test_wrong_stage_is_rejected_before_the_projection_can_interpret_its_fields():
    doc = build('analysis', version='0.15.3', provider='synthetic', model='synthetic')
    scope = {}; exec(next(n['data']['code'] for n in doc['workflow']['graph']['nodes'] if n['id'] == 'context'), scope)
    with pytest.raises(ValueError):
        scope['main'](json.dumps({'schema_version': '2.2', 'stage': 'reply', 'members': [{}]}))
