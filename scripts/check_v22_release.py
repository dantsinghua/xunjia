"""Bounded, split verification of the exact checkout under test.
Run each named group independently; each group checks collection and JUnit
results, refuses failures/skips, and deletes its own ephemeral SQLite data.
Use --group finalize to prove that all collected default Python tests plus
production web-contract tests were executed by the recorded groups.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
PY_GROUPS={
 'cutover':['tests/cutover'],
 'core':['tests/negotiation'],
 'acceptance':['tests/acceptance'],
 'developer':['tests/developer'],
 'release':['tests/release'],
 'root':[p.relative_to(ROOT).as_posix() for p in sorted((ROOT/'tests').glob('test_*.py'))],
 'web-python':['web/tests/developer'],
 'web-contract':['web/tests/acceptance'],
 'static-web':['tests/acceptance/web_assets_contract.py'],
}
WEB_GROUPS={'web-build':['npm','run','build'],'web-unit':['npm','test']}
def manifest():
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    result={}
    for relative in tracked:
        path=ROOT/relative
        if path.is_file() and not path.is_symlink():
            result[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result
def nodes(text):
    return sorted(set(s.strip() for s in text.splitlines() if '::' in s and s.split('::',1)[0].strip().endswith('.py')))
def command(args,*,cwd,env,budget,log):
    start=time.monotonic()
    with log.open('w',encoding='utf8') as f:
        try:result=subprocess.run(args,cwd=cwd,env=env,timeout=budget,stdout=f,stderr=subprocess.STDOUT);code=result.returncode
        except subprocess.TimeoutExpired:code=124
    return code,round(time.monotonic()-start,3)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--group',choices=[*PY_GROUPS,*WEB_GROUPS,'finalize'],required=True)
    p.add_argument('--reports',type=Path,required=True)
    p.add_argument('--timeout',type=int,default=100)
    args=p.parse_args()
    if not 5<=args.timeout<=180:raise SystemExit('Use a 5..180 second per-group budget')
    args.reports.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',PYTHONPATH=str(ROOT))
    before=manifest();source_hash=hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest()
    print('GROUP_STARTED',args.group,'SOURCE_FINGERPRINT',source_hash,flush=True)
    base=[sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider','--disable-warnings','--tb=short']
    if args.group=='finalize':
        reports={name:json.loads((args.reports/(name+'.json')).read_text()) for name in (*PY_GROUPS,*WEB_GROUPS)}
        if any(not r['passed'] or r['source_hash']!=source_hash for r in reports.values()):raise SystemExit('Missing/failed/drifted group report')
        targets=['tests','web/tests/developer','web/tests/acceptance']
        log=args.reports/'final-collection.log'
        code,_=command(base+targets+['--collect-only'],cwd=ROOT,env=env,budget=45,log=log)
        if code:raise SystemExit('Whole-checkout collection failed')
        expected=set(nodes(log.read_text()))
        # Pytest deduplicates explicit file/parent-dir arguments before applying
        # discovery patterns. Non-test_ contract scripts therefore need their
        # own independent collection rather than a mixed parent+file invocation.
        for index,target in enumerate(PY_GROUPS['static-web']):
            extra_log=args.reports/('final-explicit-collection-'+str(index)+'.log')
            extra_code,_=command(base+[target,'--collect-only'],cwd=ROOT,env=env,budget=30,log=extra_log)
            extra=nodes(extra_log.read_text())
            if extra_code or not extra:raise SystemExit('Explicit contract collection failed: '+target)
            expected.update(extra)
        actual=set().union(*(set(reports[n]['nodeids']) for n in PY_GROUPS))
        if not expected or expected!=actual:raise SystemExit('Collection coverage mismatch: missing='+str(sorted(expected-actual))+' unexpected='+str(sorted(actual-expected)))
        summary={'passed':True,'source_hash':source_hash,'checkout':str(ROOT),
          'python_unique_tests':len(expected),'groups':{n:{k:v for k,v in r.items() if k not in ('nodeids','source_hash','checkout')} for n,r in reports.items()},
          'external_live_im_oss_dify_executed':False}
        (args.reports/'final.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        print('ALL_GROUPS_GREEN',len(reports),'UNIQUE_PYTHON_TESTS',len(expected))
        return 0
    if args.group in WEB_GROUPS:
        code,seconds=command(WEB_GROUPS[args.group],cwd=ROOT/'web',env=env,budget=args.timeout,log=args.reports/(args.group+'.log'))
        collected=[];statistics={}
    else:
        targets=PY_GROUPS[args.group]
        log=args.reports/(args.group+'-collect.log')
        code,_=command(base+targets+['--collect-only'],cwd=ROOT,env=env,budget=30,log=log)
        collected=nodes(log.read_text())
        if code or not collected:raise SystemExit('Group collection failed or empty; inspect '+str(log))
        with tempfile.TemporaryDirectory(prefix='xunjia-v22-release-tests-') as temp:
            xml=args.reports/(args.group+'.xml')
            code,seconds=command(base+targets+['--basetemp',str(Path(temp)/'db'),'--junitxml',str(xml)],cwd=ROOT,env=env,budget=args.timeout,log=args.reports/(args.group+'.log'))
            if not xml.exists():raise SystemExit('Test group timed out or never produced results')
            cases=list(ET.parse(xml).getroot().iter('testcase'))
            statistics={'tests':len(cases),'failures':sum(c.find('failure') is not None for c in cases),'errors':sum(c.find('error') is not None for c in cases),'skipped':sum(c.find('skipped') is not None for c in cases)}
            if len(cases)!=len(collected) or any(statistics[k] for k in ('failures','errors','skipped')):code=1
        print('OWNED_TEST_DATA_REMOVED',not Path(temp).exists())
    after=manifest();drift=[key for key in before.keys()|after.keys() if before.get(key)!=after.get(key)]
    result={'group':args.group,'checkout':str(ROOT),'source_hash':source_hash,'passed':code==0 and not drift,
            'exit_code':code,'duration_seconds':seconds,'source_drift':drift,'nodeids':collected,**statistics}
    (args.reports/(args.group+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print((args.reports/(args.group+'.log')).read_text(encoding='utf8',errors='replace')[-6500:])
    print('GROUP_RESULT',json.dumps({k:v for k,v in result.items() if k!='nodeids'},ensure_ascii=False))
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())