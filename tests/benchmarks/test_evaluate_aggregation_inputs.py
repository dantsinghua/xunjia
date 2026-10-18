"""只验证 loader 输入保全；47 份合成原件与真实本地 Tool 内容门不被替换。"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

from dify.negotiation_v22.cases import cases, reference_snapshot
from dify.negotiation_v22.prompts import CALC_EXAMPLE, SIMILAR_EXAMPLE
from server.bond_tools import BondTools
from tests.benchmarks.evaluate_aggregation import load_measurement
from tests.cutover.tool_answer_artifacts import check_directory


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measured_sample(case_id):
    """合成完整审计结构，不能当作真实模型量测数据。"""
    return {
        'case_id': case_id, 'path': 'common', 'success': True, 'semantic_ok': True,
        'timeout': False, 'elapsed_ms': 1000, 'analysis_elapsed_ms': 400, 'reply_elapsed_ms': 600,
        'analysis_repair': {'attempted': False, 'initial_errors': []},
        'reply_repair': {'attempted': False, 'initial_errors': []},
        'analysis_inputs': [{}], 'reply_inputs': [{}],
        'evidence': [
            {'stage': 'analysis', 'message_id': 'synthetic-'+case_id+'-a', 'http_status': 200},
            {'stage': 'reply', 'message_id': 'synthetic-'+case_id+'-b', 'http_status': 200},
        ],
    }


def with_actual_public_tool(sample):
    case_id = sample['case_id']
    if case_id == 'tool-bond-info':
        request = {'name': 'bond_info', 'arguments': {'schema_version': '1', 'mode': 'get', 'bond_id': 'SYN-A'}}
        text, private = 'SYN-A 3.19%收500万；发行主体是SYN-ISSUER。', {'issuer_id': 'SYN-ISSUER'}
    elif case_id == 'tool-issuer-info':
        request = {'name': 'issuer_info', 'arguments': {'schema_version': '1', 'mode': 'get', 'issuer_id': 'SYN-ISSUER'}}
        text, private = 'SYN-A 3.19%收500万；主体为Synthetic Issuer。', {'registered_region': 'Synthetic Region'}
    elif case_id == 'tool-similar-bonds':
        request = deepcopy(SIMILAR_EXAMPLE)
        text = 'SYN-A 3.19%收500万；有同发行人期限更短、估值一致的候选券。'
        private = {'candidate': 'SYN-B', 'remaining_days': 335, 'valuation_comparison': '估值与锚券一致'}
    elif case_id == 'tool-bond-calc':
        request = deepcopy(CALC_EXAMPLE)
        text, private = 'SYN-A 3.19%收500万；所问价差已核算。', {'hypothetical_gap': '10.00 BP'}
    else:
        return sample
    tools = BondTools(SimpleNamespace(snapshot=lambda: reference_snapshot(case_id)))
    response = tools.invoke(request['name'], request['arguments'])
    assert response['status'] == 'ok'
    entry = {'request': request, 'result': response}
    sample['fact_commit'] = {'through_revision': 21, 'public_tool_results': [deepcopy(entry)]}
    sample['reply_inputs'] = [{
        'analysis_tool_source': {'analysis_id': 'synthetic-analysis', 'through_revision': 21},
        'analysis_tool_results': [deepcopy(entry)], 'tool_results': [],
    }]
    sample['evidence'].append({'tool': request['name'], 'status': response['status'],
                               'arguments': request['arguments'], 'result': response})
    sample['reply'] = {'action': 'SEND', 'proposal': {'kind': 'REPLY', 'text': text,
                       'our_price': {'kind': 'YIELD', 'value': '3.19'}}, 'trader_view': private}
    sample['decision_commit'] = {'state': 'QUEUED'}
    sample['path'] = 'tools'
    return sample


def test_each_loaded_group_report_is_retained_with_its_actual_sha256(tmp_path):
    directory = tmp_path/'synthetic-measurement'
    original_root, frozen_root = tmp_path/'original-source', tmp_path/'frozen-source'
    for root in (original_root, frozen_root):
        root.mkdir()
        (root/'synthetic.py').write_text('SYNTHETIC = True\n')
    files = {'synthetic.py': hashlib.sha256((original_root/'synthetic.py').read_bytes()).hexdigest()}
    source_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest = {'original_root': str(original_root), 'frozen_root': str(frozen_root),
                'files': files, 'manifest_sha256': source_hash}
    environment = {'latency_calibration_eligible': True, 'created_before_measurement': True, 'workers': 1}
    source = {'source_manifest_sha256': source_hash, 'measurement_environment': environment}
    spec_path, plan_path = tmp_path/'spec.json', tmp_path/'plan.json'
    spec_hash = write_json(spec_path, {'spec_version': 'measured-service-synthetic-arrival-grid-v1'})
    write_json(plan_path, {'aggregation_evaluation_spec_sha256': spec_hash,
        'source_manifest_sha256': source_hash, 'environment': environment, 'declared_at_epoch': 10})
    records = [with_actual_public_tool(measured_sample(case['id'])) for case in cases()]
    assert len(records) == len({record['case_id'] for record in records}) == 47
    groups, originals, group_hashes = [], [], {}
    for label, subset in [('ordinary', records[:16]), ('contextual', records[16:32]), ('fast-tools', records[32:])]:
        group_path = directory/label/'report.json'
        group_hashes[str(group_path.resolve())] = write_json(group_path, {
            'source': source, 'all_cases_passed': True,
            'source_integrity': {'frozen_drift': [], 'workspace_drift': []},
        })
        groups.append({'group': label, 'expected_cases': [row['case_id'] for row in subset],
            'runner': {'passed': True, 'exit_code': 0}, 'environment_before': {'epoch': 20}})
        for row in subset:
            original = directory/label/(row['case_id']+'.json')
            digest = write_json(original, row)
            assert write_json(directory/(row['case_id']+'.json'), row) == digest
            originals.append({'case_id': row['case_id'], 'path': str(original), 'sha256': digest})
    write_json(directory/'source-manifest.json', manifest)
    write_json(directory/'samples.json', records)
    tool_answers = check_directory(directory)
    assert tool_answers['passed'] and len(tool_answers['cases']) == 4, tool_answers
    write_json(directory/'tool-answers.json', tool_answers)
    write_json(directory/'report.json', {
        'source': source, 'passed': True, 'all_cases_passed': True, 'attempts': 47, 'successes': 47,
        'failures': 0, 'timeouts': 0, 'groups': groups, 'originals': originals,
        'source_integrity': {'frozen_drift': [], 'workspace_drift': []},
    })

    clean, summary, loaded_manifest, inputs, _ = load_measurement(directory, spec_path, plan_path)

    assert len(clean) == 47 and summary['calibration']['eligible_count'] == 47
    assert loaded_manifest == manifest
    missing = {path: digest for path, digest in group_hashes.items() if inputs.get(path) != digest}
    assert not missing, '已读取的三组 report 必须进入最终输入哈希检查：'+json.dumps(missing, ensure_ascii=False)
