"""Developer checks for source-bound natural negotiation; synthetic only."""
from copy import deepcopy
import json
import re

import pytest

from dify.negotiation_v22.build import build
from dify.negotiation_v22 import prompts
from dify.negotiation_v22.node_code import compact_context, decode_stage_result
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact
from server.negotiation.reply_render import render_reply_plan
from test_rendered_reply_plan_revision import context
from test_reference_window_policy import Desk, OWNER
from test_production_flow import desk, OWNER as HTTP_OWNER
from test_reply_text_repair import reply_case


def dynamic_context():
    ctx = context()
    ctx['quote_constraints'] = Engine._quote_constraints(ctx)
    return ctx


def natural(**changes):
    plan = {'kind': 'REPLY', 'quote_value': '3.15',
            'text': '你提出的{peer_quote}我看到了，我也往前走些，{own_quote}，这个位置再看看？'}
    plan.update(changes)
    return plan


def accepted(ctx, plan):
    rendered = render_reply_plan(ctx, plan)
    return Engine.__new__(Engine)._validate_reply(ctx, rendered['proposal'])


def test_reference_step_does_not_cap_dynamic_concession_or_create_another_boundary():
    ctx = dynamic_context()
    assert ctx['quote_constraints']['min_yield_pct'] == '3.12'
    assert ctx['quote_constraints']['max_yield_pct'] is None
    assert ctx['quote_constraints']['reference_step_bp'] == '2'
    assert accepted(ctx, natural())['our_price']['value'] == '3.15'
    assert accepted(ctx, natural(quote_value='3.30'))['our_price']['value'] == '3.30'


def test_natural_text_is_preserved_and_peer_reference_is_not_our_price_or_side():
    ctx = dynamic_context()
    body = accepted(ctx, natural())
    assert body['text'] == '你提出的3.10%出我看到了，我也往前走些，3.15%收，这个位置再看看？'
    assert 'render_plan' not in body


def test_discussing_counterparty_concession_does_not_expose_our_private_step_setting():
    assert accepted(dynamic_context(), natural(text='你这次让步幅度挺大，我也靠近些，{own_quote}。'))
    with pytest.raises((ValueError, InvalidFact), match='^Public reply exposes private policy or internal context$'):
        accepted(dynamic_context(), natural(text='我方参考步幅比较小，{own_quote}。'))


@pytest.mark.parametrize('price', ['3.11', '3.125', 'NaN'])
def test_dynamic_price_still_respects_final_boundary_and_precision(price):
    with pytest.raises((ValueError, InvalidFact)):
        accepted(dynamic_context(), natural(quote_value=price))


@pytest.mark.parametrize('text', ['我这边3.16%收',
                                '{own_quote}，相差5BP', '{unknown}', '{peer_quote}，成交'])
def test_unbound_economics_unknown_slots_and_missing_own_quote_are_rejected(text):
    with pytest.raises((ValueError, InvalidFact)):
        accepted(dynamic_context(), natural(text=text))


def test_verified_whole_lot_literal_matches_the_original_macro_but_wrong_lot_still_rejects():
    context = dynamic_context()
    expected = accepted(context, natural(text='{own_quote}，再给{peer_volume}'))
    assert accepted(context, natural(text='{own_quote}，再给500万')) == expected
    with pytest.raises((ValueError, InvalidFact)):
        accepted(context, natural(text='{own_quote}，再给501万'))


@pytest.mark.parametrize('text,reason', [
    ('{own_quote}，成交', 'Reply promises execution or reserves a counterparty lot'),
    ('{own_quote}，内部底价就在这里', 'Public reply exposes private policy or internal context'),
])
def test_bound_natural_text_does_not_allow_commitment_or_private_policy(text, reason):
    with pytest.raises((ValueError, InvalidFact), match='^'+re.escape(reason)+'$'):
        accepted(dynamic_context(), natural(text=text))


def test_old_grid_template_contract_is_rejected():
    with pytest.raises(ValueError):
        render_reply_plan(dynamic_context(), {'kind': 'REPLY', 'quote_index': 0})


