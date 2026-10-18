"""Regression from the real OceanBase CE 4.4.2.1 empty-schema acceptance.

The live server rejected xj2_group.plan_json LONGTEXT DEFAULT '{}' with 1067.
This gate validates the exported schema producer without needing a live server
for ordinary regression runs; live transactional evidence is recorded separately.
"""
import re

from server.schema import statements


def test_oceanbase_mysql_ddl_does_not_default_longtext_to_a_literal():
    unsupported = []
    for ddl in statements(mysql=True):
        table = re.search(r'CREATE TABLE IF NOT EXISTS (\w+)', ddl)[1]
        for field, value in re.findall(
                r'\b(\w+)\s+(?:LONGTEXT|TEXT|BLOB)\b[^,\n)]*\bDEFAULT\s+(\S+)', ddl, re.I):
            if value.rstrip(',').upper() != 'NULL':
                unsupported.append(table + '.' + field)
    assert unsupported == [], 'OceanBase rejected non-NULL TEXT/BLOB literal defaults: ' + ', '.join(unsupported)
