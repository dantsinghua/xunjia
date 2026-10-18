"""Real A/B App API over synthetic cash-settlement conversations, without IM I/O.

The SENT receipt is explicitly synthetic and only touches the per-case SQLite
fixture. Source, every HTTP attempt, facts, questions and final candidates are
retained. These multi-turn samples are excluded from aggregation calibration.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dify.negotiation_v22.cases import DAY, FixtureClock, reference_snapshot
from dify.negotiation_v22.freeze import freeze_sources, drift
from server.bond_tools import BondTools
from server.negotiation.application_store import ApplicationStore
from server.negotiation.chatflow import ChatflowClient
from server.negotiation.engine import Engine
from server.negotiation.prices import LimitPolicy, Price
from server.negotiation.reference import HTTPReferenceProvider
from server.negotiation.service import Inquiry
from server.negotiation.store import encoded

SCENARIOS=(
    {'id':'cash-net-t1','kind':'NET_PRICE','answer':'T+1','settlement':'2026-10-20','wait':False},
    {'id':'cash-dirty-t0','kind':'DIRTY_PRICE','answer':'T+0','settlement':DAY,'wait':False},
    {'id':'cash-net-date-after-wait','kind':'NET_PRICE','answer':'2026-10-20','settlement':'2026-10-20','wait':True},
    {'id':'cash-native-out-before-receipt','kind':'NET_PRICE','answer':'T+1','settlement':'2026-10-20',
     'wait':False,'native_out_first':True},
)


class Calendar(HTTPReferenceProvider):
    def __init__(self):
        pass

    def _read(self,key):
        if key!='calendar_url':raise ValueError('Unexpected fixture source')
        return {'trading_day':DAY,'latest_chinabond_trading_day':DAY,
                'settlement_dates':{DAY:{'T+1':'2026-10-20'}}}


def run_case(case,config,folder):
    if sys.flags.optimize:
        raise RuntimeError('Measurement refuses Python optimization: acceptance assertions must execute')
    folder.mkdir()
    record={'case_id':case['id'],'scenario':case,'success':False,'external_im_sent':False,
            'synthetic_native_receipt':True,'excluded_from_latency_calibration':True,
            'python_optimize':sys.flags.optimize,'http':[],'tools':[]}
    def save():
        (folder/'case.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    clients={stage:ChatflowClient(config['source']['base_url'],config['apps'][stage]['key'],stage=stage,timeout=90)
             for stage in ('analysis','reply')}
    class Flows:
        phase='initial'
        error=None
        def call(self,stage,context):
            attempt={'stage':stage,'phase':self.phase,'input':deepcopy(context)}
            began=time.monotonic()
            try:
                value=clients[stage].call(context)
                attempt['output']=deepcopy(value)
                return value
            except Exception as exc:
                self.error=exc
                attempt['error']={'type':type(exc).__name__,'message':str(exc)}
                raise
            finally:
                attempt.update(elapsed_ms=round((time.monotonic()-began)*1000,3),
                               transport_evidence=deepcopy(clients[stage].last_evidence))
                record['http'].append(attempt);save()
        def analyze(self,context):return self.call('analysis',context)
        def decide(self,context):return self.call('reply',context)
    class References:
        def snapshot(self):
            return replace(reference_snapshot(case['id']),terms={'SYN-A':{
                'terms_snapshot_id':'synthetic-settlement-cashflows','quality':'verified',
                'source_ref':'synthetic-measurement','bond_type':'zero_coupon',
                'convention':'ACT/365F-compounded-v1','compounding_frequency':1,
                'yield_basis':'maturity','settlement_cashflow':'exclude',
                'cashflows':[{'payment_date':'2027-10-20','amount_per_100':'100'}],
                'accrued_interest_per_100':'0','accrued_interest_settlement_date':case['settlement']}})
    class Tools(BondTools):
        def invoke(self,name,arguments):
            value=super().invoke(name,arguments)
            record['tools'].append({'name':name,'arguments':deepcopy(arguments),'result':deepcopy(value)})
            save();return value
    try:
        clock=FixtureClock();record['fixture_clock']=clock.anchor
        store=ApplicationStore('sqlite:///'+str(folder/'fixture.sqlite'),clock=clock,initialize=True)
        owner='synthetic-settlement-owner'
        store.bind_account(owner,'synthetic-account','synthetic-workbench')
        store.set_sending(owner,True)  # Synthetic local commands only; no sender is constructed.
        group=store.create_group(owner,bond_code='SYN-A',our_side='BUY',target_volume='10000',trade_day=DAY,
                                 policy=LimitPolicy('ABSOLUTE','3.12'),send_allowed=True)
        member,thread=store.add_member(owner,group,account_uid='synthetic-account',peer_uid='synthetic-peer')
        record.update(owner=owner,group_id=group,member_id=member,thread_id=thread)
        opening=store.ingest(owner,thread,event_id='synthetic-opening',source_seq=1,trade_day=DAY,
            text='SYN-A 3.20%收，请报可做价量，数量按万沟通',direction='out',native_id='synthetic-opening-native',occurred_at=clock())
        job=store.claim_analysis(owner,thread,force=True)
        store.commit_analysis(owner,job['analysis_id'],job['lease_token'],scopes={opening:[member]},observations=[])
        with store.write(owner) as tx:
            tx.execute('UPDATE xj2_group SET plan_json=? WHERE group_id=?',(encoded({
                'code':'SYN-A','name':'SYN-A','opening_yield_pct':'3.20','step_bp':'2',
                'volume_wan':'10000','our_side':'BUY','trade_day':DAY}),group))
            tx.execute('UPDATE xj2_peer SET our_quote_json=? WHERE member_id=?',(encoded({
                'price':Price('YIELD','3.20').dump(),'message_id':opening,'native_id':'synthetic-opening-native'}),member))
        references=References();flows=Flows();service=Inquiry(store,reference_provider=references,calendar=Calendar())
        engine=Engine(service,flows,Tools(references),stage_budget_seconds=150)
        label='净价' if case['kind']=='NET_PRICE' else '全价'
        store.ingest(owner,thread,event_id='synthetic-cash',source_seq=2,trade_day=DAY,
                     text='SYN-A '+label+'97.00出500万，这已经是我能给的最好条件，一点也不可能再让',occurred_at=clock())
        initial=engine.analyze_thread(owner,thread,force=True);record['initial_commit']=initial
        if flows.error:raise flows.error
        assert initial and initial.get('accepted') and not initial.get('rejected'), 'Initial cash facts failed'
        before=store.quote(owner,member,'ofr').dump();record['before']=before
        assert before['price']['kind']==case['kind'] and before['price']['value']=='97.00'
        assert before['volume_wan']=='500' and before['finality']=='FINAL' and before['price']['settlement_date'] is None
        decision=engine.decide_member(owner,member);record['question_decision']=decision
        if flows.error:raise flows.error
        assert decision and decision['state']=='QUEUED', 'Missing settlement question candidate'
        command=service.commands_core.get(owner,decision['command_id']);record['question_command']=command
        assert json.loads(command['body_json'])['settlement_question']=={'side':'ofr','quote_revision':before['revision']}
        claimed=service.commands_core.claim(owner,command['command_id'])
        assert claimed, 'Fixture question could not be claimed'
        sent_at=clock()
        if case.get('native_out_first'):
            captured=store.ingest(owner,thread,event_id='synthetic-captured-question',source_seq=3,
                trade_day=DAY,text=json.loads(command['body_json'])['text'],direction='out',
                native_id='synthetic-question-native',occurred_at=sent_at)
            record['native_out_before_receipt']=next(row for row in store.messages(owner,thread)
                                                    if row['message_id']==captured)
        delivered=service.commands_core.receipt(owner,command['command_id'],account_uid='synthetic-account',
            peer_uid='synthetic-peer',content_hash=command['content_hash'],status='SENT',
            evidence='synthetic-local-receipt-no-transport',native_id='synthetic-question-native',occurred_at=sent_at)
        assert delivered=='SENT'
        record['sent_question']=service.commands_core.get(owner,command['command_id'])
        if case.get('native_out_first'):
            after_receipt=[row for row in store.messages(owner,thread) if row.get('native_id')=='synthetic-question-native']
            assert after_receipt==[record['native_out_before_receipt']], 'Receipt rewrote or duplicated the captured raw event'
        if case['wait']:
            flows.phase='wait'
            store.ingest(owner,thread,event_id='synthetic-wait',source_seq=3,trade_day=DAY,text='SYN-A 稍等',occurred_at=clock())
            waited=engine.analyze_thread(owner,thread,force=True);record['wait_commit']=waited
            if flows.error:raise flows.error
            assert waited and waited.get('facts_committed') and not waited.get('rejected') and store.quote(owner,member,'ofr').dump()==before
        flows.phase='supplement'
        store.ingest(owner,thread,event_id='synthetic-supplement',source_seq=4,trade_day=DAY,text=case['answer'],occurred_at=clock())
        saved=engine.analyze_thread(owner,thread,force=True);record['supplement_commit']=saved
        if flows.error:raise flows.error
        assert saved and saved.get('accepted') and not saved.get('rejected'), 'Supplement was not committed'
        after=store.quote(owner,member,'ofr').dump();record['after']=after
        assert (after['price']['kind'],after['price']['value'])==(before['price']['kind'],before['price']['value'])
        assert all(after[field]==before[field] for field in ('volume_wan','assertion','validity','confirmed_day'))
        assert after['revision']==before['revision']+1 and after['finality']=='UNKNOWN'
        assert after['price']['settlement_date']==case['settlement'] and after['price']['computed_yield'] is not None
        assert after['price']['calculation_ref'] and len(record['tools'])==1
        flows.phase='after_supplement'
        final=engine.decide_member(owner,member);record['final_decision']=final
        if flows.error:raise flows.error
        assert final and final['state']=='QUEUED', 'Converted cash did not reach a normal reply candidate'
        record['final_command']=service.commands_core.get(owner,final['command_id'])
        assert json.loads(record['final_command']['body_json'])['kind']=='REPLY'
        record['success']=True
    except Exception as exc:
        record['error']={'type':type(exc).__name__,'message':str(exc)}
    finally:
        save()
    return record


def main():
    if sys.flags.optimize:
        raise RuntimeError('Measurement refuses Python optimization: acceptance assertions must execute')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--frozen-manifest',type=Path)
    args=parser.parse_args()
    if args.frozen_manifest is None:
        assert not args.output.exists(), 'Inspect retained results before another model batch'
        manifest=freeze_sources(ROOT,args.output/'source')
        return subprocess.run([sys.executable,'-B',str(Path(manifest['frozen_root'])/'scripts/measure_dify_settlement.py'),
            '--credentials',str(args.credentials.resolve()),'--output',str(args.output.resolve()),
            '--frozen-manifest',str((args.output/'source-manifest.json').resolve())],cwd=manifest['frozen_root']).returncode
    manifest=json.loads(args.frozen_manifest.read_text())
    assert Path(manifest['frozen_root']).resolve()==ROOT and not drift(manifest)
    for target in [args.output/'source.json',args.output/'report.json',*[args.output/case['id'] for case in SCENARIOS]]:
        if target.exists():raise FileExistsError('Refusing to replace existing measurement evidence: '+str(target))
    args.output.mkdir(parents=True,exist_ok=True)
    config=json.loads(args.credentials.read_text())
    source={**config['source'],'apps':{stage:{k:v for k,v in app.items() if k!='key'} for stage,app in config['apps'].items()},
            'source_manifest_sha256':manifest['manifest_sha256'],'external_im_sent':False,'python_optimize':sys.flags.optimize,
            'scope':'Real Dify App API and local synthetic receipt only; not native transport acceptance'}
    with (args.output/'source.json').open('x') as stream:
        stream.write(json.dumps(source,ensure_ascii=False,indent=2)+'\n')
    samples=[]
    for case in SCENARIOS:
        sample=run_case(case,config,args.output/case['id']);samples.append(sample)
        print(json.dumps({k:sample[k] for k in ('case_id','success')},ensure_ascii=False),flush=True)
    integrity={'frozen_drift':drift(manifest),'workspace_drift':drift(manifest,frozen=False)}
    report={'source':source,'attempts':len(samples),'successes':sum(s['success'] for s in samples),
            'source_integrity':integrity,'cases':[{'id':s['case_id'],'success':s['success'],'error':s.get('error')} for s in samples]}
    report['passed']=all(s['success'] for s in samples) and not any(integrity.values())
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('SETTLEMENT_MEASUREMENT_END '+json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
