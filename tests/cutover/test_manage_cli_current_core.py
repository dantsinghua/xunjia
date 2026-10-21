"""The shipped CLI provisions the same schema and identity read by the app."""
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.auth import AuthConfig
from server.health import contract
from server.negotiation.schema import TABLES
from test_production_flow import FakeFlows, FakeNative, User, OWNER


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('XUNJIA_', 'QB_')):
            monkeypatch.delenv(name)


def cli(database, *args):
    return subprocess.run([sys.executable, '-B', '-m', 'server.manage', *args], cwd=ROOT,
        env={**os.environ, 'XUNJIA_DATABASE_URL': 'sqlite:///'+str(database),
             'XUNJIA_RUN_BACKGROUND': '0'},
        capture_output=True, text=True, timeout=20)


def dump(database):
    with sqlite3.connect(database) as connection:
        return '\n'.join(connection.iterdump())


def migrated(tmp_path):
    database = tmp_path/'synthetic-cli.sqlite'
    result = cli(database, 'migrate')
    assert result.returncode == 0, result.stderr
    return database


def test_cli_cold_migration_and_repeated_migration_create_only_current_tables(tmp_path):
    database = migrated(tmp_path)
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(TABLES) and all(name.startswith('xj2_') for name in tables)
    before = dump(database)
    assert cli(database, 'check').returncode == 0
    assert cli(database, 'migrate').returncode == 0
    assert dump(database) == before


@pytest.mark.parametrize('enabled', [False, True])
def test_cli_binding_is_read_by_real_me_api_and_repeat_preserves_control_and_preferences(tmp_path, enabled):
    database = migrated(tmp_path)
    arguments = ('bind', '--owner', OWNER, '--account', 'synthetic-cli-account',
                 '--workbench', 'synthetic-cli-workbench', '--transport', 'mail')
    assert cli(database, *arguments).returncode == 0
    native = FakeNative()
    app = create_app(database_url='sqlite:///'+str(database),
        auth_config=AuthConfig(sso_secret='synthetic-only', session_secret='synthetic-session-at-least-32-chars'),
        enable_background=False,  chatflows=FakeFlows(), transport=native)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer '+app.state.auth.issue_token(User())
        response = client.get('/api/auth/me')
        assert response.status_code == 200, response.text
        before = response.json()['data']['binding']
        assert before['account_uid'] == 'synthetic-cli-account'
        assert before['workbench_id'] == 'synthetic-cli-workbench' and before['transport'] == 'mail'
        assert before['bot_enabled'] == 0
        if enabled:
            assert client.post('/api/bot/toggle', json={'status': '正常'}).status_code == 200
        app.state.repository.save_preferences(OWNER, {'synthetic-local-preference': 'preserve'})
        before = client.get('/api/auth/me').json()['data']['binding']
        assert cli(database, *arguments).returncode == 0
        assert client.get('/api/auth/me').json()['data']['binding'] == before
        assert app.state.repository.preferences(OWNER) == {'synthetic-local-preference': 'preserve'}
        assert not native.sent


def test_cli_rebinding_different_identity_fails_without_mutating_existing_binding(tmp_path):
    database = migrated(tmp_path)
    assert cli(database, 'bind', '--owner', OWNER, '--account', 'synthetic-original',
               '--workbench', 'synthetic-desk').returncode == 0
    before = dump(database)
    changed = cli(database, 'bind', '--owner', OWNER, '--account', 'synthetic-replacement',
                  '--workbench', 'synthetic-desk')
    assert changed.returncode != 0
    assert dump(database) == before


def test_cli_check_of_legacy_only_database_is_nonzero_and_keeps_all_data(tmp_path):
    database = tmp_path/'legacy-only.sqlite'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE xj_binding(owner TEXT PRIMARY KEY, account TEXT)')
        connection.execute('INSERT INTO xj_binding VALUES (?,?)', ('synthetic-old-owner', 'synthetic-old-account'))
    before = dump(database)
    result = cli(database, 'check')
    assert result.returncode != 0 and 'SCHEMA_INCOMPLETE' in result.stdout+result.stderr
    assert dump(database) == before


def test_cli_check_detects_missing_current_index_without_repairing_or_changing_data(tmp_path):
    database = migrated(tmp_path)
    assert cli(database, 'check').returncode == 0
    table, name = next((table, name) for table, spec in contract(False).items() for name in spec['secondary'])
    assert re.fullmatch(r'\w+', name)
    with sqlite3.connect(database) as connection:
        connection.execute('DROP INDEX '+name)
    before = dump(database)
    result = cli(database, 'check')
    assert result.returncode != 0 and 'SCHEMA_INCOMPLETE' in result.stdout+result.stderr
    assert dump(database) == before
    with sqlite3.connect(database) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
