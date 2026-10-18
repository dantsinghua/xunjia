"""Real app API measurement on synthetic messages; never constructs an IM sender.

Credentials are read from a private JSON file outside the repository. Source
metadata and full synthetic case results are recorded; credentials never are.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import tempfile
import subprocess

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dify.negotiation_v22.cases import cases,snapshot,reference_snapshot,FixtureClock
from dify.negotiation_v22.measurement import (summarize,distribution,analysis_repair_summary,reply_repair_summary,
    calibration_exclusion_reasons,calibration_summary,path_distributions)
from dify.negotiation_v22.node_code import validate_result
from server.bond_tools import BondTools
from server.negotiation.chatflow import ChatflowClient,FlowUnavailable
from server.negotiation.prices import Price,number


def check_expected(case,observations):
    for expected in case['expected']:
        matches=[o for o in observations if o['member_id']==expected['member'] and o['side']==expected['side']]
        if not matches:raise ValueError('Missing expected independently scoped quote')
        merged={k:v for obs in matches for k,v in obs['changes'].items()}
        for key,value in expected.items():
            if key in ('member','side'):continue
            actual=merged.get('volume_wan') if key=='volume' else merged.get('price',{}).get('value') if key=='price' else merged.get('price',{}).get('kind') if key=='kind' else merged.get(key)
            equal=number(actual)==number(value) if key in ('price','volume') and actual is not None else actual==value
            if not equal:raise ValueError('Semantic mismatch '+key+': expected '+str(value)+', got '+str(actual))


def read_reply_repair_audits(store, owner, member_id):
    """Read the durable job audit before a synthetic measurement DB is removed."""
    with store.db.transaction(write=False) as tx:
        rows = tx.execute("SELECT a.audit_id,a.entity_id,a.created_at,a.data_json FROM xj2_audit a "
                          "JOIN xj2_reply_job j ON j.job_id=a.entity_id "
                          "WHERE a.owner_id=? AND j.member_id=? AND a.kind='reply_text_repair' "
                          "ORDER BY a.created_at,a.audit_id", (owner, member_id)).fetchall()
    return [{'audit_id': row['audit_id'], 'job_id': row['entity_id'], 'created_at': row['created_at'],
             'data': json.loads(row['data_json'])} for row in rows]


def run_case(case,config,timeout,*,stage_budget_seconds=150):
    from server.negotiation.engine import Engine
    from server.negotiation.store import encoded
    from server.negotiation.application_store import ApplicationStore
    from server.negotiation.service import Inquiry
    from server.negotiation.prices import LimitPolicy
    from server.negotiation.facts import Quote
    from dataclasses import replace
    start=time.monotonic();evidence=[]
    record={'case_id':case['id'],'path':case['path'],'success':False,'semantic_ok':False,'timeout':False,'evidence':evidence}
    # The original corpus is explicitly reproducible under its original
    # policy. CLI runs below version its copy to the current production policy.
    policy=case.get('policy_revision','explicit_anchors_v0')
    pending=case.get('expected_reference_confirmation',False)
    record.update(policy_revision=policy,previous_expected=case.get('previous_expected'),
                  expected_reference_confirmation=pending)
    try:
        clients={stage:ChatflowClient(config['source']['base_url'],config['apps'][stage]['key'],stage=stage,timeout=timeout) for stage in ('analysis','reply')}
        class Flows:
            error=None
            def call(self,stage,context):
                record[stage+'_input']=deepcopy(context)
                record.setdefault(stage+'_inputs',[]).append(deepcopy(context))
                round_number=len(record[stage+'_inputs'])
                repair_mode=(context.get('validation_feedback') or {}).get('mode')
                attempt={'round':round_number,
                         'round_kind':'text_repair' if repair_mode=='TEXT_ONLY' else 'evidence_repair' if repair_mode=='EVIDENCE_ONLY' else 'initial' if round_number==1 else 'tool_or_history_followup',
                         'remaining_stage_budget_seconds':context.get('_stage_budget_seconds')}
                try:
                    result=clients[stage].call(context)
                    record[stage]=deepcopy(result)
                    record.setdefault(stage+'_outputs',[]).append(deepcopy(result))
                    validate_result(result,{**context,'schema_version':'2.2','stage':stage},stage)
                    evidence.append({**clients[stage].last_evidence,**attempt})
                    return result
                except Exception as exc:
                    self.error=exc
                    evidence.append({**clients[stage].last_evidence,**attempt,'stage':stage,'failed':True})
                    raise
            def analyze(self,context):return self.call('analysis',context)
            def decide(self,context):return self.call('reply',context)
        class References:
            def snapshot(self):
                return reference_snapshot(case['id'])
        class RecordedTools(BondTools):
            def invoke(self,name,arguments):
                began=time.monotonic()
                response=super().invoke(name,arguments)
                evidence.append({'tool':name,'status':response['status'],'elapsed_ms':round((time.monotonic()-began)*1000,3),
                                 'source':'production BondTools over explicitly synthetic public snapshot',
                                 'arguments':deepcopy(arguments),'result':deepcopy(response)})
                return response
        class SyntheticPublicationCalendar:
            # Independent declared test precondition, not snapshot.as_of or a
            # natural-day freshness heuristic. It never configures production.
            def latest_valuation_day(self,now):return '2026-10-17'
        flows=Flows()
        with tempfile.TemporaryDirectory(prefix='xunjia-dify-v22-') as directory:
            clock=FixtureClock()
            store=ApplicationStore('sqlite:///'+str(Path(directory)/'measurement.db'),initialize=True,clock=clock)
            record['fixture_clock']={'kind':'synthetic trading-day origin plus actual monotonic elapsed seconds','anchor':clock.anchor}
            references=References()
            service=Inquiry(store,reference_provider=references,calendar=SyntheticPublicationCalendar())
            record['fixture_publication_day']='2026-10-17'
            engine=Engine(service,flows,RecordedTools(references),stage_budget_seconds=stage_budget_seconds,
                          context_policy_revision=policy)
            record.update(stage_budget_seconds=engine.stage_budget_seconds,lease_seconds=engine.lease_seconds)
            owner='synthetic-measurement-owner';ids={};seed=snapshot(case);seq=0;scopes={};history_ids={};anchor_ids={}
            opening='3.20' if case['our_side']=='BUY' else '3.00'
            for member in seed['members']:
                suffix=member['member_id'];code=member['bond_code']
                group=store.create_group(owner,bond_code=code,our_side=case['our_side'],target_volume='20000',trade_day='2026-10-17',
                                         policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=True)
                mid,thread=store.add_member(owner,group,account_uid='synthetic-account',peer_uid='synthetic-peer')
                ids[suffix]=mid
                with store.write(owner) as tx:
                    plan={'code':code,'name':code,'opening_yield_pct':opening,'step_bp':'2','volume_wan':'20000','our_side':case['our_side'],'trade_day':'2026-10-17'}
                    tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',(encoded(plan),group))
                    tx.execute('UPDATE xj2_peer SET our_quote_json=? WHERE member_id=?',(encoded({'price':Price('YIELD',opening).dump(),'native_id':'synthetic-history-native'}),mid))
                for index,message in enumerate(member['context']):
                    seq+=1
                    text=message['text']
                    if message['direction']=='out':
                        text=(code+' 继续等您报价' if case.get('required_history') and index>0 else
                              code+' '+opening+'%'+('收' if case['our_side']=='BUY' else '出')+'，请报可做价量，数量按万沟通')
                    if index==len(member['context'])-1:text=code+' '+member['quotes']['ofr']['price']['value']+'出500万'
                    ref=store.ingest(owner,thread,event_id='history-'+str(seq),source_seq=seq,text=text,trade_day='2026-10-17',
                                     occurred_at=clock()-100+seq,direction=message['direction'],native_id='synthetic-native-'+str(seq) if message['direction']=='out' else None)
                    scopes[ref]=[mid];history_ids[suffix]=ref
                    if index==0:anchor_ids[suffix]=ref
                    if message['direction']=='out' and (not case.get('required_history') or index==0):
                        with store.write(owner) as tx:
                            tx.execute('UPDATE xj2_peer SET our_quote_json=? WHERE member_id=?',
                                (encoded({'price':Price('YIELD',opening).dump(),'native_id':'synthetic-native-'+str(seq),'message_id':ref}),mid))
            job=store.claim_analysis(owner,thread,force=True)
            store.commit_analysis(owner,job['analysis_id'],job['lease_token'],scopes=scopes,observations=[])
            with store.write(owner) as tx:
                for member in seed['members']:
                    suffix=member['member_id'];old=Quote.load(member['quotes']['ofr'])
                    store._save_quote(tx,ids[suffix],replace(old,revision=1,evidence={'price':[history_ids[suffix]],'volume_wan':[history_ids[suffix]]}),job['analysis_id'])
            message_text=case['text'].format(history_anchor_id=anchor_ids['a']) if case.get('required_history') else case['text']
            record['fixture_history_anchor_id']=anchor_ids.get('a') if case.get('required_history') else None
            current=store.ingest(owner,thread,event_id='current',source_seq=seq+1,text=message_text,trade_day='2026-10-17',occurred_at=clock())
            start=time.monotonic()
            record['failed_stage']='analysis'
            before_facts={mid:{side:store.quote(owner,mid,side).dump() for side in ('bid','ofr')} for mid in ids.values()}
            analysis_started=time.monotonic()
            saved=engine.analyze_thread(owner,thread,force=True);record['fact_commit']=saved
            record['analysis_elapsed_ms']=round((time.monotonic()-analysis_started)*1000,3)
            record['visible_facts_elapsed_ms']=record['analysis_elapsed_ms']
            record['visible_facts_scope']='synthetic immediate snapshot; force=True; no aggregation wait'
            record['analysis_repair']=deepcopy((saved or {}).get('repair',{}))
            if flows.error:raise flows.error
            if not saved or not saved.get('facts_committed') or (saved.get('rejected') and not pending):
                raise ValueError('Production fact transaction did not accept all observations: '+str(saved.get('rejected',[]) if saved else saved))
            if not record['analysis'].get('observations') and not pending:
                raise ValueError('Positive quote case produced no observations')
            committed=[]
            with store.db.transaction(write=False) as tx:
                for suffix,mid in ids.items():
                    for side in ('bid','ofr'):
                        q=store._quote(tx,mid,side).dump()
                        committed.append({'member_id':suffix,'side':side,'changes':q})
            if pending:
                after_facts={mid:{side:store.quote(owner,mid,side).dump() for side in ('bid','ofr')} for mid in ids.values()}
                if (before_facts!=after_facts or saved.get('accepted') or not saved.get('rejected')
                        or any(row.get('reason')!='REFERENCE_CONFIRMATION_REQUIRED' for row in saved['rejected'])
                        or any(ctx.get('history_results') for ctx in record['analysis_inputs'])):
                    raise ValueError('Window policy must preserve facts and ask confirmation without older-history retrieval')
                record['reference_confirmation_verified']=True
            else:
                check_expected(case,committed)
            if case.get('required_history') and not pending:
                first=next(member for member in record['analysis_inputs'][0]['members'] if member['member_id']==ids['a'])
                last=next(member for member in record['analysis_input']['members'] if member['member_id']==ids['a'])
                first_ids={message['message_id'] for message in first['context']}
                last_ids={message['message_id'] for message in last['context']}
                anchor=anchor_ids['a']
                retrieved=any(row['member_id']==ids['a'] and anchor in row['message_ids']
                              for row in record['analysis_input'].get('history_results',[]))
                referenced=any(anchor in observation.get('evidence',{}).get('price',[])
                               for observation in record['analysis']['observations'])
                if len(first_ids)!=20 or anchor in first_ids or not first_ids<=last_ids or anchor not in last_ids or not retrieved or not referenced:
                    raise ValueError('Explicit older anchor must be retrieved, used and preserve the original twenty messages')
                record['history_verified']=True
            selected=ids['b' if case['id']=='current-bond-wins' else 'a']
            record['failed_stage']='reply'
            reply_started=time.monotonic()
            outcome=engine.decide_member(owner,selected);record['decision_commit']=outcome
            record['reply_elapsed_ms']=round((time.monotonic()-reply_started)*1000,3)
            record['reply_repair_audits']=read_reply_repair_audits(store,owner,selected)
            record['reply_repair']=deepcopy(record['reply_repair_audits'][-1]['data']) if record['reply_repair_audits'] else {}
            if flows.error:raise flows.error
            if not outcome or outcome['state'] not in ('QUEUED','WAIT','HANDOFF') or 'reply' not in record:
                raise ValueError('Production decision did not commit: '+str(outcome))
            decision=record['reply']
            if case.get('reply_kind') and (decision.get('action')!='SEND' or (decision.get('proposal') or {}).get('kind')!=case['reply_kind']):
                raise ValueError('Normal valid scenario must produce '+case['reply_kind'])
            if case.get('required_tool'):
                matching=[event for event in evidence if event.get('tool')==case['required_tool'] and event.get('status')=='ok']
                if not matching:raise ValueError('The requested public tool must actually return usable data')
                if case['required_tool']=='bond_calc' and not any(
                    str(event['result'].get('data',{}).get('result',{}).get('raw_gap_bp')) in ('10','10.00') for event in matching):
                    raise ValueError('Public calculator must verify the stated 3.10/3.20 gap, not an internal boundary')
                record['required_tool_verified']=case['required_tool']
            record['commit_states']={'facts':True,'reply':outcome['state'],'external_im_sent':False}
        record['fixture_database_removed']=not Path(directory).exists()
        if not record['fixture_database_removed']:raise ValueError('Synthetic measurement database cleanup incomplete')
        record.update(success=True,semantic_ok=True)
        record.pop('failed_stage',None)
    except Exception as exc:
        record['error_type']=type(exc).__name__
        record['error_code']=exc.code if isinstance(exc,FlowUnavailable) else str(exc)[:300]
        if isinstance(exc,FlowUnavailable):record['http_status']=exc.status
        record['timeout']=isinstance(exc,FlowUnavailable) and exc.code=='DIFY_TIMEOUT'
        record['timeout_or_network_unknown']=isinstance(exc,FlowUnavailable) and exc.code=='DIFY_TIMEOUT_OR_NETWORK'
    record['elapsed_ms']=round((time.monotonic()-start)*1000,3)
    if any('tool' in event for event in evidence):record['path']='tools'
    record['calibration_exclusion_reasons']=calibration_exclusion_reasons(record)
    record['calibration_eligible']=not record['calibration_exclusion_reasons']
    return record


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cases',nargs='*')
    parser.add_argument('--workers',type=int,default=1,choices=range(1,5))
    parser.add_argument('--timeout',type=int,default=90,choices=range(1,301))
    parser.add_argument('--stage-budget-seconds',type=int,default=150,choices=range(1,541))
    parser.add_argument('--policy-revision',choices=['latest_quote_confirmation_v1','explicit_anchors_v0'],default='latest_quote_confirmation_v1')
    parser.add_argument('--frozen-manifest',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    from dify.negotiation_v22.freeze import freeze_sources,drift
    if args.frozen_manifest is None:
        args.output.mkdir(parents=True,exist_ok=True)
        manifest=freeze_sources(ROOT,args.output/'source')
        command=[sys.executable,str(Path(manifest['frozen_root'])/'scripts/measure_dify_v22.py'),
                 '--credentials',str(args.credentials.resolve()),'--output',str(args.output.resolve()),
                 '--workers',str(args.workers),'--timeout',str(args.timeout),
                 '--stage-budget-seconds',str(args.stage_budget_seconds),
                 '--policy-revision',args.policy_revision,
                 '--frozen-manifest',str((args.output/'source-manifest.json').resolve())]
        if args.cases:command+=['--cases',*args.cases]
        return subprocess.run(command,cwd=manifest['frozen_root']).returncode
    manifest=json.loads(args.frozen_manifest.read_text())
    if Path(manifest['frozen_root']).resolve()!=ROOT or drift(manifest):
        raise ValueError('Frozen measurement source integrity check failed before execution')
    config=json.loads(args.credentials.read_text())
    selected=[c for c in cases() if not args.cases or c['id'] in args.cases]
    if not selected:parser.error('No matching cases')
    for case in selected:
        case['policy_revision']=args.policy_revision
        if case.get('required_history') and args.policy_revision=='latest_quote_confirmation_v1':
            case.update(previous_expected=deepcopy(case['expected']),expected_reference_confirmation=True,reply_kind='CLARIFY')
    args.output.mkdir(parents=True,exist_ok=True)
    source={**config['source'],'kind':'real_model_with_synthetic_counterparty_cases','concurrency':args.workers,'timeout_seconds':args.timeout,
            'stage_budget_seconds':args.stage_budget_seconds,'lease_seconds':args.stage_budget_seconds+30,
            'apps':{stage:{k:v for k,v in app.items() if k!='key'} for stage,app in config['apps'].items()},
            'contract_sha256':hashlib.sha256((ROOT/'dify/negotiation_v22/prompts.py').read_bytes()+(ROOT/'dify/negotiation_v22/node_code.py').read_bytes()).hexdigest(),
            'reference_data':'explicit synthetic public master data, valuation and publication calendar; no customer source connected','external_im_sending':False}
    source['source_manifest_sha256']=manifest['manifest_sha256']
    source['policy_revision']=args.policy_revision
    source['frozen_root']=manifest['frozen_root']
    stamp=datetime.now(timezone.utc).isoformat()
    samples=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(run_case,case,config,args.timeout,stage_budget_seconds=args.stage_budget_seconds):case for case in selected}
        for future in as_completed(futures):
            sample=future.result()
            sample['calibration_exclusion_reasons']=calibration_exclusion_reasons(sample)
            sample['calibration_eligible']=not sample['calibration_exclusion_reasons']
            samples.append(sample)
            target=args.output/(sample['case_id']+'.json');target.write_text(json.dumps(sample,ensure_ascii=False,indent=2));target.chmod(0o600)
            (args.output/'samples.json').write_text(json.dumps(samples,ensure_ascii=False,indent=2))
            print(json.dumps({k:sample.get(k) for k in ('case_id','success','semantic_ok','elapsed_ms','error_type','error_code','calibration_eligible','calibration_exclusion_reasons')},ensure_ascii=False),flush=True)
    successful=[s for s in samples if s['success'] and s['semantic_ok']]
    report={'measurement_id':stamp,'source':source,'attempts':len(samples),'successes':len(successful),'complete':False,
            'failures':len(samples)-len(successful),'failure_rate':(len(samples)-len(successful))/len(samples),
            'timeouts':sum(s['timeout'] for s in samples),'timeout_or_network_unknown':sum(s.get('timeout_or_network_unknown',False) for s in samples),
            'overall':distribution([s['elapsed_ms'] for s in successful]) if successful else None,
            'paths':path_distributions(successful),'calibration':calibration_summary(samples)}
    try:
        report=summarize(samples,measurement_id=stamp,source=source);report['complete']=True
    except ValueError as exc:report['blocker']=str(exc)
    report['all_cases_passed']=len(successful)==len(samples)
    report['stage_timings']={}
    for stage in ('analysis','reply'):
        measured=[s[stage+'_elapsed_ms'] for s in samples if stage+'_elapsed_ms' in s]
        report['stage_timings'][stage]={'measured_attempts':len(measured),
            'distribution':distribution(measured) if measured else None,
            'below_10_seconds':sum(value<10000 for value in measured),
            'failed_cases':sum(not s['success'] and s.get('failed_stage')==stage for s in samples)}
    report['analysis_repair']=analysis_repair_summary(samples)
    report['reply_repair']=reply_repair_summary(samples)
    report['successful_case_ids']=[s['case_id'] for s in successful]
    report['failed_cases']=[{k:s.get(k) for k in ('case_id','failed_stage','error_type','error_code','http_status','timeout','timeout_or_network_unknown')}
                            for s in samples if not s['success'] or not s['semantic_ok']]
    report['source_integrity']={'frozen_drift':drift(manifest),'workspace_drift':drift(manifest,frozen=False)}
    if report['source_integrity']['frozen_drift']:
        report['complete']=False;report['blocker']='Frozen runtime source changed during measurement'
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if report.get('complete') and report['all_cases_passed']:
        (args.output/'aggregation-profile.json').write_text(json.dumps(report['aggregation'],indent=2))
    print('MEASUREMENT_END '+json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if all(s['success'] for s in samples) and not report['source_integrity']['frozen_drift'] else 1


if __name__=='__main__':raise SystemExit(main())
