"""Independent A/B settings and downloaded DSL checks, isolated from real Dify."""
import ast
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from server.app import create_app
from server.auth import AuthConfig, AuthUser
from server.dify_settings import DifySettings
from server.negotiation.settings import Aggregation
from server.quote_control import QuoteControl


ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_KEY = 'synthetic-analysis-key-never-public'
REPLY_KEY = 'synthetic-reply-key-never-public'
NEW_KEY = 'synthetic-new-analysis-key-never-public'
DSL = {
    'analysis': ('/api/dify/analysis/dsl', 'dify/site_workflows/analysis.yml', 'advanced-chat'),
    'reply': ('/api/dify/reply/dsl', 'dify/site_workflows/reply.yml', 'advanced-chat'),
    'qa': ('/api/qa/workflow/dsl', 'dify/qa_workflow/workflow.yml', 'workflow'),
}


@pytest.fixture
def application(tmp_path, monkeypatch):
    # Prevent environment-derived production connections, data paths or workers.
    for key in tuple(os.environ):
        if key.startswith(('XUNJIA_', 'QB_')):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('XUNJIA_PREFIX', str(tmp_path))
    monkeypatch.setenv('XUNJIA_DIFY_URL', 'https://synthetic-dify.invalid/v1')
    monkeypatch.setenv('XUNJIA_DIFY_ANALYSIS_APP_KEY', ANALYSIS_KEY)
    monkeypatch.setenv('XUNJIA_DIFY_REPLY_APP_KEY', REPLY_KEY)
    calls = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            calls.append({'url': request.full_url, 'authorization': request.get_header('Authorization'),
                          'body': json.loads(request.data), 'timeout': timeout})
            return Response(json.dumps({'answer': json.dumps({'schema_version': '2.2', 'synthetic': True})}).encode())

    monkeypatch.setattr('urllib.request.build_opener', lambda *handlers: Opener())
    app = create_app(database_url='sqlite:///' + str(tmp_path / 'business.sqlite'), enable_background=False,
        quote_control=QuoteControl(directory=tmp_path / 'innodealing'),
        auth_config=AuthConfig(sso_secret='synthetic-sso', session_secret='synthetic-session-secret-32-characters'),
        aggregation=Aggregation(quiet_ms=1, max_wait_ms=2, reference_ms=100, measurement_id='isolated-fixture'))
    token = app.state.auth.issue_token(AuthUser('synthetic-settings-owner', '合成设置用户'))
    with TestClient(app) as client:
        yield SimpleNamespace(app=app, client=client, headers={'Authorization': 'Bearer ' + token},
                              calls=calls, path=tmp_path / 'config/dify-workflows.json')


@pytest.mark.parametrize('stage', ['analysis', 'reply'])
@pytest.mark.parametrize('operation', ['read', 'save', 'dsl'])
def test_real_application_requires_login_before_read_save_or_download(application, stage, operation):
    client = application.client
    if operation == 'save':
        response = client.put('/api/dify/' + stage + '/config', json={
            'base_url': 'https://changed.invalid/v1', 'app_key': NEW_KEY, 'expected_revision': ''})
    else:
        response = client.get('/api/dify/' + stage + ('/dsl' if operation == 'dsl' else '/config'))
    assert response.status_code == 401
    assert not application.path.exists() and application.calls == []


def test_qa_dsl_requires_login_too(application):
    assert application.client.get('/api/qa/workflow/dsl').status_code == 401


def test_inherited_keys_are_preserved_without_echoing_or_rewriting_other_stage(application):
    client, headers = application.client, application.headers
    initial = {stage: client.get('/api/dify/' + stage + '/config', headers=headers).json()['data']
               for stage in ('analysis', 'reply')}
    assert all(item['app_key_set'] and item['configured'] for item in initial.values())
    assert all('app_key' not in item for item in initial.values())
    response = client.put('/api/dify/analysis/config', headers=headers,
        json={'base_url': 'https://new-dify.invalid', 'app_key': '', 'expected_revision': initial['analysis']['revision']})
    assert response.status_code == 200
    assert response.json()['data']['base_url'] == 'https://new-dify.invalid/v1'
    saved = json.loads(application.path.read_text())
    assert saved['analysis']['app_key'] == ANALYSIS_KEY and saved['reply']['app_key'] == REPLY_KEY
    assert client.get('/api/dify/reply/config', headers=headers).json()['data'] == initial['reply']
    assert application.path.stat().st_mode & 0o777 == 0o600
    for value in (ANALYSIS_KEY, REPLY_KEY):
        assert value not in response.text
        assert value not in client.get('/api/dify/analysis/config', headers=headers).text
    assert application.calls == []


def test_hot_configuration_changes_next_real_client_request_only(application):
    client = application.app.state.negotiation_engine.client
    old = client.client('analysis')
    assert old.call({'thread_id': 'synthetic-thread'})['synthetic']
    response = application.client.put('/api/dify/analysis/config', headers=application.headers,
        json={'base_url': 'https://new-dify.invalid/v1', 'app_key': NEW_KEY, 'expected_revision': ''})
    assert response.status_code == 200
    client.analyze({'thread_id': 'synthetic-thread'})
    old.call({'thread_id': 'already-started-context'})
    client.decide({'member_id': 'synthetic-member'})
    assert [item['authorization'] for item in application.calls] == [
        'Bearer ' + ANALYSIS_KEY, 'Bearer ' + NEW_KEY, 'Bearer ' + ANALYSIS_KEY, 'Bearer ' + REPLY_KEY]
    assert application.calls[1]['url'] == 'https://new-dify.invalid/v1/chat-messages'
    assert application.calls[-1]['url'] == 'https://synthetic-dify.invalid/v1/chat-messages'
    contexts = [json.loads(item['body']['inputs']['context_json']) for item in application.calls]
    assert [item['stage'] for item in contexts] == ['analysis', 'analysis', 'analysis', 'reply']
    assert all(item['schema_version'] == '2.2' for item in contexts)
    assert NEW_KEY not in response.text


