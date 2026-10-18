"""High-recall literal spans and a bounded, local batch of field classifiers.

No candidate is a fact: bond, speaker, side, negation and reference relationships
still require the joint semantic pass. Offsets always address the original text.
"""
import re
import unicodedata
from .text_roles import non_trading_spans, numeric_spelling


def extract_spans(text, codes=()):
    spans = []
    def add(start, end, lexical):
        spans.append({'start': start, 'end': end, 'token': text[start:end], 'lexical': lexical})
    for start, end, role in non_trading_spans(text):
        add(start, end, role)
    for code in sorted(set(codes), key=lambda x: (-len(x), x)):
        if code:
            for match in re.finditer(re.escape(code), text, re.I):
                add(match.start(), match.end(), 'BOND_CODE')
    for match in re.finditer(r'(?:(?:中债|估值)\s*)?(?:加|减|[+＋－−-])\s*\d+(?:[.．]\d+)?\s*[bB][pP]', text):
        add(match.start(), match.end(), 'BP')
    code_spans=[(s['start'],s['end']) for s in spans if s['lexical']=='BOND_CODE']
    # A rating-shaped suffix overlapping a known code (SYN-A3.10) must not
    # swallow the following price. Keep both annotations, but protect the code.
    protected = [(s['start'],s['end']) for s in spans if s['lexical']!='RATING'
                 or not any(a<s['end'] and s['start']<b for a,b in code_spans)]
    for match in re.finditer(r'(?<![\d.．])[-−－+＋]?\d+(?:[.．]\d+)?(?:\s*(?:亿|万|[wW](?![a-zA-Z])|[%％]|元|份))?', text):
        if not any(a < match.end() and match.start() < b for a,b in protected):
            add(match.start(), match.end(), 'NUMBER')
    words = {
        'DIRECTION': r'\b(?:bid|ofr|offer)\b|收|出|买|卖',
        'NEGATION': r'不是|不再|不|没|无',
        'REFERENCE': r'刚才|那只|那个|原价|之前|按你|上次|这只',
        'CORRECTION': r'改|更正|写错|说错',
        'WITHDRAWAL': r'撤回|撤掉|取消|不出了|不收了',
        'QUESTION': r'吗|么|[?？]|多少|是否',
        'CONDITION': r'如果|只要|条件|否则|的话',
        'NEGOTIATION_SPACE': r'还能谈|还可以谈|不能再|最终|到头|还可|实价|意向',
    }
    for lexical, pattern in words.items():
        for match in re.finditer(pattern, text, re.I):
            add(match.start(), match.end(), lexical)
    unique = {(s['start'],s['end'],s['lexical']):s for s in spans}
    return sorted(unique.values(), key=lambda s:(s['start'],s['end'],s['lexical']))


def classify_spans(text, spans):
    """Equivalent batch classification; no per-token threads or network calls."""
    result = []
    for span in spans:
        start, end = span['start'], span['end']
        if span['token'] != text[start:end]:
            raise ValueError('Candidate no longer matches immutable source')
        token = unicodedata.normalize('NFKC', span['token']).replace('−','-')
        lexical = span['lexical']; value = token
        if lexical == 'NUMBER':
            compact = re.sub(r'\s+', '', token).removeprefix('+')
            if compact.endswith(('亿','万','w','W')):
                roles = ['QUANTITY']; value = compact
            elif compact.endswith('%'):
                roles = ['YIELD']; value = compact[:-1]
            elif compact.endswith(('元','份')):
                roles = ['NON_TRADE_AMOUNT']
            else:
                label = re.search(r'(净价|全价)\s*[:：]?\s*$', text[:start])
                if label is None:
                    label = re.match(r'\s*(净价|全价)', text[end:])
                if label:
                    roles = ['NET_PRICE' if label[1] == '净价' else 'DIRTY_PRICE']
                elif re.fullmatch(r'\d{2}', compact):
                    roles = ['YIELD_SUFFIX','QUANTITY']
                elif '.' in compact or compact.startswith('-'):
                    roles = ['YIELD']
                elif int(compact) >= 100:
                    roles = ['QUANTITY']
                else:
                    roles = ['YIELD','QUANTITY']
                value = compact
        elif lexical == 'BP':
            roles = ['CHINABOND_BP']
            amount = re.search(r'\d+(?:\.\d+)?',token)[0]
            value = ('-' if re.search(r'减|-', token) else '') + amount
        elif lexical == 'DIRECTION':
            left, right = text[:start], text[end:]
            compound = ((token == '出' and (re.match(r'差|现|版|来|去|口|席|生', right) or re.search(r'指|支|提|突|进|付$', left[-1:])))
                        or (token == '收' and re.match(r'到|入|益|集|据|藏|获', right))
                        or (token == '买' and re.match(r'方|家', right))
                        or (token == '卖' and re.match(r'方|家', right)))
            roles = ['NON_DIRECTION'] if compound else ['SIDE_BID' if token.lower() in ('收','买','bid') else 'SIDE_OFR']
        else:
            roles = [lexical]
        result.append({**span, 'roles': roles, 'value': value})
    return result


