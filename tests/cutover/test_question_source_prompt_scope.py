"""QUESTION source rules plus the Oct-27 user-approved acceptance/handoff contract."""
from copy import deepcopy
import json

import pytest

from test_direct_calculation_route_revision import context as full_context
from test_prompt_modes import generated as generated_mode
import hashlib

# External oracle: the approved one-call research instruction, not the implementation constant.
CLAUSE = '\nQUESTION选完候选后再自检一次：side、price所选原文及说话主体必须属于同一个可成立的解释，不能把“能收/能出吗”的问句动词与任意另一条历史价格拼成报价。合法采用对方已报价仍按当前原文的实际收出意图判断，不要求历史候选的字面收出与当前side机械相同。该成员未命中程序reference_gaps等既有强制待确认门时，若只是询问数量且没有明确、唯一的价指代，只保留已明确的QUESTION及数量候选，省略price字段；不要为补齐price猜另一方的旧价，也不要把问题改成QUOTE、FINAL或当天确认。'


# Small external byte oracles produced from frozen candidate34, not old implementation code.
ROLE_ORACLES = {'ordinary_A': {'system': 'fdacd7ba2af56ffa6a7cf6c39df1236ae93d78715f4ddafec00adfa5a00d1064', 'context': '4bc67952992020ee557347154bae1503a17e51f0221d6619132402e608eb32a5', 'user': 'ae9e0298f9bb0aedd05c17c7a6be028c68a40575f167244bc98f4bbea46e3974'}, 'ACK': {'system': '8fb9cdc994c22f86969247fa2ce035028281e8539391ed4a1c2d8b2f557f859b', 'context': '3c97960d911b4d1df4a07489c05b060cfda32b925b05a68851261fba711ec0eb', 'user': '6d3ba842ea1fdffbaa8abd479e8eef735ce527915edd1dcb97fe835abe81531f'}, 'directcalc': {'system': '4d690e44eb4b1ebbc79339eb83e80759e96133f3834695ffd7ed23d0a4ed300e', 'context': '3a9d3dbd9bebae4283488b3be8fc42b7ab3673660cc1c05b114892b9e242c132', 'user': 'e49022c79a8d6c51cbb40f263ab06884082b3a45ab4b920b89b0ec58f4a66934'}, 'A_repair': {'system': '84101f3bf2174640c74ee6e01107f3b9841ffa0fa68b66c62fc4ee73f39de087', 'context': 'a12b225d16a8759573f543fa7384d89c52d9409491c44b5a80f26b4ce5550148', 'user': '8e9f047414b408cd8dbd202e4bde0ea682dd2a036f1d754a0df6d3e7aa2bdaa4'}, 'ordinary_B': {'system': '5e399d03c74ab820efb104b5d8dbfcc681937ec84ae708445aa9720c278ad100', 'context': '47fef44f93a32320e0110b8c3d5e99815dbaaa752bfafd5669f81d763df10a89', 'user': '0f35463839e4338407bcb7d5ec7b0faf27ce79d7df9755b125e5d4ebbfc382f8'}, 'B_OUTPUT': {'system': '93f5bd55abf1ed410bc5b85be78a46826e97412b0dee77b91fe0a4ee4abd5be4', 'context': 'bc436e57250bee81ab4e85f69d9167d7d819ffb1aa80bc88788b771bfa739353', 'user': '172e77c6fc01fca555dde6f8a462b97197335eb4a6a797b62a41ba3258bebc55'}, 'B_TEXT': {'system': 'f1cac1b2fd3484ec1785c0e0bc6baf38e9926f179fb5b87dad7571cd856a5dd6', 'context': '5915c9fc15dad51ae1affaf5c686b5b15c50c3be42702ff65dff556fcb4ee6b7', 'user': 'ecc888cff2a4b1ceacae52faf268fe196d51a6c9ee6d1242ff1c6a4dbd1cadf4'}}


def dispatch(stage,version,context):
    _doc,nodes,project,_decode=generated_mode(stage,version)
    output=project(json.dumps(context))
    user=next(p['text'] for p in nodes['model']['prompt_template'] if p['role']=='user').replace('{{#context.context#}}',output['context'])
    return output,user

# Frozen after reviewing the Oct-27 acceptance-vs-finality, retained-quantity and
# short-language contract. Unaffected roles still use the original frozen34 oracle.
# Semantic positive/negative coverage lives in test_handoff_notice_contract.py.
APPROVED_20261027_ORACLES = {'ordinary_A': {'system': 'd3abc113a7e5b4e7b9a93ee4935c5af989df8c470d1028f5803c8ed0d090df96', 'context': '4bc67952992020ee557347154bae1503a17e51f0221d6619132402e608eb32a5', 'user': 'ae9e0298f9bb0aedd05c17c7a6be028c68a40575f167244bc98f4bbea46e3974'}, 'ordinary_B': {'system': '6287f716aa6dbf3cde150a213cfd520a5cdf1c6d263a7ec494fafeb76163d47f', 'context': '47fef44f93a32320e0110b8c3d5e99815dbaaa752bfafd5669f81d763df10a89', 'user': '0f35463839e4338407bcb7d5ec7b0faf27ce79d7df9755b125e5d4ebbfc382f8'}, 'B_OUTPUT': {'system': '28f9ce0a172cb765ff1ec263e19cc3edface3be4b28c81832d7e4cbefebd91a9', 'context': 'a60410e5a67229125a1b4155de465a9dcb5f3fc78ccb1993a8f89c1ebf1503f1', 'user': 'a7030dbc30737408c07d72b59da97872f31464b35df5c716f93a24c96ca7c0aa'}}

