"""Chinese adjacency and fullwidth terms remain source-positioned date roles."""
import pytest

from server.negotiation.slots import collect_candidates
from server.negotiation.text_roles import settlement_literal, mask_non_trading


@pytest.mark.parametrize('text,token,expected',[
    ('结算T+1','T+1','T+1'),('就T+0结算','T+0','T+0'),
    ('不是T+1','T+1','T+1'),('结算Ｔ＋１','Ｔ＋１','T+1'),
    ('🙂用ｔ＋０','ｔ＋０','T+0'),('T+1','T+1','T+1')])
def test_term_candidate_survives_chinese_adjacency_and_keeps_exact_raw_offsets(text,token,expected):
    context={'messages':[{'message_id':'incoming','text':text,'direction':'in','ingest_revision':1}]}
    values=collect_candidates(context)
    terms=[value for value in values if value['roles']==['SETTLEMENT']]
    assert len(terms)==1
    span=terms[0]
    assert span['token']==text[span['start']:span['end']]==token
    assert settlement_literal(token)==expected
    assert not any(set(value['roles']) & {'YIELD','YIELD_SUFFIX','QUANTITY'} for value in values), values


@pytest.mark.parametrize('text',['AT+1','T+12','T+1.5','T+1X','T+1万','T＋１％'])
def test_code_decimal_or_trading_unit_is_not_hidden_as_a_settlement_term(text):
    context={'messages':[{'message_id':'incoming','text':text,'direction':'in'}]}
    assert not any('SETTLEMENT' in candidate['roles'] for candidate in collect_candidates(context))
    assert mask_non_trading(text)==text
