"""Bind verified economic facts inside model-written natural public wording.

Rendering does not choose an action or authorize sending. The application
rebuilds the result with its own context, then applies its normal state gates.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from .slots import collect_candidates
from .text_roles import public_language_error, numeric_spelling


def _decimal(value):
    if not isinstance(value, str):raise ValueError('Price must be a decimal string')
    if not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,8})?',value.strip()):raise ValueError('Invalid decimal spelling')
    try: result=Decimal(value)
    except InvalidOperation as exc:raise ValueError('Invalid decimal') from exc
    if not result.is_finite():raise ValueError('Nonfinite decimal')
    return result


def requested_volume_signature(proposal):
    """Freeze only the requested-lot binding category, never public wording."""
    plan=proposal.get('render_plan') if isinstance(proposal,dict) else None
    text=plan.get('text') if isinstance(plan,dict) else None
    return isinstance(text,str) and '{requested_volume}' in text


def direct_quantity_request(text, bond_codes=()):
    """Recognize a complete direct lot question, never infer a firm offer."""
    if not isinstance(text,str):return None
    text=numeric_spelling(text)
    if re.search(r'别家|别人|第三方|客户|听说|转述|假设|假如|如果|只要|条件|不是我|并非我|不代表|撤回|作废|取消',text):
        return None
    number=r'\d{1,12}(?:\.\d{1,8})?'
    lot=r'(?P<amount>'+number+r')\s*(?P<unit>万|亿|[wW])?'
    direction=r'(?P<direction>买入|卖出|收|出|买|卖)'
    # Match the complete source. Never discard an unknown leading sentence
    # such as a third party's attribution before a seemingly direct question.
    clause=text.strip().rstrip('。!！').strip()
    for code in sorted(set(bond_codes),key=len,reverse=True):
        if code:clause=re.sub(r'^\s*'+re.escape(numeric_spelling(code))+r'(?:(?![A-Za-z0-9_-])|(?=-?\d+\.\d))\s*[:：,，]?\s*','',clause,count=1,flags=re.I)
    question=bool(re.search(r'[吗么?？]\s*$',clause) or re.search(r'能否|可否|是否|能不能',clause))
    if not question:return None
    clause=re.sub(r'[吗么?？]+\s*$','',clause).strip()
    clause=re.sub(r'^(?:这个价格|这个价|这价|按这个价)\s*','',clause)
    speaker=re.match(r'^(你|您|贵方|我们|我方|本方|我)(?:这边)?\s*',clause)
    actor=('RECIPIENT' if speaker[1] in ('你','您','贵方') else 'SELF') if speaker else None
    if speaker:clause=clause[speaker.end():]
    modal=re.match(r'^(?:能不能|是否可以|是否能|能否|可否|是否|可以|能)\s*',clause)
    had_modal=modal is not None
    if modal:clause=clause[modal.end():]
    match=re.fullmatch(r'(?:(?P<offer_price>-?'+number+r')\s*[%％]?\s*)?'+direction+r'\s*'+lot,clause)
    if match is None:match=re.fullmatch(r'(?:做|接|给)\s*'+lot,clause)
    if match is None:
        match=re.fullmatch(lot+r'\s*(?:能不能|能否|可否|是否可以|是否能|是否|可以|能)\s*(?:'+direction+r'|做|接)',clause)
    if match is None:return None
    amount=_decimal(match['amount'])*(Decimal(10000) if match['unit']=='亿' else Decimal(1))
    if amount<=0:return None
    word=match.groupdict().get('direction')
    if actor is None:
        actor='RECIPIENT' if had_modal else 'OFFER' if match.groupdict().get('offer_price') is not None else 'RECIPIENT' if word else 'UNSPECIFIED'
    return {'volume_wan':format(amount,'f'),'unit':match['unit'],
        'direction':('bid' if word in ('收','买','买入') else 'ofr') if word else None,
        'actor':actor,'source_text':text}


def requested_quantity_source(context):
    """Prove one current direct request without changing either saved side."""
    rows=context.get('context',[])+context.get('unresolved_messages',[])
    if not rows or any(type(row.get('ingest_revision')) is not int for row in rows):return None
    message=max(rows,key=lambda row:row['ingest_revision'])
    if (message.get('direction')!='in' or message.get('historical')
            or message.get('kind','message')!='message'
            or not context.get('trade_day') or message.get('trade_day')!=context['trade_day']
            or message.get('thread_id')!=context.get('thread_id')):return None
    request=direct_quantity_request(message.get('text'),(context.get('bond_code',''),))
    if request is None or request['unit'] is None:return None
    own_side={'BUY':'bid','SELL':'ofr'}.get(context.get('our_side'))
    if own_side is None:return None
    if request['direction'] is not None:
        expected=own_side if request['actor']=='RECIPIENT' else ('ofr' if own_side=='bid' else 'bid')
        if request['direction']!=expected:return None
    matches=[]
    for side in ('bid','ofr'):
        quote=(context.get('quotes') or {}).get(side) or {}
        refs=(quote.get('evidence') or {}).get('volume_wan') or []
        if quote.get('assertion')!='QUESTION' or quote.get('validity')!='ACTIVE' or message.get('message_id') not in refs:continue
        try:amount=_decimal(quote.get('volume_wan'))
        except (TypeError,ValueError,ArithmeticError):continue
        if amount>0 and amount==_decimal(request['volume_wan']):matches.append(side)
    if not matches:return None
    return {'volume_wan':request['volume_wan'],'text':request['volume_wan']+'万',
        'source':{'member_id':context.get('member_id'),'thread_id':context.get('thread_id'),
            'message_ids':[message['message_id']],'observed_sides':matches,
            'actor':request['actor'],'assertion':'QUESTION','purpose':'OWN_REQUESTED_QUANTITY_OFFER'}}


def _fact(value):
    if isinstance(value,dict) and 'value' in value:
        return value['value']
    return value


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


def handoff_notice_source(context):
    """Prove eligibility only; equal prices never choose this model action."""
    try:
        if context.get('reconfirm_day') or context.get('unresolved_messages') or context.get('reference_confirmation_required'):return None
        side={'BUY':'ofr','SELL':'bid'}.get(context.get('our_side'))
        if side is None:return None
        quote=context['quotes'].get(side) or {};own=context.get('our_quote') or {}
        if (quote.get('assertion')!='QUOTE' or quote.get('validity')!='ACTIVE'
                or quote.get('confirmed_day')!=context.get('trade_day')
                or type(quote.get('revision')) is not int or quote['revision']<1
                or _decimal(quote.get('volume_wan'))<=0 or own.get('verified_sent') is not True
                or any(not isinstance(own.get(key),str) or not own[key] for key in ('command_id','message_id','native_id'))):return None
        def comparable(price):
            if price.get('kind')=='YIELD':return _decimal(price.get('value'))
            if (price.get('kind') in ('NET_PRICE','DIRTY_PRICE') and price.get('settlement_date')
                    and isinstance(price.get('calculation_ref'),str) and price['calculation_ref']):
                return _decimal(price.get('computed_yield'))
            return None
        peer_yield=comparable(quote.get('price') or {});own_yield=comparable(own.get('price') or {})
        boundary=context.get('boundary') or {}
        if peer_yield is None or own_yield is None or peer_yield!=own_yield or boundary.get('kind')!='YIELD':return None
        limit=_decimal(boundary.get('value'))
        if context['our_side']=='BUY' and peer_yield<limit or context['our_side']=='SELL' and peer_yield>limit:return None
        rows=context.get('context',[])
        if not rows or any(type(row.get('ingest_revision')) is not int for row in rows):return None
        latest=max(rows,key=lambda row:row['ingest_revision'])
        def current(row):
            return (row.get('thread_id')==context.get('thread_id') and row.get('trade_day')==context.get('trade_day')
                    and row.get('kind','message')=='message' and not row.get('historical'))
        if (not current(latest) or latest.get('direction')!='in' or not latest.get('message_id')
                or latest.get('occurred_at',-1)<own.get('sent_at',0)):return None
        evidence={row['message_id']:row for row in context.get('handoff_evidence',rows)}
        for field in ('price','volume_wan'):
            refs=(quote.get('evidence') or {}).get(field)
            if (not isinstance(refs,list) or not refs or len(refs)>30
                    or any(ref not in evidence or not current(evidence[ref]) for ref in refs)
                    or not any(evidence[ref].get('direction')=='in' for ref in refs)):return None
        return {'side':side,'quote_revision':quote['revision'],'own_command_id':own['command_id'],
                'source_ids':[latest['message_id']]}
    except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError):return None


def validate_handoff_notice_text(text):
    if (not isinstance(text,str) or not text.strip() or len(text)>1500
            or re.search(r'[{}\d%％?？]',numeric_spelling(text))
            or re.search(r'[零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億]+\s*(?:万|亿|元|点|BP|基点|厘|[%％])',text,re.I)):
        raise ValueError('Handoff notice must be non-economic confirmation text')
    error=public_language_error(text)
    if error:raise ValueError(error)


def reply_requirements(context):
    """Project current prerequisites without selecting an action or wording."""
    if context.get('reference_confirmation_required'):
        return {'allowed_kinds':['CLARIFY'],'question':'ASK_CURRENT_QUOTE','reason':'REFERENCE_CONFIRMATION_REQUIRED'}
    if context.get('unresolved_messages'):
        return {'allowed_kinds':['CLARIFY'],'question':'NONE','reason':'BOND_SCOPE_CLARIFICATION_REQUIRED'}
    if context.get('reconfirm_day'):
        if context.get('reconfirmation_waiting'):
            return {'allowed_kinds':['RECONFIRM','CLARIFY','SOCIAL'],'question':None,
                    'reason':'CURRENT_DAY_CONFIRMATION_PENDING'}
        return {'allowed_kinds':['RECONFIRM','CLARIFY'],'question':'ASK_CURRENT_QUOTE','reason':'CURRENT_DAY_CONFIRMATION_REQUIRED'}
    side='ofr' if context.get('our_side')=='BUY' else 'bid'
    peer=(context.get('quotes') or {}).get(side) or {}
    price=peer.get('price') or {}
    if price.get('kind') in ('NET_PRICE','DIRTY_PRICE') and not all(price.get(key) is not None and price.get(key)!='' for key in ('settlement_date','computed_yield','calculation_ref')):
        current=(peer.get('validity')=='ACTIVE' and peer.get('assertion')=='QUOTE' and peer.get('confirmed_day')==context.get('trade_day'))
        return {'allowed_kinds':['CLARIFY'],'question':'ASK_SETTLEMENT' if current else 'ASK_CURRENT_QUOTE',
                'reason':'CASH_BASIS_CONFIRMATION_REQUIRED'}
    allowed=['REPLY','ASK_VOLUME','CLARIFY','SOCIAL']
    if handoff_notice_source(context) is not None:allowed.append('HANDOFF_NOTICE')
    return {'allowed_kinds':allowed,'question':None,'reason':None}


def render_reply_plan(context, plan):
    """Bind facts only: the model owns sentence structure and negotiation tone."""
    if not isinstance(plan,dict) or set(plan)-{'kind','quote_value','text','question','context_ref','acceptance'}:
        raise ValueError('Unknown natural reply plan fields')
    bound={key:context.get(key) for key in ('member_id','thread_id','bond_code','our_side','trade_day',
        'basis','quotes','our_quote','quote_constraints')}
    context_ref=hashlib.sha256(json.dumps(bound,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if 'context_ref' in plan and plan['context_ref']!=context_ref:
        raise ValueError('Reply plan belongs to another member, side or facts version')
    plan={**plan,'context_ref':context_ref}
    kind=plan.get('kind');requirements=reply_requirements(context)
    if kind not in ('REPLY','RECONFIRM','ASK_VOLUME','CLARIFY','SOCIAL','HANDOFF_NOTICE'):
        raise ValueError('Unsupported reply plan kind')
    if kind not in requirements['allowed_kinds']:
        raise ValueError(requirements['reason'] or 'Reply kind does not satisfy this flow decision prerequisite')
    question=plan.get('question','NONE')
    if question not in ('NONE','NEGOTIATE','ASK_SETTLEMENT','ASK_CURRENT_QUOTE','ASK_MORE_VOLUME'):
        raise ValueError('Unknown reply question')
    if requirements['question'] and question!=requirements['question']:
        raise ValueError('Reply plan must ask the required confirmation question')
    if ((question=='NEGOTIATE' and kind!='REPLY') or (question=='ASK_SETTLEMENT' and kind!='CLARIFY')
            or (question=='ASK_CURRENT_QUOTE' and kind not in ('CLARIFY','RECONFIRM'))
            or (question=='ASK_MORE_VOLUME' and kind!='ASK_VOLUME')):
        raise ValueError('Question does not match the reply kind')
    template=plan.get('text')
    if not isinstance(template,str) or not 1<=len(template.strip())<=1500:
        raise ValueError('Natural reply text must contain 1..1500 characters')
    if kind=='HANDOFF_NOTICE':
        if plan.get('acceptance')!={'meaning':'ACCEPTED_CURRENT_QUOTE','source':'LATEST_INBOUND'} or question!='NONE':
            raise ValueError('Handoff notice requires explicit current-quote acceptance')
        validate_handoff_notice_text(template)
    elif 'acceptance' in plan:raise ValueError('Acceptance is reserved for handoff notices')
    peer_side='ofr' if context.get('our_side')=='BUY' else 'bid'
    peer=(context.get('quotes') or {}).get(peer_side) or {}
    price=None
    if kind=='REPLY':
        value=_decimal(plan.get('quote_value'))
        domain=context.get('quote_constraints') or {}
        if domain.get('status')!='AVAILABLE' or domain.get('tick_size')!='0.01' or value%Decimal('0.01'):
            raise ValueError('No authorized exact-tick quote domain')
        for key,below in (('min_yield_pct',True),('max_yield_pct',False)):
            limit=domain.get(key)
            if limit is not None and (value<_decimal(limit) if below else value>_decimal(limit)):
                raise ValueError('Selected quote is outside the authorized final boundary')
        price={'kind':'YIELD','value':format(value,'.2f')}
    elif 'quote_value' in plan or '{own_quote}' in template:
        raise ValueError('Nonquote plan cannot insert an own price')
    own_side={'BUY':'收','SELL':'出'}.get(context.get('our_side'))
    if own_side is None:raise ValueError('Missing authorized side')
    information={row['id']:row for row in public_information(context)}
    def public_quote(quote, direction):
        raw=quote.get('price') or {}
        if raw.get('kind') not in ('YIELD','NET_PRICE','DIRTY_PRICE'):
            raise ValueError('Referenced quotation has no supported price')
        label={'YIELD':'','NET_PRICE':'净价','DIRTY_PRICE':'全价'}[raw['kind']]
        return label+format(_decimal(raw.get('value')),'f')+('%' if raw['kind']=='YIELD' else '')+direction
    def fragment(key):
        if key=='bond':return {'role':'PUBLIC_INFO_OR_CALC','text':str(context.get('bond_code',''))}
        if key=='own_quote':
            if price is None:raise ValueError('Own quote is unavailable')
            return {'role':'OWN_QUOTE','text':price['value']+'%'+own_side,
                    'source':{'member_id':context.get('member_id'),'basis':context.get('basis')}}
        if key=='requested_volume':
            request=requested_quantity_source(context) if kind=='REPLY' else None
            if request is None:raise ValueError('Unknown or unavailable public fact slot')
            return {'role':'OWN_REQUESTED_QUANTITY_OFFER','text':request['text'],'source':request['source']}
        if key in ('peer_quote','peer_volume'):
            if peer.get('validity')=='WITHDRAWN' or peer.get('assertion')!='QUOTE':
                raise ValueError('Counterparty reference is not an actual quotation')
            if kind=='REPLY' and (peer.get('validity')!='ACTIVE' or peer.get('confirmed_day')!=context.get('trade_day')):
                raise ValueError('Counterparty reference is not current')
            field='price' if key=='peer_quote' else 'volume_wan'
            if not (peer.get('evidence') or {}).get(field):raise ValueError('Counterparty reference has no field evidence')
            if key=='peer_quote':text=public_quote(peer,'出' if peer_side=='ofr' else '收')
            else:
                amount=_decimal(str(peer.get('volume_wan')))
                if amount<=0:raise ValueError('Referenced whole lot is unavailable')
                text=format(amount,'f')+'万'
            return {'role':'COUNTERPARTY_REFERENCE' if key=='peer_quote' or kind!='REPLY' else 'OWN_QUOTE',
                    'text':text,'source':{'member_id':context.get('member_id'),'side':peer_side,
                    'revision':peer.get('revision'),'evidence':peer.get('evidence')}}
        if key=='our_last_quote':
            actual=context.get('our_quote') or {}
            if not actual.get('verified_sent') or not all(actual.get(k) for k in ('native_id','command_id','message_id')):
                raise ValueError('Previous own quotation is not a verified current native send')
            loaded=[row for row in context.get('context',[]) if row.get('message_id')==actual['message_id']]
            if loaded and (len(loaded)!=1 or loaded[0].get('direction')!='out'
                    or loaded[0].get('native_id')!=actual['native_id']
                    or loaded[0].get('thread_id',context.get('thread_id'))!=context.get('thread_id')
                    or loaded[0].get('trade_day')!=context.get('trade_day')):
                raise ValueError('Loaded own quotation conflicts with its verified native source')
            return {'role':'OWN_HISTORY_REFERENCE','text':public_quote(actual,own_side),
                    'source':{'member_id':context.get('member_id'),'command_id':actual['command_id'],
                              'message_id':actual['message_id'],'native_id':actual['native_id']}}
        if key=='settlement_options' and question=='ASK_SETTLEMENT':
            return {'role':'SETTLEMENT_QUESTION','text':'T+0、T+1或具体日期'}
        if key.startswith('info:') and key[5:] in information:return dict(information[key[5:]])
        raise ValueError('Unknown or unavailable public fact slot')
    # Input sugar is admitted only when it can be represented by the existing
    # fact macros. Keep the original plan text for audit and run the virtual
    # macro plan through the same role, speaker and final economic checks.
    lot=None;peer_literal=None
    current_peer=(bool(context.get('trade_day')) and peer.get('assertion')=='QUOTE'
                  and peer.get('validity')=='ACTIVE' and peer.get('confirmed_day')==context.get('trade_day'))
    if current_peer:
        try:
            fragment('peer_volume')
            lot=_decimal(str(peer.get('volume_wan')))
        except (ValueError,TypeError,ArithmeticError):pass
        peer_kind=(peer.get('price') or {}).get('kind')
        if peer_kind=='YIELD' or kind=='CLARIFY' and question=='ASK_SETTLEMENT' and peer_kind in ('NET_PRICE','DIRTY_PRICE'):
            try:peer_literal=fragment('peer_quote')['text']
            except (ValueError,TypeError,ArithmeticError):pass
    patterns=[]
    if price is not None:
        patterns.append(r'(?<![A-Za-z0-9_.+%-])(?<!净价)(?<!全价)(?P<own>-?[0-9]{1,12}(?:\.[0-9]{1,8})?)[ \t]*(?:%[ \t]*)?'
                        +re.escape(own_side)+r'(?=$|[0-9\s，,。.!！?？;；:：、{])')
    if peer_literal:
        bond_prefix=('(?P<bond_prefix>'+re.escape(context['bond_code'])+')?'
                     if peer_kind in ('NET_PRICE','DIRTY_PRICE') and context.get('bond_code') else '')
        patterns.append(r'(?<![A-Za-z0-9_.+-])'+bond_prefix+r'(?P<peer>'+re.escape(peer_literal)+r')(?![A-Za-z_.])')
    if lot is not None:
        patterns.append(r'(?<![A-Za-z0-9_.+-])(?P<amount>[0-9]{1,12}(?:\.[0-9]{1,8})?)\s*(?P<unit>万|亿)(?![A-Za-z0-9_.十百千万亿拾佰仟萬億元])')
    if patterns or information:
        literal=re.compile('|'.join(patterns)) if patterns else None
        # Protect complete verified public facts before recognizing quotation
        # components; their embedded numbers retain the original public source.
        public_text={}
        for identity,row in information.items():
            if row.get('text'):public_text.setdefault(row['text'],identity)
        public_literal=re.compile('|'.join(re.escape(text) for text in sorted(public_text,key=len,reverse=True))) if public_text else None
        def bind_free(text):
            def replace(match):
                if match.groupdict().get('own') is not None:
                    if re.search(r'(净价|全价)\s*$',match.string[:match.start()]):return match[0]
                    return '{own_quote}' if _decimal(match['own'])==value else match[0]
                if match.groupdict().get('peer') is not None:return (match.groupdict().get('bond_prefix') or '')+'{peer_quote}'
                amount=_decimal(match['amount'])*(Decimal(10000) if match['unit']=='亿' else Decimal(1))
                return '{peer_volume}' if amount==lot else match[0]
            def economic(piece):return literal.sub(replace,piece) if literal else piece
            if public_literal is None:return economic(text)
            pieces=[];end=0
            for fact in public_literal.finditer(text):
                pieces.extend((economic(text[end:fact.start()]),'{info:'+public_text[fact[0]]+'}'));end=fact.end()
            pieces.append(economic(text[end:]));return ''.join(pieces)
        pieces=[];end=0
        for macro in re.finditer(r'\{([^{}]+)\}',template):
            canonical='{info:'+macro[1]+'}' if macro[1] in information else macro[0]
            if re.fullmatch(r'info:[a-f0-9]{24}',macro[1]) and 'info-'+macro[1][5:] in information:
                canonical='{info:info-'+macro[1][5:]+'}'
            pieces.extend((bind_free(template[end:macro.start()]),canonical));end=macro.end()
        pieces.append(bind_free(template[end:]));template=''.join(pieces)
    if kind=='REPLY' and '{own_quote}' not in template:
        raise ValueError('A quote reply must bind its own structured quotation')
    # Fold only redundant components supplied by a verified adjacent field.
    # Separators keep ordinary words outside this operation; ambiguous joined
    # words and contradictory units/directions are never repaired by deletion.
    def separated(text):
        return not text or text[0].isspace() or text[0] in '，,。.!！?？;；:：、' or text.startswith('{')
    pieces=[];end=0
    for macro in re.finditer(r'\{([^{}]+)\}',template):
        key=macro[1];before=template[end:macro.start()];after=template[macro.end():];consume=0
        if key=='own_quote' and re.search(r'(?:收|出|买入?|卖出?)\s*$',before):
            raise ValueError('Own quote slot repeats or conflicts with an adjacent direction')
        if key in ('own_quote','peer_quote','our_last_quote'):
            bound=fragment(key)['text']
            prefix=re.search(r'(净价|全价)([ \t]*)$',before)
            if prefix and bound.startswith(('净价','全价')):
                if prefix[1]!=bound[:2]:raise ValueError('Cash quote basis prefix conflicts with verified field')
                before=before[:prefix.start()]+prefix[2]
            direction=re.match(r'([%％]?)(收|出|买|卖)',after)
            if direction:
                suffix=direction[2]
                tail=after[direction.end():]
                offer_noun=(suffix=='出' and bound.endswith('出') and tail.startswith('盘')
                            and (separated(tail[1:]) or tail[1:].startswith('为')))
                if not separated(tail) and not offer_noun:raise ValueError('Ambiguous adjacent quote direction word')
                if direction[1] and not bound.endswith(('%收','%出')):
                    raise ValueError('Quote percent suffix conflicts with verified price kind')
                expected='bid' if bound.endswith('收') else 'ofr'
                if ('bid' if suffix in ('收','买') else 'ofr')!=expected:
                    raise ValueError('Quote direction suffix conflicts with verified field')
                if offer_noun:
                    clause=re.split(r'[，,。;；\n]',''.join(pieces)+before)[-1]
                    speaker=re.search(r'(我(?:们|方|这边)?|本方|你(?:们|方|这边)?|您(?:方)?|贵方|对方)'
                        r'(?:现在|这次|刚才|之前|那边|这边|已经|也|再|就|还是|先|愿意|可以|能|目前|的|上次|曾经){0,5}\s*$',clause)
                    if speaker:
                        prefix=clause[:speaker.start()]
                        negated=bool(re.search(r'(?:不是|并非)\s*(?:说\s*)?$',prefix)
                            and not re.search(r'(?:不是|并非|不可能|不能|不会)\s*(?:说\s*)?(?:不是|并非)\s*(?:说\s*)?$',prefix))
                        if not negated and speaker[1].startswith(('我','本方'))!=(key!='peer_quote'):
                            raise ValueError('Public reference is attributed to the wrong speaker')
                consume=direction.end()
            elif after.startswith(('%','％')):
                raise ValueError('Ambiguous adjacent quote percent component')
        elif key in ('peer_volume','requested_volume'):
            fragment(key)
            if after.startswith('万'):
                if not (separated(after[1:]) or after[1:].startswith(('可以','是否','是不是','能否','可否','的'))):
                    raise ValueError('Ambiguous adjacent quantity unit word')
                consume=1
            elif after[:1] in '十百千亿拾佰仟萬億' and after:
                raise ValueError('Quantity unit suffix conflicts with verified whole lot')
        elif key.startswith('info:'):
            bound=fragment(key)['text']
            if bound.endswith(('。','.')) and after.startswith(bound[-1]):consume=1
        pieces.extend((before,macro[0]));end=macro.end()+consume
    pieces.append(template[end:]);template=''.join(pieces)
    template=re.sub(r'(\{(?:own_quote|peer_quote|our_last_quote|peer_volume|requested_volume)\})(?:\1)+',r'\1',template)
    if len(template)>1500:raise ValueError('Equivalent fact-macro text exceeds 1500 characters')
    parts=[];cursor=0
    for match in re.finditer(r'\{([^{}]+)\}',template):
        if match.start()>cursor:parts.append({'role':'FREE_TEXT','text':template[cursor:match.start()]})
        if match[1] in ('peer_quote','our_last_quote','own_quote','requested_volume'):
            clause=re.split(r'[，,。;；\n]',template[:match.start()])[-1]
            # Bind an explicit quotation assertion, not ordinary perception or
            # a polite request such as "I saw your quote" / "please consider".
            assertion=(r'(?:报(?:价|量)?|量(?:为|是)|出(?:价)?|收|买|卖|给(?:你|您)?|让到|调整到|改为|是)'
                       if match[1]=='requested_volume' else r'(?:报(?:价)?|出(?:价)?|收|买|卖|给(?:你|您)?|让到|调整到|改为|是)')
            temporal=None;negated_temporal=False
            if match[1] in ('own_quote','our_last_quote','peer_quote'):
                temporal=re.search(r'(我(?:们|方|这边)?|本方|你(?:们|方|这边)?|您(?:方)?|贵方|对方)'
                    r'\s*(?:之前|刚才|上次|曾经)\s*(?:的)?\s*(?:'+assertion+r'(?:过)?)?\s*$',clause)
                if temporal:
                    prefix=clause[:temporal.start()]
                    negated_temporal=bool(re.search(r'(?:不是|并非)\s*(?:说\s*)?$',prefix)
                        and not re.search(r'(?:不是|并非|不可能|不能|不会)\s*(?:说\s*)?(?:不是|并非)\s*(?:说\s*)?$',prefix))
                    if not negated_temporal:
                        own=temporal[1].startswith(('我','本方'))
                        if own!=(match[1]!='peer_quote'):
                            raise ValueError('Public reference is attributed to the wrong speaker')
                        if match[1]=='own_quote':
                            raise ValueError('Current quotation cannot be described as historical')
            speaker=re.search(r'(我(?:们|方|这边)?|本方|你(?:们|方|这边)?|您(?:方)?|贵方|对方)'
                r'(?:现在|这次|刚才|之前|那边|这边|已经|也|再|就|还是|先|愿意|可以|能|目前|的|上次|曾经){0,5}'
                +assertion+r'\s*$',clause) if not negated_temporal else None
            if speaker:
                own=speaker[1].startswith(('我','本方'))
                if own!=(match[1]!='peer_quote'):
                    raise ValueError('Public reference is attributed to the wrong speaker')
        parts.append(fragment(match[1]));cursor=match.end()
    if cursor<len(template):parts.append({'role':'FREE_TEXT','text':template[cursor:]})
    if any(part['role']=='OWN_REQUESTED_QUANTITY_OFFER' for part in parts):
        if re.search(r'(?:你|您|贵方|对方).{0,8}(?:已(?:经)?(?:确认|确定)|(?:确认|确定)的|实报|已报|最终(?:报价|报量|可做|能做))',template):
            raise ValueError('Public reference is attributed to the wrong speaker')
    for part in parts:
        if part['role']=='FREE_TEXT':
            free=part['text'].replace(str(context.get('bond_code','')),'') if context.get('bond_code') else part['text']
            if (re.search(r'[{}\d]',free)
                    or re.search(r'(?<![A-Za-z0-9_])(?:NaN|[+-]?Inf(?:inity)?)\s*(?:万|亿|[%％])',free,re.I)
                    or re.search(r'(?:净价|全价)\s*[+-]?(?:NaN|Inf(?:inity)?)',free,re.I)
                    or re.search(r'[零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億]+点[零〇一二三四五六七八九壹贰叁肆伍陆柒捌玖]+'
                                 r'|[零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億]+\s*(?:BP|基点|个点|厘|万|亿|元|[%％])'
                                 r'|(?:百分之|千分之)[零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億]+',free,re.I)):
                raise ValueError('Economic numbers and facts must use verified slots')
    text=''.join(part['text'] for part in parts)
    if not 1<=len(text.strip())<=1500:raise ValueError('Bound reply must contain 1..1500 characters')
    if question=='ASK_SETTLEMENT':
        has_options=any(part['role']=='SETTLEMENT_QUESTION' for part in parts)
        settlement_topic=has_options or re.search(r'结算|交割|交收',text)
        asks_question=re.search(r'请|吗|么|？|\?|多少|能否|是否|什么|怎么|还是',text)
        asks_date=has_options or re.search(r'日期|时间|日子|哪天|何时|什么时候|几号|(?:结算|交割|交收)日|(?:今|明|后)[天日]|当天|当日|次日',text)
        mechanism_only=re.search(r'(?:现金|实物)(?:结算|交割|交收)',text) and not asks_date
        if not settlement_topic or not asks_question or mechanism_only:
            raise ValueError('Settlement clarification must actually ask for settlement')
    if question=='ASK_CURRENT_QUOTE' and not re.search(r'请|确认|吗|么|？|\?|多少|能否|是否',text):
        raise ValueError('Current quotation clarification must actually request confirmation')
    proposal={'kind':kind,'text':text,'render_plan':plan}
    if price:proposal['our_price']=price
    economic=''.join(part['text'] for part in parts if part['role'] in ('OWN_QUOTE','OWN_REQUESTED_QUANTITY_OFFER','FREE_TEXT'))
    language_error=public_language_error(text)
    if language_error:raise ValueError(language_error)
    return {'proposal':proposal,'economic_text':economic,'parts':parts}
