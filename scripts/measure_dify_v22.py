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
from dify.negotiation_v22.cases import cases,snapshot
from dify.negotiation_v22.measurement import summarize,distribution
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


def run_case(case,config,timeout):
    from server.negotiation.engine import Engine
    from server.negotiation.store import encoded
    from server.negotiation.application_store import ApplicationStore
    from server.negotiation.service import Inquiry
    from server.negotiation.prices import LimitPolicy
    from server.negotiation.facts import Quote
    from server.reference_data import ReferenceSnapshot
    from dataclasses import replace
    start=time.monotonic();evidence=[]
    record={'case_id':case['id'],'path':case['path'],'success':False,'semantic_ok':False,'timeout':False,'evidence':evidence}
    try:
        clients={stage:ChatflowClient(config['source']['base_url'],config['apps'][stage]['key'],stage=stage,timeout=timeout) for stage in ('analysis','reply')}
        class Flows:
            error=None
            def call(self,stage,context):
                record[stage+'_input']=deepcopy(context)
                try:
                    result=clients[stage].call(context)
                    validate_result(result,{**context,'schema_version':'2.2','stage':stage},stage)
                    record[stage]=deepcopy(result);evidence.append(dict(clients[stage].last_evidence))
                    return result
                except Exception as exc:
                    self.error=exc
                    raise
            def analyze(self,context):return self.call('analysis',context)
            def decide(self,context):return self.call('reply',context)
        class References:
            def snapshot(self):
                return ReferenceSnapshot('synthetic-measurement-valuation','2026-10-17',
                    bonds=[{'bond_id':'SYN-A','code':'SYN-A','valuation_yield_pct':'2.5818' if case['id']=='bp-minus' else '2.9872'}],
                    sources=[{'id':'synthetic-fixture','origin':'declared synthetic context; not a live valuation feed'}])
        class RecordedTools(BondTools):
            def invoke(self,name,arguments):
                began=time.monotonic()
                response=super().invoke(name,arguments)
                evidence.append({'tool':name,'status':response['status'],'elapsed_ms':round((time.monotonic()-began)*1000,3),
                                 'source':'production BondTools; public reference adapter unconfigured'})
                return response
        flows=Flows()
        with tempfile.TemporaryDirectory(prefix='xunjia-dify-v22-') as directory:
            store=ApplicationStore('sqlite:///'+str(Path(directory)/'measurement.db'),initialize=True)
            service=Inquiry(store,reference_provider=References())
            engine=Engine(service,flows,RecordedTools())
            owner='synthetic-measurement-owner';ids={};seed=snapshot(case);seq=0;scopes={};history_ids={}
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
                    if message['direction']=='out':text=code+' '+opening+'%'+('收' if case['our_side']=='BUY' else '出')+'，请报可做价量，数量按万沟通'
                    if index==len(member['context'])-1:text=code+' '+member['quotes']['ofr']['price']['value']+'出500万'
                    ref=store.ingest(owner,thread,event_id='history-'+str(seq),source_seq=seq,text=text,trade_day='2026-10-17',
                                     occurred_at=time.time()-100+seq,direction=message['direction'],native_id='synthetic-native-'+str(seq) if message['direction']=='out' else None)
                    scopes[ref]=[mid];history_ids[suffix]=ref
            job=store.claim_analysis(owner,thread,force=True)
            store.commit_analysis(owner,job['analysis_id'],job['lease_token'],scopes=scopes,observations=[])
            with store.write(owner) as tx:
                for member in seed['members']:
                    suffix=member['member_id'];old=Quote.load(member['quotes']['ofr'])
                    store._save_quote(tx,ids[suffix],replace(old,revision=1,evidence={'price':[history_ids[suffix]],'volume_wan':[history_ids[suffix]]}),job['analysis_id'])
            current=store.ingest(owner,thread,event_id='current',source_seq=seq+1,text=case['text'],trade_day='2026-10-17',occurred_at=time.time())
            start=time.monotonic()
            saved=engine.analyze_thread(owner,thread,force=True);record['fact_commit']=saved
            if flows.error:raise flows.error
            if not saved or not saved.get('facts_committed') or saved.get('rejected'):
                raise ValueError('Production fact transaction did not accept all observations: '+str(saved.get('rejected',[]) if saved else saved))
            if not record['analysis'].get('observations'):
                raise ValueError('Positive quote case produced no observations')
            committed=[]
            with store.db.transaction(write=False) as tx:
                for suffix,mid in ids.items():
                    for side in ('bid','ofr'):
                        q=store._quote(tx,mid,side).dump()
                        committed.append({'member_id':suffix,'side':side,'changes':q})
            check_expected(case,committed)
            selected=ids['b' if case['id']=='current-bond-wins' else 'a']
            outcome=engine.decide_member(owner,selected);record['decision_commit']=outcome
            if flows.error:raise flows.error
            if not outcome or outcome['state'] not in ('QUEUED','WAIT','HANDOFF') or 'reply' not in record:
                raise ValueError('Production decision did not commit: '+str(outcome))
            decision=record['reply']
            if case.get('reply_kind') and (decision.get('action')!='SEND' or (decision.get('proposal') or {}).get('kind')!=case['reply_kind']):
                raise ValueError('Normal valid scenario must produce '+case['reply_kind'])
            record['commit_states']={'facts':True,'reply':outcome['state'],'external_im_sent':False}
        record['fixture_database_removed']=not Path(directory).exists()
        if not record['fixture_database_removed']:raise ValueError('Synthetic measurement database cleanup incomplete')
        record.update(success=True,semantic_ok=True)
    except Exception as exc:
        record['error_type']=type(exc).__name__
        record['error_code']=exc.code if isinstance(exc,FlowUnavailable) else str(exc)[:300]
        if isinstance(exc,FlowUnavailable):record['http_status']=exc.status
        record['timeout']=isinstance(exc,FlowUnavailable) and exc.code=='DIFY_TIMEOUT'
        record['timeout_or_network_unknown']=isinstance(exc,FlowUnavailable) and exc.code=='DIFY_TIMEOUT_OR_NETWORK'
    record['elapsed_ms']=round((time.monotonic()-start)*1000,3)
    if any('tool' in event for event in evidence):record['path']='tools'
    return record


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cases',nargs='*')
    parser.add_argument('--workers',type=int,default=1,choices=range(1,5))
    parser.add_argument('--timeout',type=int,default=90,choices=range(1,181))
    parser.add_argument('--frozen-manifest',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    from dify.negotiation_v22.freeze import freeze_sources,drift
    if args.frozen_manifest is None:
        args.output.mkdir(parents=True,exist_ok=True)
        manifest=freeze_sources(ROOT,args.output/'source')
        command=[sys.executable,str(Path(manifest['frozen_root'])/'scripts/measure_dify_v22.py'),
                 '--credentials',str(args.credentials.resolve()),'--output',str(args.output.resolve()),
                 '--workers',str(args.workers),'--timeout',str(args.timeout),
                 '--frozen-manifest',str((args.output/'source-manifest.json').resolve())]
        if args.cases:command+=['--cases',*args.cases]
        return subprocess.run(command,cwd=manifest['frozen_root']).returncode
    manifest=json.loads(args.frozen_manifest.read_text())
    if Path(manifest['frozen_root']).resolve()!=ROOT or drift(manifest):
        raise ValueError('Frozen measurement source integrity check failed before execution')
    config=json.loads(args.credentials.read_text())
    selected=[c for c in cases() if not args.cases or c['id'] in args.cases]
    if not selected:parser.error('No matching cases')
    args.output.mkdir(parents=True,exist_ok=True)
    source={**config['source'],'kind':'real_model_with_synthetic_counterparty_cases','concurrency':args.workers,'timeout_seconds':args.timeout,
            'apps':{stage:{k:v for k,v in app.items() if k!='key'} for stage,app in config['apps'].items()},
            'contract_sha256':hashlib.sha256((ROOT/'dify/negotiation_v22/prompts.py').read_bytes()+(ROOT/'dify/negotiation_v22/node_code.py').read_bytes()).hexdigest(),
            'reference_data':'not connected; declared synthetic price anchors for deterministic normalization','external_im_sending':False}
    source['source_manifest_sha256']=manifest['manifest_sha256']
    source['frozen_root']=manifest['frozen_root']
    stamp=datetime.now(timezone.utc).isoformat()
    samples=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(run_case,case,config,args.timeout):case for case in selected}
        for future in as_completed(futures):
            sample=future.result();samples.append(sample)
            target=args.output/(sample['case_id']+'.json');target.write_text(json.dumps(sample,ensure_ascii=False,indent=2));target.chmod(0o600)
            (args.output/'samples.json').write_text(json.dumps(samples,ensure_ascii=False,indent=2))
            print(json.dumps({k:sample.get(k) for k in ('case_id','success','semantic_ok','elapsed_ms','error_type','error_code')},ensure_ascii=False),flush=True)
    successful=[s for s in samples if s['success'] and s['semantic_ok']]
    report={'measurement_id':stamp,'source':source,'attempts':len(samples),'successes':len(successful),'complete':False,
            'failures':len(samples)-len(successful),'failure_rate':(len(samples)-len(successful))/len(samples),
            'timeouts':sum(s['timeout'] for s in samples),'timeout_or_network_unknown':sum(s.get('timeout_or_network_unknown',False) for s in samples),
            'overall':distribution([s['elapsed_ms'] for s in successful]) if successful else None}
    try:
        report=summarize(samples,measurement_id=stamp,source=source);report['complete']=True
    except ValueError as exc:report['blocker']=str(exc)
    report['all_cases_passed']=len(successful)==len(samples)
    report['successful_case_ids']=[s['case_id'] for s in successful]
    report['failed_cases']=[{k:s.get(k) for k in ('case_id','error_type','error_code','http_status','timeout','timeout_or_network_unknown')}
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
