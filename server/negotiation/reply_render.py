"""Source-bound public fragments and the complete authorized quote grid.

Rendering does not choose an action or authorize sending. The application
rebuilds the result with its own context, then applies its normal state gates.
"""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import re
from .slots import collect_candidates


def _decimal(value):
    if not isinstance(value, str):raise ValueError('Price must be a decimal string')
    if not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,8})?',value.strip()):raise ValueError('Invalid decimal spelling')
    try: result=Decimal(value)
    except InvalidOperation as exc:raise ValueError('Invalid decimal') from exc
    if not result.is_finite():raise ValueError('Nonfinite decimal')
    return result


def _fact(value):
    if isinstance(value,dict) and 'value' in value:
        return value['value']
    return value


def quote_grid(context):
    """Represent every authorized 0.01 tick with a constant-size index range."""
    domain=context.get('quote_constraints') or {}
    status=domain.get('status','UNAVAILABLE')
    grid={'status':status,'first_yield_pct':None,'last_yield_pct':None,'tick_size':'0.01',
          'index_min':None,'index_max':None,'count':0}
    if status!='AVAILABLE':return grid
    # An older/incomplete projection can still carry cash facts for a question.
    # It supplies no price authorization: never invent a missing tick size.
    if 'tick_size' not in domain:return {**grid,'status':'UNAVAILABLE'}
    tick=_decimal(domain.get('tick_size'))
    if tick!=Decimal('0.01'):raise ValueError('Unsupported authorized quote tick')
    lower=_decimal(domain.get('min_yield_pct'));upper=_decimal(domain.get('max_yield_pct'))
    first=int((lower/tick).to_integral_value(rounding=ROUND_CEILING))
    last=int((upper/tick).to_integral_value(rounding=ROUND_FLOOR))
    if first>last:return {**grid,'status':'EMPTY'}
    return {**grid,'first_yield_pct':format(first*tick,'.2f'),'last_yield_pct':format(last*tick,'.2f'),
            'index_min':0,'index_max':last-first,'count':last-first+1}


def _public_yield_values(context):
    """Only public yield operands, never a recursive scan of private context."""
    values=set()
    def add(value):
        try:values.add(_decimal(value))
        except (ValueError,TypeError):pass
    own=context.get('our_quote') or {}
    messages=[row for row in context.get('context',[]) if row.get('direction') in ('in','out')
              and row.get('thread_id',context.get('thread_id'))==context.get('thread_id')
              and (row['direction']=='in' or row.get('native_id')
                   or (own.get('native_id') and row.get('message_id')==own.get('message_id')))]
    for span in collect_candidates({'bond_code':context.get('bond_code',''),'context':messages}):
        if span['roles']==['YIELD']:add(span['value'])
    if own.get('native_id') and (own.get('price') or {}).get('kind')=='YIELD':
        add(own['price']['value'])
    for side in ('bid','ofr'):
        quote=(context.get('quotes') or {}).get(side) or {}
        if (quote.get('assertion')=='QUOTE' and (quote.get('evidence') or {}).get('price')
                and (quote.get('price') or {}).get('kind')=='YIELD'):
            add(quote['price']['value'])
    yield_fields={'yield_pct','valuation_yield_pct','bid_yield_pct','ofr_yield_pct'}
    for entry in context.get('analysis_tool_results',[])+context.get('tool_results',[]):
        request=entry.get('request') or {};response=entry.get('result') or {}
        if request.get('name') not in ('bond_info','similar_bonds') or response.get('status') not in ('ok','partial'):continue
        sources={row.get('id') for row in response.get('sources',[]) if isinstance(row,dict) and row.get('id')}
        def visit(node):
            if isinstance(node,list):
                for item in node:visit(item)
            elif isinstance(node,dict):
                for key,value in node.items():
                    if key in yield_fields and isinstance(value,dict):
                        if value.get('quality')=='verified' and value.get('source_ref') in sources:
                            add(value.get('value'))
                        elif (request['name']=='similar_bonds' and response.get('snapshot_id') and sources
                              and value.get('comparable') is True):
                            add(value.get('anchor_value'));add(value.get('candidate_value'))
                    visit(value)
        visit(response.get('data') or {})
    return values


def _public_gap_operands(entry, public_values):
    request=(entry.get('request') or {}).get('arguments') or {}
    used=(entry.get('result',{}).get('data') or {}).get('inputs_used') or {}
    if request.get('operation')!='yield_gap':return False
    inputs=request.get('inputs') or {}
    try:
        return all(_decimal(inputs[field])==_decimal(used[field]) and _decimal(used[field]) in public_values
                   for field in ('limit_yield_pct','peer_yield_pct'))
    except (KeyError,TypeError,ValueError):return False


