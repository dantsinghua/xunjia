"""Runtime recovery policy tests. No real services or Internet access."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import jump_maintenance as jm


def configuration(bind=''):
    servers = ['server { listen %s%s; proxy_pass %s:443; }' % (bind, port, host)
               for port, host in jm.ENDPOINTS.items()]
    return '# configuration file /etc/nginx/nginx.conf:\nstream {\n' + '\n'.join(servers) + '\n}\n'


class System:
    def __init__(self):
        self.info = dict(active=True, pid=123, binary='/usr/sbin/nginx', main='/etc/nginx/nginx.conf',
                         prefix='/usr/share/nginx', config_sha256='known', unit_sha256='known-unit')
        self.ips = ['192.0.2.1']
        self.direct = True
        self.local = True
        self.actions = []
        self.failure = False
        self.restores = True
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return copy.deepcopy(self.info)

    def resolve(self, host): return self.ips[:]

    def probe(self, host, port, local=False):
        return {'status': 'HTTP_REACHABLE' if (self.local if local else self.direct) else 'TCP_FAILED',
                'reachable': self.local if local else self.direct}

    def action(self, operation, snapshot):
        self.actions.append(operation)
        if self.failure: raise jm.MaintenanceError('COMMAND_FAILED')
        if self.restores:
            self.local = True
            self.info.update(active=True, pid=123)


@pytest.fixture
def runtime(tmp_path):
    system = System()
    return jm.Maintenance(system, tmp_path/'private'), system


def test_check_observes_without_creating_state_or_changing_service(runtime):
    maintenance, system = runtime
    system.local = False
    report = maintenance.cycle(now=1000)
    assert report['status'] == 'CHECK_DEGRADED' and report['identity_valid']
    assert not maintenance.root.exists() and not system.actions


def test_three_failures_recover_once_and_report_actual_after_state(runtime):
    maintenance, system = runtime
    system.local = False
    for now in (1000, 1030):
        report = maintenance.cycle(apply=True, now=now)
        assert report['action'] is None
    report = maintenance.cycle(apply=True, now=1060)
    assert system.actions == ['reload']
    assert report['action']['recovery_verified'] and report['status'] == 'HEALTHY'
    status = json.loads((maintenance.root/'status.json').read_text())
    assert status['phase'] == 'ready' and status['pid'] > 0 and status['updated_at'] > 0
    assert maintenance.root.stat().st_mode & 0o777 == 0o700


def test_upstream_outage_does_not_trigger_recovery(runtime):
    maintenance, system = runtime
    system.local = system.direct = False
    for i in range(4): maintenance.cycle(apply=True, now=1000+i*30)
    assert not system.actions


def test_dns_change_requires_two_observations_and_direct_reachability(runtime):
    maintenance, system = runtime
    maintenance.cycle(apply=True, now=1000)
    system.ips = ['192.0.2.2']
    maintenance.cycle(apply=True, now=1030)
    assert not system.actions
    system.direct = False
    maintenance.cycle(apply=True, now=1060)
    assert not system.actions
    system.direct = True
    report = maintenance.cycle(apply=True, now=1090)
    assert system.actions == ['reload']
    assert any(reason.startswith('DNS_CHANGED') for reason in report['reasons'])
    maintenance.cycle(apply=True, now=1400)
    assert len(system.actions) == 1


def test_cooldown_budget_survives_new_runtime_and_failed_command(runtime):
    maintenance, system = runtime
    system.info.update(active=False, pid=0)
    system.failure = True
    report = maintenance.cycle(apply=True, now=1000)
    assert report['action']['status'] == 'COMMAND_FAILED_OR_UNCERTAIN'
    replacement = jm.Maintenance(system, maintenance.root)
    report = replacement.cycle(apply=True, now=1020)
    assert report['blocked'] == 'RECOVERY_COOLDOWN'
    assert system.actions == ['start']
    replacement.cycle(apply=True, now=1300)
    replacement.cycle(apply=True, now=1600)
    report = replacement.cycle(apply=True, now=1900)
    assert report['blocked'] == 'RECOVERY_HOURLY_LIMIT' and len(system.actions) == 3


def test_command_success_is_not_claimed_as_recovery_without_probe_success(runtime):
    maintenance, system = runtime
    system.local = False; system.restores = False
    for now in (1000, 1030, 1060): report = maintenance.cycle(apply=True, now=now)
    assert report['action']['status'] == 'COMMAND_SUCCEEDED'
    assert report['action']['recovery_verified'] is False
    assert report['status'] == 'DEGRADED'


def test_inactive_nginx_is_started_not_restarted(runtime):
    maintenance, system = runtime
    system.info.update(active=False, pid=0)
    report = maintenance.cycle(apply=True, now=1000)
    assert system.actions == ['start'] and report['healthy']


def test_changed_identity_between_plan_and_action_blocks(runtime):
    maintenance, system = runtime
    system.info.update(active=False, pid=0)
    def snapshot():
        system.snapshots += 1
        return dict(system.info, config_sha256='one' if system.snapshots == 1 else 'two')
    system.snapshot = snapshot
    assert maintenance.cycle(apply=True, now=1000)['blocked'] == 'IDENTITY_CHANGED_BEFORE_ACTION'
    assert not system.actions


def test_backward_clock_and_shutdown_prevent_recovery(runtime):
    maintenance, system = runtime
    maintenance.cycle(apply=True, now=1000)
    system.info.update(active=False, pid=0)
    assert maintenance.cycle(apply=True, now=999)['blocked'] == 'CLOCK_MOVED_BACKWARD'
    maintenance.stop_requested = lambda: True
    assert maintenance.cycle(apply=True, now=1001)['blocked'] == 'STOP_REQUESTED'
    assert not system.actions


def test_mapping_parser_handles_static_include_relative_to_main_config():
    servers = configuration().split('stream {\n', 1)[1].rsplit('\n}', 1)[0]
    text = ('# configuration file /etc/nginx/nginx.conf:\nstream { include stream.conf; }\n'
            '# configuration file /etc/nginx/stream.conf:\n'+servers)
    result = jm.parse_mapping(text, '/etc/nginx/nginx.conf', '/usr/share/nginx')
    assert result['mappings'] == jm.ENDPOINTS
    assert set(result['local_addresses'].values()) == {'127.0.0.1'}


def test_mapping_explicit_host_binding_is_used_for_local_probe(monkeypatch):
    parsed = jm.parse_mapping(configuration(jm.HOST+':'), '/etc/nginx/nginx.conf', '/etc/nginx')
    assert set(parsed['local_addresses'].values()) == {jm.HOST}
    system = jm.System(); system.local_addresses = parsed['local_addresses']
    commands = []
    def execute(args, timeout):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout='200 0.01 0.02 0', stderr='')
    monkeypatch.setattr(jm, 'command', execute)
    assert system.probe('its-relay.yingmi.cn', 50443, local=True)['reachable']
    assert 'its-relay.yingmi.cn:50443:'+jm.HOST in commands[0]
    assert '--insecure' not in commands[0] and '-k' not in commands[0]


@pytest.mark.parametrize('damage', ['variable','wrong-host','ssl','missing','ipv6-only'])
def test_unreviewed_nginx_configuration_is_rejected(damage):
    text = configuration()
    if damage == 'variable': text = text.replace('its-relay.yingmi.cn:443', '$upstream')
    elif damage == 'wrong-host': text = text.replace('its-relay.yingmi.cn', 'unknown.invalid')
    elif damage == 'ssl': text = text.replace('listen 50443;', 'listen 50443 ssl;')
    elif damage == 'missing': text = text.replace('listen 50443;', 'listen 50444;')
    else: text = configuration('[::]:')
    with pytest.raises(jm.MaintenanceError):
        jm.parse_mapping(text, '/etc/nginx/nginx.conf', '/etc/nginx')


def unit(state='active', pid=123, executable='/usr/sbin/nginx'):
    return ('LoadState=loaded\nActiveState=%s\nMainPID=%s\n'
            'FragmentPath=/lib/systemd/system/nginx.service\n'
            'ExecReload={ path=/bin/kill ; argv[]=/bin/kill -s HUP $MAINPID ; ignore_errors=no ; pid=0 ; code=(null) ; status=0/0 }\n'
            'ExecStart={ path=%s ; argv[]=%s -g daemon on; master_process on; ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n') % (state,pid,executable,executable)


def test_service_parser_accepts_standard_nginx_and_rejects_wrappers():
    parsed = jm.parse_unit(unit())
    assert parsed['args'] == ['/usr/sbin/nginx','-g','daemon on; master_process on;']
    with pytest.raises(jm.MaintenanceError): jm.parse_unit(unit(executable='/bin/sh'))
    with pytest.raises(jm.MaintenanceError): jm.parse_unit(unit(state='activating'))
    with pytest.raises(jm.MaintenanceError): jm.parse_unit(unit(pid=0))


def test_reload_identity_rejects_unknown_scripts_or_different_config():
    text = unit()
    with pytest.raises(jm.MaintenanceError, match='EXECRELOAD'):
        jm.parse_unit(text.replace('/bin/kill', '/usr/local/bin/custom-reload'))
    nginx_reload = text.replace('path=/bin/kill ; argv[]=/bin/kill -s HUP $MAINPID',
                              'path=/usr/sbin/nginx ; argv[]=/usr/sbin/nginx -g daemon on; master_process on; -s reload')
    assert jm.parse_unit(nginx_reload)['reload_args'][-2:] == ['-s','reload']
    with pytest.raises(jm.MaintenanceError, match='CONFIG_MISMATCH'):
        jm.parse_unit(nginx_reload.replace('-s reload', '-c /etc/other/nginx.conf -s reload'))


@pytest.mark.parametrize('exit_code,output,error,status,reachable', [
    (0,'401 0.01 0.02 0','','HTTP_REACHABLE',True),
    (56,'000 0.01 0.02 0','tlsv13 alert certificate required','CLIENT_CERTIFICATE_REQUIRED',True),
    (35,'000 0.01 0 0','alert bad certificate','CLIENT_CERTIFICATE_REQUIRED',True),
    (60,'000 0.01 0 20','certificate required','TLS_VERIFY_FAILED',False),
    (55,'000 0.01 0.02 0','Send failure: Connection reset by peer','TLS_VERIFIED_HTTP_UNCONFIRMED',False),
    (28,'000 0 0 0','timeout','TCP_FAILED',False),
    (6,'000 0 0 0','resolve failed','DNS_FAILED',False),
])
def test_probe_distinguishes_authentication_and_transport(monkeypatch, exit_code, output, error, status, reachable):
    monkeypatch.setattr(jm, 'command', lambda *_: SimpleNamespace(returncode=exit_code, stdout=output, stderr=error))
    value = jm.System().probe('its-relay.yingmi.cn', 443)
    assert value['status'] == status and value['reachable'] is reachable


def test_state_symlink_and_corruption_fail_closed(runtime, tmp_path):
    maintenance, system = runtime
    maintenance.cycle(apply=True, now=1000)
    state = maintenance.root/'state.json'
    state.write_text('not JSON')
    with pytest.raises(jm.MaintenanceError, match='STATE_INVALID'): maintenance.cycle(apply=True)
    state.unlink(); state.symlink_to(tmp_path/'outside')
    with pytest.raises(jm.MaintenanceError, match='SYMLINK'): maintenance.cycle(apply=True)
    assert not system.actions


def test_budget_is_durable_before_recovery_request(runtime):
    maintenance, system = runtime
    system.info.update(active=False, pid=0)
    def action(*args):
        state = json.loads((maintenance.root/'state.json').read_text())
        assert state['attempts'] == [1000]
        status = json.loads((maintenance.root/'status.json').read_text())
        assert status['action']['status'] == 'REQUESTED'
        assert status['phase'] == 'recovering'
        raise jm.MaintenanceError('COMMAND_FAILED')
    system.action = action
    assert maintenance.cycle(apply=True, now=1000)['action']['status'] == 'COMMAND_FAILED_OR_UNCERTAIN'


def test_log_rotation_is_bounded_and_private(runtime):
    maintenance, _ = runtime
    maintenance.cycle(apply=True, now=1000)
    path = maintenance.root/'events.jsonl'
    path.write_text('x'*(1024*1024))
    maintenance.cycle(apply=True, now=1030)
    assert (maintenance.root/'events.jsonl.1').stat().st_size == 1024*1024
    assert path.stat().st_size < 10000 and path.stat().st_mode & 0o777 == 0o600