def collect_candidates(context):
    """Stable IDs within one immutable context, retaining message revision."""
    messages = {}
    members = context.get('members', [])
    for member in members:
        for row in member.get('context', []):
            messages[row['message_id']] = row
    for row in context.get('context', []) + context.get('correction_context', []) + context.get('messages', []):
        messages[row['message_id']] = row
    codes = [m.get('bond_code','') for m in members] + [context.get('bond_code','')]
    result = []
    for message in messages.values():
        text = message['text']
        for span in classify_spans(text, extract_spans(text, codes)):
            result.append({**span, 'id':'s'+str(len(result)), 'message_id':message['message_id'],
                           'revision':message.get('ingest_revision',0), 'direction':message.get('direction')})
    return result


def literal_member_hints(context, candidates):
    """Index an explicit same-clause bond label, never infer a speaker or fact.

    Unnamed references and multiple labels stay ambiguous. All candidates and
    complete source messages remain available to the joint semantic pass.
    """
    members = context.get('members', [])
    messages = {row['message_id']: row for member in members for row in member.get('context', [])}
    messages.update({row['message_id']: row for row in context.get('messages', [])})
    hints = {}
    numeric = {'YIELD', 'YIELD_SUFFIX', 'CHINABOND_BP', 'NET_PRICE', 'DIRTY_PRICE', 'QUANTITY'}
    for candidate in candidates:
        if not numeric.intersection(candidate['roles']):
            continue
        text = messages.get(candidate['message_id'], {}).get('text', '')
        prefix = re.split(r'[,，;；。！？!?\n]', text[:candidate['start']])[-1]
        clause_start = candidate['start'] - len(prefix)
        suffix = re.split(r'[,，;；。！？!?\n]', text[candidate['start']:], maxsplit=1)[0]
        if re.search(r'另一|那只|这只|前者|后者|两只|两券|都|同价|分别|各自|按|照', prefix + suffix):
            continue
        named = set()
        for member in members:
            code = member.get('bond_code')
            if code and any(clause_start <= match.start() and match.end() <= candidate['start']
                            for match in re.finditer(r'(?<![a-zA-Z0-9_.-])'+re.escape(code)+
                                                     r'(?![a-zA-Z0-9_.-])', text, re.I)):
                named.add(member['member_id'])
        # Duplicate/overlapping known codes must not select one arbitrary owner.
        if len(named) == 1:
            hints[candidate['id']] = sorted(named)
    return hints


def direct_price_question(text, bond_codes=()):
    """Recognize a complete modal-before-price question, not any question mark."""
    text=numeric_spelling(text)
    for code in sorted(bond_codes,key=len,reverse=True):
        text=re.sub(r'^\s*'+re.escape(numeric_spelling(code))+r'(?![a-zA-Z0-9])\s*','',text,count=1,flags=re.I)
    number=r'[+-]?\d+(?:\.\d+)?'
    return bool(re.fullmatch(r'\s*(?:能否|可否|是否能|能不能|可以|能)\s*'+number+
        r'\s*%?\s*(?:收|出|买|卖)\s*'+number+r'\s*(?:万|亿|[wW])?\s*(?:吗|么)?[?？]\s*',text))


def no_change_acknowledgement(context):
    """Select a shorter prompt only for closed, single-member wait replies.

    This changes no observations or evidence. The model and normal contract
    still handle the full input, including any known outgoing question.
    """
    members = context.get('members', [])
    if (len(members) != 1 or 'validation_feedback' in context
            or any(context.get(key) for key in ('tool_results', 'history_results', 'correction_context', 'corrections'))):
        return False
    member = members[0]
    code, mid = member.get('bond_code'), member.get('member_id')
    if not isinstance(code, str) or not code or not mid:
        return False
    pattern = (r'\s*(?:'+re.escape(code)+r'(?![a-zA-Z0-9_.-])\s*[:：,，]?\s*)?'
               r'(?:稍等|等一下)\s*[。.!！]?\s*')
    outbound = context.get('known_outbound_scopes', {})
    inbound = False
    for message in context.get('messages', []):
        if message.get('historical') or message.get('kind', 'message') != 'message':
            return False
        if message.get('direction') == 'out':
            if not isinstance(outbound, dict) or outbound.get(message.get('message_id')) != [mid]:
                return False
        elif message.get('direction') == 'in':
            text = message.get('text')
            if not isinstance(text, str) or not re.fullmatch(pattern, text, re.I):
                return False
            inbound = True
        else:
            return False
    return inbound


