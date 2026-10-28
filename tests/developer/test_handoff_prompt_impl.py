"""Exercise the actual selected and embedded DSL prompts for quote acceptance."""
import json

import pytest

from dify.negotiation_v22.build import build
from dify.negotiation_v22.prompts import select_system_prompt
from tests.developer.test_handoff_notice_impl import pure_context, plan


def context():
    return {**pure_context(), 'schema_version': '2.2', 'stage': 'reply',
            'context_policy_revision': 'latest_quote_confirmation_v1'}


def assert_acceptance_prompt(system):
    assert 'HANDOFF_NOTICE' in system
    assert 'ACCEPTED_CURRENT_QUOTE' in system
    assert 'LATEST_INBOUND' in system
    assert '优先CLARIFY/NONE自然询问对方本人当前报价及整量是否为最终可做报价' not in system


def test_accepted_current_quote_prompt_does_not_require_final_or_full_target():
    source = context()
    assert source['group_summary']['goal_ready'] is False
    assert source['quotes']['bid']['finality'] == 'UNKNOWN'
    assert_acceptance_prompt(select_system_prompt(source, 'reply'))


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_embedded_dsl_selects_acceptance_and_decodes_notice_with_original_facts(version):
    document = build('reply', version=version, provider='synthetic', model='synthetic')
    nodes = {node['id']: node['data'] for node in document['workflow']['graph']['nodes']}
    projection = {}
    exec(compile(nodes['context']['code'], '<embedded-context>', 'exec'), projection)
    source = context()
    projected = projection['main'](json.dumps(source))
    assert_acceptance_prompt(projected['system_prompt'])
    public = json.loads(projected['context'])
    assert 'handoff_evidence' not in public
    contract = {}
    exec(compile(nodes['contract']['code'], '<embedded-contract>', 'exec'), contract)
    result = json.loads(contract['main'](
        json.dumps({'schema_version': '2.2', 'reply_plan': plan()}), json.dumps(source))['result'])
    assert result['action'] == 'SEND'
    assert result['proposal']['kind'] == 'HANDOFF_NOTICE'
    assert 'our_price' not in result['proposal']
