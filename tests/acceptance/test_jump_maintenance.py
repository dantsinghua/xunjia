"""Independent offline network-maintenance acceptance; never operate host services."""
import importlib.util
import fcntl
import json
import os
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location('independent_' + name, ROOT/'deploy'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer():
    return load('install_jump_maintenance')


class Service:
    def __init__(self):
        self.active = False
        self.enabled = False
        self.calls = []
        self.fail_start_once = False
        self.fail_preflight = False

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == 'ip':
            return 0, '2: eth0 inet 172.26.64.36/24'
        if args[0] != 'systemctl':
            assert args[1:3] == ['-I', '-B'] and args[-1] == '--check'
            return (1, 'synthetic failure') if self.fail_preflight else (0, '{}')
        assert args[-1] == 'xunjia-network-maintenance.service' or args[1] == 'daemon-reload'
        op = args[1]
        if op == 'is-active':
            return (0 if self.active else 3, 'active' if self.active else 'inactive')
        if op == 'is-enabled':
            return (0 if self.enabled else 1, 'enabled' if self.enabled else 'disabled')
        if op == 'enable':
            self.enabled = True
        elif op == 'disable':
            self.enabled = False
        elif op == 'stop':
            self.active = False
        elif op == 'start':
            if self.fail_start_once:
                self.fail_start_once = False
                return 1, 'synthetic start failure'
            self.active = True
        elif op != 'daemon-reload':
            pytest.fail('Unexpected service operation: ' + repr(args))
        return 0, ''


@pytest.fixture
def install_case(tmp_path, installer):
    bundle = tmp_path/'bundle'
    bundle.mkdir()
    (bundle/'jump_maintenance.py').write_text('print("synthetic runtime")\n')
    root = tmp_path/'root'
    root.mkdir()
    (root/'nginx.conf').write_bytes(b'original unrelated nginx config\r\n')
    service = Service()
    def run(**kwargs):
        return installer.change(bundle, root=root, runner=service, python='/usr/bin/python3',
                                readiness=lambda *args: None, **kwargs)
    return root, bundle, service, run


def test_installer_readonly_has_no_files_or_service_mutations(install_case):
    root, _, service, run = install_case
    assert run()['status'] == 'CHECK_PASSED_NO_CHANGES'
    assert [p.name for p in root.iterdir()] == ['nginx.conf']
    assert all(c[0] != 'systemctl' or c[1] in ('is-active', 'is-enabled') for c in service.calls)


def test_install_repeat_and_uninstall_preserve_nginx_and_history(installer, install_case):
    root, _, service, run = install_case
    assert run(apply=True)['status'] == 'INSTALLED_AND_RUNNING'
    assert service.active and service.enabled
    state = root/installer.STATE
    (state/'history-canary').write_text('previous failure and budget must survive')
    service.calls.clear()
    assert run(apply=True)['status'] == 'ALREADY_INSTALLED_AND_RUNNING'
    assert not any(c[0] == 'systemctl' and c[1] in ('start', 'stop', 'enable') for c in service.calls)
    assert run(apply=True, uninstall=True)['status'] == 'UNINSTALLED_STATE_RETAINED'
    assert not service.active and not service.enabled
    assert not (root/installer.PROGRAM).exists()
    assert (state/'history-canary').read_text() == 'previous failure and budget must survive'
    assert (root/'nginx.conf').read_bytes() == b'original unrelated nginx config\r\n'


def test_install_failure_restores_exact_previous_files_and_activation(installer, install_case):
    root, bundle, service, run = install_case
    run(apply=True)
    paths = [root/name for name in (installer.PROGRAM, installer.UNIT, installer.OWNER)]
    before = {p:p.read_bytes() for p in paths}
    (bundle/'jump_maintenance.py').write_text('print("new synthetic runtime")\n')
    service.fail_start_once = True
    with pytest.raises(installer.InstallError, match='PREVIOUS_FILES_RESTORED'):
        run(apply=True)
    assert {p:p.read_bytes() for p in paths} == before
    assert service.active and service.enabled


def test_foreign_installation_and_changed_owned_file_refuse(installer, install_case):
    root, _, service, run = install_case
    program = root/installer.PROGRAM
    program.parent.mkdir(parents=True)
    program.write_text('foreign program')
    with pytest.raises(installer.InstallError, match='UNOWNED'):
        run(apply=True)
    assert program.read_text() == 'foreign program'
    program.unlink()
    run(apply=True)
    program.write_text('operator edited program')
    with pytest.raises(installer.InstallError, match='INSTALLED_FILES_CHANGED'):
        run(apply=True)
    assert program.read_text() == 'operator edited program'


def test_symlink_install_target_refuses_without_external_write(installer, install_case, tmp_path):
    root, _, service, run = install_case
    external = tmp_path/'external'
    external.mkdir()
    (root/'opt').symlink_to(external, target_is_directory=True)
    with pytest.raises(installer.InstallError, match='SYMLINK'):
        run(apply=True)
    assert list(external.iterdir()) == []


def test_failed_preflight_does_not_install_or_touch_service(installer, install_case):
    root, _, service, run = install_case
    service.fail_preflight = True
    with pytest.raises(installer.InstallError):
        run(apply=True)
    assert not (root/installer.PROGRAM).exists()
    assert not any(c[0] == 'systemctl' for c in service.calls)


def test_uninstall_remains_available_when_network_preflight_is_broken(installer, install_case):
    root, _, service, run = install_case
    run(apply=True)
    service.fail_preflight = True
    assert run(apply=True, uninstall=True)['status'] == 'UNINSTALLED_STATE_RETAINED'
    assert not service.active and not (root/installer.PROGRAM).exists()


@pytest.fixture
def runtime():
    return load('jump_maintenance')


class Network:
    def __init__(self):
        self.identity = dict(active=True, config_sha256='config-one', unit_sha256='unit-one',
                             pid=12345, binary='/synthetic/nginx', main='/synthetic/nginx.conf', prefix='/synthetic')
        self.dns = ['192.0.2.1']
        self.local = dict(status='HTTP_REACHABLE', reachable=True)
        self.direct = dict(status='HTTP_REACHABLE', reachable=True)
        self.actions = []
        self.error = None
        self.recover = True
        self.snapshots = 0
        self.drift_at = None

    def snapshot(self):
        self.snapshots += 1
        value = dict(self.identity)
        if self.snapshots == self.drift_at:
            value['pid'] += 1
        return value

    def resolve(self, host):
        return list(self.dns)

    def probe(self, host, port, local=False):
        if port == 30080:
            return dict(status='HTTP_REACHABLE', reachable=True)
        return dict(self.local if local else self.direct)

    def action(self, operation, snapshot):
        self.actions.append(operation)
        if self.error:
            raise self.error
        if self.recover:
            self.identity['active'] = True
            self.local = dict(status='HTTP_REACHABLE', reachable=True)


def maintain(runtime, tmp_path, network):
    return runtime.Maintenance(system=network, state_dir=tmp_path/'private')


def test_readonly_does_not_create_state_or_execute_recovery(runtime, tmp_path):
    n = Network(); n.identity['active'] = False
    m = maintain(runtime, tmp_path, n)
    assert m.cycle(now=1000)['planned_action'] == 'start'
    assert not m.root.exists() and n.actions == []


def test_three_local_failures_required_and_post_action_health_is_verified(runtime, tmp_path):
    n = Network(); n.local = dict(status='TCP_FAILED', reachable=False)
    m = maintain(runtime, tmp_path, n)
    for now in (1000, 1030):
        assert m.cycle(apply=True, now=now)['action'] is None
    result = m.cycle(apply=True, now=1060)
    assert n.actions == ['reload'] and result['healthy']
    assert result['action']['recovery_verified'] is True
    assert json.loads((m.root/'state.json').read_text())['attempts'] == [1060]


def test_dns_rotation_requires_two_consistent_observations(runtime, tmp_path):
    n = Network(); m = maintain(runtime, tmp_path, n)
    m.cycle(apply=True, now=1000)
    n.dns = ['192.0.2.2']
    assert m.cycle(apply=True, now=1030)['action'] is None
    result = m.cycle(apply=True, now=1060)
    assert n.actions == ['reload'] and any(x.startswith('DNS_CHANGED') for x in result['reasons'])
    assert m.cycle(apply=True, now=1090)['action'] is None


@pytest.mark.parametrize('kind', ['TCP_FAILED', 'DNS_FAILED', 'TLS_VERIFY_FAILED'])
def test_upstream_failure_never_triggers_local_repair(runtime, tmp_path, kind):
    n = Network(); n.direct = dict(status=kind, reachable=False)
    n.local = dict(status='TCP_FAILED', reachable=False)
    m = maintain(runtime, tmp_path, n)
    for now in (1000, 1030, 1060, 1090):
        assert m.cycle(apply=True, now=now)['action'] is None
    assert n.actions == []


def test_local_certificate_failure_is_not_treated_as_restartable_connectivity(runtime, tmp_path):
    n = Network(); n.local = dict(status='TLS_VERIFY_FAILED', reachable=False)
    m = maintain(runtime, tmp_path, n)
    for now in (1000, 1030, 1060, 1090):
        assert m.cycle(apply=True, now=now)['action'] is None
    assert n.actions == []


def test_client_certificate_requirement_is_reachable_without_recovery(runtime, tmp_path):
    n = Network(); n.local = dict(status='CLIENT_CERTIFICATE_REQUIRED', reachable=True)
    n.direct = dict(n.local)
    m = maintain(runtime, tmp_path, n)
    for now in (1000, 1030, 1060):
        assert m.cycle(apply=True, now=now)['healthy']
    assert n.actions == []


def test_uncertain_action_budget_survives_new_instance_and_hourly_limit(runtime, tmp_path):
    n = Network(); n.identity['active'] = False
    n.local = dict(status='TCP_FAILED', reachable=False)
    n.error = runtime.MaintenanceError('SYNTHETIC_COMMAND_FAILURE')
    m = maintain(runtime, tmp_path, n)
    first = m.cycle(apply=True, now=1000)
    assert first['action']['status'] == 'COMMAND_FAILED_OR_UNCERTAIN'
    assert first['action']['recovery_verified'] is False
    m = maintain(runtime, tmp_path, n)
    assert m.cycle(apply=True, now=1030)['blocked'] == 'RECOVERY_COOLDOWN'
    m.cycle(apply=True, now=1300); m.cycle(apply=True, now=1600)
    assert m.cycle(apply=True, now=1900)['blocked'] == 'RECOVERY_HOURLY_LIMIT'
    assert n.actions == ['start', 'start', 'start']


def test_identity_change_before_action_blocks_mutation(runtime, tmp_path):
    n = Network(); n.identity['active'] = False; n.drift_at = 2
    m = maintain(runtime, tmp_path, n)
    result = m.cycle(apply=True, now=1000)
    assert result['blocked'] == 'IDENTITY_CHANGED_BEFORE_ACTION'
    assert n.actions == []


def test_clock_rollback_and_corrupt_state_cannot_reset_budget(runtime, tmp_path):
    n = Network(); n.identity['active'] = False
    m = maintain(runtime, tmp_path, n); m.cycle(apply=True, now=1000)
    n.identity['active'] = False
    assert m.cycle(apply=True, now=900)['blocked'] == 'CLOCK_MOVED_BACKWARD'
    (m.root/'state.json').write_text('not json')
    with pytest.raises(runtime.MaintenanceError, match='STATE_INVALID'):
        m.cycle(apply=True, now=2000)
    assert n.actions == ['start']


def test_failed_nginx_test_never_calls_systemctl_and_does_not_print_config(runtime, monkeypatch):
    calls = []
    def command(args, timeout=15):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout='secret fixture', stderr='synthetic_password_do_not_print')
    monkeypatch.setattr(runtime, 'command', command)
    with pytest.raises(runtime.MaintenanceError, match='COMMAND_FAILED') as error:
        runtime.System().action('reload', {'test_args':['/synthetic/nginx', '-t']})
    assert len(calls) == 1 and calls[0][-1] == '-t'
    assert 'password' not in str(error.value)


