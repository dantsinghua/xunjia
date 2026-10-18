"""Only a second model response may repair public text; economics stay frozen."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Event
from types import SimpleNamespace

import pytest

import server.negotiation.engine as engine_module
from server.negotiation.engine import Engine
from test_production_flow import OWNER, desk, raw
from test_acceptance_contract import current, observation, parser, setup_groups
from test_persistent_retry_http import latest_row


ERRORS = {
    'percent': 'Public yield text differs from validated quote',
    'direction': 'Reply direction differs from our authorized side',
}
FIXED_TEXT = 'SYN-A 3.19%收500万，烦请继续沟通。'


def reply_case(desk, *, final=False):
    app, _, flows, native, clock = desk
    peer = setup_groups(desk, stopped=False)['SYN-A']['peers'][0]
    price = '3.20' if final else '3.10'
    finality = 'FINAL' if final else 'NEGOTIABLE'
    flows.analyze = parser(lambda snapshot, message: [observation(peer, message, price=price,
        finality=finality, segment=message['text'], assertion='QUOTE')])
    raw(app, peer['thread_id'], 'SYN-A '+price+'出500万，'+('不能再改善了' if final else '还可以谈'))
    saved = app.state.negotiation_engine.analyze_thread(OWNER, peer['thread_id'])
    assert saved['facts_committed'] and not saved['rejected']
    return SimpleNamespace(app=app, flows=flows, peer=peer, native=native, clock=clock,
                           fact=current(app, peer).dump())


def response(failure='percent'):
    return {'schema_version': '2.2', 'action': 'SEND', 'proposal': {
        'kind': 'REPLY', 'text': 'SYN-A 3.19收500万' if failure=='percent' else 'SYN-A 3.19%收，出500万',
        'our_price': {'kind': 'YIELD', 'value': '3.19'}},
        'trader_view': {'reason': 'synthetic frozen decision'}, 'tool_requests': [], 'history_requests': []}


def install(case, *, failure='percent', transform=None, first_hook=None, repair_hook=None):
    original = response(failure)
    calls = []
    def decide(context):
        calls.append(deepcopy(context))
        assert len(calls) <= 2, 'Text repair may request only one extra final B response'
        if len(calls) == 1:
            assert not context.get('validation_feedback')
            if first_hook: first_hook(context, original)
            return deepcopy(original)
        feedback = context.get('validation_feedback')
        assert feedback and feedback['mode'] == 'TEXT_ONLY'
        assert feedback['attempt'] == feedback['max_attempts'] == 1
        assert feedback['reason'] == ERRORS[failure]
        assert feedback['original_response'] == original
        if repair_hook: repair_hook(context)
        repaired = deepcopy(original)
        repaired['proposal']['text'] = FIXED_TEXT
        return transform(repaired) if transform else repaired
    case.flows.decide = decide
    return calls, original


def run(case, engine=None):
    return (engine or case.app.state.negotiation_engine).decide_member(OWNER, case.peer['member_id'])


def replies(case):
    return [row for row in case.app.state.inquiry.commands(OWNER)
            if json.loads(row['body_json'])['kind'] == 'REPLY']


def unchanged_facts(case):
    assert current(case.app, case.peer).dump() == case.fact


def rejected(case, result):
    assert result['state'] == 'REJECTED' and result.get('command_id') is None, result
    assert not replies(case)
    unchanged_facts(case)


@pytest.mark.parametrize('failure', ['percent', 'direction'])
def test_model_repairs_only_text_and_exact_second_response_creates_one_command(desk, failure):
    case = reply_case(desk)
    calls, original = install(case, failure=failure)
    result = run(case)
    assert len(calls) == 2 and result['state'] == 'QUEUED', result
    assert len(replies(case)) == 1
    body = json.loads(replies(case)[0]['body_json'])
    assert body['text'] == FIXED_TEXT, 'Do not substitute a local template for the second model text'
    assert body['kind'] == original['proposal']['kind'] and body['our_price']['value'] == '3.19'
    assert run(case) is None and len(calls) == 2
    case.app.state.dispatcher.run_once()
    case.app.state.dispatcher.run_once()
    assert len(case.native.sent) == 2 and case.native.sent[-1]['text'] == FIXED_TEXT
    unchanged_facts(case)


@pytest.mark.parametrize('change', ['schema', 'action', 'kind', 'price', 'equivalent_price_spelling',
                                   'trader_view', 'remove_field', 'add_field'])
def test_nontext_response_fields_cannot_change_even_to_an_otherwise_legal_value(desk, change):
    case = reply_case(desk)
    def mutate(value):
        if change=='schema': value['schema_version']='3'
        elif change=='action': value['action']='HANDOFF'; value['proposal']=None
        elif change=='kind': value['proposal']['kind']='ASK_VOLUME'; value['proposal'].pop('our_price')
        elif change=='price': value['proposal']['our_price']['value']='3.18'; value['proposal']['text']='SYN-A 3.18%收500万'
        elif change=='equivalent_price_spelling': value['proposal']['our_price']['value']='3.190'
        elif change=='trader_view': value['trader_view']['reason']='synthetic rewritten private rationale'
        elif change=='remove_field': value.pop('trader_view')
        else: value['extra']='new-field'
        return value
    calls, _ = install(case, transform=mutate)
    result = run(case)
    assert len(calls) == 2
    rejected(case, result)
    peer = case.app.state.repository.group_rows(OWNER)[0]['peers'][0]
    assert peer['send_allowed'] == 1, 'A changed HANDOFF must not alter sending authority'


@pytest.mark.parametrize('text', ['SYN-A 3.18%收500万', 'SYN-A 3.19%出500万',
    'SYN-A 3.19%收600万', 'SYN-A 3.19%收500万，成交', 'SYN-A 内部底价3.19%收500万'])
def test_second_text_must_pass_complete_price_direction_quantity_and_public_policy_guards(desk, text):
    case = reply_case(desk)
    def mutate(value): value['proposal']['text']=text; return value
    calls, _ = install(case, transform=mutate)
    result = run(case)
    assert len(calls) == 2
    rejected(case, result)


@pytest.mark.parametrize('kind', ['tool', 'history', 'timeout'])
def test_repair_cannot_open_another_tool_history_or_retry_round(desk, monkeypatch, kind):
    case = reply_case(desk)
    invoked = []
    invoke = case.app.state.bond_tools.invoke
    def counted(*args): invoked.append(args); return invoke(*args)
    monkeypatch.setattr(case.app.state.bond_tools, 'invoke', counted)
    def transform(value):
        if kind=='timeout': raise TimeoutError('synthetic repair HTTP timeout')
        if kind=='tool': value['tool_requests']=[{'name':'bond_info','arguments':{'schema_version':'1','mode':'get','bond_id':'SYN-A'}}]
        else:
            anchor=next(m for m in case.app.state.repository.context(OWNER,case.peer['member_id']) if m['direction']=='out')
            value['history_requests']=[{'member_id':case.peer['member_id'],'message_ids':[anchor['message_id']]}]
        return value
    calls, _ = install(case, transform=transform)
    result = run(case)
    assert len(calls) == 2 and not invoked
    rejected(case, result)


@pytest.mark.parametrize('failure', ['limit', 'step', 'final', 'quantity'])
def test_economic_or_other_public_rejection_never_becomes_text_repair(desk, failure):
    case = reply_case(desk, final=failure=='final')
    calls = []
    def decide(context):
        calls.append(context)
        value=response()
        if failure=='limit': value['proposal']['our_price']['value']='3.11'
        elif failure=='step': value['proposal']['our_price']['value']='3.12'
        elif failure=='quantity': value['proposal']['text']='SYN-A 3.19%收600万'
        return value
    case.flows.decide=decide
    result=run(case)
    assert len(calls)==1
    rejected(case,result)


def configured(case, monkeypatch, seconds=30):
    monkeypatch.setattr(engine_module,'time',SimpleNamespace(monotonic=lambda:case.clock.value))
    old=case.app.state.negotiation_engine
    return Engine(case.app.state.inquiry,case.flows,case.app.state.bond_tools,
                  aggregation=old.aggregation,stage_budget_seconds=seconds)


def test_text_repair_uses_remaining_stage_budget_and_original_running_lease(desk, monkeypatch):
    case=reply_case(desk); engine=configured(case,monkeypatch)
    lease=[]
    def first_hook(context, original):
        lease.append(latest_row(case.app.state.repository,'xj2_reply_job')['lease_until'])
        case.clock.value+=20
    def repair_hook(context):
        assert 0<context['_stage_budget_seconds']<=10
        assert latest_row(case.app.state.repository,'xj2_reply_job')['lease_until']==lease[0]
    calls,_=install(case,first_hook=first_hook,repair_hook=repair_hook)
    result=run(case,engine)
    assert len(calls)==2 and result['state']=='QUEUED'
    unchanged_facts(case)


@pytest.mark.parametrize('late_at',['initial','repair'])
def test_late_model_text_never_creates_command(desk, monkeypatch, late_at):
    case=reply_case(desk); engine=configured(case,monkeypatch)
    def first_hook(context, original):
        if late_at=='initial':case.clock.value+=31
    def repair_hook(context):case.clock.value+=31
    calls,_=install(case,first_hook=first_hook,repair_hook=repair_hook)
    result=run(case,engine)
    assert len(calls)==(1 if late_at=='initial' else 2)
    assert result['state']==('RETRY' if late_at=='initial' else 'REJECTED')
    assert not replies(case)
    unchanged_facts(case)


@pytest.mark.parametrize('event',['inbound','stop'])
def test_event_while_text_repair_waits_is_not_locked_and_prevents_old_candidate(desk,event):
    case=reply_case(desk)
    entered,release=Event(),Event()
    def repair_hook(context):entered.set();assert release.wait(5)
    calls,_=install(case,repair_hook=repair_hook)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(run,case)
        try:
            assert entered.wait(3)
            if event=='inbound':
                newer=pool.submit(raw,case.app,case.peer['thread_id'],'SYN-A 新价3.11出600万',2).result(timeout=3)
                assert newer not in {m['message_id'] for m in calls[-1]['context']}
            else:
                pool.submit(case.app.state.repository.set_sending,OWNER,False).result(timeout=3)
        finally:release.set()
        result=future.result(timeout=4)
    assert len(calls)==2
    rejected(case,result)
    case.app.state.dispatcher.run_once()
    assert len(case.native.sent)==1


def test_new_reply_worker_fences_old_repair_without_overwriting_its_job(desk):
    case=reply_case(desk)
    replacement={}
    def repair_hook(context):
        old=latest_row(case.app.state.repository,'xj2_reply_job')
        case.clock.value=old['lease_until']+.01
        flows=SimpleNamespace(analyze=case.flows.analyze,
            decide=lambda context:{'schema_version':'2.2','action':'WAIT','proposal':None})
        new=Engine(case.app.state.inquiry,flows,case.app.state.bond_tools)
        assert new.decide_member(OWNER,case.peer['member_id'])['state']=='WAIT'
        replacement.update(latest_row(case.app.state.repository,'xj2_reply_job'))
        assert replacement['job_id']==old['job_id'] and replacement['lease_token']!=old['lease_token']
    calls,_=install(case,repair_hook=repair_hook)
    result=run(case)
    assert len(calls)==2 and result['state']=='STALE_LEASE'
    after=latest_row(case.app.state.repository,'xj2_reply_job')
    assert after['lease_token']==replacement['lease_token'] and after['result_json']==replacement['result_json']
    assert after['state']=='DONE' and after['attempts']==2 and not replies(case)
    unchanged_facts(case)


@pytest.mark.parametrize('succeeds',[True,False])
def test_reply_repair_audit_and_persisted_result_keep_original_failure_and_spent_time(desk,monkeypatch,succeeds):
    case=reply_case(desk);engine=configured(case,monkeypatch)
    def first_hook(context,original):case.clock.value+=1
    def repair_hook(context):case.clock.value+=2
    def transform(value):
        if not succeeds:value['trader_view']['reason']='forbidden change'
        return value
    calls,original=install(case,first_hook=first_hook,repair_hook=repair_hook,transform=transform)
    result=run(case,engine)
    assert len(calls)==2
    audit=result['repair']
    assert audit['mode']=='TEXT_ONLY' and audit['claim_attempt']==1
    assert audit['attempted'] is True and audit['succeeded'] is succeeds and audit['fallback'] is (not succeeds)
    assert audit['initial_errors']==[{'reason':ERRORS['percent']}]
    assert audit['initial_response']==original and audit['repair_response'] is not None
    assert audit['initial_elapsed_ms']>=1000 and audit['repair_elapsed_ms']>=2000
    if not succeeds:assert audit['repair_error']
    row=latest_row(case.app.state.repository,'xj2_reply_job')
    assert json.loads(row['result_json'])['repair']==audit
    with case.app.state.repository.db.transaction(write=False) as tx:
        records=tx.execute("SELECT data_json FROM xj2_audit WHERE kind='reply_text_repair' AND entity_id=?",(row['job_id'],)).fetchall()
    assert len(records)==1 and json.loads(records[0]['data_json'])==audit
    unchanged_facts(case)


def test_completed_tool_results_are_preserved_when_text_repair_starts(desk,monkeypatch):
    case=reply_case(desk)
    calls,invoked=[],[]
    invoke=case.app.state.bond_tools.invoke
    def counted(*args):invoked.append(args);return invoke(*args)
    monkeypatch.setattr(case.app.state.bond_tools,'invoke',counted)
    def decide(context):
        calls.append(deepcopy(context));assert len(calls)<=3
        if len(calls)==1:
            return {'schema_version':'2.2','tool_requests':[{'name':'bond_info','arguments':{
                'schema_version':'1','mode':'search','query':'synthetic-'+str(i)}} for i in range(6)]}
        assert len(context['tool_results'])==6
        value=response()
        if len(calls)==3:
            assert context['validation_feedback']['mode']=='TEXT_ONLY'
            assert context['tool_results']==calls[1]['tool_results']
            value['proposal']['text']=FIXED_TEXT
        return value
    case.flows.decide=decide
    result=run(case)
    assert len(calls)==3 and len(invoked)==6 and result['state']=='QUEUED'
    unchanged_facts(case)


@pytest.mark.parametrize('action',['SEND','WAIT'])
def test_reply_without_repair_still_persists_explicit_clean_audit(desk,action):
    case=reply_case(desk);calls=[]
    def decide(context):
        calls.append(context)
        if action=='WAIT':return {'schema_version':'2.2','action':'WAIT','proposal':None}
        value=response();value['proposal']['text']=FIXED_TEXT;return value
    case.flows.decide=decide
    result=run(case)
    assert len(calls)==1 and result['state']==('QUEUED' if action=='SEND' else 'WAIT')
    audit=result['repair']
    assert audit['mode']=='TEXT_ONLY' and audit['attempted'] is False
    assert audit['initial_errors']==[] and audit['succeeded'] is False and audit['fallback'] is False
    row=latest_row(case.app.state.repository,'xj2_reply_job')
    assert json.loads(row['result_json'])['repair']==audit
    unchanged_facts(case)
