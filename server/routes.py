"""Owner-scoped HTTP boundary. Queries do not advance negotiations."""
from __future__ import annotations

import json
import base64
import math
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Query, Request

from .business import InquiryService, digest


def business_router(service: InquiryService) -> APIRouter:
    router = APIRouter(prefix="/api")

    def owner(request: Request):
        return request.state.user.user_id

    def binding(request: Request):
        found = service.repo.binding_for(owner(request))
        if not found:
            raise PermissionError("尚未配置本人企点账号")
        return found

    @router.post("/im/rfq/preview")
    def preview(request: Request, body: dict = Body(default_factory=dict)):
        return {"ok": True, "data": service.preview(owner(request), body.get("request_id"), body.get("items"))}

    @router.post("/im/rfq/send")
    def confirm(request: Request, body: dict = Body(default_factory=dict)):
        if "items" in body or not body.get("batch_id"):
            raise ValueError("须先生成并核对询价预览；不能直接发送原始明细")
        version = body.get("expected_version")
        if type(version) is not int:
            raise ValueError("须提供预览版本")
        result = service.confirm(owner(request), str(body["batch_id"]), version, body.get("request_id"))
        return {"ok": True, "data": result}

    @router.get("/im/rfq/status")
    def status(request: Request):
        binding(request)
        from .market import aggregate_sessions
        items, sessions = aggregate_sessions(service.sessions(owner(request)), service.status_commands(owner(request)))
        return {"ok": True, "data": {"items": items, "sessions": sessions}}

    @router.get("/im/receipts/{command_id}")
    def receipt(request: Request, command_id: str):
        with service.repo.db.transaction(write=False) as tx:
            row = tx.execute("SELECT command_id,status,result_json FROM xj_command WHERE command_id=? AND owner_user_id=?", (command_id, owner(request))).fetchone()
        if not row:
            raise PermissionError("命令不存在或无权访问")
        data = json.loads(row["result_json"])
        return {"ok": True, "data": {"cmd_id": row["command_id"], "status": row["status"], "ok": row["status"] == "sent",
                                      "code": data.get("code", ""), "reason": data.get("reason", ""), "expected_result_hash": digest(data), "adjudicated": data.get("adjudicated", False)}}

    @router.post("/bot/toggle")
    def bot(request: Request, body: dict = Body(default_factory=dict)):
        target = body.get("status")
        if target not in ("正常", "停止"):
            raise ValueError("须指定目标状态：正常或停止")
        return {"ok": True, "data": service.set_bot(owner(request), target == "正常")}

    @router.get("/monitor/summary")
    def summary(request: Request):
        row = binding(request)
        with service.repo.db.transaction(write=False) as tx:
            count = tx.execute("SELECT COUNT(*) AS n FROM xj_session WHERE owner_user_id=? AND archived=0 AND state<>'CLOSED'", (owner(request),)).fetchone()["n"]
            pending = tx.execute("SELECT COUNT(*) AS n FROM xj_command WHERE owner_user_id=? AND status IN ('pending','unknown')", (owner(request),)).fetchone()["n"]
        state = "正常" if row["bot_enabled"] else "停止中" if pending else "停止"
        return {"ok": True, "data": {"bot": {"status": state}, "stats": [
            {"label": "在谈会话", "value": count, "unit": "个", "sub": "本人未归档会话"},
            {"label": "待核对指令", "value": pending, "unit": "条", "sub": "含发送结果未知"},
        ], "account_bound": True}}

    @router.get("/monitor/counterparties")
    def counterparties(request: Request):
        binding(request)
        with service.repo.db.transaction(write=False) as tx:
            rows = tx.execute("SELECT conversation_id,peer_id,kind,mode,control_version FROM xj_conversation WHERE owner_user_id=? ORDER BY peer_id LIMIT 200", (owner(request),)).fetchall()
        sessions = service.sessions(owner(request))
        from .market import STATES
        from .domain import goal
        grouped={}
        for session in sessions:grouped.setdefault(session['conversation_id'],[]).append(session)
        visible_ids={r['conversation_id'] for r in rows}
        codes=list(dict.fromkeys(s['bond_code'] for s in sessions if s['conversation_id'] in visible_ids))
        market_rows={}
        for start in range(0,len(codes),500):
            market_rows.update({r['code']:r for r in request.app.state.market.resolve(owner(request),{'windcodes':codes[start:start+500]})})
        result = []
        for row in rows:
            own = grouped.get(row['conversation_id'],[])
            result.append({"cp_id": row["peer_id"], "name": row["peer_id"], "thread_id": row["conversation_id"],
                           "mode": row["mode"], "control_version": row["control_version"], "channel": "qidian",
                           "bonds": [{"code": s["bond_code"], "name": s["policy"]["name"], "session_id": s["session_id"],
                                      "state": s["state"], "stage": STATES.get(s["state"], "待核对"),
                                      "quote": STATES.get(s["state"], "待核对"), "phase": STATES.get(s["state"], "待核对"),
                                      "own_side": s["policy"]["our_side"], "own_limit": s["policy"]["limit_yield_pct"],
                                      "peer_quote": s["peer_quote"].get("yield_pct"),
                                      "gap_bp": goal(s["policy"]["our_side"], s["policy"]["limit_yield_pct"], s["peer_quote"].get("side"), s["peer_quote"].get("yield_pct"))["raw_gap_bp"],
                                      "market_bid": market_rows.get(s["bond_code"],{}).get("ob"), "market_ofr": market_rows.get(s["bond_code"],{}).get("oo"),
                                      "quote_at": s["peer_quote"].get("quoted_at"), "freshness": s["peer_quote"].get("freshness", "未知")} for s in own]})
        return {"ok": True, "data": result}

    @router.get("/monitor/chat")
    def chat(request: Request, cp: str = "", before: float = Query(0, ge=0), cursor: str = "", limit: int = Query(50, ge=1, le=200)):
        binding(request)
        bound=None
        if cursor:
            if before or len(cursor)>512:raise ValueError('聊天游标无效')
            try:
                bound=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                if not isinstance(bound,list) or len(bound)!=2 or type(bound[0]) not in (int,float) or not math.isfinite(bound[0]) or not isinstance(bound[1],str) or not 1<=len(bound[1])<=64:raise ValueError()
            except Exception:raise ValueError('聊天游标无效') from None
        with service.repo.db.transaction(write=False) as tx:
            conv = tx.execute("SELECT conversation_id FROM xj_conversation WHERE owner_user_id=? AND peer_id=? AND kind='private'", (owner(request), cp)).fetchone()
            rows = []
            if conv:
                sql = "SELECT message_id,direction,text,occurred_at,source_event_id,processing,data_json FROM xj_message WHERE owner_user_id=? AND conversation_id=? AND processing<>'duplicate'"
                args = [owner(request), conv["conversation_id"]]
                if bound:
                    sql += " AND (occurred_at<? OR (occurred_at=? AND message_id<?))"
                    args.extend((bound[0],bound[0],bound[1]))
                if before:
                    sql += " AND occurred_at<?"
                    args.append(before)
                sql += " ORDER BY occurred_at DESC,message_id DESC LIMIT ?"
                args.append(limit+1)
                rows = tx.execute(sql, tuple(args)).fetchall()
        has_more=len(rows)>limit
        rows=rows[:limit]
        next_cursor=base64.urlsafe_b64encode(json.dumps([rows[-1]['occurred_at'],rows[-1]['message_id']]).encode()).decode().rstrip('=') if has_more and rows else None
        messages=[]
        for r in reversed(rows):
            metadata=json.loads(r['data_json'])
            contexts=metadata.get('contexts',[])
            role=metadata.get('role')
            if role not in ('human','bot'):role='self' if r['direction'] in ('out','outgoing') else 'peer'
            messages.append({'id':r['message_id'],'dir':r['direction'],'role':role,'text':'（该消息已撤回或记录已作废）' if r['processing']=='revoked' else r['text'],'time':r['occurred_at'],
                             'ext_msg_id':r['source_event_id'],'landed':r['processing'] not in ('uncertain','revoked'),
                             'tag':'执行结果冲突，待核对' if r['processing']=='uncertain' else '',
                             'contexts':contexts,'bond':contexts[0]['code'] if len(contexts)==1 else '',
                             'bond_state':'已确认' if contexts else '未归属','cmd_id':metadata.get('command_id')})
        return {"ok": True, "data": {"cp": {"cp_id": cp, "name": cp}, "bonds": [], "messages": messages, "nextCursor": next_cursor, "hasMore": has_more}}

    @router.post("/monitor/send")
    def manual_send(request: Request, body: dict = Body(default_factory=dict)):
        result=service.manual_send(owner(request),body.get('cp'),body.get('text'),body.get('request_id'))
        return {'ok':True,'data':result}

    @router.post('/session/{session_id}/takeover')
    def takeover(request:Request,session_id:str):
        who=owner(request)
        with service.repo.db.transaction(write=False) as tx:
            row=tx.execute('SELECT conversation_id FROM xj_session WHERE session_id=? AND owner_user_id=?',(session_id,who)).fetchone()
            if not row:row=tx.execute('SELECT conversation_id FROM xj_conversation WHERE conversation_id=? AND owner_user_id=?',(session_id,who)).fetchone()
        if not row:raise PermissionError('会话不存在或无权访问')
        return {'ok':True,'data':service.takeover(who,row['conversation_id'])}

    @router.post('/im/inquiry/session/resume')
    def resume(request:Request,body:dict=Body(default_factory=dict)):
        who=owner(request)
        with service.repo.db.transaction(write=False) as tx:
            row=tx.execute("SELECT conversation_id FROM xj_conversation WHERE peer_id=? AND owner_user_id=? AND kind='private'",(body.get('peer'),who)).fetchone()
        if not row:raise PermissionError('对话不存在或无权访问')
        return {'ok':True,'data':service.resume(who,row['conversation_id'])}

    @router.get('/monitor/detail')
    def detail(request:Request,cp:str=''):
        who=owner(request)
        with service.repo.db.transaction(write=False) as tx:
            conv=tx.execute("SELECT conversation_id FROM xj_conversation WHERE peer_id=? AND owner_user_id=? AND kind='private'",(cp,who)).fetchone()
        rows=service.sessions(who)
        result=[{'id':r['session_id'],'name':r['policy']['name'],'code':r['bond_code'],'state':r['state'],
                 'policy':r['policy'],'peer_quote':r['peer_quote'],'our_quote':r['our_quote'],
                 'date':datetime.fromtimestamp(r['created_at'],timezone.utc).isoformat(),'dir':r['policy']['our_side'],
                 'my_floor':r['policy']['limit_yield_pct'],'my_vol_wan':r['policy']['volume_wan'],'peer_final_yield':r['peer_quote'].get('yield_pct'),
                 'trader':request.state.user.display_name,'term':None,'val':None,'theirVol':None,'pm':None} for r in rows if conv and r['conversation_id']==conv['conversation_id']]
        return {'ok':True,'data':result}

    @router.post('/im/inquiry/session/pause')
    def pause(request:Request,body:dict=Body(default_factory=dict)):
        who=owner(request)
        with service.repo.db.transaction(write=False) as tx:
            row=tx.execute("SELECT conversation_id FROM xj_conversation WHERE owner_user_id=? AND peer_id=? AND kind='private'",(who,body.get('peer'))).fetchone()
        if not row:raise PermissionError('对话不存在或无权访问')
        return {'ok':True,'data':service.takeover(who,row['conversation_id'])}

    @router.post('/im/inquiry/status')
    def close_inquiry(request:Request,body:dict=Body(default_factory=dict)):
        if body.get('status') not in ('已撤价','已放弃'):raise ValueError('只允许停止本轮，不能手工伪造报价或成交状态')
        code=body.get('code')
        if not isinstance(code,str) or not code:raise ValueError('债券代码无效')
        result=service.stop_sessions(owner(request),[code])
        return {'ok':True,'data':{**result,'reason':'已停止相关询价；在途指令将继续取消和对账'}}

    @router.post('/sessions/{session_id}/archive')
    def archive_inquiry(request:Request,session_id:str):
        who=owner(request)
        with service.repo.db.transaction() as tx:
            service._binding(tx,who)
            row=tx.execute('SELECT conversation_id,state,archived FROM xj_session WHERE owner_user_id=? AND session_id=?',(who,session_id)).fetchone()
            if not row:raise PermissionError('会话不存在或无权访问')
            if row['state'] not in ('CLOSED','STOPPED'):raise ValueError('请先停止本会话并完成对账')
            pending=tx.execute("SELECT command_id FROM xj_command WHERE conversation_id=? AND status IN ('queued','pending','unknown') LIMIT 1",(row['conversation_id'],)).fetchone()
            if pending:raise ValueError('对话尚有未决指令，不能归档')
            if not row['archived']:
                tx.execute('UPDATE xj_session SET archived=1,revision=revision+1 WHERE owner_user_id=? AND session_id=?',(who,session_id))
                service._audit(tx,who,'session_archived',session_id,{})
        return {'ok':True,'data':{'archived':session_id}}

    @router.get('/im/commands/unknown')
    def unknown_commands(request:Request):
        who=owner(request);binding(request)
        with service.repo.db.transaction(write=False) as tx:
            rows=tx.execute("SELECT command_id,peer_id,body_json,result_json,created_at FROM xj_command WHERE owner_user_id=? AND status='unknown' ORDER BY created_at,command_id LIMIT 200",(who,)).fetchall()
        return {'ok':True,'data':[{'cmd_id':r['command_id'],'peer_id':r['peer_id'],'text':json.loads(r['body_json']).get('text',''),'created_at':r['created_at'],'expected_result_hash':digest(json.loads(r['result_json'])),'conflict':json.loads(r['result_json']).get('conflict',False)} for r in rows]}

    @router.post('/im/receipts/{command_id}/reconcile')
    def reconcile(request:Request,command_id:str,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':service.reconcile_manually(owner(request),command_id,body.get('status'),body.get('evidence'),body.get('expected_result_hash'),body.get('confirmed'))}

    return router
