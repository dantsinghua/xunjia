"""Developer contracts for the current A/B flow, independent of legacy canvas tests."""
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    path = ROOT / 'dify' / 'negotiation_v22' / (name + '.py')
    assert path.exists(), 'V2.2 two-stage implementation is missing'
    spec = importlib.util.spec_from_file_location('dify.negotiation_v22.' + name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def context():
    return {'schema_version':'2.2', 'stage':'analysis', 'messages':[{'message_id':'m1','text':'SYN-A 2.20出5000万','direction':'in'}],
            'members':[{'member_id':'p1','bond_code':'SYN-A','context':[]}]}


def analysis():
    return {'schema_version':'2.2','scopes':{'m1':['p1']},'unresolved':{},'tool_requests':[],
            'observations':[{'member_id':'p1','side':'ofr','changes':{'price':{'kind':'YIELD','value':'2.20'},'volume_wan':'5000'},
                             'evidence':{'price':['m1'],'volume_wan':['m1']},'segments':[{'message_id':'m1','text':'SYN-A 2.20出5000万'}],
                             'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUOTE','negotiation_space':'UNKNOWN','reason':'本轮明确报出价及量'}}]}


@pytest.mark.parametrize('version,dsl',[('0.15.3','0.1.5'),('1.17.1','0.7.0')])
@pytest.mark.parametrize('stage',['analysis','reply'])
def test_current_dsl_uses_exact_client_input_and_independent_stage_output(version,dsl,stage):
    doc=module('build').build(stage,version=version,provider='test/provider',model='test-model')
    assert doc['version']==dsl
    assert doc['workflow']['conversation_variables']==[]
    assert doc['workflow']['environment_variables']==[]
    nodes={n['id']:n['data'] for n in doc['workflow']['graph']['nodes']}
    assert [v['variable'] for v in nodes['start']['variables']]==['context_json']
    assert {x['type'] for x in nodes.values()}=={'start','llm','code','answer'}
    assert not any(x['type'] in ('agent','http-request') for x in nodes.values())
    assert 'memory' not in nodes['model']
    assert nodes['answer']['answer']=='{{#contract.result#}}'
    namespace={};exec(nodes['contract']['code'],namespace)
    sample=analysis() if stage=='analysis' else {'schema_version':'2.2','action':'WAIT','proposal':None,'tool_requests':[]}
    ctx=context() if stage=='analysis' else {'schema_version':'2.2','stage':'reply','member_id':'p1'}
    assert json.loads(namespace['main'](json.dumps(sample),json.dumps(ctx))['result'])==sample


def test_valid_financial_analysis_is_preserved_including_independent_sides():
    result=module('node_code').validate_result(analysis(),context(),'analysis')
    assert result['observations'][0]['changes']['volume_wan']=='5000'
    assert result['observations'][0]['changes']['price']['value']=='2.20'


@pytest.mark.parametrize('field',['member_id','evidence','segments','interpretation'])
def test_unverifiable_analysis_keeps_explicit_rejection_slot_not_empty_success(field):
    sample=analysis();sample['observations'][0].pop(field)
    result=module('node_code').validate_result(sample,context(),'analysis')
    assert len(result['observations'])==1
    assert result['observations'][0]['validation_error']


def test_evidence_must_reference_the_actual_supplied_context():
    sample=analysis();sample['observations'][0]['evidence']['price']=['invented']
    result=module('node_code').validate_result(sample,context(),'analysis')
    assert len(result['observations'])==1
    assert result['observations'][0]['validation_error']=='Unknown field evidence'


def test_malformed_observation_does_not_erase_valid_other_bond_and_indices():
    sample=analysis();good=sample['observations'][0]
    sample['observations']=[good,{'member_id':'invented'},'bad item']
    result=module('node_code').validate_result(sample,context(),'analysis')
    assert len(result['observations'])==3
    assert result['observations'][0]==good
    assert all(set(row)=={'validation_error'} for row in result['observations'][1:])


def test_withdraw_intent_cannot_be_used_as_quote_assertion():
    sample=analysis();row=sample['observations'][0]
    row['changes']={'assertion':'WITHDRAW','validity':'WITHDRAWN'}
    row['evidence']={'assertion':['m1'],'validity':['m1']}
    row['interpretation']['intent']='WITHDRAW'
    result=module('node_code').validate_result(sample,context(),'analysis')
    assert result['observations'][0].get('validation_error')
    row['changes']={'validity':'WITHDRAWN'};row['evidence']={'validity':['m1']}
    assert module('node_code').validate_result(sample,context(),'analysis')['observations'][0]==row


def test_ambiguous_scope_can_coexist_with_other_valid_observations():
    sample=analysis();ctx=context();ctx['members'].append({'member_id':'p2','bond_code':'SYN-B','context':[]})
    sample['unresolved']={'m1':['p2']}
    assert module('node_code').validate_result(sample,ctx,'analysis')['observations']==sample['observations']


