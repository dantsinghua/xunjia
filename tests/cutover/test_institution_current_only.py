"""Current-only bootstrap approved on 2026-10-22; no gateway or native side effects."""
import ast
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from server.auth import AuthConfig,AuthUser
from server.quote_control import QuoteControl
from relay.gateway_paths import CONTRACT

@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
 import os
 for key in list(os.environ):
  if key.startswith(('XUNJIA_','QB_')):monkeypatch.delenv(key,raising=False)

def app_for(tmp_path):
 return create_app(database_url='sqlite:///'+str(tmp_path/'business.db'),enable_background=False,
  auth_config=AuthConfig(sso_secret='synthetic-s'*8,session_secret='synthetic-t'*8),
  quote_control=QuoteControl(directory=tmp_path/'quote'))

@pytest.mark.parametrize('key',['XUNJIA_TRANSPORT_CONFIG','XUNJIA_MQ_CONNECTION_CONFIG'])
def test_retired_bootstrap_is_rejected_before_database_or_network(monkeypatch,key):
 monkeypatch.setenv(key,'must-never-be-read.json')
 with pytest.raises(ValueError,match=key):create_app(database_url='invalid://never-connect')

def test_institution_config_requires_explicit_state_directory(monkeypatch):
 monkeypatch.setenv('XUNJIA_INSTITUTION_CONFIG','current.json')
 with pytest.raises(ValueError,match='XUNJIA_STATE_DIR'):create_app(database_url='invalid://never-connect')

def test_unconfigured_service_has_no_old_runtime(tmp_path):
 app=app_for(tmp_path)
 assert app.state.institution_runtime is None
 assert not hasattr(app.state,'managed_mq')
 assert app.state.collector_selection is None
 status=app.state.mq_settings.status('synthetic-owner')
 assert status['contract']==CONTRACT and not status['configured'] and not status['applied']
 with pytest.raises(ValueError,match='XUNJIA_STATE_DIR'):
  app.state.mq_settings.save('synthetic-owner',{})

def test_state_directory_uses_current_contract_only_and_does_not_start_consumers(tmp_path,monkeypatch):
 monkeypatch.setenv('XUNJIA_STATE_DIR',str(tmp_path/'state'))
 app=app_for(tmp_path)
 assert app.state.institution_runtime.path==tmp_path/'state'/'institution-connection.json'
 assert app.state.collector_selection.__class__.__name__=='InstitutionSelection'
 token=app.state.auth.issue_token(AuthUser('synthetic-owner','synthetic user'))
 with TestClient(app) as client:
  client.headers['Authorization']='Bearer '+token
  r=client.get('/api/im/mq-config');assert r.status_code==200
  data=r.json()['data'];assert 'editable' not in data and not data['configured'] and data['contract']==CONTRACT
  assert app.state.institution_runtime._thread is None
  r=client.get('/api/im/catalog');assert r.status_code==200
  assert r.json()['data']['workbenches']==[]
 assert not (tmp_path/'state'/'institution-connection.json').exists()

def test_current_runtime_import_graph_has_no_retired_factories():
 root=Path(__file__).resolve().parents[2]
 retired={'managed_mq','mq_settings','collector_selection','transport_config'}
 for name in ['app','institution_runtime','institution_selection','institution_settings']:
  tree=ast.parse((root/'server'/(name+'.py')).read_text(encoding='utf-8-sig'))
  deps={n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}
  assert not deps.intersection(retired),(name,deps.intersection(retired))