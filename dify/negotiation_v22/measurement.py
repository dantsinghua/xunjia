"""Final-outcome latency and clean calibration; no unmeasured default window."""
import math
import statistics


def analysis_repair_summary(samples):
    repairs = [sample.get('analysis_repair', {}) for sample in samples]
    return {'initial_rejection_cases': sum(bool(row.get('initial_errors')) for row in repairs),
            'initial_rejected_observations': sum(len(row.get('initial_errors', [])) for row in repairs),
            'attempted_cases': sum(bool(row.get('attempted')) for row in repairs),
            'succeeded_cases': sum(bool(row.get('succeeded')) for row in repairs),
            'fallback_cases': sum(bool(row.get('fallback')) for row in repairs),
            'analysis_http_calls': sum(len(sample.get('analysis_inputs', [])) for sample in samples),
            'reply_http_calls': sum(len(sample.get('reply_inputs', [])) for sample in samples)}


def reply_repair_summary(samples):
    repairs = [sample.get('reply_repair', {}) for sample in samples]
    durations = [row.get('repair_elapsed_ms', 0) for row in repairs if row.get('attempted')]
    return {'initial_rejection_cases': sum(bool(row.get('initial_errors')) for row in repairs),
            'attempted_cases': sum(bool(row.get('attempted')) for row in repairs),
            'succeeded_cases': sum(bool(row.get('succeeded')) for row in repairs),
            'fallback_cases': sum(bool(row.get('fallback')) for row in repairs),
            'repaired_final_success_cases': sum(sample.get('success') is True and sample.get('semantic_ok') is True
                and bool(sample.get('reply_repair', {}).get('attempted')) for sample in samples),
            'repair_elapsed_ms': distribution(durations) if durations else None,
            'reply_http_calls': sum(len(sample.get('reply_inputs', [])) for sample in samples),
            'repair_http_calls': sum(event.get('stage') == 'reply' and event.get('round_kind') == 'text_repair'
                for sample in samples for event in sample.get('evidence', []))}


def distribution(values):
    values = sorted(values)
    return {'count':len(values),'mean_ms':statistics.mean(values),'p50_ms':statistics.median(values),
            'p95_ms':values[max(0, math.ceil(len(values)*0.95)-1)]}


def path_distributions(samples):
    groups = {}
    for sample in samples:
        groups.setdefault(sample['path'], []).append(sample['elapsed_ms'])
    return {key: distribution(values) for key, values in groups.items()}


def calibration_exclusion_reasons(sample):
    """Recompute eligibility from recorded attempts, never an eligibility flag."""
    reasons = []
    if sample.get('success') is not True or sample.get('semantic_ok') is not True:
        reasons.append('FINAL_FAILURE')
    audits = [sample.get(stage + '_repair') for stage in ('analysis', 'reply')]
    if any(not isinstance(audit, dict) or type(audit.get('attempted')) is not bool
           or not isinstance(audit.get('initial_errors'), list) for audit in audits):
        reasons.append('MISSING_REPAIR_AUDIT')
    analysis_audit, reply_audit = [audit if isinstance(audit, dict) else {} for audit in audits]
    if analysis_audit.get('initial_errors'):
        reasons.append('INITIAL_ANALYSIS_REJECTION')
    if reply_audit.get('initial_errors'):
        reasons.append('INITIAL_REPLY_REJECTION')
    evidence = sample.get('evidence')
    evidence = [row for row in evidence if isinstance(row, dict)] if isinstance(evidence, list) else []
    inputs = [row for stage in ('analysis', 'reply') if isinstance(sample.get(stage + '_inputs'), list)
              for row in sample[stage + '_inputs']]
    if (any(audit.get('attempted') is True for audit in (analysis_audit, reply_audit))
            or any(row.get('round_kind') in ('evidence_repair', 'text_repair') for row in evidence)
            or any(isinstance(row, dict) and row.get('validation_feedback') for row in inputs)):
        reasons.append('REPAIR_ATTEMPTED')

    def http_failed(row):
        status = row.get('http_status')
        return bool(row.get('failed') or row.get('error_code') or
                    (status is not None and (type(status) is not int or not 200 <= status < 300)))

    if any(http_failed(row) for row in evidence if row.get('stage') in ('analysis', 'reply')):
        reasons.append('HTTP_ATTEMPT_FAILED')
    if any(row.get('status') != 'ok' for row in evidence if row.get('tool')):
        reasons.append('TOOL_RESULT_NOT_OK')
    for stage in ('analysis', 'reply'):
        inputs = sample.get(stage + '_inputs')
        attempts = [row for row in evidence if row.get('stage') == stage]
        if (not isinstance(inputs, list) or not inputs or len(inputs) != len(attempts)
                or not any(row.get('message_id') and not http_failed(row) for row in attempts)
                or any(not row.get('message_id') and not http_failed(row) for row in attempts)):
            reasons.append('MISSING_STAGE_TRACE')
            break
    return reasons


def calibration_summary(samples):
    eligible, excluded = [], []
    for sample in samples:
        reasons = calibration_exclusion_reasons(sample)
        if reasons:
            excluded.append({'case_id': sample.get('case_id'), 'reasons': reasons})
        else:
            eligible.append(sample)
    return {'eligible_count': len(eligible), 'excluded_cases': excluded,
            'overall': distribution([sample['elapsed_ms'] for sample in eligible]) if eligible else None,
            'paths': path_distributions(eligible)}


def summarize(samples, *, measurement_id, source):
    good = [s for s in samples if s['success'] and s['semantic_ok']]
    calibration = calibration_summary(samples)
    if calibration['eligible_count'] < 30:
        raise ValueError('Need at least 30 clean successful real A+B cases without repair or failed attempts')
    common = {key:calibration['paths'][key] for key in ('common','fast_common') if key in calibration['paths']}
    if not common:
        raise ValueError('Missing clean ordinary high-frequency path')
    overall = distribution([s['elapsed_ms'] for s in good])
    fastest = min(common,key=lambda key:common[key]['mean_ms'])
    ordinary = {**common[fastest],'path':fastest}
    reference = min(calibration['overall']['mean_ms'],ordinary['mean_ms'])
    # Candidate bound is derived only after successful measured calls; this is
    # conservative latency selection, not a claim of arrival-trace J optimization.
    cap = min(3000,max(0,math.ceil(reference/2)-1))
    quiet = min(1500,cap)
    return {'measurement_id':measurement_id,'source':source,'attempts':len(samples),'successes':len(good),
            'failures':len(samples)-len(good),'failure_rate':(len(samples)-len(good))/len(samples),
            'timeouts':sum(bool(s.get('timeout')) for s in samples),
            'timeout_or_network_unknown':sum(bool(s.get('timeout_or_network_unknown')) for s in samples),
            'overall':overall,'ordinary_fastest_path':ordinary,'ordinary_path_groups':common,
            'paths':path_distributions(good),'calibration':calibration,'reference_ms':reference,
            'aggregation':{'quiet_ms':quiet,'max_wait_ms':cap,'reference_ms':reference,'measurement_id':measurement_id},
            'arrival_trace_optimization':'not measured; bounded candidate only'}