def test_correction_requires_supplied_anchor_and_current_inbound_clarification():
    sample=analysis();ctx=context()
    ctx['members'][0]['context']=[{'message_id':'old','text':'2.20出5000万','direction':'in'}]
    sample['corrections']=[{'message_id':'old','member_ids':['p1'],'evidence':['m1']}]
    assert module('node_code').validate_result(sample,ctx,'analysis')['corrections']==sample['corrections']
    sample['corrections'][0]['evidence']=['old']
    with pytest.raises(ValueError):module('node_code').validate_result(sample,ctx,'analysis')


def test_tools_require_no_token_and_reject_private_callbacks():
    sample={'schema_version':'2.2','tool_requests':[{'name':'bond_info','arguments':{'query':'SYN-A'}}]}
    assert module('node_code').validate_result(sample,context(),'analysis')==sample
    sample['tool_requests'][0]['name']='context'
    with pytest.raises(ValueError):module('node_code').validate_result(sample,context(),'analysis')


def test_shipped_calculator_example_is_accepted_by_the_actual_public_tool():
    from server.bond_tools import BondTools
    example=module('prompts').CALC_EXAMPLE
    assert example['arguments']['inputs']['our_side']=='BUY'
    assert example['arguments']['inputs']['peer_side']=='ofr'
    result=BondTools().invoke(example['name'],example['arguments'])
    assert result['status']=='ok',result
    assert result['data']['result']['raw_gap_bp']=='10.00'


def test_history_evidence_id_is_rejected_if_inserted_as_a_current_scope_key():
    sample=analysis();ctx=context()
    ctx['members'][0]['context']=[{'message_id':'history','text':'3.20%收','direction':'out'}]
    sample['scopes']['history']=['p1']
    with pytest.raises(ValueError,match='Every raw message'):module('node_code').validate_result(sample,ctx,'analysis')


def test_send_needs_structured_quote_and_wait_cannot_smuggle_public_text():
    validate=module('node_code').validate_result
    good={'schema_version':'2.2','action':'SEND','proposal':{'kind':'REPLY','text':'SYN-A 2.40%收','our_price':{'kind':'YIELD','value':'2.40'}}}
    assert validate(good,{'schema_version':'2.2','stage':'reply'},'reply')==good
    good['action']='WAIT'
    with pytest.raises(ValueError):validate(good,{'schema_version':'2.2','stage':'reply'},'reply')


def test_known_dify_provider_reasoning_wrapper_is_not_parsed_as_business_json():
    parse=module('node_code').parse_object
    wrapper='<details style="color:gray;background-color: #f8f8f8;padding: 8px;border-radius: 4px;" open> <summary> Thinking... </summary>internal reasoning</details>'
    assert parse(wrapper+json.dumps(analysis()))==analysis()
    with pytest.raises(ValueError):parse('explanation '+json.dumps(analysis()))
    with pytest.raises(ValueError):parse(wrapper+json.dumps(analysis())+' extra text')
    with pytest.raises(ValueError):parse(wrapper.replace('</details>','')+json.dumps(analysis()))


def clean_statistics_sample(index, *, elapsed, path='common'):
    return {'case_id':'synthetic-stats-'+str(index),'success':True,'semantic_ok':True,
            'path':path,'elapsed_ms':elapsed,'timeout':False,
            'analysis_repair':{'attempted':False,'initial_errors':[]},
            'reply_repair':{'attempted':False,'initial_errors':[]},
            'analysis_inputs':[{}],'reply_inputs':[{}],
            'evidence':[{'stage':stage,'round':1,'round_kind':'initial','elapsed_ms':elapsed/2,
                         'message_id':'synthetic-'+stage+'-'+str(index)} for stage in ('analysis','reply')]}


def test_measurement_never_uses_failure_latency_or_unmeasured_window():
    stats=module('measurement')
    samples=[clean_statistics_sample(i,elapsed=800+i) for i in range(30)]
    samples.append({'success':False,'semantic_ok':False,'path':'tools','elapsed_ms':90000,'timeout':True})
    report=stats.summarize(samples,measurement_id='synthetic-stats-test',source={'kind':'test'})
    assert report['successes']==30 and report['failures']==1 and report['timeouts']==1
    assert report['reference_ms']==814.5
    assert 0<=report['aggregation']['quiet_ms']<=report['aggregation']['max_wait_ms']<814.5/2
    assert report['overall']['p95_ms']<900
    with pytest.raises(ValueError):stats.summarize(samples[:2],measurement_id='short',source={'kind':'test'})