def public_information(context):
    result=[]
    public_values=None
    def add(entry, label, text):
        if not text:return
        payload=json.dumps([entry,label,text],ensure_ascii=False,sort_keys=True,separators=(',',':'))
        identity='info-'+hashlib.sha256(payload.encode()).hexdigest()[:24]
        if not any(row['id']==identity for row in result):
            result.append({'id':identity,'text':text,'role':'PUBLIC_INFO_OR_CALC',
                           'source':{'tool':entry['request']['name'],'request_id':entry['result'].get('request_id'),
                                     'snapshot_id':entry['result'].get('snapshot_id'),'field':label}})
    def text_value(value):
        return json.dumps(value,ensure_ascii=False,separators=(',',':')) if isinstance(value,(dict,list)) else str(value)
    for entry in context.get('analysis_tool_results',[])+context.get('tool_results',[]):
        if not isinstance(entry,dict):continue
        request=entry.get('request') or {};response=entry.get('result') or {}
        if response.get('status') not in ('ok','partial'):continue
        data=response.get('data') or {};tool=request.get('name')
        if tool=='bond_info':
            identity=data.get('identity') or {};name=identity.get('name') or identity.get('bond_id')
            if name:add(entry,'identity','债券：'+str(name)+'。')
            master=data.get('master') or {}
            for key,label,unit in [('issuer_id','发行主体',''),('remaining_days','剩余期限','天'),
                                   ('maturity_date','到期日期',''),('balance','存续余额',str(_fact(master.get('balance_unit')) or '')),
                                   ('coupon_rate_pct','票面利率','%'),('bond_rating','债项评级','')]:
                value=_fact(master.get(key))
                if value is not None:add(entry,'master.'+key,label+'：'+text_value(value)+unit+'。')
        elif tool=='issuer_info':
            identity=data.get('identity') or {};parts=[]
            for key,label in [('full_name','发行主体'),('short_name','简称'),('registered_region','地区'),('industry','行业')]:
                value=_fact(data.get(key))
                if value is None:value=identity.get(key)
                if isinstance(value,dict):value=value.get('code') or value.get('name')
                if value is not None:parts.append(label+'：'+text_value(value))
            rating=_fact(data.get('issuer_rating'))
            if isinstance(rating,dict):
                if rating.get('value'):parts.append('评级：'+str(rating['value']))
                if rating.get('agency'):parts.append('评级机构：'+str(rating['agency']))
                if rating.get('rating_date'):parts.append('评级日期：'+str(rating['rating_date']))
            add(entry,'issuer','，'.join(parts)+'。' if parts else '')
        elif tool=='similar_bonds':
            for candidate in data.get('candidates',[]):
                parts=['相似券：'+str(candidate.get('code') or candidate.get('bond_id'))]
                if candidate.get('remaining_days') is not None:parts.append('剩余期限'+str(candidate['remaining_days'])+'天')
                if candidate.get('tenor_distance') is not None:parts.append('期限相差'+str(candidate['tenor_distance'])+'天')
                comparisons=candidate.get('comparisons') or {}
                tenor=comparisons.get('remaining_days') or {}
                if tenor.get('comparable') and tenor.get('anchor_value') is not None:parts.append('原券期限'+str(tenor['anchor_value'])+'天')
                valuation=comparisons.get('valuation_yield_pct') or {}
                if valuation.get('comparable'):
                    if valuation.get('candidate_value') is not None:parts.append('公开估值'+str(valuation['candidate_value'])+'%')
                    if valuation.get('anchor_value') is not None:parts.append('原券公开估值'+str(valuation['anchor_value'])+'%')
                add(entry,'similar.'+str(candidate.get('bond_id')),'，'.join(parts)+'。')
        elif tool=='bond_calc':
            calculation=data.get('result') or {}
            if data.get('method')=='yield_gap' and calculation.get('raw_gap_bp') is not None:
                if public_values is None:public_values=_public_yield_values(context)
                if not _public_gap_operands(entry,public_values):continue
                add(entry,'yield_gap','两者收益率相差'+str(calculation['raw_gap_bp'])+'BP。')
    return result