def sha(value):return hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_real_generated_ordinary_analysis_adds_only_the_approved_source_selfcheck(version):
    context=full_context('SYN-A 按这个价700万能收吗？');before=deepcopy(context)
    result,user=dispatch('analysis',version,context)
    assert CLAUSE in result['system_prompt']
    assert result['system_prompt'].count(CLAUSE)==1
    assert context==before
    assert '700' not in CLAUSE and '3.20' not in CLAUSE
    expected=APPROVED_20261027_ORACLES['ordinary_A']
    assert '未再提数量就省略volume_wan并保留既有量' in result['system_prompt']
    assert '不为转人工伪造FINAL' in result['system_prompt']
    assert sha(result['system_prompt'].replace(CLAUSE,'',1))==expected['system']
    assert sha(result['context'])==expected['context'] and sha(user)==expected['user']



def role_context(label):
    from test_no_change_ack_revision import context as ack_context
    from test_prompt_modes import ordinary,repairing
    from test_reply_output_repair_projection import context_with_feedback
    if label=='ACK':return 'analysis',ack_context()
    if label=='directcalc':return 'analysis',full_context()
    if label=='A_repair':return 'analysis',repairing('analysis')
    if label=='ordinary_B':return 'reply',ordinary('reply')
    if label=='B_OUTPUT':return 'reply',context_with_feedback()
    if label=='B_TEXT':return 'reply',repairing('reply')
    raise AssertionError(label)


@pytest.mark.parametrize('label',['ACK','directcalc','A_repair','ordinary_B','B_OUTPUT','B_TEXT'])
def test_dispatches_match_reviewed_contract_and_unaffected_roles_remain_frozen34(label):
    stage,context=role_context(label);before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        result,user=dispatch(stage,version,context)
        assert CLAUSE not in result['system_prompt']
        checked_system=result['system_prompt']
        if label in ('ordinary_B','B_OUTPUT'):
            # Exact externally approved B-only delta, not an implementation oracle.
            notes='\n回复计划结构核对：采用reply_plan时，最外层只按原协议放schema_version、reply_plan、可选trader_view和可选action=SEND；kind、question、quote_value、text都只在reply_plan内部，绝不在顶层重复。context_ref是可选绑定字段；只有原纠错草稿已含有效值时才原样保留，否则省略整个字段，不写null、不生成新哈希，程序会完成绑定。trader_view若提供只能是顶层对象{}。text仍需自然生成非空完整正文；这不是字段补丁。requests-only及WAIT/HANDOFF继续各自原允许协议，不混装reply_plan。\n{settlement_options}仅在kind=CLARIFY且question=ASK_SETTLEMENT的真实结算提问中可用，REPLY及其他question不要使用该槽。只有原SENT与verified_sent来源校验成立时，context.our_quote才代表已发旧价；未验证时不当作已报事实；本轮{own_quote}绑定本次reply_plan.quote_value候选，不是对手价或历史价。明确说过去我方已报时只用获准的{our_last_quote}。报价槽已含完整类型和收出，不在槽后另补收/出/买/卖。公开计算说明只引用实际工具事实，不把本次候选价冒充工具使用的输入；纯资料回答不强迫报价，按本轮允许kind自然回应。\n公开工具参数必须是工具文档规定的arguments对象；similar_bonds不含operation、mode或内部arguments字段，不用operation=list或arguments=null包装。tool_results中的invalid_input不是已完成查询：按返回问题修正为合法参数对象，并在原预算内真正再发工具请求；不能只口头承诺补查便结束，也不能捏造usable结果。unavailable/缺少用户条件仍按原规则如实澄清，不新增重试次数。\n公开正文可自然社交、表达报价意向、回答已核实资料或必要澄清，但不得正向引出后续成交、执行、锁量或占额度的话题；加上以后或再沟通也不例外。保持原对明确否定的语义规则，不叙述候选、修复步骤或系统流程，由你沿对话自然写短句，不给固定完整外发句。'
            assert checked_system.count(notes)==1
            checked_system=checked_system.replace(notes,'',1)
            if label=='ordinary_B':
                checked_system=checked_system.replace('券号用{bond}；仅CLARIFY/ASK_SETTLEMENT提问时才用{settlement_options}，其他kind/question不用；资料用{info:实际ID}。',
                    '券号用{bond}，询结算用{settlement_options}，资料用{info:实际ID}。',1)
            else:
                previous=context['validation_feedback']['previous_output']['reply_plan']
                pin=('若继续REPLY报价，reply_plan.kind必须保持原REPLY、reply_plan.question保持原有效值'+previous.get('question','NONE')+'；这些字段都在reply_plan内，不是顶层字段。'
                     '原非报价不能借纠错升级为REPLY报价；若原context许可，可改为或保持合法非报价澄清/社交，不写新的本方quote_value或own_quote，仍经原kind/question/来源权限校验。'
                     '本次原计划没有context_ref，必须省略reply_plan.context_ref整个字段，绝不能补null或自行填哈希。'
                     '仅在继续原REPLY时，本次仅修文案的既定候选reply_plan.quote_value='+json.dumps(previous['quote_value'],ensure_ascii=False)+'，必须完整写回同一字符串，不用context.our_quote的已发旧价替换，也不重新选择让价。{own_quote}将绑定该本次候选价。')
                assert checked_system.endswith(pin)
                checked_system=checked_system[:-len(pin)]
                checked_system=checked_system.replace('只修输入语法，优先保持原合法意图；若继续REPLY报价，保留原kind和question，保留原quote_value（原合法候选中），不重新选择经济条件。',
                    '只修输入语法，保留原kind和question，保留原quote_value（原合法候选中），不重新选择经济条件。',1)
        if label in ('ordinary_B','B_OUTPUT'):
            for clause in ('HANDOFF_NOTICE','ACCEPTED_CURRENT_QUOTE','LATEST_INBOUND','价格相等本身不是接受','确认消息真正SENT','语言采用交易聊天的短句'):
                assert clause in result['system_prompt']
        expected=APPROVED_20261027_ORACLES.get(label,ROLE_ORACLES[label])
        assert {key:sha(value) for key,value in [('system',checked_system),
            ('context',result['context']),('user',user)]}==expected
        assert context==before


