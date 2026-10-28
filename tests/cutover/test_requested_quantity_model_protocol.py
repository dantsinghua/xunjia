"""Actual production B guidance distinguishes a current request from an old firm lot."""
from copy import deepcopy
import json
import re

import pytest

from server.negotiation.engine import Engine
from test_reference_window_policy import OWNER
from test_text_repair_quantity_freeze import source_case
from test_requested_quantity_offer import offer
from test_reply_output_repair_projection import generated


def request700_case(path,text='你能3.10收700万吗'):
    case,_context=source_case(path,same_amount=True)
    source=case.inbound(text)
    changes={'price':{'kind':'YIELD','value':'3.10'},'volume_wan':'700','assertion':'QUESTION',
        'validity':'ACTIVE','finality':'UNKNOWN','confirmed_day':None}
    saved=case.analyze([{'member_id':case.member,'side':'bid','changes':changes,
        'evidence':{key:[source] for key in changes}}])
    assert saved['accepted'] and not saved['rejected']
    context=case.reply_context();context.update(schema_version='2.2',stage='reply')
    assert context['context_policy_revision']=='latest_quote_confirmation_v1'
    assert context['quotes']['ofr']['volume_wan']=='500' and context['quotes']['ofr']['assertion']=='QUOTE'
    assert context['quotes']['bid']['volume_wan']=='700' and context['quotes']['bid']['confirmed_day'] is None
    return case,context


def mode_context(context,mode):
    context=deepcopy(context)
    if mode=='OUTPUT_CONTRACT':
        context['validation_feedback']={'mode':mode,'attempt':1,'max_attempts':1,
            'reason_code':'DIFY_MODEL_CONTRACT_SLOTS','previous_output':{
                'schema_version':'2.2','reply_plan':offer('按您询问的700万，这边{own_quote}，请再看看？')}}
    return context


def declares_binding(system,token,amount):
    # Source-token/value anchors, not a fixed public reply or strategy sentence.
    for clause in re.split(r'[，,；;。\n]',system):
        macros=re.findall(r'\{(?:requested_volume|peer_volume)\}',clause)
        if macros==[token] and re.search(r'(?<![\d.])'+re.escape(amount)+r'(?:\.0+)?(?:万)?(?![\d.])',clause):
            if not any(word in clause for word in ('不可用','禁用','不可引用','不得引用')):return True
    return False


@pytest.mark.parametrize('mode',['ordinary','OUTPUT_CONTRACT'])
def test_actual_production_context_guidance_maps_current700_request_and_old500_firm_to_their_own_slots(tmp_path,mode):
    case,base=request700_case(tmp_path/'requested700.sqlite')
    context=mode_context(base,mode);before=deepcopy(context)
    for version in ['0.15.3','1.17.1']:
        project,decode=generated(version);output=project(json.dumps(context));system=output['system_prompt']
        assert not re.search(r'提量(?:只用|只能用|仅用)\{peer_volume\}',system), 'An eligible requested lot cannot be governed by a peer-only instruction'
        assert declares_binding(system,'{requested_volume}','700'), 'Bind the eligible current requested700 explicitly, rather than the old peer500 or private target'
        assert declares_binding(system,'{peer_volume}','500'), 'Keep the independently verified old firm500 separate from the requested700'
        good={'schema_version':'2.2','reply_plan':offer('按您询问的{requested_volume}，这边{own_quote}，请再看看？')}
        decoded=json.loads(decode(json.dumps(good),json.dumps(context))['result'])
        body=Engine.__new__(Engine)._validate_reply(context,decoded['proposal'])
        assert body['our_price']['value']=='3.15' and '700万' in body['text'] and '500万' not in body['text']
        assert context==before and case.reply_context()['quotes']==base['quotes']



def test_both_production_modes_keep_source_guards_no_bare_numbers_and_no_quantity_option(tmp_path):
    case,base=request700_case(tmp_path/'binding-and-guards.sqlite')
    for mode in ['ordinary','OUTPUT_CONTRACT']:
        context=mode_context(base,mode);before=deepcopy(context)
        for version in ['0.15.3','1.17.1']:
            project,decode=generated(version);output=project(json.dumps(context))
            system=output['system_prompt']
            assert any(word in system for word in ('可不提量','不提量','省略数量'))
            assert any(word in system for word in ('裸数字','裸数','不抄价格','不复制数字'))
            assert any('requested_volume' in line and any(word in line for word in ('不确认','不把','不是'))
                for line in system.splitlines()), 'A requested lot must not be presented as a confirmed peer quote'
            for text,amount in [('按您询问的{requested_volume}，这边{own_quote}，请再看看？','700万'),
                    ('您报{peer_volume}，这边{own_quote}，请再看看？','500万'),('这边{own_quote}，请再看看？',None)]:
                answer=json.loads(decode(json.dumps({'schema_version':'2.2','reply_plan':offer(text)}),json.dumps(context))['result'])
                body=Engine.__new__(Engine)._validate_reply(context,answer['proposal'])
                assert body['our_price']['value']=='3.15'
                if amount is not None:assert amount in body['text']
                else:assert '万' not in body['text']
            with pytest.raises(ValueError):
                decode(json.dumps({'schema_version':'2.2','reply_plan':offer('按您询问的700万，这边{own_quote}。')}),json.dumps(context))
            if mode=='OUTPUT_CONTRACT':
                expected=deepcopy(context['validation_feedback']);del expected['previous_output']['reply_plan']['text']
                assert json.loads(output['context'])['validation_feedback']==expected
            assert context==before and case.reply_context()['quotes']==base['quotes']


def test_ineligible_request_sources_do_not_gain_dynamic_requested700_authority(tmp_path):
    _case,base=request700_case(tmp_path/'ineligible.sqlite')
    _wrong_case,wrong_actor=request700_case(tmp_path/'wrong-actor.sqlite','我能3.10收700万吗')
    candidates=[wrong_actor]
    for fault in ['missing_evidence','old_day','reported']:
        context=deepcopy(base)
        if fault=='missing_evidence':context['quotes']['bid']['evidence']['volume_wan']=[]
        elif fault=='old_day':context['context'][-1]['trade_day']='2026-10-16'
        else:context['quotes']['bid']['assertion']='REPORTED'
        candidates.append(context)
    for base_context in candidates:
        for mode in ['ordinary','OUTPUT_CONTRACT']:
            context=mode_context(base_context,mode);before=deepcopy(context)
            for version in ['0.15.3','1.17.1']:
                project,decode=generated(version);system=project(json.dumps(context))['system_prompt']
                assert not declares_binding(system,'{requested_volume}','700')
                with pytest.raises(ValueError):
                    decode(json.dumps({'schema_version':'2.2','reply_plan':offer('按您询问的{requested_volume}，这边{own_quote}。')}),json.dumps(context))
                assert context==before
