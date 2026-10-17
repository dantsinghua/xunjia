"""Liveness is independent; readiness checks every table, column, index and build identity."""
from __future__ import annotations
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[2]
APP=ROOT/'app' if (ROOT/'app/server').is_dir() else ROOT
sys.path.insert(0,str(APP))
from server.app import create_app
from server.auth import AuthConfig
from server.schema import TABLES
def application(tmp_path):
    return create_app(database_url='sqlite:///'+str(tmp_path/'schema.db'),enable_background=False,
                      auth_config=AuthConfig(sso_secret='synthetic-portal',session_secret='synthetic-session-secret-at-least-32-bytes'))
def test_all_schema_and_readiness_details(tmp_path):
    app=application(tmp_path)
    with TestClient(app) as client:
        response=client.get('/api/health/ready')
        assert response.status_code==200,response.text
        value=response.json()['data']
        assert value['ready'] and value['database_ready']
        assert value['schema']['tables_expected']==15 and value['schema']['indexes_expected']==13
        assert value['build_id'] and value['release_id'] and value['boot_id']
        assert value['automation_ready'] is False
        assert client.get('/api/health/automation').status_code==503
@pytest.mark.parametrize('table',TABLES)
def test_missing_any_business_table_is_not_ready(tmp_path,table):
    app=application(tmp_path)
    with app.state.repository.db.transaction() as tx:tx.execute('DROP TABLE '+table)
    with TestClient(app) as client:
        live=client.get('/api/health/live')
        ready=client.get('/api/health/ready')
        assert live.status_code==200
        assert ready.status_code==503
        assert table in ready.json()['data']['schema']['missing_tables']
        assert client.get('/api/health').status_code==200  # old summary contract, not a readiness gate
def test_missing_secondary_index_fails_readiness(tmp_path):
    app=application(tmp_path)
    with app.state.repository.db.transaction() as tx:tx.execute('DROP INDEX idx_xj2_cmd_pending')
    with TestClient(app) as client:
        response=client.get('/api/health/ready')
        assert response.status_code==503
        assert 'xj2_command.idx_xj2_cmd_pending' in response.json()['data']['schema']['missing_indexes']
def test_missing_column_is_not_hidden_by_one_working_table(tmp_path):
    app=application(tmp_path)
    with app.state.repository.db.transaction() as tx:
        tx.execute('ALTER TABLE xj2_owner RENAME COLUMN projection_version TO obsolete_revision')
    with TestClient(app) as client:
        response=client.get('/api/health/ready')
        assert response.status_code==503
        assert 'xj2_owner.projection_version' in response.json()['data']['schema']['missing_columns']
def test_wrong_type_is_not_reported_as_ready(tmp_path):
    app=application(tmp_path)
    with app.state.repository.db.transaction() as tx:
        tx.execute('DROP TABLE xj2_owner')
        tx.execute('CREATE TABLE xj2_owner (owner_id VARCHAR(160) PRIMARY KEY, control_version BIGINT NOT NULL, send_allowed INTEGER NOT NULL, projection_version TEXT NOT NULL)')
    with TestClient(app) as client:
        response=client.get('/api/health/ready')
        assert response.status_code==503
        assert 'xj2_owner.projection_version' in response.json()['data']['schema']['invalid_columns']
def test_liveness_never_connects_to_broken_database_or_echoes_secrets(tmp_path,monkeypatch):
    app=application(tmp_path)
    def broken(*a,**kw):raise RuntimeError('NEVER-LOG-THIS-FAKE-PASSWORD')
    monkeypatch.setattr(app.state.repository.db,'transaction',broken)
    with TestClient(app) as client:
        assert client.get('/api/health/live').status_code==200
        response=client.get('/api/health/ready')
        assert response.status_code==503
        assert 'NEVER-LOG' not in response.text
        assert response.json()['data']['schema']['code']=='DATABASE_UNAVAILABLE'
def test_unconfigured_login_is_not_business_ready(tmp_path):
    app=create_app(database_url='sqlite:///'+str(tmp_path/'no-auth.db'),auth_config=AuthConfig(),enable_background=False)
    with TestClient(app) as client:
        response=client.get('/api/health/ready')
        assert response.status_code==503 and response.json()['data']['database_ready']
        assert 'AUTH_NOT_CONFIGURED' in response.json()['data']['reasons']