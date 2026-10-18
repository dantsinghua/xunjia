from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import node_code
from dify.negotiation_v22.build import build
from server.negotiation.slots import collect_candidates


def sample(text='SYN-A 20 出 2000'):
    context = {'schema_version':'2.2','stage':'analysis',
               'messages':[{'message_id':'in','direction':'in','ingest_revision':1,'text':text,'trade_day':'2026-10-18'}],
               'members':[{'member_id':'a','bond_code':'SYN-A','context':[]}]}
    candidates = collect_candidates(context)
    def ref(token, role):
        return {'candidate_id':next(s['id'] for s in candidates if s['token']==token), 'role':role}
    result = {'schema_version':'2.2','scopes':{'in':['a']},'unresolved':{},'observations':[
        {'member_id':'a','side':'ofr','source_ids':['in'],
         'slots':{'price':ref('20','YIELD_SUFFIX'),'volume_wan':ref('2000','QUANTITY')},
         'status':{'assertion':'QUOTE','validity':'ACTIVE','finality':'NEGOTIABLE'},
         'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUOTE','negotiation_space':'NEGOTIABLE','reason':'本对手报出价量'}}]}
    return context,result


@pytest.mark.parametrize('version', ['0.15.3','1.17.1'])
def test_actual_generated_code_assembles_literals_and_evidence(version):
    ctx, result = sample(); before=deepcopy((ctx,result))
    graph = build('analysis', version=version, provider='synthetic', model='synthetic')['workflow']['graph']
    nodes={n['id']:n['data'] for n in graph['nodes']}
    namespace={};exec(nodes['context']['code'],namespace)
    projected=json.loads(namespace['main'](json.dumps(ctx))['context'])
    assert projected['slot_candidates']
    assert projected['messages'][0]['text']==ctx['messages'][0]['text']
    namespace={};exec(nodes['contract']['code'],namespace)
    decoded=json.loads(namespace['main'](json.dumps(result),json.dumps(ctx))['result'])
    row=decoded['observations'][0]
    assert row['changes']['price']=={'kind':'YIELD','notation':'YIELD_SUFFIX','value':'20'}
    assert row['changes']['volume_wan']=='2000'
    assert row['evidence']['price']==['in']
    assert row['changes']['confirmed_day']=='2026-10-18'
    assert row['segments']==[{'message_id':'in','text':ctx['messages'][0]['text']}]
    assert (ctx,result)==before


@pytest.mark.parametrize('change', ['unknown','wrong_type','raw_number','other_member','no_current','mixed'])
def test_slot_selection_cannot_forge_or_retype_economic_values(change):
    ctx,result=sample();row=result['observations'][0]
    if change=='unknown':row['slots']['price']['candidate_id']='not-loaded'
    elif change=='wrong_type':row['slots']['price']['role']='ABSOLUTE'
    elif change=='raw_number':row['slots']['price']['value']='99.9'
    elif change=='other_member':row['member_id']='other'
    elif change=='no_current':row['source_ids']=[]
    else:row['changes']={'price':{'kind':'YIELD','value':'3.12'}}
    decoded=node_code.decode_stage_result(result,ctx,'analysis')['observations'][0]
    assert decoded.get('validation_error')
    assert decoded['member_id']==row['member_id'] and decoded['side']=='ofr'
    assert decoded['validation_path'].startswith('observations[0]')


def test_quantity_or_state_alone_cannot_reconfirm_a_price_for_a_new_day():
    ctx,result=sample()
    result['observations'][0]['slots'].pop('price')
    decoded=node_code.decode_stage_result(result,ctx,'analysis')['observations'][0]
    assert decoded['changes']['volume_wan']=='2000'
    assert 'confirmed_day' not in decoded['changes']


@pytest.mark.parametrize('space', ['UNKNOWN','NEGOTIABLE','FINAL'])
def test_meaning_states_finality_once_without_a_second_conflicting_copy(space):
    ctx,result=sample();row=result['observations'][0]
    row.pop('status');row.pop('interpretation')
    row['meaning']={'intent':'QUOTE','validity':'ACTIVE','negotiation_space':space,'reason':'本轮本人明确报价'}
    row.pop('member_id');row.pop('source_ids')
    row['slots']={field:value['candidate_id'] for field,value in row['slots'].items()}
    decoded=node_code.decode_stage_result(result,ctx,'analysis')['observations'][0]
    assert decoded['member_id']=='a'
    assert decoded['changes']['finality']==space
    assert decoded['interpretation']['negotiation_space']==space
    assert decoded['changes']['price']['notation']=='YIELD_SUFFIX'
    assert decoded['evidence']['price']==['in']


def test_compact_meaning_cannot_choose_between_multiple_members_or_current_sources():
    ctx,result=sample();row=result['observations'][0]
    row.pop('status');row.pop('interpretation');row.pop('member_id')
    row['meaning']={'intent':'QUOTE','validity':'ACTIVE','negotiation_space':'UNKNOWN','reason':'本人报盘'}
    ctx['members'].append({'member_id':'b','bond_code':'SYN-B','context':[]})
    assert node_code.decode_stage_result(result,ctx,'analysis')['observations'][0].get('validation_error')


def test_meaning_cannot_silently_override_the_legacy_duplicate_state():
    ctx,result=sample();row=result['observations'][0]
    row['meaning']={'intent':'QUOTE','validity':'ACTIVE','negotiation_space':'FINAL','reason':'最终盘'}
    assert node_code.decode_stage_result(result,ctx,'analysis')['observations'][0].get('validation_error')