def test_short_actual_history_fast_path_limits_window_even_when_full_context_is_slower():
    stats=module('measurement')
    samples=[clean_statistics_sample(i,elapsed=10000) for i in range(30)]
    samples += [clean_statistics_sample('fast-'+str(i),elapsed=800,path='fast_common') for i in range(6)]
    report=stats.summarize(samples,measurement_id='synthetic-short-history-test',source={'kind':'test'})
    assert report['reference_ms']==800
    assert report['ordinary_fastest_path']['path']=='fast_common'
    assert report['aggregation']['max_wait_ms']==399
    assert report['overall']['mean_ms']>800


def test_ordinary_latency_reference_uses_one_bond_twenty_actual_context_messages():
    fixture=module('cases')
    ordinary=fixture.snapshot(fixture.cases()[0])
    assert len(ordinary['members'])==1
    assert len(ordinary['members'][0]['context'])==20
    multi=fixture.snapshot(next(c for c in fixture.cases() if c['id']=='multi-bond'))
    assert len(multi['members'])==2
    assert all(len(m['context'])==20 for m in multi['members'])


def test_semantic_measurement_compares_decimal_amount_not_storage_spelling():
    from scripts.measure_dify_v22 import check_expected
    case={'expected':[{'member':'a','side':'ofr','volume':'5000'}]}
    observation={'member_id':'a','side':'ofr','changes':{'volume_wan':'5000.0'}}
    check_expected(case,[observation])
    observation['changes']['volume_wan']='500.0'
    with pytest.raises(ValueError):check_expected(case,[observation])


@pytest.mark.parametrize('malformed',[False,True])
def test_measurement_counts_actual_engine_fact_rejection_as_failure(monkeypatch,malformed):
    from scripts import measure_dify_v22 as measurement
    class LocalFixtureClient:
        def __init__(self,*args,stage,**kwargs):self.stage=stage;self.last_evidence={'stage':stage,'elapsed_ms':0,'source':'developer-fixture'}
        def call(self,ctx):
            if self.stage=='reply':return {'schema_version':'2.2','action':'SEND','proposal':{'kind':'REPLY','text':'SYN-A 3.18%收','our_price':{'kind':'YIELD','value':'3.18'}}}
            peer=next(x for x in ctx['members'] if x['bond_code']=='SYN-A');msg=ctx['messages'][-1]
            changes={'price':{'kind':'YIELD','value':'9.99' if malformed else '3.10'},'volume_wan':'500','finality':'NEGOTIABLE','confirmed_day':'2026-10-17'}
            return {'schema_version':'2.2','scopes':{msg['message_id']:[peer['member_id']]},'unresolved':{},'observations':[
                {'member_id':peer['member_id'],'side':'ofr','changes':changes,'evidence':{k:[msg['message_id']] for k in changes},
                 'segments':[{'message_id':msg['message_id'],'text':msg['text']}],
                 'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUOTE','negotiation_space':'NEGOTIABLE','reason':'当前明确报价仍能谈'}}]}
    monkeypatch.setattr(measurement,'ChatflowClient',LocalFixtureClient)
    config={'source':{'base_url':'https://synthetic.invalid'},'apps':{s:{'key':'synthetic-only'} for s in ('analysis','reply')}}
    result=measurement.run_case(measurement.cases()[0],config,90)
    assert result['success'] is (not malformed),result
    if malformed:
        assert result['fact_commit']['rejected']
        assert 'reply' not in result
    else:
        assert result['commit_states']=={'facts':True,'reply':'QUEUED','external_im_sent':False}


@pytest.mark.parametrize('case_id,side,offset',[('bp-minus','bid','-5'),('bp-plus','ofr','5')])
def test_bp_measurement_fixture_supplies_an_explicit_synthetic_publication_calendar(monkeypatch,case_id,side,offset):
    from scripts import measure_dify_v22 as measurement
    class LocalBPClient:
        def __init__(self,*args,stage,**kwargs):self.stage=stage;self.last_evidence={'stage':stage,'elapsed_ms':0}
        def call(self,ctx):
            if self.stage=='reply':return {'schema_version':'2.2','action':'WAIT','proposal':None}
            peer=ctx['members'][0];msg=ctx['messages'][-1]
            changes={'price':{'kind':'YIELD','value':offset,'notation':'CHINABOND_BP'},'volume_wan':'1000万'}
            return {'schema_version':'2.2','scopes':{msg['message_id']:[peer['member_id']]},'unresolved':{},'observations':[
                {'member_id':peer['member_id'],'side':side,'changes':changes,'evidence':{k:[msg['message_id']] for k in changes},
                 'segments':[{'message_id':msg['message_id'],'text':msg['text']}],
                 'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUOTE','negotiation_space':'UNKNOWN','reason':'当前对手按明确的中债BP偏移报盘'}}]}
    monkeypatch.setattr(measurement,'ChatflowClient',LocalBPClient)
    config={'source':{'base_url':'https://synthetic.invalid'},'apps':{s:{'key':'synthetic-only'} for s in ('analysis','reply')}}
    case=next(case for case in measurement.cases() if case['id']==case_id)
    result=measurement.run_case(case,config,90)
    assert result['success'],result.get('error_code')
    assert result['fact_commit']['accepted'] and not result['fact_commit']['rejected']
    assert result['fixture_publication_day']=='2026-10-17'


