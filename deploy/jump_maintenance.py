#!/usr/bin/env python3
"""Observe the approved jump host and perform bounded nginx recovery only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

HOST = '172.26.64.36'
ENDPOINTS = {50443: 'its-relay.yingmi.cn', 11443: 'web.innodealing.com',
             12443: 'rest.innodealing.com', 13443: 'bosh2.innodealing.com'}
STATE_DIR = '/var/lib/xunjia-network-maintenance'
COOLDOWN = 300
HOURLY_LIMIT = 3
INTERVAL = 30


class MaintenanceError(ValueError):
    pass


def command(args, timeout=15):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout,
                              env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.TimeoutExpired):
        raise MaintenanceError('COMMAND_UNAVAILABLE_OR_TIMEOUT') from None


def checked(args, timeout=15):
    result = command(args, timeout)
    if result.returncode:
        raise MaintenanceError('COMMAND_FAILED')
    return result.stdout


def check_host():
    addresses = checked(['ip', '-o', '-4', 'addr', 'show'])
    if not re.search(r'\binet\s+' + re.escape(HOST) + r'/', addresses):
        raise MaintenanceError('WRONG_HOST_EXPECTED_172_26_64_36')


def _nodes(text):
    lexer = shlex.shlex(text, posix=True, punctuation_chars='{};')
    lexer.whitespace_split = True
    lexer.commenters = '#'
    tokens = []
    for token in lexer:
        if token and all(c in '{};' for c in token):
            tokens.extend(token)
        else:
            tokens.append(token)
    index = [0]
    def parse(nested=False):
        result, head = [], []
        while index[0] < len(tokens):
            value = tokens[index[0]]
            index[0] += 1
            if value == '}':
                if head or not nested:
                    raise MaintenanceError('NGINX_CONFIG_PARSE_FAILED')
                return result
            if value == ';':
                if not head:
                    raise MaintenanceError('NGINX_CONFIG_PARSE_FAILED')
                result.append((head, None)); head = []
            elif value == '{':
                if not head:
                    raise MaintenanceError('NGINX_CONFIG_PARSE_FAILED')
                result.append((head, parse(True))); head = []
            else:
                head.append(value)
        if head or nested:
            raise MaintenanceError('NGINX_CONFIG_PARSE_FAILED')
        return result
    return parse()


def parse_mapping(dump, main, prefix):
    """Resolve nginx -T includes without reading or writing configuration files."""
    matches = list(re.finditer(r'^# configuration file (/.+):\s*$', dump, re.M))
    if not matches:
        raise MaintenanceError('NGINX_CONFIG_DUMP_UNRECOGNIZED')
    documents = {}
    for i, match in enumerate(matches):
        path = match[1]
        if path in documents:
            raise MaintenanceError('NGINX_CONFIG_DUPLICATE_DUMP')
        documents[path] = dump[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(dump)]
    if main not in documents:
        raise MaintenanceError('NGINX_MAIN_CONFIG_NOT_PROVEN')
    def expand(path, stack):
        if path in stack or len(stack) > 16:
            raise MaintenanceError('NGINX_INCLUDE_RECURSION')
        def resolve(nodes):
            output = []
            for head, body in nodes:
                if head[0] == 'include' and body is None:
                    if len(head) != 2 or '$' in head[1]:
                        raise MaintenanceError('NGINX_INCLUDE_UNRECOGNIZED')
                    pattern = head[1] if head[1].startswith('/') else str(Path(main).parent / head[1])
                    found = sorted(p for p in documents if len(Path(p).parts) == len(Path(pattern).parts)
                                   and all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(Path(p).parts, Path(pattern).parts)))
                    if not found and not any(c in pattern for c in '*?['):
                        raise MaintenanceError('NGINX_INCLUDE_NOT_DUMPED')
                    for child in found:
                        output.extend(expand(child, stack + [path]))
                else:
                    output.append((head, resolve(body) if body is not None else None))
            return output
        return resolve(_nodes(documents[path]))
    roots = expand(main, [])
    mappings, local_addresses = {}, {}
    for heading, children in roots:
        if heading != ['stream'] or children is None:
            continue
        for head, body in children:
            if head != ['server'] or body is None:
                continue
            listens = [h for h, b in body if h[0] == 'listen' and b is None]
            ports, addresses = [], {}
            for listen in listens:
                match = re.fullmatch(r'(?:(?:0\.0\.0\.0|127\.0\.0\.1|172\.26\.64\.36|\[::\]):)?(\d+)', listen[1]) if len(listen) > 1 else None
                if match and int(match[1]) in ENDPOINTS:
                    if len(listen) != 2:
                        raise MaintenanceError('NGINX_LISTENER_OPTIONS_UNREVIEWED')
                    port = int(match[1]); ports.append(port)
                    address = listen[1].rsplit(':', 1)[0] if ':' in listen[1] else '0.0.0.0'
                    addresses.setdefault(port, set()).add(address)
            if not ports:
                continue
            proxies = [h for h, b in body if h[0] == 'proxy_pass' and b is None]
            if len(proxies) != 1 or len(proxies[0]) != 2:
                raise MaintenanceError('NGINX_PROXY_MAPPING_UNREVIEWED')
            if any(h[0] in ('ssl', 'ssl_certificate', 'proxy_protocol') for h, b in body):
                raise MaintenanceError('NGINX_PASSTHROUGH_REQUIRED')
            for port in set(ports):
                if port in mappings or proxies[0][1] != ENDPOINTS[port] + ':443':
                    raise MaintenanceError('NGINX_PROXY_MAPPING_UNREVIEWED')
                mappings[port] = ENDPOINTS[port]
                candidates = addresses[port]
                if candidates & {'0.0.0.0', '127.0.0.1'}:
                    local_addresses[port] = '127.0.0.1'
                elif HOST in candidates:
                    local_addresses[port] = HOST
                else:
                    raise MaintenanceError('NGINX_IPV4_LISTENER_REQUIRED')
    if mappings != ENDPOINTS:
        raise MaintenanceError('NGINX_FOUR_STATIC_MAPPINGS_REQUIRED')
    fingerprint = hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()
    return {'mappings': mappings, 'local_addresses': local_addresses, 'config_sha256': fingerprint}


def parse_unit(properties):
    fields = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
    if fields.get('LoadState') != 'loaded' or fields.get('ActiveState') not in ('active', 'inactive', 'failed'):
        raise MaintenanceError('NGINX_UNIT_NOT_STABLE')
    start = fields.get('ExecStart', '')
    match = re.fullmatch(r'\{ path=(/[^ ;]+) ; argv\[\]=(.*?) ; ignore_errors=(?:yes|no) ; .*\}', start)
    if not match:
        raise MaintenanceError('NGINX_EXECSTART_UNRECOGNIZED')
    binary, raw = match.groups()
    if Path(binary).name != 'nginx' or not Path(binary).is_absolute():
        raise MaintenanceError('NGINX_EXECSTART_UNRECOGNIZED')
    raw = raw.replace('-g daemon on; master_process on;', '-g "daemon on; master_process on;"')
    args = shlex.split(raw)
    if not args or args[0] != binary:
        raise MaintenanceError('NGINX_EXECSTART_UNRECOGNIZED')
    options = {}
    index = 1
    while index < len(args):
        flag = args[index]
        if flag not in ('-c', '-p', '-g') or flag in options or index + 1 == len(args):
            raise MaintenanceError('NGINX_EXECSTART_OPTIONS_UNREVIEWED')
        options[flag] = args[index + 1]; index += 2
    if '-g' in options and options['-g'] != 'daemon on; master_process on;':
        raise MaintenanceError('NGINX_GLOBAL_OPTIONS_UNREVIEWED')
    for flag in ('-c', '-p'):
        if flag in options and not Path(options[flag]).is_absolute():
            raise MaintenanceError('NGINX_ABSOLUTE_CONFIG_REQUIRED')
    reload_match = re.fullmatch(r'\{ path=(/[^ ;]+) ; argv\[\]=(.*?) ; ignore_errors=(?:yes|no) ; .*\}', fields.get('ExecReload', ''))
    if not reload_match:
        raise MaintenanceError('NGINX_EXECRELOAD_UNRECOGNIZED')
    reload_binary, reload_raw = reload_match.groups()
    reload_args = shlex.split(reload_raw.replace('-g daemon on; master_process on;', '-g "daemon on; master_process on;"'))
    if not reload_args or reload_args[0] != reload_binary:
        raise MaintenanceError('NGINX_EXECRELOAD_UNRECOGNIZED')
    if reload_binary in ('/bin/kill', '/usr/bin/kill'):
        if reload_args[1:] not in (['-s', 'HUP', '$MAINPID'], ['-HUP', '$MAINPID'],
                                  ['-s', 'HUP', '${MAINPID}'], ['-HUP', '${MAINPID}']):
            raise MaintenanceError('NGINX_EXECRELOAD_UNRECOGNIZED')
    elif reload_binary == binary:
        reload_options = {}
        for index in range(1, len(reload_args), 2):
            flag = reload_args[index]
            if flag not in ('-g', '-p', '-c', '-s') or flag in reload_options or index + 1 >= len(reload_args):
                raise MaintenanceError('NGINX_EXECRELOAD_UNRECOGNIZED')
            reload_options[flag] = reload_args[index + 1]
        if reload_options.pop('-s', None) != 'reload' or reload_options != options:
            raise MaintenanceError('NGINX_EXECRELOAD_CONFIG_MISMATCH')
    else:
        raise MaintenanceError('NGINX_EXECRELOAD_UNRECOGNIZED')
    fragment = fields.get('FragmentPath', '')
    if not fragment.startswith(('/etc/systemd/system/', '/usr/lib/systemd/system/', '/lib/systemd/system/')) or '..' in Path(fragment).parts:
        raise MaintenanceError('NGINX_UNIT_FRAGMENT_UNREVIEWED')
    try:
        pid = int(fields.get('MainPID', '0'))
    except ValueError:
        raise MaintenanceError('NGINX_PID_UNPROVEN') from None
    if fields['ActiveState'] == 'active' and pid <= 1 or fields['ActiveState'] != 'active' and pid != 0:
        raise MaintenanceError('NGINX_PID_UNPROVEN')
    return {'binary': binary, 'args': args, 'options': options,
            'active': fields['ActiveState'] == 'active', 'pid': pid,
            'fragment': fragment, 'reload_args': reload_args,
            'unit_sha256': hashlib.sha256(json.dumps([args,reload_args,fragment,fields.get('DropInPaths','')]).encode()).hexdigest()}


class System:
    def snapshot(self):
        check_host()
        info = parse_unit(checked(['systemctl', 'show', 'nginx.service',
                                  '--property=LoadState,ActiveState,ExecStart,ExecReload,FragmentPath,DropInPaths,MainPID'], 5))
        fragment = Path(info['fragment'])
        if not fragment.is_file() or fragment.stat().st_uid != 0 or fragment.stat().st_mode & 0o022:
            raise MaintenanceError('NGINX_UNIT_FRAGMENT_NOT_PROTECTED')
        info['unit_sha256'] = hashlib.sha256(info['unit_sha256'].encode() + fragment.read_bytes()).hexdigest()
        binary = Path(info['binary'])
        if not binary.is_file():
            raise MaintenanceError('NGINX_BINARY_MISSING')
        if info['active']:
            process = Path('/proc') / str(info['pid'])
            try:
                executable = (process / 'exe').resolve(strict=True)
                cmdline = (process / 'cmdline').read_bytes().replace(b'\0', b' ').decode().strip()
            except (OSError, UnicodeError):
                raise MaintenanceError('NGINX_PROCESS_UNPROVEN') from None
            expected = 'nginx: master process ' + ' '.join(info['args'])
            if executable != binary.resolve() or cmdline != expected:
                raise MaintenanceError('NGINX_PROCESS_IDENTITY_MISMATCH')
        version = command([str(binary), '-V'], 5)
        if version.returncode:
            raise MaintenanceError('NGINX_BUILD_CONFIG_UNAVAILABLE')
        compiled = {}
        for token in shlex.split(version.stderr + '\n' + version.stdout):
            if token.startswith(('--prefix=', '--conf-path=')):
                key, value = token.split('=', 1); compiled[key] = value
        prefix = info['options'].get('-p', compiled.get('--prefix'))
        main = info['options'].get('-c', compiled.get('--conf-path'))
        if not prefix or not main or not Path(prefix).is_absolute():
            raise MaintenanceError('NGINX_BUILD_PATH_UNPROVEN')
        main = main if Path(main).is_absolute() else str(Path(prefix) / main)
        info.update(main=main, prefix=prefix)
        args = [str(binary), '-p', prefix, '-c', main]
        if '-g' in info['options']:
            args.extend(['-g', info['options']['-g']])
        dump = command(args + ['-T'], 10)
        if dump.returncode:
            if 'host not found in upstream' in dump.stderr:
                raise MaintenanceError('NGINX_CONFIG_DNS_FAILED')
            raise MaintenanceError('NGINX_CONFIG_TEST_FAILED')
        info.update(parse_mapping(dump.stdout, main, prefix))
        self.local_addresses = info['local_addresses']
        info['test_args'] = args + ['-t']
        return info

    def resolve(self, host):
        try:
            result = command(['getent', 'ahostsv4', host], 5)
            ips = sorted({str(ipaddress.IPv4Address(line.split()[0])) for line in result.stdout.splitlines() if line.split()})
            return ips if result.returncode == 0 else []
        except (MaintenanceError, ValueError):
            return []

    def probe(self, host, port, local=False):
        scheme = 'http' if port == 30080 else 'https'
        url = '{}://{}:{}/'.format(scheme, host, port)
        args = ['curl', '-q', '--head', '--silent', '--show-error', '--noproxy', '*',
                '--connect-timeout', '4', '--max-time', '8', '--output', '/dev/null',
                '--write-out', '%{http_code} %{time_connect} %{time_appconnect} %{ssl_verify_result}']
        if local:
            address = getattr(self, 'local_addresses', {}).get(port, '127.0.0.1')
            args += ['--resolve', '{}:{}:{}'.format(host, port, address)]
        try:
            result = command(args + [url], 10)
            values = result.stdout.strip().split()
            code, tcp, tls, verify = int(values[0]), float(values[1]), float(values[2]), int(values[3])
        except (MaintenanceError, ValueError, IndexError):
            return {'status': 'PROBE_FAILED', 'reachable': False}
        if verify != 0 or result.returncode == 60:
            status = 'TLS_VERIFY_FAILED'
        elif code >= 100:
            status = 'HTTP_REACHABLE'
        elif tcp > 0 and result.returncode in (35, 56) and re.search(r'certificate required|alert bad certificate|alert certificate required', result.stderr, re.I):
            status = 'CLIENT_CERTIFICATE_REQUIRED'
        elif tcp > 0 and tls > 0:
            # TLS 1.3 client-auth rejection can arrive as curl 55 or 56 after
            # the verified handshake. Do not call this HTTP success or repair it.
            status = 'TLS_VERIFIED_HTTP_UNCONFIRMED'
        elif result.returncode == 6:
            status = 'DNS_FAILED'
        elif tcp <= 0:
            status = 'TCP_FAILED'
        else:
            status = 'TLS_OR_HTTP_FAILED'
        return {'status': status, 'reachable': status in ('HTTP_REACHABLE', 'CLIENT_CERTIFICATE_REQUIRED'),
                'http': code, 'tcp_seconds': tcp, 'tls_seconds': tls, 'curl_exit': result.returncode}

    def action(self, operation, snapshot):
        checked(snapshot['test_args'], 10)
        # This controlled start/reload never invokes a shell or stops a service.
        checked(['systemctl', operation, 'nginx.service'], 45)


def preflight(system=None):
    return (system or System()).snapshot()


def _private_root(root, create=False):
    root = Path(root).absolute()
    if root.resolve() != root:
        raise MaintenanceError('STATE_PATH_SYMLINK_FORBIDDEN')
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.exists() and (not root.is_dir() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077):
        raise MaintenanceError('STATE_DIRECTORY_NOT_PRIVATE')
    return root


def atomic_json(path, value):
    if path.is_symlink():
        raise MaintenanceError('STATE_FILE_SYMLINK_FORBIDDEN')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


class Maintenance:
    def __init__(self, system=None, state_dir=STATE_DIR):
        self.system = system or System()
        self.root = Path(state_dir).absolute()
        self.stop_requested = lambda: False

    def read(self):
        _private_root(self.root)
        path = self.root / 'state.json'
        if path.is_symlink():
            raise MaintenanceError('STATE_FILE_SYMLINK_FORBIDDEN')
        if not path.exists():
            return {'version': 1, 'attempts': [], 'endpoints': {}, 'clock': 0}
        try:
            state = json.loads(path.read_text())
            if (state['version'] != 1 or not isinstance(state['endpoints'], dict)
                    or not isinstance(state['clock'], (float, int))
                    or not isinstance(state['attempts'], list)
                    or any(not isinstance(x, (int, float)) for x in state['attempts'])):
                raise ValueError()
            return state
        except (ValueError, TypeError, KeyError):
            raise MaintenanceError('STATE_INVALID_NO_AUTOMATIC_RESET') from None

    def save(self, state):
        _private_root(self.root, create=True)
        atomic_json(self.root / 'state.json', state)

    def record(self, report):
        _private_root(self.root, create=True)
        phase = report.get('phase', 'ready' if report.get('identity_valid') else 'blocked')
        if (report.get('action') or {}).get('status') == 'REQUESTED':
            phase = 'recovering'
        report.update(pid=os.getpid(), updated_at=time.time(), phase=phase)
        atomic_json(self.root / 'status.json', report)
        path, rotated = self.root / 'events.jsonl', self.root / 'events.jsonl.1'
        if path.is_symlink() or rotated.is_symlink():
            raise MaintenanceError('STATE_FILE_SYMLINK_FORBIDDEN')
        if path.exists() and path.stat().st_size >= 1024 * 1024:
            os.replace(str(path), str(rotated))
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(report, sort_keys=True) + '\n')

    def probes(self):
        def endpoint(port, host):
            return {'dns': self.system.resolve(host), 'direct': self.system.probe(host, 443),
                    'local': self.system.probe(host, port, local=True)}
        with ThreadPoolExecutor(max_workers=4) as workers:
            pending = {str(port): workers.submit(endpoint, port, host) for port, host in ENDPOINTS.items()}
            local = workers.submit(self.system.probe, '127.0.0.1', 30080, local=True)
            result = {key: future.result() for key, future in pending.items()}
            result['30080'] = {'local': local.result(), 'observe_only': True}
            return result

    def cycle(self, apply=False, now=None):
        now = time.time() if now is None else now
        snapshot = self.system.snapshot()
        state = self.read()
        probes = self.probes()
        report = {'time': now, 'host': HOST, 'mode': 'active' if apply else 'check',
                  'identity_valid': True, 'config_sha256': snapshot['config_sha256'],
                  'nginx_active': snapshot['active'], 'probes': probes, 'action': None,
                  'healthy': snapshot['active'] and all(p['local']['reachable'] for p in probes.values())}
        stable_identity = state.get('config_sha256', snapshot['config_sha256']) == snapshot['config_sha256']
        if not stable_identity:
            state['endpoints'] = {}
        reasons = []
        for port in ENDPOINTS:
            key = str(port); row = probes[key]
            previous = state['endpoints'].get(key, {})
            dns = row['dns']
            repairable = row['local']['status'] in ('TCP_FAILED', 'TLS_OR_HTTP_FAILED')
            previous['failures'] = previous.get('failures', 0) + 1 if row['direct']['reachable'] and repairable else 0
            previous['dns_count'] = previous.get('dns_count', 0) + 1 if dns and dns == previous.get('candidate_dns') else 1 if dns else 0
            previous['candidate_dns'] = dns
            if 'accepted_dns' not in previous and dns:
                previous['accepted_dns'] = dns
            if dns and dns != previous.get('accepted_dns') and previous['dns_count'] >= 2 and row['direct']['reachable']:
                reasons.append('DNS_CHANGED_' + key)
            if previous['failures'] >= 3:
                reasons.append('LOCAL_FORWARD_FAILED_' + key)
            state['endpoints'][key] = previous
        operation = 'reload' if snapshot['active'] and reasons else None
        if not snapshot['active']:
            operation = 'start'; reasons = ['NGINX_INACTIVE']
        attempts = [value for value in state['attempts'] if now - value < 3600]
        blocked = None
        if now < state['clock']:
            blocked = 'CLOCK_MOVED_BACKWARD'
        elif self.stop_requested():
            blocked = 'STOP_REQUESTED'
        elif attempts and now - max(attempts) < COOLDOWN:
            blocked = 'RECOVERY_COOLDOWN'
        elif len(attempts) >= HOURLY_LIMIT:
            blocked = 'RECOVERY_HOURLY_LIMIT'
        report.update(planned_action=operation, reasons=reasons, blocked=blocked if operation else None)
        if not apply:
            report['status'] = 'CHECK_PASSED' if report['healthy'] else 'CHECK_DEGRADED'
            return report
        state['attempts'] = attempts
        state['clock'] = max(now, state['clock'])
        state['config_sha256'] = snapshot['config_sha256']
        self.save(state)
        report['status'] = 'HEALTHY' if report['healthy'] else 'DEGRADED'
        report['phase'] = 'recovering' if operation and not blocked else 'ready'
        # Readiness means a complete observation; recovery, if needed, is separate.
        self.record(report)
        if operation and not blocked:
            again = self.system.snapshot()
            if any(again[k] != snapshot[k] for k in ('config_sha256', 'unit_sha256', 'pid', 'active', 'binary', 'main', 'prefix')):
                report['blocked'] = 'IDENTITY_CHANGED_BEFORE_ACTION'
            else:
                state['attempts'].append(now)
                # Persist the budget before requesting recovery, including uncertain outcomes.
                self.save(state)
                report['action'] = {'operation': operation, 'status': 'REQUESTED'}
                self.record(report)
                try:
                    self.system.action(operation, again)
                    report['action']['status'] = 'COMMAND_SUCCEEDED'
                except MaintenanceError:
                    report['action']['status'] = 'COMMAND_FAILED_OR_UNCERTAIN'
                report['after'] = self.probes()
                current = self.system.snapshot()
                report['healthy'] = current['active'] and all(p['local']['reachable'] for p in report['after'].values())
                report['action']['recovery_verified'] = report['healthy']
                if report['action']['status'] == 'COMMAND_SUCCEEDED':
                    for key, previous in state['endpoints'].items():
                        if report['after'][key]['local']['reachable'] and report['after'][key]['dns'] == previous['candidate_dns']:
                            previous['accepted_dns'] = previous['candidate_dns']
                            previous['failures'] = 0
                    self.save(state)
        report['status'] = 'HEALTHY' if report['healthy'] else 'DEGRADED'
        report['phase'] = 'ready'
        self.record(report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--daemon', action='store_true')
    parser.add_argument('--state-dir', default=STATE_DIR)
    args = parser.parse_args(argv)
    maintenance = Maintenance(state_dir=args.state_dir)
    if args.check:
        print(json.dumps(maintenance.cycle(), sort_keys=True)); return 0
    if os.geteuid() != 0:
        raise MaintenanceError('ROOT_REQUIRED')
    check_host()
    root = _private_root(maintenance.root, create=True)
    fd = os.open(str(root / 'maintenance.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise MaintenanceError('MAINTENANCE_ALREADY_RUNNING') from None
        stop = threading.Event()
        maintenance.stop_requested = stop.is_set
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            try:
                report = maintenance.cycle(apply=True)
            except MaintenanceError as error:
                report = {'time': time.time(), 'status': 'BLOCKED', 'error': str(error)}
                maintenance.record(report)
            print(json.dumps(report, sort_keys=True), flush=True)
            if args.once:
                return 2 if report['status'] == 'BLOCKED' else 0
            stop.wait(INTERVAL)
    finally:
        os.close(fd)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        code = str(error) if isinstance(error, MaintenanceError) else 'MAINTENANCE_INTERNAL_ERROR'
        print(json.dumps({'status': 'BLOCKED', 'error': code}), file=sys.stderr)
        sys.exit(2)
