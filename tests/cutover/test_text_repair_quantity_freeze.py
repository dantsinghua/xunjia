"""Freeze the new request-source category at Code and the final Engine gate."""
from copy import deepcopy
import json

import pytest

from server.negotiation.engine import Engine
from server.negotiation.reply_render import render_reply_plan
from test_requested_quantity_offer import asked_case,offer
from test_reply_output_repair_projection import generated
from test_reference_window_policy import Desk,OWNER

REQUEST='按您询问的{requested_volume}，这边{own_quote}，请再看看？'
FIRM='这边{own_quote}{peer_volume}，请再看看？'
NOLOT='这边{own_quote}，请再看看？'
REPEAT='Reply repeats the last verified native quotation'


def source_case(path,*,same_amount=False):
    if not same_amount:
        case,_source,saved,context,_=asked_case(path,private_target='100',prior_firm_side='ofr')
        assert saved['accepted'] and not saved['rejected']
        return case,context
    case=Desk(path)
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
            (json.dumps({'opening_yield_pct':'3.20','step_bp':'2','volume_wan':'10000'}),case.group))
    for side,text,intent in [('ofr','SYN-A 3.10出500万','QUOTE'),('bid','你能3.10收500万吗','QUESTION')]:
        ref=case.inbound(text)
        changes={'price':{'kind':'YIELD','value':'3.10'},'volume_wan':'500','assertion':intent,
            'validity':'ACTIVE','finality':'UNKNOWN','confirmed_day':'2026-10-17' if intent=='QUOTE' else None}
        saved=case.analyze([{'member_id':case.member,'side':side,'changes':changes,
            'evidence':{key:[ref] for key in changes}}])
        assert saved['accepted'] and not saved['rejected']
    context=case.reply_context();context.update(schema_version='2.2',stage='reply')
    assert context['quotes']['ofr']['volume_wan']==context['quotes']['bid']['volume_wan']=='500'
    return case,context


def response(context,text):
    return {'schema_version':'2.2','action':'SEND','proposal':render_reply_plan(context,offer(text))['proposal'],'trader_view':{}}


def text_context(context,original):
    value=deepcopy(context);value['validation_feedback']={'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
        'reason':REPEAT,'original_response':deepcopy(original)}
    return value


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
@pytest.mark.parametrize('initial,patched',[(REQUEST,FIRM),(FIRM,REQUEST),(REQUEST,NOLOT),(NOLOT,REQUEST)])
def test_generated_text_repair_cannot_change_the_request_source_binding(tmp_path,version,initial,patched):
    _case,context=source_case(tmp_path/'quantity-freeze.sqlite')
    original=response(context,initial)
    Engine.__new__(Engine)._validate_reply(context,deepcopy(original['proposal']))
    Engine.__new__(Engine)._validate_reply(context,response(context,patched)['proposal'])
    trusted=text_context(context,original);before=deepcopy(trusted);_project,decode=generated(version)
    with pytest.raises(ValueError):
        decode(json.dumps({'schema_version':'2.2','text_repair':{'text':patched}}),json.dumps(trusted))
    assert trusted==before


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_equal500_amount_does_not_authorize_changing_question_offer_into_peer_firm_source(tmp_path,version):
    _case,context=source_case(tmp_path/'same-amount.sqlite',same_amount=True)
    original=response(context,REQUEST)
    assert '500万' in original['proposal']['text'] and '500万' in response(context,FIRM)['proposal']['text']
    trusted=text_context(context,original);before=deepcopy(trusted);_project,decode=generated(version)
    with pytest.raises(ValueError):
        decode(json.dumps({'schema_version':'2.2','text_repair':{'text':FIRM}}),json.dumps(trusted))
    assert trusted==before


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
@pytest.mark.parametrize('initial,patched',[(REQUEST,'按您询问的{requested_volume}，我这边{own_quote}，等您反馈。'),
    (FIRM,'我这边{own_quote}{peer_volume}，等您反馈。'),(NOLOT,'我这边{own_quote}，等您反馈。')])