def test_verified_reference_cannot_be_relabelled_as_the_other_speaker():
    with pytest.raises(ValueError):
        accepted(dynamic_context(), natural(text='我报{peer_quote}，现在{own_quote}。'))


def test_unplanned_chinese_step_and_wrong_own_quote_speaker_are_not_free_wording():
    for text in ('{own_quote}，再让十个点。', '{own_quote}，我再退五厘。', '你报{own_quote}，再看看？'):
        with pytest.raises(ValueError):
            accepted(dynamic_context(), natural(text=text))


def test_unbound_legacy_proposal_cannot_attach_the_opposite_side_to_its_lot():
    with pytest.raises(InvalidFact):
        Engine.__new__(Engine)._validate_reply(dynamic_context(), {'kind':'REPLY',
            'text':'SYN-A 3.15%收，出500万', 'our_price':{'kind':'YIELD','value':'3.15'}})


def test_text_only_patch_rebinds_only_natural_plan_text_and_keeps_economics_frozen():
    ctx=dynamic_context()
    ctx.update(schema_version='2.2',stage='reply')
    ctx['quotes']['ofr']['evidence']['volume_wan']=['peer-a-message']
    proposal=render_reply_plan(ctx,natural(text='{own_quote}，出{peer_volume}。'))['proposal']
    original={'schema_version':'2.2','action':'SEND','proposal':proposal,'trader_view':{}}
    ctx['validation_feedback']={'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
        'reason':'Reply direction differs from our authorized side','original_response':deepcopy(original)}
    fixed=decode_stage_result({'schema_version':'2.2','text_repair':{'text':'{own_quote}，{peer_volume}整笔，烦请再看看。'}},ctx,'reply')
    checked=Engine.__new__(Engine)._validate_reply(ctx,fixed['proposal'])
    assert checked['text']=='3.15%收，500万整笔，烦请再看看。'
    assert Engine._reply_without_text(fixed)==Engine._reply_without_text(original)


def test_ordinary_model_send_cannot_bypass_source_binding_with_a_bare_proposal(desk):
    case=reply_case(desk)
    case.flows.decide=lambda context:{'schema_version':'2.2','action':'SEND',
        'proposal':{'kind':'REPLY','text':'SYN-A 3.19%收，500万报价。',
                    'our_price':{'kind':'YIELD','value':'3.19'}},'trader_view':{}}
    result=case.app.state.negotiation_engine.decide_member(HTTP_OWNER,case.peer['member_id'])
    assert result['state']=='REJECTED'
    assert result.get('command_id') is None


@pytest.mark.parametrize('text', [
    '我方底线就是{own_quote}。', '就这么定了，{own_quote}。',
    '按这个给你做了，{own_quote}。', '不会自动成交但这笔给你锁量，{own_quote}。',
    '不得不成交，{own_quote}。', '不得不自动成交，{own_quote}。', '不会不自动成交，{own_quote}。',
])
def test_private_floor_and_each_execution_assertion_are_rejected_independently(text):
    reason = ('Public reply exposes private policy or internal context' if '底线' in text
              else 'Reply promises execution or reserves a counterparty lot')
    with pytest.raises((ValueError, InvalidFact), match='^'+re.escape(reason)+'$'):
        accepted(dynamic_context(), natural(text=text))


@pytest.mark.parametrize('text', [
    '不自动成交、不锁量，只继续报价，{own_quote}。',
    '不会承诺成交，也不占用额度，{own_quote}。',
    '这里只谈报价，不涉及成交，{own_quote}。',
    '不能就这么定了，先看看{own_quote}。',
])
def test_explicit_nonexecution_disclaimer_remains_legal_natural_wording(text):
    assert accepted(dynamic_context(), natural(text=text))['text']


