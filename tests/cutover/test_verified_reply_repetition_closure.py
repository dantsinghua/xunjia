"""Verified native repetition and public-price closure keep original hard gates."""
from copy import deepcopy
import json
import pytest

from server.negotiation.engine import Engine
from dify.negotiation_v22.build import build
from server.negotiation.reply_render import render_reply_plan
from test_reference_window_policy import DAY,OWNER,Desk
from test_verified_literal_fact_binding import canonical_analysis_view,cash_context
from test_dynamic_negotiation_acceptance import dynamic_context
from test_literal_member_hints_revision import context as analysis_context
from server.negotiation.slots import collect_candidates


REPEAT='Reply repeats the last verified native quotation'
ORIGINAL='{bond} {own_quote}，您再看看？'
REPAIRED='{bond} {own_quote}，我这边等您进一步反馈。'


def proposal(context,text=ORIGINAL,value='3.19',kind='REPLY'):
    plan={'kind':kind,'text':text,'question':'NONE'}
    if kind=='REPLY':plan['quote_value']=value
    return {'schema_version':'2.2','action':'SEND','proposal':render_reply_plan(context,plan)['proposal']}


def sent_case(tmp_path):
    case=Desk(tmp_path/'verified-repeat.sqlite')
    with case.store.write(OWNER) as tx:
        tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
            (json.dumps({'code':'SYN-A','name':'SYN-A','opening_yield_pct':'3.20',
                         'step_bp':'2','volume_wan':'10000'}),case.group))
    case.store.set_sending(OWNER,True)
    incoming=case.inbound('SYN-A 3.10出500万，还可以谈')
    assert case.analyze([case.observation(incoming,price={'kind':'YIELD','value':'3.10'},
        volume_wan='500',assertion='QUOTE',validity='ACTIVE',finality='NEGOTIABLE',confirmed_day=DAY)])['accepted']
    case.flows.decide=lambda context:proposal(context)
    first=case.engine.decide_member(OWNER,case.member);assert first['state']=='QUEUED'
    cid=first['command_id'];commands=case.service.commands_core;assert commands.claim(OWNER,cid)
    command=commands.get(OWNER,cid);case.now+=1
    assert commands.receipt(OWNER,cid,account_uid='synthetic-account',peer_uid='synthetic-peer',
        content_hash=command['content_hash'],status='SENT',evidence='synthetic exact native record',
        native_id='synthetic-repeat-native-'+cid,occurred_at=case.now)=='SENT'
    case.now+=1;case.inbound('SYN-A 稍等，我再想想',occurred_at=case.now)
    assert case.analyze([])['facts_committed']
    assert case.reply_context()['our_quote']['verified_sent'] is True
    return case,cid


def contract(version='1.17.1'):
    nodes=build('reply',version=version,provider='synthetic',model='synthetic')['workflow']['graph']['nodes']
    scope={};exec(next(row['data']['code'] for row in nodes if row['id']=='contract'),scope)
    return scope['main']


def test_repeating_current_verified_native_reply_gets_one_real_text_patch_before_any_new_queue(tmp_path):
    case,_cid=sent_case(tmp_path);facts=deepcopy(case.quote());calls=[];decode=contract()
    def model(context):
        calls.append(deepcopy(context))
        assert len(calls)<=2
        if len(calls)==1:return proposal(context)
        feedback=context['validation_feedback']
        assert feedback['mode']=='TEXT_ONLY' and feedback['reason']==REPEAT
        assert feedback['attempt']==feedback['max_attempts']==1
        return json.loads(decode(json.dumps({'schema_version':'2.2','text_repair':{'text':REPAIRED}}),
            json.dumps({**context,'schema_version':'2.2','stage':'reply'}))['result'])
    case.flows.decide=model
    result=case.engine.decide_member(OWNER,case.member)
    assert len(calls)==2, 'A fresh job must not queue the exact last verified SENT text again'
    assert result['state']=='QUEUED' and result['repair']['succeeded'] is True
    body=json.loads(case.service.commands_core.get(OWNER,result['command_id'])['body_json'])
    assert body['text']=='SYN-A 3.19%收，我这边等您进一步反馈。'
    assert body['kind']=='REPLY' and body['our_price']['value']=='3.19'
    assert case.quote()==facts


