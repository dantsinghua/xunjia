"""Bounded read-only questions over existing market facts and caller-owned sessions."""
from collections import Counter
from html import escape
import math
import re

from fastapi import APIRouter, Body, HTTPException, Request
from .qa_workflow import QAWorkflow, WorkflowError, TOOL_PATH


HELP = '暂不支持此问题。可使用：查券 简称或代码、报价 完整代码、我的在谈概要。成交趋势、自由统计及操作指令尚未接入。'


def _text(value):
    return escape(str(value if value is not None else '未提供')).replace('|', '\\|').replace('\n', ' ')


def _number(value):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def qa_router(service, market, tools, workflow=None):
    router = APIRouter(prefix='/api')
    workflow = workflow or QAWorkflow(market)
    from .qa_history import QAHistory, OceanBaseHistory
    database=getattr(getattr(service,'store',None),'db',None)
    state=workflow.path.parent.parent/'state' if getattr(workflow,'path',None) is not None else None
    if database is not None and database.mysql:
        history=OceanBaseHistory(database,receipt_source=state/'qa-workflow.sqlite3' if state else None)
    else:
        history=QAHistory(state/'qa-history.db') if state else None

    def logged_in(request):
        user = getattr(request.state, 'user', None)
        if user is None:raise HTTPException(401, '请先登录')
        return user.user_id

    @router.get('/qa/workflow')
    def workflow_status(request: Request):
        logged_in(request)
        return {'ok': True, 'data': workflow.status()}

    @router.put('/qa/workflow')
    def workflow_config(request: Request, body: dict = Body(default_factory=dict)):
        logged_in(request)
        try:return {'ok': True, 'data': workflow.save(body)}
        except (ValueError, WorkflowError) as exc:raise HTTPException(400, str(exc)) from None

    @router.get('/qa/workflow/dsl')
    def workflow_dsl(request: Request):
        logged_in(request)
        from pathlib import Path
        from fastapi.responses import FileResponse
        path=Path(__file__).resolve().parents[1]/'dify/qa_workflow/workflow.yml'
        if not path.is_file() or path.is_symlink():raise HTTPException(404,'工作流文件尚未随版本安装')
        return FileResponse(path,media_type='application/yaml',filename='xunjia-qa-workflow.yml')

    # The extra /query segment avoids the existing A/B /agent/tools/{name}
    # catch-all. Middleware permits this path; capability validation is mandatory.
    @router.post(TOOL_PATH.removeprefix('/api'))
    def workflow_market(request: Request, body: dict = Body(default_factory=dict)):
        authorization = request.headers.get('Authorization', '')
        if not authorization.startswith('Bearer '):raise HTTPException(403, '只读查询凭证缺失')
        try:return workflow.read_market(authorization[7:], body)
        except WorkflowError as exc:
            if str(exc) in ('QA_MARKET_UNAVAILABLE', 'QA_QUOTE_CAPACITY_EXCEEDED'):
                raise HTTPException(503, '当日行情明细不可用或超过查询容量；未生成报价') from None
            raise HTTPException(403, '只读查询凭证无效、已过期或查询额度已用完') from None
        except ValueError as exc:raise HTTPException(400, str(exc)) from None

    @router.post('/qa')
    def answer(request: Request, body: dict = Body(default_factory=dict)):
        owner = logged_in(request)
        if set(body)-{'question','session_id','request_id'} or not isinstance(body.get('question'), str) or not 1 <= len(body['question'].strip()) <= 200:
            raise HTTPException(400, '仅支持1至200字的question，不接受身份或查询参数')
        sid=body.get('session_id');rid=body.get('request_id')
        if sid is None and rid is None:return perform_answer(request,{'question':body['question']})
        if history is None:raise HTTPException(503,'会话存储尚未配置')
        if not isinstance(sid,str) or not isinstance(rid,str):raise HTTPException(400,'会话和请求标识必须同时提供')
        try:tid,saved=history.claim(owner,sid,rid,body['question'].strip())
        except LookupError:raise HTTPException(404,'会话不存在') from None
        except BlockingIOError as exc:raise HTTPException(409,str(exc)) from None
        if saved is not None:return {'ok':True,'data':saved}
        try:
            result=perform_answer(request,{'question':body['question']})
            result['data']['session_id']=sid
            history.finish(owner,tid,result['data'])
            return result
        except Exception:
            history.finish(owner,tid)
            raise

    @router.get('/qa/sessions')
    def sessions(request:Request,before:str=''):
        owner=logged_in(request)
        if history is None:raise HTTPException(503,'会话存储尚未配置')
        notice=''
        try:history.restore_receipts(owner,workflow)
        except Exception:notice='部分旧查询回执暂未恢复；已保存的新会话仍可读取。'
        try:return {'ok':True,'data':{**history.list(owner,before or None),'notice':notice}}
        except LookupError:raise HTTPException(404,'会话游标不存在') from None

    @router.post('/qa/sessions')
    def new_session(request:Request,body:dict=Body(default_factory=dict)):
        owner=logged_in(request)
        if body:raise HTTPException(400,'新会话不接受身份参数')
        if history is None:raise HTTPException(503,'会话存储尚未配置')
        return {'ok':True,'data':history.create(owner)}

    @router.get('/qa/sessions/{session_id}')
    def session_detail(request:Request,session_id:str):
        owner=logged_in(request)
        if history is None:raise HTTPException(503,'会话存储尚未配置')
        try:return {'ok':True,'data':history.read(owner,session_id)}
        except LookupError:raise HTTPException(404,'会话不存在') from None

    def perform_answer(request,body):
        owner=logged_in(request)
        state = workflow.status()
        if body['question'].strip() in ('我的在谈概要', '本人在谈概要'):
            result = legacy_answer(request, body)
            result['data'].update(workflow_enabled=state['enabled'], notice='本人会话摘要由应用账本直接提供。' if state['enabled'] else state['hint'])
            return result
        if state['enabled']:
            try:return {'ok': True, 'data': workflow.answer(owner, body['question'].strip())}
            except WorkflowError as exc:
                raise HTTPException(502, '智能问答工作流未完成：' + str(exc) + '。未生成报价或发送询价。') from None
        result = legacy_answer(request, body)
        result['data'].update(workflow_enabled=False, notice=state['hint'])
        return result

    def legacy_answer(request: Request, body: dict):
        user = getattr(request.state, 'user', None)
        if user is None:
            raise HTTPException(401, '请先登录')
        if set(body) != {'question'} or not isinstance(body.get('question'), str) or not 1 <= len(body['question'].strip()) <= 200:
            raise HTTPException(400, '仅支持1至200字的question，不接受身份或查询参数')
        question = body['question'].strip()
        if re.search(r'\b(select|insert|update|delete|drop|alter|union)\b|;|--|/\*', question, re.I):
            raise HTTPException(400, '不接受SQL或脚本查询')
        owner = user.user_id
        if question in ('我的在谈概要', '本人在谈概要'):
            rows = [row for row in service.sessions(owner) if row['state'] not in ('CLOSED', 'STOPPED')]
            if not rows:
                return {'ok': True, 'data': {'answer': '本人当前没有在谈会话。来源：本人会话账本。'}}
            peers = {row['conversation_id']: row['peer_id'] for row in service.conversations(owner)}
            counts = Counter(row['state'] for row in rows)
            lines = [f'本人在谈会话共 {len(rows)} 个。来源：本人会话账本；状态为记录值。',
                     '状态数量：' + '；'.join(f'{_text(state)} {count}' for state, count in sorted(counts.items()))]
            lines.extend(f"- {_text(row.get('bond_code'))} · 对手 {_text(peers.get(row['conversation_id']))} · {_text(row['state'])}" for row in rows[:50])
            if len(rows) > 50:
                lines.append('这里只列前50条，请在监控页查看其余会话。')
            return {'ok': True, 'data': {'answer': '\n\n'.join(lines)}}
        match = re.fullmatch(r'(查券|报价)\s+([\w\u3400-\u9fff().（）·\- ]{1,80})', question)
        if not match:
            return {'ok': True, 'data': {'answer': HELP}}
        action, query = match[1], match[2].strip()
        page = market.page(owner, filter_spec={'keyword': query, 'channel': 'best'}, limit=20)
        rows = page['data']
        if action == '查券':
            reference = tools.invoke('bond_info', {'schema_version': '1', 'mode': 'search', 'query': query, 'limit': 20})
            candidates = (reference.get('data') or {}).get('candidates', [])
            lines = [f"- {_text(row.get('code'))} · {_text(row.get('name'))}（已接入行情）" for row in rows]
            known = {row['code'] for row in rows}
            lines.extend(f"- {_text(row.get('code'))} · {_text(row.get('name'))}（参考资料候选，需核对完整代码）" for row in candidates if row.get('code') not in known)
            if not lines:
                reason = page.get('emptyReason') or '未找到匹配债券'
                return {'ok': True, 'data': {'answer': f'{reason}；参考资料状态：{_text(reference.get("status"))}。未生成报价。'}}
            lines.insert(0, '查券结果（最多各20条，简称匹配须核对完整代码）：')
            if reference.get('status') == 'unavailable':
                lines.append('参考资料尚未接入或当前不可用，仅列已接入行情。')
            return {'ok': True, 'data': {'answer': '\n\n'.join(lines)}}
        exact = [row for row in rows if query.casefold() in (str(row['code']).casefold(), str(row['name']).casefold())]
        if len(exact) != 1:
            reason = page.get('emptyReason') or ('匹配不唯一，请先查券并指定完整代码' if rows else '未找到该券已接入的行情')
            return {'ok': True, 'data': {'answer': reason + '。未生成对比图。'}}
        row = exact[0]
        prices = [('Bid', _number(row.get('ob'))), ('Ofr', _number(row.get('oo')))]
        lines = [f"{_text(row['code'])} {_text(row['name'])}，来源：已接入市场最优收益率（%），并非成交价。",
                 '；'.join(f'{name}：{value:g}%' if value is not None else f'{name}：未接入或缺失' for name, value in prices),
                 f"源更新时间：{_text(row.get('issueTime'))}；此处不判断报价可成交性。"]
        data = {'answer': '\n\n'.join(lines)}
        known_prices = [(name, value) for name, value in prices if value is not None]
        if known_prices:
            data['chart'] = {'type': 'bar', 'title': f"{row['code']} 已接入 Bid / Ofr", 'yLabel': '收益率（%）',
                             'categories': [name for name, _ in known_prices],
                             'series': [{'name': '收益率（%）', 'data': [value for _, value in known_prices]}]}
        return {'ok': True, 'data': data}

    return router
