"""Equivalent public literals must compile to the existing authorized macros.

Only synthetic frozen contexts and real renderer/Code/Engine are used. There
are no model, IM, gateway, credential or database calls in these tests.
"""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22.build import build
from dify.negotiation_v22.node_code import compact_context
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan, public_information
from test_rendered_reply_plan_revision import DAY, context as sourced_context, with_public


def canonical_analysis_view(context, projected):
    """Restore metadata through the real Contract, without using its map helper.

    Only bounded synthetic history request envelopes are decoded; no request is
    executed. Free text, public tools, economic facts and slot IDs stay intact.
    """
    from dify.negotiation_v22.node_code import decode_model_output
    view = deepcopy(projected)
    message_refs, member_refs = [], []
    def add(values, item):
        if isinstance(item,str) and item and item not in values: values.append(item)
    def walk(value, mapping=None):
        def one(item, domain):
            if mapping is None:
                add(message_refs if domain=='m' else member_refs,item)
                return item
            return mapping[domain][item]
        def field(row,key,domain):
            if row.get(key) is not None: row[key]=one(row[key],domain)
        def items(row,key,domain):
            if key in row: row[key]=[one(v,domain) for v in row[key]]
        def message(row):
            field(row,'message_id','m'); field(row,'target_id','m')
            if mapping is not None and 'original_reference_id' in row:
                assert row.pop('original_reference_id') == row['message_id']
        for key in ('messages','correction_context','context'):
            for row in value.get(key,[]): message(row)
        for row in value.get('members',[]):
            field(row,'member_id','p')
            for msg in row.get('context',[]): message(msg)
        for row in value.get('history_results',[]):
            field(row,'member_id','p');items(row,'message_ids','m')
            for msg in row.get('messages',[]):message(msg)
        for row in value.get('evidence_index',[]):
            field(row,'member_id','p');items(row,'loaded_message_ids','m')
            items(row,'unloaded_reference_ids','m');field(row,'our_quote_message_id','m')
        for row in value.get('slot_candidates',[]):
            field(row,'message_id','m');items(row,'explicit_bond_members','p')
        for row in value.get('current_member_candidates',[]):field(row,'member_id','p')
        for key in ('reference_gaps','known_outbound_scopes'):
            if key not in value:continue
            result={}
            for identity,row in value[key].items():
                if key=='reference_gaps':
                    items(row,'source_ids','m');items(row,'missing_ids','m')
                    result[one(identity,'p')]=row
                else:result[one(identity,'m')]=[one(v,'p') for v in row]
            value[key]=result
    walk(view)
    maps={'m':{},'p':{}}
    current=projected['messages'][0]['message_id']
    for member in member_refs:
        for start in range(0,len(message_refs),20):
            refs=list(dict.fromkeys([current]+message_refs[start:start+20]))
            wire={'schema_version':'2.2','history_requests':[{'member_id':member,'message_ids':refs}]}
            request=decode_model_output(wire,context,'analysis')['history_requests'][0]
            maps['p'][member]=request['member_id']
            for alias,canonical in zip(refs,request['message_ids']):
                if alias in maps['m']:assert maps['m'][alias]==canonical
                maps['m'][alias]=canonical
    walk(view,maps)
    return view


def quantity_context(side='BUY'):
    context = sourced_context()
    context['context_policy_revision'] = 'latest_quote_confirmation_v1'
    context['our_side'] = side
    if side == 'SELL':
        context['boundary']['value'] = '3.30'
        context['quotes'] = {'bid': {**context['quotes']['ofr'], 'side': 'bid',
            'price': {'kind': 'YIELD', 'value': '3.20'}}}
        context['context'][0]['text'] = 'SYN-A 3.20收500万'
    context['quote_constraints'] = Engine._quote_constraints(context)
    return context


def reply(text):
    return {'kind': 'REPLY', 'quote_value': '3.15', 'text': text, 'question': 'NONE'}


def validated(context, plan):
    rendered = render_reply_plan(context, plan)
    body = Engine.__new__(Engine)._validate_reply(context, deepcopy(rendered['proposal']))
    return rendered, body


