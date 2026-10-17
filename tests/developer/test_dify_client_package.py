import importlib
import json
from pathlib import Path
import pytest


@pytest.fixture
def code():
    return importlib.import_module('dify.chatflow_1171.node_code')


def turn(**changes):
    x=dict(schema_version='1',turn_id='t',conversation_epoch=2,source_message_id='m',observations=[],
        proposed_actions=[{'type':'WAIT'}],public_message=None,trader_view={},memory_patch=None,history_request=None,validation_ref=None)
    x.update(changes);return x


def trusted():return json.dumps(dict(turn_id='t',conversation_epoch=2,source_message_id='m'))


def test_context_omits_capabilities_before_agent(code):
    body=json.dumps(dict(ok=True,context=dict(turn_id='t',conversation_epoch=2,source_message_id='m',decision_context={'market':[],'tool_budget_ref':'public-budget'})))
    result=code.prepare_context(body,200,'2','1')
    assert 'turn_ref' not in result['agent_context']
    assert 'service_capability' not in result['agent_context']
    with pytest.raises(ValueError):code.prepare_context(body,200,'3','1')


def test_proposal_identity_cannot_be_repaired_by_overwriting(code):
    with pytest.raises(ValueError):code.parse_strict(json.dumps(turn(turn_id='other')),trusted(),False)


def test_model_public_draft_and_fake_validation_are_discarded(code):
    result=code.parse_strict(json.dumps(turn(public_message='PRIVATE-DRAFT',validation_ref='invented')),trusted(),False)
    parsed=json.loads(result['proposal'])
    assert parsed['public_message'] is None and parsed['validation_ref'] is None


def test_history_second_request_is_rejected(code):
    request={'message_refs':['m'],'reason':'check'}
    with pytest.raises(ValueError):code.parse_strict(json.dumps(turn(history_request=request)),trusted(),True)


def test_public_node_input_is_only_backend_public_context(code):
    body=json.dumps(dict(ok=True,needs_public_reply=True,public_context={'allowed_actions':['ASK_QUOTE'],'public_facts':[],'peer_message':'hello','relationship_context':[]}))
    result=code.validation_response(body,200,json.dumps(turn()))
    assert result['needs_public_reply'] is True
    assert 'trader_view' not in result['public_context']
    with pytest.raises(ValueError):code.validation_response(json.dumps(dict(ok=True,needs_public_reply=True,public_context={'limit_yield_pct':'secret'})),200,json.dumps(turn()))


def test_internal_branch_always_nulls_public_text_and_receipt(code):
    result=code.internal_result(json.dumps(turn(public_message='old',validation_ref='old')))
    assert json.loads(result['result'])['public_message'] is None
    assert json.loads(result['result'])['validation_ref'] is None


def test_final_rejected_text_has_no_machine_answer(code):
    with pytest.raises(ValueError):code.final_response('{"ok":false}',200,json.dumps(turn(public_message='candidate')))
    body='{"ok":true,"validation_ref":"server-signed"}'
    out=code.final_response(body,200,json.dumps(turn(public_message='candidate')))
    assert json.loads(out['result'])['validation_ref']=='server-signed'


def test_generated_graph_has_only_final_machine_answers_and_public_tools():
    package=importlib.import_module('dify.chatflow_1171.render')
    graph=package.build_graph(package.example_config())
    report=package.validate_graph(graph)
    assert report['agent_nodes']==2
    assert report['answer_nodes']==2
    assert report['tools']==['bond_info','similar_bonds','issuer_info','bond_calc']
    assert graph['app']['mode']=='advanced-chat'
    assert graph['version']=='0.7.0'
    public=next(n for n in graph['workflow']['graph']['nodes'] if n['id']=='public_reply')
    assert 'private_agent' not in str(public)
    assert 'turn_ref' not in str(public) and 'service_capability' not in str(public)
    assert 'memory' not in public['data']


def test_render_requires_exact_deployment_pins():
    package=importlib.import_module('dify.chatflow_1171.render')
    with pytest.raises(ValueError):package.validate_deployment(package.example_config())


def test_aggregators_cannot_choose_values_from_before_the_branch():
    package=importlib.import_module('dify.chatflow_1171.render')
    nodes={n['id']:n['data'] for n in package.build_graph(package.example_config())['workflow']['graph']['nodes']}
    assert ['parse_first','proposal'] not in nodes['proposal_merge']['variables']
    assert ['history_check','proposal'] not in nodes['ready_proposal']['variables']


