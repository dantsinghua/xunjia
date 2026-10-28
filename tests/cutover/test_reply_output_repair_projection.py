"""Only the rejected draft text may leave the validated B OUTPUT_CONTRACT model view."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.engine import Engine
from test_dify_output_contract_recovery import valid_context, failed_candidate, successful_stage


def context_with_feedback():
    context = valid_context()
    context.update(schema_version='2.2', stage='reply')
    context['validation_feedback'] = {'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,
        'reason_code':'DIFY_MODEL_CONTRACT_SLOTS','previous_output':failed_candidate()}
    return context


def generated(version):
    nodes = {row['id']:row['data'] for row in build('reply',version=version,
        provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
    context, contract = {}, {}
    exec(nodes['context']['code'],context)
    exec(nodes['contract']['code'],contract)
    return context['main'], contract['main']


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_only_the_bad_slots_reply_draft_text_is_omitted_from_model_view_not_code_input(version):
    context = context_with_feedback()
    before = deepcopy(context)
    project, decode = generated(version)
    projected = json.loads(project(json.dumps(context))['context'])
    expected_feedback = deepcopy(context['validation_feedback'])
    del expected_feedback['previous_output']['reply_plan']['text']
    assert projected['validation_feedback'] == expected_feedback
    assert context == before
    original_feedback = deepcopy(context['validation_feedback'])
    without_feedback = deepcopy(context)
    without_feedback.pop('validation_feedback')
    projected_without = json.loads(project(json.dumps(without_feedback))['context'])
    actual_without = deepcopy(projected)
    actual_without.pop('validation_feedback')
    assert actual_without == projected_without, 'All original business context stays in the model view'
    good = {'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'1.55',
        'question':'NONE','text':'我再靠近些，{own_quote}，你再看看？'},'trader_view':{}}
    decoded = json.loads(decode(json.dumps(good),json.dumps(context))['result'])
    expected, _raw = successful_stage(context)
    assert decoded == expected
    assert Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])['our_price']['value']=='1.55'
    assert context == before and context['validation_feedback'] == original_feedback



def test_ordinary_and_text_only_views_keep_their_existing_feedback_contract():
    for version in ['0.15.3','1.17.1']:
        project, _decode = generated(version)
        ordinary = valid_context(); ordinary.update(schema_version='2.2',stage='reply')
        before = deepcopy(ordinary)
        assert 'validation_feedback' not in json.loads(project(json.dumps(ordinary))['context'])
        assert ordinary == before
        original, _raw = successful_stage(ordinary)
        ordinary['validation_feedback'] = {'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
            'reason':'Reply direction differs from our authorized side','original_response':original}
        before = deepcopy(ordinary)
        projected = json.loads(project(json.dumps(ordinary))['context'])
        assert projected['validation_feedback'] == ordinary['validation_feedback']
        assert ordinary == before


def test_all_b_output_contract_causes_omit_only_rejected_text_from_the_model_view():
    from test_dify_output_contract_recovery import REASONS
    for version in ['0.15.3','1.17.1']:
        project, _decode = generated(version)
        for code, candidate in [('DIFY_MODEL_CONTRACT_BOUNDARY',failed_candidate(REASONS[1][0])),
                ('DIFY_MODEL_CONTRACT_OWN_QUOTE',failed_candidate(REASONS[2][0])),
                ('DIFY_MODEL_CONTRACT_SLOTS',{'schema_version':'2.2','reply_plan':{
                    'kind':'REPLY','quote_value':'1.55','question':'NONE','text':'这边{own_quote}出，你再看看？'}})]:
            context = context_with_feedback()
            context['validation_feedback'].update(reason_code=code,previous_output=candidate)
            before = deepcopy(context)
            expected_feedback = deepcopy(context['validation_feedback'])
            del expected_feedback['previous_output']['reply_plan']['text']
            assert json.loads(project(json.dumps(context))['context'])['validation_feedback'] == expected_feedback
            assert context == before


def test_nonreply_and_cash_output_contract_views_omit_only_rejected_draft_text():
    from test_verified_literal_fact_binding import cash_context, settlement
    for version in ['0.15.3','1.17.1']:
        project, _decode = generated(version)
        nonreply = context_with_feedback()
        nonreply['validation_feedback']['previous_output'] = {'schema_version':'2.2','reply_plan':{
            'kind':'CLARIFY','question':'NONE','text':'请确认501万是否是当前整量？'}}
        cash = cash_context(); cash.update(schema_version='2.2',stage='reply')
        cash['validation_feedback'] = deepcopy(nonreply['validation_feedback'])
        cash['validation_feedback']['previous_output'] = {'schema_version':'2.2',
            'reply_plan':settlement('你刚才净价97.01出，结算日期是哪一天？')}
        for context in [nonreply,cash]:
            before = deepcopy(context)
            expected_feedback = deepcopy(context['validation_feedback'])
            del expected_feedback['previous_output']['reply_plan']['text']
            assert json.loads(project(json.dumps(context))['context'])['validation_feedback'] == expected_feedback
            assert context == before



def test_generated_user_footer_only_allows_source_authorized_peer_quantity():
    for version in ['0.15.3','1.17.1']:
        nodes = {row['id']:row['data'] for row in build('reply',version=version,
            provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
        user = next(row['text'] for row in nodes['model']['prompt_template'] if row['role']=='user')
        assert '{peer_volume}' in user and 'system' in user and '来源' in user
        assert 'QUESTION' in user and any(word in user for word in ('省略','不提','不可用','禁用'))
        from test_reply_source_permission_hints import context_for
        from test_verified_literal_fact_binding import reply
        project, decode = generated(version)
        context = context_for('QUESTION','ordinary'); before = deepcopy(context)
        assert project(json.dumps(context))['system_prompt']
        for text in ['这边{own_quote}，{peer_volume}你再看看？','这边{own_quote}，500万你再看看？']:
            with pytest.raises(ValueError):
                decode(json.dumps({'schema_version':'2.2','reply_plan':reply(text)}),json.dumps(context))
        safe = {'schema_version':'2.2','reply_plan':reply('这边{own_quote}，你再看看？')}
        decoded = json.loads(decode(json.dumps(safe),json.dumps(context))['result'])
        body = Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])
        assert body['our_price']['value']=='3.15' and '500' not in body['text']
        assert context == before


def test_output_contract_trader_view_is_top_level_only_and_nested_null_is_still_rejected():
    import re
    for version in ['0.15.3','1.17.1']:
        project, decode = generated(version)
        context = context_with_feedback(); before = deepcopy(context)
        system = project(json.dumps(context))['system_prompt']
        assert 'trader_view' in system and any(word in system for word in ('最外层','顶层'))
        assert re.search(r'(?:不能|不得|禁止|不要|勿).{0,25}reply_plan|reply_plan.{0,25}(?:不能|不得|禁止|不要|勿)',system)
        valid = {'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'1.55',
            'question':'NONE','text':'我再靠近些，{own_quote}，你再看看？'},'trader_view':{}}
        invalid = deepcopy(valid);invalid['reply_plan']['trader_view']=None
        with pytest.raises(ValueError):decode(json.dumps(invalid),json.dumps(context))
        decoded = json.loads(decode(json.dumps(valid),json.dumps(context))['result'])
        expected, _raw = successful_stage(context)
        assert decoded == expected and context == before


def test_analysis_and_evidence_only_generated_views_keep_their_original_feedback():
    from test_prompt_modes import ordinary, repairing, generated as generated_modes
    for version in ['0.15.3','1.17.1']:
        for context in [ordinary('analysis'),repairing('analysis')]:
            before = deepcopy(context)
            _doc, _nodes, project, _decode = generated_modes('analysis',version)
            projected = json.loads(project(json.dumps(context))['context'])
            assert projected.get('validation_feedback') == context.get('validation_feedback')
            assert context == before



def test_unknown_peer_volume_does_not_disable_a_separately_verified_current_peer_price():
    from test_verified_literal_fact_binding import quantity_context, reply
    for version in ['0.15.3','1.17.1']:
        context = quantity_context(); context.update(schema_version='2.2',stage='reply')
        context['quotes']['ofr']['volume_wan'] = None
        context['quotes']['ofr']['evidence']['volume_wan'] = []
        context['context'][0]['text'] = 'SYN-A 3.10出'
        before = deepcopy(context)
        nodes = {row['id']:row['data'] for row in build('reply',version=version,
            provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
        user = next(row['text'] for row in nodes['model']['prompt_template'] if row['role']=='user')
        assert any(word in user for word in ('未知量','量未知','无量证据'))
        assert 'peer_quote' in user and any(word in user for word in ('独立','不影响','仍可','仍按')), 'Unknown volume must not revoke a separately authorized peer price in the generated guidance'
        project, decode = generated(version)
        assert project(json.dumps(context))['system_prompt']
        good = {'schema_version':'2.2','reply_plan':reply('您报{peer_quote}，这边{own_quote}，请问整量多少？')}
        decoded = json.loads(decode(json.dumps(good),json.dumps(context))['result'])
        body = Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])
        assert '3.10%出' in body['text'] and body['our_price']['value']=='3.15'
        bad = {'schema_version':'2.2','reply_plan':reply('您报{peer_quote}{peer_volume}，这边{own_quote}。')}
        with pytest.raises(ValueError):decode(json.dumps(bad),json.dumps(context))
        assert context == before