def assert_same_macro(context, literal, macro):
    before = deepcopy((context, literal, macro))
    expected, expected_body = validated(context, macro)
    actual, actual_body = validated(context, literal)
    assert (context, literal, macro) == before
    assert actual_body == expected_body, 'The final native message must already be authorized by the original macro plan P*'
    assert actual['proposal']['text'] == expected['proposal']['text']
    assert actual['parts'] == expected['parts'], 'Do not replace an economic source role with a public-info exemption'
    assert actual['economic_text'] == expected['economic_text']
    assert actual['proposal']['render_plan']['text'] == literal['text'], 'Keep the original model sentence in its audit plan'
    actual_plan = {**actual['proposal']['render_plan'], 'text': macro['text']}
    assert actual_plan == expected['proposal']['render_plan']
    return actual, actual_body


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
@pytest.mark.parametrize('literal', ['500万', '500.0万', '0.05亿', '0.0500亿', '0.05000000亿', '500 万'])
def test_exact_decimal_whole_lot_is_the_same_source_bound_economic_fragment(side, literal):
    context = quantity_context(side)
    actual, body = assert_same_macro(context,
        reply('{own_quote}，'+literal+'整笔，你再看看？'),
        reply('{own_quote}，{peer_volume}整笔，你再看看？'))
    quantity = next(part for part in actual['parts'] if part['text'] == '500万')
    assert quantity['role'] == 'OWN_QUOTE'
    assert quantity['source']['member_id'] == context['member_id']
    assert quantity['source']['side'] == ('ofr' if side == 'BUY' else 'bid')
    assert '500万' in actual['economic_text'] and body['our_price']['value'] == '3.15'


@pytest.mark.parametrize('literal', ['499万', '501万', '0万', '-500万', '+500万', '5e2万', '5E2万',
    'NaN万', 'Infinity万', '500万元', '500万亿', '500万千', '500万百', '500万十',
    '0.05亿千', '0.05亿百', '0.05亿十', '500', '0.05亿元', '500w',
    '1,500万', '500万/600万', 'id_500万', 'trace500万'])
def test_wrong_or_partial_numeric_token_is_never_ignored_clipped_or_borrowed(literal):
    with pytest.raises((ValueError, InvalidFact)):
        validated(quantity_context(), reply('{own_quote}，'+literal+'整笔。'))


@pytest.mark.parametrize('change', ['no_evidence', 'QUESTION', 'REPORTED', 'WITHDRAWN', 'UNCONFIRMED', 'old_day', 'missing_lot'])
def test_whole_lot_literal_requires_the_same_current_quote_evidence_as_peer_volume(change):
    context = quantity_context()
    peer = context['quotes']['ofr']
    if change == 'no_evidence': peer['evidence']['volume_wan'] = []
    elif change in ('QUESTION', 'REPORTED'): peer['assertion'] = change
    elif change in ('WITHDRAWN', 'UNCONFIRMED'): peer['validity'] = change
    elif change == 'old_day': peer['confirmed_day'] = '2026-10-16'
    else: peer['volume_wan'] = None
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，{peer_volume}整笔。'))
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，500万整笔。'))


@pytest.mark.parametrize('amount,literal', [('0', '0万'), ('-500', '-500万'), ('NaN', 'NaN万'), ('Infinity', 'Infinity万')])
def test_nonpositive_or_nonfinite_source_amount_cannot_authorize_a_literal(amount, literal):
    context = quantity_context()
    context['quotes']['ofr']['volume_wan'] = amount
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，'+literal+'。'))


def test_private_target_or_other_side_lot_cannot_authorize_the_missing_peer_lot():
    context = quantity_context()
    context['plan']['volume_wan'] = '500'
    context['quotes']['bid'] = deepcopy(context['quotes']['ofr'])
    context['quotes']['bid']['side'] = 'bid'
    context['quotes']['ofr']['volume_wan'] = None
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，500万。'))
    context['quotes']['ofr']['volume_wan'] = '600'
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，500万。'))


def test_equal_public_lot_does_not_declassify_a_private_target_statement():
    context = quantity_context()
    context['plan']['volume_wan'] = '500'
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，我方目标量500万。'))


@pytest.mark.parametrize('text', ['{own_quote}，我出500万。', '{own_quote}，500万成交。',
    '{own_quote}，确认买入500万。', '{own_quote}，参考SYN-TEXT-ID-900123，500万。',
    '{own_quote}，另报3.16%收500万。', '{own_quote}，你刚才3.11%出500万。'])
def test_literal_binding_does_not_bypass_direction_execution_ids_or_yield_rules(text):
    with pytest.raises((ValueError, InvalidFact)):
        validated(quantity_context(), reply(text))


def test_literal_does_not_bypass_the_original_virtual_macro_plan_length_limit():
    context = quantity_context()
    context['quotes']['ofr']['volume_wan'] = '10000'
    literal = reply('{own_quote}，'+'1亿，'*140)
    macro = reply('{own_quote}，'+'{peer_volume}，'*140)
    assert len(literal['text']) < 1500 < len(macro['text'])
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, macro)
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, literal)