def reply_requirements(context):
    """Project existing decision prerequisites without narrowing the price grid."""
    if context.get('reference_confirmation_required'):
        return {'allowed_kinds':['CLARIFY'],'question':'ASK_CURRENT_QUOTE','reason':'REFERENCE_CONFIRMATION_REQUIRED'}
    if context.get('unresolved_messages'):
        return {'allowed_kinds':['CLARIFY'],'question':'NONE','reason':'BOND_SCOPE_CLARIFICATION_REQUIRED'}
    if context.get('reconfirm_day'):
        return {'allowed_kinds':['RECONFIRM','CLARIFY'],'question':'ASK_CURRENT_QUOTE','reason':'CURRENT_DAY_CONFIRMATION_REQUIRED'}
    side='ofr' if context.get('our_side')=='BUY' else 'bid'
    peer=(context.get('quotes') or {}).get(side) or {}
    price=peer.get('price') or {}
    if price.get('kind') in ('NET_PRICE','DIRTY_PRICE') and not all(price.get(key) is not None and price.get(key)!='' for key in ('settlement_date','computed_yield','calculation_ref')):
        current=(peer.get('validity')=='ACTIVE' and peer.get('assertion')=='QUOTE' and peer.get('confirmed_day')==context.get('trade_day'))
        return {'allowed_kinds':['CLARIFY'],'question':'ASK_SETTLEMENT' if current else 'ASK_CURRENT_QUOTE',
                'reason':'CASH_BASIS_CONFIRMATION_REQUIRED'}
    return {'allowed_kinds':['REPLY','ASK_VOLUME','CLARIFY','SOCIAL'],'question':None,'reason':None}


