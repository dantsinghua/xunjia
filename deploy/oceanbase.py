#!/usr/bin/env python3
"""OceanBase-only preflight; schema DDL requires an explicit --apply."""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from server.negotiation.application_store import ApplicationStore
from server.schema import TABLES
from server.health import schema_health

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true',help='Create missing xj2_* tables after DBA review')
    args=parser.parse_args()
    url=os.getenv('XUNJIA_DATABASE_URL','')
    parts=urlsplit(url)
    if parts.scheme!='oceanbase' or not parts.hostname or not parts.path.lstrip('/').startswith('xunjia_'):
        raise SystemExit('Use an explicitly selected dedicated xunjia_* OceanBase schema; do not point at the njcb business database')
    repo=ApplicationStore(url,initialize=False)
    with repo.db.transaction(write=False) as tx:
        version=tx.execute('SELECT VERSION() AS version, DATABASE() AS selected').fetchone()
        compatibility=tx.execute("SHOW VARIABLES LIKE 'ob_compatibility_mode'").fetchone()
    if 'OceanBase' not in version['version'] or not compatibility or str(compatibility['Value']).upper()!='MYSQL':
        raise SystemExit('Expected a real OceanBase server in MySQL compatibility mode')
    print('Verified OceanBase MySQL-mode, schema:',version['selected'])
    root=Path(__file__).resolve().parents[1]
    exporter=root/'scripts/export_oceanbase_schema.py'
    subprocess.run([sys.executable,str(exporter),'--check'],check=True)
    if args.apply:
        print('DBA-authorized DDL: CREATE TABLE IF NOT EXISTS for project xj2_* tables only')
        repo.initialize()
    with repo.db.transaction(write=False) as tx:
        names=[next(iter(row.values())) for row in tx.execute('SHOW TABLES').fetchall()]
    existing=[name for name in names if name in TABLES]
    print('xj2_* tables:',len(existing),'expected:',len(TABLES))
    health=schema_health(repo)
    if not health['ready']:
        raise SystemExit('V2.2 schema incomplete; review exported DDL and explicit migration before --apply')
if __name__=='__main__':
    main()
