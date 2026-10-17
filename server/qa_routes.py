"""Bounded read-only questions over existing market facts and caller-owned sessions."""
from collections import Counter
from html import escape
import math
import re

from fastapi import APIRouter, Body, HTTPException, Request


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


def qa_router(service, market, tools):
    router = APIRouter(prefix='/api')

    @router.post('/qa')
    def answer(request: Request, body: dict = Body(default_factory=dict)):
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
