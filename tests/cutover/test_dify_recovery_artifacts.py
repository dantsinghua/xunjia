"""Pure-file adversarial checks for the independent recovery artifact gate."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from dify_recovery_artifacts import check_run


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def quote(revision):
    return {'side': 'ofr', 'price': {'kind': 'YIELD', 'value': '3.10', 'settlement_date': None,
        'computed_yield': None, 'calculation_ref': None} if revision else None,
        'volume_wan': '500' if revision else None, 'assertion': 'QUOTE', 'validity': 'ACTIVE',
        'finality': 'NEGOTIABLE' if revision else 'UNKNOWN', 'confirmed_day': '2026-10-17' if revision else None,
        'revision': revision, 'evidence': {'price': ['inbound'], 'volume_wan': ['inbound']} if revision else {}}


def reported_job(job):
    value = deepcopy(job)
    value['lease_token_sha256'] = fingerprint(value.pop('lease_token'))
    for field in ('snapshot_json', 'basis_json'):
        if field in value:
            value[field+'_sha256'] = fingerprint(json.loads(value.pop(field)))
    return value


def phase(name, stage, pid, before, after, outcome, http_count):
    return {'phase': name, 'stage': stage, 'pid': pid, 'stage_budget_seconds': 150, 'lease_seconds': 180,
        'before': deepcopy(before), 'before_target': deepcopy(before), 'outcome': outcome,
        'after': after, 'after_repeat': deepcopy(after), 'immediate_repeat': None,
        'repeat_http_attempts': 0, 'errors': ['DIFY_TIMEOUT'] if name == 'fault' else [],
        'http_attempts': http_count, 'external_im_sent': False}


def attempt(stage, phase_name, pid, context, start, result=None):
    failed = phase_name == 'fault'
    value = {'stage': stage, 'phase': phase_name, 'pid': pid, 'round': 1, 'round_kind': 'initial',
        'started_at': start, 'ended_at': start+1, 'http_timeout_seconds': 1 if failed else 90,
        'stage_budget_seconds': 150, 'fault_injection': 'actual HTTP timeout budget of one second' if failed else None,
        'context': deepcopy(context), 'context_sha256': fingerprint(context)}
    if failed:
        value.update(error_type='FlowUnavailable', error_code='DIFY_TIMEOUT',
                     evidence={'stage': stage, 'elapsed_ms': 1000, 'error_code': 'DIFY_TIMEOUT', 'http_status': None})
    else:
        value.update(result=deepcopy(result), evidence={'stage': stage, 'elapsed_ms': 1000,
                     'message_id': 'synthetic-message-'+str(pid), 'workflow_run_id': 'native-'+str(pid)+'-'+stage})
    return value


def case(stage):
    mid, tid = stage+'-member', stage+'-thread'
    epoch = 1792209600.0
    pid = 100 if stage == 'analysis' else 200
    spec = {'owner': 'synthetic-owner', 'member_id': mid, 'thread_id': tid,
            'message_id': 'inbound', 'external_im_sent': False, 'source': 'explicit synthetic fixture'}
    raw = {'message_id': 'inbound', 'thread_id': tid, 'event_id': 'synthetic-inbound',
        'source_seq': 2, 'ingest_revision': 2, 'direction': 'in', 'kind': 'message',
        'text': 'SYN-A 3.10出500万，还可以谈', 'native_id': None, 'target_id': None,
        'occurred_at': epoch, 'received_at': epoch, 'trade_day': '2026-10-17', 'historical': 0,
        'content_hash': hashlib.sha256('synthetic raw'.encode()).hexdigest()}
    opening = {'message_id': 'opening', 'direction': 'out', 'text': 'SYN-A 3.20%收，请报可做价量',
               'native_id': 'synthetic-opening', 'ingest_revision': 1}
    snapshot = {'thread_id': tid, 'from_revision': 1, 'through_revision': 2, 'context_messages': 20,
                'messages': [raw], 'members': [{'member_id': mid, 'bond_code': 'SYN-A',
                    'our_side': 'BUY', 'context': [opening], 'version': 1,
                    'quotes': {'ofr': quote(0)},
                    'plan': {'name': 'SYN-A', 'code': 'SYN-A', 'our_side': 'BUY',
                             'trade_day': '2026-10-17', 'step_bp': '2',
                             'opening_yield_pct': '3.20', 'volume_wan': '2000'}}]}
    a_context = {**deepcopy(snapshot), 'context_policy_revision': 'latest_quote_confirmation_v1',
                 'tool_results': [], 'history_results': []}
    a_context['members'][0]['plan'] = {key: snapshot['members'][0]['plan'][key]
        for key in ('name', 'code', 'our_side', 'trade_day')}
    complete_quote = quote(1)
    a_result = {'schema_version': '2.2', 'scopes': {'inbound': [mid]}, 'observations': [{
        'member_id': mid, 'side': 'ofr', 'changes': {k: deepcopy(complete_quote[k]) for k in
            ('price', 'volume_wan', 'assertion', 'validity', 'finality', 'confirmed_day')},
        'evidence': {k: ['inbound'] for k in ('price', 'volume_wan', 'assertion', 'validity', 'finality', 'confirmed_day')}}]}
    committed = {'facts_committed': True, 'accepted': [{'index': 0, 'member_id': mid, 'side': 'ofr', 'revision': 1}],
                 'rejected': [], 'through_revision': 2}
    a_job = {'analysis_id': stage+'-analysis', 'thread_id': tid, 'from_revision': 1, 'through_revision': 2,
        'state': 'COMMITTED', 'lease_token': 'a-recovered-token', 'lease_until': epoch+200,
        'attempts': 2 if stage == 'analysis' else 1, 'snapshot_json': dump(snapshot),
        'result_json': dump(committed), 'created_at': epoch, 'committed_at': epoch+10}
    quote_row = {'quote_id': stage+'-quote', 'member_id': mid, 'side': 'ofr', 'revision': 1,
                 'analysis_id': a_job['analysis_id'], 'state_json': dump(complete_quote), 'created_at': epoch+10}
    basis = {'member_id': mid, 'member_version': 1, 'observed_watermark': 2}
    b_context = {'member_id': mid, 'thread_id': tid, 'bond_code': 'SYN-A', 'our_side': 'BUY',
        'trade_day': '2026-10-17', 'basis': basis, 'quotes': {'ofr': complete_quote},
        'context': [opening, raw], 'context_policy_revision': 'latest_quote_confirmation_v1',
        'tool_results': [], 'history_results': [], 'quote_constraints': {'status': 'AVAILABLE',
            'min_yield_pct': '3.18', 'max_yield_pct': '3.22', 'tick_size': '0.01'}}
    body = {'kind': 'REPLY', 'text': 'SYN-A 3.19%收，500万报价。',
            'our_price': {'kind': 'YIELD', 'value': '3.19'}}
    b_result = {'schema_version': '2.2', 'action': 'SEND', 'proposal': {**body,
        'render_plan': {'kind': 'REPLY', 'quote_index': 1}}, 'trader_view': {'reason': 'Synthetic decision'}}
    b_outcome = {'state': 'QUEUED', 'command_id': 'synthetic-command'}
    command = {'command_id': 'synthetic-command', 'request_id': 'synthetic-request',
        'member_id': mid, 'thread_id': tid, 'state': 'QUEUED', 'body_json': dump(body),
        'basis_json': dump(basis), 'created_at': epoch+20, 'native_id': None, 'sent_at': None}
    b_job = {'job_id': 'synthetic-reply-job', 'member_id': mid, 'request_id': 'synthetic-request',
        'state': 'DONE', 'lease_token': 'b-recovered-token', 'lease_until': epoch+30, 'attempts': 2,
        'basis_json': dump(basis), 'result_json': dump(b_outcome), 'created_at': epoch+10}
    def state(job, fact, *, commands=()):
        return {'job': reported_job(job) if job else None, 'quote': quote(int(fact)),
            'quote_rows': [deepcopy(quote_row)] if fact else [], 'commands': list(deepcopy(commands)),
            'message': {**deepcopy(raw), 'analysis_state': 'ANALYZED' if fact else 'QUEUED', 'contexts': []},
            'thread': {'thread_id': tid, 'owner_id': spec['owner'], 'ingest_revision': 2,
                       'analyzed_revision': 2 if fact else 1}, 'reply_repair_audits': []}
    final_job = a_job if stage == 'analysis' else b_job
    fault_job = {**deepcopy(final_job), 'state': 'RETRY', 'attempts': 1,
                 'lease_token': 'failed-token', 'lease_until': epoch+3 if stage == 'analysis' else epoch+16,
                 'result_json': dump({'error_type': 'FlowUnavailable'})}
    if stage == 'analysis':
        fault_job['committed_at'] = None
    before_target = state(None, stage == 'reply')
    after_fault = state(fault_job, stage == 'reply')
    final = state(final_job, 1, commands=[command] if stage == 'reply' else [])
    fault = phase('fault', stage, pid, before_target, after_fault,
        {'facts_committed': False, 'analysis_id': a_job['analysis_id']} if stage == 'analysis' else {'state': 'RETRY'},
        1 if stage == 'analysis' else 2)
    recovered = phase('recover', stage, pid+1, after_fault, final,
                      committed if stage == 'analysis' else b_outcome, 1)
    start = 1800000000.0 + pid
    target_context = a_context if stage == 'analysis' else b_context
    traces = [attempt(stage, 'fault', pid, target_context, start),
              attempt(stage, 'recover', pid+1, target_context, start+(4 if stage == 'analysis' else 7),
                      a_result if stage == 'analysis' else b_result)]
    if stage == 'reply':
        fault['before'] = state(None, False)
        fault['analysis_commit'] = committed
        preparation = attempt('analysis', 'recover', pid, a_context, start-5, a_result)
        preparation['phase'] = 'fault'
        traces.insert(0, preparation)
    return {'spec': spec, 'clock': {'epoch': epoch, 'monotonic': 1000.0+pid}, 'fault': fault,
            'recover': recovered, 'traces': traces, 'a_job': a_job, 'final_job': final_job,
            'raw': raw, 'quote_row': quote_row, 'command': command if stage == 'reply' else None}


def sqlite_table(connection, name, rows):
    sample = rows[0]
    columns = {key: 'INTEGER' if type(value) is int else 'REAL' if type(value) is float else 'TEXT'
               for key, value in sample.items()}
    connection.execute('CREATE TABLE '+name+' ('+', '.join('"'+key+'" '+kind for key, kind in columns.items())+')')
    placeholders = ','.join('?' for _ in sample)
    for row in rows:
        connection.execute('INSERT INTO '+name+' VALUES ('+placeholders+')', list(row.values()))


def save_case(directory, value):
    directory.mkdir(exist_ok=True)
    for name in ('spec', 'clock', 'fault', 'recover'):
        write(directory/(name+'.json'), value[name])
    (directory/'http-attempts.jsonl').write_text(''.join(dump(row)+'\n' for row in value['traces']))
    with sqlite3.connect(directory/'state.db') as db:
        sqlite_table(db, 'xj2_analysis', [value['a_job']])
        if directory.name == 'reply':
            sqlite_table(db, 'xj2_reply_job', [value['final_job']])
        sqlite_table(db, 'xj2_quote', [value['quote_row']])
        if value['command']:
            sqlite_table(db, 'xj2_command', [value['command']])
        else:
            db.execute('CREATE TABLE xj2_command (command_id TEXT, created_at REAL)')
        sqlite_table(db, 'xj2_message', [value['raw']])
        sqlite_table(db, 'xj2_thread', [value['recover']['after']['thread']])


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def native(root, source, cases):
    entries = []
    for target, case_value in cases.items():
        for index, row in enumerate(case_value['traces']):
            app = source['apps'][row['stage']]
            native_id = 'native-'+target+'-'+str(index)
            context = {**row['context'], 'schema_version': '2.2', 'stage': row['stage']}
            raw_text = dump(row.get('result', {'schema_version': '2.2', 'synthetic_timeout_output': True}))
            created, finished = utc(row['started_at']), utc(row['ended_at']+1)
            payload = {'database_timezone': 'UTC', 'workflow': {'id': app['workflow_id'],
                'app_id': app['app_id'], 'graph': app['_graph']},
                'run': {'id': native_id, 'app_id': app['app_id'], 'workflow_id': app['workflow_id'],
                    'graph': dump(json.loads(app['_graph'])), 'inputs': dump({'context_json': dump(context)}),
                    'status': 'succeeded', 'error': None, 'created_at': created, 'finished_at': finished},
                'nodes': [{'id': 'node-'+native_id, 'node_id': 'model', 'node_type': 'llm', 'status': 'succeeded',
                    'inputs': None, 'outputs': dump({'text': raw_text}), 'error': None,
                    'created_at': created, 'finished_at': finished}]}
            if 'result' in row:
                payload['nodes'].append({'id': 'contract-'+native_id, 'node_id': 'contract', 'node_type': 'code',
                    'status': 'succeeded', 'inputs': dump({'text': raw_text, 'context_json': dump(context)}),
                    'outputs': dump({'result': dump(row['result'])}), 'error': None,
                    'created_at': created, 'finished_at': finished})
            path = root/'native'/(native_id+'.json')
            write(path, payload)
            entries.append({'target_stage': target, 'phase': row['phase'], 'stage': row['stage'],
                'round': row['round'], 'pid': row['pid'], 'context_sha256': row['context_sha256'],
                'app_id': app['app_id'], 'workflow_run_id': native_id,
                'artifact': str(path.relative_to(root)), 'artifact_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            # Native IDs known from successful HTTP are the same actual run;
            # timeout attempts intentionally have no returned workflow ID.
            if 'result' in row:
                row['evidence']['workflow_run_id'] = native_id
        (root/target/'http-attempts.jsonl').write_text(''.join(dump(row)+'\n' for row in case_value['traces']))
    write(root/'native-recovery-audit.json', {'schema_version': '1', 'entries': entries})


@pytest.fixture
def complete(tmp_path):
    root = tmp_path/'recovery'
    root.mkdir()
    frozen = tmp_path/'frozen'/'source'
    names = ['scripts/measure_dify_recovery.py', 'scripts/measure_dify_v22.py',
             'server/negotiation/engine.py', 'server/negotiation/store.py',
             'server/negotiation/reply_render.py', 'dify/negotiation_v22/cases.py',
             'dify/negotiation_v22/slot_wire.py', 'dify/negotiation_v22/node_code.py',
             'dify/negotiation_v22/build.py', 'dify/negotiation_v22/prompts.py']
    hashes = {}
    for name in names:
        path = frozen/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Explicit synthetic source artifact: '+name+'\n')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    write(frozen.parent/'source-manifest.json', {'frozen_root': str(frozen), 'files': hashes, 'manifest_sha256': manifest_hash})
    source = {'source_root': str(frozen), 'source_manifest_sha256': manifest_hash,
              'excluded_from_latency_calibration': True, 'external_im_sent': False, 'apps': {}}
    for stage in ('analysis', 'reply'):
        graph = json.dumps({'nodes': [{'id': 'model', 'data': {'type': 'llm', 'vision': {'enabled': False}}},
                                     {'id': 'contract', 'data': {'type': 'code'}}]}, indent=2)
        source['apps'][stage] = {'app_id': stage+'-app', 'workflow_id': stage+'-workflow',
                                'graph_sha256': hashlib.sha256(graph.encode()).hexdigest(), '_graph': graph}
    cases = {stage: case(stage) for stage in ('analysis', 'reply')}
    for stage, value in cases.items():
        save_case(root/stage, value)
    write(root/'source.json', {**source, 'apps': {
        stage: {key: value for key, value in app.items() if key != '_graph'}
        for stage, app in source['apps'].items()}})
    report = {'cases': [{'stage': stage, 'first_attempt': value['fault'], 'recovery': value['recover'],
        'recovery_attempted': True, 'recovery_process_exit': 0} for stage, value in cases.items()],
        'acceptance': 'independent reviewer required', 'frozen_source_drift': [],
        'excluded_from_latency_calibration': True, 'external_im_sent': False}
    write(root/'report.json', report)
    native(root, source, cases)
    return root


def mutate_phase(root, stage, phase_name, change):
    path = root/stage/(phase_name+'.json')
    value = json.loads(path.read_text())
    change(value)
    write(path, value)
    report = json.loads((root/'report.json').read_text())
    entry = next(entry for entry in report['cases'] if entry['stage'] == stage)
    entry['first_attempt' if phase_name == 'fault' else 'recovery'] = value
    write(root/'report.json', report)


def test_complete_synthetic_recovery_requires_all_local_and_native_evidence(complete):
    result = check_run(complete)
    assert result['status'] == 'PASS' and result['passed'], result
    assert result['local_recovery']['passed']
    assert len(result['native_linkage']['attempts']) == 5


def test_missing_native_linkage_is_explicitly_incomplete_and_never_pass(complete):
    (complete/'native-recovery-audit.json').unlink()
    result = check_run(complete)
    assert result['status'] == 'INCOMPLETE' and not result['passed'], result
    assert result['local_recovery']['passed']
    assert result['native_linkage']['pending'] == ['NATIVE_WORKFLOW_AND_MODEL_LINKAGE_REQUIRED']


@pytest.mark.parametrize('fault', ['wait', 'failed', 'new_job', 'snapshot', 'basis', 'attempt', 'lease',
                                  'same_pid', 'repeat_http', 'repeated_fact', 'duplicated_command', 'changed_a_fact'])
def test_zero_collector_exit_cannot_hide_failed_or_unrelated_recovery(complete, fault):
    stage = 'analysis' if fault in ('snapshot', 'repeated_fact') else 'reply'
    def change(value):
        if fault == 'wait': value['outcome']['state'] = 'WAIT'
        elif fault == 'failed': value['after']['job']['state'] = 'RETRY'
        elif fault == 'new_job': value['after']['job']['job_id'] = 'different-job'
        elif fault == 'snapshot': value['after']['job']['snapshot_json_sha256'] = 'f'*64
        elif fault == 'basis': value['after']['job']['basis_json_sha256'] = 'e'*64
        elif fault == 'attempt': value['after']['job']['attempts'] = 1
        elif fault == 'lease': value['after']['job']['lease_token_sha256'] = value['before']['job']['lease_token_sha256']
        elif fault == 'same_pid': value['pid'] = 200
        elif fault == 'repeat_http': value['repeat_http_attempts'] = 1
        elif fault == 'repeated_fact': value['after']['quote']['revision'] = 2
        elif fault == 'duplicated_command': value['after']['commands'].append(deepcopy(value['after']['commands'][0]))
        else: value['after']['quote']['volume_wan'] = '600'
        if fault not in ('repeat_http',): value['after_repeat'] = deepcopy(value['after'])
    mutate_phase(complete, stage, 'recover', change)
    checked = check_run(complete)
    assert not checked['passed']


@pytest.mark.parametrize('fault', ['timeout_code', 'early_retry', 'a_rerun_in_b', 'context_hash', 'missing_trace'])
def test_trace_and_real_wait_are_required_not_summary_claims(complete, fault):
    stage = 'analysis' if fault in ('timeout_code', 'early_retry') else 'reply'
    path = complete/stage/'http-attempts.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if fault == 'timeout_code': rows[0]['error_code'] = 'DIFY_HTTP_ERROR'
    elif fault == 'early_retry':
        rows[-1]['started_at'] = rows[0]['ended_at']+.01
        rows[-1]['ended_at'] = rows[-1]['started_at']+1
    elif fault == 'a_rerun_in_b': rows[-1]['stage'] = 'analysis'
    elif fault == 'context_hash': rows[-1]['context']['member_id'] = 'another-member'
    else: rows.pop()
    path.write_text(''.join(dump(row)+'\n' for row in rows))
    assert not check_run(complete)['passed']


@pytest.mark.parametrize('missing', ['state.db', 'clock.json', 'fault.json'])
def test_missing_persistent_evidence_cannot_be_replaced_by_report(complete, missing):
    (complete/'analysis'/missing).unlink()
    assert not check_run(complete)['passed']


def test_database_disagreement_is_detected_even_when_report_copies_are_consistent(complete):
    with sqlite3.connect(complete/'reply'/'state.db') as db:
        db.execute("UPDATE xj2_command SET state='SENT'")
    assert not check_run(complete)['passed']


@pytest.mark.parametrize('fault', ['missing_attempt', 'reused_run', 'wrong_app', 'wrong_context',
                                  'context_numeric_type', 'graph_bytes', 'run_graph', 'graph_numeric_type',
                                  'model_not_entered', 'wrong_contract_output', 'corrupt_sha'])
def test_native_claims_need_raw_matching_workflow_model_and_contract_evidence(complete, fault):
    audit_path = complete/'native-recovery-audit.json'
    audit = json.loads(audit_path.read_text())
    if fault == 'missing_attempt': audit['entries'].pop()
    elif fault == 'corrupt_sha': audit['entries'][-1]['artifact_sha256'] = '0'*64
    else:
        entry = audit['entries'][-1]
        path = complete/entry['artifact']
        raw = json.loads(path.read_text())
        if fault == 'reused_run':
            entry['workflow_run_id'] = raw['run']['id'] = audit['entries'][0]['workflow_run_id']
        elif fault == 'wrong_app': raw['run']['app_id'] = 'other-app'
        elif fault == 'wrong_context': raw['run']['inputs'] = dump({'context_json': dump({'stage': 'reply'})})
        elif fault == 'context_numeric_type':
            request = json.loads(raw['run']['inputs'])
            context = json.loads(request['context_json'])
            context['basis']['member_version'] = True
            raw['run']['inputs'] = dump({**request, 'context_json': dump(context)})
        elif fault == 'graph_bytes': raw['workflow']['graph'] += ' '
        elif fault == 'run_graph': raw['run']['graph'] = dump({'nodes': []})
        elif fault == 'graph_numeric_type':
            graph = json.loads(raw['run']['graph'])
            graph['nodes'][0]['data']['vision']['enabled'] = 0
            raw['run']['graph'] = dump(graph)
        elif fault == 'model_not_entered': raw['nodes'][0]['status'] = 'pending'
        else: raw['nodes'][-1]['outputs'] = dump({'result': dump({'schema_version': '2.2', 'action': 'WAIT'})})
        write(path, raw)
        entry['artifact_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(audit_path, audit)
    checked = check_run(complete)
    assert not checked['passed'] and checked['local_recovery']['passed']


def test_native_fault_only_proves_model_entry_not_a_successful_physical_model_result(complete):
    audit_path = complete/'native-recovery-audit.json'
    audit = json.loads(audit_path.read_text())
    entry = audit['entries'][0]
    path = complete/entry['artifact']
    raw = json.loads(path.read_text())
    raw['run'].update(status='failed', error='Synthetic provider failure after the client timeout')
    raw['nodes'][0].update(status='failed', outputs=None, error='Synthetic provider failure')
    write(path, raw)
    entry['artifact_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(audit_path, audit)
    result = check_run(complete)
    assert result['passed'], result
    assert result['native_linkage']['attempts'][0]['model_status'] == 'failed'


def test_identical_contexts_do_not_allow_swapping_fault_and_recovery_native_runs(complete):
    path = complete/'native-recovery-audit.json'
    audit = json.loads(path.read_text())
    first, second = audit['entries'][:2]
    for key in ('artifact', 'artifact_sha256', 'workflow_run_id'):
        first[key], second[key] = second[key], first[key]
    write(path, audit)
    result = check_run(complete)
    assert not result['passed'] and result['local_recovery']['passed']
    assert result['errors'][0]['reason'] in ('NATIVE_RUN_OUTSIDE_REQUEST_TIME_WINDOW', 'NATIVE_RUN_DIFFERS_FROM_RETURNED_HTTP_ID')


@pytest.mark.parametrize('fault', ['source', 'duplicate_key', 'invalid_shape', 'unfrozen'])
def test_missing_or_damaged_source_contract_is_fail_closed(complete, fault):
    if fault == 'source':
        source = json.loads((complete/'source.json').read_text())
        (Path(source['source_root'])/'server/negotiation/engine.py').write_text('# changed\n')
    elif fault == 'duplicate_key':
        (complete/'report.json').write_text('{"cases":[],"cases":[]}')
    elif fault == 'invalid_shape':
        report = json.loads((complete/'report.json').read_text())
        report['cases'] = [None, False]
        write(complete/'report.json', report)
    else:
        source = json.loads((complete/'source.json').read_text())
        source['source_manifest_sha256'] = None
        write(complete/'source.json', source)
    assert check_run(complete)['status'] == 'FAIL'


def test_checker_reads_artifacts_without_rewriting_or_normalizing_them(complete):
    files = [path for path in complete.rglob('*') if path.is_file()]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    assert check_run(complete)['passed']
    assert {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files} == before


def test_native_artifact_reference_must_stay_inside_this_run_directory(complete):
    path = complete/'native-recovery-audit.json'
    audit = json.loads(path.read_text())
    audit['entries'][0]['artifact'] = '../unowned-native.json'
    write(path, audit)
    result = check_run(complete)
    assert not result['passed'] and result['errors'][0]['reason'] == 'ARTIFACT_ESCAPES_RUN_DIRECTORY'


def test_cli_refuses_to_overwrite_existing_acceptance_evidence(complete):
    output = complete/'existing.json'
    output.write_text('PRESERVE_EXISTING_EVIDENCE\n')
    done = subprocess.run([sys.executable, str(Path(__file__).with_name('dify_recovery_artifacts.py')),
                           '--run-dir', str(complete), '--output', str(output)],
                          capture_output=True, text=True, timeout=5)
    assert done.returncode != 0 and output.read_text() == 'PRESERVE_EXISTING_EVIDENCE\n'


@pytest.mark.parametrize('mode,expected_code', [('pass', 0), ('incomplete', 2), ('fail', 1)])
def test_cli_exit_and_written_status_match_real_acceptance(complete, mode, expected_code):
    if mode == 'incomplete': (complete/'native-recovery-audit.json').unlink()
    if mode == 'fail': (complete/'reply'/'recover.json').unlink()
    output = complete/'checked.json'
    script = Path(__file__).with_name('dify_recovery_artifacts.py')
    done = subprocess.run([sys.executable, str(script), '--run-dir', str(complete), '--output', str(output)],
                          capture_output=True, text=True, timeout=5)
    assert done.returncode == expected_code, done.stdout+done.stderr
    result = json.loads(output.read_text())
    assert result['status'] == mode.upper() and result['passed'] is (mode == 'pass')


def test_real_analysis_projection_is_required_for_both_recovery_and_b_preparation(complete):
    for stage in ('analysis', 'reply'):
        with sqlite3.connect(complete/stage/'state.db') as db:
            snapshot = json.loads(db.execute('SELECT snapshot_json FROM xj2_analysis').fetchone()[0])
        assert set(snapshot['members'][0]['plan']) == {
            'name', 'code', 'our_side', 'trade_day', 'step_bp', 'opening_yield_pct', 'volume_wan'}
        rows = [json.loads(line) for line in (complete/stage/'http-attempts.jsonl').read_text().splitlines()]
        for row in rows:
            if row['stage'] != 'analysis': continue
            assert set(row['context']['members'][0]['plan']) == {'name', 'code', 'our_side', 'trade_day'}
            assert set(row['context']) - set(snapshot) == {
                'context_policy_revision', 'history_results', 'tool_results'}
    checked = check_run(complete)
    assert checked['passed'] and checked['local_recovery']['passed'], checked


@pytest.mark.parametrize('target', ['analysis', 'reply'])
@pytest.mark.parametrize('change', [
    'private_plan', 'missing_name', 'message_text',
    'quote_revision', 'member_identity', 'member_version_type', 'quote_revision_type',
    'extra_top_field', 'preloaded_tool', 'preloaded_history',
])
def test_analysis_projection_neither_leaks_private_plan_nor_accepts_any_other_input_drift(complete, target, change):
    path = complete/target/'http-attempts.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row['stage'] != 'analysis': continue
        context = row['context']
        member = context['members'][0]
        if change.startswith('private_'):
            # Reinsert the complete persisted plan so the old subset check
            # cannot mistake this leak for a missing snapshot field.
            member['plan'].update(step_bp='2', opening_yield_pct='3.20', volume_wan='2000')
        elif change == 'missing_name': member['plan'].pop('name')
        elif change == 'message_text': context['messages'][0]['text'] = 'SYN-A 3.09出500万，还可以谈'
        elif change == 'quote_revision': member['quotes']['ofr']['revision'] = 2
        elif change == 'member_identity': member['member_id'] = 'another-member'
        elif change == 'member_version_type': member['version'] = True
        elif change == 'quote_revision_type': member['quotes']['ofr']['revision'] = False
        elif change == 'extra_top_field': context['allow_fake_history'] = True
        elif change == 'preloaded_tool': context['tool_results'] = [{'result': {'status': 'ok'}}]
        else: context['history_results'] = [{'member_id': member['member_id'], 'message_ids': ['invented']}]
        row['context_sha256'] = fingerprint(context)
    path.write_text(''.join(dump(row)+'\n' for row in rows))
    checked = check_run(complete)
    assert not checked['passed'] and not checked['local_recovery']['passed'], checked
    assert any(error.get('stage') == target for error in checked['errors']), checked


@pytest.fixture
def complete_v2(complete):
    """New evidence version; never retrofit a real v1 artifact."""
    source = json.loads((complete/'source.json').read_text())
    source['evidence_version'] = 2
    write(complete/'source.json', source)
    boot = '11111111-2222-4333-8444-555555555555'
    audit = json.loads((complete/'native-recovery-audit.json').read_text())
    sequence = 0
    for target in ('analysis', 'reply'):
        for phase_name in ('fault', 'recover'):
            mutate_phase(complete, target, phase_name, lambda value: value.update(boot_id=boot))
        clock = json.loads((complete/target/'clock.json').read_text())
        path = complete/target/'http-attempts.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        base = next(row['started_at'] for row in rows if row['phase'] == 'fault' and row['stage'] == target)
        for row in rows:
            sequence += 1
            row.update(boot_id=boot, request_trace_id=format(sequence, '032x'),
                started_monotonic=clock['monotonic']+(0 if target == 'analysis' else 10)+row['started_at']-base,
                ended_monotonic=clock['monotonic']+(0 if target == 'analysis' else 10)+row['ended_at']-base)
            entry = next(e for e in audit['entries'] if
                (e['target_stage'], e['phase'], e['stage'], e['round'], e['pid']) ==
                (target, row['phase'], row['stage'], row['round'], row['pid']))
            entry['request_trace_id'] = row['request_trace_id']
            raw_path = complete/entry['artifact']
            raw = json.loads(raw_path.read_text())
            inputs = json.loads(raw['run']['inputs'])
            inputs['sys.query'] = ('Execute the current versioned '+row['stage']+
                ' stage and return its JSON contract. [xunjia_recovery_trace='+row['request_trace_id']+']')
            raw['run']['inputs'] = dump(inputs)
            for index, node in enumerate(raw['nodes']): node['index'] = index
            write(raw_path, raw)
            entry['artifact_sha256'] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        path.write_text(''.join(dump(row)+'\n' for row in rows))
    write(complete/'native-recovery-audit.json', audit)
    return complete


def rewrite_traces(root, target, change):
    path = root/target/'http-attempts.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    change(rows)
    path.write_text(''.join(dump(row)+'\n' for row in rows))


def test_v2_complete_evidence_uses_monotonic_backoff_and_unique_native_query_trace(complete_v2):
    result = check_run(complete_v2)
    assert result['passed'] and result['evidence_version'] == 2, result
    assert len(result['native_linkage']['attempts']) == 5
    assert result['local_recovery']['cases']['analysis']['clock_basis'] == 'monotonic'
    assert result['local_recovery']['cases']['reply']['observed_backoff_seconds'] == 6


def test_v2_wall_clock_rollback_is_preserved_reported_and_never_used_as_retry_elapsed_time(complete_v2):
    def change(rows):
        # All clock evidence remains visible; v2 must prove duration and wait
        # independently rather than clamp or silently replace these values.
        rows[-1]['started_at'] = rows[-2]['ended_at']-20
        rows[-1]['ended_at'] = rows[-1]['started_at']-.027
    rewrite_traces(complete_v2, 'reply', change)
    before = (complete_v2/'reply'/'http-attempts.jsonl').read_bytes()
    result = check_run(complete_v2)
    assert result['passed'], result
    case_result = result['local_recovery']['cases']['reply']
    assert case_result['observed_backoff_seconds'] == 6
    assert case_result['wall_clock_regressions']
    assert (complete_v2/'reply'/'http-attempts.jsonl').read_bytes() == before


def test_v1_negative_wall_duration_still_fails_even_with_no_native_error(complete):
    rewrite_traces(complete, 'reply', lambda rows: rows[-1].update(ended_at=rows[-1]['started_at']-.027))
    result = check_run(complete)
    assert not result['passed'] and any(e['reason'] == 'INVALID_HTTP_TIMING' for e in result['errors'])


@pytest.mark.parametrize('change', ['missing_start', 'missing_end', 'negative_mono', 'reverse_mono',
    'bool_mono', 'nonfinite_mono', 'missing_trace', 'malformed_trace', 'duplicate_trace',
    'missing_boot', 'wrong_boot', 'phase_boot_changed', 'early_monotonic_retry'])
def test_v2_cannot_fall_back_to_wall_time_when_required_clock_or_trace_evidence_is_invalid(complete_v2, change):
    other_boot = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    if change == 'phase_boot_changed':
        mutate_phase(complete_v2, 'analysis', 'recover', lambda value: value.update(boot_id=other_boot))
    def modify(rows):
        row = rows[-1]
        if change == 'missing_start': row.pop('started_monotonic')
        elif change == 'missing_end': row.pop('ended_monotonic')
        elif change == 'negative_mono': row['started_monotonic'] = -1
        elif change == 'reverse_mono': row['ended_monotonic'] = row['started_monotonic']-.01
        elif change == 'bool_mono': row['started_monotonic'] = True
        elif change == 'nonfinite_mono': row['ended_monotonic'] = float('inf')
        elif change == 'missing_trace': row.pop('request_trace_id')
        elif change == 'malformed_trace': row['request_trace_id'] = 'not-a-trace'
        elif change == 'duplicate_trace': row['request_trace_id'] = rows[0]['request_trace_id']
        elif change == 'missing_boot': row.pop('boot_id')
        elif change in ('wrong_boot', 'phase_boot_changed'): row['boot_id'] = other_boot
        else:
            row['started_monotonic'] = rows[0]['ended_monotonic']+.1
            row['ended_monotonic'] = row['started_monotonic']+1
    rewrite_traces(complete_v2, 'analysis', modify)
    result = check_run(complete_v2)
    assert not result['passed'] and not result['local_recovery']['passed'], result


@pytest.mark.parametrize('change', ['missing_tag', 'wrong_tag', 'duplicate_tag', 'audit_trace',
    'missing_index', 'index_bool', 'duplicate_index', 'reversed_index', 'forged_graph', 'changed_context'])
def test_v2_native_tag_graph_context_and_causal_node_indexes_are_all_mandatory(complete_v2, change):
    audit_path = complete_v2/'native-recovery-audit.json'
    audit = json.loads(audit_path.read_text())
    entry = audit['entries'][-1]
    path = complete_v2/entry['artifact']
    raw = json.loads(path.read_text())
    inputs = json.loads(raw['run']['inputs'])
    if change == 'missing_tag': inputs['sys.query'] = 'Execute the current versioned reply stage and return its JSON contract.'
    elif change == 'wrong_tag': inputs['sys.query'] = inputs['sys.query'].replace(entry['request_trace_id'], 'f'*32)
    elif change == 'duplicate_tag': inputs['sys.query'] += ' [xunjia_recovery_trace='+entry['request_trace_id']+']'
    elif change == 'audit_trace': entry['request_trace_id'] = audit['entries'][0]['request_trace_id']
    elif change == 'missing_index': raw['nodes'][0].pop('index')
    elif change == 'index_bool': raw['nodes'][0]['index'] = False
    elif change == 'duplicate_index': raw['nodes'][1]['index'] = raw['nodes'][0]['index']
    elif change == 'reversed_index': raw['nodes'][0]['index'], raw['nodes'][1]['index'] = 2, 1
    elif change == 'forged_graph': raw['workflow']['graph'] += ' '
    else:
        context = json.loads(inputs['context_json']);context['member_id'] = 'someone-else'
        inputs['context_json'] = dump(context)
    raw['run']['inputs'] = dump(inputs)
    write(path, raw);entry['artifact_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(audit_path, audit)
    result = check_run(complete_v2)
    assert not result['passed'] and result['local_recovery']['passed'], result


def test_v2_native_wall_order_cannot_override_real_model_then_contract_node_order(complete_v2):
    audit_path = complete_v2/'native-recovery-audit.json';audit = json.loads(audit_path.read_text())
    entry = audit['entries'][-1];path = complete_v2/entry['artifact'];raw = json.loads(path.read_text())
    raw['run']['finished_at'] = '2000-01-01T00:00:00+00:00'
    raw['nodes'][0]['created_at'] = '1999-01-01T00:00:00+00:00'
    raw['nodes'][0]['finished_at'] = '1998-01-01T00:00:00+00:00'
    write(path, raw);entry['artifact_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest();write(audit_path, audit)
    result = check_run(complete_v2)
    assert result['passed'], result
    assert result['native_linkage']['attempts'][-1]['wall_clock_regressions']


@pytest.mark.parametrize('version', [True, '2', 0, 3])
def test_evidence_version_is_explicit_strict_and_unknown_versions_do_not_downgrade(complete, version):
    source = json.loads((complete/'source.json').read_text());source['evidence_version'] = version
    write(complete/'source.json', source)
    result = check_run(complete)
    assert not result['passed'], result
