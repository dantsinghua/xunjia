"""A trader owns one fixed account; both configured channels remain explicit."""
import json
from fastapi import APIRouter,Body,Request
from .business import encoded


def link_router(service,transport):
    router=APIRouter(prefix='/api/im')
    def own(request):
        value=service.repo.binding_for(request.state.user.user_id)
        if not value:raise PermissionError('尚未配置本人企点账号')
        return value
    def adapter(binding,route=None):
        route=route or binding['transport']
        values=getattr(transport,'adapters',{})
        found=values.get((binding['owner_user_id'],binding['account_uid'],route))
        if found is None:raise ValueError('该通道尚未由管理员配置')
        return found
    def public(request):
        binding=own(request);prefs=service.repo.preferences(binding['owner_user_id'])
        health=prefs.get('transport_health',{})
        routes=[]
        for route in ('oss','mail'):
            try:adapter(binding,route);configured=True
            except ValueError:configured=False
            checked=health.get(route,{})
            tested=bool(configured and checked.get('ok') and 0<=service.clock()-checked.get('at',0)<300)
            routes.append({'id':binding['account_uid']+':'+route,'account_id':binding['account_uid'],'account_uid':binding['account_uid'],
                           'self_uid':binding['account_uid'],'name':request.state.user.display_name,'mode':route,
                           'mode_label':'OSS' if route=='oss' else '邮件','workbench_id':binding['workbench_id'],
                           'active':binding['transport']==route,'configured':configured,'version':binding['version'],'tested':tested,
                           'catalog_stale':not tested,'state':'只读检查通过' if tested else '未检查' if configured else '未配置',
                           'test_note':'仅验证目录读取；实际发送以执行回执为准' if tested else '',
                           'account':{'id':binding['account_uid'],'self_uid':binding['account_uid'],'label':binding['account_uid'],'state':'已绑定'},
                           'form':{route:{'workbench_id':binding['workbench_id'],'account_uid':binding['account_uid']}},
                           'contacts':[],'contacts_source':'','secret_set':configured})
        return {'active_id':binding['account_uid']+':'+binding['transport'],'links':routes,'binding_version':binding['version']}
    def route_id(binding,link_id):
        parts=link_id.rsplit(':',1)
        if len(parts)!=2 or parts[0]!=binding['account_uid'] or parts[1] not in ('oss','mail'):raise PermissionError('通道不存在或无权访问')
        return parts[1]

    @router.get('/links')
    def links(request:Request):return {'ok':True,'data':public(request)}

    @router.get('/contacts')
    def contacts(request:Request):
        binding=own(request);data=adapter(binding).contacts()
        if not isinstance(data,dict) or data.get('account_uid')!=binding['account_uid']:raise PermissionError('通讯录账号不匹配')
        people=data.get('contacts',[]);org=data.get('org',{})
        if not isinstance(people,list) or not isinstance(org,dict):raise ValueError('通讯录格式无效')
        clean=[{k:str(row.get(k) or '') for k in ('uin','name','remark')} for row in people if isinstance(row,dict) and row.get('uin')]
        departments=[{k:row.get(k) for k in ('id','parent_id','name','order')} for row in org.get('departments',[]) if isinstance(row,dict)]
        members=[{k:row.get(k) for k in ('uin','name','account','department_id','order')} for row in org.get('members',[]) if isinstance(row,dict)]
        return {'ok':True,'data':{'source':binding['transport'],'contacts':clean,'org':{'departments':departments,'members':members},'name':request.state.user.display_name,'account_id':binding['account_uid']}}

    @router.get('/catalog')
    def catalog(request:Request):
        binding=own(request);value=adapter(binding).catalog()
        if value.get('account_uid')!=binding['account_uid'] or value.get('workbench_id')!=binding['workbench_id']:raise PermissionError('目录绑定不匹配')
        return {'ok':True,'data':{'workbenches':[{'workbench_id':binding['workbench_id'],'stale':False,'accounts':[{'account_uid':binding['account_uid'],'account_nick':request.state.user.display_name}]}]}}

    @router.post('/links/{link_id}/test')
    def test(request:Request,link_id:str):
        binding=own(request);route=route_id(binding,link_id)
        passed=False
        try:
            selected=adapter(binding,route)
            data=selected.catalog();book=selected.contacts()
            passed=data.get('account_uid')==book.get('account_uid')==binding['account_uid'] and data.get('workbench_id')==binding['workbench_id']
        except Exception:pass
        with service.repo.db.transaction() as tx:
            row=service._binding(tx,binding['owner_user_id']);prefs=json.loads(row['config_json'])
            prefs.setdefault('transport_health',{})[route]={'ok':passed,'at':service.clock()}
            tx.execute('UPDATE xj_binding SET config_json=? WHERE owner_user_id=?',(encoded(prefs),binding['owner_user_id']))
        view=next(r for r in public(request)['links'] if r['mode']==route)
        return {'ok':True,'data':{'passed':passed,'steps':[{'name':'本人目录及通讯录读取','ok':passed,'detail':'只读检查通过；外发仍需执行回执' if passed else '配置或通道暂不可用'}],'accounts':[view['account']],'link':view}}

    @router.post('/links/{link_id}/activate')
    def activate(request:Request,link_id:str,body:dict=Body(default_factory=dict)):
        binding=own(request);route=route_id(binding,link_id);adapter(binding,route)
        expected=body.get('expected_version')
        if type(expected) is not int:raise ValueError('须提供当前绑定版本')
        with service.repo.db.transaction() as tx:
            row=service._binding(tx,binding['owner_user_id'])
            if row['version']!=expected:raise ValueError('绑定已更新，请刷新后重试')
            if row['transport']!=route:
                pending=tx.execute("SELECT command_id FROM xj_command WHERE owner_user_id=? AND status IN ('queued','pending','unknown') LIMIT 1",(binding['owner_user_id'],)).fetchone()
                if row['bot_enabled'] or pending:raise ValueError('请先停止机器人并完成所有在途命令对账')
                tx.execute('UPDATE xj_binding SET transport=?,version=version+1 WHERE owner_user_id=?',(route,binding['owner_user_id']))
                tx.execute("UPDATE xj_conversation SET mode='STOPPED',control_version=control_version+1 WHERE owner_user_id=?",(binding['owner_user_id'],))
                service._audit(tx,binding['owner_user_id'],'transport_switched',binding['account_uid'],{'from':row['transport'],'to':route})
        return {'ok':True,'data':public(request)}

    @router.post('/send')
    def send(request:Request,body:dict=Body(default_factory=dict)):
        binding=own(request)
        return {'ok':True,'data':service.manual_send(binding['owner_user_id'],body.get('uin'),body.get('text'),body.get('request_id'))}

    @router.get('/quote')
    def quote_status():
        import os
        configured=os.environ.get('XUNJIA_QUOTE_SOURCE','').lower()=='qb'
        return {'ok':True,'data':{'configured':configured,'managed':True,'source':'innodealing' if configured else 'not_configured'}}
    return router