def test_measurement_freezes_only_source_and_detects_both_drift_boundaries(tmp_path):
    root=tmp_path/'repo'
    for name in ('server','dify','scripts'):(root/name).mkdir(parents=True)
    (root/'server/engine.py').write_text('value=1\n')
    (root/'dify/flow.yml').write_text('version: 0.1.5\n')
    (root/'server/private.env').write_text('DO_NOT_COPY=secret\n')
    frozen=tmp_path/'run/source'
    freezer=module('freeze');manifest=freezer.freeze_sources(root,frozen)
    assert not (frozen/'server/private.env').exists()
    assert freezer.drift(manifest)==[]
    (root/'server/engine.py').write_text('value=2\n')
    assert freezer.drift(manifest,frozen=False)==['server/engine.py']
    assert freezer.drift(manifest)==[]
    (frozen/'server/engine.py').write_text('value=3\n')
    assert freezer.drift(manifest)==['server/engine.py']


@pytest.mark.parametrize('failure,code',[
    ('timeout','DIFY_TIMEOUT'),('wrapped-timeout','DIFY_TIMEOUT'),('network','DIFY_NETWORK_ERROR'),
    ('http','DIFY_HTTP_ERROR'),('json','DIFY_INVALID_STAGE_JSON'),
])
def test_v22_client_distinguishes_timeout_network_and_clears_old_success_evidence(failure,code):
    import urllib.error
    from server.negotiation.chatflow import ChatflowClient,FlowUnavailable
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):return b'upstream-private-body'
    class Opener:
        def open(self,*args,**kwargs):
            if failure=='timeout':raise TimeoutError('secret')
            if failure=='wrapped-timeout':raise urllib.error.URLError(TimeoutError('secret'))
            if failure=='network':raise urllib.error.URLError(ConnectionRefusedError('secret'))
            if failure=='http':raise urllib.error.HTTPError('https://private.invalid',503,'secret',{},None)
            return Response()
    clock=iter([0,0.125])
    client=ChatflowClient('https://synthetic.invalid','synthetic-key',stage='analysis',opener=Opener(),clock=lambda:next(clock))
    client.last_evidence={'message_id':'stale-success'}
    with pytest.raises(FlowUnavailable) as caught:client.call({})
    assert caught.value.code==code
    assert 'secret' not in str(caught.value)
    assert client.last_evidence['error_code']==code
    assert client.last_evidence['elapsed_ms']==125
    assert 'message_id' not in client.last_evidence
    assert 'private' not in json.dumps(client.last_evidence)


@pytest.mark.parametrize('stage',['analysis','reply'])
def test_model_projection_keeps_all_semantic_evidence_and_control_inputs(stage):
    from copy import deepcopy
    messages=[{'message_id':str(i),'text':'完整原文\n净价34.19出，不能删句 '+str(i),'direction':'in' if i%2 else 'out',
               'trade_day':'2026-10-17','native_id':'native-'+str(i),'ingest_revision':i,'historical':False,
               'content_hash':'mechanical-hash','thread_id':'thread','received_at':1000+i} for i in range(20)]
    quote={'price':{'kind':'YIELD','value':'3.10'},'evidence':{'price':['1']}}
    common={'stage':stage,'schema_version':'2.2','thread_id':'thread','from_revision':19,'through_revision':20,'context_messages':20,
            'aliases':['alias'],'tool_results':[],'unresolved':{'1':['b']},'boundary':{'kind':'YIELD','value':'3.12'},
            'policy':{'mode':'ABSOLUTE'},'group_summary':{'reported_shortfall_wan':'500'},'reconfirm_day':'2026-10-17'}
    if stage=='analysis':
        common.update(messages=messages[-1:],members=[{'member_id':'a','bond_code':'SYN-A','aliases':['alias'],'quotes':{'ofr':quote},'our_quote_json':'actual-sent','context':messages,'control_version':3}])
    else:common.update(member_id='a',context=messages,quotes={'ofr':quote},our_quote={'price':'actual-sent'},basis={'member_version':2})
    original=deepcopy(common)
    compact=module('node_code').compact_context(common)
    assert common==original
    for key in ('stage','schema_version','from_revision','through_revision','context_messages','aliases','tool_results','unresolved','policy','boundary','group_summary','reconfirm_day'):
        assert compact[key]==common[key]
    actual=compact['members'][0]['context'] if stage=='analysis' else compact['context']
    assert len(actual)==20
    for before,after in zip(messages,actual):
        for key in ('message_id','text','direction','trade_day','native_id','ingest_revision','historical'):assert after[key]==before[key]
        assert 'content_hash' not in after and 'thread_id' not in after
    if stage=='analysis':
        assert compact['members'][0]['quotes']==common['members'][0]['quotes']
        assert compact['members'][0]['our_quote_json']=='actual-sent'
    else:assert compact['quotes']==common['quotes'] and compact['our_quote']==common['our_quote']