def test_original_direction_inconsistent_question_is_still_rejected_without_rewriting_it():
    from test_explicit_adoption_revision import adoption
    from server.negotiation.validation import validate_observation
    from server.negotiation.prices import InvalidFact
    snapshot,observation=adoption('SYN-A 按这个价700万能收吗？',selected='3.10',side='ofr')
    observation['side']='bid';observation['changes']['assertion']='QUESTION'
    observation['interpretation']['intent']='QUESTION'
    before=deepcopy((snapshot,observation))
    with pytest.raises(InvalidFact,match='Quote direction contradicts the evidence'):
        validate_observation(snapshot,observation,{'syn-a':{'a'}})
    assert (snapshot,observation)==before


@pytest.mark.parametrize('side,text',[('ofr','SYN-A 按你刚才3.20收的价，我出500万'),
    ('bid','SYN-A 按你之前3.20出的价，我收500万')])
def test_explicit_legitimate_adoption_keeps_its_source_even_when_history_literal_direction_differs(side,text):
    from test_explicit_adoption_revision import adoption
    from server.negotiation.validation import validate_observation
    snapshot,observation=adoption(text,selected='3.20',side=side)
    before=deepcopy((snapshot,observation))
    result=validate_observation(snapshot,observation,{'syn-a':{'a'}})
    assert result['side']==side and result['changes']['price']['value']=='3.20'
    assert result['evidence']['price']==['current','our-native']
    assert (snapshot,observation)==before



def test_existing_reference_gap_still_blocks_a_question_quantity_observation():
    from types import SimpleNamespace
    from test_explicit_adoption_revision import gap_context
    from server.negotiation.engine import Engine
    snapshot=gap_context();before=deepcopy(snapshot)
    message=snapshot['messages'][0]
    changes={'volume_wan':'500','assertion':'QUESTION','validity':'ACTIVE','finality':'UNKNOWN','confirmed_day':None}
    observation={'member_id':'a','side':'ofr','changes':changes,
        'evidence':{key:[message['message_id']] for key in changes},
        'segments':[{'message_id':message['message_id'],'text':message['text']}],
        'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUESTION','negotiation_space':'UNKNOWN','reason':'已知量但报价引用缺口'}}
    result={'schema_version':'2.2','scopes':{'in-a':['a'],'in-b':['b']},'unresolved':{},'observations':[observation]}
    engine=Engine.__new__(Engine);engine.service=SimpleNamespace(reference_provider=None)
    engine.store=None;engine.context_policy_revision='latest_quote_confirmation_v1'
    _scopes,checked,_unresolved=engine._normalize(snapshot,deepcopy(result))
    blocked=[row for row in checked if row.get('member_id')=='a']
    assert blocked and all(row.get('validation_error')=='REFERENCE_CONFIRMATION_REQUIRED' for row in blocked)
    assert not any(row.get('member_id')=='a' and 'changes' in row for row in checked)
    assert snapshot==before
