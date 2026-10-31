"""Bounded, additive reply-context migration; no database/table recreation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic_json, read_env
from database_upgrade import connection_options, verify_server, query, execute

DATABASE = 'xunjia_business'
TABLE = 'xj2_message'
DDL = 'ALTER TABLE `xunjia_business`.`xj2_message` ADD COLUMN `reply_json` LONGTEXT NULL'
BASE_COLUMNS = {
    'message_id', 'thread_id', 'event_id', 'content_hash', 'source_seq',
    'ingest_revision', 'direction', 'kind', 'text', 'native_id', 'target_id',
    'occurred_at', 'received_at', 'trade_day', 'historical',
}
BASE_TYPES = {
    'message_id': ('varchar', 'NO', 64), 'thread_id': ('varchar', 'NO', 64),
    'event_id': ('varchar', 'NO', 160), 'content_hash': ('varchar', 'NO', 64),
    'source_seq': ('bigint', 'NO', None), 'ingest_revision': ('bigint', 'NO', None),
    'direction': ('varchar', 'NO', 8), 'kind': ('varchar', 'NO', 16),
    'text': ('longtext', 'NO', None), 'native_id': ('varchar', 'YES', 160),
    'target_id': ('varchar', 'YES', 64), 'occurred_at': ('double', 'NO', None),
    'received_at': ('double', 'NO', None), 'trade_day': ('varchar', 'NO', 10),
    'historical': ('int', 'NO', None),
}


class MigrationError(ValueError):
    pass


def inspect(connection):
    row = query(connection, 'SELECT DATABASE() AS database_name', one=True)
    if row != {'database_name': DATABASE}:
        raise MigrationError('MIGRATION_DATABASE_IDENTITY_MISMATCH')
    tables = query(connection, 'SELECT TABLE_TYPE FROM information_schema.TABLES '
                   'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s', (DATABASE, TABLE))
    if tables != [{'TABLE_TYPE': 'BASE TABLE'}]:
        raise MigrationError('MIGRATION_EXISTING_MESSAGE_TABLE_REQUIRED')
    columns = query(connection, 'SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,CHARACTER_MAXIMUM_LENGTH '
                    'FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',
                    (DATABASE, TABLE))
    names = {item['COLUMN_NAME'] for item in columns}
    if names not in (BASE_COLUMNS, BASE_COLUMNS | {'reply_json'}):
        raise MigrationError('MIGRATION_MESSAGE_SCHEMA_UNRECOGNIZED')
    for item in columns:
        name = item['COLUMN_NAME']
        if name not in BASE_TYPES:
            continue
        kind, nullable, length = BASE_TYPES[name]
        if (item['DATA_TYPE'].lower() != kind or item['IS_NULLABLE'] != nullable
                or item['COLUMN_DEFAULT'] is not None or item['EXTRA']
                or length is not None and item['CHARACTER_MAXIMUM_LENGTH'] != length):
            raise MigrationError('MIGRATION_MESSAGE_COLUMN_INCOMPATIBLE')
    reply = next((item for item in columns if item['COLUMN_NAME'] == 'reply_json'), None)
    if reply and (reply['DATA_TYPE'].lower() != 'longtext' or reply['IS_NULLABLE'] != 'YES'
                  or reply['COLUMN_DEFAULT'] is not None or reply['EXTRA']):
        raise MigrationError('MIGRATION_REPLY_COLUMN_INCOMPATIBLE')
    return bool(reply)


def run(prefix, *, apply=False, connect=None):
    """Caller holds the deployment lock and stops the service during apply.

    The backup is the original table definition; existing row values are never
    rewritten. A successful DDL is retained on application rollback, because the
    old reader accepts the additive nullable column.
    """
    prefix = Path(prefix)
    values = read_env(prefix / 'config/xunjia.env')
    options = connection_options(values.get('XUNJIA_DATABASE_URL'))
    identity = hashlib.sha256(json.dumps([options['host'], options['port'], DATABASE]).encode()).hexdigest()
    folder = prefix / 'deployments' / 'reply-migration'
    if folder.resolve() != folder or folder.is_symlink():
        raise MigrationError('MIGRATION_JOURNAL_PATH_INVALID')
    state = folder / 'journal.json'
    if state.is_symlink():
        raise MigrationError('MIGRATION_JOURNAL_PATH_INVALID')
    prior = json.loads(state.read_text()) if state.exists() else None
    if prior and (prior.get('database_identity') != identity or prior.get('ddl') != DDL):
        raise MigrationError('MIGRATION_JOURNAL_IDENTITY_MISMATCH')
    connection = None
    try:
        if connect is None:
            import pymysql
            connect = lambda **kw: pymysql.connect(**kw, cursorclass=pymysql.cursors.DictCursor)
        connection = connect(**options)
        verify_server(connection)
        connection.select_db(DATABASE)
        exists = inspect(connection)
        report = {'database': DATABASE, 'migration': 'reply_json', 'ddl_executed': False,
                  'column_verified': exists, 'schema_backup_only': True}
        if not apply:
            return {**report, 'status': 'ALREADY_APPLIED' if exists else 'MIGRATION_REQUIRED'}
        record = {'database_identity': identity, 'ddl': DDL}
        if exists:
            if prior:
                atomic_json(state, {**record, 'status': 'VERIFIED'})
            return {**report, 'status': 'ALREADY_APPLIED'}
        if prior:
            # An interrupted ALTER may still be running server-side. Never replay
            # while the observed column is absent; operator reconciliation needed.
            raise MigrationError('MIGRATION_PREVIOUS_ATTEMPT_REQUIRES_DBA_RECONCILIATION')
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = query(connection, 'SHOW CREATE TABLE `xunjia_business`.`xj2_message`', one=True)
        atomic_json(folder / 'schema-before.json', backup)
        atomic_json(state, {**record, 'status': 'STARTED'})
        try:
            execute(connection, DDL)
            if not inspect(connection):
                raise MigrationError('MIGRATION_COLUMN_VERIFICATION_FAILED')
        except BaseException:
            atomic_json(state, {**record, 'status': 'UNKNOWN'})
            raise MigrationError('MIGRATION_OUTCOME_UNKNOWN_RETRY_CHECK_FIRST') from None
        atomic_json(state, {**record, 'status': 'VERIFIED'})
        return {**report, 'status': 'MIGRATION_VERIFIED', 'ddl_executed': True, 'column_verified': True}
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('MIGRATION_DATABASE_CHECK_FAILED_VERIFY_CONNECTIVITY_AND_ALTER_PRIVILEGE') from None
    finally:
        if connection is not None:
            connection.close()