def repeat_model(case,*,patch_text=REPAIRED,mutation=None):
    calls=[];decode=contract()
    def model(context):
        calls.append(deepcopy(context));assert len(calls)<=2
        if len(calls)==1:return proposal(context)
        assert context['validation_feedback']['reason']==REPEAT
        answer=json.loads(decode(json.dumps({'schema_version':'2.2','text_repair':{'text':patch_text}}),
            json.dumps({**context,'schema_version':'2.2','stage':'reply'}))['result'])
        if mutation=='price':
            plan={**answer['proposal']['render_plan'],'quote_value':'3.18'}
            answer['proposal']=render_reply_plan(context,plan)['proposal']
        elif mutation=='kind':
            answer['proposal']=render_reply_plan(context,{'kind':'CLARIFY','question':'NONE',
                'text':'请问您当前的报价和整量？'})['proposal']
        return answer
    case.flows.decide=model
    return calls


def assert_no_new_command(case,cid,facts):
    with case.store.db.transaction(write=False) as tx:
        assert tx.execute('SELECT COUNT(*) AS n FROM xj2_command WHERE thread_id=?',(case.thread,)).fetchone()['n']==1
    assert case.service.commands_core.get(OWNER,cid)['state']=='SENT' and case.quote()==facts


def test_text_patch_that_repeats_again_never_queues_a_second_native_command(tmp_path):
    case,cid=sent_case(tmp_path);facts=deepcopy(case.quote());calls=repeat_model(case,patch_text=ORIGINAL)
    result=case.engine.decide_member(OWNER,case.member)
    assert len(calls)==2 and result['state']=='REJECTED' and not result.get('command_id')
    assert result['repair']['attempted'] and not result['repair']['succeeded']
    assert_no_new_command(case,cid,facts)


@pytest.mark.parametrize('mutation',['price','kind'])
def test_repetition_repair_cannot_change_the_price_or_nontext_business_action(tmp_path,mutation):
    case,cid=sent_case(tmp_path);facts=deepcopy(case.quote());calls=repeat_model(case,mutation=mutation)
    result=case.engine.decide_member(OWNER,case.member)
    assert len(calls)==2 and result['state']=='REJECTED' and not result.get('command_id')
    assert_no_new_command(case,cid,facts)


def test_same_legal_price_with_different_meaningful_wording_is_not_a_repeat(tmp_path):
    case,_cid=sent_case(tmp_path);facts=deepcopy(case.quote());calls=[]
    case.flows.decide=lambda ctx:calls.append(deepcopy(ctx)) or proposal(ctx,REPAIRED)
    result=case.engine.decide_member(OWNER,case.member)
    assert result['state']=='QUEUED' and len(calls)==1 and not result['repair']['attempted']
    assert case.quote()==facts