@pytest.mark.parametrize('stage',['analysis','reply'])
def test_private_history_is_a_bounded_intermediate_stage_without_final_fields(stage):
    ctx=context() if stage=='analysis' else {'schema_version':'2.2','stage':'reply','member_id':'p1','context':[]}
    result={'schema_version':'2.2','history_requests':[{'member_id':'p1','message_ids':['explicit-older-anchor']}]}
    assert module('node_code').validate_result(result,ctx,stage)==result


@pytest.mark.parametrize('history_request',[
    {'member_id':'foreign','message_ids':['old']},
    {'member_id':'p1','message_ids':[]},
    {'member_id':'p1','message_ids':['old','old']},
    {'member_id':'p1','message_ids':[str(i) for i in range(31)]},
    {'member_id':'p1','message_ids':['']},
    {'member_id':'p1','message_ids':[42]},
    {'member_id':'p1','message_ids':['x'*161]},
    {'member_id':'p1','message_ids':['old'],'service_token':'forbidden'},
])
def test_private_history_request_cannot_expand_identity_or_unbounded_input(history_request):
    with pytest.raises(ValueError):
        module('node_code').validate_result({'schema_version':'2.2','history_requests':[history_request]},context(),'analysis')


def test_history_cannot_mix_with_public_tool_or_repeat_a_completed_request():
    validate=module('node_code').validate_result
    request={'member_id':'p1','message_ids':['old']}
    result={'schema_version':'2.2','history_requests':[request]}
    with pytest.raises(ValueError):validate({**result,'tool_requests':[{'name':'bond_info','arguments':{}}]},context(),'analysis')
    ctx=context();ctx['history_results']=[{**request,'messages':[{'message_id':'old','text':'prior','direction':'out'}]}]
    with pytest.raises(ValueError):validate(result,ctx,'analysis')
    ctx['history_results']=[{'member_id':'p1','message_ids':[str(i) for i in range(30)],'messages':[]}]
    with pytest.raises(ValueError):validate(result,ctx,'analysis')
    with pytest.raises(ValueError):validate({'schema_version':'2.2','history_requests':[request]*7},context(),'analysis')


def test_history_projection_keeps_original_twenty_plus_extra_anchor_and_final_evidence():
    code=module('node_code');ctx=context();original=[{'message_id':str(i),'text':'actual '+str(i),'direction':'out'} for i in range(20)]
    anchor={'message_id':'older','text':'SYN-A 3.20%收','direction':'out','native_id':'native-old','ingest_revision':1}
    ctx['members'][0]['context']=original+[anchor]
    ctx['history_results']=[{'member_id':'p1','message_ids':['older'],'messages':[anchor]}]
    projected=code.compact_context(ctx)
    assert len(projected['members'][0]['context'])==21
    assert projected['members'][0]['context'][:20]==original
    assert projected['history_results']==ctx['history_results']
    result=analysis();result['observations'][0]['evidence']['price'].append('older')
    assert code.validate_result(result,ctx,'analysis')['observations'][0]['evidence']['price']==['m1','older']


def test_formal_cases_keep_original_fortytwo_and_add_history_and_each_public_tool():
    fixture=module('cases');rows=fixture.cases()
    assert len(rows)==47 and len({row['id'] for row in rows})==47
    assert sum(row['id'].startswith('ordinary-') for row in rows)==18
    assert sum(row['id'].startswith('fast-initial-') for row in rows)==6
    assert {row['required_tool'] for row in rows if row.get('required_tool')}=={'bond_info','similar_bonds','issuer_info','bond_calc'}
    history=next(row for row in rows if row.get('required_history'))
    assert history['history_count']>20


def test_declared_synthetic_tool_snapshot_resolves_identities_and_similar_bonds():
    from server.bond_tools import BondTools
    fixture=module('cases')
    class Provider:
        def snapshot(self):return fixture.reference_snapshot('ordinary-01')
    tools=BondTools(Provider())
    bond=tools.invoke('bond_info',{'schema_version':'1','mode':'get','bond_id':'SYN-A'})
    assert bond['status']=='ok',bond
    assert bond['data']['master']['issuer_id']['value']=='SYN-ISSUER'
    issuer=tools.invoke('issuer_info',{'schema_version':'1','mode':'get','issuer_id':'SYN-ISSUER'})
    assert issuer['status']=='ok',issuer
    assert 'Synthetic' in issuer['data']['full_name']['value']
    similar=tools.invoke('similar_bonds',{'schema_version':'1','anchor_bond_id':'SYN-A','tenor_basis':'maturity','remaining_days':{'min':0,'max':365},'issuer_scope':'same','compare_fields':['remaining_days','valuation_yield_pct']})
    assert similar['status']=='ok',similar
    assert [row['bond_id'] for row in similar['data']['candidates']]==['SYN-B']
    assert all('synthetic' in source['origin'].lower() for source in bond['sources'])


