"""OceanBase DDL artifact contract. No database access or network requests."""
from __future__ import annotations

import re

from scripts.export_oceanbase_schema import SQL_FILE, TABLES, render
from server.schema import statements


def test_readable_sql_is_generated_from_current_authoritative_schema():
    assert SQL_FILE.exists()
    assert SQL_FILE.read_text(encoding="utf-8") == render()


def test_sql_has_fifteen_v22_tables_and_thirteen_secondary_indexes():
    content = SQL_FILE.read_text(encoding="utf-8")
    executable = "\n".join(line for line in content.splitlines()
                           if line.strip() and not line.lstrip().startswith("--"))
    entries = [statement.strip() for statement in executable.split(";") if statement.strip()]
    assert len(entries) == len(TABLES) == 15
    tables = [re.search(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", entry).group(1)
              for entry in entries]
    assert tuple(tables) == TABLES
    assert all("ENGINE=InnoDB" in entry and
               "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin" in entry
               for entry in entries)
    secondary = re.findall(r"\bKEY (idx_\w+)\s*\(", executable)
    assert len(secondary) == len(set(secondary)) == 13


def test_sql_is_not_privileged_and_contains_no_password_or_customer_data():
    sql = SQL_FILE.read_text(encoding="utf-8")
    executable = "\n".join(line for line in sql.splitlines()
                           if line.strip() and not line.lstrip().startswith("--"))
    for prohibited in ("CREATE DATABASE", "DROP TABLE", "DROP DATABASE",
                       "GRANT ", "ALTER USER", "USE ", "INSERT INTO", "DELETE FROM",
                       "UPDATE ", "REPLACE INTO", "SET PASSWORD"):
        assert prohibited not in executable.upper()
    assert "IF NOT EXISTS" in executable.upper()


def test_sql_matches_the_python_init_statement_order():
    sql = SQL_FILE.read_text(encoding="utf-8")
    entries = [statement.strip() for statement in
               "\n".join(line for line in sql.splitlines()
                         if line.strip() and not line.lstrip().startswith("--")).split(";")
               if statement.strip()]
    assert entries == statements(mysql=True)
