"""Lossless model wire decoding; original validators and raw facts remain authoritative."""
from copy import deepcopy
import json

import pytest

from dify.negotiation_v22 import node_code
from dify.negotiation_v22.build import build


def context():
    return {'schema_version':'2.2','stage':'analysis',
            'messages':[{'message_id':'current','direction':'in','text':'SYN-A 按你刚才的价，出0.5亿，不能再改善'}],
            'members':[{'member_id':'p1','context':[{'message_id':'old','direction':'out','text':'SYN-A 2.65%收'}]}]}


def original():
    return {'schema_version':'2.2','scopes':{'current':['p1']},'unresolved':{},
            'observations':[{'member_id':'p1','side':'ofr',
                'changes':{'price':{'kind':'YIELD','value':'2.65','notation':'ABSOLUTE'},'volume_wan':'0.5亿','finality':'FINAL'},
                'evidence':{'price':['current'],'volume_wan':['current'],'finality':['current']},
                'segments':[{'message_id':'current','text':'SYN-A 按你刚才的价，出0.5亿，不能再改善'}],
                'interpretation':{'quote_owner':'COUNTERPARTY','intent':'QUOTE','negotiation_space':'FINAL','reason':'当前引用并明确无改善空间'}}],
            'tool_requests':[],'history_requests':[]}


def paired():
    result=original();obs=result['observations'][0]
    changes,evidence=obs.pop('changes'),obs.pop('evidence')
    obs['fields']=[{'field':key,'value':value,'evidence':evidence[key]} for key,value in changes.items()]
    return result


@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_generated_code_preserves_each_paired_value_evidence_and_original_order(version):
    document=build('analysis',version=version,provider='synthetic',model='synthetic')
    code=next(n['data']['code'] for n in document['workflow']['graph']['nodes'] if n['id']=='contract')
    scope={};exec(code,scope)
    value=paired();before=deepcopy(value)
    decoded=json.loads(scope['main'](json.dumps(value),json.dumps(context()))['result'])
    assert value==before
    assert decoded==original()
    assert decoded['observations'][0]['changes']['volume_wan']=='0.5亿'


def test_fields_collision_rejects_only_bad_slot_and_does_not_fill_canonical_missing_evidence():
    value=paired();good=deepcopy(value['observations'][0]);bad=deepcopy(good)
    bad['changes']={'finality':'FINAL'}
    value['observations']=[bad,good]
    decoded=node_code.decode_stage_result(value,context(),'analysis')
    assert len(decoded['observations'])==2 and decoded['observations'][0]['validation_error']
    assert decoded['observations'][1]==original()['observations'][0]
    canonical=original();canonical['observations'][0]['evidence'].pop('finality')
    assert node_code.decode_stage_result(canonical,context(),'analysis')==node_code.validate_result(canonical,context(),'analysis')
    assert node_code.decode_stage_result(canonical,context(),'analysis')['observations'][0]['validation_error']


def test_evidence_patch_only_adds_explicit_supplied_source_and_preserves_other_rejection_slot():
    ctx=context();base=original();sentinel={'validation_error':'Unknown field evidence'}
    base['observations'].append(sentinel)
    ctx['validation_feedback']={'mode':'EVIDENCE_ONLY','attempt':1,'max_attempts':1,
        'targets':[{'index':0,'member_id':'p1','side':'ofr','reason':'Price is not supported by its classified raw evidence'}],
        'original_response':base}
    before=deepcopy(ctx)
    patch={'schema_version':'2.2','evidence_repairs':[{'index':0,'add_evidence':{'price':['old']},
              'add_segments':[{'message_id':'old','text':'SYN-A 2.65%收'}]}]}
    decoded=node_code.decode_stage_result(patch,ctx,'analysis')
    assert ctx==before
    assert decoded['observations'][0]['evidence']['price']==['current','old']
    assert decoded['observations'][0]['segments']==base['observations'][0]['segments']+patch['evidence_repairs'][0]['add_segments']
    assert decoded['observations'][0]['changes']==base['observations'][0]['changes']
    assert decoded['observations'][0]['interpretation']==base['observations'][0]['interpretation']
    assert decoded['observations'][1]==sentinel


