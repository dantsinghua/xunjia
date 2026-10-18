"""Check the multi-turn API collector without opening a network connection."""
from copy import deepcopy
import subprocess
import sys
import json

import pytest

from dify.negotiation_v22.node_code import decode_stage_result
from server.negotiation.slots import collect_candidates
from server.negotiation.chatflow import FlowUnavailable
from scripts import measure_dify_settlement as measure


class ScriptedFlow:
    omit_supplement=False

    def __init__(self, _base, _key, *, stage, timeout):
        self.stage=stage
        self.last_evidence={'scope':'synthetic scripted response, no HTTP'}

    def call(self, context):
        context={**deepcopy(context),'schema_version':'2.2','stage':self.stage}
        if self.stage=='reply':
            quote=context['quotes']['ofr']
            plan=({'kind':'CLARIFY','question':'ASK_SETTLEMENT'} if not quote['price']['computed_yield'] else
                  {'kind':'REPLY','quote_index':0,'include_volume':True,'question':'NEGOTIATE'})
            return decode_stage_result({'schema_version':'2.2','reply_plan':plan},context,'reply')
        member=context['members'][0]['member_id']
        incoming=[m for m in context['messages'] if m['direction']=='in'][-1]
        candidates=[c for c in collect_candidates(context) if c['message_id']==incoming['message_id']]
        def choose(roles):
            return next(c['id'] for c in candidates if set(c['roles']) & roles)
        observations=[]
        if '稍等' not in incoming['text']:
            cash=any(set(c['roles']) & {'NET_PRICE','DIRTY_PRICE'} for c in candidates)
            slots=({'price':choose({'NET_PRICE','DIRTY_PRICE'}),'volume_wan':choose({'QUANTITY'})} if cash else
                   {'settlement':choose({'DATE','SETTLEMENT'})})
            meaning=({'intent':'QUOTE','validity':'ACTIVE','negotiation_space':'FINAL','reason':'本人明确最终价'} if cash else
                     {'intent':'SETTLEMENT','reason':'本人明确补结算'})
            if cash or not self.omit_supplement:
                observations=[{'member_id':member,'source_ids':[incoming['message_id']],'side':'ofr',
                               'slots':slots,'meaning':meaning}]
        return decode_stage_result({'schema_version':'2.2',
            'scopes':{m['message_id']:[member] for m in context['messages']},'observations':observations},context,'analysis')


CONFIG={'source':{'base_url':'http://never-contact.invalid'},
        'apps':{'analysis':{'key':'synthetic-a'},'reply':{'key':'synthetic-b'}}}


@pytest.mark.parametrize('case',measure.SCENARIOS,ids=lambda case:case['id'])
def test_collector_exercises_actual_synthetic_sent_question_and_preserves_original_economics(tmp_path,monkeypatch,case):
    monkeypatch.setattr(measure,'ChatflowClient',ScriptedFlow)
    result=measure.run_case(case,CONFIG,tmp_path/case['id'])
    assert result['success'],result.get('error')
    assert result['synthetic_native_receipt'] and not result['external_im_sent']
    assert result['excluded_from_latency_calibration']
    assert result['sent_question']['state']=='SENT' and result['final_command']['state']=='QUEUED'
    assert len(result['http'])==(5 if case['wait'] else 4)
    assert all(event['transport_evidence']['scope']=='synthetic scripted response, no HTTP' for event in result['http'])
    assert result['after']['price']['settlement_date']==case['settlement']
    assert (tmp_path/case['id']/'fixture.sqlite').exists()


def test_collector_rejects_an_empty_supplement_instead_of_counting_a_completed_job_as_success(tmp_path,monkeypatch):
    class EmptySupplement(ScriptedFlow):
        omit_supplement=True
    monkeypatch.setattr(measure,'ChatflowClient',EmptySupplement)
    result=measure.run_case(measure.SCENARIOS[0],CONFIG,tmp_path/'missing')
    assert not result['success'] and result['error']['message']=='Supplement was not committed'
    assert not result['supplement_commit']['accepted'] and 'final_command' not in result


def test_failed_analysis_preserves_original_http_error_instead_of_secondary_missing_fields(tmp_path,monkeypatch):
    class FailedSupplement(ScriptedFlow):
        def call(self,context):
            if self.stage=='analysis' and context['members'][0].get('settlement_question'):
                raise FlowUnavailable('DIFY_HTTP_ERROR',400)
            return super().call(context)
    monkeypatch.setattr(measure,'ChatflowClient',FailedSupplement)
    result=measure.run_case(measure.SCENARIOS[0],CONFIG,tmp_path/'http-failure')
    assert not result['success'] and result['error']=={'type':'FlowUnavailable','message':'DIFY_HTTP_ERROR HTTP 400'}
    assert result['http'][-1]['error']==result['error'] and 'final_command' not in result
    assert result['sent_question']['state']=='SENT' and result['python_optimize']==0


def test_optimized_python_cannot_skip_acceptance_and_submit_requests():
    result=subprocess.run([sys.executable,'-O',measure.__file__,'--help'],capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and 'Measurement refuses Python optimization' in result.stderr


def test_reusing_output_is_rejected_before_reading_credentials_or_replacing_evidence(tmp_path,monkeypatch):
    output=tmp_path/'existing'
    output.mkdir()
    source=output/'source.json'
    source.write_text('retained-original-evidence')
    manifest=tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'frozen_root':str(measure.ROOT),'files':{}}))
    monkeypatch.setattr(sys,'argv',['measure','--credentials',str(tmp_path/'missing-credentials'),
        '--output',str(output),'--frozen-manifest',str(manifest)])
    with pytest.raises(FileExistsError,match='Refusing to replace existing measurement evidence'):
        measure.main()
    assert source.read_text()=='retained-original-evidence'
