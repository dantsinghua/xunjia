"""Explicit joint acceptance entrypoint; never touches running deployments."""
import argparse,importlib.util,json,os,subprocess,sys,time
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--suite',choices=('http','media','browser','all'),default='http')
    parser.add_argument('--build-web',action='store_true')
    a=parser.parse_args();Q=Path(__file__).resolve().parent;X=Q.parents[1];C=a.collector_root.resolve();O=a.output.resolve()
    if not (C/'src/qtrade_agent').is_dir():parser.error('collector root must contain src/qtrade_agent')
    if any(O==p or O.is_relative_to(p) for p in (X/'server',X/'relay',C/'src',C/'console')):
        parser.error('output must be outside product source directories')
    O.mkdir(parents=True,exist_ok=True);os.environ['ITS_RELAY_QA_OUTPUT']=str(O)
    required=['pytest','yaml','cryptography','fastapi','httpx','uvicorn']
    if a.suite in ('browser','all'):required+=['playwright']
    missing=[m for m in required if not importlib.util.find_spec(m)]
    if missing:raise SystemExit('TEST_DEPENDENCIES_MISSING: '+','.join(missing))
    if a.build_web:
        for label,command in [('typecheck',['node','node_modules/vue-tsc/bin/vue-tsc.js','--noEmit']),('build',['node','node_modules/vite/bin/vite.js','build','--outDir',str(O/'web-dist')])]:
            with (O/(label+'.log')).open('w') as log:
                p=subprocess.run(command,cwd=X/'web',stdout=log,stderr=subprocess.STDOUT,timeout=120)
            if p.returncode:raise SystemExit('FRONTEND_'+label.upper()+'_FAILED: '+str(O/(label+'.log')))
    files=[]
    if a.suite in ('http','all'):files+=['cases_chain.py']
    if a.suite in ('media','all'):files+=['cases_media_contract.py','cases_media_extended.py','cases_retention.py']
    if a.suite in ('browser','all'):
        if not (O/'web-dist/index.html').is_file():raise SystemExit('BUILD_FRONTEND_FIRST: use --build-web')
        files+=['cases_browser.py']
    sys.path[:0]=[str(Q),str(X),str(C/'src')]
    import pytest
    started=time.time()
    result=pytest.main(['-q','--tb=short','--rootdir='+str(Q),'--confcutdir='+str(Q),*[str(Q/f) for f in files],'--junitxml='+str(O/(a.suite+'.xml'))])
    (O/(a.suite+'-run.json')).write_text(json.dumps({'started':started,'ended':time.time(),'exit_code':int(result),'collector_root':str(C),'xunjia_root':str(X),'boundary':'local HTTP mock gateway, mock Dify response, synthetic native sink; real production protocol and business paths','real_gateway_requests':0,'real_native_sends':0},indent=2))
    return int(result)
if __name__=='__main__':raise SystemExit(main())