def test_save_refreshes_runtime_readiness_without_service_restart(application):
    flows = application.app.state.chat_client
    flows.settings.env['XUNJIA_DIFY_REPLY_APP_KEY'] = ''
    application.client.get('/api/runtime/status', headers=application.headers)
    assert not application.app.state.runtime_status['analysis_enabled']
    response = application.client.put('/api/dify/reply/config', headers=application.headers,
        json={'base_url': 'https://reply.invalid/v1', 'app_key': REPLY_KEY, 'expected_revision': ''})
    assert response.status_code == 200
    assert application.app.state.runtime_status['analysis_enabled']
    assert application.app.state.negotiation_engine.client is flows
    assert application.calls == []


@pytest.mark.parametrize('bad', [
    {'base_url': 'https://user:password@unsafe.invalid/v1'}, {'base_url': 'https://unsafe.invalid/v1?token=secret'},
    {'app_key': 'invalid\nkey'}, {'app_key': 12}, {'expected_revision': 'stale-revision'}, {'owner': 'other-owner'},
])
def test_rejected_configuration_preserves_saved_bytes(application, bad):
    body = {'base_url': 'https://saved.invalid/v1', 'app_key': NEW_KEY, 'expected_revision': ''}
    saved = application.client.put('/api/dify/analysis/config', headers=application.headers, json=body)
    assert saved.status_code == 200
    before = application.path.read_bytes()
    body.update(expected_revision=saved.json()['data']['revision'], app_key='')
    body.update(bad)
    response = application.client.put('/api/dify/analysis/config', headers=application.headers, json=body)
    assert response.status_code == 400
    assert application.path.read_bytes() == before and application.calls == []
    assert NEW_KEY not in response.text


@pytest.mark.parametrize('stage', list(DSL))
def test_downloaded_dsl_is_060_with_valid_graph_and_no_deployment_credentials(application, stage):
    endpoint, relative, mode = DSL[stage]
    response = application.client.get(endpoint, headers=application.headers)
    assert response.status_code == 200
    assert response.content == (ROOT / relative).read_bytes()
    assert 'attachment' in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'no-store'
    document = yaml.safe_load(response.content)
    assert document['version'] == '0.6.0' and document['kind'] == 'app' and document['app']['mode'] == mode
    workflow = document['workflow']
    assert workflow['environment_variables'] == [] and workflow['conversation_variables'] == []
    nodes = {item['id']: item['data'] for item in workflow['graph']['nodes']}
    assert len(nodes) == len(workflow['graph']['nodes'])
    assert nodes['start']['type'] == 'start'
    reached = {'start'}
    for edge in workflow['graph']['edges']:
        assert edge['source'] in nodes and edge['target'] in nodes
    for _ in nodes:
        reached.update(edge['target'] for edge in workflow['graph']['edges'] if edge['source'] in reached)
    assert reached == set(nodes)
    for node in nodes.values():
        if node['type'] == 'code':
            assert node['code_language'] == 'python3'
            ast.parse(node['code'])
        assert node['type'] not in ('agent', 'tool')
    variables = [item['variable'] for item in nodes['start']['variables']]
    if stage == 'qa':
        assert variables == ['question', 'tool_url', 'capability']
        assert nodes['market']['url'] == '{{#start.tool_url#}}'
        assert 'Bearer {{#start.capability#}}' in nodes['market']['headers']
        assert nodes['end']['type'] == 'end'
    else:
        assert variables == ['context_json']
        assert nodes['answer']['type'] == 'answer' and nodes['answer']['answer'] == '{{#contract.result#}}'
    for secret in (ANALYSIS_KEY, REPLY_KEY, NEW_KEY):
        assert secret not in response.text
    assert application.calls == []


def test_public_configuration_file_is_rejected_without_key_disclosure(tmp_path):
    path = tmp_path / 'dify-workflows.json'
    path.write_text(json.dumps({'analysis': {'app_key': ANALYSIS_KEY}}))
    path.chmod(0o644)
    with pytest.raises(ValueError) as error:
        DifySettings(path, {}).status('analysis')
    assert ANALYSIS_KEY not in str(error.value)


def test_non_regular_configuration_is_rejected_without_waiting_for_fifo_writer(tmp_path):
    path = tmp_path / 'dify-workflows.json'
    os.mkfifo(path, 0o600)
    script = '''import sys
from server.dify_settings import DifySettings
try:DifySettings(sys.argv[1],{}).status('analysis')
except ValueError:print('REJECTED_NON_REGULAR')
else:raise AssertionError('Non-regular configuration was accepted')
'''
    result = subprocess.run([sys.executable, '-c', script, str(path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'REJECTED_NON_REGULAR'
