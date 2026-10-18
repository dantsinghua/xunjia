"""Bounded Linux/WSL test-command lifecycle with durable, verifiable evidence.

Each child starts in its own session with a unique inherited ownership token.
Cleanup targets verified process identities, including descendants after the
leader exits. This module does not start services or choose verification gates.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import uuid

TOKEN_ENV = 'TASK_RUNNER_OWNERSHIP_TOKEN'


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_progress(progress_path, event):
    """All cooperating writers lock a stable file, then atomically replace JSON."""
    path = Path(progress_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path)+'.lock').open('a') as lock:
        deadline = time.monotonic()+2
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Progress lock deadline exceeded')
                time.sleep(.01)
        try:
            value = json.loads(path.read_text()) if path.exists() else {'events': []}
            if not isinstance(value, dict) or not isinstance(value.get('events'), list):
                raise ValueError('Invalid progress journal; original file preserved')
            value['events'].append(event)
            _atomic_json(path, value)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_result(path, value):
    _atomic_json(path, value)


def _proc(pid):
    path = Path('/proc')/str(pid)
    try:
        raw = (path/'stat').read_text()
        fields = raw[raw.rfind(')')+2:].split()
        return {'pid': int(pid), 'ppid': int(fields[1]), 'pgid': int(fields[2]),
                'sid': int(fields[3]), 'start_ticks': int(fields[19]),
                'state': fields[0], 'uid': path.stat().st_uid}
    except (FileNotFoundError, ProcessLookupError):
        return None


def _token_matches(pid, token):
    try:
        return (TOKEN_ENV+'='+token).encode() in (Path('/proc')/str(pid)/'environ').read_bytes().split(b'\0')
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


class OwnedTask:
    def __init__(self, process, token):
        self.process, self.token = process, token
        self.known = {}
        self.errors = []
        try:
            self.identity = _proc(process.pid)
        except OSError as exc:
            self.identity = None
            self.errors.append('initial identity inspection: '+str(exc))
        if self.identity:
            self.known[process.pid] = self.identity

    def members(self):
        rows = {}
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():
                continue
            try:
                row = _proc(int(path.name))
            except (PermissionError, ValueError, IndexError, OSError) as exc:
                # An unrelated process may disappear/change while enumerating.
                if int(path.name) in self.known:
                    self.errors.append('owned process inspection: '+str(exc))
                continue
            if row:
                rows[row['pid']] = row
        owned = {}
        for pid, row in rows.items():
            known = self.known.get(pid)
            same_identity = known and all(known[key] == row[key] for key in ('pid','pgid','sid','start_ticks'))
            if same_identity or _token_matches(pid, self.token):
                owned[pid] = row
        # Include changed-session descendants while ancestry can be proven.
        changed = True
        while changed:
            changed = False
            for pid, row in rows.items():
                if pid not in owned and row['ppid'] in owned and row['start_ticks'] >= owned[row['ppid']]['start_ticks']:
                    owned[pid] = row
                    changed = True
        self.known.update(owned)
        return [row for row in owned.values() if row['state'] not in ('Z', 'X')]

    def signal_members(self, rows, signum):
        sent = []
        for expected in rows:
            pid = expected['pid']
            descriptor = None
            try:
                # pidfd keeps the signal bound to this process, even if a PID
                # is reused between inspection and delivery.
                descriptor = os.pidfd_open(pid)
                actual = _proc(pid)
                if not actual or any(actual[key] != expected[key] for key in ('pid','pgid','sid','start_ticks')):
                    continue
                signal.pidfd_send_signal(descriptor, signum)
                sent.append(pid)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError) as exc:
                self.errors.append('signal '+str(pid)+': '+str(exc))
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        return sent

    def cleanup(self, term_grace, kill_grace):
        term_sent, kill_sent = [], []
        live = self.members()
        term_sent.extend(self.signal_members(live, signal.SIGTERM))
        deadline = time.monotonic()+term_grace
        while live and time.monotonic() < deadline:
            self.process.poll()
            time.sleep(.02)
            live = self.members()
        # The leader may already be reaped. Check the owned set, not wait().
        deadline = time.monotonic()+kill_grace
        while live:
            kill_sent.extend(self.signal_members(live, signal.SIGKILL))
            self.process.poll()
            if time.monotonic() >= deadline:
                break
            time.sleep(.02)
            live = self.members()
        self.process.poll()
        remaining = self.members()
        return {'complete': not remaining and not self.errors, 'remaining': remaining,
                'term_sent': sorted(set(term_sent)), 'kill_sent': sorted(set(kill_sent)),
                'owned_processes': [{**row, 'token': self.token} for row in self.known.values()],
                'errors': list(self.errors)}


def diagnose(task, log_path, timeout, command):
    """Diagnostic failure is separate from, and cannot replace, task cleanup."""
    path = Path(str(log_path)+'.diagnostic.log')
    command = list(command) if command else ['ps','-o','pid,ppid,pgid,sid,stat,etime,args','--sid',str(task.process.pid)]
    token = uuid.uuid4().hex
    diagnostic = None
    error = None
    code = None
    cleanup = None
    with path.open('x') as log:
        try:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, env={**os.environ,TOKEN_ENV:token})
            diagnostic = OwnedTask(process, token)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                error = 'Diagnostic timeout'
            if code not in (None,0):
                error = 'Diagnostic exit '+str(code)
        finally:
            if diagnostic:
                cleanup = diagnostic.cleanup(.1,.5)
    return {'path':str(path),'command':command,'timeout_seconds':timeout,
            'exit_code':code,'error':error,'cleanup':cleanup}


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pytest_contract(path, mode, expected_nodeids):
    value = json.loads(path.read_text())
    nodes = value.get('nodeids', [])
    errors = []
    if not value.get('complete') or value.get('mode') != mode:
        errors.append('Pytest did not finish the requested phase')
    if not nodes or len(nodes) != len(set(nodes)) or value.get('collection_errors'):
        errors.append('Pytest collection is empty, duplicated or failed')
    if expected_nodeids is not None and set(nodes) != set(expected_nodeids):
        errors.append('Executed pytest collection differs from the frozen inventory')
    if mode == 'run':
        counts = value.get('counts', {})
        if counts.get('passed') != len(nodes) or any(counts.get(key,0) for key in ('failed','skipped','incomplete')):
            errors.append('Pytest requires every collected test to pass without skips')
    return value, errors


def run_command(command, *, cwd, log_path, timeout, env=None, progress_path=None,
                expected_markers=(), expected_artifacts=(), pytest_mode=None,
                expected_nodeids=None, term_grace=.5, kill_grace=1.0,
                diagnostic_timeout=.5, diagnostic_command=None):
    started = time.monotonic()
    run_id = uuid.uuid4().hex
    log_path, cwd = Path(log_path).absolute(), Path(cwd).absolute()
    directory = log_path.parent/(log_path.name+'.run-'+run_id)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    result_path, manifest_path, events_path = [directory/name for name in ('result.json','manifest.json','events.json')]
    pytest_path = directory/'pytest.json'
    progress_path = Path(progress_path) if progress_path is not None else log_path.parent/'progress.json'
    result = {'run_id':run_id,'command':list(command),'cwd':str(cwd),'passed':False,'status':'ERROR',
              'exit_code':125,'process_exit_code':None,'timed_out':False,'cancelled':False,
              'errors':[], 'cleanup':{'complete':True,'remaining':[],'term_sent':[],'kill_sent':[],'owned_processes':[]},
              'evidence':{'log':str(log_path),'result':str(result_path),'manifest':str(manifest_path),'events':str(events_path)}}
    events, task, log = [], None, None
    source_before, durable_inputs = {}, {}
    cancelled = []
    handlers = {}
    def error(stage, exc):
        result['errors'].append({'stage':stage,'type':type(exc).__name__,'message':str(exc)})
        if isinstance(exc,KeyboardInterrupt):
            result['cancelled']=True
            if not result['timed_out']:result.update(status='CANCELLED',exit_code=130)
    def emit(kind, **fields):
        event = {'run_id':run_id,'event':kind,'at':time.time(),**fields}
        events.append(event)
        append_progress(progress_path,event)
    def on_signal(signum, _frame):
        cancelled.append(signum)
    try:
        if (not command or not all(isinstance(arg,str) for arg in command)
                or not cwd.is_dir() or not Path('/proc').is_dir()
                or not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal')):
            raise ValueError('Execution prerequisite missing: command, cwd, Linux /proc or pidfd')
        for name,value in [('timeout',timeout),('term_grace',term_grace),('kill_grace',kill_grace),('diagnostic_timeout',diagnostic_timeout)]:
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise ValueError('Invalid '+name)
        if pytest_mode not in (None,'collect','run'):
            raise ValueError('Unknown pytest mode')
        sources={Path(__file__).resolve()}
        if pytest_mode:
            sources.add(Path(__file__).parent/'execution/xunjia_pytest_evidence.py')
        for arg in command[1:]:
            path=Path(arg) if Path(arg).is_absolute() else cwd/arg
            try:
                if path.is_file():sources.add(path.resolve())
            except OSError:
                pass  # Inline code/ordinary arguments are not filesystem paths.
        for nodeid in expected_nodeids or ():
            path=cwd/nodeid.split('::',1)[0]
            if path.is_file():sources.add(path.resolve())
        (directory/'inputs').mkdir()
        for path in sources:
            data=path.read_bytes(); digest=hashlib.sha256(data).hexdigest()
            captured=directory/'inputs'/digest
            captured.write_bytes(data)
            source_before[str(path)]=digest
            durable_inputs[str(captured)]=digest
        child_env = dict(os.environ if env is None else env)
        child_env[TOKEN_ENV] = run_id
        actual_command = list(command)
        if pytest_mode:
            plugin_dir = Path(__file__).parent/'execution'
            child_env['PYTHONPATH'] = str(plugin_dir)+os.pathsep+child_env.get('PYTHONPATH','')
            child_env['XUNJIA_PYTEST_EVIDENCE'] = str(pytest_path)
            actual_command += ['-p','xunjia_pytest_evidence']
            if pytest_mode=='collect' and '--collect-only' not in actual_command:
                actual_command.append('--collect-only')
        result['executed_command'] = actual_command
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT,signal.SIGTERM):
                handlers[signum] = signal.signal(signum,on_signal)
        log = log_path.open('x')
        process = subprocess.Popen(actual_command,cwd=cwd,env=child_env,stdout=log,
                                   stderr=subprocess.STDOUT,start_new_session=True)
        task = OwnedTask(process,run_id)
        emit('BEGIN',pid=process.pid,command=actual_command,cwd=str(cwd),timeout=timeout)
        deadline = started+timeout
        while process.poll() is None:
            task.members()
            if process.poll() is not None:
                result['process_exit_code']=process.returncode
                result.update(status='PASS' if process.returncode==0 else 'FAIL',exit_code=process.returncode)
                break
            if cancelled:
                raise KeyboardInterrupt('Task cancellation signal '+str(cancelled[-1]))
            if time.monotonic()>=deadline:
                result.update(status='TIMEOUT',exit_code=124,timed_out=True)
                result['timeout_processes']=task.members()
                try:
                    result['diagnostic']=diagnose(task,log_path,diagnostic_timeout,diagnostic_command)
                    if result['diagnostic'].get('error'):
                        error('diagnostic',RuntimeError(result['diagnostic']['error']))
                except BaseException as exc:
                    error('diagnostic',exc)
                break
            time.sleep(.02)
        else:
            result['process_exit_code']=process.returncode
            result.update(status='PASS' if process.returncode==0 else 'FAIL',exit_code=process.returncode)
    except KeyboardInterrupt as exc:
        result.update(status='CANCELLED',exit_code=128+(cancelled[-1] if cancelled else signal.SIGINT),cancelled=True)
        error('cancelled',exc)
    except BaseException as exc:
        error('execution',exc)
    finally:
        if task is not None:
            try:
                result['cleanup']=task.cleanup(term_grace,kill_grace)
                result['process_exit_code']=task.process.poll()
            except BaseException as exc:
                result['cleanup']['complete']=False
                error('cleanup',exc)
        if log is not None:
            log.close()
        if cancelled:
            result['cancelled']=True
            if not result['timed_out']:
                result.update(status='CANCELLED',exit_code=128+cancelled[-1])
        for signum,previous in handlers.items():
            signal.signal(signum,previous)
        result['elapsed_seconds']=round(time.monotonic()-started,6)
    if result['status']=='FAIL':
        error('process_exit',RuntimeError('Command exited with code '+str(result['process_exit_code'])+'; original output retained'))
    if not events:
        try:emit('BEGIN',not_spawned=True,command=list(command),cwd=str(cwd))
        except BaseException as exc:error('begin_record',exc)
    if not log_path.exists():
        try:
            with log_path.open('x') as stream:
                stream.write(json.dumps(result['errors'],ensure_ascii=False)+'\n')
        except BaseException as exc:error('failure_log',exc)
    diagnostic_cleanup=(result.get('diagnostic') or {}).get('cleanup')
    if diagnostic_cleanup:
        result['cleanup']['complete']=result['cleanup']['complete'] and diagnostic_cleanup['complete']
        for key in ('remaining','term_sent','kill_sent','owned_processes'):
            result['cleanup'][key].extend(diagnostic_cleanup[key])
    if result['status']=='PASS':
        try:
            output=log_path.read_text(errors='replace')
            if any(marker not in output for marker in expected_markers):
                raise ValueError('Expected command completion marker is missing')
            for path in expected_artifacts:
                path=Path(path) if Path(path).is_absolute() else cwd/path
                if not path.is_file():raise ValueError('Expected artifact missing: '+str(path))
            if pytest_mode:
                result['pytest'],issues=_pytest_contract(pytest_path,pytest_mode,expected_nodeids)
                if issues:raise ValueError('; '.join(issues))
        except BaseException as exc:
            error('contract',exc)
    result['source_drift']=[]
    for path,expected in source_before.items():
        try:
            if not Path(path).is_file() or _hash(path)!=expected:
                result['source_drift'].append(path)
        except BaseException as exc:
            result['source_drift'].append(path)
            error('source_inspection',exc)
    if result['source_drift']:
        error('source_drift',ValueError('Execution inputs changed while the command was running'))
    if not result['cleanup']['complete']:
        error('cleanup_incomplete',RuntimeError('Owned task cleanup could not be verified'))
    if result['errors'] and result['status']=='PASS':
        result.update(status='ERROR',exit_code=125)
    try:
        # END records termination, not a success claim before result persistence.
        emit('END',exit_code=result['exit_code'],elapsed_seconds=result['elapsed_seconds'],cleanup=result['cleanup']['complete'])
    except BaseException as exc:
        error('end_record',exc)
        if result['status']=='PASS':result.update(status='ERROR',exit_code=125)
    try:
        _atomic_json(events_path,{'events':events})
        files=dict(durable_inputs)
        for path in (log_path,events_path,pytest_path):
            if path.is_file():files[str(path.resolve())]=_hash(path)
        diagnostic_path=Path(str(log_path)+'.diagnostic.log')
        if diagnostic_path.is_file():files[str(diagnostic_path.resolve())]=_hash(diagnostic_path)
        outputs={}
        for value in expected_artifacts:
            path=Path(value) if Path(value).is_absolute() else cwd/value
            if path.is_file():
                data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
                captured=directory/('artifact-'+digest)
                captured.write_bytes(data);files[str(captured)]=digest
                outputs[str(path)]=str(captured)
        _atomic_json(manifest_path,{'run_id':run_id,'files':files,'source_inputs':source_before,
            'output_artifacts':outputs,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'command_sha256':hashlib.sha256(json.dumps(list(command)).encode()).hexdigest()})
    except BaseException as exc:
        error('evidence',exc)
        if result['status']=='PASS':result.update(status='ERROR',exit_code=125)
    result['passed']=result['status']=='PASS' and result['cleanup']['complete'] and not result['errors']
    try:
        write_result(result_path,result)
    except BaseException as exc:
        error('result_record',exc)
        result['passed']=False
        if not result['cancelled']:result.update(status='ERROR',exit_code=125)
        # Preserve a faulty writer's partial result, never publish it as PASS.
        if result_path.exists():
            os.replace(result_path,directory/'uncommitted-result.json')
        result_path=directory/'emergency-result.json'
        result['evidence']['result']=str(result_path)
        try:_atomic_json(result_path,result)
        except BaseException as fallback:error('emergency_record',fallback)
    return result


def verify_evidence(result_path):
    result=json.loads(Path(result_path).read_text())
    manifest=json.loads(Path(result['evidence']['manifest']).read_text())
    if result['run_id']!=manifest['run_id']:
        raise ValueError('Evidence identity mismatch')
    for path,expected in manifest['files'].items():
        if not Path(path).is_file() or _hash(path)!=expected:
            raise ValueError('Evidence missing or changed: '+path)
    events=json.loads(Path(result['evidence']['events']).read_text())['events']
    if not any(event['event']=='END' and event['run_id']==result['run_id'] for event in events):
        raise ValueError('Durable END event missing')
    return result
