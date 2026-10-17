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


def test_measurement_never_uses_failure_latency_or_unmeasured_window():
    stats=module('measurement')
    samples=[{'success':True,'semantic_ok':True,'path':'common','elapsed_ms':800+i,'timeout':False} for i in range(30)]
    samples.append({'success':False,'semantic_ok':False,'path':'tools','elapsed_ms':90000,'timeout':True})
    report=stats.summarize(samples,measurement_id='synthetic-stats-test',source={'kind':'test'})
    assert report['successes']==30 and report['failures']==1 and report['timeouts']==1
    assert report['reference_ms']==814.5
    assert 0<=report['aggregation']['quiet_ms']<=report['aggregation']['max_wait_ms']<814.5/2
    assert report['overall']['p95_ms']<900
    with pytest.raises(ValueError):stats.summarize(samples[:2],measurement_id='short',source={'kind':'test'})


def test_short_actual_history_fast_path_limits_window_even_when_full_context_is_slower():
    stats=module('measurement')
    samples=[{'success':True,'semantic_ok':True,'path':'common','elapsed_ms':10000,'timeout':False} for _ in range(30)]
    samples += [{'success':True,'semantic_ok':True,'path':'fast_common','elapsed_ms':800,'timeout':False} for _ in range(6)]
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
    else:common.update(context=messages,quotes={'ofr':quote},our_quote={'price':'actual-sent'},basis={'member_version':2})
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
