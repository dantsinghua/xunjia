"""Generate importable Dify 0.15.3 Chatflow A/B, no Agent plugin dependency."""
from pathlib import Path
import argparse
import yaml
ROOT=Path(__file__).resolve().parent
def render(stage,provider,model):
    name='消息分析' if stage=='analysis' else '逐券谈判回复'
    model_config={'provider':provider,'name':model,'mode':'chat','completion_params':{'temperature':0}}
    nodes=[
      {'id':'start','type':'custom','position':{'x':80,'y':120},'data':{'title':'输入快照','type':'start','variables':[{'variable':'context_json','label':'context_json','type':'paragraph','required':True,'max_length':262144}]}},
      {'id':'model','type':'custom','position':{'x':400,'y':120},'data':{'title':name,'type':'llm','model':model_config,'context':{'enabled':False,'variable_selector':[]},'vision':{'enabled':False},'prompt_template':[{'role':'system','text':(ROOT/(stage+'-prompt.txt')).read_text()},{'role':'user','text':'{{#start.context_json#}}'}]}},
      {'id':'answer','type':'custom','position':{'x':720,'y':120},'data':{'title':'结构化结果','type':'answer','answer':'{{#model.text#}}'}}]
    edges=[{'id':a+'-'+b,'source':a,'target':b,'sourceHandle':'source','targetHandle':'target','type':'custom','data':{'sourceType':atype,'targetType':btype}} for a,b,atype,btype in [('start','model','start','llm'),('model','answer','llm','answer')]]
    return {'kind':'app','version':'0.1.5','app':{'name':'现券询价 V2.2 '+name,'mode':'advanced-chat','description':'由xunjia-agent持久化事实与发送状态；本应用仅生成结构化候选','icon':'💬','icon_background':'#EDF1F8'},'workflow':{'environment_variables':[],'conversation_variables':[],'features':{'opening_statement':'','suggested_questions':[],'suggested_questions_after_answer':{'enabled':False},'text_to_speech':{'enabled':False},'speech_to_text':{'enabled':False},'retriever_resource':{'enabled':False},'sensitive_word_avoidance':{'enabled':False},'file_upload':{'enabled':False,'image':{'enabled':False,'number_limits':3,'transfer_methods':['local_file']}}},'graph':{'nodes':nodes,'edges':edges,'viewport':{'x':0,'y':0,'zoom':.8}}}}
def main():
    p=argparse.ArgumentParser();p.add_argument('--provider',default='openai_api_compatible');p.add_argument('--model',default='kimi-k2.5');args=p.parse_args()
    for stage in ('analysis','reply'):
        target=ROOT/(stage+'.yml');target.write_text(yaml.safe_dump(render(stage,args.provider,args.model),allow_unicode=True,sort_keys=False),encoding='utf8');print('GENERATED',target)
if __name__=='__main__':main()