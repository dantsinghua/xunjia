"""Independent organization failure wire/projection contracts; synthetic data only."""
from collections import deque
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest

from relay.file_gateway import GatewayAuth
from relay.gateway_paths import CONTRACT, ORGANIZATION_PATH, InstitutionKeys, account_paths
from relay.institution_directory import InstitutionDirectory
from relay.protocol import Codec, envelope


@pytest.fixture
def directory(tmp_path):
    now = [1800000000.0]
    keys = InstitutionKeys.from_auth(GatewayAuth('synthetic-org-contract', b'0' * 16))
    directory = InstitutionDirectory(tmp_path / 'directory.db', keys, clock=lambda: now[0])
    wb, uid = 'synthetic-wb', 'synthetic-account'
    binding = keys.binding(wb, 'directory')
    directory.put(envelope(binding, 'manifest', {'contract': CONTRACT, 'op': 'directory',
        'revision': 1, 'updated_at': now[0], 'accounts': [{'account_uid': uid, 'account_id': 'synthetic-native',
        'channel': 'qidian', 'online': True, 'paths': account_paths(wb, uid), 'capabilities': []}]},
        message_id='synthetic-directory-1', created_at=now[0], expires_at=now[0] + 3600))
    queue = deque()
    class Channel:
        path = ORGANIZATION_PATH
        def pending(self): return len(queue)
        def download(self): return queue.popleft() if queue else None
    revision = [0]
    def publish(state, *, org=None, code=None):
        revision[0] += 1
        payload = {'contract': CONTRACT, 'op': 'organization', 'revision': revision[0],
                   'updated_at': now[0], 'state': state}
        if org is not None: payload['org'] = org
        if code is not None: payload['error_code'] = code
        binding = keys.binding(wb, 'organization')
        message = envelope(binding, 'manifest', payload, message_id='synthetic-org-' + str(revision[0]),
                           created_at=now[0], expires_at=now[0] + 3600)
        blob = Codec(binding, clock=lambda: now[0]).pack(message)
        queue.append(SimpleNamespace(name='synthetic-organization.bin', content=blob))
        assert directory.consume(Channel()) == 1
        return payload
    def consume_blob(path, name, blob):
        queue.append(SimpleNamespace(name=name, content=blob))
        assert path == ORGANIZATION_PATH
        return directory.consume(Channel())
    return SimpleNamespace(store=directory, now=now, keys=keys, publish=publish, consume_blob=consume_blob,
                           wb=wb, uid=uid, contacts=lambda: directory.contacts(wb, uid, 'synthetic-viewer'))


def good_org(label='last-good'):
    return {'departments': [{'id': 'synthetic-department', 'name': label, 'parent_id': ''}],
            'members': [{'uin': 'synthetic-person', 'name': label, 'department_id': 'synthetic-department'}]}


def test_exact_safe_failure_code_crosses_authenticated_wire_without_erasing_good_cache(directory):
    directory.publish('ready', org=good_org())
    before = directory.contacts()
    directory.now[0] += 5
    directory.publish('error', code='ORG_DECRYPT_HEADER_INVALID')
    after = directory.contacts()
    assert after['org_error'] == 'ORG_DECRYPT_HEADER_INVALID'
    assert after['org_state'] == 'error'
    assert after['org'] == before['org'] and after['org_updated_at'] == before['org_updated_at']


@pytest.mark.parametrize('state', ['error', 'unreported'])
def test_failure_without_success_cache_never_fabricates_empty_organization(directory, state):
    directory.publish(state, code='ORG_CREDENTIALS_NOT_CONFIGURED')
    result = directory.contacts()
    assert result['org_state'] == 'error' and result['org_state'] != 'empty'
    assert result['org'] == {} and result['org_updated_at'] is None
    assert result['org_error'] == 'ORG_CREDENTIALS_NOT_CONFIGURED'


@pytest.mark.parametrize('code', [None, 'synthetic-private-detail', 'ORG_BAD\nsynthetic-secret',
                                 'ORG_' + 'X' * 76, 17, {'secret': 'synthetic'}])
def test_invalid_error_code_is_not_exposed(directory, code):
    directory.publish('error', code=code)
    result = directory.contacts()
    assert result['org_error'] == 'ORGANIZATION_REFRESH_FAILED'
    assert 'synthetic-private-detail' not in json.dumps(result) and 'synthetic-secret' not in json.dumps(result)


@pytest.mark.parametrize('state', ['ready', 'empty'])
def test_successful_new_snapshot_clears_error_and_replaces_old_cache(directory, state):
    directory.publish('ready', org=good_org())
    directory.now[0] += 5
    directory.publish('error', code='ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR')
    directory.now[0] += 5
    org = good_org('recovered') if state == 'ready' else {'departments': [], 'members': []}
    directory.publish(state, org=org)
    result = directory.contacts()
    assert result['org'] == org and result['org_state'] == state
    assert result['org_updated_at'] == directory.now[0] and result['org_error'] is None


