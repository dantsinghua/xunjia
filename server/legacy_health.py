"""Read-only release identity and full business-schema readiness; no live Dify/OSS calls."""
from __future__ import annotations
import json
import os
import re
import uuid
from pathlib import Path
from .legacy_schema import statements
def _parts(body):
    out=[]; start=0; depth=0
    for i, c in enumerate(body):
        if c=='(': depth+=1
        elif c==')': depth-=1
        elif c==',' and depth==0: out.append(body[start:i].strip());start=i+1
    out.append(body[start:].strip())
    return out
def contract(mysql):
    tables={}
    for ddl in statements(mysql):
        if not ddl.startswith('CREATE TABLE'): continue
        table=re.search(r'CREATE TABLE IF NOT EXISTS (\w+)',ddl).group(1)
        body=ddl[ddl.index('(')+1:ddl.rfind(')')]
        columns={}; unique=[]; secondary={}
        for part in _parts(body):
            key=re.match(r'KEY (\w+)\s*\(([^)]+)\)',part)
            composite=re.match(r'UNIQUE\s*\(([^)]+)\)',part)
            if key:
                secondary[key[1]]=tuple(x.strip() for x in key[2].split(','));continue
            if composite:
                unique.append(tuple(x.strip() for x in composite[1].split(',')));continue
            col=re.match(r'(\w+)\s+(VARCHAR\(\d+\)|BIGINT|INTEGER|DOUBLE|LONGTEXT|TEXT)',part)
            if col is None:
                col=re.match(r'(\w+)\s+(VARCHAR\(\d+\)|BIGINT|INTEGER|DOUBLE|LONGTEXT|TEXT)',part)
            if col is None: raise ValueError('Invalid generated schema column')
            columns[col[1]]=col[2].lower().replace('integer','int')
            if 'PRIMARY KEY' in part or 'UNIQUE' in part: unique.append((col[1],))
        tables[table]={'columns':columns,'unique':unique,'secondary':secondary}
    if not mysql:
        for ddl in statements(False):
            match=re.match(r'CREATE INDEX IF NOT EXISTS (\w+) ON (\w+) \(([^)]+)\)',ddl)
            if match: tables[match[2]]['secondary'][match[1]]=tuple(x.strip() for x in match[3].split(','))
    return tables
def _type(text):
    lower=text.lower().replace('integer','int')
    return re.sub(r'^(bigint|int)\(\d+\)',r'\1',lower)
def schema_health(repository):
    expected=contract(repository.db.mysql)
    missing_tables=[];missing_columns=[];invalid_columns=[];missing_indexes=[]
    try:
        with repository.db.transaction(write=False) as tx:
            if repository.db.mysql:
                rows=tx.execute('SHOW TABLES').fetchall()
                existing={next(iter(row.values())) for row in rows}
            else:
                existing={row['name'] for row in tx.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for table,spec in expected.items():
                if table not in existing:
                    missing_tables.append(table);continue
                indexes={};unique=set()
                if repository.db.mysql:
                    cols=tx.execute('SHOW COLUMNS FROM '+table).fetchall()
                    actual={row['Field']:_type(row['Type']) for row in cols}
                    for row in tx.execute('SHOW INDEX FROM '+table).fetchall():
                        indexes.setdefault(row['Key_name'],[]).append((int(row['Seq_in_index']),row['Column_name']))
                        if int(row['Non_unique'])==0:unique.add(row['Key_name'])
                else:
                    cols=tx.execute('PRAGMA table_info('+table+')').fetchall()
                    actual={row['name']:_type(row['type']) for row in cols}
                    for row in tx.execute('PRAGMA index_list('+table+')').fetchall():
                        name=row['name']
                        if not re.fullmatch(r'\w+',name): continue
                        indexes[name]=[(int(c['seqno']),c['name']) for c in tx.execute('PRAGMA index_info('+name+')').fetchall()]
                        if row['unique']: unique.add(name)
                normalized={name:tuple(col for _,col in sorted(parts)) for name,parts in indexes.items()}
                for name,typ in spec['columns'].items():
                    if name not in actual:missing_columns.append(table+'.'+name)
                    elif actual[name]!=typ:invalid_columns.append(table+'.'+name)
                for name,columns in spec['secondary'].items():
                    if normalized.get(name)!=columns:missing_indexes.append(table+'.'+name)
                constraints={normalized[name] for name in unique}
                for cols in spec['unique']:
                    if cols not in constraints:missing_indexes.append(table+'.unique('+','.join(cols)+')')
    except Exception as exc:
        return {'ready':False,'code':'DATABASE_UNAVAILABLE','error_type':type(exc).__name__}
    problems=missing_tables or missing_columns or invalid_columns or missing_indexes
    return {'ready':not bool(problems),'code':'SCHEMA_INCOMPLETE' if problems else 'SCHEMA_OK',
            'tables_expected':len(expected),'indexes_expected':sum(len(s['secondary']) for s in expected.values()),
            'missing_tables':missing_tables,'missing_columns':missing_columns,
            'invalid_columns':invalid_columns,'missing_indexes':missing_indexes}
def release_identity():
    root=Path(__file__).resolve().parents[1]
    identity={'build_id':'development','release_id':'development','boot_id':uuid.uuid4().hex,'pid':os.getpid()}
    try:
        version=json.loads((root/'VERSION.json').read_text(encoding='utf8'))
        deployment=json.loads((root/'.deployment.json').read_text(encoding='utf8'))
        if version['build_id']!=deployment['build_id']: raise ValueError('build mismatch')
        identity.update(build_id=version['build_id'],release_id=deployment['release_id'])
    except FileNotFoundError:
        if (root/'VERSION.json').exists() or (root/'.deployment.json').exists():
            identity.update(build_id='invalid-metadata',release_id='invalid-metadata')
    except (ValueError,KeyError,OSError):
        identity.update(build_id='invalid-metadata',release_id='invalid-metadata')
    return identity
def health_report(repository, auth, identity, *, enabled, transport, chat):
    schema=schema_health(repository)
    ready=bool(schema['ready'] and auth.configured and identity['build_id']!='invalid-metadata')
    reasons=[]
    if not schema['ready']:reasons.append(schema['code'])
    if not auth.configured:reasons.append('AUTH_NOT_CONFIGURED')
    if identity['build_id']=='invalid-metadata':reasons.append('RELEASE_IDENTITY_INVALID')
    # Configured is not the same as successfully tested with a native collector.
    automation=bool(enabled and transport is not None and chat is not None)
    return {'service':'xunjia',**identity,'ready':ready,'database_ready':schema['ready'],
            'schema':schema,'auth_configured':auth.configured,'reasons':reasons,
            'background_enabled':enabled,'automation_configured':automation,
            'automation_ready':False,'automation_state':'live_chain_not_verified'}