def execute_graph(*,need_repair=False,need_history=False,public_allowed=True,final_ok=True):
    """Local Code/branch harness; HTTP and model are bounded synthetic fixtures."""
    package=importlib.import_module('dify.chatflow_1171.render')
    graph=package.build_graph(package.example_config())['workflow']['graph']
    nodes={n['id']:n['data'] for n in graph['nodes']}
    values={('sys','query'):'hello'};pending=['start'];answers=[];seen=[]
    def selected(selector):return values[tuple(selector)]
    context={'ok':True,'context':{'turn_id':'t','conversation_epoch':2,'source_message_id':'m','decision_context':{'limit_yield_pct':'PRIVATE-2.10','tool_budget_ref':'public-budget'}}}
    while pending:
        identity=pending.pop(0);data=nodes[identity];kind=data['type'];seen.append(identity);handle='source'
        if kind=='start':out={'turn_ref':'SECRET-CAP','service_capability':'SECRET-SERVICE','context_epoch':'2','schema_version':'1'}
        elif kind=='code':
            namespace={};exec(data['code'],namespace)
            out=namespace['main'](**{v['variable']:selected(v['value_selector']) for v in data['variables']})
        elif kind=='agent':
            if identity=='private_agent' and need_repair:out={'text':'malformed'}
            else:out={'text':json.dumps(turn(history_request={'message_refs':['m'],'reason':'check'} if need_history and identity=='private_agent' else None))}
        elif kind=='llm':
            if identity=='repair_json':out={'text':json.dumps(turn(history_request={'message_refs':['m'],'reason':'check'} if need_history else None))}
            else:
                payload=selected(['allow_public','public_context'])
                assert 'PRIVATE-' not in payload and 'SECRET-' not in payload
                out={'text':'请问这只券目前可做多少量？'}
        elif kind=='http-request':
            if identity=='context_http':response=context
            elif identity=='history_http':response={'ok':True,'messages':[{'id':'m','text':'真实公开历史'}]}
            elif identity=='validate_action':response={'ok':public_allowed,'needs_public_reply':public_allowed,'public_context':{'allowed_actions':['ASK_QUOTE'],'public_facts':[],'peer_message':'hello','relationship_context':[]}}
            else:response={'ok':final_ok,'validation_ref':'final-server-proof' if final_ok else None}
            out={'body':json.dumps(response),'status_code':200}
        elif kind=='if-else':
            handle='true' if selected(data['cases'][0]['conditions'][0]['variable_selector']) is True else 'false';out={}
        elif kind=='variable-aggregator':out={'output':next(selected(s) for s in data['variables'] if tuple(s) in values)}
        elif kind=='answer':
            ref=data['answer'][3:-3].split('.');answers.append(json.loads(selected(ref)));out={}
        for key,value in out.items():values[(identity,key)]=value
        pending.extend(e['target'] for e in graph['edges'] if e['source']==identity and e['sourceHandle']==handle)
    return answers,seen


@pytest.mark.parametrize('repair,history',[(False,False),(True,False),(False,True),(True,True)])
def test_graph_executes_complete_candidate_after_selected_repair_and_history(repair,history):
    answers,seen=execute_graph(need_repair=repair,need_history=history)
    assert len(answers)==1
    assert answers[0]['public_message']=='请问这只券目前可做多少量？'
    assert answers[0]['validation_ref']=='final-server-proof'
    assert seen.count('repair_json')==int(repair)
    assert seen.count('history_agent')==int(history)
    assert answers[0]['history_request'] is None


def test_graph_rejection_branch_never_calls_public_language_model():
    answers,seen=execute_graph(public_allowed=False)
    assert 'public_reply' not in seen and 'validate_text' not in seen
    assert answers[0]['public_message'] is None and answers[0]['validation_ref'] is None


def test_graph_final_rejection_does_not_answer_raw_public_candidate():
    with pytest.raises(ValueError):execute_graph(final_ok=False)


def test_four_openapi_tools_only_expose_arguments_string_and_no_identity_headers():
    artifacts=importlib.import_module('dify.chatflow_1171.artifacts')
    schema=artifacts.openapi()
    assert len(schema['paths'])==4
    assert 'security' not in schema and 'components' not in schema
    for path,methods in schema['paths'].items():
        operation=methods['post'];body=operation['requestBody']['content']['application/json']['schema']
        assert list(body['properties'])==['arguments_json']
        assert body['properties']['arguments_json']['type']=='string'
        assert 'parameters' not in operation


def test_agent_model_selector_is_not_the_llm_node_model_schema():
    package=importlib.import_module('dify.chatflow_1171.render')
    config=package.example_config()
    graph=package.build_graph(config)['workflow']['graph']
    for node in graph['nodes']:
        if node['data']['type']=='agent':
            value=node['data']['agent_parameters']['model']['value']
            # AgentRuntimeSupport.fetch_model uses model and model_type, unlike LLMNodeData.name.
            assert value.get('model')==config['model_name']
            assert value.get('model_type')=='llm'


def test_renderer_outputs_consistent_endpoints_with_explicit_synthetic_pins(tmp_path):
    import subprocess
    import sys
    import yaml
    package=importlib.import_module('dify.chatflow_1171.render')
    config=package.example_config()
    config.update(service_url='https://fixture.internal',tool_provider_id='fixture-provider',model_provider='fixture/model',model_name='fixture-model',
        model_plugin_identifier='fixture/model:1.0.0@'+'a'*64,strategy_plugin_identifier='langgenius/agent:0.0.50@'+'b'*64,
        dify_api_image='langgenius/dify-api:1.17.1@sha256:'+'c'*64,dify_web_image='langgenius/dify-web:1.17.1@sha256:'+'d'*64)
    source=tmp_path/'deployment.json';source.write_text(json.dumps(config))
    graph_file=tmp_path/'graph.yml';api_file=tmp_path/'tools.yml'
    run=subprocess.run([sys.executable,str(Path(package.__file__)),'--config',str(source),'--output',str(graph_file),'--openapi-output',str(api_file)],capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    assert yaml.safe_load(api_file.read_text())['servers']==[{'url':'https://fixture.internal'}]
    graph=yaml.safe_load(graph_file.read_text())
    assert all(n['data']['url'].startswith('https://fixture.internal/internal/agent/') for n in graph['workflow']['graph']['nodes'] if n['data']['type']=='http-request')
    assert graph['workflow']['environment_variables'][0]['value']==''
