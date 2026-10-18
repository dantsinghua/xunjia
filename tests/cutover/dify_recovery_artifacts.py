"""Read-only, independent acceptance of retained A/B recovery evidence.

The collector's exit code and summary are not an acceptance result. Native
workflow/model linkage is a separate required artifact; absence is INCOMPLETE.
Only stdlib file/SQLite reads are used. No application or network is started.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3


STAGES = ('analysis', 'reply')
DAY = '2026-10-17'
CLOCK_EPOCH = 1792209600.0


class EvidenceError(ValueError):
    pass


def need(condition, code):
    if not condition:
        raise EvidenceError(code)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse(text):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def nonfinite(_):
        raise EvidenceError('NONFINITE_JSON_NUMBER')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)


def read(path):
    return parse(Path(path).read_text(encoding='utf8'))


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def identity(value):
    return isinstance(value, str) and bool(value)


def fingerprint(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def evidence_version(source):
    version = source.get('evidence_version', 1)
    need(type(version) is int and version in (1, 2), 'UNSUPPORTED_EVIDENCE_VERSION')
    return version


def boot_identity(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value) is not None


def trace_identity(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def number(value, expected):
    try:
        return not isinstance(value, bool) and Decimal(str(value)) == Decimal(str(expected))
    except Exception:
        return False


def contained(root, relative):
    path = (root / relative).resolve()
    need(path.is_relative_to(root.resolve()), 'ARTIFACT_ESCAPES_RUN_DIRECTORY')
    return path


def source_evidence(run_dir):
    source = read(run_dir / 'source.json')
    evidence_version(source)
    need(source.get('excluded_from_latency_calibration') is True and source.get('external_im_sent') is False,
         'SOURCE_BOUNDARY_MISSING')
    need(fingerprint(source.get('source_manifest_sha256')), 'FROZEN_SOURCE_MANIFEST_MISSING')
    root = Path(source['source_root']).resolve()
    manifest = read(root.parent / 'source-manifest.json')
    files = manifest['files']
    need(isinstance(files, dict) and files, 'EMPTY_SOURCE_MANIFEST')
    actual = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    need(actual == manifest['manifest_sha256'] == source['source_manifest_sha256'] and
         Path(manifest['frozen_root']).resolve() == root, 'SOURCE_MANIFEST_IDENTITY_MISMATCH')
    required = {'scripts/measure_dify_recovery.py', 'scripts/measure_dify_v22.py',
                'server/negotiation/engine.py', 'server/negotiation/store.py',
                'server/negotiation/reply_render.py', 'dify/negotiation_v22/cases.py',
                'dify/negotiation_v22/slot_wire.py', 'dify/negotiation_v22/node_code.py',
                'dify/negotiation_v22/build.py', 'dify/negotiation_v22/prompts.py'}
    need(required <= set(files), 'REQUIRED_SOURCE_NOT_FROZEN')
    for relative, expected in files.items():
        path = contained(root, relative)
        need(fingerprint(expected) and path.is_file() and sha(path) == expected, 'FROZEN_SOURCE_CHANGED')
    need(set(source['apps']) == set(STAGES), 'STAGE_APP_IDENTITY_MISSING')
    for app in source['apps'].values():
        need(identity(app.get('app_id')) and identity(app.get('workflow_id')) and
             fingerprint(app.get('graph_sha256')), 'STAGE_GRAPH_IDENTITY_MISSING')
    need(source['apps']['analysis']['app_id'] != source['apps']['reply']['app_id'], 'STAGE_APPS_NOT_SEPARATE')
    return source


def expected_quote(quote):
    need(quote.get('revision') == 1 and type(quote.get('revision')) is int, 'FACT_NOT_COMMITTED_EXACTLY_ONCE')
    need(quote.get('side') == 'ofr' and quote.get('price', {}).get('kind') == 'YIELD' and
         number(quote['price'].get('value'), '3.10') and number(quote.get('volume_wan'), '500') and
         quote.get('assertion') == 'QUOTE' and quote.get('validity') == 'ACTIVE' and
         quote.get('finality') == 'NEGOTIABLE' and quote.get('confirmed_day') == DAY,
         'RECOVERED_FACT_SEMANTICS_WRONG')


def analysis_http_projection(snapshot):
    """The exact persisted-snapshot projection authorized for the initial A call.

    Engine removes private plan fields and _tool_call adds these three fields.
    Everything else, including member facts, identities and raw messages, must
    be byte-equivalent after canonical JSON encoding, not merely a subset.
    """
    expected = deepcopy(snapshot)
    for member in expected['members']:
        member['plan'] = {key: value for key, value in member.get('plan', {}).items()
                          if key in ('name', 'code', 'our_side', 'trade_day')}
    expected.update(context_policy_revision='latest_quote_confirmation_v1',
                    history_results=[], tool_results=[])
    return expected


def final_database(directory, spec, stage, final):
    path = directory / 'state.db'
    need(path.is_file(), 'PERSISTENT_DATABASE_MISSING')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        table, key = ('xj2_analysis', 'analysis_id') if stage == 'analysis' else ('xj2_reply_job', 'job_id')
        rows = connection.execute('SELECT * FROM '+table+' WHERE '+key+'=?', (final['job'][key],)).fetchall()
        need(len(rows) == 1, 'FINAL_JOB_MISSING_FROM_DATABASE')
        job = dict(rows[0])
        job['lease_token_sha256'] = digest(job.pop('lease_token'))
        for field in ('snapshot_json', 'basis_json'):
            if field in job:
                job[field+'_sha256'] = digest(parse(job.pop(field)))
        need(job == final['job'], 'FINAL_JOB_REPORT_DIFFERS_FROM_DATABASE')
        quotes = [dict(row) for row in connection.execute(
            'SELECT * FROM xj2_quote WHERE member_id=? ORDER BY side,revision', (spec['member_id'],))]
        need(quotes == final['quote_rows'] and len(quotes) == 1, 'QUOTE_ROWS_MISSING_DUPLICATED_OR_CHANGED')
        need(parse(quotes[0]['state_json']) == final['quote'], 'QUOTE_REPORT_DIFFERS_FROM_DATABASE')
        commands = [dict(row) for row in connection.execute('SELECT * FROM xj2_command ORDER BY created_at')]
        need(commands == final['commands'], 'COMMAND_REPORT_DIFFERS_FROM_DATABASE')
        raw = connection.execute('SELECT * FROM xj2_message WHERE message_id=?', (spec['message_id'],)).fetchone()
        need(raw is not None and all(final['message'].get(k) == v for k, v in dict(raw).items()),
             'RAW_MESSAGE_REPORT_DIFFERS_FROM_DATABASE')
        thread = connection.execute('SELECT * FROM xj2_thread WHERE thread_id=?', (spec['thread_id'],)).fetchone()
        need(thread is not None and dict(thread) == final['thread'], 'THREAD_REPORT_DIFFERS_FROM_DATABASE')
        rows = connection.execute('SELECT * FROM xj2_analysis WHERE thread_id=? AND through_revision=?',
                                  (spec['thread_id'], final['message']['ingest_revision'])).fetchall()
        need(len(rows) == 1 and rows[0]['state'] == 'COMMITTED', 'REAL_A_COMMIT_MISSING_OR_DUPLICATED')
        need(rows[0]['analysis_id'] == quotes[0]['analysis_id'], 'QUOTE_NOT_BOUND_TO_REAL_A_COMMIT')
        return dict(rows[0])
    finally:
        connection.close()


def attempts(directory, phases, spec, target, version=1):
    rows = [parse(line) for line in (directory / 'http-attempts.jsonl').read_text().splitlines() if line.strip()]
    need(bool(rows), 'HTTP_ATTEMPTS_MISSING')
    keys, trace_ids = set(), set()
    for row in rows:
        key = (target, row['phase'], row['stage'], row['round'], row['pid'])
        need(key not in keys, 'DUPLICATE_HTTP_ATTEMPT')
        keys.add(key)
        need(row['phase'] in phases and row['stage'] in STAGES and type(row['round']) is int and row['round'] >= 1,
             'INVALID_HTTP_ATTEMPT_IDENTITY')
        need(row['pid'] == phases[row['phase']]['pid'], 'HTTP_PID_DIFFERS_FROM_PHASE')
        need(finite(row['started_at']) and finite(row['ended_at']) and
             (version == 2 or row['ended_at'] >= row['started_at']),
             'INVALID_HTTP_TIMING')
        if version == 2:
            start, end = row.get('started_monotonic'), row.get('ended_monotonic')
            need(finite(start) and finite(end) and 0 <= start <= end, 'INVALID_MONOTONIC_HTTP_TIMING')
            need(boot_identity(row.get('boot_id')) and row['boot_id'] == phases[row['phase']]['boot_id'],
                 'HTTP_BOOT_DIFFERS_FROM_PHASE')
            trace = row.get('request_trace_id')
            need(trace_identity(trace) and trace not in trace_ids, 'INVALID_OR_REUSED_REQUEST_TRACE')
            trace_ids.add(trace)
        need(digest(row['context']) == row['context_sha256'], 'HTTP_CONTEXT_DIGEST_MISMATCH')
        context = row['context']
        need(context.get('context_policy_revision') == 'latest_quote_confirmation_v1' and
             context.get('thread_id') == spec['thread_id'], 'HTTP_CONTEXT_WRONG_POLICY_OR_THREAD')
        if row['stage'] == 'reply':
            need(context.get('member_id') == spec['member_id'], 'HTTP_CONTEXT_WRONG_MEMBER')
        else:
            need({member['member_id'] for member in context.get('members', [])} == {spec['member_id']},
                 'HTTP_CONTEXT_WRONG_MEMBER')
        need(row.get('round_kind') in ('initial', 'tool_or_history_followup', 'evidence_repair', 'text_repair'),
             'HTTP_ROUND_KIND_MISSING')
        need(finite(row.get('http_timeout_seconds')) and 1 <= row['http_timeout_seconds'] <= 300 and
             row['stage_budget_seconds'] == phases[row['phase']]['stage_budget_seconds'], 'HTTP_BUDGET_MISMATCH')
        if 'error_code' not in row:
            need(isinstance(row.get('result'), dict) and row['result'].get('schema_version') == '2.2' and
                 not row.get('evidence', {}).get('error_code'), 'HTTP_SUCCESS_WITHOUT_RESULT')
    for phase, report in phases.items():
        actual = [row for row in rows if row['phase'] == phase]
        need(type(report['http_attempts']) is int and report['http_attempts'] == len(actual), 'HTTP_ATTEMPT_COUNT_MISMATCH')
        for stage in STAGES:
            selected = [row for row in actual if row['stage'] == stage]
            need([row['round'] for row in selected] == list(range(1, len(selected)+1)), 'HTTP_ROUNDS_NOT_CONTIGUOUS')
    return rows


def check_case(run_dir, entry, version=1):
    stage = entry['stage']
    directory = run_dir / stage
    spec, clock = read(directory / 'spec.json'), read(directory / 'clock.json')
    need(all(identity(spec.get(key)) for key in ('owner', 'member_id', 'thread_id', 'message_id')) and
         spec.get('external_im_sent') is False, 'SPEC_IDENTITY_OR_SEND_BOUNDARY_MISSING')
    need(clock.get('epoch') == CLOCK_EPOCH and finite(clock.get('monotonic')) and clock['monotonic'] >= 0,
         'SYNTHETIC_CLOCK_ANCHOR_CHANGED')
    fault, recover = read(directory / 'fault.json'), read(directory / 'recover.json')
    need(digest(entry.get('first_attempt')) == digest(fault) and digest(entry.get('recovery')) == digest(recover) and
         entry.get('recovery_attempted') is True and type(entry.get('recovery_process_exit')) is int and
         entry['recovery_process_exit'] == 0 and not entry.get('stopped'),
         'RECOVERY_NOT_COMPLETED_OR_REPORT_DIVERGED')
    phases = {'fault': fault, 'recover': recover}
    if version == 2:
        need(boot_identity(fault.get('boot_id')) and fault['boot_id'] == recover.get('boot_id'),
             'RETRY_MONOTONIC_BOOT_CHANGED_OR_MISSING')
    for phase, report in phases.items():
        need(report.get('phase') == phase and report.get('stage') == stage and
             type(report.get('pid')) is int and report['pid'] > 0 and report.get('external_im_sent') is False,
             'PHASE_IDENTITY_OR_SEND_BOUNDARY_MISSING')
        need(finite(report.get('stage_budget_seconds')) and 1 <= report['stage_budget_seconds'] <= 540 and
             report.get('lease_seconds') == math.ceil(report['stage_budget_seconds'])+30, 'PHASE_LEASE_BUDGET_MISMATCH')
        need(report.get('immediate_repeat') is None and type(report.get('repeat_http_attempts')) is int and
             report['repeat_http_attempts'] == 0 and report.get('after_repeat') == report.get('after'),
             'IMMEDIATE_REPEAT_CHANGED_STATE_OR_CALLED_HTTP')
    need(fault['pid'] != recover['pid'], 'RECOVERY_DID_NOT_RESTART_PROCESS')
    need(recover['stage_budget_seconds'] == fault['stage_budget_seconds'], 'STAGE_BUDGET_CHANGED_DURING_RECOVERY')
    need(fault.get('errors') == ['DIFY_TIMEOUT'] and recover.get('errors') == [], 'INTENDED_TIMEOUT_OR_RECOVERY_SUCCESS_NOT_PROVEN')
    need(recover['before'] == recover['before_target'] == fault['after'], 'RECOVERY_DID_NOT_REOPEN_SAME_PERSISTED_STATE')
    first, last = fault['after']['job'], recover['after']['job']
    key, immutable = ('analysis_id', 'snapshot_json_sha256') if stage == 'analysis' else ('job_id', 'basis_json_sha256')
    need(first['state'] == 'RETRY' and last['state'] == ('COMMITTED' if stage == 'analysis' else 'DONE'),
         'JOB_DID_NOT_RECOVER_TO_REQUIRED_TERMINAL_STATE')
    need(identity(first.get(key)) and first[key] == last[key] and first[immutable] == last[immutable] and
         fingerprint(first[immutable]), 'JOB_OR_IMMUTABLE_INPUT_CHANGED')
    need(type(first.get('attempts')) is int and first['attempts'] == 1 and
         type(last.get('attempts')) is int and last['attempts'] == 2, 'RETRY_ATTEMPT_SEQUENCE_WRONG')
    need(fingerprint(first.get('lease_token_sha256')) and fingerprint(last.get('lease_token_sha256')) and
         first['lease_token_sha256'] != last['lease_token_sha256'], 'RETRY_LEASE_TOKEN_NOT_REPLACED')
    need(finite(first.get('lease_until')) and finite(last.get('lease_until')) and
         last['lease_until'] > first['lease_until'], 'RETRY_LEASE_DID_NOT_ADVANCE')
    rows = attempts(directory, phases, spec, stage, version)
    failed = [row for row in rows if row['phase'] == 'fault' and row['stage'] == stage]
    recovered = [row for row in rows if row['phase'] == 'recover']
    need(len(failed) == 1 and recovered and all(row['stage'] == stage for row in recovered),
         'TARGET_TIMEOUT_NOT_UNIQUE_OR_RECOVERY_RERAN_OTHER_STAGE')
    original = failed[0]
    need(original.get('error_code') == original.get('evidence', {}).get('error_code') == 'DIFY_TIMEOUT' and
         original.get('http_timeout_seconds') == 1 and original.get('fault_injection'), 'ACTUAL_HTTP_TIMEOUT_NOT_PROVEN')
    need(all('error_code' not in row for row in rows if row is not original), 'UNEXPECTED_HTTP_FAILURE')
    need(original['context_sha256'] == recovered[0]['context_sha256'], 'RETRY_INITIAL_CONTEXT_CHANGED')
    wait = (recovered[0]['started_monotonic'] - original['ended_monotonic'] if version == 2 else
            recovered[0]['started_at'] - original['ended_at'])
    minimum = 2 if stage == 'analysis' else 5
    need(wait >= minimum and first['lease_until'] >= first['created_at']+minimum,
         'REAL_RETRY_BACKOFF_NOT_OBSERVED')
    if version == 2:
        need(clock['epoch'] + recovered[0]['started_monotonic'] - clock['monotonic'] >= first['lease_until'],
             'MONOTONIC_RETRY_PRECEDES_PERSISTED_DEADLINE')
    for frame in (fault['before_target'], fault['after'], recover['after']):
        need(frame['message']['message_id'] == spec['message_id'] and
             frame['message']['thread_id'] == frame['thread']['thread_id'] == spec['thread_id'] and
             frame['message']['text'] == 'SYN-A 3.10出500万，还可以谈', 'FIXTURE_RAW_INPUT_CHANGED')
    final = recover['after']
    expected_quote(final['quote'])
    need(final['message']['analysis_state'] == 'ANALYZED' and
         final['thread']['analyzed_revision'] == final['thread']['ingest_revision'], 'INPUT_LEFT_PENDING_OR_REJECTED')
    if stage == 'analysis':
        need(fault['outcome'].get('facts_committed') is False and recover['outcome'].get('facts_committed') is True and
             not recover['outcome'].get('rejected') and len(recover['outcome'].get('accepted', [])) == 1,
             'A_FACT_COMMIT_NOT_EXACTLY_ONCE')
        need(fault['outcome'].get('analysis_id') == first[key] and first['thread_id'] == last['thread_id'] == spec['thread_id'] and
             first['through_revision'] == final['message']['ingest_revision'], 'A_TARGET_JOB_OR_WATERMARK_MISMATCH')
        need(fault['after']['quote'] == fault['before_target']['quote'] and
             fault['after']['quote']['revision'] == 0 and fault['after']['quote_rows'] == [] and
             fault['after']['message']['analysis_state'] == 'QUEUED', 'A_TIMEOUT_CHANGED_FACTS_OR_LOST_RAW_INPUT')
        need(first['from_revision'] == last['from_revision'] and first['through_revision'] == last['through_revision'],
             'A_SNAPSHOT_WATERMARK_CHANGED')
        need(all(frame['commands'] == [] for frame in (fault['before_target'], fault['after'], final)), 'A_RECOVERY_CREATED_COMMAND')
    else:
        need(fault.get('analysis_commit', {}).get('facts_committed') is True and
             not fault['analysis_commit'].get('rejected') and len(fault['analysis_commit'].get('accepted', [])) == 1,
             'B_RECOVERY_HAS_NO_VALID_REAL_A_COMMIT')
        expected_quote(fault['before_target']['quote'])
        need(fault['outcome'].get('state') == 'RETRY' and recover['outcome'].get('state') == 'QUEUED',
             'B_RECOVERY_DID_NOT_QUEUE_A_VALID_REPLY')
        need(first['request_id'] == last['request_id'] and first['member_id'] == last['member_id'] == spec['member_id'],
             'B_REQUEST_IDENTITY_CHANGED')
        need(fault['before_target']['quote'] == fault['after']['quote'] == final['quote'] and
             fault['before_target']['quote_rows'] == fault['after']['quote_rows'] == final['quote_rows'], 'B_RETRY_CHANGED_A_FACTS')
        need(fault['before_target']['commands'] == fault['after']['commands'] == [] and len(final['commands']) == 1,
             'B_CANDIDATE_NOT_UNIQUE')
        command = final['commands'][0]
        need(command['command_id'] == recover['outcome']['command_id'] and command['state'] == 'QUEUED' and
             command['request_id'] == last['request_id'] and
             command['member_id'] == spec['member_id'] and command['thread_id'] == spec['thread_id'] and
             command.get('native_id') is None and command.get('sent_at') is None, 'B_COMMAND_IDENTITY_OR_NO_SEND_BOUNDARY_WRONG')
        body = parse(command['body_json'])
        proposal = recovered[-1]['result'].get('proposal', {})
        need(recovered[-1]['result'].get('action') == 'SEND' and body.get('kind') == proposal.get('kind') == 'REPLY' and
             body.get('text') == proposal.get('text') and identity(body.get('text')), 'B_NATIVE_BODY_DIFFERS_FROM_REAL_RESPONSE')
        price = body.get('our_price', {})
        bounds = recovered[0]['context']['quote_constraints']
        amount = Decimal(str(price.get('value')))
        need(price.get('kind') == proposal.get('our_price', {}).get('kind') == 'YIELD' and
             number(price['value'], proposal['our_price']['value']) and bounds['status'] == 'AVAILABLE' and
             Decimal(bounds['min_yield_pct']) <= amount <= Decimal(bounds['max_yield_pct']) and amount % Decimal('.01') == 0,
             'B_REPLY_OUTSIDE_VALIDATED_QUOTE_DOMAIN')
        need(digest(recovered[0]['context']['basis']) == last[immutable] == digest(parse(command['basis_json'])),
             'B_COMMAND_BASIS_MISMATCH')
        need(any(row['stage'] == 'analysis' for row in rows if row['phase'] == 'fault'), 'B_REAL_A_HTTP_TRACE_MISSING')
    database_analysis = final_database(directory, spec, stage, final)
    snapshot = parse(database_analysis['snapshot_json'])
    expected_analysis = digest(analysis_http_projection(snapshot))
    initial_a = [row for row in rows if row['stage'] == 'analysis' and row['round'] == 1]
    # Include the real A commit preceding B's fault; B recovery must not hide
    # a leaked or altered preparation request behind its own correct basis.
    need(initial_a and all(digest(row['context']) == expected_analysis for row in initial_a),
         'A_HTTP_DIFFERS_FROM_PERSISTED_SNAPSHOT')
    clock_details = {}
    if version == 2:
        regressions = [{'kind': 'HTTP_NEGATIVE_WALL_DURATION', 'phase': row['phase'], 'stage': row['stage'],
                        'round': row['round'], 'delta_seconds': row['ended_at']-row['started_at']}
                       for row in rows if row['ended_at'] < row['started_at']]
        wall_wait = recovered[0]['started_at'] - original['ended_at']
        if wall_wait < 0:
            regressions.append({'kind': 'NEGATIVE_WALL_BACKOFF', 'delta_seconds': wall_wait})
        clock_details = {'clock_basis': 'monotonic', 'boot_id': fault['boot_id'],
            'observed_wall_backoff_seconds': wall_wait, 'wall_clock_regressions': regressions,
            'http_timings': [{'phase': row['phase'], 'stage': row['stage'], 'round': row['round'],
                             'elapsed_seconds': row['ended_monotonic']-row['started_monotonic']} for row in rows]}
    return {'passed': True, 'job_id': first[key], 'attempts': [1, 2], 'pids': [fault['pid'], recover['pid']],
            'observed_backoff_seconds': wait, 'http_attempts': len(rows), **clock_details}, rows


def timestamp(value):
    need(isinstance(value, str) and value, 'NATIVE_TIMESTAMP_MISSING')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def native_evidence(run_dir, source, all_attempts):
    version = evidence_version(source)
    path = run_dir / 'native-recovery-audit.json'
    if not path.is_file():
        return {'status': 'INCOMPLETE', 'pending': ['NATIVE_WORKFLOW_AND_MODEL_LINKAGE_REQUIRED'], 'attempts': []}
    audit = read(path)
    need(audit.get('schema_version') == '1' and isinstance(audit.get('entries'), list), 'NATIVE_AUDIT_SCHEMA_WRONG')
    expected = {(target, row['phase'], row['stage'], row['round'], row['pid']): row
                for target, rows in all_attempts.items() for row in rows}
    seen, run_ids, checked = set(), set(), []
    for entry in audit['entries']:
        need(type(entry.get('round')) is int and type(entry.get('pid')) is int, 'NATIVE_ATTEMPT_IDENTITY_TYPE_WRONG')
        key = tuple(entry[k] for k in ('target_stage', 'phase', 'stage', 'round', 'pid'))
        need(key in expected and key not in seen, 'NATIVE_ATTEMPT_MISSING_DUPLICATE_OR_EXTRA')
        seen.add(key)
        attempt = expected[key]
        need(entry['context_sha256'] == attempt['context_sha256'], 'NATIVE_CONTEXT_DIGEST_MISMATCH')
        artifact = contained(run_dir, entry['artifact'])
        need(artifact.is_file() and sha(artifact) == entry['artifact_sha256'], 'NATIVE_RAW_ARTIFACT_MISSING_OR_CHANGED')
        raw = read(artifact)
        need(raw.get('database_timezone') == 'UTC', 'NATIVE_TIMEZONE_UNDECLARED')
        workflow, native, nodes = raw['workflow'], raw['run'], raw['nodes']
        app = source['apps'][attempt['stage']]
        need(entry['app_id'] == workflow['app_id'] == native['app_id'] == app['app_id'] and
             native['workflow_id'] == workflow['id'] == app['workflow_id'], 'NATIVE_APP_OR_WORKFLOW_MISMATCH')
        need(native['id'] == entry['workflow_run_id'] and native['id'] not in run_ids, 'NATIVE_RUN_REUSED_OR_MISMATCHED')
        returned_id = attempt.get('evidence', {}).get('workflow_run_id')
        need(returned_id is None or returned_id == native['id'], 'NATIVE_RUN_DIFFERS_FROM_RETURNED_HTTP_ID')
        run_ids.add(native['id'])
        need(isinstance(workflow['graph'], str) and hashlib.sha256(workflow['graph'].encode()).hexdigest() == app['graph_sha256'],
             'NATIVE_PUBLISHED_GRAPH_HASH_MISMATCH')
        graph = parse(workflow['graph'])
        need(digest(parse(native['graph'])) == digest(graph), 'NATIVE_EXECUTED_GRAPH_MISMATCH')
        request = parse(native['inputs'])
        if version == 2:
            trace = attempt['request_trace_id']
            need(entry.get('request_trace_id') == trace, 'NATIVE_REQUEST_TRACE_MISMATCH')
            expected_query = ('Execute the current versioned '+attempt['stage']+
                ' stage and return its JSON contract. [xunjia_recovery_trace='+trace+']')
            need(request.get('sys.query') == expected_query, 'NATIVE_QUERY_TRACE_MISMATCH')
            indexes = [node.get('index') for node in nodes]
            need(all(type(index) is int and index >= 0 for index in indexes) and
                 len(set(indexes)) == len(indexes), 'NATIVE_NODE_INDEX_MISSING_INVALID_OR_DUPLICATED')
        context = parse(request['context_json'])
        want = dict(attempt['context'])
        want.pop('_stage_budget_seconds', None)
        want.update(schema_version='2.2', stage=attempt['stage'])
        context.pop('_stage_budget_seconds', None)
        need(digest(context) == digest(want), 'NATIVE_INPUT_DIFFERS_FROM_ACTUAL_HTTP')
        original_context = dict(context)
        for field in ('schema_version', 'stage'):
            if field not in attempt['context']:
                original_context.pop(field)
        need(digest(original_context) == attempt['context_sha256'], 'NATIVE_CONTEXT_DIGEST_MISMATCH')
        created = timestamp(native['created_at']).timestamp()
        # In the declared UTC clock domain, a successful run must start while
        # that HTTP call is active. A timed-out call can start later, but must
        # already be identifiable before its next recovery request is sent.
        if version == 1:
            upper = attempt['ended_at']
            if 'error_code' in attempt:
                upper = min(row['started_at'] for row in all_attempts[key[0]]
                            if row['phase'] == 'recover' and row['stage'] == attempt['stage'])
            in_window = attempt['started_at'] <= created and (created < upper if 'error_code' in attempt else created <= upper)
            need(in_window, 'NATIVE_RUN_OUTSIDE_REQUEST_TIME_WINDOW')
        model_ids = {node['id'] for node in graph['nodes'] if node.get('data', {}).get('type') == 'llm'}
        model_nodes = [node for node in nodes if node.get('node_id') in model_ids and node.get('node_type') == 'llm']
        need(len(model_ids) == len(model_nodes) == 1, 'ACTUAL_MODEL_NODE_NOT_UNIQUELY_PROVEN')
        model = model_nodes[0]
        need(model['status'] in ('running', 'succeeded', 'failed', 'exception'), 'MODEL_NODE_NOT_ENTERED')
        wall_regressions = []
        def wall_order(condition, code):
            if version == 1: need(condition, code)
            elif not condition: wall_regressions.append(code)
        wall_order(timestamp(model['created_at']) >= timestamp(native['created_at']), 'NATIVE_MODEL_TIME_ORDER_INVALID')
        if native.get('finished_at'):
            wall_order(timestamp(native['finished_at']) >= timestamp(native['created_at']), 'NATIVE_RUN_TIME_ORDER_INVALID')
        contracts = [node for node in nodes if node.get('node_id') == 'contract']
        if version == 2:
            need(all(model['index'] < node['index'] for node in contracts), 'NATIVE_MODEL_CONTRACT_ORDER_INVALID')
        if 'error_code' not in attempt:
            need(native['status'] == model['status'] == 'succeeded' and native.get('finished_at') and model.get('finished_at'),
                 'SUCCESSFUL_HTTP_HAS_NO_SUCCESSFUL_NATIVE_MODEL')
            wall_order(timestamp(model['finished_at']) >= timestamp(model['created_at']), 'NATIVE_MODEL_TIME_ORDER_INVALID')
            output = parse(model['outputs'])
            need(identity(output.get('text')), 'NATIVE_MODEL_OUTPUT_MISSING')
            need(len(contracts) == 1 and contracts[0]['status'] == 'succeeded', 'NATIVE_CONTRACT_NOT_SUCCESSFUL')
            contract_input, contract_output = parse(contracts[0]['inputs']), parse(contracts[0]['outputs'])
            need(contract_input['text'] == output['text'] and digest(parse(contract_input['context_json'])) == digest(want),
                 'MODEL_OUTPUT_NOT_LINKED_TO_NATIVE_CONTRACT')
            need(digest(parse(contract_output['result'])) == digest(attempt['result']), 'NATIVE_CANONICAL_RESULT_DIFFERS_FROM_HTTP')
        checked.append({'key': list(key), 'workflow_run_id': native['id'], 'workflow_status': native['status'],
                        'model_status': model['status'], 'artifact': str(artifact),
                        **({'request_trace_id': attempt['request_trace_id'],
                            'causal_basis': 'request_trace_id+node.index',
                            'wall_clock_regressions': wall_regressions} if version == 2 else {})})
    need(seen == set(expected), 'NOT_EVERY_HTTP_ATTEMPT_HAS_NATIVE_EVIDENCE')
    return {'status': 'PASS', 'pending': [], 'attempts': checked}


def check_run(run_dir):
    run_dir = Path(run_dir).resolve()
    errors, cases, traces = [], {}, {}
    local_passed = False
    source = None
    native = {'status': 'INCOMPLETE', 'pending': ['LOCAL_EVIDENCE_NOT_VALIDATED'], 'attempts': []}
    try:
        source = source_evidence(run_dir)
        report = read(run_dir / 'report.json')
        need(report.get('frozen_source_drift') == [] and report.get('excluded_from_latency_calibration') is True and
             report.get('external_im_sent') is False, 'FINAL_SOURCE_OR_SEND_BOUNDARY_NOT_PROVEN')
        entries = report['cases']
        need(isinstance(entries, list) and len(entries) == 2 and {entry['stage'] for entry in entries} == set(STAGES),
             'BOTH_RECOVERY_STAGES_REQUIRED')
        for entry in entries:
            stage = entry['stage']
            try:
                cases[stage], traces[stage] = check_case(run_dir, entry, evidence_version(source))
            except (EvidenceError, ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, sqlite3.Error, ArithmeticError) as exc:
                errors.append({'stage': stage, 'reason': str(exc) if isinstance(exc, EvidenceError) else 'MISSING_OR_MALFORMED_LOCAL_EVIDENCE'})
                cases[stage] = {'passed': False}
        if not errors:
            if evidence_version(source) == 2:
                trace_ids = [row['request_trace_id'] for rows in traces.values() for row in rows]
                need(len(set(trace_ids)) == len(trace_ids), 'REQUEST_TRACE_REUSED_ACROSS_CASES')
            local_passed = True
            try:
                native = native_evidence(run_dir, source, traces)
            except (EvidenceError, ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, ArithmeticError) as exc:
                reason = str(exc) if isinstance(exc, EvidenceError) else 'MISSING_OR_MALFORMED_NATIVE_EVIDENCE'
                errors.append({'stage': 'native', 'reason': reason})
                native = {'status': 'FAIL', 'pending': [], 'attempts': []}
    except (EvidenceError, ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, sqlite3.Error, ArithmeticError) as exc:
        errors.append({'stage': 'artifact', 'reason': str(exc) if isinstance(exc, EvidenceError) else 'MISSING_OR_MALFORMED_REQUIRED_EVIDENCE'})
    status = 'FAIL' if errors else native['status']
    paths = [run_dir/'report.json', run_dir/'source.json', run_dir/'native-recovery-audit.json']
    paths += [run_dir/stage/name for stage in STAGES for name in
              ('spec.json', 'clock.json', 'state.db', 'fault.json', 'recover.json', 'http-attempts.jsonl')]
    paths += [Path(row['artifact']) for row in native.get('attempts', [])]
    version = evidence_version(source) if source is not None else None
    return {'schema_version': '1', 'evidence_version': version, 'status': status, 'passed': status == 'PASS', 'run_dir': str(run_dir),
            'checker_sha256': sha(Path(__file__)),
            'source_manifest_sha256': source.get('source_manifest_sha256') if source else None,
            'input_sha256': {str(path): sha(path) for path in paths if path.is_file()},
            'local_recovery': {'passed': local_passed, 'cases': cases},
            'native_linkage': native, 'errors': errors,
            'limits': ['Fault-native entry proves model-stage entry, not physical model success.',
                       ('Same-boot monotonic backoff plus persistent lease is checked; arbitrary clock evidence tampering is not attested.'
                        if version == 2 else 'Wall-time backoff plus persistent lease is checked; arbitrary wall-clock tampering is not attested.'),
                       ('Native linkage uses exact query trace, frozen graph/context and node indexes; wall clock ordering is diagnostic only.'
                        if version == 2 else 'Native request-time linkage assumes the explicitly declared UTC database and client wall clocks agree.'),
                       'Synthetic retained-state recovery is separate from latency calibration and older failed-case databases.']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check_run(args.run_dir)
    with args.output.open('x', encoding='utf8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    args.output.chmod(0o600)
    print(json.dumps({'status': result['status'], 'passed': result['passed'], 'report': str(args.output)}, ensure_ascii=False))
    return {'PASS': 0, 'FAIL': 1, 'INCOMPLETE': 2}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