def test_same_source_wording_repair_and_original_no_request_repair_remain_legal(tmp_path,version,initial,patched):
    _case,context=source_case(tmp_path/'same-source.sqlite');original=response(context,initial)
    trusted=text_context(context,original);before=deepcopy(trusted);_project,decode=generated(version)
    repaired=json.loads(decode(json.dumps({'schema_version':'2.2','text_repair':{'text':patched}}),json.dumps(trusted))['result'])
    expected=response(context,patched)
    assert repaired==expected and Engine._reply_without_text(repaired)==Engine._reply_without_text(original)
    assert Engine.__new__(Engine)._validate_reply(context,deepcopy(repaired['proposal']))['our_price']['value']=='3.15'
    assert trusted==before


def command_rows(case):
    with case.store.db.transaction(write=False) as tx:
        return [dict(row) for row in tx.execute('SELECT * FROM xj2_command WHERE member_id=?',(case.member,)).fetchall()]


@pytest.mark.parametrize('initial,patched',[(REQUEST,FIRM),(FIRM,REQUEST),(REQUEST,NOLOT),(NOLOT,REQUEST)])
def test_full_engine_repair_rejects_a_faked_code_success_that_changes_request_binding(tmp_path,initial,patched):
    case,context=source_case(tmp_path/'engine-freeze.sqlite');case.store.set_sending(OWNER,True)
    before=deepcopy(context['quotes']);calls=[]
    def decide(ctx):
        calls.append(deepcopy(ctx));assert len(calls)<=2
        if len(calls)==1:return response(ctx,initial+'我出给你。')
        assert ctx['validation_feedback']['mode']=='TEXT_ONLY'
        assert ctx['validation_feedback']['reason']=='Reply direction differs from our authorized side'
        return response(ctx,patched)  # Deliberately bypass generated Code to exercise the independent final gate.
    case.flows.decide=decide
    result=case.engine.decide_member(OWNER,case.member)
    assert len(calls)==2 and result['state']=='REJECTED' and not result.get('command_id'), result
    assert result['repair']['attempted'] and not result['repair']['succeeded']
    assert command_rows(case)==[] and case.reply_context()['quotes']==before


@pytest.mark.parametrize('initial,patched',[(REQUEST,'按您询问的{requested_volume}，我这边{own_quote}，等您反馈。'),
    (NOLOT,'我这边{own_quote}，等您反馈。')])
def test_full_engine_same_binding_repair_queues_only_the_valid_new_words(tmp_path,initial,patched):
    case,context=source_case(tmp_path/'engine-positive.sqlite');case.store.set_sending(OWNER,True)
    before=deepcopy(context['quotes']);calls=[]
    def decide(ctx):
        calls.append(deepcopy(ctx));assert len(calls)<=2
        return response(ctx,initial+'我出给你。' if len(calls)==1 else patched)
    case.flows.decide=decide;result=case.engine.decide_member(OWNER,case.member)
    assert result['state']=='QUEUED' and len(calls)==2 and result['repair']['succeeded'], result
    rows=command_rows(case);assert len(rows)==1
    assert json.loads(rows[0]['body_json'])['text']==response(calls[-1],patched)['proposal']['text']
    assert case.reply_context()['quotes']==before


def test_first_ordinary_reply_can_still_choose_not_to_offer_a_quantity(tmp_path):
    case,context=source_case(tmp_path/'first-no-lot.sqlite');case.store.set_sending(OWNER,True)
    before=deepcopy(context['quotes']);calls=[]
    def decide(ctx):calls.append(deepcopy(ctx));return response(ctx,NOLOT)
    case.flows.decide=decide;result=case.engine.decide_member(OWNER,case.member)
    assert result['state']=='QUEUED' and len(calls)==1 and not result['repair']['attempted'], result
    assert len(command_rows(case))==1 and case.reply_context()['quotes']==before