def cash_context(kind='NET_PRICE', side='BUY'):
    context = quantity_context(side)
    peer_side = 'ofr' if side == 'BUY' else 'bid'
    peer = context['quotes'][peer_side]
    peer['price'] = {'kind': kind, 'value': '97.00', 'settlement_date': None,
                     'computed_yield': None, 'calculation_ref': None}
    label = '净价' if kind == 'NET_PRICE' else '全价'
    context['context'][0]['text'] = 'SYN-A '+label+'97.00'+('出' if side == 'BUY' else '收')+'500万'
    return context


def settlement(text):
    return {'kind': 'CLARIFY', 'question': 'ASK_SETTLEMENT', 'text': text}


@pytest.mark.parametrize('kind,label', [('NET_PRICE', '净价'), ('DIRTY_PRICE', '全价')])
@pytest.mark.parametrize('side,direction', [('BUY', '出'), ('SELL', '收')])
def test_exact_current_counterparty_cash_spelling_matches_the_original_reference_macro(kind, label, side, direction):
    context = cash_context(kind, side)
    _, body = assert_same_macro(context,
        settlement('你刚才'+label+'97.00'+direction+'，这500万结算是{settlement_options}？'),
        settlement('你刚才{peer_quote}，这{peer_volume}结算是{settlement_options}？'))
    assert body['kind'] == 'CLARIFY' and body.get('our_price') is None
    assert body['settlement_question']['side'] == ('ofr' if side == 'BUY' else 'bid')


def test_current_real_bond_code_adjacent_to_exact_cash_price_and_lot_keeps_the_identical_original_macro_body():
    context=cash_context('DIRTY_PRICE')
    context['quotes']['ofr']['price']['value']='99.32'
    context['context'][0]['text']='您好，请问SYN-A全价99.32出500万，您是基于哪种结算方式报价的？'
    _,body=assert_same_macro(context,
        settlement('您好，请问SYN-A全价99.32出500万，您是基于哪种结算方式报价的？'),
        settlement('您好，请问SYN-A{peer_quote}{peer_volume}，您是基于哪种结算方式报价的？'))
    assert body['kind']=='CLARIFY' and body.get('our_price') is None


@pytest.mark.parametrize('literal', ['97.00', '97.00出', '净价97.00', '全价97.00出', '净价97.00收',
    '净价97.01出', '净价97.0出', '净价+97.00出', '净价9.7e1出', '净价NaN出',
    '我报价97.00出', '我报净价97.00出', '收益率3.10%出', 'trace_净价97.00出'])
def test_cash_binding_never_guesses_basis_value_direction_or_speaker(literal):
    with pytest.raises((ValueError, InvalidFact)):
        validated(cash_context(), settlement('你刚才'+literal+'，结算是{settlement_options}？'))


@pytest.mark.parametrize('change', ['missing_price_evidence', 'QUESTION', 'WITHDRAWN', 'old_day'])
def test_cash_literal_cannot_promote_unconfirmed_or_unsupported_reference_to_a_current_quote(change):
    context = cash_context()
    if change == 'missing_price_evidence': context['quotes']['ofr']['evidence']['price'] = []
    elif change == 'QUESTION': context['quotes']['ofr']['assertion'] = 'QUESTION'
    elif change == 'WITHDRAWN': context['quotes']['ofr']['validity'] = 'WITHDRAWN'
    else: context['quotes']['ofr']['confirmed_day'] = '2026-10-16'
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, settlement('你刚才净价97.00出，结算是{settlement_options}？'))


def test_old_day_cash_literal_is_not_bound_even_when_the_old_reference_macro_can_be_asked_about():
    context = cash_context()
    context['quotes']['ofr'].update(validity='UNCONFIRMED', confirmed_day='2026-10-16')
    macro = {'kind': 'CLARIFY', 'question': 'ASK_CURRENT_QUOTE',
             'text': '上次你报{peer_quote}，今天仍是这个报价吗？'}
    validated(context, macro)
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, {**macro, 'text': '上次你报净价97.00出，今天仍是这个报价吗？'})


def test_a_bound_literal_plan_cannot_move_to_another_member_or_fact_basis():
    context = quantity_context()
    rendered, _ = validated(context, reply('{own_quote}，500万整笔。'))
    newer = deepcopy(context)
    newer['member_id'] = 'another-member'
    newer['basis']['member_id'] = 'another-member'
    with pytest.raises((ValueError, InvalidFact)):
        render_reply_plan(newer, rendered['proposal']['render_plan'])


