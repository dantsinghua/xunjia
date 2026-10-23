"""Collect actual HTTP-timeout recovery traces, separate from latency samples.

Only synthetic messages enter a retained local SQLite database. Each phase runs
in a new process. No dispatcher, sender, customer datasource or console API is
constructed. Independent acceptance owns the interpretation of these traces.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dify.negotiation_v22.cases import reference_snapshot, FixtureClock
from dify.negotiation_v22.node_code import validate_result
from server.bond_tools import BondTools
from server.negotiation.application_store import ApplicationStore
from server.negotiation.chatflow import ChatflowClient
from server.negotiation.engine import Engine
from server.negotiation.prices import LimitPolicy, Price
from server.negotiation.service import Inquiry
from server.negotiation.store import encoded
from scripts.measure_dify_v22 import read_reply_repair_audits


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def current_boot_id():
    """Linux boot identity binds monotonic readings across the two processes."""
    return str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))


class RecoveryTraceOpener:
    """Tag only Dify's query envelope; never change the business context/user."""
    def __init__(self, inner, stage, trace_id):
        self.inner, self.stage, self.trace_id = inner, stage, trace_id

    def open(self, request, timeout):
        payload = json.loads(request.data)
        expected = 'Execute the current versioned ' + self.stage + ' stage and return its JSON contract.'
        if payload.get('query') != expected:
            raise ValueError('Unexpected recovery request envelope')
        payload['query'] = expected + ' [xunjia_recovery_trace=' + self.trace_id + ']'
        request.data = encoded(payload).encode('utf8')
        return self.inner.open(request, timeout=timeout)


class References:
    def snapshot(self):
        return reference_snapshot('recovery')


class Calendar:
    def latest_valuation_day(self, now):
        return '2026-10-17'


