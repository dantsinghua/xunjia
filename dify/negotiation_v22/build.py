"""Render one common A/B contract using nodes supported by Dify 0.15.3 and 1.17.1."""
from pathlib import Path
import inspect
from dify.negotiation_v22 import prompts, node_code


def build(stage, *, version, provider, model):
    if stage not in ('analysis', 'reply') or version not in ('0.15.3', '1.17.1'):
        raise ValueError('Unsupported stage/version')
    if not provider or not model:
        raise ValueError('Choose an existing configured model/provider')
    root = Path(__file__).resolve().parents[2]
    runtime = root/'server/negotiation'
    if not runtime.is_dir():
        runtime = root/'app/server/negotiation'  # Uninstalled offline bundle layout.
    shared = (runtime/'text_roles.py').read_text(encoding='utf8')+'\n'
    shared += (runtime/'slots.py').read_text(encoding='utf8').replace('from .text_roles import non_trading_spans, numeric_spelling, valuation_offset_spans\n','')+'\n'
    shared += (runtime/'reply_render.py').read_text(encoding='utf8').replace('from .slots import collect_candidates\n','')+'\n'
    wire = Path(__file__).with_name('slot_wire.py').read_text(encoding='utf8').replace('from server.negotiation.slots import collect_candidates\n','').replace('from server.negotiation.text_roles import settlement_literal\n','')
    source = shared+wire+'\n'+Path(__file__).with_name('node_code.py').read_text(encoding='utf8').replace(
        'from server.negotiation.slots import collect_candidates, literal_member_hints, window_reference_gaps\n','').replace(
        'from dify.negotiation_v22.slot_wire import decode_slot_observation\n','').replace(
        'from server.negotiation.reply_render import public_information, render_reply_plan, reply_requirements, quote_grid\n','')
    source += '\n\ndef main(text: str, context_json: str) -> dict:\n'
    source += f"    result = decode_stage_result(parse_object(text), parse_object(context_json), {stage!r})\n"
    source += "    return {'result': json.dumps(result, ensure_ascii=False, separators=(',', ':'))}\n"
    projection = shared+'\nimport json\n'+inspect.getsource(node_code.compact_context)
    projection += '\n_SYSTEMS = '+repr({stage: prompts._SYSTEMS[stage]})+'\n'
    projection += '\n'+inspect.getsource(prompts.select_system_prompt)
    projection += '\n\ndef main(context_json: str) -> dict:\n'
    projection += "    context = json.loads(context_json)\n"
    projection += f"    system = select_system_prompt(context, {stage!r})\n"
    projection += "    return {'context': json.dumps(compact_context(context), ensure_ascii=False, separators=(',', ':')), 'system_prompt': system}\n"
    specs = [
        ('start', {'type':'start','variables':[{'variable':'context_json','label':'context_json','type':'paragraph','required':True,'default':'','max_length':1000000}]}),
        ('context', {'type':'code','code_language':'python3','code':projection,
                     'variables':[{'variable':'context_json','value_selector':['start','context_json']}],
                     'outputs':{'context':{'type':'string','children':None},'system_prompt':{'type':'string','children':None}}}),
        ('model', {'type':'llm','model':{'provider':provider,'name':model,'mode':'chat','completion_params':{'temperature':0}},
                   'context':{'enabled':False,'variable_selector':[]},'vision':{'enabled':False},'variables':[],
                   'prompt_template':[{'role':'system','text':'{{#context.system_prompt#}}'},
                                      {'role':'user','text':'本轮完整业务context_json（仅省数据库机械字段，原文/证据未裁剪）：\n{{#context.context#}}'}]}),
        ('contract', {'type':'code','code_language':'python3','code':source,
                      'variables':[{'variable':'text','value_selector':['model','text']},{'variable':'context_json','value_selector':['start','context_json']}],
                      'outputs':{'result':{'type':'string','children':None}}}),
        ('answer', {'type':'answer','answer':'{{#contract.result#}}','variables':[],'outputs':{}}),
    ]
    nodes = [{'id':key,'type':'custom','position':{'x':i*340,'y':240},'sourcePosition':'right','targetPosition':'left',
              'data':{'title':key,'desc':'','selected':False,**data}} for i,(key,data) in enumerate(specs)]
    edges = [{'id':a+'-'+b,'type':'custom','source':a,'target':b,'sourceHandle':'source','targetHandle':'target',
              'data':{'sourceType':specs[i][1]['type'],'targetType':specs[i+1][1]['type'],'isInIteration':False}}
             for i,(a,b) in enumerate(zip([s[0] for s in specs],[s[0] for s in specs][1:]))]
    features = {'file_upload':{'enabled':False},'opening_statement':'','suggested_questions':[],
                **{key:{'enabled':False} for key in ('suggested_questions_after_answer','speech_to_text','text_to_speech','retriever_resource','sensitive_word_avoidance')}}
    document = {'version':'0.1.5' if version=='0.15.3' else '0.7.0','kind':'app',
                'app':{'name':'Xunjia V2.2 '+('A analysis' if stage=='analysis' else 'B reply'),
                       'mode':'advanced-chat','description':'Versioned facts and independent negotiation; application validates and controls all sending.',
                       'icon':'🤖','icon_background':'#E4FBCC','use_icon_as_answer_icon':False},
                'workflow':{'conversation_variables':[],'environment_variables':[],'features':features,
                            'graph':{'nodes':nodes,'edges':edges,'viewport':{'x':0,'y':0,'zoom':0.8}}}}
    if version=='1.17.1':
        document['dependencies']=[]  # Existing configured provider; no Agent/plugin installation required.
    return document
