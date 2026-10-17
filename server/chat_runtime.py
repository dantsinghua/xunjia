"""Conversation-scoped Chat Agent harness.

The model chooses a proposed action; this module owns identity, versions,
price/quantity checks, durable memory and the final command transaction.
"""
from __future__ import annotations
import json
import re
import secrets
import time
from datetime import datetime, timezone
from copy import deepcopy
from zoneinfo import ZoneInfo

from .business import digest,encoded,identity
from .domain import decimal,goal

TOP_KEYS={'schema_version','turn_id','conversation_epoch','source_message_id','observations','proposed_actions','public_message','trader_view','memory_patch','history_request','validation_ref'}
ACTIONS={'SOCIAL_REPLY','CLARIFY_BOND','ASK_QUOTE','COUNTER','WAIT','REQUEST_HANDOFF'}
from .public_text import SECRET_TEXT, DEAL_TEXT


class GuardError(ValueError):
    pass


class ChatRunner:
    def __init__(self,service,client,tools=None,*,clock=time.time):
        self.service=service;self.client=client;self.tools=tools;self.clock=clock

    def _load(self,tx,ref,*,lock=True):
        row=tx.execute('SELECT * FROM xj_turn WHERE turn_ref=?',(ref,)).fetchone()
        if not row:raise PermissionError('本轮能力无效')
        binding=self.service._binding(tx,row['owner_user_id'],lock=lock)
        row=tx.execute('SELECT * FROM xj_turn WHERE turn_ref=?',(ref,)).fetchone()
        conv=tx.execute('SELECT * FROM xj_conversation WHERE conversation_id=?',(row['conversation_id'],)).fetchone()
        msg=tx.execute('SELECT * FROM xj_message WHERE message_id=?',(row['message_id'],)).fetchone()
        if (row['state']!='running' or row['expires_at']<=self.clock() or not binding['bot_enabled'] or conv['mode']!='AUTO'
            or conv['control_version']!=row['control_version'] or binding['version']!=row['binding_version']
            or conv['memory_revision']!=row['memory_revision'] or msg['processing']!='running'):
            raise ValueError('本轮授权已失效')
        snapshot=json.loads(row['snapshot_json'])
        for saved in snapshot['sessions']:
            current=tx.execute('SELECT revision FROM xj_session WHERE session_id=?',(saved['session_id'],)).fetchone()
            if not current or current['revision']!=saved['revision']:raise ValueError('询价状态已更新')
        return row,binding,conv,msg,snapshot

    def _start(self,message_id):
        with self.service.repo.db.transaction() as tx:
            msg=tx.execute('SELECT * FROM xj_message WHERE message_id=?',(message_id,)).fetchone()
            if not msg:raise ValueError('消息不存在')
            binding=self.service._binding(tx,msg['owner_user_id'])
            msg=tx.execute('SELECT * FROM xj_message WHERE message_id=?',(message_id,)).fetchone()
            previous=tx.execute('SELECT * FROM xj_turn WHERE message_id=?',(message_id,)).fetchone()
            if previous:return None
            conv=tx.execute('SELECT * FROM xj_conversation WHERE conversation_id=?',(msg['conversation_id'],)).fetchone()
            busy=tx.execute("SELECT turn_id FROM xj_turn WHERE conversation_id=? AND state='running' LIMIT 1",(conv['conversation_id'],)).fetchone()
            pending=tx.execute("SELECT command_id FROM xj_command WHERE conversation_id=? AND status IN ('queued','pending','unknown') LIMIT 1",(conv['conversation_id'],)).fetchone()
            if busy or pending:return None
            if msg['processing']!='queued' or not binding['bot_enabled'] or conv['mode']!='AUTO' or not conv['social_authorized'] or msg['source_seq']<=conv['resume_seq']:return None
            older=tx.execute("SELECT message_id FROM xj_message WHERE conversation_id=? AND processing='queued' AND source_seq<? LIMIT 1",(conv['conversation_id'],msg['source_seq'])).fetchone()
            if older:return None
            rows=tx.execute("SELECT * FROM xj_session WHERE conversation_id=? AND archived=0 AND state='NEGOTIATING' ORDER BY session_id",(conv['conversation_id'],)).fetchall()
            trade_day=datetime.fromtimestamp(self.clock(),ZoneInfo('Asia/Shanghai')).date()
            old=[r for r in rows if datetime.fromtimestamp(r['created_at'],ZoneInfo('Asia/Shanghai')).date()!=trade_day]
            if old:
                for stale in old:
                    tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE session_id=?",(stale['session_id'],))
                self.service._audit(tx,msg['owner_user_id'],'crossday_reconfirmation',conv['conversation_id'],{'session_ids':[r['session_id'] for r in old]})
                if re.search(r'报价|收益|bp|买|卖|净价|全价|\d+(?:\.\d+)?%',msg['text'],re.I):
                    tx.execute("UPDATE xj_message SET processing='ignored' WHERE message_id=?",(message_id,))
                    return None
                rows=[r for r in rows if r not in old]
            sessions=[{'session_id':r['session_id'],'bond_code':r['bond_code'],'revision':r['revision'],'authorized_at':r['created_at'],'policy':json.loads(r['policy_json']),'peer_quote':json.loads(r['peer_quote_json']),'our_quote':json.loads(r['our_quote_json'])} for r in rows]
            for session in sessions:
                quote=session['peer_quote']
                evidence=tx.execute('SELECT occurred_at,processing FROM xj_message WHERE message_id=? AND conversation_id=?',(quote.get('source_message_id'),conv['conversation_id'])).fetchone() if quote.get('source_message_id') else None
                session['peer_quote_expires_at']=evidence['occurred_at']+300 if evidence else 0
                session['peer_quote_usable']=bool(evidence and evidence['processing'] not in ('revoked','failed','uncertain') and -60<=self.clock()-evidence['occurred_at']<=300 and quote.get('yield_pct') is not None)
            tid=identity('turn_');ref=secrets.token_urlsafe(48);budget=secrets.token_urlsafe(48)
            snapshot={'sessions':sessions,'source_seq':msg['source_seq']}
            tx.execute('INSERT INTO xj_turn(turn_id,message_id,owner_user_id,conversation_id,turn_ref,tool_budget_ref,state,control_version,memory_revision,binding_version,snapshot_json,data_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(tid,message_id,msg['owner_user_id'],conv['conversation_id'],ref,budget,'running',conv['control_version'],conv['memory_revision'],binding['version'],encoded(snapshot),encoded({'history_count':0}),self.clock(),self.clock()+300))
            tx.execute("UPDATE xj_message SET processing='running' WHERE message_id=?",(message_id,))
            return ref

    def context(self,turn_ref):
        with self.service.repo.db.transaction(write=False) as tx:
            row,binding,conv,msg,snapshot=self._load(tx,turn_ref,lock=False)
            messages=tx.execute("SELECT message_id,text,direction,occurred_at,source_event_id FROM xj_message WHERE conversation_id=? AND processing NOT IN ('revoked','duplicate') AND source_seq<=? ORDER BY occurred_at DESC,message_id DESC LIMIT 30",(conv['conversation_id'],msg['source_seq'])).fetchall()
            # Outgoing messages have source_seq=0 but include only real sent facts.
            history=[dict(m) for m in reversed(messages)]
            ambiguity=json.loads(conv['ambiguity_json'])
            issue=ambiguity.get('issue_id') or 'ambiguity_'+digest([conv['conversation_id'],[s['session_id'] for s in snapshot['sessions']]])[:24]
            return {'turn_id':row['turn_id'],'conversation_epoch':row['control_version'],'source_message_id':msg['message_id'],
                    'control_version':conv['control_version'],'binding_version':binding['version'],'memory_revision':conv['memory_revision'],
                    'sessions':snapshot['sessions'],'messages':history,'current_message':{'message_id':msg['message_id'],'text':msg['text'],'source_seq':msg['source_seq']},
                    'memory':json.loads(conv['memory_json']),'ambiguity_issue_id':issue,'ambiguity':ambiguity,
                    'history_used':json.loads(row['data_json']).get('history_count',0),'tool_budget_ref':row['tool_budget_ref']}

    def history(self,turn_ref,request):
        if not isinstance(request,dict) or set(request)-{'message_refs','keywords','date_range','reason'}:raise ValueError('历史查询条件无效')
        refs=request.get('message_refs',[]);keywords=request.get('keywords',[])
        if not isinstance(refs,list) or len(refs)>50 or any(not isinstance(v,str) for v in refs):raise ValueError('消息引用无效')
        if not isinstance(keywords,list) or len(keywords)>5 or any(not isinstance(v,str) or len(v)>100 for v in keywords):raise ValueError('关键词无效')
        with self.service.repo.db.transaction() as tx:
            row,_,conv,_,_=self._load(tx,turn_ref)
            data=json.loads(row['data_json'])
            if data.get('history_count',0)>=1:raise ValueError('本轮历史补取额度已用完')
            data['history_count']=1
            tx.execute('UPDATE xj_turn SET data_json=? WHERE turn_id=?',(encoded(data),row['turn_id']))
            lo,hi=self.clock()-30*86400,self.clock()
            dates=request.get('date_range')
            if dates is not None:
                if not isinstance(dates,dict) or set(dates)!={'start','end'}:raise ValueError('日期范围须包含start/end')
                try:
                    start=datetime.fromisoformat(dates['start'].replace('Z','+00:00'))
                    end=datetime.fromisoformat(dates['end'].replace('Z','+00:00'))
                    if start.tzinfo is None:start=start.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                    if end.tzinfo is None:end=end.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                    lo,hi=start.timestamp(),end.timestamp()
                except (ValueError,AttributeError,TypeError):raise ValueError('日期范围无效') from None
                if lo>hi or hi>self.clock() or lo<self.clock()-365*86400:raise ValueError('历史查询超出保留范围')
            args=[conv['conversation_id'],lo,hi]
            sql="SELECT message_id,text,direction,occurred_at FROM xj_message WHERE conversation_id=? AND processing NOT IN ('revoked','duplicate') AND occurred_at>=? AND occurred_at<=?"
            if refs:sql+=' AND message_id IN ('+','.join('?' for _ in refs)+')';args.extend(refs)
            if keywords:
                sql+=' AND ('+' OR '.join("text LIKE ? ESCAPE '!'" for _ in keywords)+')'
                args.extend('%'+v.replace('!','!!').replace('%','!%').replace('_','!_')+'%' for v in keywords)
            sql+=' ORDER BY occurred_at DESC,message_id DESC LIMIT 50'
            found=[dict(r) for r in tx.execute(sql,tuple(args)).fetchall()]
            self.service._audit(tx,row['owner_user_id'],'history_read',row['turn_id'],{'count':len(found)})
            return {'messages':list(reversed(found)),'remaining':0}

    @staticmethod
    def _schema(proposal):
        if not isinstance(proposal,dict) or set(proposal)!=TOP_KEYS:raise ValueError('AgentTurn字段不完整或包含额外字段')
        if proposal['schema_version']!='1' or type(proposal['conversation_epoch']) is not int:raise ValueError('AgentTurn版本无效')
        for field in ('observations','proposed_actions'):
            if not isinstance(proposal[field],list) or len(proposal[field])>10:raise ValueError('AgentTurn明细无效')
        if proposal['public_message'] is not None and (not isinstance(proposal['public_message'],str) or not proposal['public_message'].strip() or len(proposal['public_message'])>1500):raise ValueError('公开正文无效')
        if not isinstance(proposal['trader_view'],dict):raise ValueError('交易员视图无效')
        for field in ('memory_patch','history_request'):
            if proposal[field] is not None and not isinstance(proposal[field],dict):raise ValueError('AgentTurn补充字段无效')

    def _check(self,row,conv,msg,snapshot,p):
        self._schema(p)
        if (p['turn_id']!=row['turn_id'] or p['conversation_epoch']!=row['control_version'] or p['source_message_id']!=row['message_id']):raise GuardError('AgentTurn身份不匹配')
        if p['history_request'] is not None:
            if p['public_message'] or any(a != {'type':'WAIT'} for a in p['proposed_actions']) or p['observations']:raise GuardError('补历史轮不能产生交易动作')
            return {'actions':[],'observations':[],'handoff':False,'issue':None}
        sessions={s['session_id']:s for s in snapshot['sessions']}
        explicit=[s for s in sessions.values() if s['bond_code'] in msg['text'] or s['policy']['name'] in msg['text']]
        anchor=json.loads(conv['memory_json']).get('active_bond_session_id')
        selected=explicit[0]['session_id'] if len(explicit)==1 else (anchor if anchor in sessions else next(iter(sessions)) if len(sessions)==1 else None) if not explicit else None
        known_mentions=snapshot.get('bond_mentions',[])
        active_codes={s['bond_code'] for s in sessions.values()}
        if any(code not in active_codes for code in known_mentions):selected=None
        hints=re.findall(r'(?<!\d)\d{2}[\u4e00-\u9fffA-Za-z]{2,16}\d{1,3}(?!\d)',msg['text'])
        if any(not any(hint in s['policy']['name'] for s in sessions.values()) for hint in hints):selected=None
        # If the counterparty refers to an unknown code, the single live session
        # is not evidence that their message concerns that session.
        mentioned=re.findall(r'\b[A-Z0-9]{4,12}\.(?:IB|SH|SZ|BJ)\b',msg['text'])
        if any(code not in {s['bond_code'] for s in sessions.values()} for code in mentioned):selected=None
        for bare in re.finditer(r'(?<![\d.])\d{6,9}(?![\d.])',msg['text']):
            tail=msg['text'][bare.end():].lstrip()
            if tail.startswith(('万','亿','元','%','％')):continue
            if bare.group() not in {s['bond_code'].split('.')[0] for s in sessions.values()}:selected=None
        if not explicit and re.search(r'另一只|另一个|那只|那个|前一个|后一个',msg['text']):selected=None
        observations=[];handoff=False
        explicit_buy=bool(re.search(r'(?:我(?:方)?|我们)?(?:买入|买|收)(?:价)?\s*[-\d]|\bbid\b',msg['text'],re.I))
        explicit_sell=bool(re.search(r'(?:我(?:方)?|我们)?(?:卖出|卖|出)(?:价)?\s*[-\d]|\b(?:ofr|offer)\b',msg['text'],re.I))
        terms_changed=bool(re.search(r'交割|清算|结算|付款|付息|过户|T\s*\+\s*\d|今天交|明天交',msg['text'],re.I))
        amounts=[decimal(v)*(10000 if unit=='亿' else 1 if unit=='万' else decimal('0.0001')) for v,unit in re.findall(r'(\d+(?:\.\d+)?)\s*(亿|万|元)',msg['text'])]
        explicit_yields=[decimal(v) for v in re.findall(r'(-?\d+(?:\.\d+)?)\s*[%％]',msg['text'])]
        bare_yields=[decimal(v) for v in re.findall(r'(?<![\d.])(-?\d+\.\d+)(?![\d.])',msg['text'])]
        raw_yields=list(dict.fromkeys(explicit_yields or bare_yields))
        source_has_price=bool(raw_yields or re.search(r'净价|全价|收益率|基点|bp',msg['text'],re.I))
        if terms_changed or re.search(r'净价|全价|行权',msg['text']):handoff=True
        if selected and any(v>decimal(sessions[selected]['policy']['volume_wan']) for v in amounts):handoff=True
        if selected and source_has_price and sessions[selected]['policy'].get('strict_match',True) and any(v!=decimal(sessions[selected]['policy']['volume_wan']) for v in amounts):handoff=True
        if source_has_price and not amounts:
            # Missing quantity is not evidence of the approved volume. A
            # quotation without an explicit amount cannot authorize a counter.
            handoff=True
        if source_has_price and (not p['observations'] or self.clock()-msg['occurred_at']>300):handoff=True
        for obs in p['observations']:
            if not isinstance(obs,dict):raise GuardError('观察无效')
            sid=obs.get('rfq_session_id');session=sessions.get(sid)
            if session and msg['occurred_at']<session.get('authorized_at',row['created_at']):raise GuardError('源报价早于当前券授权')
            if not session or sid!=selected or obs.get('bond_id') not in (session['bond_code'],session.get('bond_id')) or obs.get('attribution')!='unique':raise GuardError('报价不能唯一归属')
            refs=obs.get('evidence_refs');quote=obs.get('peer_quote')
            if not isinstance(refs,list) or refs != [msg['message_id']] or not isinstance(quote,dict):raise GuardError('报价缺少当前源消息证据')
            if quote.get('source_message_id')!=msg['message_id'] or quote.get('side') not in ('bid','ofr') or type(quote.get('firm')) is not bool:raise GuardError('报价证据字段无效')
            if explicit_buy and not explicit_sell and quote['side']!='bid':raise GuardError('对手明确买入，不能解释为卖价')
            if explicit_sell and not explicit_buy and quote['side']!='ofr':raise GuardError('对手明确卖出，不能解释为买价')
            if explicit_buy and explicit_sell:raise GuardError('双向报价须逐边核对，不能简化归属')
            value=decimal(quote.get('value'),'对手报价')
            nums=[decimal(n) for n in re.findall(r'(?<![\d.])-?\d{1,8}(?:\.\d{1,8})?(?![\d.])',msg['text'])]
            if value not in nums:raise GuardError('源消息不含所报价格')
            if quote.get('kind')=='net_price':handoff=True;normalized=None
            elif quote.get('kind')=='bp':
                valuation=session.get('valuation')
                if not valuation or re.search(r'行权|净价|全价',msg['text']) or not re.search(r'bp|基点',msg['text'],re.I):handoff=True;normalized=None
                else:normalized=str(decimal(valuation['yield_pct'])+value/100)
            elif quote.get('kind')=='yield':
                if re.search(r'净价|全价|基点|bp',msg['text'],re.I):raise GuardError('源文报价口径与收益率观察不一致')
                if len(raw_yields)!=1 or value!=raw_yields[0]:raise GuardError('收益率观察未完整且唯一对应原文价格')
                normalized=str(value)
            else:raise GuardError('报价口径无效')
            expected='ofr' if session['policy']['our_side']=='BUY' else 'bid'
            if quote['side']!=expected:handoff=True
            derived=goal(session['policy']['our_side'],session['policy']['limit_yield_pct'],quote['side'],normalized,valid=normalized is not None)
            handoff=handoff or derived['target_reached']
            observations.append({'session_id':sid,'quote':{'yield_pct':normalized,'kind':quote['kind'],'value':str(value),'side':quote['side'],'firm':quote['firm'],'source_message_id':msg['message_id'],'quoted_at':datetime.fromtimestamp(msg['occurred_at'],timezone.utc).isoformat(),'freshness':'本轮对手消息'},'goal':derived})
        actions=[];issue=None
        for action in p['proposed_actions']:
            if not isinstance(action,dict) or action.get('type') not in ACTIONS:raise GuardError('行动类型无效')
            if set(action)-{'type','rfq_session_id','yield_pct','volume_wan','ambiguity_issue_id'}:raise GuardError('行动字段无效')
            kind=action['type'];sid=action.get('rfq_session_id')
            if kind in ('COUNTER','ASK_QUOTE'):
                if not sid or sid!=selected or sid not in sessions:raise GuardError('交易动作未能唯一归属')
                session=sessions[sid];policy=session['policy']
                if msg['occurred_at']<session.get('authorized_at',row['created_at']):raise GuardError('本轮源消息早于当前询价授权')
                if handoff or terms_changed or any(v>decimal(policy['volume_wan']) for v in amounts):raise GuardError('已触发人工处理条件，不得继续还价')
                if policy.get('strict_match',True) and any(v!=decimal(policy['volume_wan']) for v in amounts):raise GuardError('对手报量与本次严格匹配授权不一致')
                if kind=='ASK_QUOTE' and action.get('yield_pct') is not None:raise GuardError('询价动作不能夹带未核验还价')
                if kind=='COUNTER' and not any(o['session_id']==sid for o in observations):
                    peer=session['peer_quote']
                    if source_has_price or not session.get('peer_quote_usable') or self.clock()>session.get('peer_quote_expires_at',0):raise GuardError('还价缺少当前或仍有效的已核验对手价')
                    previous_goal=goal(policy['our_side'],policy['limit_yield_pct'],peer.get('side'),peer.get('yield_pct'))
                    if previous_goal['raw_gap_bp'] is None or previous_goal['target_reached']:raise GuardError('上下文报价已触发停止条件')
                if kind=='COUNTER':
                    price=decimal(action.get('yield_pct'),'候选收益率');limit=decimal(policy['limit_yield_pct'])
                    previous=decimal(session['our_quote'].get('yield_pct',policy['opening_yield_pct']))
                    step=decimal(policy['step_bp'])/100
                    if policy['our_side']=='BUY' and (price<limit or price>previous or previous-price>step):raise GuardError('买入候选超授权边界或步幅')
                    if policy['our_side']=='SELL' and (price>limit or price<previous or price-previous>step):raise GuardError('卖出候选超授权边界或步幅')
                volume=decimal(action.get('volume_wan',policy['volume_wan']))
                if not 0<volume<=decimal(policy['volume_wan']):raise GuardError('数量超授权上限')
            elif kind=='CLARIFY_BOND':
                ambiguity=json.loads(conv['ambiguity_json'])
                issue=ambiguity.get('issue_id') or 'ambiguity_'+digest([conv['conversation_id'],list(sessions)])[:24]
                if action.get('ambiguity_issue_id')!=issue:raise GuardError('歧义问题标识无效')
                if ambiguity.get('count',0)>=1:raise GuardError('该歧义已自动澄清，须转人工')
                if action.get('yield_pct') is not None or action.get('volume_wan') is not None:raise GuardError('澄清不得包含报价承诺')
            elif kind=='REQUEST_HANDOFF':handoff=True
            elif kind=='SOCIAL_REPLY':
                if not conv['social_authorized']:raise GuardError('未授权关系维护')
                if re.search(r'报价|收益|bp|买|卖|净价|全价|\d+(?:\.\d+)?%',msg['text'],re.I):raise GuardError('交易内容不能绕过社交分支')
            actions.append(action)
        outgoing=[a for a in actions if a['type'] not in ('WAIT','REQUEST_HANDOFF')]
        if len(outgoing)>1:raise GuardError('每轮最多一个公开行动')
        text=p['public_message']
        if text:
            if not actions or all(a['type']=='WAIT' for a in actions):raise GuardError('无公开行动不能附带正文')
            if SECRET_TEXT.search(text) or DEAL_TEXT.search(text):raise GuardError('公开正文涉及敏感信息或成交承诺')
            allowed_numbers=set()
            for action in actions:
                session=sessions.get(action.get('rfq_session_id'))
                if session:
                    allowed_numbers.update(re.findall(r'\d+(?:\.\d+)?',session['bond_code']+' '+session['policy']['name']))
                    for v in (action.get('yield_pct'),action.get('volume_wan',session['policy']['volume_wan'])):
                        if v is not None:allowed_numbers.add(str(decimal(v)))
            if any(a['type']=='CLARIFY_BOND' for a in actions):
                for session in sessions.values():allowed_numbers.update(re.findall(r'\d+(?:\.\d+)?',session['bond_code']+' '+session['policy']['name']))
            permitted={decimal(n) for n in allowed_numbers}
            for n in re.findall(r'-?\d+(?:\.\d+)?',text):
                if decimal(n) not in permitted:raise GuardError('公开正文含未授权数字')
            trading=[a for a in actions if a['type'] in ('COUNTER','ASK_QUOTE')]
            for action in trading:
                session=sessions[action['rfq_session_id']]
                expected_side='买' if session['policy']['our_side']=='BUY' else '卖'
                expressed=re.findall(r'我(?:们|方)?(?:考虑|想|愿意)?\s*(买|卖)',text)
                if expressed and set(expressed)!={expected_side}:raise GuardError('公开交易方向与授权不一致')
                public_codes=re.findall(r'\b[A-Z0-9]{4,12}\.(?:IB|SH|SZ|BJ)\b',text,re.I)
                if any(code.upper()!=session['bond_code'].upper() for code in public_codes):raise GuardError('公开正文债券代码与授权对象不一致')
                direction_text=text.replace(session['bond_code'],'').replace(session['policy']['name'],'')
                tokens=re.findall(r'买|卖|\bbuy\b|\bbid\b|\bsell\b|\bofr\b|\boffer\b',direction_text,re.I)
                normalized={'买' if t.lower() in ('买','buy','bid') else '卖' for t in tokens}
                if normalized and normalized!={expected_side}:raise GuardError('公开方向省略主语或与授权冲突')
                shorthand=re.findall(r'(?:^|[ ，,:：])(收|出)(?=\s*\d)',direction_text)
                if any(('买' if word=='收' else '卖')!=expected_side for word in shorthand):raise GuardError('公开方向简称与授权冲突')
                quantity_text=text.replace(session['bond_code'],'').replace(session['policy']['name'],'')
                if re.search(r'亿|兆|千|百|\d\s*(?:元|块|张|手)',quantity_text):raise GuardError('公开数量单位未经授权')
                quantities=[decimal(v) for v in re.findall(r'(\d+(?:\.\d+)?)\s*万(?:元)?',text)]
                permitted_volume=decimal(action.get('volume_wan',session['policy']['volume_wan']))
                if any(q!=permitted_volume for q in quantities):raise GuardError('公开数量与结构化动作不一致')
            if not trading and re.search(r'我(?:们|方)?(?:考虑|想|愿意)?\s*(?:买|卖)|\d\s*(?:万|亿)|报价|收益率|净价|全价',text):raise GuardError('非交易分支不能夹带交易承诺')
            counters=[a for a in actions if a['type']=='COUNTER']
            if counters:
                candidate=decimal(counters[0]['yield_pct'])
                percent=[decimal(n) for n in re.findall(r'(-?\d+(?:\.\d+)?)\s*[%％]',text)]
                if percent!=[candidate]:raise GuardError('公开收益率必须明确且匹配候选动作')
                session=sessions[counters[0]['rfq_session_id']]
                expected_side='买' if session['policy']['our_side']=='BUY' else '卖'
                expressed=re.findall(r'我(?:们|方)?(?:考虑|想|愿意)?\s*(买|卖)',text)
                if not expressed or set(expressed)!={expected_side}:raise GuardError('公开交易方向与授权不一致或不明确')
                if session['bond_code'] not in text and session['policy']['name'] not in text:raise GuardError('公开还价须明确债券')
                quantity_text=text.replace(session['bond_code'],'').replace(session['policy']['name'],'')
                if re.search(r'亿|兆|千|百|\d\s*(?:元|块|张|手)',quantity_text):raise GuardError('公开数量只能使用已核验的万单位')
                if re.search(r'净价|全价|bp|基点',quantity_text,re.I):raise GuardError('公开还价仅允许已核验的收益率口径')
                quantities=[decimal(v) for v in re.findall(r'(\d+(?:\.\d+)?)\s*万(?:元)?',text)]
                permitted_volume=decimal(counters[0].get('volume_wan',session['policy']['volume_wan']))
                if any(q!=permitted_volume for q in quantities):raise GuardError('公开数量与结构化动作不一致')
            elif re.search(r'%|％|bp|收益率|净价|全价',text,re.I):raise GuardError('非还价动作不得夹带价格')
            if any(a['type']=='CLARIFY_BOND' for a in actions) and re.search(r'%|％|bp|收益率|买入|卖出|\d+万',text,re.I):raise GuardError('澄清正文不得报价')
        patch=p['memory_patch']
        if patch is not None:
            if set(patch)-{'expected_revision','summary','evidence_refs'} or patch.get('expected_revision')!=conv['memory_revision']:raise GuardError('记忆版本或字段无效')
            if not isinstance(patch.get('summary'),str) or len(patch['summary'])>4000 or not isinstance(patch.get('evidence_refs'),list) or patch['evidence_refs'] != [msg['message_id']]:raise GuardError('记忆须仅引用已核对的本轮来源')
        return {'actions':actions,'observations':observations,'handoff':handoff,'issue':issue,'selected':selected,'resolved_explicit':len(explicit)==1 and bool(observations)}

    def validate(self,turn_ref,proposal):
        with self.service.repo.db.transaction() as tx:
            row,_,conv,msg,snapshot=self._load(tx,turn_ref)
            checked=self._check(row,conv,msg,snapshot,proposal)
            if proposal['public_message'] is None:
                public={'allowed_actions':checked['actions'],'peer_message':msg['text'],
                        'public_facts':[{'code':s['bond_code'],'name':s['policy']['name']} for s in snapshot['sessions']], 'relationship_context':{}}
                return {'ok':True,'validation_ref':None,'public_context':public,'needs_public_reply':any(a['type']!='WAIT' for a in checked['actions'])}
            data=json.loads(row['data_json']);ref=secrets.token_urlsafe(32)
            candidate={**proposal,'validation_ref':None}
            data['validation']={'ref':ref,'digest':digest(candidate)}
            tx.execute('UPDATE xj_turn SET data_json=? WHERE turn_id=?',(encoded(data),row['turn_id']))
            return {'ok':True,'validation_ref':ref}

    def process(self,message_id):
        ref=self._start(message_id)
        if ref is None:return {'status':'not_runnable'}
        stage='context'
        try:
            self._enrich(ref)
            ctx=self.context(ref)
            with self.service.repo.db.transaction(write=False) as tx:
                row=tx.execute('SELECT owner_user_id,conversation_id FROM xj_turn WHERE turn_ref=?',(ref,)).fetchone()
                conv=tx.execute('SELECT memory_json FROM xj_conversation WHERE conversation_id=?',(row['conversation_id'],)).fetchone()
            memory=json.loads(conv['memory_json'])
            inputs={'turn_ref':ref,'schema_version':'1','context_epoch':str(ctx['conversation_epoch'])}
            user=digest([row['owner_user_id'],row['conversation_id']])
            stage='generation'
            result=self.client.chat(query=ctx['current_message']['text'],user=user,inputs=inputs,conversation_id=memory.get('dify_conversation_id'),response_mode='streaming')
            p=result.proposal if hasattr(result,'proposal') else result
            if p.get('history_request') is not None:
                with self.service.repo.db.transaction() as tx:
                    values=self._load(tx,ref);self._check(values[0],values[2],values[3],values[4],p)
                extra=self.history(ref,p['history_request'])
                result=self.client.chat(query=ctx['current_message']['text'],user=user,inputs={**inputs,'history':encoded(extra)},conversation_id=getattr(result,'conversation_id',memory.get('dify_conversation_id')),response_mode='streaming')
                p=result.proposal if hasattr(result,'proposal') else result
                if p.get('history_request') is not None:raise ValueError('补历史最多一轮')
            stage='commit'
            outcome=self._commit(ref,p,getattr(result,'conversation_id',None))
            self.service.diagnostics.record('chat',owner=row['owner_user_id'])
            return outcome
        except Exception as exc:
            reason=str(exc) if isinstance(exc,GuardError) else None
            self._fail(ref,code='CHAT_VALIDATION_REJECTED' if isinstance(exc,GuardError) else 'CHAT_'+stage.upper()+'_FAILED',error=exc,reason=reason)
            return {'status':'handoff','reason':'本轮未通过完整验证，已停止自动回复'}

    def _fail(self,ref,*,code="CHAT_TURN_EXPIRED",error=None,reason=None):
        with self.service.repo.db.transaction() as tx:
            row=tx.execute('SELECT * FROM xj_turn WHERE turn_ref=?',(ref,)).fetchone()
            if not row:return
            self.service._binding(tx,row['owner_user_id'])
            tx.execute("UPDATE xj_turn SET state='failed' WHERE turn_id=? AND state='running'",(row['turn_id'],))
            tx.execute("UPDATE xj_message SET processing='failed' WHERE message_id=? AND processing='running'",(row['message_id'],))
            # Do not undo a concurrent takeover/stop/new epoch.
            changed=tx.execute("UPDATE xj_conversation SET mode='HUMAN',control_version=control_version+1 WHERE conversation_id=? AND control_version=? AND mode='AUTO'",(row['conversation_id'],row['control_version'])).rowcount
            if changed:
                tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing='queued'",(row['conversation_id'],))
                tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND state='NEGOTIATING'",(row['conversation_id'],))
            self.service._audit(tx,row['owner_user_id'],'agent_failed',row['turn_id'],{'code':code,'reason':reason or '本轮未完整通过核验','error_type':type(error).__name__ if error else None})
            self.service.diagnostics.record('chat',owner=row['owner_user_id'],code=code,error=error,entity=row['turn_id'])

    def _commit(self,ref,p,dify_id):
        with self.service.repo.db.transaction() as tx:
            row,binding,conv,msg,snapshot=self._load(tx,ref)
            checked=self._check(row,conv,msg,snapshot,p)
            data=json.loads(row['data_json'])
            if p['public_message']:
                validation=data.get('validation',{})
                if not p['validation_ref'] or p['validation_ref']!=validation.get('ref') or digest({**p,'validation_ref':None})!=validation.get('digest'):raise ValueError('最终正文无匹配核验凭据')
            elif p['validation_ref'] is not None:raise ValueError('内部结果不得复用核验凭据')
            for obs in checked['observations']:
                tx.execute('UPDATE xj_session SET peer_quote_json=? WHERE session_id=?',(encoded(obs['quote']),obs['session_id']))
            anchored={o['session_id'] for o in checked['observations']}|{a['rfq_session_id'] for a in checked['actions'] if a.get('rfq_session_id')}
            message_data=json.loads(msg['data_json'])
            message_data['contexts']=[{'code':s['bond_code'],'name':s['policy']['name'],'session_id':s['session_id']} for s in snapshot['sessions'] if s['session_id'] in anchored]
            message_data['turn_id']=row['turn_id']
            tx.execute('UPDATE xj_message SET data_json=? WHERE message_id=?',(encoded(message_data),msg['message_id']))
            memory=json.loads(conv['memory_json'])
            if p['memory_patch']:memory.update(summary=p['memory_patch']['summary'],evidence_refs=p['memory_patch']['evidence_refs'])
            if dify_id:memory['dify_conversation_id']=dify_id
            if checked.get('resolved_explicit'):memory['active_bond_session_id']=checked['selected']
            tx.execute('UPDATE xj_conversation SET memory_json=?,memory_revision=memory_revision+1 WHERE conversation_id=?',(encoded(memory),conv['conversation_id']))
            if checked['issue']:
                tx.execute('UPDATE xj_conversation SET ambiguity_json=? WHERE conversation_id=?',(encoded({'issue_id':checked['issue'],'count':1,'source_message_id':msg['message_id']}),conv['conversation_id']))
            elif checked.get('resolved_explicit'):
                tx.execute("UPDATE xj_conversation SET ambiguity_json='{}' WHERE conversation_id=?",(conv['conversation_id'],))
            command_id=None
            if p['public_message']:
                command_id='cmd_'+digest(row['turn_id'])[:32]
                affected={a['rfq_session_id'] for a in checked['actions'] if a.get('rfq_session_id')}
                revisions={s['session_id']:s['revision'] for s in snapshot['sessions'] if s['session_id'] in affected}
                body={'workbench_id':binding['workbench_id'],'binding_version':binding['version'],'transport':binding['transport'],
                      'receiver_fence_id':conv['receiver_fence_id'] or '', 'receiver_fence_version':conv['receiver_fence_version'], 'turn_id':row['turn_id'],'text':p['public_message'],'role':'bot','session_ids':list(revisions),'session_revisions':revisions,
                      'quote_updates':{a['rfq_session_id']:a['yield_pct'] for a in checked['actions'] if a['type']=='COUNTER'}}
                tx.execute('INSERT INTO xj_command(command_id,owner_user_id,account_uid,peer_id,conversation_id,kind,request_id,content_hash,body_json,status,control_version,created_at,expires_at,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(command_id,row['owner_user_id'],binding['account_uid'],conv['peer_id'],conv['conversation_id'],'send_text',row['turn_id'],digest(body),encoded(body),'queued',conv['control_version'],self.clock(),self.clock()+120,'{}'))
                for sid in revisions:tx.execute("UPDATE xj_session SET pending_command_id=?,state='WAITING_SEND' WHERE session_id=?",(command_id,sid))
            if checked['handoff']:
                tx.execute("UPDATE xj_message SET processing='ignored' WHERE conversation_id=? AND processing='queued'",(conv['conversation_id'],))
                tx.execute("UPDATE xj_session SET state='HANDOFF',revision=revision+1 WHERE conversation_id=? AND archived=0 AND state NOT IN ('CLOSED','STOPPED')",(conv['conversation_id'],))
                # Handoff is an internal state transition; it never requires a
                # courtesy message to succeed. Any queued candidate is cancelled.
                tx.execute("UPDATE xj_conversation SET mode='HUMAN',control_version=control_version+1 WHERE conversation_id=?",(conv['conversation_id'],))
                tx.execute("UPDATE xj_command SET status='cancelled' WHERE conversation_id=? AND status='queued'",(conv['conversation_id'],))
            tx.execute("UPDATE xj_turn SET state='done',data_json=? WHERE turn_id=?",(encoded({**data,'result':checked,'command_id':command_id}),row['turn_id']))
            tx.execute("UPDATE xj_message SET processing='done' WHERE message_id=?",(msg['message_id'],))
            self.service._audit(tx,row['owner_user_id'],'agent_committed',row['turn_id'],{'command_id':command_id,'handoff':checked['handoff']})
            return {'status':'handoff' if checked['handoff'] else 'queued' if command_id else 'done','command_id':command_id}

    def invoke_tool(self,tool_budget_ref,name,arguments):
        if name not in ('bond_info','similar_bonds','issuer_info','bond_calc'):raise ValueError('不支持的只读工具')
        with self.service.repo.db.transaction() as tx:
            found=tx.execute('SELECT turn_ref FROM xj_turn WHERE tool_budget_ref=?',(tool_budget_ref,)).fetchone()
            if not found:raise PermissionError('工具预算票据无效')
            row,_,_,_,_=self._load(tx,found['turn_ref'])
            data=json.loads(row['data_json']);used=data.get('tool_calls',0)
            if used>=6:raise ValueError('本轮最多6次只读工具调用')
            data['tool_calls']=used+1
            tx.execute('UPDATE xj_turn SET data_json=? WHERE turn_id=?',(encoded(data),row['turn_id']))
        if self.tools is None:raise ValueError('只读工具未配置')
        return self.tools.invoke(name,arguments)

    def _enrich(self,ref):
        """Capture one trusted reference snapshot outside the business lock."""
        if self.tools is None:return
        try:
            raw=deepcopy(self.tools.provider.snapshot())
            from .reference_data import ReferenceSnapshot
            if not isinstance(raw,ReferenceSnapshot):return
            source_ids={s['id'] for s in raw.sources if isinstance(s,dict) and isinstance(s.get('id'),str)}
            with self.service.repo.db.transaction(write=False) as tx:
                row,_,_,message,snapshot=self._load(tx,ref,lock=False)
            today=datetime.fromtimestamp(self.clock(),timezone.utc).date()
            mentions=[]
            for bond in raw.bonds:
                if not isinstance(bond,dict):continue
                names=[bond.get('code'),bond.get('name')]+(bond.get('aliases') if isinstance(bond.get('aliases'),list) else [])
                if any(isinstance(name,str) and len(name)>=3 and name in message['text'] for name in names):mentions.append(bond.get('code'))
            snapshot['bond_mentions']=list(dict.fromkeys(code for code in mentions if isinstance(code,str)))
            for session in snapshot['sessions']:
                if session['policy'].get('pricing_basis') == 'bp':
                    # A confirmed BP policy must never silently rebase itself on
                    # a newer, unrelated reference-data snapshot.
                    pinned = session['policy'].get('valuation_snapshot')
                    if isinstance(pinned,dict):
                        session['valuation'] = dict(pinned)
                    continue
                matches=[b for b in raw.bonds if isinstance(b,dict) and b.get('code')==session['bond_code']]
                if len(matches)!=1:continue
                session['bond_id']=matches[0].get('bond_id')
                data=matches[0].get('valuation') or {}
                fields={}
                for key in ('provider','basis','yield_pct','valuation_date'):
                    fact=data.get(key)
                    if not isinstance(fact,dict) or fact.get('quality')!='verified' or fact.get('source_ref') not in source_ids:break
                    fields[key]=fact.get('value')
                if len(fields)!=4 or fields['provider'] not in ('中债','CNBD','ChinaBond') or fields['basis']!='maturity':continue
                value=decimal(fields['yield_pct'])
                day=datetime.fromisoformat(fields['valuation_date']).date()
                if not 0<=(today-day).days<=3:continue
                session['valuation']={**fields,'yield_pct':str(value),'snapshot_id':raw.snapshot_id}
            with self.service.repo.db.transaction() as tx:
                current,_,_,_,_=self._load(tx,ref)
                tx.execute('UPDATE xj_turn SET snapshot_json=? WHERE turn_id=?',(encoded(snapshot),current['turn_id']))
        except Exception:
            # Missing/invalid public reference data never becomes a fabricated
            # valuation. Absolute yield may still be usable; BP cannot convert.
            return
