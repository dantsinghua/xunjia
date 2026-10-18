"""Evaluate a declared grid from saved real measurements; never call a model.

Writes a proposed four-field runtime profile separately from its evidence.
Independent review is required before treating the proposal as a selected profile.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from aggregation_cost import arrival_traces, candidate_windows, simulate_trace
from dify.negotiation_v22.cases import cases
from dify.negotiation_v22.freeze import drift
from dify.negotiation_v22.measurement import calibration_exclusion_reasons, distribution, summarize
from server.negotiation.settings import Aggregation
from tests.cutover.tool_answer_artifacts import check_directory


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def load_measurement(directory, spec_path, plan_path):
    directory = Path(directory).resolve()
    report = json.loads((directory/'report.json').read_text())
    samples = json.loads((directory/'samples.json').read_text())
    manifest = json.loads((directory/'source-manifest.json').read_text())
    spec = json.loads(Path(spec_path).read_text())
    plan = json.loads(Path(plan_path).read_text())
    require(spec['spec_version']=='measured-service-synthetic-arrival-grid-v1', 'Unknown evaluation specification')
    require(plan['aggregation_evaluation_spec_sha256']==sha(spec_path), 'Evaluation specification changed after declaration')
    require(plan['source_manifest_sha256']==manifest['manifest_sha256'], 'Plan and runtime source differ')
    require(report['source']['source_manifest_sha256']==manifest['manifest_sha256'], 'Report and source differ')
    require(not drift(manifest) and not drift(manifest, frozen=False), 'Measured production source drift')
    environment = report['source']['measurement_environment']
    require(environment==plan['environment'] and environment.get('latency_calibration_eligible') is True
            and environment.get('created_before_measurement') is True and environment.get('workers')==1,
            'Measurement was not declared eligible and serial before execution')
    expected = {row['id'] for row in cases()}
    require(len(expected)==47 and len(samples)==47 and {s['case_id'] for s in samples}==expected,
            'The complete original corpus is required exactly once')
    require(report['passed'] is True and report['all_cases_passed'] is True
            and report['attempts']==report['successes']==47 and report['failures']==report['timeouts']==0,
            'Every real case must pass before selecting a window')
    require(not any(report['source_integrity'].values()), 'Recorded source integrity failed')
    require(all(s.get('success') is True and s.get('semantic_ok') is True and not s.get('timeout') for s in samples),
            'Failed samples cannot be removed to select a window')
    inputs = {str(directory/name):sha(directory/name) for name in ('report.json','samples.json','source-manifest.json','tool-answers.json')}
    covered = []
    for group in report['groups']:
        require(group['runner']['passed'] and group['runner']['exit_code']==0, 'A corpus group did not finish successfully')
        require(plan['declared_at_epoch'] <= group['environment_before']['epoch'], 'Plan was recorded after measurement began')
        covered.extend(group['expected_cases'])
        group_path = directory/group['group']/'report.json'
        group_bytes = group_path.read_bytes()
        grouped = json.loads(group_bytes)
        inputs[str(group_path)] = hashlib.sha256(group_bytes).hexdigest()
        require(grouped['source']==report['source'] and grouped['all_cases_passed'] is True
                and not any(grouped['source_integrity'].values()), 'Group configuration, result or integrity differs')
    require(len(covered)==47 and set(covered)==expected, 'Group coverage is incomplete or duplicated')
    originals = {row['case_id']: row for row in report['originals']}
    require(len(originals)==len(report['originals'])==47 and set(originals)==expected, 'Missing original artifacts')
    for sample in samples:
        entry = originals[sample['case_id']]
        original = Path(entry['path']).resolve()
        require(original.is_relative_to(directory), 'Original artifact is outside this measurement')
        copied = directory/(sample['case_id']+'.json')
        require(sha(original)==sha(copied)==entry['sha256'], 'Original case artifact changed')
        require(json.loads(copied.read_text())==sample, 'Sample differs from its retained original')
        inputs.update({str(original):sha(original),str(copied):sha(copied)})
    tool = check_directory(directory)
    require(tool['passed'] and len(tool['cases'])==4, 'Public tool answers are not all verified')
    clean = [sample for sample in samples if not calibration_exclusion_reasons(sample)]
    require(len(clean)>=30, 'At least 30 clean complete measured pairs are required')
    for sample in clean:
        for stage in ('analysis','reply'):
            duration = sample[stage+'_elapsed_ms']
            require(type(duration) in (int,float) and 0 < duration < 150000,
                    'A clean sample is outside the current simulator stage budget; do not drop it')
    summary = summarize(samples, measurement_id='real-corpus-reference', source=report['source'])
    inputs.update({str(Path(spec_path).resolve()):sha(spec_path),str(Path(plan_path).resolve()):sha(plan_path)})
    return clean, summary, manifest, inputs, spec


def evaluate(directory, output, spec_path, plan_path):
    output = Path(output).resolve()
    require(not output.exists(), 'Preserve previous evaluation; choose a new output directory')
    clean, summary, manifest, inputs, spec = load_measurement(directory, spec_path, plan_path)
    source_files = [Path(__file__).resolve(),Path(__file__).with_name('aggregation_cost.py').resolve()]
    inputs.update({str(path):sha(path) for path in source_files})
    output.mkdir(parents=True, exist_ok=False)
    write(output/'inputs.json', {'files':inputs,'runtime_manifest_sha256':manifest['manifest_sha256'],
                               'clean_case_ids':[s['case_id'] for s in clean], 'spec':spec})
    reference = summary['reference_ms']
    windows = candidate_windows(reference)
    phases = spec['arrival_traces']['poll_phases_ms']
    require(phases==[0,125], 'Unsupported polling phase specification')
    table = []
    started = time.monotonic()
    with (output/'events.jsonl').open('x') as ledger:
        for quiet, cap in windows:
            scores, delays, extras, visible, final_tails, scheduling, rejected_cost = [], [], [], [], [], [], []
            phase_scores = {str(phase):[] for phase in phases}
            calls_a = calls_b = 0
            for sample in clean:
                analysis_ms, reply_ms = sample['analysis_elapsed_ms'], sample['reply_elapsed_ms']
                for label, arrivals in arrival_traces(reference, analysis_ms, reply_ms).items():
                    for phase in phases:
                        result = simulate_trace(arrivals_ms=arrivals,analysis_ms=analysis_ms,reply_ms=reply_ms,
                            quiet_ms=quiet,max_wait_ms=cap,reference_ms=reference,poll_phase_ms=phase)
                        ledger.write(json.dumps({'case_id':sample['case_id'],'trace':label,'poll_phase_ms':phase,
                            'quiet_ms':quiet,'max_wait_ms':cap,'analysis_ms':analysis_ms,'reply_ms':reply_ms,
                            'arrivals_ms':arrivals,'result':result},ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
                        scores.append(result['J_ms']);delays.append(result['D_ms']);extras.append(result['extra_ms'])
                        phase_scores[str(phase)].append(result['J_ms'])
                        calls_a+=result['analysis_calls'];calls_b+=result['reply_calls']
                        visible.extend(row['facts_ms']-row['received_ms'] for row in result['message_events'])
                        scheduling.extend(row['scheduling_wait_ms'] for row in result['message_events'])
                        final_tails.append(result['reply_events'][-1]['finished_ms']-arrivals[-1])
                        rejected_cost.append(sum(reply_ms for row in result['reply_events'] if row['state']=='REJECTED'))
            expected_count = len(clean)*6*len(phases)
            require(len(scores)==expected_count, 'A trace or measured pair was lost')
            entry = {'quiet_ms':quiet,'max_wait_ms':cap,'simulations':len(scores),
                'D_ms':statistics.mean(delays),'extra_ms':statistics.mean(extras),'J_ms':statistics.mean(scores),
                'analysis_calls':calls_a,'reply_calls':calls_b,'mean_rejected_reply_cost_ms':statistics.mean(rejected_cost),
                'visible_facts_ms':{**distribution(visible),'max_ms':max(visible)},
                'final_reply_tail_ms':{**distribution(final_tails),'max_ms':max(final_tails)},
                'scheduling_wait_ms':{**distribution(scheduling),'max_ms':max(scheduling)},
                'phase_J_ms':{phase:statistics.mean(values) for phase,values in phase_scores.items()}}
            table.append(entry)
            ledger.flush()
            print(json.dumps({'window':[quiet,cap],'simulations':len(scores),'J_ms':entry['J_ms'],
                              'elapsed_seconds':round(time.monotonic()-started,3)}),flush=True)
    require(not drift(manifest) and not drift(manifest,frozen=False), 'Measured production source changed during evaluation')
    require(all(sha(path)==digest for path,digest in inputs.items()), 'An evaluation input changed')
    selected = min(table,key=lambda row:(row['J_ms'],row['max_wait_ms'],row['quiet_ms']))
    phase_winners = {str(phase):min(table,key=lambda row:(row['phase_J_ms'][str(phase)],row['max_wait_ms'],row['quiet_ms']))
                     for phase in phases}
    pareto = [row for row in table if not any(other['D_ms']<=row['D_ms'] and other['extra_ms']<=row['extra_ms']
        and (other['D_ms']<row['D_ms'] or other['extra_ms']<row['extra_ms']) for other in table)]
    profile = {'quiet_ms':selected['quiet_ms'],'max_wait_ms':selected['max_wait_ms'],
               'reference_ms':reference,'measurement_id':'r20-full47-synthetic-arrival-grid-v1'}
    Aggregation(**profile)
    write(output/'proposed-aggregation-profile.json',profile)
    result = {'status':'PROPOSED_PENDING_INDEPENDENT_REVIEW','all_simulations_completed':True,
        'source_manifest_sha256':manifest['manifest_sha256'],'measurement_directory':str(Path(directory).resolve()),
        'clean_count':len(clean),'reference_ms':reference,'candidate_count':len(table),
        'simulation_count':sum(row['simulations'] for row in table),'candidates':table,'selected':selected,
        'phase_winners':{phase:[row['quiet_ms'],row['max_wait_ms']] for phase,row in phase_winners.items()},
        'pareto_windows':[[row['quiet_ms'],row['max_wait_ms']] for row in pareto],
        'service_mean_ms':statistics.mean(s['analysis_elapsed_ms']+s['reply_elapsed_ms'] for s in clean),
        'inputs_unchanged':True,'events_sha256':sha(output/'events.jsonl'),'profile_sha256':sha(output/'proposed-aggregation-profile.json'),
        'elapsed_seconds':time.monotonic()-started,'external_im_sent':False,'limits':spec['limits']}
    write(output/'evaluation.json',result)
    print('AGGREGATION_EVALUATION_END '+json.dumps({'selected':profile,'simulations':result['simulation_count']}),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--spec',required=True)
    parser.add_argument('--measurement-plan',required=True)
    args=parser.parse_args()
    evaluate(args.run_dir,args.output,args.spec,args.measurement_plan)


if __name__=='__main__':
    main()
