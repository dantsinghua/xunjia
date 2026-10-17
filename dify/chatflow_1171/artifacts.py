"""Generate reviewable JSON schemas and an OpenAPI import surface."""
import json
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parent


def obj(properties,required=()):return {'type':'object','additionalProperties':False,'properties':properties,'required':list(required)}
def string(**values):return dict(type='string',**values)
def array(items):return dict(type='array',items=items,maxItems=20)
def ref(name):return {'$ref':'#/definitions/'+name}


def arguments_schemas():
    version=string(enum=['1']);identifier=string(minLength=1,maxLength=100)
    page={'limit':{'type':'integer','minimum':1,'maximum':20},'cursor':string(maxLength=1024)}
    search={'schema_version':version,'mode':string(enum=['search','get']),'query':string(minLength=1,maxLength=80),**page}
    bond=obj({**search,'bond_id':identifier,'market':string(enum=['IB','SH','SZ','BJ']),
        'include':array(string(enum=['master','valuation','quotes']))},['schema_version','mode'])
    issuer=obj({**search,'issuer_id':identifier},['schema_version','mode'])
    for schema,key in ((bond,'bond_id'),(issuer,'issuer_id')):
        schema['allOf']=[{'if':{'properties':{'mode':{'const':'search'}}},'then':{'required':['query'],'not':{'required':[key]}},'else':{'required':[key],'not':{'required':['query']}}}]
    similar=obj({'schema_version':version,'anchor_bond_id':identifier,'as_of_date':string(format='date'),'tenor_basis':string(enum=['maturity','exercise']),
        'remaining_days':obj({'min':{'type':'integer','minimum':0},'max':{'type':'integer','minimum':0}},['min','max']),
        'issuer_scope':string(enum=['same','different','any']),'industry':obj({'scheme':identifier,'level':identifier,'codes':array(identifier)},['scheme','level','codes']),
        'bond_types':array(identifier),'rating':obj({'scope':string(enum=['bond','issuer']),'agency':identifier,'scale':identifier,'allowed_values':array(identifier)},['scope','agency','scale','allowed_values']),
        'compare_fields':array(string(enum=['remaining_days','balance','coupon_rate_pct','valuation_yield_pct','bid_yield_pct','ofr_yield_pct','bond_rating','issuer_rating','industry','bond_type'])),
        'sort':{'enum':[['tenor_distance','code'],['code']]},**page},['schema_version','anchor_bond_id','tenor_basis','remaining_days','issuer_scope','compare_fields'])
    decimal=string(pattern=r'^-?\d{1,12}(?:\.\d{1,40})?$');day=string(format='date')
    common={'hypothetical':{'type':'boolean'},'bond_id':identifier,'terms_snapshot_id':identifier}
    terms={'bond_type':string(enum=['fixed','zero_coupon']),'settlement_date':day,'cashflows':dict(type='array',minItems=1,maxItems=1000,items=obj({'payment_date':day,'amount_per_100':decimal},['payment_date','amount_per_100'])),
        'convention':string(enum=['ACT/365F-compounded-v1']),'compounding_frequency':{'type':'integer','minimum':1,'maximum':12},'yield_basis':string(enum=['maturity']),
        'settlement_cashflow':string(enum=['include','exclude']),'accrued_interest_per_100':decimal,'accrued_interest_settlement_date':day}
    inputs={
        'yield_gap':{'our_side':string(enum=['BUY','SELL']),'peer_side':string(enum=['bid','ofr']),'limit_yield_pct':decimal,'peer_yield_pct':decimal,'same_basis':{'type':'boolean'},'quote_type':string(enum=['yield','clean_price','dirty_price','spread','unknown']),
                     'yield_basis':string(enum=['maturity','exercise','recommended','unknown']),'peer_yield_basis':string(enum=['maturity','exercise','recommended','unknown']),'settlement_date':day,'peer_settlement_date':day,'source_ref':identifier,'authorization_ref':identifier},
        'clean_dirty':{'clean_price_per_100':decimal,'dirty_price_per_100':decimal,'accrued_interest_per_100':decimal,'settlement_date':day,'accrued_interest_settlement_date':day},
        'current_yield':{'annual_coupon_per_100':decimal,'clean_price_per_100':decimal,'coupon_basis':string(enum=['fixed_annual'])},
        'price_from_yield':{**terms,'yield_pct':decimal},'yield_from_price':{**terms,'price_type':string(enum=['clean','dirty']),'price_per_100':decimal}}
    calc={'oneOf':[obj({'schema_version':version,'operation':string(enum=[operation]),'inputs':obj({**common,**fields})},['schema_version','operation','inputs']) for operation,fields in inputs.items()]}
    return {'$schema':'http://json-schema.org/draft-07/schema#','definitions':dict(bond_info=bond,similar_bonds=similar,issuer_info=issuer,bond_calc=calc)}


