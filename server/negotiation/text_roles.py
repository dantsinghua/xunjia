"""Literal non-trading number roles; offsets are Unicode code points.

This is lexical evidence, not permission to publish a sourced price or amount.
Invalid dates and trading units attached to a metadata-shaped token stay visible
to the economic validator. No rewriting of the original text occurs.
"""
from datetime import date
import re


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
