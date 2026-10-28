"""Model-only short references must return to the unchanged canonical domain."""
from copy import deepcopy
import json
import pytest

from dify.negotiation_v22.build import build
from server.negotiation.slots import collect_candidates
from test_reference_window_policy import OWNER, Desk, window, no_upward_query


def generated(version='1.17.1', stage='analysis'):
    graph = build(stage, version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    nodes = {row['id']:row['data'] for row in graph['nodes']}
    projection, contract = {}, {}
    exec(nodes['context']['code'],projection); exec(nodes['contract']['code'],contract)
    return projection['main'], contract['main']


def scene(tmp_path):
    case = Desk(tmp_path/'analysis-reference.sqlite')
    source = case.inbound('SYN-A 3.10出500万，还可以谈')
    job = case.store.claim_analysis(OWNER, case.thread, force=True)
    context = {**job['snapshot'],'schema_version':'2.2','stage':'analysis',
               'context_policy_revision':'latest_quote_confirmation_v1'}
    return case, source, job, context


def model_for(project, context):
    projected = json.loads(project(json.dumps(context,ensure_ascii=False))['context'])
    message = projected['messages'][0]['message_id']
    member = projected['members'][0]['member_id']
    candidates = collect_candidates(context)
    price = next(row['id'] for row in candidates if row['roles']==['YIELD'])
    volume = next(row['id'] for row in candidates if row['roles']==['QUANTITY'])
    model = {'schema_version':'2.2',
        'scopes':{message:[member]},'observations':[{'member_id':member,'side':'ofr','source_ids':[message],
            'slots':{'price':price,'volume_wan':volume},'meaning':{'intent':'QUOTE','validity':'ACTIVE',
                'negotiation_space':'NEGOTIABLE','reason':'本人明确当前报价及整量'}}]}
    return projected, model


def run_decode(decode, model, context):
    return json.loads(decode(json.dumps(model,ensure_ascii=False),json.dumps(context,ensure_ascii=False))['result'])


@pytest.mark.parametrize('version', ['0.15.3','1.17.1'])
def test_short_snapshot_references_round_trip_through_real_code_to_true_ids_and_facts(tmp_path, version):
    case, source, job, context = scene(tmp_path)
    before = deepcopy(context)
    project, decode = generated(version)
    projected, model = model_for(project, context)
    message = projected['messages'][0]['message_id']
    member = projected['members'][0]['member_id']
    assert len(message) < len(source) and len(member) < len(case.member), 'Model references must be short metadata handles'
    decoded = run_decode(decode, model, context)
    assert decoded['scopes'] == {source:[case.member]}
    assert decoded['observations'][0]['member_id'] == case.member
    scopes, observations, unresolved = case.engine._normalize(context,decoded)
    saved = case.store.commit_analysis(OWNER,job['analysis_id'],job['lease_token'],
        scopes=scopes,observations=observations,unresolved=unresolved)
    assert saved['facts_committed'] and saved['accepted'] and not saved['rejected']
    assert case.quote()['price']['value']=='3.10' and case.quote()['volume_wan']=='500'
    assert context == before


@pytest.mark.parametrize('failure', ['unknown_message','old_snapshot','mixed_kind','wrong_uuid',
                                     'duplicate_message','duplicate_member','missing_scope'])
def test_bad_references_never_gain_scope_or_silent_correction(tmp_path, failure):
    case, source, _job, context = scene(tmp_path)
    project, decode = generated()
    projected, model = model_for(project, context)
    message = projected['messages'][0]['message_id']
    member = projected['members'][0]['member_id']
    if failure == 'unknown_message': model['scopes'] = {message+'999':[member]}
    elif failure == 'old_snapshot': context['through_revision'] = context.get('through_revision',0)+1
    elif failure == 'mixed_kind': model['scopes'] = {member:[message]}
    elif failure == 'wrong_uuid': model['scopes'] = {source+'8':[case.member]}
    elif failure == 'duplicate_message': model['scopes'][source] = [case.member]
    elif failure == 'duplicate_member': model['scopes'][message].append(case.member)
    else: model['scopes'] = {}
    before = deepcopy((context,model))
    with pytest.raises(ValueError): run_decode(decode,model,context)
    assert (context,model) == before


def test_original_canonical_domain_still_uses_the_same_strict_normalization(tmp_path):
    case, source, _job, context = scene(tmp_path)
    project, decode = generated()
    _view, model = model_for(project,context)
    model['scopes'] = {source:[case.member]}
    model['observations'][0].update(member_id=case.member,source_ids=[source])
    result = run_decode(decode,model,context)
    assert result['scopes'] == {source:[case.member]}
    assert not result['observations'][0].get('validation_error')
    assert result['observations'][0]['changes']['price']['value'] == '3.10'


def test_projection_preserves_raw_history_order_candidates_and_original_reference_alignment(tmp_path):
    case = Desk(tmp_path/'history.sqlite')
    anchor = case.inbound('SYN-A 3.10出500万')
    case.analyze([case.observation(anchor,price={'kind':'YIELD','value':'3.10'},
        volume_wan='500',assertion='QUOTE',validity='ACTIVE',confirmed_day='2026-10-17')])
    for index in range(19): case.inbound('SYN-A 稍后交流 '+str(index))
    assert case.analyze([])['facts_committed']
    source = case.inbound('SYN-A 参考 '+anchor+'，3.10出500万')
    job = case.store.claim_analysis(OWNER,case.thread,force=True)
    context = {**job['snapshot'],'schema_version':'2.2','stage':'analysis',
        'context_policy_revision':'latest_quote_confirmation_v1'}
    before = deepcopy(context)
    project, _decode = generated()
    view = json.loads(project(json.dumps(context,ensure_ascii=False))['context'])
    old_rows, new_rows = context['members'][0]['context'], view['members'][0]['context']
    assert len(old_rows) == len(new_rows) == 20
    assert [r['text'] for r in old_rows] == [r['text'] for r in new_rows]
    assert view['messages'][0]['text'] == context['messages'][0]['text']
    assert source not in {r['message_id'] for r in new_rows}
    aligned = [r for r in new_rows if r.get('original_reference_id') == anchor]
    assert len(aligned) == 1, 'An ID quoted in intact raw text must remain alignable'
    assert view['members'][0]['quotes'] == context['members'][0]['quotes']
    original_candidates = collect_candidates(context)
    assert {r['id'] for r in view['slot_candidates']} <= {r['id'] for r in original_candidates}
    current_ids = {r['id'] for r in original_candidates if r['message_id']==source}
    assert current_ids <= {r['id'] for r in view['slot_candidates']}
    assert context == before


def test_public_tool_arguments_are_never_reference_decoded(tmp_path):
    _case, _source, _job, context = scene(tmp_path)
    project, decode = generated()
    view, _model = model_for(project,context)
    arguments = {'bond_id':view['messages'][0]['message_id'],'query':view['members'][0]['member_id']}
    model = {'schema_version':'2.2','tool_requests':[{'name':'bond_info','arguments':arguments}]}
    result = run_decode(decode,model,context)
    assert result['tool_requests'] == model['tool_requests']


def test_evidence_only_keeps_its_frozen_canonical_response_and_patch_references():
    from test_model_wire_adapter import feedback_context
    context, original, patch = feedback_context()
    project, decode = generated()
    before = deepcopy(context)
    projected = json.loads(project(json.dumps(context))['context'])
    assert projected['validation_feedback'] == before['validation_feedback']
    assert projected['messages'] == before['messages']
    result = run_decode(decode,patch,context)
    assert result['scopes'] == original['scopes']
    assert result['observations'][0]['evidence']['price'][-1] == 'old'
    assert context == before
    from test_verified_literal_fact_binding import quantity_context, reply
    from server.negotiation.reply_render import render_reply_plan
    for feedback in [None, {'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
        'reason':'Reply direction differs from our authorized side'}]:
        context = quantity_context(); context.update(schema_version='2.2',stage='reply')
        if feedback:
            original = {'schema_version':'2.2','action':'SEND',
                'proposal':render_reply_plan(context,reply('{own_quote}，你看看？'))['proposal']}
            context['validation_feedback'] = {**feedback,'original_response':original}
        project, decode = generated(stage='reply')
        view = json.loads(project(json.dumps(context))['context'])
        assert 'reference_namespace' not in view
        if feedback:
            assert view['validation_feedback'] == context['validation_feedback']
        else:
            output = run_decode(decode,{'schema_version':'2.2','reply_plan':reply('{own_quote}，你看看？')},context)
            assert output['proposal']['render_plan']['text'] == '{own_quote}，你看看？'


def test_system_confirmation_uses_the_same_handles_and_keeps_missing_history_unloaded(window, monkeypatch):
    case = window
    original_quote = case.quote()
    source = case.inbound('SYN-A 按消息 '+case.anchor+' 的报价出500万')
    project, decode = generated()
    calls = []
    def model(snapshot):
        context = {**snapshot,'schema_version':'2.2','stage':'analysis',
            'context_policy_revision':'latest_quote_confirmation_v1'}
        projected = project(json.dumps(context,ensure_ascii=False))
        view = json.loads(projected['context'])
        candidate = json.loads(projected['system_prompt'].splitlines()[-1])
        assert candidate['scopes'] == {view['messages'][0]['message_id']:[view['members'][0]['member_id']]}
        calls.append(deepcopy(context))
        return run_decode(decode,candidate,context)
    case.flows.analyze = model
    upward = no_upward_query(case,monkeypatch)
    result = case.engine.analyze_thread(OWNER,case.thread,force=True)
    assert len(calls) == 1 and result['facts_committed'] and not result['accepted']
    assert any(row['reason']=='REFERENCE_CONFIRMATION_REQUIRED' for row in result['rejected'])
    assert case.quote() == original_quote and not upward
    assert source in {r['message_id'] for r in calls[0]['messages']}


def test_precise_formatter_error_is_one_slots_correction_not_a_price_or_lot_relaxation(monkeypatch):
    from test_verified_literal_fact_binding import quantity_context, reply
    from test_dify_output_contract_recovery import (
        make_client, http_error, generated_error_carrier, structured_payload, sent_context)
    from server.negotiation.reply_render import render_reply_plan
    from server.negotiation.chatflow import FlowUnavailable
    from server.negotiation.engine import Engine
    context = quantity_context(); context.update(schema_version='2.2',stage='reply')
    bad = {'schema_version':'2.2','reply_plan':reply('{own_quote}出，你再看看？')}
    carrier = generated_error_carrier(context,bad)
    assert structured_payload(carrier)['reason_code'] == 'DIFY_MODEL_CONTRACT_SLOTS'
    assert structured_payload(carrier)['previous_output'] == bad
    good = {'schema_version':'2.2','reply_plan':reply('{own_quote}，你再看看？')}
    expected = {'schema_version':'2.2','action':'SEND',
        'proposal':render_reply_plan(context,good['reply_plan'])['proposal']}
    raw = json.dumps({'answer':json.dumps(expected)}).encode()
    client, opener, _clock = make_client(monkeypatch,[(http_error(carrier),.1),(raw,.1)])
    result = client.call(context)
    assert len(opener.calls) == 2 and result == expected
    assert sent_context(opener.calls[1])['validation_feedback']['previous_output'] == bad
    assert result['proposal']['our_price'] == expected['proposal']['our_price']
    assert Engine.__new__(Engine)._validate_reply(context,result['proposal'])['our_price']['value']=='3.15'
    client, opener, _clock = make_client(monkeypatch,[(http_error(carrier),.1),(http_error(carrier),.1)])
    with pytest.raises(FlowUnavailable): client.call(context)
    assert len(opener.calls) == 2
    with pytest.raises(ValueError):
        render_reply_plan(context,{**good['reply_plan'],'quote_value':'3.11'})
