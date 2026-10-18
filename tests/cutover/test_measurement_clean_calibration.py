"""Calibration uses clean end-to-end cases; final outcome statistics keep all cases.

Every trace here is explicitly synthetic. The real collector's report/profile
gate executes without credentials, model calls, native sends or changed gates.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from dify.negotiation_v22.measurement import summarize
from scripts import measure_dify_v22 as runner


def clean(index, *, elapsed=1000, path='common'):
    identity = 'synthetic-clean-'+str(index)
    return {'case_id': identity, 'path': path, 'success': True, 'semantic_ok': True,
        'elapsed_ms': elapsed, 'timeout': False, 'timeout_or_network_unknown': False,
        'analysis_repair': {'mode': 'EVIDENCE_ONLY', 'claim_attempt': 1, 'attempted': False,
            'succeeded': False, 'fallback': False, 'targets': [], 'initial_errors': [],
            'initial_response': {'schema_version': '2.2', 'observations': []},
            'repair_response': None, 'repair_error': None, 'initial_elapsed_ms': elapsed*.4,
            'repair_elapsed_ms': 0},
        'reply_repair': {'mode': 'TEXT_ONLY', 'claim_attempt': 1, 'attempted': False,
            'succeeded': False, 'fallback': False, 'initial_errors': [], 'initial_response': {
                'schema_version': '2.2', 'action': 'WAIT', 'proposal': None},
            'repair_response': None, 'repair_error': None, 'initial_elapsed_ms': elapsed*.6,
            'repair_elapsed_ms': 0},
        'analysis_inputs': [{'thread_id': 'synthetic-thread'}],
        'reply_inputs': [{'member_id': 'synthetic-member'}],
        'evidence': [{'stage': stage, 'round': 1, 'round_kind': 'initial',
            'elapsed_ms': elapsed*fraction, 'message_id': identity+'-'+stage}
            for stage, fraction in [('analysis', .4), ('reply', .6)]]}


def repaired(index, *, elapsed=900000, path='common'):
    sample = clean(index, elapsed=elapsed, path=path)
    sample['analysis_repair'].update(attempted=True, succeeded=True,
        initial_errors=[{'index': 0, 'member_id': 'synthetic-member', 'side': 'ofr',
                         'reason': 'Price is not supported by its classified raw evidence'}])
    sample['analysis_inputs'].append({'validation_feedback': {'mode': 'EVIDENCE_ONLY'}})
    sample['evidence'].insert(1, {'stage': 'analysis', 'round': 2, 'round_kind': 'evidence_repair',
        'elapsed_ms': elapsed*.3, 'message_id': sample['case_id']+'-repair'})
    return sample


def report(samples):
    return summarize(samples, measurement_id='synthetic-independent-calibration', source={'kind': 'synthetic-test'})


def excluded(value):
    return {row['case_id']: set(row['reasons']) for row in value['calibration']['excluded_cases']}


def collect(samples, tmp_path, monkeypatch, capsys):
    output = tmp_path/'output'
    output.mkdir()
    config = tmp_path/'synthetic-credentials.json'
    config.write_text(json.dumps({'source': {'base_url': 'https://synthetic.invalid', 'model': 'synthetic'},
        'apps': {stage: {'key': 'synthetic-not-live'} for stage in ('analysis', 'reply')}}))
    manifest = tmp_path/'synthetic-manifest.json'
    manifest.write_text(json.dumps({'frozen_root': str(runner.ROOT), 'original_root': str(runner.ROOT),
                                    'files': {}, 'manifest_sha256': 'synthetic-gate-fixture'}))
    monkeypatch.setattr(runner, 'cases', lambda: [{'id': value['case_id']} for value in samples])
    by_id = {value['case_id']: value for value in samples}
    monkeypatch.setattr(runner, 'run_case', lambda case, *args, **kwargs: deepcopy(by_id[case['id']]))
    monkeypatch.setattr(sys, 'argv', ['measure_dify_v22.py', '--credentials', str(config),
        '--output', str(output), '--frozen-manifest', str(manifest), '--workers', '1'])
    exit_code = runner.main()
    capsys.readouterr()
    return json.loads((output/'report.json').read_text()), output, exit_code


def test_twenty_nine_clean_plus_repair_success_cannot_publish_profile(tmp_path, monkeypatch, capsys):
    value, output, _ = collect([clean(i) for i in range(29)]+[repaired('repair')], tmp_path, monkeypatch, capsys)
    assert not (output/'aggregation-profile.json').exists(), 'A repaired success cannot supply the thirtieth clean sample'
    assert value['complete'] is False and value['successes'] == 30 and value['failures'] == 0
    assert value['overall']['count'] == 30
    assert value['calibration']['eligible_count'] == 29
    assert 'REPAIR_ATTEMPTED' in excluded(value)['synthetic-clean-repair']


def test_repair_http_and_tool_failures_keep_overall_latency_without_inflating_reference():
    dirty_repair = repaired('repair')
    dirty_http = clean('http', elapsed=800000)
    dirty_http['analysis_inputs'].insert(0, {'thread_id': 'synthetic-first-failed'})
    dirty_http['evidence'].insert(0, {'stage': 'analysis', 'round': 1, 'elapsed_ms': 300000,
        'failed': True, 'error_code': 'DIFY_TIMEOUT', 'http_status': None})
    dirty_tool = clean('tool', elapsed=700000)
    dirty_tool['evidence'].insert(1, {'tool': 'bond_info', 'status': 'unavailable', 'elapsed_ms': 200000})
    samples = [clean(i) for i in range(30)]+[dirty_repair, dirty_http, dirty_tool]
    value = report(samples)
    assert value['overall']['count'] == 33 and value['overall']['mean_ms'] > 1000
    assert value['successes'] == 33 and value['failures'] == 0
    assert value['reference_ms'] == 1000, 'Retries and failed tools cannot enlarge the calibration bound'
    assert value['calibration']['eligible_count'] == 30 and value['calibration']['overall']['mean_ms'] == 1000
    assert value['calibration']['paths']['common']['count'] == 30
    reasons = excluded(value)
    assert {'INITIAL_ANALYSIS_REJECTION', 'REPAIR_ATTEMPTED'} <= reasons[dirty_repair['case_id']]
    assert 'HTTP_ATTEMPT_FAILED' in reasons[dirty_http['case_id']]
    assert 'TOOL_RESULT_NOT_OK' in reasons[dirty_tool['case_id']]


@pytest.mark.parametrize('missing', ['audit', 'attempted', 'initial_errors', 'analysis_trace', 'reply_trace'])
def test_missing_proof_is_excluded_even_when_input_claims_eligible(missing):
    suspect = clean('unproved', elapsed=999999)
    suspect['calibration_eligible'] = True
    suspect['exclusion_reasons'] = []
    if missing == 'audit': suspect.pop('analysis_repair')
    elif missing in ('attempted', 'initial_errors'): suspect['analysis_repair'].pop(missing)
    else:
        stage = missing.split('_')[0]
        suspect['evidence'] = [entry for entry in suspect['evidence'] if entry.get('stage') != stage]
    value = report([clean(i) for i in range(30)]+[suspect])
    assert value['reference_ms'] == 1000 and value['calibration']['eligible_count'] == 30
    expected = 'MISSING_STAGE_TRACE' if missing.endswith('_trace') else 'MISSING_REPAIR_AUDIT'
    assert expected in excluded(value)[suspect['case_id']]


def test_clean_proof_cannot_be_overridden_by_a_false_eligible_marker():
    samples = [clean(i) for i in range(30)]
    for sample in samples:
        sample.update(calibration_eligible=False, exclusion_reasons=['MODEL_SUPPLIED_EXCLUSION'])
    value = report(samples)
    assert value['calibration']['eligible_count'] == 30 and excluded(value) == {}
    assert value['reference_ms'] == 1000


@pytest.mark.parametrize('status', ['partial', 'invalid_input', 'unsupported', 'unavailable'])
def test_every_non_ok_tool_status_excludes_that_sample(status):
    suspect = clean('tool-'+status, elapsed=900000)
    suspect['evidence'].append({'tool': 'bond_calc', 'status': status, 'elapsed_ms': 10})
    value = report([clean(i) for i in range(30)]+[suspect])
    assert value['calibration']['eligible_count'] == 30 and value['reference_ms'] == 1000
    assert 'TOOL_RESULT_NOT_OK' in excluded(value)[suspect['case_id']]


def test_http_error_code_cannot_be_hidden_by_false_failed_flag():
    suspect = clean('http-error', elapsed=900000)
    suspect['evidence'][0].update(failed=False, error_code='DIFY_HTTP_ERROR', http_status=400)
    value = report([clean(i) for i in range(30)]+[suspect])
    assert value['calibration']['eligible_count'] == 30 and value['reference_ms'] == 1000
    assert 'HTTP_ATTEMPT_FAILED' in excluded(value)[suspect['case_id']]


def test_a_successful_round_cannot_hide_another_round_missing_actual_response_trace():
    suspect = clean('missing-followup-id', elapsed=900000)
    suspect['analysis_inputs'].append({'thread_id': 'synthetic-followup'})
    suspect['evidence'].insert(1, {'stage': 'analysis', 'round': 2,
        'round_kind': 'tool_or_history_followup', 'elapsed_ms': 100})
    value = report([clean(i) for i in range(30)]+[suspect])
    assert value['calibration']['eligible_count'] == 30
    assert value['reference_ms'] == 1000
    assert 'MISSING_STAGE_TRACE' in excluded(value)[suspect['case_id']]


def test_clean_fast_ordinary_path_remains_reference_when_repaired_fast_path_is_slow():
    samples = [clean(i, elapsed=1000) for i in range(30)]
    samples += [clean('fast-'+str(i), elapsed=100, path='fast_common') for i in range(6)]
    samples.append(repaired('slow-fast-repair', elapsed=1000000, path='fast_common'))
    value = report(samples)
    assert value['overall']['count'] == 37 and value['overall']['mean_ms'] > 1000
    assert value['reference_ms'] == 100
    assert value['ordinary_fastest_path']['path'] == 'fast_common'
    assert value['ordinary_fastest_path']['count'] == 6
    assert value['calibration']['eligible_count'] == 36


def test_dirty_only_ordinary_path_cannot_make_clean_tool_cohort_publishable(tmp_path, monkeypatch, capsys):
    samples = [clean(i, path='tools') for i in range(30)]+[repaired('ordinary', path='common')]
    value, output, _ = collect(samples, tmp_path, monkeypatch, capsys)
    assert not (output/'aggregation-profile.json').exists()
    assert value['complete'] is False and value['calibration']['eligible_count'] == 30


def test_final_failure_still_blocks_profile_even_with_thirty_clean_samples(tmp_path, monkeypatch, capsys):
    failed = clean('failed', elapsed=900000)
    failed.update(success=False, semantic_ok=False, timeout=True, error_code='DIFY_TIMEOUT')
    failed['evidence'][0].update(failed=True, error_code='DIFY_TIMEOUT')
    value, output, exit_code = collect([clean(i) for i in range(30)]+[failed], tmp_path, monkeypatch, capsys)
    assert exit_code != 0 and not value['all_cases_passed'] and not (output/'aggregation-profile.json').exists()
    assert value['successes'] == 30 and value['failures'] == 1 and value['timeouts'] == 1
    assert value['failure_rate'] == pytest.approx(1/31)
    assert value['overall']['count'] == 30 and value['calibration']['eligible_count'] == 30
    assert 'FINAL_FAILURE' in excluded(value)[failed['case_id']]


@pytest.mark.parametrize('reference', [800, 801, .25])
def test_strict_window_bound_remains_less_than_half_clean_reference(reference):
    value = report([clean(i, elapsed=reference) for i in range(30)])
    bound = value['aggregation']
    assert value['reference_ms'] == reference
    assert 0 <= bound['quiet_ms'] <= bound['max_wait_ms'] < reference/2


def text_repaired(index, *, elapsed=900000):
    sample=clean(index,elapsed=elapsed)
    sample['reply_repair'].update(attempted=True,succeeded=True,
        initial_errors=[{'reason':'Public yield text differs from validated quote'}],repair_elapsed_ms=elapsed*.5)
    sample['reply_inputs'].append({'validation_feedback':{'mode':'TEXT_ONLY'}})
    sample['evidence'].append({'stage':'reply','round':2,'round_kind':'text_repair',
        'elapsed_ms':elapsed*.5,'message_id':sample['case_id']+'-text-repair'})
    return sample


def test_twenty_nine_clean_plus_b_text_repair_success_cannot_publish_profile(tmp_path,monkeypatch,capsys):
    value,output,_=collect([clean(i) for i in range(29)]+[text_repaired('b-repair')],tmp_path,monkeypatch,capsys)
    assert not (output/'aggregation-profile.json').exists()
    assert value['complete'] is False and value['successes']==30 and value['overall']['count']==30
    assert value['calibration']['eligible_count']==29
    assert {'INITIAL_REPLY_REJECTION','REPAIR_ATTEMPTED'}<=excluded(value)['synthetic-clean-b-repair']


def test_b_text_repair_latency_is_reported_but_cannot_raise_clean_reference():
    repaired_sample=text_repaired('b-repair')
    value=report([clean(i) for i in range(30)]+[repaired_sample])
    assert value['overall']['count']==31 and value['overall']['mean_ms']>1000
    assert value['reference_ms']==1000 and value['calibration']['eligible_count']==30
    assert {'INITIAL_REPLY_REJECTION','REPAIR_ATTEMPTED'}<=excluded(value)[repaired_sample['case_id']]


@pytest.mark.parametrize('missing',['audit','attempted','initial_errors'])
def test_missing_b_repair_audit_excludes_a_claimed_clean_success(missing):
    sample=clean('missing-b',elapsed=900000)
    sample['calibration_eligible']=True
    if missing=='audit':sample.pop('reply_repair')
    else:sample['reply_repair'].pop(missing)
    value=report([clean(i) for i in range(30)]+[sample])
    assert value['reference_ms']==1000 and value['calibration']['eligible_count']==30
    assert 'MISSING_REPAIR_AUDIT' in excluded(value)[sample['case_id']]


@pytest.mark.parametrize('proof',['round_kind','feedback','first_error'])
def test_b_actual_trace_or_original_rejection_overrides_false_attempted_flag(proof):
    sample=clean('hidden-b',elapsed=900000)
    if proof=='round_kind':sample['evidence'][-1]['round_kind']='text_repair'
    elif proof=='feedback':sample['reply_inputs'][0]['validation_feedback']={'mode':'TEXT_ONLY'}
    else:sample['reply_repair']['initial_errors']=[{'reason':'Reply direction differs from our authorized side'}]
    value=report([clean(i) for i in range(30)]+[sample])
    assert value['calibration']['eligible_count']==30 and value['reference_ms']==1000
    expected='INITIAL_REPLY_REJECTION' if proof=='first_error' else 'REPAIR_ATTEMPTED'
    assert expected in excluded(value)[sample['case_id']]


def test_b_repair_summary_preserves_first_error_final_success_and_actual_spent_time(tmp_path,monkeypatch,capsys):
    success=text_repaired('success',elapsed=5000)
    failed=text_repaired('failed',elapsed=6000)
    failed.update(success=False,semantic_ok=False)
    failed['reply_repair'].update(succeeded=False,fallback=True,repair_error='synthetic timeout')
    value,_,_=collect([clean(i) for i in range(30)]+[success,failed],tmp_path,monkeypatch,capsys)
    summary=value['reply_repair']
    assert summary['initial_rejection_cases']==2 and summary['attempted_cases']==2
    assert summary['succeeded_cases']==1 and summary['fallback_cases']==1
    assert summary['repaired_final_success_cases']==1 and summary['reply_http_calls']==34
    assert summary['repair_elapsed_ms']['count']==2
    assert summary['repair_elapsed_ms']['mean_ms']==2750
    assert value['successes']==31 and value['failures']==1
    assert value['calibration']['eligible_count']==30