@pytest.mark.parametrize('scope',['unverified','other_peer','old_day','old_epoch'])
def test_a_text_match_without_current_member_day_epoch_native_proof_is_not_repeat_authority(tmp_path,scope):
    case,cid=sent_case(tmp_path)
    with case.store.write(OWNER) as tx:
        member=case.store._member(tx,OWNER,case.member);actual=json.loads(member['our_quote_json'])
        if scope=='unverified':
            actual.pop('native_id');tx.execute('UPDATE xj2_peer SET our_quote_json=? WHERE member_id=?',
                (json.dumps(actual),case.member))
        elif scope=='old_day':
            tx.execute('UPDATE xj2_message SET trade_day=? WHERE message_id=?',('2026-10-16',actual['message_id']))
        elif scope=='old_epoch':
            tx.execute('UPDATE xj2_command SET account_epoch=-1 WHERE command_id=?',(cid,))
    if scope=='other_peer':
        other,_thread=case.store.add_member(OWNER,case.group,account_uid='synthetic-account',peer_uid='synthetic-other-peer')
        # A stale/cross-peer signed-scope link cannot lend its SENT proof.
        with case.store.write(OWNER) as tx:
            tx.execute('UPDATE xj2_command_member SET member_id=? WHERE command_id=? AND member_id=?',
                       (other,cid,case.member))
    assert not case.reply_context()['our_quote'].get('verified_sent')
    facts=deepcopy(case.quote());calls=[]
    case.flows.decide=lambda ctx:calls.append(deepcopy(ctx)) or proposal(ctx)
    result=case.engine.decide_member(OWNER,case.member)
    assert result['state']=='QUEUED' and len(calls)==1 and not result['repair']['attempted']
    assert case.quote()==facts


def test_legal_social_reply_is_not_forced_into_quotation_text_repair(tmp_path):
    case,_cid=sent_case(tmp_path);facts=deepcopy(case.quote());calls=[]
    case.flows.decide=lambda ctx:calls.append(deepcopy(ctx)) or proposal(ctx,'收到，我这边等您反馈。',kind='SOCIAL')
    result=case.engine.decide_member(OWNER,case.member)
    assert result['state']=='QUEUED' and len(calls)==1 and not result['repair']['attempted']
    assert case.quote()==facts


def project_and_contract(stage,version):
    nodes={row['id']:row['data'] for row in build(stage,version=version,provider='synthetic',model='synthetic')['workflow']['graph']['nodes']}
    a,b={},{};exec(nodes['context']['code'],a);exec(nodes['contract']['code'],b)
    return a['main'],b['main']


def crossed_context(side):
    ctx=dynamic_context(side);ctx.update(schema_version='2.2',stage='reply')
    key='ofr' if side=='BUY' else 'bid';peer=ctx['quotes'][key]
    peer['price']['value']='1.65' if side=='BUY' else '1.55';peer['finality']='UNKNOWN'
    ctx['context'][0]['text']='SYN-A '+peer['price']['value']+('出' if side=='BUY' else '收')+'500万'
    return ctx,key


def closing_hint(system):return '优先CLARIFY/NONE' in system and 'FINAL' in system


def test_both_crossed_yield_sides_prefer_final_quote_clarification_without_inventing_finality_or_handoff():
    for version in ('0.15.3','1.17.1'):
        project,decode=project_and_contract('reply',version)
        for side in ('BUY','SELL'):
            ctx,key=crossed_context(side);before=deepcopy(ctx);system=project(json.dumps(ctx))['system_prompt']
            assert system.startswith('本轮受信执行摘要') and closing_hint(system)
            plan={'kind':'CLARIFY','question':'NONE','text':'你当前{peer_quote}和{peer_volume}，是本人最终可做报价和整量吗？'}
            result=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':plan}),json.dumps(ctx))['result'])
            assert result['action']=='SEND' and result['proposal']['kind']=='CLARIFY' and not result['proposal'].get('our_price')
            assert Engine.__new__(Engine)._validate_reply(ctx,result['proposal'])['kind']=='CLARIFY'
            assert ctx==before and ctx['quotes'][key]['finality']=='UNKNOWN'
            for invalid in ('not_crossed','unverified','question','withdrawn','old_day','no_volume_source','already_final'):
                changed=deepcopy(ctx);peer=changed['quotes'][key]
                if invalid=='not_crossed':peer['price']['value']='1.50' if side=='BUY' else '1.70'
                elif invalid=='unverified':changed['our_quote']['verified_sent']=False
                elif invalid=='question':peer['assertion']='QUESTION'
                elif invalid=='withdrawn':peer['validity']='WITHDRAWN'
                elif invalid=='old_day':peer['confirmed_day']='2026-10-16'
                elif invalid=='no_volume_source':peer['evidence'].pop('volume_wan')
                else:peer['finality']='FINAL'
                frozen=deepcopy(changed);assert not closing_hint(project(json.dumps(changed))['system_prompt']);assert changed==frozen
            waiting=deepcopy(ctx);waiting.update(reconfirm_day=DAY,reconfirmation_waiting=True)
            output=project(json.dumps(waiting));assert not closing_hint(output['system_prompt'])
            assert json.loads(decode(json.dumps({'schema_version':'2.2','action':'WAIT'}),json.dumps(waiting))['result'])['action']=='WAIT'
            cash=cash_context();cash.update(schema_version='2.2',stage='reply')
            assert not closing_hint(project(json.dumps(cash))['system_prompt'])
            tool=deepcopy(ctx);tool['context'].append({'message_id':'synthetic-calculation','direction':'in','ingest_revision':22,
                'trade_day':DAY,'text':'SYN-A '+ctx['quotes'][key]['price']['value']+('出' if side=='BUY' else '收')+'500万。另外请用债券计算工具核算：假设同一到期收益率口径，1.65和你之前1.60收的差是多少BP？答复不重复这两个原始收益率，不作成交。'})
            tool_system=project(json.dumps(tool))['system_prompt']
            assert 'bond_calc' in tool_system and tool_system.index('bond_calc')<tool_system.index('优先CLARIFY/NONE')


