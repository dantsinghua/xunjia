"""Literal non-trading number roles; offsets are Unicode code points.

This is lexical evidence, not permission to publish a sourced price or amount.
Invalid dates and trading units attached to a metadata-shaped token stay visible
to the economic validator. No rewriting of the original text occurs.
"""
from datetime import date
import re


def public_language_error(text):
    """Existing public-language policy, independent of economic authority."""
    if re.search(r'内部|底价|底线|限价|授权|步长|(?:我(?:们|方)?|本方|参考|初始|起始|每轮|设置的)步幅|目标(?:价|量)|其他对手|别家给|system.prompt|tool_budget|policy_version|member_id|group_id|reference_step_bp|quote_constraints|opening_yield_pct',text,re.I):
        return ('Public reply exposes private policy or internal context')
    # The tool negotiates and collects quotes, never executes or reserves a lot.
    for clause in re.split(r'[，,。;；\n]', text):
        if re.search(r'\d+(?:\.\d+)?\s*(?:万|亿)\s*确认(?!报价)',clause):
            return ('Quantity confirmation must explicitly refer to a quotation, not execution')
        for commitment in re.finditer(r'成交|锁(?:量|定|券)|占(?:用)?额度|确认买入|确认卖出|就(?:按.*)?要了'
                r'|就(?:这么|这样)定(?:了|下|吧)|按(?:这个(?:报价|价)?|这价)(?:给(?:你|您))?(?:做|执行)(?:了|吧)?'
                r'|给(?:你|您)做了|执行|執行',clause):
            prefix=clause[:commitment.start()]
            # A disclaimer only negates this predicate. An earlier "no trade"
            # must never authorize a later positive lock/execute assertion.
            double_negative=re.search(r'(?:不得不|不能不|不会不|不可能不|不是不)'
                r'(?:(?:自动|承诺|涉及|进行|执行|作|做|再)\s*){0,2}(?:给(?:你|您))?\s*$',prefix)
            negated=re.search(r'(?:不(?:会|能|要)?|禁止|不得|拒绝)'
                r'(?:(?:自动|承诺|涉及|进行|执行|作|做|再)\s*){0,2}(?:给(?:你|您))?\s*$',prefix)
            if double_negative or not negated:
                return ('Reply promises execution or reserves a counterparty lot')
    return None


def numeric_spelling(text):
    """Normalize one-code-point numeric glyphs without moving source offsets."""
    return text.translate(str.maketrans('０１２３４５６７８９．＋－−％', '0123456789.+--%'))


def settlement_literal(token):
    """Read a selected date/settlement token, without inventing a calendar."""
    token = numeric_spelling(token).translate(str.maketrans('Ｔｔ','Tt'))
    if re.fullmatch(r'T\+[01]', token, re.I):
        return token.upper()
    for pattern in (r'(\d{4})-(\d{1,2})-(\d{1,2})', r'(\d{4})/(\d{1,2})/(\d{1,2})',
                    r'(\d{4})年(\d{1,2})月(\d{1,2})日?'):
        match = re.fullmatch(pattern, token)
        if match:
            return date(*(int(part) for part in match.groups())).isoformat()
    raise ValueError('Unsupported settlement literal')


def non_trading_spans(text):
    result = []
    dates = []
    # Separate patterns keep mixed separators and partial invalid dates visible.
    patterns = (r'(?<![\d.])(\d{4})-(\d{1,2})-(\d{1,2})(?!\d|[.．]\d)',
                r'(?<![\d.])(\d{4})/(\d{1,2})/(\d{1,2})(?!\d|[.．]\d)',
                r'(?<![\d.])(\d{4})年(\d{1,2})月(\d{1,2})日?(?!日|\d|[.．]\d)')
    def trading_tail(end):
        return bool(re.match(r'\s*(?:万|亿|[wW](?![a-zA-Z])|[%％])', text[end:], re.I))
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            dates.append((match.start(), match.end()))
            try:
                date(*(int(v) for v in match.groups()))
            except ValueError:
                continue
            if not trading_tail(match.end()):
                result.append((match.start(), match.end(), 'DATE'))
    terms_text=numeric_spelling(text).translate(str.maketrans('Ｔｔ','Tt'))
    for match in re.finditer(r'(?<![a-zA-Z0-9_.])T\+[01](?![a-zA-Z0-9_.])', terms_text, re.I):
        if not trading_tail(match.end()):
            result.append((match.start(), match.end(), 'SETTLEMENT'))
    for match in re.finditer(r'(?<![\d.])\d+(?:\.\d+)?\s*(?:个?月|年|天|日|[dDyYmM](?![a-zA-Z]))', text):
        if (not any(a < match.end() and match.start() < b for a, b in dates)
                and not trading_tail(match.end())):
            result.append((match.start(), match.end(), 'TENOR'))
    for match in re.finditer(r'(?<![a-zA-Z0-9])(?:(?:A|P|F)[-－][123]|(?:Aaa|Aa|Baa|Ba|Caa|Ca|AAA|AA|A|BBB|BB|B|CCC|CC|C|D)[123+－-]?)(?![a-zA-Z0-9])', text):
        if not trading_tail(match.end()):
            result.append((match.start(), match.end(), 'RATING'))
    return sorted(result)


def mask_non_trading(text):
    masked = list(text)
    for start, end, _role in non_trading_spans(text):
        masked[start:end] = ' ' * (end - start)
    return ''.join(masked)

def valuation_offset_spans(text):
    """Literal BP operands, including an omitted BP unit after 中债/估值.
    No inferred price/side/intent: a valuation label plus an arithmetic operator
    is required when the unit is absent. Naked negative yields remain yields.
    Spans address original Unicode code points, so the same lexer is embedded
    in both Dify Code stages and used by server evidence validation.
    """
    normalized = numeric_spelling(text).translate(str.maketrans('ＢＰｂｐ','BPbp'))
    operator = r'(?:上浮|下浮|加|减|[+-])'
    amount = r'\d+(?:\.\d+)?'
    unit = r'(?:[bB][pP](?![a-zA-Z])|基点)'
    labelled = (r'(?:中债\s*(?:估值)?|估值)\s*(?:收益率\s*)?'
                + operator + r'\s*' + amount + r'(?:\s*' + unit + r')?')
    # Explicit %, money and quantity units are not an omitted BP unit. The
    # digit/dot guard also forbids regex backtracking to a shorter operand.
    boundary = r'(?![\d.a-zA-Z])(?!\s*[%％万亿元份wW])'
    labelled += boundary
    explicit = r'(?<![a-zA-Z0-9_.])(?:' + operator + r'\s*)?' + amount + r'\s*' + unit
    result = []
    for match in re.finditer(labelled + '|' + explicit, normalized):
        token = match[0]
        digits = re.search(amount, token)[0]
        negative = re.search(r'下浮|减|-', token) is not None
        result.append((match.start(), match.end(), ('-' if negative else '') + digits))
    return result
