"""Deterministic economic text checks; semantic interpretation remains in A/B.

Classify quantity tokens before prices. Never search untyped numbers against
an internal limit, and never treat an outgoing quantity as the peer's amount.
"""
from __future__ import annotations
import re
from .prices import InvalidFact, number, rounded_yield, volume_wan
from .text_roles import mask_non_trading, numeric_spelling
from .slots import direct_price_question

NUM = r'[+-]?\d+(?:\.\d+)?'
QUANTITY = re.compile(r'(?<![\da-zA-Z.+-])(' + NUM + r')\s*(亿|万|[wW])(?![a-zA-Z])')


def _explicit_adoption(text, bond_codes):
    """Recognize a narrow affirmative price adoption, never infer a new price.

    Negated/reported/conditional prefixes and a later correction do not fit
    this form. Other reference language remains a joint semantic decision.
    """
    text = numeric_spelling(text)
    for code in sorted(bond_codes, key=len, reverse=True):
        text = re.sub(r'^\s*'+re.escape(code)+r'(?![a-zA-Z0-9])\s*', '', text, count=1, flags=re.I)
    match = re.match(r'\s*(?:就|那就|好的[，,]?)?按(?:照)?(?:你|您|贵方)'
        r'(?:(?:刚才|上次|之前|那个|那条|所报|报的)\s*){0,6}'
        r'(?P<price>'+NUM+r')\s*[%％]?\s*(?:收|出|买|卖)?\s*的?\s*(?:报价|价格|价)'
        r'\s*[，,]\s*(?:我|本方)(?:这边)?\s*(?P<side>收|出|买|卖)\s*'+NUM+r'\s*(?:万|亿|[wW])', text)
    if not match:
        return None
    remainder = text[match.end():]
    if (re.search(r'现在|目前|改为|更正|不过|但是|但|不是|不按|别按|如果|假如|条件|吗|么|[?？]', remainder)
            or economics(remainder, bond_codes=bond_codes)[0]):
        return None
    return rounded_yield(match['price'].removeprefix('+')), 'bid' if match['side'] in ('收', '买') else 'ofr'


def economics(text, *, bond_codes=(), identifiers=()):
    """Return classified literal amounts and price spellings, with positions."""
    masked = text
    for code in sorted(set(bond_codes)|set(identifiers), key=len, reverse=True):
        if code:
            masked=re.sub(re.escape(code),lambda m:' '*len(m[0]),masked,flags=re.I)
    masked=re.sub(r'(?<![a-zA-Z0-9])(?:[a-fA-F0-9]{24,160}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?![a-zA-Z0-9])',
                  lambda m:' '*len(m[0]),masked)
    masked = numeric_spelling(mask_non_trading(masked))
    quantities = []
    for match in QUANTITY.finditer(masked):
        quantities.append((volume_wan(match[1].removeprefix('+') + match[2]), match.start(), match.end()))
    for _value, start, end in quantities:
        masked = masked[:start] + ' ' * (end-start) + masked[end:]
    prices = []
    bare = []
    for match in re.finditer(r'(?<![\d.+-])(' + NUM + r')(?![\d.])', masked):
        raw = match[1].removeprefix('+'); tail = masked[match.end():]
        # BP offsets are not literal yield quotes.
        if re.match(r'\s*(?:BP|bp)', tail):
            continue
        if '.' in raw or re.match(r'\s*[%％收出买卖]', tail):
            prices.append((raw, match.start(), match.end()))
        else:
            bare.append((raw, match.start(), match.end()))
    # Established desk notation: "20 5000" is a yield suffix + wan amount.
    # A lone integer without a role remains unclassified.
    for i, (raw, start, end) in enumerate(bare):
        if len(raw) == 2 and i+1 < len(bare) and number(bare[i+1][0]) >= 100:
            prices.append((raw, start, end))
        elif number(raw) >= 100 and (prices or quantities):
            quantities.append((number(raw), start, end))
    return prices, quantities


