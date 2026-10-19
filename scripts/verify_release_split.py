"""Split, evidence-checked offline verification. No live models/IM/database calls."""
from pathlib import Path
import argparse,hashlib,json,os,subprocess,sys,tempfile,shutil,re,time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.process_runner import run_command
def manifest():
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT,timeout=10).decode().split('\0')
    return {n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in sorted(set(names))
            if n and (ROOT/n).is_file() and not any(x in Path(n).parts for x in ('.venv','node_modules','dist','__pycache__','.omx'))}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--browsers-path',type=Path,required=True)
    args=parser.parse_args();args.output_root.mkdir(parents=True,exist_ok=True)
    folder=Path(tempfile.mkdtemp(prefix='full-',dir=args.output_root));tmp=folder/'temporary';tmp.mkdir()
    before=manifest();(folder/'source-before.json').write_text(json.dumps(before,indent=2))
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',PYTHONPATH=str(ROOT),
        PYTEST_ADDOPTS='',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',TMPDIR=str(tmp),PLAYWRIGHT_BROWSERS_PATH=str(args.browsers_path))
    results=[];inventory={};seen={};issues=[];start=time.monotonic()
    def run(name,command,cwd=ROOT,budget=110,mode=None,expected=None):
        r=run_command(command,cwd=cwd,env=env,timeout=budget,log_path=folder/(name+'.log'),
                      pytest_mode=mode,expected_nodeids=expected)
        results.append({'name':name,'passed':r['passed'],'exit_code':r['exit_code'],'seconds':r['elapsed_seconds'],
                        'pytest':r.get('pytest'),'evidence':r['evidence']})
        print('GROUP',name,'PASS' if r['passed'] else 'FAIL','seconds='+str(r['elapsed_seconds']),flush=True)
        return r
    base=[sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider','--tb=short']
    try:
        # The offline suite creates its own loopback HTTP servers. Diagnose a
        # denied test namespace immediately, rather than timing out each group.
        import socket
        try:
            with socket.socket() as listener:
                listener.settimeout(2);listener.bind(('127.0.0.1',0));listener.listen(1)
                with socket.create_connection(listener.getsockname(),timeout=2):
                    connection,_=listener.accept();connection.close()
        except OSError as exc:
            raise RuntimeError('TEST_LOOPBACK_PERMISSION_REQUIRED: '+str(exc)) from exc
        print('PREFLIGHT_LOOPBACK_OK',flush=True)
        collected=run('all-default-inventory',base+['tests','--collect-only'],budget=60,mode='collect')
        if not collected['passed']:raise RuntimeError('Default collection failed')
        expected_main=set(collected['pytest']['nodeids'])
        chunks=[]
        cutover=sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'tests/cutover').glob('test_*.py'))
        for i in range(0,len(cutover),18):chunks.append(('cutover-'+str(i//18+1),cutover[i:i+18]))
        groups=[('root',sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'tests').glob('test_*.py'))),
                ('negotiation',['tests/negotiation']),*chunks,
                ('developer',['tests/developer']),('acceptance',['tests/acceptance']),
                ('release',['tests/release']),('execution',['tests/execution']),('benchmarks',['tests/benchmarks'])]
        def pytest_group(name,targets):
            c=run(name+'-collect',base+targets+['--collect-only'],budget=50,mode='collect')
            if not c['passed']:issues.append(name+':collection_failed');return
            nodes=c['pytest']['nodeids'];inventory[name]=nodes
            for node in nodes:
                if node in seen:issues.append('duplicate:'+node)
                seen[node]=name
            run(name,base+targets+['--basetemp',str(tmp/name)],mode='run',expected=nodes)
        for name,targets in groups:pytest_group(name,targets)
        if set(seen)!=expected_main:
            issues.extend('uncovered:'+n for n in expected_main-set(seen))
            issues.extend('unexpected:'+n for n in set(seen)-expected_main)
        for name,command in [('web-unit',['node','--experimental-strip-types','--test','tests/quoteFilter.test.ts','tests/rfqLimit.test.ts']),
                             ('web-developer',['node','tests/developer/run.mjs']),('web-build',['npm','run','build'])]:
            run(name,command,ROOT/'web',budget=90)
        pytest_group('web-python',['web/tests/developer','tests/acceptance/web_assets_contract.py','web/tests/acceptance'])
        node_counts={}
        for name in ('web-unit','web-developer'):
            text=(folder/(name+'.log')).read_text()
            counts=re.findall(r'(?m)^(?:#|ℹ) tests (\d+)\s*$',text)
            if not counts or not re.search(r'(?m)^(?:#|ℹ) fail 0\s*$',text):issues.append(name+':missing_full_results')
            else:node_counts[name]=int(counts[-1])
    except BaseException as exc:
        issues.append(type(exc).__name__+':'+str(exc));node_counts=locals().get('node_counts',{})
    finally:
        # This literal directory was created by this invocation only; no customer data.
        shutil.rmtree(tmp)
    after=manifest();drift=sorted(n for n in before.keys()|after.keys() if before.get(n)!=after.get(n))
    report={'passed':bool(results) and not issues and not drift and all(r['passed'] for r in results),
            'groups':results,'python_unique':len(seen),'inventory':inventory,'node_counts':node_counts,
            'issues':issues,'source_drift':drift,'seconds':round(time.monotonic()-start,3),
            'test_data_removed':not tmp.exists(),'real_model_called':False,'real_database_modified':False,
            'real_oss_im_called':False,'report':str(folder/'result.json')}
    (folder/'source-after.json').write_text(json.dumps(after,indent=2))
    (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (args.output_root/'LATEST.json').write_text(json.dumps({k:report[k] for k in ('passed','python_unique','node_counts','issues','source_drift','seconds','report')},ensure_ascii=False,indent=2))
    print('FINAL',json.dumps({k:report[k] for k in ('passed','python_unique','node_counts','issues','source_drift','seconds','report')},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())