def direct_priced_calculation(context):
    """A narrow prompt route, never a fact or permission to discard output.

    One complete current quote precedes a separate, explicitly hypothetical
    yield comparison with two supplied operands. Anything else uses the full
    joint/reference prompt, including a second side with an unstated price.
    """
    members=context.get('members',[]);messages=context.get('messages',[])
    if len(members)!=1 or len(messages)!=1:return False
    message=messages[0];code=members[0].get('bond_code')
    if not code or message.get('direction')!='in' or message.get('historical'):return False
    text=numeric_spelling(message.get('text',''))
    marker=re.search(r'(?<=[。；;，,])\s*(?:另外|顺便|另请)[^。；;：:\d]{0,28}(?:核算|计算|比较|算一下)\s*[:：]?',text)
    if not marker:return False
    quoted=text[:marker.start()].rstrip('。；;，, \t\n')
    price=r'(?:[+-]?\d+\.\d+%?|[+-]?\d+%)'
    size=r'\d+(?:\.\d+)?\s*(?:万|亿|[wW])?'
    if not re.fullmatch(r'\s*'+re.escape(numeric_spelling(code))+r'\s*'+price+
                        r'\s*(?:出|收|买|卖|ofr|bid)\s*'+size+r'\s*',quoted,re.I):return False
    calculation=text[marker.end():]
    if not (re.search(r'假设',calculation) and re.search(r'收益率',calculation)
            and re.search(r'(?:差|差值|价差|利差).{0,12}(?:BP|基点)',calculation,re.I)):return False
    if re.search(r'按|照|原价|那个价|不变|改|撤|如果|假如|条件|别家|客户|\b(?:bid|ofr|offer)\b',calculation,re.I):return False
    candidates=classify_spans(calculation,extract_spans(calculation))
    numeric=[span for span in candidates if span['lexical'] in ('NUMBER','BP','DATE','SETTLEMENT','TENOR')]
    if len(numeric)!=2 or any(span['roles']!=['YIELD'] for span in numeric):return False
    first,second=numeric
    # Two numbers alone do not establish whose comparison this is. Unknown
    # bond names/identifiers or extra instructions fall back to the full pass.
    return bool(re.fullmatch(r'假设(?:同一|相同)(?:到期|行权)?收益率口径\s*[，,:：]?\s*',calculation[:first['start']])
        and re.fullmatch(r'\s*(?:和|与|跟)\s*(?:(?:你|您|贵方|我方)(?:之前|刚才|上次|已报)?\s*)?',calculation[first['end']:second['start']])
        and re.fullmatch(r'\s*(?:收|出|买|卖)?\s*的?\s*(?:相差|差值|价差|利差|差)(?:是)?多少\s*(?:BP|基点)[？?]?'
                         r'(?:[。.]?\s*(?:答复|回复)不重复(?:这两个|两个)?(?:原始)?收益率[，,]\s*不作成交[。.]?)?\s*',
                         calculation[second['end']:],re.I))


def window_reference_gaps(context, scopes=None):
    """Locate explicitly named price references in the supplied window only."""
    members={m['member_id']:m for m in context.get('members',[])}
    current=context.get('messages',[])
    gaps={}
    for message in current:
        if message.get('direction')!='in':continue
        text=message['text'];refs=set()
        for match in re.finditer(r'(?:消息\s*|\bmessage\s+)([a-zA-Z0-9_-]{6,160})',text,re.I):
            near=text[max(0,match.start()-6):match.start()]+text[match.end():match.end()+24]
            if re.search(r'按|照|价|报盘',near):refs.add(match[1])
        if not refs:continue
        mids=(scopes.get(message['message_id'],[]) if scopes is not None else
              [mid for mid,m in members.items() if m.get('bond_code') and m['bond_code'].casefold() in text.casefold()])
        for mid in mids:
            if mid not in members:continue
            loaded={m['message_id'] for m in members[mid].get('context',[])+current}
            missing=refs-loaded
            if missing:
                gap=gaps.setdefault(mid,{'source_ids':[],'missing_ids':[]})
                gap['source_ids']=sorted(set(gap['source_ids'])|{message['message_id']})
                gap['missing_ids']=sorted(set(gap['missing_ids'])|missing)
    return gaps