def token_side(text,start,end):
    """Bind one classified numeric field to its explicit side, not whole sentence.
    No final-price/intent decision is made by this deterministic association.
    """
    direct=re.match(r'\s*(?:[%％]|万|亿|[wW])?\s*(收|出|买|卖|bid\b|ofr\b|offer\b)',text[end:],re.I)
    if direct:
        return 'bid' if direct[1].lower() in ('收','买','bid') else 'ofr'
    anchors=list(re.finditer(r'\b(bid|ofr|offer)\b|(?<=[\d%％])\s*(收|出|买|卖)|(?<![\u4e00-\u9fff])(收|出|买|卖)(?=\s*\d)',text,re.I))
    prior=[a for a in anchors if a.end()<=start]
    if prior:
        found=prior[-1]
        word=next(v for v in found.groups() if v).lower()
        return 'bid' if word in ('收','买','bid') else 'ofr'
    return None


def _literal_quote_quantities(text, bond_codes):
    """Read complete price-side-size clauses, not reported or conditional lots.

    This supplies a contradiction check only. It does not establish quote
    ownership, finality or permission to replace a model-selected field.
    """
    # A following correction/reference may qualify an earlier complete clause.
    # This narrow check must abstain rather than invent precedence for them.
    if re.search(r'改|更正|说错|写错|不是|不再|不算|作废|撤|取消|但|不过|如果|假如|条件|的话|按|照|昨天|之前|上次|原来', text):
        return
    for clause in re.split(r'[,，;；。\n]', numeric_spelling(text)):
        for code in sorted(bond_codes, key=len, reverse=True):
            clause = re.sub(r'^\s*'+re.escape(code)+r'(?![a-zA-Z0-9_.-])\s*',
                            '', clause, count=1, flags=re.I)
        prices, quantities = economics(clause)
        if len(prices) != 1 or len(quantities) != 1:
            continue
        _price, start, end = prices[0]
        amount, qstart, qend = quantities[0]
        if not re.fullmatch(r'\s*(?:(?:我|我们|本方)(?:这边)?\s*)?(?:(?:净价|全价)\s*)?', clause[:start]):
            continue
        side = re.fullmatch(r'\s*%?\s*(收|出|买|卖|bid|ofr|offer)\s*', clause[end:qstart], re.I)
        if not side or clause[qend:].strip():
            continue
        yield ('bid' if side[1].lower() in ('收', '买', 'bid') else 'ofr'), amount


