"""V2 production HTTP projections and explicit human actions.
Compatibility URLs map to new group/member state; no legacy-table queries.
"""
from __future__ import annotations
import asyncio
import base64
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import io
import json
from fastapi import APIRouter,Body,Query,Request
from fastapi.responses import Response,StreamingResponse
from .prices import InvalidFact,number,comparison
from .service import digest
from .store import encoded
from ..market import parse_filter
from ..quote_runtime import public_static
def router(service,market,tools,transport,*,collector_selection=None):
    r=APIRouter(prefix='/api');store=service.store;commands=service.commands_core
    def owner(req):return req.state.user.user_id
    def require_online(req):
        guard=getattr(collector_selection,"require_online",None)
        if callable(guard):guard(owner(req))
    def bound(req):
        binding=store.binding_for(owner(req))
        if not binding:raise PermissionError('尚未绑定本人采集账号')
        return binding
    def thread_for(who,peer):
        selected=store.binding_for(who)
        with store.db.transaction(write=False) as tx:
            row=tx.execute('SELECT thread_id FROM xj2_thread WHERE owner_id=? AND peer_uid=? AND account_uid=? AND workbench_id=? ORDER BY thread_id',(who,peer,selected['account_uid'] if selected else '',selected['workbench_id'] if selected else '')).fetchall()
        if len(row)!=1:raise InvalidFact('请明确选择唯一对手聊天线程')
        return row[0]['thread_id']
    def adapter(b,route=None):
        route=route or b['transport'];adapters=getattr(transport,'adapters',{})
        a=adapters.get((b['owner_user_id'],b['account_uid'],route))
        if a is None:raise InvalidFact('传输适配器尚未配置')
        return a
    def current_command(who,command_id):
        row=commands.get(who,command_id)
        with store.db.transaction(write=False) as tx:store.require_current_thread(tx,who,row['thread_id'])
        return row
    def label(group,peer=None):
        if group['state']=='HANDOFF':return '待人工确认'
        if not group['send_allowed'] or peer and not peer['send_allowed']:return '已暂停'
        if peer and peer['command_state']=='UNKNOWN':return '待核对'
        if peer and peer['pending_command_id']:return '待回执'
        return '询价中'
    @r.get('/negotiation/capacity')
    def capacity(request:Request):
        b=bound(request)
        return {'ok':True,'data':store.counterparty_capacity(owner(request),b['account_uid'])}
    @r.get('/negotiation/state')
    def state(request:Request):return {'ok':True,'data':store.snapshot(owner(request))}
    @r.get('/negotiation/messages/{thread_id}')
    def messages(request:Request,thread_id:str,after_revision:int=Query(0,ge=0),limit:int=Query(200,ge=1,le=500)):
        return {'ok':True,'data':store.raw_page(owner(request),thread_id,after_revision=after_revision,limit=limit)}
    @r.get('/negotiation/updates')
    async def updates(request:Request,after:int=Query(0,ge=0)):
        who=owner(request)
        async def events():
            revision=after
            while not await request.is_disconnected():
                data=await asyncio.to_thread(store.snapshot,who)
                if data['revision']>revision:
                    revision=data['revision'];yield 'id: '+str(revision)+'\nevent: changed\ndata: '+encoded({'revision':revision})+'\n\n'
                else:yield ': keepalive\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(events(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
    @r.post('/negotiation/messages/{message_id}/association')
    def correct_association(request:Request,message_id:str,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':store.correct_association(owner(request),body.get('thread_id'),message_id,
            body.get('member_ids'),body.get('expected_revision'),body.get('reason'))}
    @r.post('/negotiation/groups/{group_id}/sending')
    def group_sending(request:Request,group_id:str,body:dict=Body(default_factory=dict)):
        if body.get("allowed") is True:require_online(request)
        store.set_sending(owner(request),body.get('allowed'),group_id=group_id)
        return {'ok':True,'data':{'group_id':group_id,'analysis_enabled':True}}
    @r.post('/negotiation/members/{member_id}/sending')
    def member_sending(request:Request,member_id:str,body:dict=Body(default_factory=dict)):
        if body.get("allowed") is True:require_online(request)
        store.set_sending(owner(request),body.get('allowed'),member_id=member_id)
        return {'ok':True,'data':{'member_id':member_id,'analysis_enabled':True}}
    @r.get('/negotiation/members/{member_id}/context')
    def context(request:Request,member_id:str):return {'ok':True,'data':store.context(owner(request),member_id)}
    @r.get('/negotiation/members/{member_id}/decisions')
    def decisions(request:Request,member_id:str):
        with store.db.transaction(write=False) as tx:
            member=store._member(tx,owner(request),member_id)
            store.require_current_thread(tx,owner(request),member['thread_id'])
            rows=tx.execute('SELECT * FROM xj2_decision WHERE member_id=? ORDER BY created_at DESC LIMIT 100',(member_id,)).fetchall()
        return {'ok':True,'data':[{**dict(row),'result':json.loads(row['result_json'])} for row in rows]}
    @r.post('/im/binding')
    def bind(request:Request,body:dict=Body(default_factory=dict)):
        if collector_selection and collector_selection.configured:
            raise InvalidFact('请从采集端目录选择工作台和企点号，使用版本校验的选择接口')
        return {'ok':True,'data':store.bind_account(owner(request),body.get('account_uid'),body.get('workbench_id'),body.get('transport','oss'))}
    @r.post('/im/binding/select')
    def select_collector(request:Request,body:dict=Body(default_factory=dict)):
        if not collector_selection or not collector_selection.configured:raise InvalidFact('当前服务尚未接入采集端目录发现')
        return {'ok':True,'data':collector_selection.select(owner(request),body)}
    @r.post('/im/catalog/refresh')
    def refresh_collectors(request:Request):
        if not collector_selection or not collector_selection.configured:raise InvalidFact('当前服务尚未接入采集端目录发现')
        return {'ok':True,'data':collector_selection.catalog(owner(request),refresh=True)}
    @r.post('/im/session-reset')
    def request_reset(request:Request,body:dict=Body(default_factory=dict)):
        require_online(request)
        return {'ok':True,'data':service.session_reset.request(owner(request),body)}
    @r.get('/im/session-reset')
    def latest_reset(request:Request):
        return {'ok':True,'data':service.session_reset.get(owner(request))}
    @r.get('/im/session-reset/{reset_id}')
    def reset_status(request:Request,reset_id:str):
        return {'ok':True,'data':service.session_reset.get(owner(request),reset_id)}
    @r.post('/im/rfq/preview')
    def preview(request:Request,body:dict=Body(default_factory=dict)):
        require_online(request)
        return {'ok':True,'data':service.preview(owner(request),body.get('request_id'),body.get('items'))}
    @r.post('/im/rfq/send')
    def confirm(request:Request,body:dict=Body(default_factory=dict)):
        require_online(request)
        if 'items' in body:raise InvalidFact('须先预览并确认，不能直接发送原始明细')
        return {'ok':True,'data':service.confirm(owner(request),body.get('batch_id'),body.get('expected_version'),body.get('request_id'))}
    @r.get('/im/rfq/status')
    def rfq_status(request:Request):
        bound(request);groups=store.group_rows(owner(request));items=[];sessions=[]
        for g in groups:
            if g['archived']:continue
            items.append({'code':g['bond_code'],'group_id':g['group_id'],'status':label(g),'summary':g['summary']})
            sessions.extend({'session_id':p['member_id'],'code':g['bond_code'],'status':label(g,p),'revision':p['version'],'command_id':p['pending_command_id'],'command_status':str(p['command_state'] or '').lower()} for p in g['peers'])
        return {'ok':True,'data':{'engine':'v22','items':items,'sessions':sessions}}
    @r.post('/bot/toggle')
    def bot(request:Request,body:dict=Body(default_factory=dict)):
        if body.get("status")=="正常":require_online(request)
        if body.get('status') not in ('正常','停止'):raise InvalidFact('指定正常或停止')
        return {'ok':True,'data':service.set_bot(owner(request),body['status']=='正常')}
    @r.get('/monitor/summary')
    def summary(request:Request):
        bound(request);s=store.snapshot(owner(request))
        return {'ok':True,'data':{'revision':s['revision'],'engine':'v22','bot':{'status':'正常' if s['send_allowed'] else '停止'},'stats':[
            {'label':'券组','value':len(s['groups']),'unit':'组','sub':'按券共享目标量'},
            {'label':'对手子会话','value':sum(len(g['peers']) for g in s['groups']),'unit':'个','sub':'逐对手独立价量'},
            {'label':'待解析线程','value':sum(t['analysis_pending'] for t in s['threads']),'unit':'个','sub':'停止发送仍继续分析'}]}}
    @r.get('/monitor/counterparties')
    def counterparties(request:Request):
        bound(request);s=store.snapshot(owner(request));peers={}
        for g in s['groups']:
            if g['archived']:continue
            for p in g['peers']:
                cp=peers.setdefault(p['thread_id'],{'cp_id':p['peer_uid'],'name':p['peer_uid'],'thread_id':p['thread_id'],'channel':'qidian','bonds':[]})
                side='ofr' if g['our_side']=='BUY' else 'bid';q=p['quotes'][side];price=q.get('price')
                compared=comparison(__import__('server.negotiation.prices',fromlist=['Price']).Price.load(price),g['boundary'],our_side=g['our_side']) if price and g['boundary'] else None
                cp['bonds'].append({'session_id':p['member_id'],'group_id':g['group_id'],'code':g['bond_code'],'name':g['plan'].get('name',g['bond_code']),
                    'state':g['state'],'stage':label(g,p),'quote':label(g,p),'phase':label(g,p),'own_side':g['our_side'],
                    'own_limit':g['boundary']['value'] if g['boundary'] else None,'peer_quote':price.get('value') if price else None,
                    'gap_bp':compared['raw_gap'] if compared and compared['unit']=='BP' else None,'quotes':p['quotes'],
                    'peer_volume_wan':q['volume_wan'],'analysis_pending':p['analysis_pending'],'summary':g['summary'],
                    'freshness':q['validity'],'finality':q['finality'],'revision':p['version'],
                    'group_revision':g['version'],'group_send_allowed':bool(g['send_allowed']),
                    'send_allowed':bool(s['send_allowed'] and g['send_allowed'] and p['send_allowed']),
                    'pending_command_id':p['pending_command_id'],'command_state':p['command_state'],
                    'response_state':p['response_state'],'trade_day':g['trade_day'],
                    'needs_review':bool(g['review_needed'])})
        return {'ok':True,'revision':s['revision'],'data':list(peers.values())}
    @r.get('/monitor/chat')
    def chat(request:Request,cp:str='',cursor:str='',limit:int=Query(50,ge=1,le=200)):
        bound(request)
        try:tid=thread_for(owner(request),cp)
        except InvalidFact:return {'ok':True,'data':{'cp':{'cp_id':cp,'name':cp},'messages':[],'hasMore':False,'nextCursor':None}}
        before=None
        if cursor:
            try:before=int(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)).decode())
            except Exception:raise InvalidFact('聊天游标无效') from None
        data=store.raw_page(owner(request),tid,limit=500)
        # Pagination is stable on receive sequence; UI ordering stays by native time.
        with store.db.transaction(write=False) as tx:
            query='SELECT ingest_revision FROM xj2_message WHERE thread_id=?';args=[tid]
            if before is not None:query+=' AND ingest_revision<?';args.append(before)
            positions=tx.execute(query+' ORDER BY ingest_revision DESC LIMIT ?',(*args,limit+1)).fetchall()
        positions=[p['ingest_revision'] for p in positions];selected=positions[:limit]
        if selected:data=store.raw_page(owner(request),tid,after_revision=min(selected)-1,limit=limit)
        messages=[]
        for m in data['messages'] if selected else []:
            if m['ingest_revision'] not in selected:continue
            contexts=[{'code':c['bond_code'],'name':c['name'],'member_id':c['member_id'],'relation':c['relation']} for c in m['contexts']]
            messages.append({'id':m['message_id'],'text':m['text'],'dir':m['direction'],'role':'self' if m['direction']=='out' else 'peer',
                'time':m['occurred_at'],'received_at':m['received_at'],'revision':m['ingest_revision'],
                'projection_revision':data['revision'],'analysis_state':m['analysis_state'],
                'received':True,'landed':True,'contexts':contexts,'bond':contexts[0]['code'] if len(contexts)==1 else '',
                'bond_state':'解析中' if m['analysis_state']=='QUEUED' else '待核验' if m['analysis_state']=='NEEDS_REVIEW' else '已分析','tag':m['analysis_state'],'kind':m['kind'],'target_id':m['target_id']})
        messages.sort(key=lambda m:(m['time'],m['revision']))
        return {'ok':True,'data':{'revision':data['revision'],'cp':{'cp_id':cp,'name':cp},'messages':messages,
          'hasMore':len(positions)>limit,'nextCursor':base64.urlsafe_b64encode(str(min(selected)).encode()).decode().rstrip('=') if len(positions)>limit else None}}
    @r.get('/monitor/detail')
    def detail(request:Request,cp:str=''):
        rows=service.sessions(owner(request));rows=[s for s in rows if not cp or s['peer_id']==cp]
        return {'ok':True,'data':[{'id':s['session_id'],'group_id':s['group_id'],'name':s['policy'].get('name',s['bond_code']),'code':s['bond_code'],
            'state':s['state'],'policy':s['policy'],'quotes':s['quotes'],'peer_quote':s['peer_quote'],'our_quote':s['our_quote'],
            'dir':s['policy'].get('our_side'),'my_floor':s['policy'].get('limit_yield_pct'),'my_vol_wan':s['policy'].get('volume_wan'),
            'theirVol':s['peer_quote']['volume_wan'],'peer_final_yield':s['peer_quote'].get('yield_pct'),'summary':s['summary'],
            'analysis_pending':s['analysis_pending'],'trader':request.state.user.display_name} for s in rows]}
    @r.post('/monitor/send')
    def send(request:Request,body:dict=Body(default_factory=dict)):
        require_online(request)
        return {'ok':True,'data':service.manual_send(owner(request),body.get('cp'),body.get('text'),body.get('request_id'))}
    @r.post('/im/send')
    def im_send(request:Request,body:dict=Body(default_factory=dict)):
        require_online(request)
        return {'ok':True,'data':service.manual_send(owner(request),body.get('uin'),body.get('text'),body.get('request_id'))}
    @r.post('/session/{session_id}/takeover')
    def takeover(request:Request,session_id:str):
        store.set_sending(owner(request),False,member_id=session_id)
        return {'ok':True,'data':{'member_id':session_id,'analysis_enabled':True}}
    @r.post('/im/inquiry/session/pause')
    def pause(request:Request,body:dict=Body(default_factory=dict)):
        mid=body.get('member_id') or body.get('session_id')
        if not mid:raise InvalidFact('请选择单券对手子会话，不能隐式暂停整个线程')
        store.set_sending(owner(request),False,member_id=mid)
        return {'ok':True,'data':{'member_id':mid,'analysis_enabled':True}}
    @r.post('/im/inquiry/session/resume')
    def resume(request:Request,body:dict=Body(default_factory=dict)):
        require_online(request)
        mid=body.get('member_id') or body.get('session_id')
        if not mid:raise InvalidFact('请选择需要恢复的单券对手子会话')
        store.set_sending(owner(request),True,member_id=mid)
        return {'ok':True,'data':{'member_id':mid,'analysis_enabled':True}}
    @r.post('/im/inquiry/status')
    def stop_bond(request:Request,body:dict=Body(default_factory=dict)):
        if body.get('status') not in ('已撤价','已放弃'):raise InvalidFact('不能通过按钮伪造成交或对方报价')
        return {'ok':True,'data':service.stop_sessions(owner(request),[body.get('code')])}
    @r.post('/sessions/{member_id}/archive')
    def archive(request:Request,member_id:str):
        with store.write(owner(request)) as tx:
            member=store._member(tx,owner(request),member_id);g=store._group(tx,owner(request),member['group_id'])
            for participant in tx.execute('SELECT thread_id FROM xj2_peer WHERE group_id=?',(g['group_id'],)).fetchall():
                store.require_current_thread(tx,owner(request),participant['thread_id'])
            active=tx.execute("SELECT c.command_id FROM xj2_command c LEFT JOIN xj2_command_member cm ON cm.command_id=c.command_id LEFT JOIN xj2_peer participant ON participant.member_id=cm.member_id WHERE (c.group_id=? OR participant.group_id=?) AND c.state IN ('QUEUED','PENDING','UNKNOWN') LIMIT 1",(g['group_id'],g['group_id'])).fetchone()
            if active or g['send_allowed']:raise InvalidFact('先停止券组并完成所有命令对账')
            tx.execute('UPDATE xj2_group SET archived=1,version=version+1 WHERE group_id=?',(g['group_id'],))
        return {'ok':True,'data':{'archived_group':g['group_id']}}
    @r.get('/im/receipts/{command_id}')
    def receipt(request:Request,command_id:str):
        row=current_command(owner(request),command_id);result=json.loads(row['result_json'])
        return {'ok':True,'data':{'cmd_id':command_id,'status':row['state'].lower(),'ok':row['state']=='SENT','reason':result.get('reason',''),'expected_result_hash':digest(result),'native_id':row['native_id']}}
    @r.get('/im/commands/unknown')
    def unknown(request:Request):
        return {'ok':True,'data':[{'cmd_id':c['command_id'],'peer_id':c['peer_id'],'text':c['text'],'created_at':c['created_at'],'expected_result_hash':digest(json.loads(c['result_json'])),'conflict':json.loads(c['result_json']).get('conflict',False)} for c in service.commands(owner(request)) if c['state']=='UNKNOWN']}
    @r.post('/im/receipts/{command_id}/reconcile')
    def reconcile(request:Request,command_id:str,body:dict=Body(default_factory=dict)):
        row=current_command(owner(request),command_id)
        if not body.get('confirmed') or body.get('expected_result_hash')!=digest(json.loads(row['result_json'])):raise InvalidFact('请刷新原始执行证据后明确确认')
        with store.db.transaction(write=False) as tx:thread=store._thread(tx,owner(request),row['thread_id'])
        status=str(body.get('status','')).upper()
        if status=='SENT' and not body.get('native_id'):raise InvalidFact('确认已发送必须提供原生消息ID，不可只凭文字推断')
        value=commands.receipt(owner(request),command_id,account_uid=thread['account_uid'],peer_uid=thread['peer_uid'],content_hash=row['content_hash'],status=status,evidence=body.get('evidence'),native_id=body.get('native_id'))
        return {'ok':True,'data':{'state':value,'needs_review':value=='UNKNOWN'}}
    def links_view(request):
        if (collector_selection and collector_selection.configured
                and store.binding_for(owner(request)) is None):
            return {'links':[],'hints':{'selection':'请先配置文件中转，再选择采集端工作台与台内企点号'}}
        b=bound(request);links=[]
        checks=store.preferences(owner(request)).get('collector_directory_checks',{}) if collector_selection and collector_selection.configured else {}
        for route in ('oss','mail'):
            try:adapter(b,route);configured=True
            except InvalidFact:configured=False
            check=checks.get(route,{})
            tested=bool(configured and all(check.get(k)==b[k] for k in ('account_uid','workbench_id','version')))
            stale=True
            if tested:
                if route=='oss' and collector_selection and collector_selection.configured:
                    try:
                        fresh=collector_selection.freshness(owner(request))
                        if any(fresh.get(k)!=check.get(k) for k in ('publication_id','publication_hash')):check=fresh
                    except PermissionError:raise
                    except (ValueError,OSError):check={**check,'stale':True}
                try:stale=bool(check.get('stale')) or not 0<=store.clock()-float(check['updated_at'])<=300
                except (KeyError,TypeError,ValueError):pass
            links.append({'id':b['account_uid']+':'+route,'account_id':b['account_uid'],'account_uid':b['account_uid'],'self_uid':b['account_uid'],
              'mode':route,'name':request.state.user.display_name,'workbench_id':b['workbench_id'],'active':route==b['transport'],
              'configured':configured,'version':b['version'],'tested':tested,'catalog_stale':stale,
              'state':('缓存目录已校验' if stale else '目录已校验') if tested else '未检查' if configured else '未配置',
              'account':{'id':b['account_uid'],'self_uid':b['account_uid'],'label':b['account_uid'],'state':'已绑定'},
              'form':{route:{'workbench_id':b['workbench_id'],'account_uid':b['account_uid']}},'contacts':[],'secret_set':configured})
        return {'active_id':b['account_uid']+':'+b['transport'],'links':links,'binding_version':b['version']}
    @r.get('/im/links')
    def links(request:Request):return {'ok':True,'data':links_view(request)}
    @r.get('/im/contacts')
    def contacts(request:Request,refresh:int=Query(0,ge=0,le=1)):
        if collector_selection and collector_selection.configured:
            return {'ok':True,'data':collector_selection.contacts(owner(request),refresh=bool(refresh))}
        b=bound(request);data=adapter(b).contacts()
        if data.get('account_uid')!=b['account_uid']:raise InvalidFact('通讯录账号不匹配')
        people=[{k:str(row.get(k) or '') for k in ('uin','name','remark')} for row in data.get('contacts',[]) if isinstance(row,dict)]
        return {'ok':True,'data':{'source':b['transport'],'contacts':people,'org':data.get('org',{}),'name':request.state.user.display_name,'account_id':b['account_uid']}}
    @r.get('/im/files')
    def files(request:Request,before:int|None=Query(None,ge=1),limit:int=Query(50,ge=1,le=100)):
        if set(request.query_params)-{'before','limit'}:raise InvalidFact('文件记录只读取本人已选采集端与账号')
        if not collector_selection or not collector_selection.configured:raise InvalidFact('当前服务尚未接入文件中转目录')
        return {'ok':True,'data':collector_selection.files(owner(request),before=before,limit=limit)}
    @r.get('/im/catalog')
    def catalog(request:Request):
        if collector_selection and collector_selection.configured:
            return {'ok':True,'data':collector_selection.catalog(owner(request))}
        b=bound(request);data=adapter(b).catalog()
        if data.get('account_uid')!=b['account_uid'] or data.get('workbench_id')!=b['workbench_id']:raise InvalidFact('采集目录身份不一致')
        return {'ok':True,'data':{'workbenches':[{'workbench_id':b['workbench_id'],'stale':False,'accounts':[{'account_uid':b['account_uid'],'account_nick':request.state.user.display_name}],'capabilities':data.get('capabilities',[])}]}}
    @r.post('/im/links/{link_id}/activate')
    def activate(request:Request,link_id:str,body:dict=Body(default_factory=dict)):
        b=bound(request);account,_,route=link_id.rpartition(':')
        if account!=b['account_uid']:raise PermissionError('通道不属于本人')
        adapter(b,route);store.switch_transport(owner(request),route,body.get('expected_version'))
        return {'ok':True,'data':links_view(request)}
    @r.post('/im/links/{link_id}/test')
    def test_link(request:Request,link_id:str):
        b=bound(request);account,_,route=link_id.rpartition(':')
        if account!=b['account_uid']:raise PermissionError('通道不属于本人')
        if collector_selection and collector_selection.configured and route=='oss':
            book=collector_selection.check(owner(request))
            return {'ok':True,'data':{'passed':True,'steps':[{'name':'所选账号目录校验','ok':True,
                'detail':'已校验认证缓存，目录已过期，尚未确认实时连接' if book.get('stale') else '已校验所选账号目录；不代表原生发送成功'}],
                'link':next(x for x in links_view(request)['links'] if x['mode']==route)}}
        a=adapter(b,route);data=a.catalog();book=a.contacts()
        valid=data.get('account_uid')==book.get('account_uid')==b['account_uid']
        return {'ok':True,'data':{'passed':valid,'steps':[{'name':'目录只读检查','ok':valid,'detail':'不代表原生发送成功'}],'link':next(x for x in links_view(request)['links'] if x['mode']==route)}}
    @r.get('/bonds')
    def bonds(request:Request,filter:str='',groups:str='',channel:str='best',broker:str='',before:int=0,beforeUni:str='',limit:int=Query(50,ge=1,le=500)):
        spec=parse_filter(filter) if filter else parse_filter({'groups':[x for x in groups.split(',') if x] or ['bond-all'],'channel':channel,'broker':broker})
        return market.page(owner(request),filter_spec=spec,before=before,before_uni=beforeUni,limit=limit)
    @r.post('/bonds/resolve')
    def resolve(request:Request,body:dict=Body(default_factory=dict)):
        rows=market.resolve(owner(request),body.get('selection',body));return {'ok':True,'data':rows,'total':len(rows)}
    @r.get('/brokers')
    def brokers():return {'ok':True,'data':market.brokers_provider() if market.brokers_provider else []}
    @r.get('/bond-static/all')
    def all_static():return {'ok':True,'data':[public_static(v) for v in market.static_provider()] if market.static_provider else []}
    @r.get('/bond-static')
    def selected_static(codes:str=''):
        wanted=set(codes.split(','));rows=market.static_provider() if market.static_provider else []
        return {'ok':True,'data':[public_static(v) for v in rows if str(v.get('bond_uni_code')) in wanted]}
    @r.get('/bond/{code}')
    def bond(request:Request,code:str):
        rows=market.resolve(owner(request),{'windcodes':[code]})
        if not rows:return {'ok':False,'hint':'债券数据源尚无对应记录'}
        details=market.detail_provider(code) if market.detail_provider else {}
        return {'ok':True,'data':{'bond':rows[0],'instructions':[],**(details or {})}}
    @r.post('/tools/{name}')
    @r.post('/agent/tools/{name}')
    def tool(name:str,body:dict=Body(default_factory=dict)):
        from .chatflow import json_object
        args=json_object(body['arguments_json']) if 'arguments_json' in body else body
        return tools.invoke(name,args)
    @r.get('/im/inquiry/floor')
    def floors(request:Request):
        values=store.preferences(owner(request)).get('floors',{})
        return {'ok':True,'data':{'items':[{'code':k,**v} for k,v in values.items()]}}
    def save_floor(who,codes,kind,value):
        if kind not in ('bp','yield'):raise InvalidFact('未知底价口径')
        n=number(str(value));codes=list(dict.fromkeys(codes))
        if not 1<=len(codes)<=500:raise InvalidFact('请选择1到500只明确债券')
        with store.write(who) as tx:
            b=store.require_binding(tx,who);prefs=json.loads(b['config_json']);values=prefs.setdefault('floors',{})
            for code in codes:
                values[code]={'kind':kind,'value':str(n),'revision':values.get(code,{}).get('revision',0)+1}
            tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?',(encoded(prefs),who))
        # Saved defaults do not silently change already-authorized active groups.
        return {'ok':True,'data':{'applied':codes,'count':len(codes),'kind':kind,'value':str(n),'active_groups_unchanged':True}}
    @r.post('/im/inquiry/floor')
    def floor(request:Request,body:dict=Body(default_factory=dict)):
        kind=body.get('kind','bp');return save_floor(owner(request),body.get('codes',[]),kind,body.get('bp') if kind=='bp' else body.get('value'))
    @r.post('/instruction/floor')
    def instruction(request:Request,body:dict=Body(default_factory=dict)):
        return save_floor(owner(request),[v['code'] for v in market.resolve(owner(request),body)],'bp',body.get('bp'))
    def strategy_view(prefs):
        value=prefs.get('strategy')
        return {'templates':[value] if value else [],'active_template_id':value.get('template_id') if value else None,'system':value.get('system',{}) if value else {},'configured':bool(value)}
    @r.get('/strategy')
    def strategy(request:Request):return {'ok':True,'data':strategy_view(store.preferences(owner(request)))}
    @r.post('/strategy')
    def set_strategy(request:Request,body:dict=Body(default_factory=dict)):
        if not body.get('template_id') or number(str(body.get('step_bp','')))<0:raise InvalidFact('策略必须有名称及正步幅')
        prefs=store.save_preferences(owner(request),{'strategy':body})
        return {'ok':True,'data':strategy_view(prefs)}
    @r.post('/rfq/stop')
    def rfq_stop(request:Request,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':service.stop_sessions(owner(request),[v['code'] for v in market.resolve(owner(request),body)])}
    @r.post('/rfq/start')
    def rfq_start():raise InvalidFact('须先分配、预览并确认')
    def spreadsheet(rows,columns):
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        wb=Workbook(write_only=True);sheet=wb.create_sheet('询价');sheet.append([title for key,title in columns])
        for i,row in enumerate(rows):
            if i>=100000:raise InvalidFact('最多导出10万行，请缩小范围')
            cells=[]
            for key,_title in columns:
                value=row.get(key)
                if isinstance(value,(dict,list)):value=encoded(value)
                if isinstance(value,str):
                    cell=WriteOnlyCell(sheet,value=value[:32767]);cell.data_type='s';value=cell
                cells.append(value)
            sheet.append(cells)
        stream=io.BytesIO();wb.save(stream)
        return Response(stream.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="xunjia.xlsx"'})
    @r.get('/bonds/export')
    def export_bonds(request:Request,filter:str=''):
        return spreadsheet(market.rows(owner(request),parse_filter(filter)),[('code','代码'),('name','债券'),('ob','买价'),('oo','卖价'),('val_yield','中债估值')])
    @r.get('/monitor/export')
    def export_messages(request:Request,start:str='',end:str='',peer:str=''):
        if not start or not end:raise InvalidFact('请选择明确起止日期')
        def stamp(value,last=False):
            d=datetime.fromisoformat(value.replace('Z','+00:00'))
            if d.tzinfo is None:d=d.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
            if last and len(value)==10:d+=timedelta(days=1)
            return d.timestamp()
        lo,hi=stamp(start),stamp(end,True)
        if hi<lo:raise InvalidFact('日期范围错误')
        from ..db import stream_rows
        selected=bound(request)
        query='SELECT m.occurred_at,m.direction,m.text FROM xj2_message m JOIN xj2_thread t ON t.thread_id=m.thread_id WHERE t.owner_id=? AND t.account_uid=? AND t.workbench_id=? AND m.occurred_at>=? AND m.occurred_at<?'
        params=[owner(request),selected['account_uid'],selected['workbench_id'],lo,hi]
        if peer:query+=' AND t.peer_uid=?';params.append(peer)
        return spreadsheet(stream_rows(store.db,query+' ORDER BY m.occurred_at,m.message_id LIMIT 100001',tuple(params)),[('occurred_at','时间'),('direction','方向'),('text','原文')])
    return r
