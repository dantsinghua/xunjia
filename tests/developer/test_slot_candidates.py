"""Lexical candidates precede field typing; they never assert a trade."""
import pytest

from server.negotiation.slots import extract_spans, classify_spans, collect_candidates


def candidates(text):
    raw = extract_spans(text)
    return classify_spans(text, raw)


def test_separate_literal_extraction_and_batched_field_typing():
    text = '20 出 2000'
    raw = extract_spans(text)
    assert [text[s['start']:s['end']] for s in raw] == ['20', '出', '2000']
    assert all('roles' not in s for s in raw)
    typed = classify_spans(text, raw)
    assert typed[0]['roles'] == ['YIELD_SUFFIX', 'QUANTITY']
    assert typed[1]['roles'] == ['SIDE_OFR']
    assert typed[2]['roles'] == ['QUANTITY']
    assert all('assertion' not in s and 'member_id' not in s for s in typed)


@pytest.mark.parametrize('text', ['明天出差', '请指出问题', '支出2000元', '报告已出版', '价格出现波动'])
def test_direction_substrings_are_candidates_but_not_offer_fields(text):
    typed = candidates(text)
    assert not any('SIDE_OFR' in s['roles'] for s in typed)


@pytest.mark.parametrize('text,role,value', [
    ('20%出2000', 'YIELD', '20'), ('净价34.19出500万', 'NET_PRICE', '34.19'),
    ('全价99.32出500万', 'DIRTY_PRICE', '99.32'), ('0.5亿', 'QUANTITY', '0.5亿'),
    ('估值减5BP出1000万', 'CHINABOND_BP', '-5'), ('-0.05出500万', 'YIELD', '-0.05'),
])
def test_explicit_field_notation_is_not_reinterpreted(text, role, value):
    assert any(s['roles'] == [role] and s['value'] == value for s in candidates(text))


def test_unicode_positions_duplicate_tokens_and_revision_are_unambiguous():
    text = '🙂２０出２０００万；20不出了，改量20万'
    ctx = {'messages': [{'message_id': 'm', 'ingest_revision': 7, 'text': text}], 'members': []}
    rows = collect_candidates(ctx)
    assert len({s['id'] for s in rows}) == len(rows)
    for row in rows:
        assert row['token'] == text[row['start']:row['end']]
        assert row['revision'] == 7 and row['message_id'] == 'm'
    assert any(s['value'] == '2000万' for s in rows)
    assert any('NEGATION' in s['roles'] for s in rows)
    assert any('CORRECTION' in s['roles'] for s in rows)


def test_metadata_numbers_are_not_price_or_quantity_candidates():
    rows = candidates('评级2026年10月17日，剩余335天，T+1；SYN-A 20出2000万')
    economic = [s['token'] for s in rows if set(s['roles']) & {'YIELD','YIELD_SUFFIX','QUANTITY'}]
    assert economic == ['20', '2000万']


def test_absence_of_numeric_spans_still_preserves_semantic_changes():
    rows = candidates('不出了，刚才那只按原价，量改一下；还能谈吗？')
    roles = {r for row in rows for r in row['roles']}
    assert {'NEGATION','REFERENCE','CORRECTION','QUESTION'} <= roles