def validate_observation(snapshot, observation, aliases, *, raw_price=None):
    """Check the model's field evidence against actual, bond-scoped fragments.

    The interpretation explains intention/finality; numeric facts must also
    be present in the cited text. This does not turn keyword matches into a
    final-price decision.
    """
    mid = observation['member_id']
    member = next(p for p in snapshot['members'] if p['member_id'] == mid)
    changes, evidence = observation['changes'], observation['evidence']
    if not isinstance(changes, dict) or not isinstance(evidence, dict):
        raise InvalidFact('Invalid field evidence')
    known = {m['message_id']: m for p in snapshot['members'] for m in p['context']}
    known.update({m['message_id']: m for m in snapshot['messages']})
    known.update({m['message_id']: m for m in snapshot.get('correction_context',[])})
    identifiers=tuple(known)+tuple(str(m[key]) for m in known.values()
                                  for key in ('native_id','event_id') if m.get(key))
    segments = observation.get('segments', [])
    if not isinstance(segments, list):
        raise InvalidFact('Segments must be actual message fragments')
    excerpts = {}
    for seg in segments:
        if not isinstance(seg, dict) or set(seg) != {'message_id', 'text'}:
            raise InvalidFact('Invalid segment fields')
        message = known.get(seg['message_id'])
        if not message or not isinstance(seg['text'], str) or not seg['text'] or seg['text'] not in message['text']:
            raise InvalidFact('Segment is not present in the actual message')
        excerpts.setdefault(seg['message_id'], []).append(seg['text'])

    def hits(text):
        result = []
        for alias, members in aliases.items():
            if len(members) != 1:
                continue
            for match in re.finditer(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])', text.casefold()):
                result.append((match.start(), match.end(), next(iter(members))))
        return sorted(result)

    def source_parts(ref, *, preserve_prefix=False):
        message = known.get(ref)
        if not message:
            raise InvalidFact('Evidence is outside the supplied actual context')
        anchors = hits(message['text'])
        if not anchors:
            return [message['text']]
        selected = []
        for i, (start, _end, target) in enumerate(anchors):
            if target != mid:
                continue
            end = next((p for p, _q, m in anchors[i+1:] if m != mid), len(message['text']))
            clause = max((message['text'].rfind(mark, 0, start) for mark in ';；。\n'), default=-1)+1
            prefix = message['text'][clause:start] if preserve_prefix else ''
            selected.append(prefix+message['text'][start:end])
        if not selected:
            raise InvalidFact('Evidence explicitly names another bond')
        return selected

    def clarified_member(ref):
        return any(c['message_id'] == ref and mid in c['member_ids'] and
                   any(mid in {m for _a, _b, m in hits(known[e]['text'])} for e in c['evidence'] if e in known)
                   for c in snapshot.get('corrections', []))

    def parts(ref):
        message = known.get(ref)
        if not message:
            raise InvalidFact('Evidence is outside the supplied actual context')
        if ref in excerpts:
            selected = excerpts[ref]
            clarified = clarified_member(ref)
            for fragment in selected:
                owners = {m for _a, _b, m in hits(fragment)}
                if owners and owners != {mid}:
                    raise InvalidFact('Fragment explicitly belongs to a different bond')
                whole_owners = {m for _a, _b, m in hits(message['text'])}
                if len(whole_owners) > 1 and not owners and not clarified:
                    raise InvalidFact('Multibond fragment needs its explicit bond anchor')
            return selected
        return source_parts(ref)

    def texts(field, *, inbound_only=False):
        refs = evidence.get(field)
        if not isinstance(refs, list) or not refs:
            raise InvalidFact('Every field needs actual message references')
        return [part for ref in refs if not inbound_only or known.get(ref, {}).get('direction') == 'in'
                for part in parts(ref)]

    all_text = '\n'.join(part for field in evidence for part in texts(field, inbound_only=True))
    # Earlier indicative/negotiable messages are history, not a permanent ban.
    # Contradiction checks apply to the latest incoming evidence for this fact.
    incoming_refs={ref for refs in evidence.values() for ref in refs
                   if known.get(ref,{}).get('direction')=='in'}
    latest=max((known[ref]['ingest_revision'] for ref in incoming_refs),default=-1)
    latest_text='\n'.join(part for ref in incoming_refs if known[ref]['ingest_revision']==latest for part in parts(ref))
    # Preserve contextual classification supplied by the model; these narrow
    # checks only reject an explicit incompatible claim. They never infer FINAL.
    if re.search(r'现在|目前|改为',latest_text):
        latest_text=re.split(r'现在|目前|改为',latest_text)[-1]
    latest_text=re.sub(r'(?:不是|不再是|并非|不只是|已经不是)(?:一个)?意向(?:价)?','',latest_text)

    interpretation = observation.get('interpretation')
    if interpretation is not None:
        if (not isinstance(interpretation, dict) or interpretation.get('quote_owner') != 'COUNTERPARTY'
                or interpretation.get('intent') not in ('QUOTE', 'INDICATIVE', 'QUESTION', 'REPORTED', 'WITHDRAW')
                or not isinstance(interpretation.get('reason'), str) or not interpretation['reason'].strip()):
            raise InvalidFact('Quote interpretation lacks counterparty/contextual evidence')
        if interpretation['intent'] in ('INDICATIVE', 'QUESTION', 'REPORTED'):
            if changes.get('assertion') != interpretation['intent']:
                raise InvalidFact('Semantic assertion differs from the saved quote assertion')
    # These are explicit contradictions to COUNTERPARTY/QUOTE, not rules that
    # infer a final price from a keyword. Finality still requires contextual
    # interpretation and price-version evidence.
    if changes.get('assertion','QUOTE')=='QUOTE':
        for ref in {ref for refs in evidence.values() for ref in refs}:
            message=known[ref]
            if message['direction']=='in' and message['ingest_revision']==latest:
                if any(direct_price_question(part,tuple(aliases)) for part in source_parts(ref,preserve_prefix=True)):
                    raise InvalidFact('Explicit asking price is not a counterparty quote')
        if re.search(r'(?:是你|你(?:刚才)?说的|你(?:刚才)?报的).*[?？]',all_text):
            raise InvalidFact('Question about our quote is not a counterparty quote')
        if (re.search(r'(?:客户|别人|别家|市场|有人|他).*(?:说|报)',all_text) and
            re.search(r'(?:我|本方|我们).*(?:没|不).*(?:报价|报盘|出券|做)',all_text)):
            raise InvalidFact('Explicit third-party quote cannot replace this counterparty price')
    if changes.get('finality') == 'FINAL':
        if not interpretation or interpretation.get('negotiation_space') != 'FINAL' or interpretation['intent'] != 'QUOTE':
            raise InvalidFact('Finality requires contextual interpretation, not a boolean or keyword')
        if re.search(r'还能(?:再)?让|还可以再(?:让|降|提高)|还有谈判空间|不是最终|仅供参考|先报个意向', latest_text):
            raise InvalidFact('Finality contradicts the cited negotiation context')
        if re.search(r'(?:是你说的|最终价(?:格)?(?:吗|么)|暂时没有报价|只是转述)', latest_text):
            raise InvalidFact('A question or third-party quotation is not our counterparty final offer')
    if re.search(r'(?:只是|仅为|仅供|先报|暂报).{0,6}意向|仅供参考|只是参考价|不是实价', latest_text) and changes.get('assertion', 'QUOTE') == 'QUOTE':
        raise InvalidFact('Indicative evidence cannot become an executable counterparty quote')
    side = observation['side']
    if side not in ('bid', 'ofr'):
        raise InvalidFact('Invalid quote side')
    directions = set()
    for text in texts('price', inbound_only=True) if 'price' in changes and changes['price'] is not None else []:
        for explicit in re.finditer(r'(?:我|本方)(?:这边)?\s*(收|出|买|卖)',text):
            directions.add('bid' if explicit[1] in ('收','买') else 'ofr')
        for found in re.finditer(r'(?:\d\s*[%％]?\s*)(收|出|买|卖)|\b(bid|ofr|offer)\b', text, re.I):
            word = found[1] or found[2].lower()
            directions.add('bid' if word in ('收', '买', 'bid') else 'ofr')
    if directions and side not in directions:
        raise InvalidFact('Quote direction contradicts the evidence')
    if 'price' in changes and changes['price'] is not None:
        value = number(changes['price']['value'])
        literal = raw_price or changes['price']
        notation = literal.get('notation', 'ABSOLUTE')
        if notation == 'ABSOLUTE' and changes['price']['kind'] == 'YIELD' and changes.get('assertion', 'QUOTE') == 'QUOTE':
            for ref in evidence.get('price', []):
                if known[ref]['direction'] != 'in' or known[ref]['ingest_revision'] != latest:
                    continue
                for text in source_parts(ref, preserve_prefix=True):
                    adopted = _explicit_adoption(text, tuple(aliases))
                    if adopted is not None and adopted[1] == side and adopted[0] != value:
                        raise InvalidFact('Selected price contradicts the explicitly adopted current price')
        candidates = []
        outbound_parts = {part for ref in evidence['price'] if known[ref]['direction']=='out' for part in parts(ref)}
        for text in texts('price'):
            prices, _quantities = economics(text, bond_codes=tuple(aliases),identifiers=identifiers)
            for raw, start, end in prices:
                explicit_side=token_side(text,start,end)
                if text not in outbound_parts and explicit_side not in (None,side):
                    continue
                candidates.append(raw)
        expected = number(literal['value']) if notation != 'ABSOLUTE' else value
        if notation == 'CHINABOND_BP':
            offsets=[]
            for text in texts('price'):
                for match in re.finditer(r'(加|减|\+|-)?\s*(\d+(?:\.\d+)?)\s*(?:BP|bp|收|出)', numeric_spelling(text)):
                    offsets.append(number(match[2])*(-1 if match[1] in ('减','-') else 1))
            supported=expected in offsets
        else:
            supported = any((number(raw) == expected if notation == 'YIELD_SUFFIX' or changes['price']['kind'] != 'YIELD'
                             else rounded_yield(raw) == expected) for raw in candidates)
        if not supported:
            raise InvalidFact('Price is not supported by its classified raw evidence')
        # The model may select a substring without the cash-basis label. Read
        # the actual bond-scoped source to reject an explicit incompatible
        # label; never rewrite the price or infer an unstated basis here.
        ordered_refs = sorted(set(evidence['price']), key=lambda ref: (
            known[ref]['direction'] != 'in', -known[ref]['ingest_revision']))
        for ref in ordered_refs:
            if not re.search(r'净价|全价', known[ref]['text']):
                continue
            selected_positions = None
            if clarified_member(ref) and ref in excerpts:
                # A correction can change the bond association, not which raw
                # numeric fragment supplied this price. Expand only around its
                # unique original position; another old bond is not evidence.
                selected_positions = []
                for fragment in excerpts[ref]:
                    if not any(number(raw) == expected for raw, _start, _end in
                               economics(fragment, bond_codes=tuple(aliases), identifiers=identifiers)[0]):
                        continue
                    matches = list(re.finditer(re.escape(fragment), known[ref]['text']))
                    if len(matches) != 1:
                        raise InvalidFact('Corrected price fragment is not unique in its raw message')
                    selected_positions.append(matches[0].span())
                basis_parts = [known[ref]['text']]
            else:
                basis_parts = source_parts(ref)
            labelled = set()
            for text in basis_parts:
                prices, _ = economics(text, bond_codes=tuple(aliases), identifiers=identifiers)
                for raw, start, end in prices:
                    if number(raw) != expected:
                        continue
                    if selected_positions is not None and not any(a <= start and end <= b for a, b in selected_positions):
                        continue
                    if known[ref]['direction'] == 'in' and token_side(text, start, end) not in (None, side):
                        continue
                    label = re.search(r'(净价|全价)\s*[:：]?\s*$', text[:start])
                    if label is None:
                        label = re.match(r'\s*(净价|全价)', text[end:])
                    if label:
                        labelled.add('NET_PRICE' if label[1] == '净价' else 'DIRTY_PRICE')
            if labelled:
                if len(labelled) != 1:
                    raise InvalidFact('Price evidence has conflicting explicit cash bases')
                if changes['price']['kind'] not in labelled:
                    raise InvalidFact('Price kind contradicts the explicit cash basis in its raw evidence')
                break
        if changes['price']['kind'] == 'YIELD' and re.search(r'净价|全价', '\n'.join(texts('price'))):
            raise InvalidFact('Cash-price evidence cannot be saved as a yield')
    if changes.get('volume_wan') is not None:
        amount = number(changes['volume_wan'])
        # History can supply a referenced quantity, but cannot override an
        # explicitly different current lot. Read the actual scoped source;
        # cropped model segments must not hide that contradiction.
        current_amounts = {value for ref in incoming_refs
                           if known[ref]['ingest_revision'] == latest
                           for text in source_parts(ref, preserve_prefix=True)
                           for direction, value in _literal_quote_quantities(text, tuple(aliases))
                           if direction == side}
        if current_amounts and amount not in current_amounts:
            raise InvalidFact('Selected quantity contradicts the explicit current counterparty lot')
        amounts = []
        for text in texts('volume_wan', inbound_only=True):
            for value, start, end in economics(text, bond_codes=tuple(aliases),identifiers=identifiers)[1]:
                before = list(re.finditer(r'收|出|买|卖|\bbid\b|\bofr\b|\boffer\b', text[:start], re.I))
                after = re.match(r'\s*(收|出|买|卖)', text[end:])
                word = before[-1][0].lower() if before else after[1] if after else None
                direction = 'bid' if word in ('收','买','bid') else 'ofr' if word else None
                if direction is None or direction == side:
                    amounts.append(value)
        if amount not in amounts:
            # A classified standalone quantity is supported only with a same-side
            # previous quantity/unit context; our requested size is never used.
            previous = member['quotes'][side].get('volume_wan')
            standalone = any(re.fullmatch(r'\s*'+re.escape(str(amount))+r'\s*', t) for t in texts('volume_wan', inbound_only=True))
            if not standalone or previous is None and not changes.get('price'):
                raise InvalidFact('Quantity is not supported by counterparty evidence')
    return {key: observation[key] for key in ('member_id', 'side', 'changes', 'evidence')}


