"""Successful semantic A+B latency only; no default without measured evidence."""
import math
import statistics


def distribution(values):
    values = sorted(values)
    return {'count':len(values),'mean_ms':statistics.mean(values),'p50_ms':statistics.median(values),
            'p95_ms':values[max(0, math.ceil(len(values)*0.95)-1)]}


def summarize(samples, *, measurement_id, source):
    good = [s for s in samples if s['success'] and s['semantic_ok']]
    if len(good) < 30:
        raise ValueError('Need at least 30 semantically successful real A+B cases')
    groups = {}
    for sample in good:
        groups.setdefault(sample['path'],[]).append(sample['elapsed_ms'])
    common = {key:distribution(groups[key]) for key in ('common','fast_common') if groups.get(key)}
    if not common:
        raise ValueError('Missing ordinary high-frequency path')
    overall = distribution([s['elapsed_ms'] for s in good])
    fastest = min(common,key=lambda key:common[key]['mean_ms'])
    ordinary = {**common[fastest],'path':fastest}
    reference = min(overall['mean_ms'],ordinary['mean_ms'])
    # Candidate bound is derived only after successful measured calls; this is
    # conservative latency selection, not a claim of arrival-trace J optimization.
    cap = min(3000,max(0,math.ceil(reference/2)-1))
    quiet = min(1500,cap)
    return {'measurement_id':measurement_id,'source':source,'attempts':len(samples),'successes':len(good),
            'failures':len(samples)-len(good),'failure_rate':(len(samples)-len(good))/len(samples),
            'timeouts':sum(bool(s.get('timeout')) for s in samples),
            'timeout_or_network_unknown':sum(bool(s.get('timeout_or_network_unknown')) for s in samples),
            'overall':overall,'ordinary_fastest_path':ordinary,'ordinary_path_groups':common,
            'paths':{key:distribution(values) for key,values in groups.items()},'reference_ms':reference,
            'aggregation':{'quiet_ms':quiet,'max_wait_ms':cap,'reference_ms':reference,'measurement_id':measurement_id},
            'arrival_trace_optimization':'not measured; bounded candidate only'}