def test_wrong_host_is_rejected_before_system_or_network_operations(runtime, monkeypatch):
    calls = []
    def command(args, timeout=15):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='2: eth0 inet 192.0.2.3/24', stderr='')
    monkeypatch.setattr(runtime, 'command', command)
    with pytest.raises(runtime.MaintenanceError, match='WRONG_HOST'):
        runtime.System().snapshot()
    assert calls == [['ip', '-o', '-4', 'addr', 'show']]


def test_mapping_parser_requires_four_exact_static_stream_routes(runtime):
    body = '\n'.join('server { listen %d; proxy_pass %s:443; }' % (p,h) for p,h in runtime.ENDPOINTS.items())
    dump = '# configuration file /etc/nginx/nginx.conf:\nevents {}\nstream { '+body+' }\n'
    assert runtime.parse_mapping(dump, '/etc/nginx/nginx.conf', '/etc/nginx')['mappings'] == runtime.ENDPOINTS
    for corrupted in (dump.replace('stream {', 'http {'), dump.replace('its-relay.yingmi.cn:443', '$unknown'),
                      dump.replace('listen 50443;', 'listen 50443 ssl;')):
        with pytest.raises(runtime.MaintenanceError):
            runtime.parse_mapping(corrupted, '/etc/nginx/nginx.conf', '/etc/nginx')