def test_analysis_recommends_only_inbound_candidates_but_keeps_all_facts_and_both_text_repair_closures():
    for version in ('0.15.3','1.17.1'):
        project,_decode=project_and_contract('analysis',version)
        ctx=analysis_context('SYN-A 3.10出500万');ctx['messages'].append({'message_id':'verified-current-out','direction':'out',
            'native_id':'synthetic-known-native','historical':False,'ingest_revision':42,'trade_day':DAY,'text':'SYN-A 3.20%收1000万'})
        ctx['known_outbound_scopes']={'verified-current-out':['a']};before=deepcopy(ctx);view=canonical_analysis_view(ctx,json.loads(project(json.dumps(ctx))['context']))
        assert view['messages']==ctx['messages'] and view['known_outbound_scopes']==ctx['known_outbound_scopes']
        assert [m['context'] for m in view['members']]==[m['context'] for m in ctx['members']]
        candidates=collect_candidates(ctx);by_id={c['id']:c for c in candidates};recommended={i for row in view['current_member_candidates'] for i in row['candidate_ids']}
        assert recommended and all(by_id[i]['message_id']=='current' for i in recommended)
        expected={c['id'] for c in candidates if c['message_id'] in {'current','verified-current-out'} or set(c['roles'])&{'YIELD','YIELD_SUFFIX','CHINABOND_BP','NET_PRICE','DIRTY_PRICE','QUANTITY'}}
        assert {c['id'] for c in view['slot_candidates']}==expected and ctx==before
        reply_ctx=dynamic_context();reply_ctx.update(schema_version='2.2',stage='reply')
        original=proposal(reply_ctx,'{bond} {own_quote}',value='1.60')
        reply_ctx['validation_feedback']={'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,'reason':REPEAT,'original_response':original}
        old=deepcopy(reply_ctx);project_reply,decode=project_and_contract('reply',version)
        assert json.loads(project_reply(json.dumps(reply_ctx))['context'])['validation_feedback']==reply_ctx['validation_feedback']
        changed=json.loads(decode(json.dumps({'schema_version':'2.2','text_repair':{'text':'{bond} {own_quote}，我等您回复。'}}),json.dumps(reply_ctx))['result'])
        expected=deepcopy(original);expected['proposal']=render_reply_plan(reply_ctx,{**original['proposal']['render_plan'],'text':'{bond} {own_quote}，我等您回复。'})['proposal']
        assert changed==expected and reply_ctx==old
