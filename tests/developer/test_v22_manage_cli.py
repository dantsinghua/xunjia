"""Run the deployed administration entrypoint against an isolated database."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from server.negotiation.application_store import ApplicationStore
from server.schema import TABLES


ROOT = Path(__file__).resolve().parents[2]


def cli(database, *args):
    return subprocess.run(
        [sys.executable, '-B', '-m', 'server.manage', *args], cwd=ROOT,
        env={**os.environ, 'XUNJIA_DATABASE_URL': 'sqlite:///' + str(database)},
        capture_output=True, text=True, timeout=20,
    )


def tables(database):
    with sqlite3.connect(database) as connection:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


def test_explicit_migration_creates_only_current_business_tables(tmp_path):
    database = tmp_path / 'new.sqlite'
    result = cli(database, 'migrate')
    assert result.returncode == 0, result.stderr
    assert tables(database) == set(TABLES)
    assert cli(database, 'migrate').returncode == 0
    assert tables(database) == set(TABLES)


def test_cli_binding_is_visible_to_current_store_and_starts_paused(tmp_path):
    database = tmp_path / 'new.sqlite'
    store = ApplicationStore('sqlite:///' + str(database), initialize=True)
    arguments = ('bind', '--owner', 'trader', '--account', 'account',
                 '--workbench', 'workbench', '--transport', 'mail')
    result = cli(database, *arguments)
    assert result.returncode == 0, result.stderr
    binding = store.binding_for('trader')
    assert binding['account_uid'] == 'account'
    assert binding['workbench_id'] == 'workbench'
    assert binding['transport'] == 'mail'
    assert binding['bot_enabled'] == 0
    assert cli(database, *arguments).returncode == 0
    assert store.binding_for('trader') == binding


def test_check_rejects_empty_database_without_provisioning(tmp_path):
    database = tmp_path / 'empty.sqlite'
    before = tables(database)
    result = cli(database, 'check')
    assert result.returncode != 0
    assert 'SCHEMA_INCOMPLETE' in result.stdout + result.stderr
    assert tables(database) == before


def test_check_rejects_legacy_only_database_without_changing_it(tmp_path):
    from server.repository import Repository
    database = tmp_path / 'legacy.sqlite'
    legacy = Repository('sqlite:///' + str(database), initialize=True)
    legacy.bind_account('legacy-trader', 'legacy-account', 'legacy-desk')
    before = tables(database)
    result = cli(database, 'check')
    assert result.returncode != 0
    assert 'SCHEMA_INCOMPLETE' in result.stdout + result.stderr
    assert tables(database) == before
    assert legacy.binding_for('legacy-trader')['account_uid'] == 'legacy-account'


def test_migration_keeps_existing_legacy_data(tmp_path):
    from server.repository import Repository
    database = tmp_path / 'legacy.sqlite'
    legacy = Repository('sqlite:///' + str(database), initialize=True)
    legacy.bind_account('legacy-trader', 'legacy-account', 'legacy-desk')
    before = tables(database)
    result = cli(database, 'migrate')
    assert result.returncode == 0, result.stderr
    assert tables(database) == before | set(TABLES)
    assert legacy.binding_for('legacy-trader')['account_uid'] == 'legacy-account'


def test_check_requires_complete_current_schema(tmp_path):
    database = tmp_path / 'new.sqlite'
    ApplicationStore('sqlite:///' + str(database), initialize=True)
    assert cli(database, 'check').returncode == 0
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE xj2_quote')
    result = cli(database, 'check')
    assert result.returncode != 0
    assert 'SCHEMA_INCOMPLETE' in result.stdout + result.stderr
    assert 'xj2_quote' not in tables(database)