@pytest.fixture
def tls_server(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
            .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None), True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),False).sign(key,hashes.SHA256()))
    cp, kp = tmp_path/'server.crt', tmp_path/'server.key'
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    requests, snis = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_HEAD(self):
            requests.append({'method':'HEAD','host':self.headers['Host'],'authorization':self.headers.get('Authorization')})
            self.send_response(401); self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cp,kp)
    context.set_servername_callback(lambda sock,name,ctx:snis.append(name))
    server.socket = context.wrap_socket(server.socket,server_side=True)
    thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True); thread.start()
    try:
        yield server, context, cp, requests, snis
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_real_tls_probe_keeps_sni_host_validation_and_http_auth_semantics(runtime, tls_server, monkeypatch):
    server, context, cert, requests, snis = tls_server
    original = runtime.command
    def trusted(args, timeout=15):
        assert args[0] == 'curl' and '--insecure' not in args and '-k' not in args
        assert '--noproxy' in args
        return original(args[:-1]+['--cacert',str(cert),args[-1]],timeout)
    monkeypatch.setattr(runtime, 'command', trusted)
    result = runtime.System().probe('localhost',server.server_port,local=True)
    assert result['status'] == 'HTTP_REACHABLE' and result['http'] == 401 and result['reachable']
    assert snis == ['localhost'] and requests == [{'method':'HEAD','host':'localhost:'+str(server.server_port),'authorization':None}]
    monkeypatch.setattr(runtime, 'command', original)
    rejected = runtime.System().probe('localhost',server.server_port,local=True)
    assert rejected['status'] == 'TLS_VERIFY_FAILED' and not rejected['reachable']


