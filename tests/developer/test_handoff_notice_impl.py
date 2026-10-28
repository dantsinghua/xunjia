"""Synthetic acknowledgement notice lifecycle; no real model or native sends."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from server.negotiation.application_store import ApplicationStore
from server.negotiation.commands import Commands
from server.negotiation.engine import Engine
from server.negotiation.prices import InvalidFact, LimitPolicy, Price
from server.negotiation.reply_render import render_reply_plan, reply_requirements
from server.negotiation.store import encoded

DAY='2026-10-27'
OWNER='synthetic-notice-owner'
ACCEPTANCE={'meaning':'ACCEPTED_CURRENT_QUOTE','source':'LATEST_INBOUND'}


def pure_context():
    messages=[{'message_id':'own-message','thread_id':'thread','direction':'out','kind':'message','historical':0,
               'trade_day':DAY,'ingest_revision':1,'occurred_at':10,'text':'SYN-A 2.12%出','native_id':'native-own'},
              {'message_id':'quote-message','thread_id':'thread','direction':'in','kind':'message','historical':0,
               'trade_day':DAY,'ingest_revision':2,'occurred_at':11,'text':'SYN-A 2.12收1000万'},
              {'message_id':'accept-message','thread_id':'thread','direction':'in','kind':'message','historical':0,
               'trade_day':DAY,'ingest_revision':3,'occurred_at':12,'text':'这个报价认可'}]
    return {'member_id':'member','thread_id':'thread','bond_code':'SYN-A','our_side':'SELL','trade_day':DAY,
            'basis':{'observed_watermark':3},'reconfirm_day':None,'unresolved_messages':[],
            'boundary':{'kind':'YIELD','value':'2.12'},
            'plan':{'opening_yield_pct':'1.98','step_bp':'1','volume_wan':'2000'},
            'group_summary':{'reported_shortfall_wan':'1000','goal_ready':False},
            'quote_constraints':{'status':'AVAILABLE','price_kind':'YIELD','tick_size':'0.01','min_yield_pct':None,'max_yield_pct':'2.12'},
            'our_quote':{'price':Price('YIELD','2.12').dump(),'verified_sent':True,'command_id':'own-command',
                         'message_id':'own-message','native_id':'native-own','sent_at':10},
            'quotes':{'bid':{'side':'bid','price':Price('YIELD','2.12').dump(),'volume_wan':'1000','assertion':'QUOTE',
                              'validity':'ACTIVE','confirmed_day':DAY,'finality':'UNKNOWN','revision':1,
                              'evidence':{'price':['quote-message'],'volume_wan':['quote-message']}}},
            'context':messages,'handoff_evidence':messages[:2]}


def plan(text='收到，我这边跟进。'):
    return {'kind':'HANDOFF_NOTICE','text':text,'acceptance':dict(ACCEPTANCE)}


def test_notice_requires_explicit_model_choice_and_keeps_old_current_day_lot():
    context=pure_context();before=deepcopy(context)
    assert 'HANDOFF_NOTICE' in reply_requirements(context)['allowed_kinds']
    social=render_reply_plan(context,{'kind':'SOCIAL','text':'收到，谢谢。'})['proposal']
    assert social['kind']=='SOCIAL'
    rendered=render_reply_plan(context,plan())['proposal']
    body=Engine.__new__(Engine)._validate_reply(context,rendered)
    assert body['handoff_notice']=={'side':'bid','quote_revision':1,'own_command_id':'own-command','source_ids':['accept-message']}
    assert 'our_price' not in body and '1000' not in body['text'] and '2000' not in body['text']
    assert context==before


@pytest.mark.parametrize('field',['scope_members','scope_prices','scope_bases'])
def test_notice_body_cannot_smuggle_scoped_economics(field):
    context=pure_context();body=Engine.__new__(Engine)._validate_reply(context,render_reply_plan(context,plan())['proposal'])
    body[field]=['member'] if field=='scope_members' else {'member':Price('YIELD','9.99').dump()}
    with pytest.raises(InvalidFact):Commands._body(body)


@pytest.mark.parametrize('change',['no-acceptance','no-quantity','old-quantity','question','withdrawn','old-day','unverified-own','different-price','old-inbound'])
def test_notice_rejects_unproved_current_price_quantity_and_semantic_contract(change):
    context=pure_context();candidate=plan()
    if change=='no-acceptance':candidate.pop('acceptance')
    if change=='no-quantity':context['quotes']['bid']['volume_wan']=None
    if change=='old-quantity':
        for message in context['context']+context['handoff_evidence']:
            if message['message_id']=='quote-message':message['trade_day']='2026-10-26'
    if change=='question':context['quotes']['bid']['assertion']='QUESTION'
    if change=='withdrawn':context['quotes']['bid']['validity']='WITHDRAWN'
    if change=='old-day':context['quotes']['bid']['confirmed_day']='2026-10-26'
    if change=='unverified-own':context['our_quote']['verified_sent']=False
    if change=='different-price':context['quotes']['bid']['price']=Price('YIELD','2.10').dump()
    if change=='old-inbound':context['context'][-1]['historical']=1
    with pytest.raises((ValueError,InvalidFact)):
        proposal=render_reply_plan(context,candidate)['proposal']
        Engine.__new__(Engine)._validate_reply(context,proposal)


@pytest.mark.parametrize('text',['收到1000万，我来跟进。','按2.12%确认。','{peer_volume}，收到。','成交，给您锁量。','目标2000万已经完成。'])
def test_notice_never_publishes_economics_or_trade_commitment(text):
    with pytest.raises((ValueError,InvalidFact)):
        proposal=render_reply_plan(pure_context(),plan(text))['proposal']
        Engine.__new__(Engine)._validate_reply(pure_context(),proposal)


@pytest.mark.parametrize('text',['我出{own_quote}，您看。','我收{own_quote}，您看。'])
def test_own_quote_repeated_or_conflicting_prefix_direction_is_not_silently_edited(text):
    with pytest.raises(ValueError,match='adjacent direction'):
        render_reply_plan(pure_context(),{'kind':'REPLY','quote_value':'2.12','text':text})


def test_own_quote_suffix_keeps_existing_matching_normalization_and_conflict_rejection():
    for side,matching,opposite in [('SELL','出','收'),('BUY','收','出')]:
        context=pure_context();context['our_side']=side
        base={'kind':'REPLY','quote_value':'2.12','text':'{own_quote}，您看。'}
        duplicate={**base,'text':'{own_quote}'+matching+'，您看。'}
        assert render_reply_plan(context,duplicate)['proposal']['text']==render_reply_plan(context,base)['proposal']['text']
        with pytest.raises(ValueError,match='suffix conflicts'):
            render_reply_plan(context,{**base,'text':'{own_quote}'+opposite+'，您看。'})


@pytest.mark.parametrize('text',['我这边{own_quote}，您看。','{own_quote}，您看。'])
def test_own_quote_normal_natural_position_remains_valid(text):
    value=render_reply_plan(pure_context(),{'kind':'REPLY','quote_value':'2.12','text':text})
    assert value['proposal']['our_price']['value']=='2.12'


class Desk:
    def __init__(self,tmp_path):
        self.now=datetime(2026,10,27,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.store=ApplicationStore('sqlite:///'+str(tmp_path/'notice.db'),clock=lambda:self.now,initialize=True)
        self.store.bind_account(OWNER,'synthetic-account','synthetic-workbench')
        self.store.set_sending(OWNER,True)
        self.commands=Commands(self.store,current_day=lambda:DAY)
        self.group=self.store.create_group(OWNER,bond_code='SYN-A',our_side='SELL',target_volume='2000',
                                           trade_day=DAY,policy=LimitPolicy('ABSOLUTE','2.12'),send_allowed=True)
        self.member,self.thread=self.store.add_member(OWNER,self.group,account_uid='synthetic-account',peer_uid='peer-a')
        self.other,_=self.store.add_member(OWNER,self.group,account_uid='synthetic-account',peer_uid='peer-b')
        with self.store.write(OWNER) as tx:
            tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',
                       (encoded({'opening_yield_pct':'1.98','step_bp':'1','volume_wan':'2000'}),self.group))
        initial=self.commands.propose(OWNER,self.member,basis=self.commands.basis(OWNER,self.member),request_id='own-quote',
                                     proposal={'kind':'REPLY','text':'SYN-A 2.12%出','our_price':Price('YIELD','2.12').dump()},
                                     validator=lambda value:value,expires_at=self.now+120)
        self.commands.claim(OWNER,initial['command_id']);self.receipt(initial['command_id'],'SENT','native-own')
        self.analyze()
        self.inbound('SYN-A 2.12收1000万',quote=True)
        self.inbound('这个报价认可',quote=False)
        self.engine=Engine(SimpleNamespace(store=self.store,commands_core=self.commands,
                                          diagnostics=SimpleNamespace(record=lambda *args,**kwargs:None)),None,None)
    def analyze(self,observation=None):
        job=self.store.claim_analysis(OWNER,self.thread,force=True)
        assert job
        messages=job['snapshot']['messages']
        self.store.commit_analysis(OWNER,job['analysis_id'],job['lease_token'],
                                   scopes={m['message_id']:[self.member] for m in messages},observations=observation or [])
    def inbound(self,text,quote=False,price='2.12'):
        self.now+=1
        mid=self.store.ingest(OWNER,self.thread,event_id='event-'+str(self.now),source_seq=int(self.now),
                              text=text,trade_day=DAY,occurred_at=self.now)
        changes={'price':Price('YIELD',price).dump(),'volume_wan':'1000','assertion':'QUOTE','validity':'ACTIVE','confirmed_day':DAY}
        observation=[{'member_id':self.member,'side':'bid','changes':changes,'evidence':{key:[mid] for key in changes}}] if quote else []
        self.analyze(observation)
    def context(self):
        with self.store.db.transaction(write=False) as tx:
            basis=self.commands._basis(tx,OWNER,self.member,True)
            return self.engine._reply_context(tx,OWNER,self.member,basis)
    def queue(self):
        context=self.context();proposal=render_reply_plan(context,plan())['proposal']
        return self.commands.propose(OWNER,self.member,basis=context['basis'],request_id='notice',proposal=proposal,
                                     validator=lambda value:self.engine._validate_reply(context,value),expires_at=self.now+120)
    def receipt(self,cid,status,native=None):
        command=self.commands.get(OWNER,cid)
        return self.commands.receipt(OWNER,cid,account_uid='synthetic-account',peer_uid='peer-a',
                                     content_hash=command['content_hash'],status=status,evidence='synthetic-'+status,native_id=native,occurred_at=self.now)
    def peer(self,mid=None):
        with self.store.db.transaction(write=False) as tx:return self.store._member(tx,OWNER,mid or self.member)
    def handoffs(self):
        with self.store.db.transaction(write=False) as tx:
            return list(tx.execute("SELECT * FROM xj2_decision WHERE member_id=? AND state='HANDOFF'",(self.member,)).fetchall())


def test_notice_transitions_only_after_native_sent_without_group_completion(tmp_path):
    desk=Desk(tmp_path);facts=desk.store.quote(OWNER,desk.member,'bid').dump();own=desk.peer()['our_quote_json']
    queued=desk.queue();assert queued['state']=='QUEUED'
    cid=queued['command_id'];assert not desk.handoffs() and desk.peer()['send_allowed']==1
    assert desk.commands.claim(OWNER,cid)
    assert desk.commands.execution_permission(OWNER,cid)
    assert desk.receipt(cid,'SENT','native-notice')=='SENT'
    assert len(desk.handoffs())==1 and desk.peer()['send_allowed']==0
    assert desk.peer()['waiting_command_id'] is None and desk.peer()['response_state']!='WAITING'
    assert desk.peer()['our_quote_json']==own and desk.store.quote(OWNER,desk.member,'bid').dump()==facts
    group=desk.store.group_rows(OWNER)[0]
    assert group['state']=='OPEN' and not group['summary']['goal_ready']
    assert desk.peer(desk.other)['send_allowed']==1
    before=desk.peer()['control_version'];desk.receipt(cid,'SENT','native-notice')
    assert desk.peer()['control_version']==before and len(desk.handoffs())==1


@pytest.mark.parametrize('status',['UNKNOWN','FAILED','CANCELLED'])
def test_non_sent_notice_does_not_claim_confirmation_or_handoff(tmp_path,status):
    desk=Desk(tmp_path);cid=desk.queue()['command_id'];desk.commands.claim(OWNER,cid)
    assert desk.receipt(cid,status)==status
    assert not desk.handoffs() and desk.peer()['send_allowed']==1
    replay=desk.queue();assert replay['command_id']==cid and replay['replayed']
    if status=='UNKNOWN':
        assert desk.receipt(cid,'SENT','native-after-unknown')=='SENT'
        assert len(desk.handoffs())==1


def test_late_notice_sent_does_not_override_manual_resume(tmp_path):
    desk=Desk(tmp_path);cid=desk.queue()['command_id'];desk.commands.claim(OWNER,cid)
    desk.store.set_sending(OWNER,False,member_id=desk.member)
    desk.store.set_sending(OWNER,True,member_id=desk.member)
    control=desk.peer()['control_version']
    assert desk.receipt(cid,'SENT','native-late-notice')=='SENT'
    assert not desk.handoffs() and desk.peer()['send_allowed']==1 and desk.peer()['control_version']==control


def test_conflicting_failed_then_sent_notice_never_handoffs(tmp_path):
    desk=Desk(tmp_path);cid=desk.queue()['command_id'];desk.commands.claim(OWNER,cid)
    desk.receipt(cid,'FAILED')
    assert desk.receipt(cid,'SENT','native-conflicting-notice')=='UNKNOWN'
    assert not desk.handoffs()


@pytest.mark.parametrize('own_echo',[True,False])
def test_sent_receipt_moves_to_human_with_pending_echo_or_new_peer_message(tmp_path,own_echo):
    desk=Desk(tmp_path);cid=desk.queue()['command_id'];desk.commands.claim(OWNER,cid)
    body=json.loads(desk.commands.get(OWNER,cid)['body_json'])
    desk.now+=1
    desk.store.ingest(OWNER,desk.thread,event_id='pending-before-notice-receipt',source_seq=int(desk.now),
                      text=body['text'] if own_echo else '先等一下',trade_day=DAY,occurred_at=desk.now,
                      direction='out' if own_echo else 'in',native_id='native-echo-notice' if own_echo else None)
    assert desk.receipt(cid,'SENT','native-echo-notice')=='SENT'
    assert len(desk.handoffs())==1


@pytest.mark.parametrize('changed_price',[False,True])
def test_peer_response_analyzed_before_ack_is_left_for_human_without_accepting_new_economics(tmp_path,changed_price):
    desk=Desk(tmp_path);cid=desk.queue()['command_id'];desk.commands.claim(OWNER,cid)
    original_marker=json.loads(desk.commands.get(OWNER,cid)['body_json'])['handoff_notice']
    desk.inbound('SYN-A 改2.11收1000万' if changed_price else '好，谢谢',quote=changed_price,price='2.11')
    latest=desk.store.quote(OWNER,desk.member,'bid').dump()
    assert desk.receipt(cid,'SENT','native-notice-after-peer')=='SENT'
    assert len(desk.handoffs())==1 and desk.peer()['send_allowed']==0
    assert desk.store.quote(OWNER,desk.member,'bid').dump()==latest
    if changed_price:
        assert latest['revision']!=original_marker['quote_revision'] and latest['price']['value']=='2.11'
    assert desk.peer(desk.other)['send_allowed']==1


@pytest.mark.parametrize('control',['owner','group','policy','account','context','retired','day'])
def test_notice_ack_cannot_override_changed_authorization_identity_or_day(tmp_path,control):
    desk=Desk(tmp_path);cid=desk.queue()['command_id'];desk.commands.claim(OWNER,cid)
    if control in ('owner','group'):
        scope={'group_id':desk.group} if control=='group' else {}
        desk.store.set_sending(OWNER,False,**scope);desk.store.set_sending(OWNER,True,**scope)
    elif control=='policy':
        with desk.store.write(OWNER) as tx:tx.execute('UPDATE xj2_group SET policy_version=policy_version+1 WHERE group_id=?',(desk.group,))
    elif control=='account':
        with desk.store.write(OWNER) as tx:tx.execute("UPDATE xj2_binding SET account_uid='other-synthetic-account',version=version+1 WHERE owner_id=?",(OWNER,))
    elif control=='context':desk.store.save_preferences(OWNER,{'context_id':'synthetic-next-context','context_epoch':1})
    elif control=='retired':
        with desk.store.write(OWNER) as tx:desk.store.audit(tx,OWNER,'workspace_group_retired',desk.group,{})
    else:desk.commands.current_day=lambda:'2026-10-28'
    before=desk.peer()['control_version']
    assert desk.receipt(cid,'SENT','native-notice-late-control')=='SENT'
    assert not desk.handoffs() and desk.peer()['control_version']==before


def test_model_wire_notice_contract_preserves_semantic_choice_without_exposing_ids():
    from dify.negotiation_v22 import node_code
    from server.negotiation.chatflow import _validated_previous_output
    context={**pure_context(),'schema_version':'2.2','stage':'reply','context_policy_revision':'latest_quote_confirmation_v1'}
    result=node_code.decode_stage_result({'schema_version':'2.2','reply_plan':plan()},context,'reply')
    assert result['action']=='SEND' and result['proposal']['kind']=='HANDOFF_NOTICE'
    assert result['proposal']['render_plan']['acceptance']==ACCEPTANCE
    projected=node_code.compact_context(context)
    assert 'handoff_evidence' not in projected and 'message_id' not in projected['current_inbound_message']
    previous={'schema_version':'2.2','reply_plan':plan()}
    assert _validated_previous_output(previous)==previous


@pytest.mark.parametrize('changed_price',[False,True])
def test_adjacent_direction_uses_existing_text_only_repair_with_price_frozen(tmp_path,changed_price):
    desk=Desk(tmp_path);calls=[];facts=desk.store.quote(OWNER,desk.member,'bid').dump()
    class Flows:
        def decide(self,context):
            calls.append(deepcopy(context))
            value='2.11' if changed_price and len(calls)>1 else '2.12'
            proposal=render_reply_plan(context,{'kind':'REPLY','quote_value':value,'text':'我这边{own_quote}，您看。'})['proposal']
            if len(calls)==1:
                proposal['render_plan']['text']='我出{own_quote}，您看。'
                proposal['text']='我出2.12%出，您看。'
            else:
                assert context['validation_feedback']['mode']=='TEXT_ONLY'
                assert context['validation_feedback']['original_response']['proposal']['our_price']['value']=='2.12'
            return {'schema_version':'2.2','action':'SEND','proposal':proposal}
    desk.engine.client=Flows()
    result=desk.engine.decide_member(OWNER,desk.member)
    assert len(calls)==2 and result['repair']['attempted']
    assert result['state']==('REJECTED' if changed_price else 'QUEUED')
    assert desk.store.quote(OWNER,desk.member,'bid').dump()==facts
    assert not desk.handoffs()


def test_code_output_correction_cannot_change_frozen_price_for_direction_error():
    from dify.negotiation_v22 import node_code
    previous={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'2.12','text':'我出{own_quote}，您看。'}}
    context={**pure_context(),'schema_version':'2.2','stage':'reply','validation_feedback':{
        'mode':'OUTPUT_CONTRACT','attempt':1,'max_attempts':1,'reason_code':'DIFY_MODEL_CONTRACT_SLOTS','previous_output':previous}}
    fixed={'schema_version':'2.2','reply_plan':{'kind':'REPLY','quote_value':'2.12','text':'我这边{own_quote}，您看。'}}
    assert node_code.decode_stage_result(fixed,context,'reply')['proposal']['our_price']['value']=='2.12'
    fixed['reply_plan']['quote_value']='2.11'
    with pytest.raises(ValueError,match='frozen'):
        node_code.decode_stage_result(fixed,context,'reply')
