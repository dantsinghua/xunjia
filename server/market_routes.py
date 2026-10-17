"""Authenticated market, preferences and real spreadsheet exports."""
import io
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response

from .business import encoded
from .domain import decimal
from .market import parse_filter
from .quote_runtime import public_static


def market_router(market,service,tools):
    router=APIRouter(prefix='/api')
    def owner(request):return request.state.user.user_id
    def filters(raw,groups,channel,broker):
        return parse_filter(raw) if raw else parse_filter({'groups':[x for x in groups.split(',') if x] or ['bond-all'],'channel':channel,'broker':broker})

    @router.get('/bonds')
    def bonds(request:Request,filter:str='',groups:str='',channel:str='best',broker:str='',
              before:int=Query(0,ge=0),beforeUni:str='',limit:int=Query(50,ge=1,le=500)):
        return market.page(owner(request),filter_spec=filters(filter,groups,channel,broker),before=before,before_uni=beforeUni,limit=limit)

    @router.post('/bonds/resolve')
    def resolve(request:Request,body:dict=Body(default_factory=dict)):
        rows=market.resolve(owner(request),body.get('selection',body))
        if len(rows)>500:raise ValueError('本批超过500只，请缩小筛选范围')
        return {'ok':True,'data':rows,'total':len(rows)}

    def spreadsheet(rows,columns):
        from openpyxl import Workbook
        wb=Workbook(write_only=True);ws=wb.create_sheet('询价工作台')
        ws.append([title for _,title in columns])
        too_large=False;characters=0
        for index,row in enumerate(rows):
            if index>=100000:
                too_large=True;break
            cells=[]
            for key,_ in columns:
                value=row.get(key)
                if isinstance(value,(dict,list)):value=encoded(value)
                # Mark strings as text so financial names cannot become formulas.
                if isinstance(value,str):
                    characters+=len(value)
                    if characters>20000000:too_large=True;break
                    from openpyxl.cell import WriteOnlyCell
                    cell=WriteOnlyCell(ws,value=value[:32767]);cell.data_type='s';value=cell
                cells.append(value)
            if too_large:break
            ws.append(cells)
        if too_large and hasattr(rows,'close'):rows.close()
        stream=io.BytesIO();wb.save(stream)
        if too_large:raise ValueError('导出超过10万行或内容过大，请缩小时间范围')
        return Response(stream.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition':'attachment; filename="xunjia.xlsx"','Cache-Control':'no-store'})

    @router.get('/bonds/export')
    def export(request:Request,filter:str=''):
        rows=market.rows(owner(request),parse_filter(filter))
        if len(rows)>100000:raise ValueError('导出超过10万行，请缩小范围')
        return spreadsheet(rows,[('code','债券代码'),('name','简称'),('ob','Bid收益率'),('oo','Ofr收益率'),('val_yield','中债估值'),('valDate','估值日期'),('status','本人询价状态')])

    @router.get('/brokers')
    def brokers():
        return {'ok':True,'data':market.brokers_provider() if market.brokers_provider else []}

    @router.get('/bond-static/all')
    def all_static():return {'ok':True,'data':[public_static(r) for r in market.static_provider()] if market.static_provider else []}

    @router.get('/bond-static')
    def selected_static(codes:str=''):
        selected=set(codes.split(','))
        if len(selected)>500:raise ValueError('静态券查询最多500只')
        rows=market.static_provider() if market.static_provider else []
        return {'ok':True,'data':[public_static(r) for r in rows if str(r.get('bond_uni_code')) in selected]}

    @router.get('/bond/{code}')
    def bond(request:Request,code:str):
        rows=market.resolve(owner(request),{'windcodes':[code]})
        if not rows:return {'ok':False,'hint':'未找到已接入的债券资料'}
        details=market.detail_provider(code) if market.detail_provider else {}
        details=details or {}
        return {'ok':True,'data':{'bond':rows[0],'instructions':[],
                                 **{key:details.get(key,[] if key not in ('hist','intra') else {}) for key in ('quotes','trades','hist','intra')},'modalCps':[]}}

    @router.post('/tools/{name}')
    def public_tool(name:str,body:dict=Body(default_factory=dict)):
        return tools.invoke(name,body)

    @router.get('/im/inquiry/floor')
    def floors(request:Request):
        prefs=service.repo.preferences(owner(request))
        return {'ok':True,'data':{'items':[{'code':code,**value} for code,value in prefs.get('floors',{}).items()]}}

    def save_floors(who,codes,kind,value):
        if kind not in ('bp','yield'):raise ValueError('底价口径无效')
        number=decimal(value,'底价')
        if not isinstance(codes,list) or not codes or len(codes)>500 or any(not isinstance(code,str) for code in codes):raise ValueError('须选择1至500只明确债券')
        rows=market.resolve(who,{'windcodes':codes})
        if {r['code'] for r in rows}!=set(codes):raise ValueError('选择包含未接入的债券，请刷新行情')
        with service.repo.db.transaction() as tx:
            binding=service._binding(tx,who);prefs=json.loads(binding['config_json']);saved=prefs.setdefault('floors',{})
            for code in codes:
                sessions=tx.execute("SELECT conversation_id,state FROM xj_session WHERE owner_user_id=? AND bond_code=? AND archived=0",(who,code)).fetchall()
                if any(s['state'] not in ('CLOSED','STOPPED') for s in sessions):raise ValueError('该券仍有授权中的询价，请先停止并对账')
                for session in sessions:
                    pending=tx.execute("SELECT command_id FROM xj_command WHERE conversation_id=? AND status IN ('pending','unknown') LIMIT 1",(session['conversation_id'],)).fetchone()
                    if pending:raise ValueError('该券仍有未决指令，请先完成对账')
                saved[code]={'kind':kind,'value':float(number),'revision':saved.get(code,{}).get('revision',0)+1}
            tx.execute('UPDATE xj_binding SET config_json=?,version=version+1 WHERE owner_user_id=?',(encoded(prefs),who))
            service._audit(tx,who,'floor_update',binding['account_uid'],{'codes':codes,'kind':kind,'value':str(number)})
        return {'ok':True,'data':{'applied':codes,'count':len(codes),'kind':kind,'value':float(number)}}

    @router.post('/im/inquiry/floor')
    def update_floor(request:Request,body:dict=Body(default_factory=dict)):
        kind=body.get('kind','bp')
        return save_floors(owner(request),body.get('codes'),kind,body.get('bp') if kind=='bp' else body.get('value'))

    @router.post('/instruction/floor')
    def floor(request:Request,body:dict=Body(default_factory=dict)):
        rows=market.resolve(owner(request),body)
        return save_floors(owner(request),[r['code'] for r in rows],'bp',body.get('bp'))

    def strategy_view(prefs):
        saved=prefs.get('strategy')
        if saved is None:return {'templates':[],'active_template_id':None,'system':{},'configured':False}
        return {'templates':[{k:v for k,v in saved.items() if k!='system'}],'active_template_id':saved['template_id'],'system':saved['system'],'configured':True}

    @router.get('/strategy')
    def get_strategy(request:Request):return {'ok':True,'data':strategy_view(service.repo.preferences(owner(request)))}

    @router.post('/strategy')
    def save_strategy(request:Request,body:dict=Body(default_factory=dict)):
        if set(body)-{'template_id','start_offset_bp','boundary_bp','step_bp','strict_match','system'}:raise ValueError('不支持的策略字段')
        if not isinstance(body.get('template_id'),str) or len(body['template_id'])>80 or not body['template_id']:raise ValueError('策略ID无效')
        if type(body.get('strict_match')) is not bool:raise ValueError('严格匹配须为布尔值')
        values={k:float(decimal(body.get(k),k)) for k in ('start_offset_bp','boundary_bp','step_bp')}
        if values['step_bp']<=0:raise ValueError('步幅须大于0')
        if not 0<=values['boundary_bp']<=values['start_offset_bp']:raise ValueError('起始偏离须不小于非负边界')
        system=body.get('system')
        if not isinstance(system,dict) or set(system)!={'s1_round_lot_wan','s2_crossday_bp'}:raise ValueError('系统参数无效')
        system={k:float(decimal(v,k)) for k,v in system.items()}
        if system['s1_round_lot_wan']<=0 or system['s2_crossday_bp']<0:raise ValueError('系统参数超界')
        value={**values,'template_id':body['template_id'],'strict_match':body['strict_match'],'system':system}
        who=owner(request)
        with service.repo.db.transaction() as tx:
            binding=service._binding(tx,who);prefs=json.loads(binding['config_json']);prefs['strategy']=value
            tx.execute('UPDATE xj_binding SET config_json=? WHERE owner_user_id=?',(encoded(prefs),who))
            service._audit(tx,who,'strategy_update',binding['account_uid'],value)
        return {'ok':True,'data':strategy_view(prefs)}

    @router.get('/monitor/export')
    def monitor_export(request:Request,start:str='',end:str='',peer:str=''):
        who=owner(request)
        if not start or not end:raise ValueError('导出须明确开始与结束时间')
        def stamp(value,end_of_day=False):
            try:
                date=datetime.fromisoformat(value.replace('Z','+00:00'))
                if date.tzinfo is None:date=date.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                if end_of_day and len(value)==10:date+=timedelta(days=1)
                return date.timestamp()
            except ValueError:raise ValueError('日期格式无效') from None
        lo=stamp(start) if start else 0;hi=stamp(end,True) if end else service.clock()
        if lo>hi:raise ValueError('开始日期晚于结束日期')
        from .db import stream_rows
        sql="SELECT occurred_at,direction,text FROM xj_message WHERE owner_user_id=? AND occurred_at>=? AND occurred_at<? AND processing NOT IN ('duplicate','revoked')"
        args=[who,lo,hi]
        if peer:
            if len(peer)>160:raise ValueError('对手标识无效')
            sql+=" AND conversation_id IN (SELECT conversation_id FROM xj_conversation WHERE owner_user_id=? AND peer_id=? AND kind='private')"
            args.extend((who,peer))
        sql+=' ORDER BY occurred_at,message_id LIMIT 100001'
        return spreadsheet(stream_rows(service.repo.db,sql,tuple(args)),[('occurred_at','时间戳'),('direction','方向'),('text','内容')])

    @router.post('/rfq/stop')
    def stop(request:Request,body:dict=Body(default_factory=dict)):
        rows=market.resolve(owner(request),body)
        return {'ok':True,'data':service.stop_sessions(owner(request),[r['code'] for r in rows])}

    @router.post('/rfq/start')
    def start(request:Request,body:dict=Body(default_factory=dict)):
        raise ValueError('须通过询价分配预览并确认方向、量和策略后启动')

    return router