def test_text_patch_copies_original_nontext_fields_and_cannot_smuggle_full_action():
    base={'schema_version':'2.2','action':'SEND','proposal':{'kind':'REPLY','text':'SYN-A 2.65出',
          'our_price':{'kind':'YIELD','value':'2.65'}},'trader_view':{'reason':'原始判断不改'}}
    ctx={'schema_version':'2.2','stage':'reply','member_id':'p1',
         'validation_feedback':{'mode':'TEXT_ONLY','attempt':1,'max_attempts':1,
              'reason':'Reply direction differs from our authorized side','original_response':base}}
    patch={'schema_version':'2.2','text_repair':{'text':'SYN-A 2.65%收'}}
    decoded=node_code.decode_stage_result(patch,ctx,'reply')
    expected=deepcopy(base);expected['proposal']['text']='SYN-A 2.65%收'
    assert decoded==expected and base['proposal']['text']=='SYN-A 2.65出'
    with pytest.raises(ValueError):node_code.decode_stage_result({**patch,'action':'WAIT'},ctx,'reply')
    with pytest.raises(ValueError):node_code.decode_stage_result(patch,{'schema_version':'2.2','stage':'reply'},'reply')


def test_actual_v15_negotiation_space_fact_is_rejected_without_remapping_or_dropping():
    from pathlib import Path
    fixture=json.loads((Path(__file__).parent/'fixtures/dify_v15_negotiation_space_field.json').read_text())
    raw=fixture['result'];before=deepcopy(raw)
    assert any(field['field']=='negotiation_space' for field in raw['observations'][0]['fields'])
    result=node_code.decode_stage_result(raw,fixture['context'],'analysis')
    assert raw==before
    assert result['observations']==[{'validation_error':'Paired field name is unknown or repeated'}]
    assert result['scopes']==raw['scopes']


@pytest.mark.parametrize('index',[0,1])
def test_actual_v16_cash_clarification_still_rejects_price_echo_and_accepts_the_plain_question(index):
    from pathlib import Path
    from server.negotiation.engine import Engine
    from server.negotiation.prices import InvalidFact
    fixture=json.loads((Path(__file__).parent/'fixtures/dify_v16_cash_clarify.json').read_text())[index]
    proposal=fixture['result']['proposal'];before=deepcopy(proposal)
    assert proposal['kind']=='CLARIFY'
    with pytest.raises(InvalidFact,match='Nonquote reply contains an unvalidated public price'):
        Engine.__new__(Engine)._validate_reply(fixture['context'],proposal)
    assert proposal==before
    clean=deepcopy(proposal)
    clean['text']='请确认SYN-A结算按T+0、T+1还是具体日期，并提供必要换算依据？'
    assert Engine.__new__(Engine)._validate_reply(fixture['context'],clean)==clean


@pytest.mark.parametrize('index',[0,1,2])
def test_actual_v19_computed_bp_price_and_replayed_history_remain_rejected(index):
    from pathlib import Path
    from server.negotiation.prices import InvalidFact
    from server.negotiation.validation import validate_observation
    fixture=json.loads((Path(__file__).parent/'fixtures/dify_v19_price_notation_and_history.json').read_text())[index]
    ctx,raw=fixture['context'],fixture['result'];before=deepcopy(raw)
    decoded=node_code.decode_stage_result(raw,ctx,'analysis')
    assert raw==before
    if fixture['case_id']=='carry-suffix':
        assert decoded['observations'][0]=={'validation_error':'Field requires current inbound evidence'}
        assert decoded['observations'][1]['changes']['price']=={'kind':'YIELD','value':'04','notation':'YIELD_SUFFIX'}
    else:
        row=decoded['observations'][0]
        price=row['changes']['price']
        assert price['notation']=='ABSOLUTE'
        assert price['value']==('2.53' if index==0 else '3.0372')
        aliases={member['bond_code'].casefold():{member['member_id']} for member in ctx['members']}
        with pytest.raises(InvalidFact,match='Price is not supported by its classified raw evidence'):
            validate_observation(ctx,row,aliases,raw_price=price)