def test_prompt_similar_example_runs_against_real_public_tool_schema():
    from server.bond_tools import BondTools
    example=module('prompts').SIMILAR_EXAMPLE
    fixture=module('cases')
    class Provider:
        def snapshot(self):return fixture.reference_snapshot('tool-similar-bonds')
    response=BondTools(Provider()).invoke(example['name'],example['arguments'])
    assert response['status']=='ok',response
    assert example['arguments']['compare_fields']==['remaining_days','valuation_yield_pct']
    assert [row['bond_id'] for row in response['data']['candidates']]==['SYN-B']


def test_measurement_executes_real_private_history_and_explicit_stage_budget(monkeypatch):
    import re
    import itertools
    from scripts import measure_dify_v22 as measurement
    from server.negotiation import store as store_module
    sequence=itertools.count(1)
    # A valid 32-hex anchor with a long numeric run must stay an identifier.
    monkeypatch.setattr(store_module,'new_id',lambda:'abcdef12345678901234567890'+format(next(sequence),'08x'))
    seen=[]
    class HistoryFixtureClient:
        def __init__(self,*args,stage,**kwargs):self.stage=stage;self.last_evidence={'stage':stage,'elapsed_ms':0}
        def call(self,ctx):
            seen.append((self.stage,len(ctx.get('history_results',[])),ctx['_stage_budget_seconds']))
            if self.stage=='reply':return {'schema_version':'2.2','action':'WAIT','proposal':None}
            member=ctx['members'][0];msg=ctx['messages'][-1]
            anchor_id=re.search(r'消息 ([a-z0-9]+) ',msg['text']).group(1)
            if not ctx.get('history_results'):
                assert len(member['context'])==20
                assert anchor_id not in {row['message_id'] for row in member['context']}
                return {'schema_version':'2.2','history_requests':[{'member_id':member['member_id'],'message_ids':[anchor_id]}]}
            anchor=next(row for row in member['context'] if row['message_id']==anchor_id)
            assert len(member['context'])==21 and anchor['native_id']
            changes={'price':{'kind':'YIELD','value':'3.20'},'volume_wan':'500万','finality':'FINAL','confirmed_day':'2026-10-17'}
            evidence={key:[msg['message_id']] for key in changes};evidence['price'].append(anchor_id)
            return {'schema_version':'2.2','history_requests':[],'scopes':{msg['message_id']:[member['member_id']]},'unresolved':{},'observations':[
                {'member_id':member['member_id'],'side':'ofr','changes':changes,'evidence':evidence,
                 'segments':[{'message_id':msg['message_id'],'text':msg['text']},{'message_id':anchor_id,'text':anchor['text']}],
                 'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUOTE','negotiation_space':'FINAL','reason':'对手明确引用真实已发价格出量并无进一步改善'}}]}
    monkeypatch.setattr(measurement,'ChatflowClient',HistoryFixtureClient)
    config={'source':{'base_url':'https://synthetic.invalid'},'apps':{s:{'key':'synthetic-only'} for s in ('analysis','reply')}}
    case=next(case for case in measurement.cases() if case.get('required_history'))
    result=measurement.run_case(case,config,175,stage_budget_seconds=360)
    assert result['success'],result.get('error_code')
    assert result['history_verified'] is True
    assert len(result['analysis_inputs'][0]['members'][0]['context'])==20
    assert len(result['analysis_inputs'][-1]['members'][0]['context'])==21
    assert [count for stage,count,_ in seen if stage=='analysis']==[0,1]
    assert all(350<budget<=360 for _,_,budget in seen)
    assert result['stage_budget_seconds']==360 and result['lease_seconds']==390


def test_projection_exposes_loaded_and_missing_reference_ids_without_inventing_text():
    from copy import deepcopy
    ctx=context();member=ctx['members'][0]
    member['context']=[{'message_id':'loaded-'+str(i),'text':'实际完整原文'+str(i),'direction':'out'} for i in range(20)]
    member['our_quote_json']=json.dumps({'message_id':'old-outside-window','price':{'kind':'YIELD','value':'3.20'}})
    member['quotes']={'ofr':{'evidence':{'price':['loaded-0','old-price'],'volume_wan':['old-price']}}}
    before=deepcopy(ctx)
    projected=module('node_code').compact_context(ctx)
    assert ctx==before
    assert projected['evidence_index']==[{'member_id':'p1','loaded_message_ids':['m1']+['loaded-'+str(i) for i in range(20)],
                                          'unloaded_reference_ids':['old-outside-window','old-price'],
                                          'our_quote_message_id':'old-outside-window','our_quote_loaded':False}]
    assert projected['members'][0]['context']==member['context']
    assert projected['members'][0]['our_quote_json']==member['our_quote_json']
    assert list(projected).index('evidence_index')<list(projected).index('members')