def validate_public_text(context, proposal, *, economic_text=None):
    text = proposal.get('text')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1500:
        raise InvalidFact('Reply text must contain 1..1500 characters')
    if re.search(r'内部|底价|限价|授权|步长|目标(?:价|量)|其他对手|别家给|system.prompt|tool_budget|policy_version|member_id|group_id',text,re.I):
        raise InvalidFact('Public reply exposes private policy or internal context')
    # The tool negotiates and collects quotes, never executes or reserves a lot.
    for clause in re.split(r'[，,。;；\n]', text):
        if re.search(r'\d+(?:\.\d+)?\s*(?:万|亿)\s*确认(?!报价)',clause):
            raise InvalidFact('Quantity confirmation must explicitly refer to a quotation, not execution')
        if re.search(r'成交|锁(?:量|定|券)|占(?:用)?额度|确认买入|确认卖出|就(?:按.*)?要了', clause):
            if not re.search(r'不(?:会|作|做|能)?(?:自动)?(?:成交|锁|占|确认)', clause):
                raise InvalidFact('Reply promises execution or reserves a counterparty lot')
    economic_text = text if economic_text is None else economic_text
    prices, quantities = economics(economic_text, bond_codes=(context.get('bond_code', ''),))
    kind = proposal['kind']
    if kind != 'REPLY' and prices:
        raise InvalidFact('Nonquote reply contains an unvalidated public price')
    if kind == 'SOCIAL' and quantities:
        raise InvalidFact('Social text cannot contain an economic quantity')
    if kind == 'REPLY':
        expected = number(proposal['our_price']['value'])
        normalize=rounded_yield if proposal['our_price']['kind']=='YIELD' else number
        if not prices or any(normalize(p) != expected for p, _a, _b in prices):
            raise InvalidFact('All public price tokens must match the structured quote')
        own_side = context['our_side']
        if own_side == 'BUY' and re.search(r'出|卖', economic_text) or own_side == 'SELL' and re.search(r'收|买', economic_text):
            raise InvalidFact('Reply direction differs from our authorized side')
    if not quantities:
        return
    target = number(context['plan']['volume_wan'])
    if kind == 'ASK_VOLUME':
        ceiling = number(context['group_summary']['reported_shortfall_wan'])
    elif kind == 'REPLY':
        quote = context['quotes']['ofr' if context['our_side'] == 'BUY' else 'bid']
        # Do not invent missing peer size or split a larger offered lot.
        peer_amount = quote.get('volume_wan')
        ceiling = number(peer_amount) if peer_amount is not None else target
        if quantities and peer_amount is not None and any(q != ceiling for q, _a, _b in quantities):
            raise InvalidFact('Reply must preserve the peer whole lot; ask separately to change size')
    else:
        ceiling = target
    if any(q <= 0 or q > ceiling for q, _a, _b in quantities):
        raise InvalidFact('Public quantity exceeds the currently authorized shared amount')
