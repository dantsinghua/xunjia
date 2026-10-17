"""Protected conversational capabilities and stateless public market-analysis tools."""
import hmac
import json
import os
from fastapi import APIRouter,Body,Request


def strict_object(text):
    if not isinstance(text,str) or len(text)>65536:raise ValueError('工具参数过大或格式无效')
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError('JSON重复字段')
            out[key]=value
        return out
    def invalid(_):raise ValueError('JSON非有限数值')
    value=json.loads(text,object_pairs_hook=pairs,parse_constant=invalid)
    if not isinstance(value,dict):raise ValueError('工具参数须为对象')
    return value


def agent_router(runner,*,service_token=None):
    private=service_token if service_token is not None else os.environ.get('XUNJIA_AGENT_SERVICE_TOKEN','')
    from .bond_tools import BondTools
    public_tools = runner.tools if runner.tools is not None else BondTools()
    router=APIRouter()
    def authenticate(request,expected):
        supplied=request.headers.get('authorization','')
        if not expected or len(expected)<32 or not hmac.compare_digest(supplied,'Bearer '+expected):raise PermissionError('Agent服务凭证无效')
    def ref(body):
        value=body.get('turn_ref')
        if not isinstance(value,str) or len(value)>96:raise ValueError('本轮能力无效')
        return value

    @router.post('/internal/agent/context')
    def context(request:Request,body:dict=Body(default_factory=dict)):
        authenticate(request,private)
        value=runner.context(ref(body))
        return {'ok':True,'context':{key:value[key] for key in ('turn_id','conversation_epoch','source_message_id')}|{'decision_context':value}}

    @router.post('/internal/agent/validate')
    def validate(request:Request,body:dict=Body(default_factory=dict)):
        authenticate(request,private)
        return runner.validate(ref(body),body.get('proposal'))

    @router.post('/internal/agent/history')
    def history(request:Request,body:dict=Body(default_factory=dict)):
        authenticate(request,private)
        return {'ok':True,**runner.history(ref(body),body.get('request'))}

    @router.post('/api/agent/tools/{name}')
    def tool(name:str,body:dict=Body(default_factory=dict)):
        # No authentication, trader binding, conversation token or turn ticket:
        # only market facts and deterministic analysis go through this endpoint.
        if set(body) in ({'arguments_json'}, {'arguments_json','tool_budget_ref'}):
            # Legacy imported Dify Tool payload is accepted, but the old
            # tool_budget_ref is deliberately ignored and grants no permissions.
            arguments=strict_object(body['arguments_json'])
        elif 'arguments_json' in body:
            raise ValueError('公共工具封装字段无效')
        else:
            arguments=body
        return public_tools.invoke(name,arguments)
    return router