def test_real_mutual_tls_missing_client_certificate_is_not_network_down(runtime, tls_server, monkeypatch):
    server, context, cert, requests, snis = tls_server
    context.verify_mode = ssl.CERT_REQUIRED; context.load_verify_locations(str(cert))
    original = runtime.command
    observed = []
    def trusted(args, timeout=15):
        result = original(args[:-1]+['--cacert',str(cert),args[-1]],timeout)
        observed.append((result.returncode,result.stdout,result.stderr))
        return result
    monkeypatch.setattr(runtime, 'command', trusted)
    result = runtime.System().probe('localhost',server.server_port,local=True)
    assert result['status'] in ('CLIENT_CERTIFICATE_REQUIRED', 'TLS_VERIFIED_HTTP_UNCONFIRMED'), observed
    if result['status'] == 'CLIENT_CERTIFICATE_REQUIRED':
        assert result['reachable']
    else:
        assert not result['reachable']
    assert requests == []


def test_tls_verified_but_http_unconfirmed_is_neither_healthy_nor_repairable(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'command', lambda args,timeout=15:SimpleNamespace(
        returncode=55, stdout='000 0.000113 0.002673 0', stderr='curl: (55) Send failure: Connection reset by peer'))
    probe = runtime.System().probe('localhost', 50443, local=True)
    assert probe['status'] == 'TLS_VERIFIED_HTTP_UNCONFIRMED' and not probe['reachable']
    n = Network(); n.local = probe
    m = maintain(runtime, tmp_path, n)
    for now in (1000, 1030, 1060, 1090):
        result = m.cycle(apply=True, now=now)
        assert not result['healthy'] and result['action'] is None
    assert n.actions == []