def projection_context():
    context = quantity_context()
    context.update(schema_version='2.2', stage='reply')
    context['our_quote'].update(command_id='SYN-MECHANICAL-COMMAND-ID', message_id='SYN-MECHANICAL-MESSAGE-ID',
        native_id='SYN-MECHANICAL-NATIVE-ID', verified_sent=True, sent_at=1000)
    context['reconfirm_command_id'] = 'SYN-MECHANICAL-RECONFIRM-ID'
    context['context'] = [{'message_id':'SYN-MECHANICAL-'+str(i), 'native_id':'SYN-NATIVE-'+str(i),
        'text':'SYN-A 原文含ID SYN-TEXT-ID-'+str(900100+i), 'direction':'in', 'historical':False,
        'trade_day':DAY, 'ingest_revision':i+1, 'received_at':1000+i} for i in range(20)]
    return context


def test_latest_reply_projection_hides_only_mechanical_metadata_and_preserves_twenty_raw_texts():
    context = projection_context()
    before = deepcopy(context)
    projected = compact_context(context)
    assert context == before
    assert 'evidence_index' not in projected
    assert all(key not in projected for key in ['member_id', 'thread_id', 'reconfirm_command_id'])
    assert projected['quotes'] == context['quotes']
    assert projected['current_counterparty_quote'] == context['quotes']['ofr']
    assert [row['text'] for row in projected['context']] == [row['text'] for row in context['context']]
    assert len(projected['context']) == 20
    assert all('message_id' not in row and 'native_id' not in row for row in projected['context'])
    assert projected['current_inbound_message']['text'] == context['context'][-1]['text']
    assert all(key not in projected['current_inbound_message'] for key in
               ['message_id', 'native_id', 'event_id', 'thread_id', 'target_id', 'source_seq', 'content_hash'])
    assert all(key not in projected['our_quote'] for key in ['command_id', 'message_id', 'native_id'])
    assert projected['our_quote']['price'] == context['our_quote']['price']
    assert projected['our_quote']['verified_sent'] and projected['our_quote']['sent_at'] == 1000


def test_analysis_and_explicit_history_keep_their_original_source_ids():
    context = projection_context()
    explicit = {**deepcopy(context), 'context_policy_revision':'explicit_anchors_v0'}
    projected = compact_context(explicit)
    assert projected['context'][0]['message_id'] == explicit['context'][0]['message_id']
    assert projected['our_quote']['native_id'] == explicit['our_quote']['native_id']
    assert projected.get('evidence_index')
    analysis = {'schema_version':'2.2', 'stage':'analysis', 'context_policy_revision':'latest_quote_confirmation_v1',
        'messages':[deepcopy(context['context'][-1])], 'members':[deepcopy(context)]}
    projected_a = canonical_analysis_view(analysis, compact_context(analysis))
    assert projected_a['messages'][0]['message_id'] == analysis['messages'][0]['message_id']
    assert projected_a['members'][0]['member_id'] == context['member_id']
    assert len(projected_a['members'][0]['context']) == 20 and projected_a.get('evidence_index')


def test_public_information_slot_ids_remain_available_to_the_latest_reply_model():
    context, _ = with_public('issuer_info')
    context.update(schema_version='2.2', stage='reply', context_policy_revision='latest_quote_confirmation_v1')
    projected = compact_context(context)
    assert projected['public_information_candidates'] == public_information(context)
    assert all(row['id'] for row in projected['public_information_candidates'])


def test_technical_id_in_an_original_message_is_preserved_as_data_not_granted_public_numeric_permission():
    context = projection_context()
    technical_id = 'SYN-TEXT-ID-900119'
    assert technical_id in context['context'][-1]['text']
    projected = compact_context(context)
    assert technical_id in projected['context'][-1]['text']
    with pytest.raises((ValueError, InvalidFact)):
        validated(context, reply('{own_quote}，参考'+technical_id+'，500万整笔。'))


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_contract_still_receives_the_original_context_and_binds_the_same_message(version):
    context = projection_context()
    before = deepcopy(context)
    graph = build('reply', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    nodes = {node['id']:node['data'] for node in graph['nodes']}
    variable = next(row for row in nodes['contract']['variables'] if row['variable'] == 'context_json')
    assert variable['value_selector'] == ['start', 'context_json']
    code = {}; exec(nodes['contract']['code'], code)
    literal = reply('{own_quote}，500万整笔，你再看看？')
    model = {'schema_version':'2.2', 'reply_plan':literal}
    result = json.loads(code['main'](json.dumps(model, ensure_ascii=False), json.dumps(context, ensure_ascii=False))['result'])
    original_macro, expected_body = validated(context, reply('{own_quote}，{peer_volume}整笔，你再看看？'))
    assert result['proposal']['text'] == original_macro['proposal']['text']
    assert Engine.__new__(Engine)._validate_reply(context, result['proposal']) == expected_body
    assert result['proposal']['render_plan']['text'] == literal['text']
    assert context == before
