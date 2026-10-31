"""Add QA tables and retain/copy old SQLite history before application cutover.

The caller owns the deployment lock and has stopped the old application for
apply. No grants, database creation, deletion or cross-schema writes occur.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic_json, read_env
from database_upgrade import connection_options, verify_server, query, execute, safe_failure

DATABASE = 'xunjia_business'
SOURCE_TABLES = {'qa_session': 'xj2_qa_session', 'qa_turn': 'xj2_qa_turn',
                 'qa_history_meta': 'xj2_qa_meta', 'qa_history_import': 'xj2_qa_import'}
SCHEMA = {
    'xj2_qa_session': {'id': ('varchar',64), 'owner': ('varchar',160), 'title': ('varchar',200),
                     'created_at': ('double',None), 'updated_at': ('double',None)},
    'xj2_qa_turn': {'id': ('varchar',64), 'owner': ('varchar',160), 'session_id': ('varchar',64),
                  'request_id': ('varchar',128), 'question': ('text',None), 'state': ('varchar',16),
                  'result': ('longtext',None), 'created_at': ('double',None), 'updated_at': ('double',None)},
    'xj2_qa_meta': {'key': ('varchar',192), 'value': ('double',None)},
    'xj2_qa_import': {'source': ('varchar',160), 'session_id': ('varchar',64), 'imported_at': ('double',None)},
}
INDEXES = {
    'xj2_qa_session': {'PRIMARY': (0,['id']), 'idx_xj2_qa_session_owner': (1,['owner','updated_at','id'])},
    'xj2_qa_turn': {'PRIMARY': (0,['id']), 'uq_xj2_qa_request': (0,['owner','request_id']),
                   'idx_xj2_qa_turn_session': (1,['session_id','created_at','id'])},
    'xj2_qa_meta': {'PRIMARY': (0,['key'])}, 'xj2_qa_import': {'PRIMARY': (0,['source'])},
}


class MigrationError(ValueError):
    """Fixed diagnostics only; never return connection errors or source rows."""


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False)


def table(name):
    if name not in SCHEMA:
        raise MigrationError('QA_HISTORY_TABLE_OUTSIDE_MIGRATION')
    return '`xunjia_business`.`'+name+'`'


def statements():
    source = Path(__file__).resolve().parents[1] / 'sql/oceanbase/005_qa_history.sql'
    text = re.sub(r'--[^\r\n]*', '', source.read_text(encoding='utf8'))
    result = [part.strip() for part in text.split(';') if part.strip()]
    if len(result) != len(SCHEMA):
        raise MigrationError('QA_HISTORY_SCHEMA_SOURCE_INVALID')
    for name, sql in zip(SCHEMA, result):
        if (not sql.startswith('CREATE TABLE IF NOT EXISTS '+table(name)+' (')
                or re.search(r'\b(DROP|TRUNCATE|ALTER|GRANT|INSERT|DELETE|UPDATE|REPLACE|SELECT|REFERENCES)\b',sql,re.I)):
            raise MigrationError('QA_HISTORY_SCHEMA_SOURCE_INVALID')
    return dict(zip(SCHEMA,result))


def inspect(connection):
    if query(connection,'SELECT DATABASE() AS database_name',one=True) != {'database_name': DATABASE}:
        raise MigrationError('QA_HISTORY_DATABASE_IDENTITY_MISMATCH')
    found = {}
    for name, expected in SCHEMA.items():
        rows = query(connection,'SELECT TABLE_TYPE,TABLE_COLLATION FROM information_schema.TABLES '
                     'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',(DATABASE,name))
        if not rows:
            continue
        if rows != [{'TABLE_TYPE':'BASE TABLE','TABLE_COLLATION':'utf8mb4_bin'}]:
            raise MigrationError('QA_HISTORY_TABLE_INCOMPATIBLE')
        columns = query(connection,'SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE,CHARACTER_MAXIMUM_LENGTH,EXTRA '
                        'FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',(DATABASE,name))
        if {r['COLUMN_NAME'] for r in columns} != set(expected):
            raise MigrationError('QA_HISTORY_COLUMNS_INCOMPATIBLE')
        for row in columns:
            kind,length = expected[row['COLUMN_NAME']]
            if (row['DATA_TYPE'].lower()!=kind or row['IS_NULLABLE']!='NO' or row['EXTRA']
                    or length is not None and row['CHARACTER_MAXIMUM_LENGTH']!=length):
                raise MigrationError('QA_HISTORY_COLUMN_INCOMPATIBLE')
        indexes = query(connection,'SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART '
                        'FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s '
                        'ORDER BY INDEX_NAME,SEQ_IN_INDEX',(DATABASE,name))
        actual = {}
        for row in indexes:
            if row['SUB_PART'] is not None:
                raise MigrationError('QA_HISTORY_PARTIAL_INDEX_FORBIDDEN')
            value = actual.setdefault(row['INDEX_NAME'],(int(row['NON_UNIQUE']),[]))
            value[1].append(row['COLUMN_NAME'])
        if actual != INDEXES[name]:
            raise MigrationError('QA_HISTORY_INDEX_INCOMPATIBLE')
        query(connection,'SELECT '+','.join('`'+key+'`' for key in expected)+' FROM '+table(name)+' LIMIT 0')
        found[name] = True
    return found


def write(connection, sql, args=()):
    cursor=connection.cursor()
    try:cursor.execute(sql,args)
    finally:cursor.close()


def verify_writes(connection):
    """Exercise required privileges with statements that cannot change a row."""
    for name,columns in SCHEMA.items():
        values=tuple(0 if kind=='double' else '' for kind,_ in columns.values())
        write(connection,'INSERT INTO '+table(name)+' ('+','.join('`'+key+'`' for key in columns)+
              ') SELECT '+','.join('%s' for _ in columns)+' WHERE 1=0',values)
        if name!='xj2_qa_import':
            key=next(iter(columns))
            write(connection,'UPDATE '+table(name)+' SET `'+key+'`=`'+key+'` WHERE 1=0')


@contextmanager
def sqlite_source(path):
    if not path.exists():
        yield None
        return
    if not path.is_file() or path.resolve()!=path or path.stat().st_mode & 0o077:
        raise MigrationError('QA_HISTORY_SQLITE_SOURCE_UNSAFE')
    source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)
    source.row_factory=sqlite3.Row
    try:
        source.execute('BEGIN')
        if source.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise MigrationError('QA_HISTORY_SQLITE_INTEGRITY_FAILED')
        names={r['name'] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'qa_session','qa_turn'} <= names or names-set(SOURCE_TABLES):
            raise MigrationError('QA_HISTORY_SQLITE_SCHEMA_UNRECOGNIZED')
        for name in names:
            columns=list(source.execute('PRAGMA table_info('+name+')'))
            expected=SCHEMA[SOURCE_TABLES[name]]
            if ({r['name'] for r in columns} != set(expected)
                    or [r['name'] for r in columns if r['pk']] != [next(iter(expected))]):
                raise MigrationError('QA_HISTORY_SQLITE_COLUMNS_INCOMPATIBLE')
        if source.execute('SELECT 1 FROM qa_turn GROUP BY owner,request_id HAVING COUNT(*)>1 LIMIT 1').fetchone():
            raise MigrationError('QA_HISTORY_SQLITE_DUPLICATE_REQUEST')
        if source.execute('SELECT 1 FROM qa_turn t LEFT JOIN qa_session s ON s.id=t.session_id '
                          'WHERE s.id IS NULL OR t.owner<>s.owner LIMIT 1').fetchone():
            raise MigrationError('QA_HISTORY_SQLITE_OWNER_MISMATCH')
        if 'qa_history_import' in names and source.execute('SELECT 1 FROM qa_history_import i LEFT JOIN qa_session s '
                'ON s.id=i.session_id WHERE s.id IS NULL LIMIT 1').fetchone():
            raise MigrationError('QA_HISTORY_SQLITE_IMPORT_ORPHAN')
        yield source
    finally:
        source.close()


def source_rows(source):
    if source is None:return
    names={r['name'] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for old,new in SOURCE_TABLES.items():
        if old not in names:continue
        key=next(iter(SCHEMA[new]))
        for row in source.execute('SELECT * FROM '+old+' ORDER BY `'+key+'`'):
            data=dict(row)
            for column,(kind,length) in SCHEMA[new].items():
                value=data[column]
                if kind=='double':
                    if type(value) not in (int,float) or not math.isfinite(value):
                        raise MigrationError('QA_HISTORY_SQLITE_TIMESTAMP_INVALID')
                elif not isinstance(value,str) or length is not None and len(value)>length:
                    raise MigrationError('QA_HISTORY_SQLITE_TEXT_INVALID')
                elif column in ('id','owner','session_id','request_id','source') and not value:
                    raise MigrationError('QA_HISTORY_SQLITE_IDENTITY_INVALID')
            if new=='xj2_qa_turn' and (data['state'] not in ('pending','complete','failed')
                    or not isinstance(json.loads(data['result']),dict)):
                raise MigrationError('QA_HISTORY_SQLITE_TURN_INVALID')
            if new=='xj2_qa_meta' and data['key']!='created_at':
                raise MigrationError('QA_HISTORY_SQLITE_META_UNRECOGNIZED')
            yield new,data


def source_summary(source):
    digest=hashlib.sha256();counts={name:0 for name in SCHEMA}
    for name,row in source_rows(source):
        counts[name]+=1
        digest.update(encoded([name,row]).encode('utf8')+b'\n')
    return {'sha256':digest.hexdigest(),'counts':counts}


def transfer(connection, source, present, source_identity, *, apply=False):
    """Exact comparisons precede inserts; markers allow legitimate later edits.

    An already imported source row may have a newer target result/title, but its
    owner and immutable identity must still match. Unmarked conflicts fail closed.
    """
    counts={name:0 for name in SCHEMA};missing=0;unmarked=0
    for name,row in source_rows(source):
        key=next(iter(SCHEMA[name]));columns=list(row)
        digest=hashlib.sha256(encoded([name,row]).encode()).hexdigest()
        marker='sqlite:'+source_identity+':'+name+':'+digest
        imported=None
        if 'xj2_qa_import' in present:
            imported=query(connection,'SELECT source FROM '+table('xj2_qa_import')+' WHERE source=%s',(marker,),one=True)
        if not imported:unmarked+=1
        existing=query(connection,'SELECT * FROM '+table(name)+' WHERE `'+key+'`=%s'+(' FOR UPDATE' if apply else ''),
                       (row[key],),one=True) if name in present else None
        if imported:
            if name=='xj2_qa_turn' and existing is None:
                # Explicit retry of an expired read replaces only its attempt
                # identity. The durable owner/request identity stays unique.
                existing=query(connection,'SELECT * FROM '+table(name)+' WHERE owner=%s AND request_id=%s'+
                               (' FOR UPDATE' if apply else ''),(row['owner'],row['request_id']),one=True)
            immutable=set(row)-({'title','updated_at'} if name=='xj2_qa_session' else
                               {'id','state','result','updated_at'} if name=='xj2_qa_turn' else {'value'} if name=='xj2_qa_meta' else set())
            if existing is None or any(existing[k]!=row[k] for k in immutable):
                raise MigrationError('QA_HISTORY_PREVIOUS_IMPORT_IDENTITY_CHANGED')
        elif existing is not None and name=='xj2_qa_meta':
            if apply and row['value']<existing['value']:
                write(connection,'UPDATE '+table(name)+' SET value=%s WHERE `key`=%s',(row['value'],row['key']))
        elif existing is not None and any(existing[k]!=row[k] for k in row):
            raise MigrationError('QA_HISTORY_SQLITE_TARGET_CONFLICT')
        elif existing is None:
            missing+=1
            if apply:
                write(connection,'INSERT INTO '+table(name)+' ('+','.join('`'+k+'`' for k in columns)+') VALUES ('+
                      ','.join('%s' for _ in columns)+')',tuple(row[k] for k in columns))
        if apply and not imported:
            sid=row.get('session_id',row.get('id','')) if name!='xj2_qa_meta' else ''
            write(connection,'INSERT INTO '+table('xj2_qa_import')+' (source,session_id,imported_at) VALUES(%s,%s,%s)',
                  (marker,sid,time.time()))
        counts[name]+=1
    return {'verified_rows':counts,'missing_rows':missing,'unmarked_rows':unmarked}


def run(prefix, *, apply=False, connect=None):
    prefix=Path(prefix).absolute()
    folder=prefix/'deployments/qa-history-migration'
    if folder.resolve()!=folder:
        raise MigrationError('QA_HISTORY_JOURNAL_PATH_INVALID')
    state=folder/'journal.json'
    if state.is_symlink():raise MigrationError('QA_HISTORY_JOURNAL_PATH_INVALID')
    values=read_env(prefix/'config/xunjia.env')
    options=connection_options(values.get('XUNJIA_DATABASE_URL'))
    identity=hashlib.sha256(encoded([options['host'],options['port'],options['user'],DATABASE]).encode()).hexdigest()
    ddl=statements();ddl_hash=hashlib.sha256(encoded(ddl).encode()).hexdigest()
    prior=json.loads(state.read_text()) if state.exists() else None
    if prior and (prior.get('database_identity')!=identity or prior.get('ddl_sha256')!=ddl_hash):
        raise MigrationError('QA_HISTORY_JOURNAL_IDENTITY_MISMATCH')
    # QAHistory historically follows QAWorkflow's managed config prefix, not
    # the independently configurable collector state directory.
    if values.get('XUNJIA_PREFIX') and Path(values['XUNJIA_PREFIX'])!=prefix:
        raise MigrationError('QA_HISTORY_SOURCE_PREFIX_MISMATCH')
    source_path=prefix/'state/qa-history.db'
    if not source_path.is_absolute() or source_path.resolve()!=source_path:
        raise MigrationError('QA_HISTORY_SQLITE_SOURCE_UNSAFE')
    source_identity=hashlib.sha256(str(source_path).encode()).hexdigest()[:16]
    connection=None
    try:
        if connect is None:
            import pymysql
            connect=lambda **kw:pymysql.connect(**kw,cursorclass=pymysql.cursors.DictCursor)
        connection=connect(**options)
        verify_server(connection);connection.select_db(DATABASE)
        present=inspect(connection)
        with sqlite_source(source_path) as source:
            summary=source_summary(source)
            initial=transfer(connection,source,present,source_identity)
            report={'database':DATABASE,'migration':'qa_history','ddl_executed':False,
                    'source_retained':True,'source':summary,'tables_verified':len(present),**initial}
            metadata=query(connection,"SELECT value FROM "+table('xj2_qa_meta')+" WHERE `key`='created_at'",one=True) if 'xj2_qa_meta' in present else None
            required=len(present)!=len(SCHEMA) or initial['missing_rows'] or initial['unmarked_rows'] or metadata is None
            if not apply:
                return {**report,'status':'MIGRATION_REQUIRED' if required else 'ALREADY_APPLIED'}
            folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            if folder.stat().st_mode & 0o077:
                raise MigrationError('QA_HISTORY_JOURNAL_PERMISSIONS')
            if not (folder/'schema-before.json').exists():
                atomic_json(folder/'schema-before.json',{name:query(connection,'SHOW CREATE TABLE '+table(name),one=True) for name in present})
            if source is not None:
                backup=folder/('source-'+summary['sha256']+'.sqlite3')
                if backup.is_symlink():raise MigrationError('QA_HISTORY_BACKUP_PATH_INVALID')
                if not backup.exists():
                    fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                    os.close(fd)
                    destination=sqlite3.connect(backup)
                    try:source.backup(destination)
                    finally:destination.close()
                with sqlite_source(backup) as saved:
                    if source_summary(saved)!=summary:raise MigrationError('QA_HISTORY_BACKUP_VERIFICATION_FAILED')
            record={'database_identity':identity,'ddl_sha256':ddl_hash,'status':'STARTED',
                    'source':summary,'steps':dict(prior.get('steps',{})) if prior else {}}
            atomic_json(state,record)
            for name,sql in ddl.items():
                if name in present:
                    record['steps'][name]={'status':'VERIFIED'}
                    continue
                if record['steps'].get(name,{}).get('status') in ('STARTED','UNKNOWN'):
                    raise MigrationError('QA_HISTORY_DDL_REQUIRES_DBA_RECONCILIATION')
                record['steps'][name]={'status':'STARTED'};atomic_json(state,record)
                try:
                    execute(connection,sql)
                    present=inspect(connection)
                    if name not in present:raise MigrationError('QA_HISTORY_TABLE_NOT_CREATED')
                except Exception as exc:
                    failure=safe_failure(exc)
                    status='REJECTED' if failure['database_error_code'] in (1044,1045,1142,1143,1227) else 'UNKNOWN'
                    record['steps'][name]={'status':status,**failure};record['status']=status
                    atomic_json(state,record)
                    raise MigrationError('QA_HISTORY_DDL_PERMISSION_DENIED' if status=='REJECTED' else 'QA_HISTORY_DDL_OUTCOME_UNKNOWN') from None
                report['ddl_executed']=True
                record['steps'][name]={'status':'VERIFIED'};atomic_json(state,record)
            connection.begin()
            try:
                verify_writes(connection)
                transferred=transfer(connection,source,present,source_identity,apply=True)
                write(connection,"INSERT IGNORE INTO "+table('xj2_qa_meta')+" (`key`,value) VALUES('created_at',%s)",(time.time(),))
                verified=transfer(connection,source,present,source_identity)
                if verified['missing_rows'] or verified['unmarked_rows'] or verified['verified_rows']!=summary['counts']:
                    raise MigrationError('QA_HISTORY_IMPORT_COUNT_MISMATCH')
                connection.commit()
            except Exception:
                connection.rollback();record['status']='IMPORT_NOT_CONFIRMED';atomic_json(state,record)
                raise
            record.update(status='VERIFIED',verified_rows=verified['verified_rows'],source_retained=True)
            atomic_json(state,record)
            return {**report,**verified,'tables_verified':len(present),'rows_inserted':transferred['missing_rows'],
                    'write_privileges_verified':True,'status':'MIGRATION_VERIFIED' if required else 'ALREADY_APPLIED'}
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('QA_HISTORY_MIGRATION_FAILED_VERIFY_CONNECT_CREATE_AND_WRITE_PRIVILEGES') from None
    finally:
        if connection is not None:connection.close()