@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_generated_contract_uses_the_same_natural_body_and_no_grid(version):
    ctx = {**dynamic_context(), 'schema_version': '2.2', 'stage': 'reply',
           'context_policy_revision': 'latest_quote_confirmation_v1'}
    raw = {'schema_version': '2.2', 'reply_plan': natural(), 'trader_view': {}}
    expected = decode_stage_result(raw, ctx, 'reply')
    graph = build('reply', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    scope = {}
    exec(next(n['data']['code'] for n in graph['nodes'] if n['id'] == 'contract'), scope)
    assert json.loads(scope['main'](json.dumps(raw), json.dumps(ctx))['result']) == expected
    assert 'quote_grid' not in compact_context(ctx)
    assert accepted(ctx, expected['proposal']['render_plan'])['text'] == expected['proposal']['text']


def test_complete_trusted_outbound_snapshot_commits_without_model_call(tmp_path):
    case = Desk(tmp_path / 'trusted-out.sqlite')
    commands = case.service.commands_core
    case.store.set_sending(OWNER, True)
    command = commands.propose(OWNER, case.member, basis=commands.basis(OWNER, case.member),
        request_id='synthetic-bound-clarify', proposal={'kind':'CLARIFY', 'text':'请确认当前报价'},
        validator=lambda value:value, expires_at=case.now+60)
    assert commands.claim(OWNER, command['command_id'])
    row=commands.get(OWNER, command['command_id'])
    assert commands.receipt(OWNER, command['command_id'], account_uid='synthetic-account',
        peer_uid='synthetic-peer', content_hash=row['content_hash'], status='SENT',
        native_id='synthetic-known-out', evidence='synthetic-receipt', occurred_at=case.now) == 'SENT'
    calls=[]
    case.flows.analyze=lambda snapshot:calls.append(deepcopy(snapshot)) or case.envelope(snapshot, [])
    result=case.engine.analyze_thread(OWNER, case.thread, force=True)
    assert result['facts_committed'] and not result['accepted'] and not result['rejected']
    assert calls == []
    assert result['repair']['mode'] == 'KNOWN_OUTBOUND_ONLY'


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
@pytest.mark.parametrize('side,operator',[('BUY','>='),('SELL','<=')])
def test_current_reply_side_and_single_hard_boundary_are_front_loaded_in_both_generated_versions(side,operator,version):
    domain={'status':'AVAILABLE','tick_size':'0.01','min_yield_pct':'3.12' if side=='BUY' else None,
            'max_yield_pct':'3.12' if side=='SELL' else None,'reference_step_bp':'2'}
    ctx={'schema_version':'2.2','stage':'reply','member_id':'synthetic-member',
         'our_side':side,'quote_constraints':domain,'context':[]}
    system=prompts.select_system_prompt(ctx,'reply')
    assert system.startswith('本轮受信执行摘要')
    assert '我方'+side in system and 'quote_value '+operator+' 3.12' in system
    assert 'text必须逐字保留ASCII花括号{own_quote}' in system
    assert '不得出现任何裸写的0-9数字' in system
    graph=build('reply',version=version,provider='synthetic',model='synthetic')['workflow']['graph']
    scope={}
    exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='context'),scope)
    assert scope['main'](json.dumps(ctx))['system_prompt']==system


def test_text_only_repair_rejects_the_removed_unbound_body_compatibility_branch():
    ctx={'schema_version':'2.2','stage':'reply','validation_feedback':{
        'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
        'reason':'Public yield text differs from validated quote',
        'original_response':{'schema_version':'2.2','action':'SEND','trader_view':{},
            'proposal':{'kind':'REPLY','text':'SYN-A 3.15收',
                        'our_price':{'kind':'YIELD','value':'3.15'}}}}}
    with pytest.raises(ValueError,match='source-bound'):
        decode_stage_result({'schema_version':'2.2','text_repair':{'text':'SYN-A 3.15%收'}},ctx,'reply')


def test_repair_system_no_longer_instructs_a_naked_body_exception():
    repair=prompts._SYSTEMS['reply']['TEXT_ONLY']
    assert '缺少计划直接拒绝' in repair
    assert '没有render_plan的冻结候选才修完整公开正文' not in repair