def open_service(directory):
    clock = FixtureClock(anchor=json.loads((directory / 'clock.json').read_text()))
    store = ApplicationStore('sqlite:///' + str(directory / 'state.db'), initialize=True, clock=clock)
    return store, Inquiry(store, reference_provider=References(), calendar=Calendar())


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / name).exists() for name in ('state.db', 'spec.json', 'clock.json')):
        raise FileExistsError('Recovery evidence output already exists')
    write(directory / 'clock.json', FixtureClock().anchor)
    store, _ = open_service(directory)
    owner = 'synthetic-recovery-owner'
    store.bind_account(owner, 'synthetic-account', 'synthetic-workbench')
    store.set_sending(owner, True)  # Synthetic local commands only; no sender is constructed.
    group = store.create_group(owner, bond_code='SYN-A', our_side='BUY', target_volume='2000',
                               trade_day='2026-10-17', policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
    member, thread = store.add_member(owner, group, account_uid='synthetic-account', peer_uid='synthetic-peer')
    with store.write(owner) as tx:
        plan = {'code': 'SYN-A', 'name': 'SYN-A', 'opening_yield_pct': '3.20', 'step_bp': '2',
                'volume_wan': '2000', 'our_side': 'BUY', 'trade_day': '2026-10-17'}
        tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?', (encoded(plan), group))
    opening = store.ingest(owner, thread, event_id='synthetic-opening', source_seq=1,
                           text='SYN-A 3.20%收，请报可做价量', trade_day='2026-10-17', occurred_at=store.clock()-1,
                           direction='out', native_id='synthetic-native-opening')
    job = store.claim_analysis(owner, thread, force=True)
    store.commit_analysis(owner, job['analysis_id'], job['lease_token'], scopes={opening: [member]}, observations=[])
    with store.write(owner) as tx:
        tx.execute('UPDATE xj2_peer SET our_quote_json=? WHERE member_id=?',
                   (encoded({'price': Price('YIELD', '3.20').dump(), 'native_id': 'synthetic-native-opening',
                             'message_id': opening}), member))
    message = store.ingest(owner, thread, event_id='synthetic-inbound', source_seq=2,
                           text='SYN-A 3.10出500万，还可以谈', trade_day='2026-10-17', occurred_at=store.clock())
    spec = {'owner': owner, 'member_id': member, 'thread_id': thread, 'message_id': message,
            'source': 'explicit synthetic fixture; no customer source', 'external_im_sent': False}
    write(directory / 'spec.json', spec)
    return spec


def state(store, spec, stage):
    table, order = ('xj2_analysis', 'through_revision DESC') if stage == 'analysis' else ('xj2_reply_job', 'created_at DESC')
    with store.db.transaction(write=False) as tx:
        row = tx.execute('SELECT * FROM ' + table + ' ORDER BY ' + order + ' LIMIT 1').fetchone()
        job = dict(row) if row else None
        if job:
            job['lease_token_sha256'] = digest(job.pop('lease_token'))
            for key in ('snapshot_json', 'basis_json'):
                if key in job:
                    job[key + '_sha256'] = digest(json.loads(job.pop(key)))
        quote = store._quote(tx, spec['member_id'], 'ofr').dump()
        quote_rows = [dict(row) for row in tx.execute('SELECT * FROM xj2_quote WHERE member_id=? ORDER BY side,revision', (spec['member_id'],)).fetchall()]
        commands = [dict(row) for row in tx.execute('SELECT * FROM xj2_command ORDER BY created_at').fetchall()]
    raw = store.raw_page(spec['owner'], spec['thread_id'])
    message = next(row for row in raw['messages'] if row['message_id'] == spec['message_id'])
    return {'job': job, 'quote': quote, 'quote_rows': quote_rows, 'message': message,
            'thread': raw['thread'], 'commands': commands,
            'reply_repair_audits': read_reply_repair_audits(store, spec['owner'], spec['member_id'])}


class TracedFlows:
    def __init__(self, config, directory, phase, fault_stage, timeout, stage_budget_seconds, *,
                 wall_clock=time.time, monotonic_clock=time.monotonic, boot_id=None):
        self.directory, self.phase, self.fault_stage = directory, phase, fault_stage
        self.timeout, self.stage_budget_seconds = timeout, stage_budget_seconds
        self.config, self.calls, self.errors = config, [], []
        self.wall_clock, self.monotonic_clock = wall_clock, monotonic_clock
        self.boot_id = current_boot_id() if boot_id is None else boot_id

    def call(self, stage, context):
        injected = self.phase == 'fault' and stage == self.fault_stage
        timeout = 1 if injected else self.timeout
        client = ChatflowClient(self.config['source']['base_url'], self.config['apps'][stage]['key'], stage=stage, timeout=timeout)
        trace_id = uuid.uuid4().hex
        client.opener = RecoveryTraceOpener(client.opener, stage, trace_id)
        stable = deepcopy(context)
        stable.pop('_stage_budget_seconds', None)
        round_number = 1 + sum(row['stage'] == stage for row in self.calls)
        repair_mode = (context.get('validation_feedback') or {}).get('mode')
        attempt = {'stage': stage, 'phase': self.phase, 'pid': os.getpid(),
                   'started_monotonic': self.monotonic_clock(), 'started_at': self.wall_clock(),
                   'boot_id': self.boot_id, 'request_trace_id': trace_id,
                   'round': round_number,
                   'round_kind': 'text_repair' if repair_mode == 'TEXT_ONLY' else 'evidence_repair' if repair_mode == 'EVIDENCE_ONLY' else 'initial' if round_number == 1 else 'tool_or_history_followup',
                   'http_timeout_seconds': timeout, 'stage_budget_seconds': self.stage_budget_seconds,
                   'fault_injection': 'actual HTTP timeout budget of one second' if injected else None,
                   'context_sha256': digest(stable), 'context': stable}
        try:
            result = client.call(context)
            attempt['result'] = deepcopy(result)
            validate_result(result, {**context, 'stage': stage, 'schema_version': '2.2'}, stage)
            return result
        except Exception as exc:
            attempt.update(error_type=type(exc).__name__, error_code=getattr(exc, 'code', 'CONTRACT_VALIDATION_ERROR'))
            self.errors.append(attempt['error_code'])
            raise
        finally:
            attempt.update(evidence=dict(client.last_evidence), ended_at=self.wall_clock(),
                           ended_monotonic=self.monotonic_clock())
            self.calls.append(attempt)
            target = self.directory / 'http-attempts.jsonl'
            with target.open('a') as stream:
                stream.write(json.dumps(attempt, ensure_ascii=False) + '\n')
            target.chmod(0o600)

    def analyze(self, context):
        return self.call('analysis', context)

    def decide(self, context):
        return self.call('reply', context)


def phase(args):
    spec = json.loads((args.output / 'spec.json').read_text())
    config = json.loads(args.credentials.read_text())
    store, service = open_service(args.output)
    flows = TracedFlows(config, args.output, args.phase, args.stage, args.timeout, args.stage_budget_seconds)
    engine = Engine(service, flows, BondTools(References()), stage_budget_seconds=args.stage_budget_seconds)
    result = {'phase': args.phase, 'pid': os.getpid(), 'stage': args.stage, 'boot_id': flows.boot_id,
              'stage_budget_seconds': engine.stage_budget_seconds, 'lease_seconds': engine.lease_seconds,
              'before': state(store, spec, args.stage)}
    if args.phase == 'fault' and args.stage == 'reply':
        result['analysis_commit'] = engine.analyze_thread(spec['owner'], spec['thread_id'], force=True)
        if not result['analysis_commit'] or not result['analysis_commit'].get('facts_committed') or result['analysis_commit'].get('rejected'):
            result['stopped'] = 'Real A did not pass; do not create an unrelated B recovery claim'
            write(args.output / (args.phase + '.json'), result)
            return 1
    result['before_target'] = state(store, spec, args.stage)
    call = (lambda: engine.analyze_thread(spec['owner'], spec['thread_id'], force=True)) if args.stage == 'analysis' else (lambda: engine.decide_member(spec['owner'], spec['member_id']))
    result['outcome'] = call()
    result['after'] = state(store, spec, args.stage)
    calls_before_probe = len(flows.calls)
    result['immediate_repeat'] = call()
    result['repeat_http_attempts'] = len(flows.calls) - calls_before_probe
    result['after_repeat'] = state(store, spec, args.stage)
    result['errors'] = flows.errors
    result['http_attempts'] = len(flows.calls)
    result['external_im_sent'] = False
    write(args.output / (args.phase + '.json'), result)
    print(json.dumps({'stage': args.stage, 'phase': args.phase, 'errors': flows.errors,
                      'job_state': (result['after']['job'] or {}).get('state'), 'pid': os.getpid()}), flush=True)
    return 0


def collect(args):
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.credentials.read_text())
    manifest_path = ROOT.parent / 'source-manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if manifest:
        from dify.negotiation_v22.freeze import drift
        if Path(manifest['frozen_root']).resolve() != ROOT or drift(manifest):
            raise ValueError('Recovery source snapshot integrity check failed')
    write(args.output / 'source.json', {'evidence_version': 2, 'boot_id': current_boot_id(),
        'source': config['source'], 'apps': {
        stage: {key: value for key, value in app.items() if key != 'key'} for stage, app in config['apps'].items()},
        'source_manifest_sha256': manifest['manifest_sha256'] if manifest else None,
        'source_root': str(ROOT),
        'kind': 'fault-injected actual HTTP timeout plus real-model persistent recovery',
        'fixture_clock': 'persistent synthetic trading-day origin plus actual monotonic elapsed seconds; no manual backoff advance',
        'excluded_from_latency_calibration': True, 'external_im_sent': False})
    results = []
    for stage in ('analysis', 'reply'):
        directory = args.output / stage
        prepare(directory)
        command = [sys.executable, str(Path(__file__).resolve()), '--credentials', str(args.credentials.resolve()),
                   '--output', str(directory.resolve()), '--stage', stage, '--timeout', str(args.timeout),
                   '--stage-budget-seconds', str(args.stage_budget_seconds)]
        fault = subprocess.run(command + ['--phase', 'fault'], timeout=2*args.stage_budget_seconds+30)
        first = json.loads((directory / 'fault.json').read_text())
        entry = {'stage': stage, 'first_attempt': first, 'recovery_attempted': False}
        results.append(entry)
        if fault.returncode or first.get('errors') != ['DIFY_TIMEOUT'] or (first['after']['job'] or {}).get('state') != 'RETRY':
            entry['stopped'] = 'The intended real HTTP timeout was not established; no automatic repeated fault injection'
            continue
        persisted_store, _ = open_service(directory)
        remaining = max(0, first['after']['job']['lease_until'] - persisted_store.clock() + .1)
        if remaining > 60:
            entry['stopped'] = 'Unexpected retry backoff; preserve evidence without unbounded waiting'
            continue
        time.sleep(remaining)
        recovered = subprocess.run(command + ['--phase', 'recover'], timeout=args.stage_budget_seconds+30)
        entry['recovery_attempted'] = True
        entry['recovery_process_exit'] = recovered.returncode
        entry['recovery'] = json.loads((directory / 'recover.json').read_text())
        write(args.output / 'report.json', {'cases': results, 'acceptance': 'independent reviewer required'})
    write(args.output / 'report.json', {'cases': results, 'acceptance': 'independent reviewer required',
                                      'frozen_source_drift': drift(manifest) if manifest else None,
                                      'excluded_from_latency_calibration': True, 'external_im_sent': False})
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, choices=range(1, 301), default=175)
    parser.add_argument('--stage-budget-seconds', type=int, choices=range(1, 541), default=360)
    parser.add_argument('--stage', choices=('analysis', 'reply'))
    parser.add_argument('--phase', choices=('fault', 'recover'))
    args = parser.parse_args()
    if bool(args.phase) != bool(args.stage):
        parser.error('--stage and --phase must be supplied together')
    return phase(args) if args.phase else collect(args)


if __name__ == '__main__':
    raise SystemExit(main())