def test_projection_marks_history_anchor_loaded_after_real_context_is_added():
    ctx=context();member=ctx['members'][0]
    member['our_quote_json']=json.dumps({'message_id':'old'})
    member['context']=[{'message_id':'old','text':'真实补取的完整旧原文','direction':'out'}]
    projected=module('node_code').compact_context(ctx)
    assert projected['evidence_index']==[{'member_id':'p1','loaded_message_ids':['m1','old'],'unloaded_reference_ids':[],
                                          'our_quote_message_id':'old','our_quote_loaded':True}]
    assert projected['members'][0]['context']==member['context']


def test_reply_projection_frontloads_only_existing_current_member_comparison():
    from copy import deepcopy
    lot={'member_id':'p1','quote_revision':4,'comparison':{'price_eligible':True,'remaining_gap':'0','improvement':'8.00'}}
    ctx={'schema_version':'2.2','stage':'reply','member_id':'p1','context':[{'message_id':'m1','text':'原文','direction':'in'}],
         'our_quote':{'message_id':'old'},'quotes':{},'group_summary':{'whole_lots':[{'member_id':'other'},lot]},
         'policy':{'step_bp':'2'},'boundary':{'value':'3.12'},'unresolved_messages':[]}
    before=deepcopy(ctx);projected=module('node_code').compact_context(ctx)
    assert ctx==before
    assert projected['current_member_lots']==[lot]
    assert projected['group_summary']==ctx['group_summary']
    assert projected['evidence_index']==[{'member_id':'p1','loaded_message_ids':['m1'],'unloaded_reference_ids':['old'],
                                          'our_quote_message_id':'old','our_quote_loaded':False}]
    assert list(projected).index('current_member_lots')<list(projected).index('group_summary')


def test_our_quote_index_does_not_borrow_another_members_loaded_anchor():
    ctx=context()
    ctx['members'][0].update(our_quote_json=json.dumps({'message_id':'other-out'}),
                             context=[{'message_id':'own-out','text':'本券原文','direction':'out'}])
    ctx['members'].append({'member_id':'p2','our_quote_json':json.dumps({'message_id':'other-out'}),
                           'context':[{'message_id':'other-out','text':'另券原文','direction':'out'}]})
    index=module('node_code').compact_context(ctx)['evidence_index']
    assert index[0]['our_quote_message_id']=='other-out' and index[0]['our_quote_loaded'] is False
    assert 'other-out' not in index[0]['loaded_message_ids']
    assert index[1]['our_quote_message_id']=='other-out' and index[1]['our_quote_loaded'] is True


@pytest.mark.parametrize('our_side,peer_side',[('BUY','ofr'),('SELL','bid')])
def test_reply_projection_frontloads_the_exact_relevant_counterparty_quote_without_changing_authority(our_side,peer_side):
    from copy import deepcopy
    quotes={side:{'side':side,'price':{'kind':'NET_PRICE' if side=='ofr' else 'DIRTY_PRICE','value':'34.19',
                    'settlement_date':None,'computed_yield':None,'calculation_ref':None},
                    'volume_wan':'500','assertion':'QUOTE','validity':'ACTIVE','finality':'UNKNOWN',
                    'confirmed_day':'2026-10-17','revision':2,'evidence':{'price':['current']}}
            for side in ('bid','ofr')}
    ctx={'schema_version':'2.2','stage':'reply','member_id':'p1','our_side':our_side,'quotes':quotes,
         'our_quote':{},'context':[{'message_id':'current','text':'实际现金报价原文','direction':'in'}],
         'quote_constraints':{'status':'AVAILABLE','min_yield_pct':'3.18','max_yield_pct':'3.22'}}
    before=deepcopy(ctx);result=module('node_code').compact_context(ctx)
    assert ctx==before
    assert result['current_counterparty_quote']==quotes[peer_side]
    assert result['quotes']==quotes
    assert result['quote_constraints']==ctx['quote_constraints']
    assert list(result).index('current_counterparty_quote')<list(result).index('quotes')