@pytest.mark.parametrize('expression,expected',[
    ({'basis':'收益率','value':'2.6500'}, {'kind':'YIELD','value':'2.6500','notation':'ABSOLUTE'}),
    ({'basis':'净价','value':'99.32'}, {'kind':'NET_PRICE','value':'99.32','notation':'ABSOLUTE'}),
    ({'basis':'全价','value':'99.32','settlement_date':'2026-10-17'},
     {'kind':'DIRTY_PRICE','value':'99.32','notation':'ABSOLUTE','settlement_date':'2026-10-17'}),
    ({'basis':'收益率尾数','value':'04'}, {'kind':'YIELD','value':'04','notation':'YIELD_SUFFIX'}),
    ({'basis':'中债估值','operation':'加','bp':'05.00'}, {'kind':'YIELD','value':'05.00','notation':'CHINABOND_BP'}),
    ({'basis':'中债估值','operation':'减','bp':'5.00'}, {'kind':'YIELD','value':'-5.00','notation':'CHINABOND_BP'}),
])
def test_explicit_price_expression_is_only_a_lossless_representation_map(expression,expected):
    raw=paired();raw['observations'][0]['fields'][0]['value']=expression
    before=deepcopy(raw);decoded=node_code.decode_stage_result(raw,context(),'analysis')
    canonical=original();canonical['observations'][0]['changes']['price']=expected
    assert raw==before
    assert decoded==canonical


@pytest.mark.parametrize('information,negotiation',[
    ('发行主体已核为 Synthetic Issuer。','SYN-A 2.65%收。'),
    ('','SYN-A 2.65%收。'),
    ('请确认券归属。',''),
])
def test_reply_parts_preserve_exact_strings_and_all_nontext_fields(information,negotiation):
    kind='REPLY' if negotiation else 'CLARIFY'
    proposal={'kind':kind,'information_text':information,'negotiation_text':negotiation}
    if kind=='REPLY':proposal['our_price']={'kind':'YIELD','value':'2.65'}
    raw={'schema_version':'2.2','action':'SEND','proposal':proposal,'trader_view':{'source':'actual-tool'},
         'tool_requests':[],'history_requests':[]}
    ctx={'schema_version':'2.2','stage':'reply','member_id':'p1'}
    before=deepcopy(raw);decoded=node_code.decode_stage_result(raw,ctx,'reply')
    expected=deepcopy(raw);expected['proposal'].pop('information_text');expected['proposal'].pop('negotiation_text')
    expected['proposal']['text']='\n'.join(text for text in (information,negotiation) if text)
    assert raw==before and decoded==expected


@pytest.mark.parametrize('index',[0,1])
def test_original_v20_typed_errors_are_not_reinterpreted_from_their_correct_reason(index):
    from pathlib import Path
    from server.negotiation.prices import InvalidFact
    from server.negotiation.validation import validate_observation
    fixture=json.loads((Path(__file__).parent/'fixtures/dify_v20_typed_price_errors.json').read_text())[index]
    ctx,raw=fixture['context'],fixture['result'];before=deepcopy(raw)
    decoded=node_code.decode_stage_result(raw,ctx,'analysis')
    assert raw==before
    row=decoded['observations'][0];price=row['changes']['price']
    if index==0:
        assert price=={'kind':'NET_PRICE','value':'99.32','notation':'ABSOLUTE'}
        reason='Price kind contradicts the explicit cash basis'
    else:
        assert price=={'kind':'YIELD','value':'5','notation':'CHINABOND_BP'}
        reason='Price is not supported by its classified raw evidence'
    aliases={member['bond_code'].casefold():{member['member_id']} for member in ctx['members']}
    with pytest.raises(InvalidFact,match=reason):
        validate_observation(ctx,row,aliases,raw_price=price)