def agent_turn_schema():
    identifier=string(minLength=1,maxLength=200)
    proposal=obj({'schema_version':string(enum=['1']),'turn_id':identifier,'conversation_epoch':{'type':'integer','minimum':0},'source_message_id':identifier,
        'observations':{'type':'array','maxItems':50,'items':{'type':'object'}},
        'proposed_actions':{'type':'array','maxItems':50,'items':{'type':'object','required':['type'],'properties':{'type':string(enum=['SOCIAL_REPLY','CLARIFY_BOND','ASK_QUOTE','COUNTER','WAIT','REQUEST_HANDOFF'])}}},
        'public_message':{'type':['string','null'],'maxLength':4000},'trader_view':{'type':'object'},'memory_patch':{'type':['object','null']},'history_request':{'type':['object','null']},'validation_ref':{'type':['string','null'],'maxLength':2048}})
    proposal['required']=list(proposal['properties']);proposal['$schema']='http://json-schema.org/draft-07/schema#'
    return proposal


def openapi():
    schemas=arguments_schemas()['definitions'];paths={}
    descriptions={'bond_info':'按全量字典查券或稳定ID取资料；歧义不选top1，缺数据不补模拟值。',
        'similar_bonds':'按发行人/行业/评级/期限硬筛；未知排除，不静默放宽。',
        'issuer_info':'查法人发行人；不得名称推断评级、地区或归属。',
        'bond_calc':'确定性计算，收益率字符串2.40表示2.40%；BP独立单位；假设必须hypothetical=true，真实需已验收条款ID；缺项partial，复杂券unsupported。'}
    for name,schema in schemas.items():
        description=descriptions[name]+' arguments_json必须是下列JSON schema对应的完整JSON文本，禁止代码块、SQL、URL或身份字段：'+json.dumps(schema,ensure_ascii=False,separators=(',',':'))
        request_schema=obj({'arguments_json':string(minLength=2,maxLength=65536,description=description)},
                           ['arguments_json'])
        response_schema={'type':'object','required':['schema_version','status','data','usage'],'properties':{
            'schema_version':string(enum=['1']),
            'status':string(enum=['ok','partial','ambiguous','not_found','unsupported','invalid_input','unavailable']),
            'data':{'type':'object','nullable':True},'usage':string(enum=['analysis_only'])}}
        operation={'operationId':name,'summary':descriptions[name],
            'requestBody':{'required':True,'content':{'application/json':{'schema':request_schema}}},
            'responses':{'200':{'description':'只读ToolResult；status不是ok时不得编造结果',
                                'content':{'application/json':{'schema':response_schema}}}}}
        paths['/api/agent/tools/'+name]={'post':operation}
    return {'openapi':'3.0.3','info':{'title':'现券四类公共只读工具','version':'1.1.0','description':'公开市场分析工具，无认证Token、无会话票据，不提供交易员私有数据及操作能力。'},
        'servers':[{'url':'https://xunjia.internal.invalid'}],'paths':paths}


if __name__=='__main__':
    (ROOT/'arguments-schemas.json').write_text(json.dumps(arguments_schemas(),ensure_ascii=False,indent=2)+'\n')
    (ROOT/'agent-turn.schema.json').write_text(json.dumps(agent_turn_schema(),ensure_ascii=False,indent=2)+'\n')
    (ROOT/'public-tools.openapi.yml').write_text(yaml.safe_dump(openapi(),allow_unicode=True,sort_keys=False))