def test_analysis_projection_places_current_messages_first_without_changing_delta_or_history():
    from copy import deepcopy
    ctx=context();ctx.update(from_revision=20,through_revision=21,context_messages=20)
    ctx['members'][0]['context']=[{'message_id':'history-'+str(i),'direction':'in' if i%2 else 'out',
                                  'text':'完整历史原文'+str(i),'trade_day':'2026-10-17'} for i in range(20)]
    before=deepcopy(ctx);result=module('node_code').compact_context(ctx)
    assert ctx==before
    assert list(result)[0]=='messages'
    assert result['messages']==ctx['messages']
    assert result['members'][0]['context']==ctx['members'][0]['context']
    assert result['from_revision']==20 and result['through_revision']==21
    assert result['context_messages']==20


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_generated_reply_projection_preserves_inherited_and_current_tool_results_separately(version):
    from copy import deepcopy
    inherited={'request':{'name':'issuer_info','arguments':{'schema_version':'1','mode':'search','query':'SYN-ISSUER'}},
               'result':{'status':'ok','data':{'resolved_issuer_id':'SYN-ISSUER'},'sources':[{'id':'synthetic'}]}}
    current={'request':{'name':'issuer_info','arguments':{'schema_version':'1','mode':'get','issuer_id':'SYN-ISSUER'}},
             'result':{'status':'ok','data':{'full_name':'Synthetic Issuer'},'missing_fields':['rating']}}
    ctx={'schema_version':'2.2','stage':'reply','member_id':'p1','our_side':'BUY','context':[],
         'analysis_tool_results':[inherited],
         'analysis_tool_source':{'analysis_id':'current-analysis','through_revision':21},
         'tool_results':[current],'history_results':[]}
    before=deepcopy(ctx)
    doc=module('build').build('reply',version=version,provider='synthetic',model='synthetic')
    namespace={}
    exec(next(n['data']['code'] for n in doc['workflow']['graph']['nodes'] if n['id']=='context'),namespace)
    projected=json.loads(namespace['main'](json.dumps(ctx))['context'])
    assert ctx==before
    assert projected['analysis_tool_results']==[inherited]
    assert projected['analysis_tool_source']==ctx['analysis_tool_source']
    assert projected['tool_results']==[current]
    assert projected['history_results']==[]


@pytest.mark.parametrize('stage',['analysis','reply'])
def test_projection_frontloads_actual_tool_and_history_results_without_rewriting(stage):
    from copy import deepcopy
    ctx=context() if stage=='analysis' else {'schema_version':'2.2','stage':'reply','member_id':'p1','context':[]}
    ctx['tool_results']=[{'request':{'name':'bond_info','arguments':{'mode':'get','bond_id':'SYN-A'}},
                          'result':{'status':'ok','data':{'issuer_id':'SYN-ISSUER'}}}]
    ctx['history_results']=[{'member_id':'p1','message_ids':['old'],
                             'messages':[{'message_id':'old','text':'完整旧锚原文'}]}]
    if stage=='reply':
        ctx['analysis_tool_results']=[{'request':{'name':'issuer_info','arguments':{'mode':'search'}},
                                      'result':{'status':'unavailable','missing_fields':['rating']}}]
        ctx['analysis_tool_source']={'analysis_id':'actual-analysis','through_revision':21}
    before=deepcopy(ctx);result=module('node_code').compact_context(ctx)
    assert ctx==before
    for key in ('tool_results','history_results','analysis_tool_results','analysis_tool_source'):
        if key in ctx:
            assert result[key]==ctx[key]
            assert list(result).index(key)<list(result).index('schema_version')


@pytest.mark.parametrize('latest_direction,latest_historical,expected',[
    ('in',0,'latest'),('out',0,None),('in',1,None),
])
def test_reply_current_message_uses_engine_recent_order_without_falling_back_to_old_inbound(latest_direction,latest_historical,expected):
    from copy import deepcopy
    rows=[{'message_id':'old-'+str(i),'ingest_revision':i,'direction':'in' if i%2 else 'out',
           'historical':0,'text':'完整双向历史原文'+str(i),'trade_day':'2026-10-17'} for i in range(20)]
    latest={'message_id':'latest','ingest_revision':21,'direction':latest_direction,'historical':latest_historical,
            'text':'SYN-A 当前真实问询，不应重写','trade_day':'2026-10-17','native_id':None}
    ctx={'schema_version':'2.2','stage':'reply','member_id':'p1','context':list(reversed(rows)),
         'unresolved_messages':[latest]}
    before=deepcopy(ctx);result=module('node_code').compact_context(ctx)
    assert ctx==before
    assert result['current_inbound_message']==(latest if expected else None)
    assert result['context']==ctx['context'] and len(result['context'])==20
    assert result['unresolved_messages']==ctx['unresolved_messages']
    assert list(result).index('current_inbound_message')<list(result).index('context')


def test_reply_current_message_does_not_invent_order_for_a_snapshot_missing_revision():
    ctx={'schema_version':'2.2','stage':'reply','member_id':'p1',
         'context':[{'message_id':'unversioned','direction':'in','text':'没有水位的合成输入'}]}
    assert module('node_code').compact_context(ctx)['current_inbound_message'] is None