@pytest.mark.parametrize('bad', ['old_pid', 'old_timestamp', 'blocked_phase'])
def test_ready_requires_current_pid_fresh_time_and_ready_phase(installer, tmp_path, monkeypatch, bad):
    root = tmp_path/'root'; path = root/installer.STATE/'status.json';path.parent.mkdir(parents=True)
    value = {'pid':4242,'updated_at':1000,'phase':'ready'}
    if bad == 'old_pid': value['pid'] = 4241
    if bad == 'old_timestamp': value['updated_at'] = 999
    if bad == 'blocked_phase': value['phase'] = 'blocked'
    path.write_text(json.dumps(value))
    ticks = [0]
    monkeypatch.setattr(installer.time, 'monotonic', lambda:ticks[0])
    monkeypatch.setattr(installer.time, 'sleep', lambda seconds:ticks.__setitem__(0,ticks[0]+seconds))
    def runner(args):
        if args[1] == 'is-active': return 0,'active'
        if args[1] == 'is-enabled': return 0,'enabled'
        assert args[1] == 'show'
        return 0,'4242'
    with pytest.raises(installer.InstallError,match='STATUS_NOT_READY'):
        installer.await_ready(root,runner,1000)
    path.write_text(json.dumps({'pid':4242,'updated_at':1000,'phase':'ready'}))
    installer.await_ready(root,runner,1000)


def test_second_active_instance_is_rejected_before_any_probe(runtime, tmp_path, monkeypatch):
    root=tmp_path/'private';root.mkdir(mode=0o700)
    fd=os.open(root/'maintenance.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        monkeypatch.setattr(runtime,'check_host',lambda:None)
        # Fixture privilege and private path are injected; the real flock is not.
        monkeypatch.setattr(runtime,'_private_root',lambda path,create=False:root)
        monkeypatch.setattr(runtime.os,'geteuid',lambda:0)
        monkeypatch.setattr(runtime.Maintenance,'cycle',lambda *a,**kw:pytest.fail('second instance probed'))
        with pytest.raises(runtime.MaintenanceError,match='ALREADY_RUNNING'):
            runtime.main(['--once','--state-dir',str(root)])
    finally:
        os.close(fd)


def test_event_log_is_bounded_and_state_symlinks_cannot_write_outside(runtime, tmp_path):
    m=maintain(runtime,tmp_path,Network());m.cycle(apply=True,now=1000)
    log=m.root/'events.jsonl';log.write_text('x'*(1024*1024))
    m.record({'status':'synthetic'})
    assert log.stat().st_size<1024 and (m.root/'events.jsonl.1').stat().st_size==1024*1024
    external=tmp_path/'external';external.write_text('must remain')
    (m.root/'state.json').unlink();(m.root/'state.json').symlink_to(external)
    with pytest.raises(runtime.MaintenanceError,match='SYMLINK'):
        m.cycle(apply=True,now=1030)
    assert external.read_text()=='must remain'


def test_unit_reload_identity_rejects_other_commands_and_configuration(runtime):
    props = ('LoadState=loaded\nActiveState=active\nMainPID=4242\n'
             'ExecStart={ path=/usr/sbin/nginx ; argv[]=/usr/sbin/nginx -c /etc/nginx/nginx.conf ; ignore_errors=no ; pid=4242 ; code=(null) ; status=0/0 }\n'
             'ExecReload={ path=/bin/kill ; argv[]=/bin/kill -s HUP $MAINPID ; ignore_errors=no ; pid=0 ; code=(null) ; status=0/0 }\n'
             'FragmentPath=/lib/systemd/system/nginx.service\nDropInPaths=\n')
    parsed = runtime.parse_unit(props)
    assert parsed['reload_args'] == ['/bin/kill','-s','HUP','$MAINPID']
    other = runtime.parse_unit(props.replace('DropInPaths=', 'DropInPaths=/etc/systemd/system/nginx.service.d/operator.conf'))
    assert other['unit_sha256'] != parsed['unit_sha256']
    for bad in (props.replace('-s HUP', '-s TERM'), props.replace('FragmentPath=/lib/', 'FragmentPath=/tmp/'),
                props.replace('path=/bin/kill ; argv[]=/bin/kill -s HUP $MAINPID',
                              'path=/usr/sbin/nginx ; argv[]=/usr/sbin/nginx -c /etc/other.conf -s reload')):
        with pytest.raises(runtime.MaintenanceError):
            runtime.parse_unit(bad)