def render_reply_plan(context, plan):
    allowed={'kind','quote_value','quote_index','include_volume','information_ids','question','lead','tail','tone','context_ref'}
    if not isinstance(plan,dict) or set(plan)-allowed:raise ValueError('Unknown reply plan fields')
    if 'quote_index' in plan and 'quote_value' in plan:
        raise ValueError('Select a quote index or a literal value, not both')
    bound={key:context.get(key) for key in ('member_id','thread_id','bond_code','our_side','trade_day','basis','quotes','quote_constraints')}
    context_ref=hashlib.sha256(json.dumps(bound,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if 'context_ref' in plan and plan['context_ref']!=context_ref:
        raise ValueError('Reply plan belongs to another member, side or facts version')
    plan={**plan,'context_ref':context_ref}
    kind=plan.get('kind')
    if kind not in ('REPLY','RECONFIRM','ASK_VOLUME','CLARIFY','SOCIAL'):raise ValueError('Unsupported reply plan kind')
    requirements=reply_requirements(context)
    if kind not in requirements['allowed_kinds']:
        raise ValueError(requirements['reason'] or 'Reply kind does not satisfy this flow decision prerequisite')
    if requirements['question'] and requirements['question']!='NONE' and plan.get('question')!=requirements['question']:
        raise ValueError('Reply plan must ask the required confirmation question')
    information=plan.get('information_ids',[])
    if not isinstance(information,list) or len(information)>30 or any(not isinstance(x,str) for x in information) or len(set(information))!=len(information):
        raise ValueError('Invalid source information selection')
    lookup={row['id']:row for row in public_information(context)}
    if any(value not in lookup for value in information):raise ValueError('Public information source is missing or stale')
    parts=[dict(lookup[key]) for key in information]
    tone=plan.get('tone','NEUTRAL')
    if tone not in ('NEUTRAL','POLITE'):raise ValueError('Unknown public tone')
    if tone=='POLITE':parts.append({'role':'FREE_TEXT','text':'您好。'})
    for field in ('lead','tail'):
        if not isinstance(plan.get(field,''),str) or len(plan.get(field,''))>300:
            raise ValueError('Unbounded free wording')
    if plan.get('lead'):parts.append({'role':'FREE_TEXT','text':plan['lead']})
    price=None
    quote_value=plan.get('quote_value')
    peer_side='ofr' if context.get('our_side')=='BUY' else 'bid'
    peer=(context.get('quotes') or {}).get(peer_side) or {}
    bond=str(context.get('bond_code',''))
    include=plan.get('include_volume',False)
    if type(include) is not bool:raise ValueError('include_volume must be boolean')
    if kind=='REPLY':
        if 'quote_index' in plan:
            grid=quote_grid(context);index=plan['quote_index']
            if grid['status']!='AVAILABLE' or type(index) is not int or not 0<=index<grid['count']:
                raise ValueError('Selected index is outside the complete authorized grid')
            quote_value=format(_decimal(grid['first_yield_pct'])+index*Decimal('0.01'),'.2f')
        value=_decimal(quote_value)
        domain=context.get('quote_constraints') or {}
        if domain.get('status')!='AVAILABLE':raise ValueError('No authorized quote domain')
        lower=_decimal(domain['min_yield_pct']);upper=_decimal(domain['max_yield_pct']);tick=_decimal(domain['tick_size'])
        if tick!=Decimal('0.01') or not lower<=value<=upper or value%tick:
            raise ValueError('Selected quote is outside the complete authorized grid')
        spelling=format(value.quantize(Decimal('0.01')),'.2f')
        price={'kind':'YIELD','value':spelling}
        side={'BUY':'收','SELL':'出'}.get(context.get('our_side'))
        if side is None:raise ValueError('Missing authorized side')
        text=bond+' '+spelling+'%'+side
        if include:
            amount=peer.get('volume_wan')
            if amount is None:raise ValueError('Cannot render an unknown peer lot')
            if _decimal(str(amount))<=0:raise ValueError('Invalid peer lot')
            text+='，'+str(amount)+'万报价'
        parts.append({'role':'OWN_QUOTE','text':text+'。','source':{'member_id':context.get('member_id'),'basis':context.get('basis')}})
    elif quote_value is not None or 'quote_index' in plan or include:
        raise ValueError('Nonquote plan cannot insert an own price or lot')
    question=plan.get('question','NONE')
    if question=='ASK_SETTLEMENT':
        cash=peer.get('price') or {}
        if (kind!='CLARIFY' or cash.get('kind') not in ('NET_PRICE','DIRTY_PRICE')
                or peer.get('validity')!='ACTIVE' or peer.get('assertion')!='QUOTE'
                or peer.get('confirmed_day')!=context.get('trade_day')):
            raise ValueError('Settlement clarification needs a current active counterpart cash quote')
        label='净价' if cash['kind']=='NET_PRICE' else '全价'
        value=format(_decimal(cash['value']),'f')
        parts.append({'role':'COUNTERPARTY_REFERENCE','text':bond+' 你刚才报的'+label+value+'，结算是T+0、T+1还是具体日期？',
                      'source':{'member_id':context.get('member_id'),'side':peer_side,'revision':peer.get('revision'),'evidence':peer.get('evidence')}})
    elif question=='ASK_CURRENT_QUOTE':
        if kind not in ('CLARIFY','RECONFIRM'):raise ValueError('Current quote question requires clarification or reconfirmation')
        recorded=peer.get('price') or {}
        if peer.get('validity')!='WITHDRAWN' and peer.get('assertion')=='QUOTE' and recorded.get('kind') in ('YIELD','NET_PRICE','DIRTY_PRICE'):
            label={'YIELD':'收益率','NET_PRICE':'净价','DIRTY_PRICE':'全价'}[recorded['kind']]
            value=format(_decimal(recorded['value']),'f')+('%' if recorded['kind']=='YIELD' else '')
            side='出' if peer_side=='ofr' else '收'
            text=bond+' 上次记录你方'+label+value+side
            if peer.get('volume_wan') is not None:text+=str(peer['volume_wan'])+'万'
            parts.append({'role':'COUNTERPARTY_REFERENCE','text':text+'，现在仍是这个报价和数量吗？',
                          'source':{'member_id':context.get('member_id'),'side':peer_side,'revision':peer.get('revision'),'evidence':peer.get('evidence')}})
        else:
            parts.append({'role':'FREE_TEXT','text':bond+' 请确认当前报价和数量。'})
    elif question=='ASK_MORE_VOLUME':
        if kind!='ASK_VOLUME':raise ValueError('Additional size needs the ASK_VOLUME action')
        parts.append({'role':'FREE_TEXT','text':bond+' 请问还能提供多少量？'})
    elif question=='NEGOTIATE':
        if kind!='REPLY':raise ValueError('Negotiation question requires a quote')
        parts.append({'role':'FREE_TEXT','text':'请问这个报价还能调整吗？'})
    elif question!='NONE':raise ValueError('Unknown reply question')
    if plan.get('tail'):parts.append({'role':'FREE_TEXT','text':plan['tail']})
    text='\n'.join(part['text'] for part in parts)
    if not 1<=len(text)<=1500:raise ValueError('Rendered reply must contain 1..1500 characters')
    proposal={'kind':kind,'text':text,'render_plan':plan}
    if price:proposal['our_price']=price
    economic='\n'.join(part['text'] for part in parts if part['role'] in ('OWN_QUOTE','FREE_TEXT'))
    return {'proposal':proposal,'economic_text':economic,'parts':parts}