@pytest.fixture
def producer(tmp_path, monkeypatch, directory):
    # Import only the current producer and its local host class; never AgentApp,
    # native adapters, credentials, ADB, network clients, or a real account store.
    root = Path('/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent/ossbox')
    package = types.ModuleType('_independent_org_failure_producer')
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    name = package.__name__ + '.institution'
    spec = importlib.util.spec_from_file_location(name, root / 'institution.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    state = {'book': {'org_state': 'ready', 'org': good_org()}, 'raise': None,
             'uploaded': [], 'during_upload': None, 'fail_upload': False}
    class Client:
        def __init__(self, path): self.path = path
        def upload(self, filename, blob):
            if self.path == ORGANIZATION_PATH:
                if state['fail_upload']: raise OSError('synthetic-upload-failure-no-secret')
                state['uploaded'].append(blob)
                directory.consume_blob(self.path, filename, blob)
                if state['during_upload']:
                    callback, state['during_upload'] = state['during_upload'], None
                    callback()
    row = {'id': 'synthetic-native', 'self_uid': directory.uid, 'channel': 'qidian', 'state': 'running'}
    store = SimpleNamespace(list_accounts=lambda channel: [row] if channel == 'qidian' else [])
    def provider(row):
        if state['raise'] is not None: raise state['raise']
        return state['book']
    loop = asyncio.new_event_loop()
    host = module.InstitutionCollectorHost(store=store, bus=None, loop=loop, keys=directory.keys,
        config={'workbench_name': 'synthetic-workbench', 'contract': CONTRACT},
        settings=SimpleNamespace(workbench_id=directory.wb), data_dir=tmp_path / 'producer',
        legacy_inbox_disabled=lambda: True, friends_provider=lambda row: [], organization_provider=provider,
        clock=lambda: directory.now[0], client_factory=Client)
    host.active = True
    try:
        yield SimpleNamespace(module=module, host=host, state=state, receiver=directory,
            publications=lambda: [Codec(directory.keys.binding(directory.wb, 'organization'),
                                       clock=lambda: directory.now[0]).unpack(blob)['payload'] for blob in state['uploaded']])
    finally:
        host.active = False
        loop.close()


def test_producer_failure_preserves_success_timestamp_and_sync_generation_then_recovers(producer):
    p = producer; r = p.receiver; host = p.host
    host.metadata_once(); before = r.contacts(); success_stamp = host.publisher.last('organization:organization')
    r.now[0] += 1
    p.state['book'] = {'org_state': 'error', 'org_error': 'ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR',
                       'org': good_org('must-not-publish-failed-content')}
    host.publisher.request_sync(r.uid, True)
    pending = host.publisher.pending_sync()
    host.metadata_once()
    failure = p.publications()[-1]
    assert failure['state'] == 'error' and failure['error_code'] == 'ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR'
    assert 'org' not in failure and 'must-not-publish' not in json.dumps(failure)
    assert host.publisher.last('organization:organization') == success_stamp
    assert host.publisher.pending_sync() == pending
    assert r.contacts()['org'] == before['org'] and r.contacts()['org_updated_at'] == before['org_updated_at']
    assert r.contacts()['org_error'] == failure['error_code']
    count = len(p.publications()); r.now[0] += 30
    host.metadata_once()
    assert len(p.publications()) == count and host.publisher.pending_sync() == pending
    r.now[0] += 31; host.metadata_once()
    assert len(p.publications()) == count + 1
    p.state['book'] = {'org_state': 'ready', 'org': good_org('recovered')}; r.now[0] += 1
    host.metadata_once()
    assert host.publisher.pending_sync() == [] and host.publisher.last('organization-error:organization') == 0
    assert r.contacts()['org_state'] == 'ready' and r.contacts()['org_error'] is None
    assert r.contacts()['org']['departments'][0]['name'] == 'recovered'


def test_provider_typed_exception_survives_without_raw_exception_text(producer):
    p = producer; r = p.receiver
    class TypedFailure(RuntimeError):
        code = 'ORG_MAIN_HEADER_INVALID'
        state = 'error'
    p.state['raise'] = TypedFailure('synthetic-private-source-key-do-not-expose')
    p.host.metadata_once()
    assert p.publications()[-1]['error_code'] == TypedFailure.code
    assert p.host.errors['contacts:' + r.uid] == TypedFailure.code
    assert r.contacts()['org_error'] == TypedFailure.code
    assert 'synthetic-private-source-key' not in json.dumps(p.publications()) + json.dumps(p.host.errors)


def test_new_sync_arriving_during_failure_or_recovery_is_not_acknowledged_as_old_request(producer):
    p = producer; r = p.receiver
    p.state['book'] = {'org_state': 'unreported', 'org_error': 'ORG_CREDENTIALS_NOT_CONFIGURED'}
    p.host.publisher.request_sync(r.uid, True)
    old = p.host.publisher.pending_sync()[0]
    p.state['during_upload'] = lambda: p.host.publisher.request_sync(r.uid, True)
    p.host.metadata_once()
    current = p.host.publisher.pending_sync()[0]
    assert current['generation'] == old['generation'] + 1
    p.state['book'] = {'org_state': 'ready', 'org': good_org('recovered')}
    p.state['during_upload'] = lambda: p.host.publisher.request_sync(r.uid, True)
    r.now[0] += 1; p.host.metadata_once()
    newer = p.host.publisher.pending_sync()[0]
    assert newer['generation'] == current['generation'] + 1


def test_failed_error_upload_never_advances_success_or_failure_publication_stamp(producer):
    p = producer; r = p.receiver
    p.host.metadata_once()
    stamp = p.host.publisher.last('organization:organization')
    p.state.update(book={'org_state': 'error', 'org_error': 'ORG_SOURCE_UNAVAILABLE'}, fail_upload=True)
    p.host.publisher.request_sync(r.uid, True); pending = p.host.publisher.pending_sync()
    r.now[0] += 1; p.host.metadata_once()
    assert p.host.publisher.last('organization:organization') == stamp
    assert p.host.publisher.last('organization-error:organization') == 0
    assert p.host.publisher.pending_sync() == pending and len(p.publications()) == 1
