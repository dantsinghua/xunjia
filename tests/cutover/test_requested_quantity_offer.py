"""A verified requested whole lot permits our offer, never a peer confirmation."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22.build import build
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from server.negotiation.slots import collect_candidates
from test_reference_window_policy import OWNER, Desk


def test_current_source_verified_question_lot_can_bind_our_offer_without_confirming_peer_volume(tmp_path):
    case = Desk(tmp_path/'requested-quantity.sqlite')
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
            (json.dumps({'opening_yield_pct':'3.20','step_bp':'2','volume_wan':'10000'}),case.group))
    source = case.inbound('SYN-A3.10出500万吗')
    job = case.store.claim_analysis(OWNER,case.thread,force=True)
    snapshot = {**job['snapshot'],'schema_version':'2.2','stage':'analysis'}
    original_snapshot = deepcopy(snapshot)
    candidates = collect_candidates(snapshot)
    price = next(row['id'] for row in candidates if row['roles']==['YIELD'] and row['value']=='3.10')
    quantity = next(row['id'] for row in candidates if row['roles']==['QUANTITY'] and row['value']=='500万')
    model = {'schema_version':'2.2','scopes':{source:[case.member]},'observations':[{
        'member_id':case.member,'side':'ofr','source_ids':[source],
        'slots':{'price':price,'volume_wan':quantity},'meaning':{'intent':'QUESTION',
            'validity':'ACTIVE','negotiation_space':'UNKNOWN','reason':'当前对方直接询问本券价格和整量，并未实报。'}}]}
    nodes = {row['id']:row['data'] for row in build('analysis',version='1.17.1',
        provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
    contract = {};exec(nodes['contract']['code'],contract)
    decoded = json.loads(contract['main'](json.dumps(model),json.dumps(snapshot))['result'])
    scopes, observations, unresolved = case.engine._normalize(snapshot,decoded)
    saved = case.store.commit_analysis(OWNER,job['analysis_id'],job['lease_token'],
        scopes=scopes,observations=observations,unresolved=unresolved)
    assert saved['facts_committed'] and len(saved['accepted'])==1 and not saved['rejected']
    context = case.reply_context();before = deepcopy(context)
    peer = context['quotes']['ofr']
    assert peer['assertion']=='QUESTION' and peer['confirmed_day'] is None
    assert peer['finality']=='UNKNOWN' and peer['volume_wan']=='500'
    assert peer['evidence']['volume_wan']==[source]
    assert context['group_summary']['reported_volume_wan']=='0'
    assert context['group_summary']['final_confirmed_volume_wan']=='0'
    assert snapshot == original_snapshot
    assert case.store.group(OWNER,case.group)['target_volume']=='10000', 'Private target is not the source lot'
    for slot in ['peer_quote','peer_volume']:
        with pytest.raises((ValueError,InvalidFact)):
            render_reply_plan(context,{'kind':'REPLY','quote_value':'3.15','question':'NONE',
                'text':'您问{'+slot+'}，这边{own_quote}。'})
    plan = {'kind':'REPLY','quote_value':'3.15','question':'NONE',
        'text':'按您询问的{requested_volume}，这边{own_quote}，请再看看？'}
    rendered = render_reply_plan(context,plan)
    body = Engine.__new__(Engine)._validate_reply(context,deepcopy(rendered['proposal']))
    assert body['kind']=='REPLY' and body['our_price']['value']=='3.15'
    assert '500万' in body['text'] and '10000万' not in body['text']
    assert rendered['proposal']['render_plan']['text']==plan['text']
    assert context == before and case.quote()==before['quotes']['ofr']



def asked_case(path, text='你能3.10收500万吗', *, our_side='BUY', side='bid', intent='QUESTION',
               private_target='10000', include_price=True, include_quantity=True, prior_firm_side=None, crop_segment=None):
    case = Desk(path)
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_group SET our_side=?,target_volume=?,plan_json=? WHERE group_id=?',
            (our_side,private_target,json.dumps({'opening_yield_pct':'3.20','step_bp':'2',
                'volume_wan':private_target}),case.group))
    if prior_firm_side:
        direction='收' if prior_firm_side=='bid' else '出'
        previous=case.inbound('SYN-A 3.10'+direction+'1000万')
        saved=case.analyze([{'member_id':case.member,'side':prior_firm_side,
            'changes':{'price':{'kind':'YIELD','value':'3.10'},'volume_wan':'1000',
                'assertion':'QUOTE','validity':'ACTIVE','finality':'UNKNOWN','confirmed_day':'2026-10-17'},
            'evidence':{key:[previous] for key in ['price','volume_wan','assertion','validity','finality','confirmed_day']}}])
        assert saved['accepted'] and not saved['rejected']
    source = case.inbound(text)
    job = case.store.claim_analysis(OWNER,case.thread,force=True)
    snapshot = {**job['snapshot'],'schema_version':'2.2','stage':'analysis'}
    candidates = collect_candidates(snapshot); slots = {}
    if include_price:
        slots['price'] = next(row['id'] for row in candidates if row['roles']==['YIELD'] and row['message_id']==source)
    if include_quantity:
        slots['volume_wan'] = next(row['id'] for row in candidates if row['roles']==['QUANTITY'] and row['message_id']==source)
    model = {'schema_version':'2.2','scopes':{source:[case.member]},'observations':[{
        'member_id':case.member,'side':side,'source_ids':[source],'slots':slots,
        'meaning':{'intent':intent,'validity':'ACTIVE','negotiation_space':'UNKNOWN',
            'reason':'仅依当前完整原文的真实源字段；不把询问改为实报。'}}]}
    nodes = {row['id']:row['data'] for row in build('analysis',version='1.17.1',
        provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
    code = {};exec(nodes['contract']['code'],code)
    decoded = json.loads(code['main'](json.dumps(model),json.dumps(snapshot))['result'])
    if crop_segment is not None:
        decoded['observations'][0]['segments']=[{'message_id':source,'text':crop_segment}]
    scopes, observations, unresolved = case.engine._normalize(snapshot,decoded)
    saved = case.store.commit_analysis(OWNER,job['analysis_id'],job['lease_token'],
        scopes=scopes,observations=observations,unresolved=unresolved)
    context=case.reply_context();context.update(schema_version='2.2',stage='reply')
    return case,source,saved,context,observations


def offer(text='按您询问的{requested_volume}，这边{own_quote}，请再看看？',value='3.15'):
    return {'kind':'REPLY','quote_value':value,'question':'NONE','text':text}


def accept_offer(context, plan=None):
    plan = plan or offer()
    before = deepcopy((context,plan))
    rendered = render_reply_plan(context,plan)
    body = Engine.__new__(Engine)._validate_reply(context,deepcopy(rendered['proposal']))
    for version in ['0.15.3','1.17.1']:
        nodes = {row['id']:row['data'] for row in build('reply',version=version,
            provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
        project,decode = {},{}
        exec(nodes['context']['code'],project);exec(nodes['contract']['code'],decode)
        assert project['main'](json.dumps(context))['system_prompt']
        result = json.loads(decode['main'](json.dumps({'schema_version':'2.2','reply_plan':plan}),
            json.dumps(context))['result'])
        assert Engine.__new__(Engine)._validate_reply(context,result['proposal']) == body
    assert (context,plan) == before
    assert rendered['proposal']['render_plan']['text'] == plan['text']
    return rendered,body


def deny_offer(context, plan=None):
    before = deepcopy(context)
    with pytest.raises((ValueError,InvalidFact)):
        rendered=render_reply_plan(context,plan or offer())
        Engine.__new__(Engine)._validate_reply(context,deepcopy(rendered['proposal']))
    assert context == before


def test_our_side_question_and_quantity_only_question_use_their_actual_requested_lot(tmp_path):
    for index,text in enumerate(['你能3.10收500万吗','这个价能做500万吗','你能收500万吗','能3.10收500万吗']):
        case,source,saved,context,_ = asked_case(tmp_path/('question'+str(index)+'.sqlite'),text,
            include_price=index in (0,3))
        assert saved['accepted'] and not saved['rejected']
        assert context['quotes']['bid']['assertion']=='QUESTION'
        assert context['quotes']['bid']['confirmed_day'] is None
        before = deepcopy(context)
        rendered,body = accept_offer(context)
        parts = [part for part in rendered['parts'] if part['role']=='OWN_REQUESTED_QUANTITY_OFFER']
        assert len(parts)==1 and parts[0]['text']=='500万'
        assert source in json.dumps(parts[0]['source']) and case.member in json.dumps(parts[0]['source'])
        assert '500万' in rendered['economic_text'] and '500万' in body['text']
        assert context['group_summary']['reported_volume_wan']=='0'
        assert context['group_summary']['final_confirmed_volume_wan']=='0'
        assert case.quote()==before['quotes']['ofr'] and context==before
    _case,_source,saved,context,_=asked_case(tmp_path/'explicit-self-seller.sqlite',
        '我能出500万吗',side='ofr',include_price=False)
    assert saved['accepted'] and not saved['rejected']
    _rendered,body=accept_offer(context)
    assert '500万' in body['text'] and body['our_price']['value']=='3.15'
    assert context['quotes']['ofr']['assertion']=='QUESTION'
    assert context['quotes']['ofr']['confirmed_day'] is None


def test_sell_addressee_direction_is_authorized_but_the_opposite_addressee_direction_is_not(tmp_path):
    _case,_source,saved,context,_ = asked_case(tmp_path/'sell.sqlite','您能3.10出500万吗',our_side='SELL',side='ofr')
    assert saved['accepted'] and not saved['rejected']
    _rendered,body = accept_offer(context,offer(value='3.10'))
    assert '3.10%出' in body['text'] and '500万' in body['text']
    _case,_source,saved,wrong,_ = asked_case(tmp_path/'wrong-direction.sqlite','你能3.10出500万吗',side='ofr')
    assert saved['accepted'] and not saved['rejected']
    deny_offer(wrong)
    for index,(text,side) in enumerate([('能出500万吗','ofr'),('我能收500万吗','bid')]):
        _case,_source,saved,context,_=asked_case(tmp_path/('speaker-direction'+str(index)+'.sqlite'),
            text,side=side,include_price=False)
        assert saved['accepted'] and not saved['rejected']
        deny_offer(context)


def test_requested_lot_is_not_replaced_by_opposite_firm_lot_or_smaller_private_target(tmp_path):
    case,_source,saved,context,_ = asked_case(tmp_path/'different-lots.sqlite',private_target='100',prior_firm_side='ofr')
    assert saved['accepted'] and not saved['rejected']
    assert context['quotes']['ofr']['assertion']=='QUOTE' and context['quotes']['ofr']['volume_wan']=='1000'
    before=deepcopy(context)
    _rendered,body=accept_offer(context)
    assert '500万' in body['text'] and '1000万' not in body['text']
    assert context==before and case.store.group(OWNER,case.group)['target_volume']=='100'


def test_requested_lot_requires_current_unchanged_inbound_source_and_exact_evidence(tmp_path):
    _case,_source,saved,base,_ = asked_case(tmp_path/'source.sqlite')
    assert saved['accepted'] and not saved['rejected']
    for change in ['old','historical','out','wrong_thread','edited','missing_evidence','wrong_value','missing_unit','wrong_bond']:
        context=deepcopy(base);row=context['context'][-1];peer=context['quotes']['bid']
        if change=='old':row['trade_day']='2026-10-16'
        elif change=='historical':row['historical']=True
        elif change=='out':row['direction']='out'
        elif change=='wrong_thread':row['thread_id']='another-thread'
        elif change=='edited':row['kind']='edit';row['target_id']=row['message_id']
        elif change=='missing_evidence':peer['evidence']['volume_wan']=[]
        elif change=='wrong_value':peer['volume_wan']='501'
        elif change=='missing_unit':row['text']='你能收500吗'
        elif change=='wrong_bond':context['bond_code']='SYN-B';row['text']='SYN-A你能3.10收500万吗'
        deny_offer(context)


def test_complete_third_party_hypothetical_and_cropped_prefix_never_authorize_our_requested_offer(tmp_path):
    for index,text in enumerate(['客户问你能3.10收500万吗','假设你能3.10收500万吗','如果你能3.10收500万吗',
            '他说；你能收500万吗','老王问；你能收500万吗','转达：你能收500万吗']):
        case,_source,saved,context,_=asked_case(tmp_path/('qualifier'+str(index)+'.sqlite'),text,include_price=index<3)
        # A may already reject the contradictory ownership. It must never leave a usable request offer.
        assert saved['facts_committed']
        deny_offer(context)
        assert case.store.group(OWNER,case.group)['summary']['final_confirmed_volume_wan']=='0'


def test_requested_macro_does_not_authorize_peer_confirmation_execution_or_other_economic_changes(tmp_path):
    _case,_source,saved,context,_=asked_case(tmp_path/'wording.sqlite')
    assert saved['accepted'] and not saved['rejected']
    for text in ['你报{requested_volume}，我{own_quote}。','你方报价{requested_volume}，我{own_quote}。',
            '您已确认{requested_volume}，这边{own_quote}。',
            '您已实报{requested_volume}，这边{own_quote}。',
            '您最终可做{requested_volume}，这边{own_quote}。',
            '{requested_volume}，这边{own_quote}，成交。',
            '{requested_volume}，这边{own_quote}，锁量。',
            '按您询问的{requested_volume}，这边{own_quote}出。',
            '按您询问的{requested_volume}，这边{own_quote}，拆250万。']:
        deny_offer(context,offer(text))
    deny_offer(context,offer(value='3.11'))
    for kind in ['CLARIFY','SOCIAL','ASK_VOLUME']:
        deny_offer(context,{'kind':kind,'question':'NONE','text':'按您询问的{requested_volume}再看看？'})
    deny_offer(context,offer('这边{own_quote}，500万你再看看？'))
    _rendered,body=accept_offer(context,offer('我方最终可报{own_quote}{requested_volume}，您再看看？'))
    assert '500万' in body['text'] and body['our_price']['value']=='3.15'


def test_clear_addressee_and_quantity_only_questions_mislabeled_quote_are_rejected_not_rewritten(tmp_path):
    for index,text in enumerate(['你能3.10收500万吗','这个价能做500万吗']):
        case,_source,saved,context,observations=asked_case(tmp_path/('false-quote'+str(index)+'.sqlite'),
            text,intent='QUOTE',include_price=index==0,prior_firm_side='bid')
        assert saved['rejected'] and not saved['accepted']
        assert observations[0].get('validation_error'), 'Contradictory QUOTE must be rejected, never silently rewritten QUESTION'
        assert context['quotes']['bid']['assertion']!='QUESTION'
        assert case.store.group(OWNER,case.group)['summary']['reported_volume_wan']=='0'


def test_firm_whole_lot_and_a_declaration_followed_by_a_settlement_question_keep_the_original_quote_route(tmp_path):
    for index,text in enumerate(['SYN-A 3.10出1000万','SYN-A 3.10出1000万。哪天结算？']):
        case,source,saved,context,_=asked_case(tmp_path/('firm'+str(index)+'.sqlite'),text,side='ofr',intent='QUOTE')
        assert saved['accepted'] and not saved['rejected']
        assert context['quotes']['ofr']['assertion']=='QUOTE' and context['quotes']['ofr']['confirmed_day']==context['trade_day']
        before=deepcopy(context)
        rendered,body=accept_offer(context,offer('您报{peer_quote}{peer_volume}，这边{own_quote}，你再看看？'))
        assert '1000万' in body['text'] and '500万' not in body['text']
        assert all(part['role']!='OWN_REQUESTED_QUANTITY_OFFER' for part in rendered['parts'])
        deny_offer(context)
        assert context==before and case.store.group(OWNER,case.group)['summary']['reported_volume_wan']=='1000'


def test_request_role_does_not_revoke_independently_verified_price_when_quantity_is_unknown(tmp_path):
    _case,_source,saved,context,_=asked_case(tmp_path/'price-only.sqlite','SYN-A 3.10出',side='ofr',intent='QUOTE',include_quantity=False)
    assert saved['accepted'] and not saved['rejected']
    assert context['quotes']['ofr']['volume_wan'] is None
    _rendered,body=accept_offer(context,offer('您报{peer_quote}，这边{own_quote}，请问整量多少？'))
    assert '3.10%出' in body['text'] and body['our_price']['value']=='3.15'
    deny_offer(context)


def test_one_real_quantity_shared_by_both_question_sides_is_not_two_lots_and_ambiguous_requests_are_unavailable(tmp_path):
    _case,_source,saved,context,_=asked_case(tmp_path/'duplicate-source.sqlite','这个价能做500万吗',include_price=False)
    assert saved['accepted'] and not saved['rejected']
    context['quotes']['ofr']=deepcopy(context['quotes']['bid']);context['quotes']['ofr']['side']='ofr'
    rendered,body=accept_offer(context)
    assert len([part for part in rendered['parts'] if part['role']=='OWN_REQUESTED_QUANTITY_OFFER'])==1
    assert body['text'].count('500万')==1
    context['context'][-1]['text']='这个价能做500万还是600万吗'
    deny_offer(context)



def test_cropped_qualifiers_and_a_different_same_thread_member_cannot_borrow_request_authority(tmp_path):
    from server.negotiation.prices import LimitPolicy
    case,_source,saved,context,_=asked_case(tmp_path/'cropped-source.sqlite',
        '客户问你能3.10收500万吗',crop_segment='你能3.10收500万吗')
    assert saved['facts_committed']
    assert context['context'][-1]['text']=='客户问你能3.10收500万吗'
    deny_offer(context)
    case,_source,saved,_context,_=asked_case(tmp_path/'other-member.sqlite')
    assert saved['accepted'] and not saved['rejected']
    other_group=case.store.create_group(OWNER,bond_code='SYN-B',our_side='BUY',target_volume='10000',
        trade_day='2026-10-17',policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=True)
    other,thread=case.store.add_member(OWNER,other_group,account_uid='synthetic-account',peer_uid='synthetic-peer')
    assert thread==case.thread
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
            (json.dumps({'opening_yield_pct':'3.20','step_bp':'2','volume_wan':'10000'}),other_group))
        other_context=case.engine._reply_context(tx,OWNER,other,
            case.service.commands_core._basis(tx,OWNER,other,True))
    assert all(quote['volume_wan'] is None for quote in other_context['quotes'].values())
    deny_offer(other_context)
