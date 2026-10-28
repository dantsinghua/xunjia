"""Explicit fresh schema for xunjia_business only; never migrate old business data.

The caller stops the dedicated service before --apply.  --check is read-only.
Every DDL has a durable outcome. Interrupted/uncertain operations are not replayed.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" if (ROOT / "app/server/schema.py").is_file() else ROOT
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "deploy"))
from common import atomic_json, read_env, effective_env
from server.schema import TABLES, statements
from server.health import schema_health
from server.db import Transaction

DATABASE = "xunjia_business"
QB_TABLES = ("qb_live_best_quote", "qb_live_trade", "qb_broker", "qb_bond_dict")
QB_VIEW = "qb_live_best_quote_latest"


class UpgradeError(ValueError):
    """Fixed safe code; database exceptions/DSNs never enter output."""


def connection_options(dsn):
    try:
        parsed = urlsplit(dsn)
        if (parsed.scheme != "oceanbase" or not parsed.hostname or parsed.query or parsed.fragment
                or parsed.path != "/" + DATABASE or not parsed.username or not parsed.password
                or not 1 <= (parsed.port or 3306) <= 65535
                or "REPLACE_" in dsn or any(c.isspace() for c in parsed.hostname)):
            raise ValueError()
        user, password = unquote(parsed.username), unquote(parsed.password)
        if any(c in user + password for c in "\x00\r\n"):
            raise ValueError()
        return {"host": parsed.hostname, "port": parsed.port or 3306, "user": user, "password": password,
                "charset": "utf8mb4", "connect_timeout": 10, "read_timeout": 30,
                "write_timeout": 30, "autocommit": True}
    except (ValueError, TypeError, AttributeError):
        raise UpgradeError("EXACT_XUNJIA_BUSINESS_DSN_REQUIRED") from None


def identifier(name):
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) is None:
        raise UpgradeError("UNSAFE_DATABASE_OBJECT_NAME")
    return "`" + name + "`"


def quote_enabled(values, business):
    source = values.get("XUNJIA_QUOTE_SOURCE", "")
    if not isinstance(source, str):
        raise UpgradeError("QUOTE_SOURCE_INVALID")
    source = source.strip().lower()
    if source not in ("", "qb"):
        raise UpgradeError("QUOTE_SOURCE_INVALID")
    if not source:
        return False
    try:
        same = (values["QB_MYSQL_HOST"] == business["host"]
                and int(values["QB_MYSQL_PORT"]) == business["port"]
                and values["QB_MYSQL_USER"] == business["user"]
                and values["QB_MYSQL_PASSWORD"] == business["password"]
                and values["QB_MYSQL_DB"] == DATABASE)
    except (KeyError, ValueError, TypeError):
        same = False
    if not same:
        raise UpgradeError("QUOTE_DATABASE_MUST_MATCH_BUSINESS")
    return True


def quote_statements(source=None):
    """Accept only the five current collector DDL declarations, never arbitrary SQL."""
    if source is None:
        source = (APP / "collector/schema_qb_live.sql").read_text(encoding="utf8")
    text = re.sub(r"^\s*--[^\n]*$", "", source, flags=re.M)
    # This fixed source needs no escaped identifiers, inline comments or SQL directives.
    if any(marker in text for marker in ("/*", "*/", "--", "`", "\x00", "\\")):
        raise UpgradeError("QUOTE_SCHEMA_SOURCE_INVALID")
    parts = [part.strip() for part in text.split(";") if part.strip()]
    if len(parts) != 5:
        raise UpgradeError("QUOTE_SCHEMA_SOURCE_INVALID")
    result = []
    for table, sql in zip(QB_TABLES, parts[:4]):
        prefix = "CREATE TABLE IF NOT EXISTS " + table + " ("
        if (not sql.startswith(prefix)
                or re.search(r"\)\s*ENGINE=InnoDB DEFAULT CHARSET=utf8mb4(?: COMMENT='[^']*')?$", sql) is None
                or re.search(r"\b(?:REFERENCES|SELECT|DROP|ALTER|GRANT|INSERT|DELETE|UPDATE)\b", sql, re.I)):
            raise UpgradeError("QUOTE_SCHEMA_SOURCE_INVALID")
        declared = set(re.findall(r"^\s*(\w+)\s+(?:BIGINT|INT|TINYINT|VARCHAR|DATETIME|DATE|DECIMAL|JSON)\b", sql, re.M))
        if not quote_columns()[table] <= declared:
            raise UpgradeError("QUOTE_SCHEMA_SOURCE_INVALID")
        result.append({"kind": "create_table", "object": table,
                       "sql": sql.replace("CREATE TABLE IF NOT EXISTS " + table,
                                          "CREATE TABLE IF NOT EXISTS `xunjia_business`." + identifier(table), 1)})
    view = parts[-1]
    if (not view.startswith("CREATE OR REPLACE VIEW " + QB_VIEW + " AS")
            or re.search(r"\bSELECT\s+b\.name_broker\s+AS\s+broker\s*,\s*d\.bond_code\s*,\s*d\.bond_short_name\s*,\s*q\.\*", view, re.I) is None
            or re.search(r"\b(?:INTO|OUTFILE|DUMPFILE|UNION|LOAD|DROP|ALTER|GRANT|INSERT|DELETE|UPDATE|CALL)\b", view, re.I)
            or re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)", view, re.I)
            != ["qb_live_best_quote", "qb_live_best_quote", "qb_bond_dict", "qb_broker"]
            or set(re.findall(r"\b([A-Za-z_]\w*)\.(?:[A-Za-z_]\w*|\*)", view)) - {"q", "d", "b"}):
        raise UpgradeError("QUOTE_SCHEMA_SOURCE_INVALID")
    for name in (*QB_TABLES, QB_VIEW):
        view = re.sub(r"\b" + re.escape(name) + r"\b", "`xunjia_business`." + identifier(name), view)
    result.append({"kind": "create_view", "object": QB_VIEW, "sql": view})
    return result


def quote_columns():
    # Read the writer's literal contract without importing its environment-derived DB defaults.
    columns = {}
    try:
        tree = ast.parse((APP / "collector/store.py").read_text(encoding="utf8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("Q_COLS", "T_COLS"):
                        value = ast.literal_eval(node.value)
                        if (target.id in columns or not isinstance(value, list) or not value
                                or any(not isinstance(item, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", item) for item in value)
                                or len(set(value)) != len(value)):
                            raise ValueError()
                        columns[target.id] = set(value)
        if set(columns) != {"Q_COLS", "T_COLS"}:
            raise ValueError()
    except (OSError, ValueError, SyntaxError, TypeError):
        raise UpgradeError("QUOTE_WRITER_COLUMN_CONTRACT_INVALID") from None
    return {"qb_live_best_quote": columns["Q_COLS"] | {"id"}, "qb_live_trade": columns["T_COLS"] | {"id"},
            "qb_broker": {"broker_id", "broker_name"},
            "qb_bond_dict": {"bond_uni_code", "bond_code", "bond_short_name", "com_short_name"},
            QB_VIEW: columns["Q_COLS"] | {"id", "broker", "bond_code", "bond_short_name"}}


def query(connection, sql, args=(), *, one=False):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, args)
        return cursor.fetchone() if one else cursor.fetchall()
    finally:
        cursor.close()


def execute(connection, sql):
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()


def verify_server(connection):
    version = query(connection, "SELECT VERSION() AS version", one=True)
    mode = query(connection, "SHOW VARIABLES LIKE 'ob_compatibility_mode'", one=True)
    if (not isinstance(version, dict) or "oceanbase" not in str(version.get("version", "")).lower()
            or not isinstance(mode, dict) or str(mode.get("Value", "")).upper() != "MYSQL"):
        raise UpgradeError("REAL_OCEANBASE_MYSQL_MODE_REQUIRED")


def inspect_database(connection):
    found = query(connection, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (DATABASE,))
    before = {"database_exists": bool(found), "objects": [], "external_references": [], "routines": [], "events": []}
    if not found:
        return before
    connection.select_db(DATABASE)
    selected = query(connection, "SELECT DATABASE() AS selected", one=True)
    if not selected or selected.get("selected") != DATABASE:
        raise UpgradeError("SELECTED_DATABASE_MISMATCH")
    # Enumerate objects only; no old customer rows or credential contents are read.
    rows = query(connection, "SELECT TABLE_NAME,TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME", (DATABASE,))
    before["objects"] = [{"name": row["TABLE_NAME"], "type": row["TABLE_TYPE"]} for row in rows]
    before["external_references"] = query(connection,
        "SELECT CONSTRAINT_SCHEMA,TABLE_NAME,CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS "
        "WHERE UNIQUE_CONSTRAINT_SCHEMA=%s AND CONSTRAINT_SCHEMA<>%s", (DATABASE, DATABASE))
    before["routines"] = [row["ROUTINE_NAME"] for row in query(connection,
        "SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=%s", (DATABASE,))]
    before["events"] = [row["EVENT_NAME"] for row in query(connection,
        "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA=%s", (DATABASE,))]
    return before


def plan_fresh(before, *, quote_enabled=False):
    if type(quote_enabled) is not bool:
        raise UpgradeError("QUOTE_SOURCE_INVALID")
    if set(before) != {"database_exists", "objects", "external_references", "routines", "events"}:
        raise UpgradeError("INVALID_DATABASE_SNAPSHOT")
    if type(before["database_exists"]) is not bool or not isinstance(before["objects"], list):
        raise UpgradeError("INVALID_DATABASE_SNAPSHOT")
    if before["external_references"]:
        raise UpgradeError("OTHER_DATABASE_REFERENCES_TARGET")
    if before["routines"] or before["events"]:
        raise UpgradeError("UNEXPECTED_DATABASE_ROUTINES_OR_EVENTS")
    names, objects = set(), []
    for item in before["objects"]:
        if not isinstance(item, dict) or set(item) != {"name", "type"}:
            raise UpgradeError("INVALID_DATABASE_OBJECT")
        identifier(item["name"])
        if item["name"] in names or item["type"] not in ("BASE TABLE", "VIEW"):
            raise UpgradeError("UNEXPECTED_DATABASE_OBJECT_TYPE")
        names.add(item["name"])
        objects.append(item)
    if not before["database_exists"] and objects:
        raise UpgradeError("INVALID_DATABASE_SNAPSHOT")
    steps = []
    if not before["database_exists"]:
        steps.append({"kind": "create_database", "sql": "CREATE DATABASE IF NOT EXISTS `xunjia_business` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"})
    for obj in sorted(objects, key=lambda item: (item["type"] != "VIEW", item["name"])):
        kind = "VIEW" if obj["type"] == "VIEW" else "TABLE"
        steps.append({"kind": "drop_" + kind.lower(), "object": obj["name"],
                      "sql": "DROP " + kind + " IF EXISTS `xunjia_business`." + identifier(obj["name"])})
    ddl = statements(mysql=True)
    if len(ddl) != len(TABLES) or len(set(TABLES)) != len(TABLES):
        raise UpgradeError("SCHEMA_SOURCE_INVALID")
    for table, sql in zip(TABLES, ddl):
        prefix = "CREATE TABLE IF NOT EXISTS " + table + " ("
        if not table.startswith("xj2_") or not sql.startswith(prefix) or ";" in sql:
            raise UpgradeError("SCHEMA_SOURCE_INVALID")
        qualified = sql.replace("CREATE TABLE IF NOT EXISTS " + table,
                                "CREATE TABLE IF NOT EXISTS `xunjia_business`." + identifier(table), 1)
        steps.append({"kind": "create_table", "object": table, "sql": qualified})
    if quote_enabled:
        steps.extend(quote_statements())
    return {"database": DATABASE, "preserve_history": False, "quote_enabled": quote_enabled,
            "before": before, "steps": steps}


def require_service_stopped(service):
    if not isinstance(service, str) or not re.fullmatch(r"xunjia-agent(?:-[A-Za-z0-9_-]{1,50})?\.service", service):
        raise UpgradeError("OTHER_SERVICE_FORBIDDEN")
    executable = shutil.which("systemctl")
    if not executable:
        raise UpgradeError("SERVICE_STATUS_UNVERIFIED")
    try:
        result = subprocess.run([executable, "show", service, "--property=LoadState", "--property=ActiveState", "--property=MainPID"],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise UpgradeError("SERVICE_STATUS_UNVERIFIED") from None
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or values.get("LoadState") not in ("loaded", "not-found"):
        raise UpgradeError("SERVICE_STATUS_UNVERIFIED")
    if values.get("ActiveState") not in ("inactive", "failed") or values.get("MainPID") != "0":
        raise UpgradeError("SERVICE_NOT_STOPPED")


def safe_failure(exc):
    code = exc.args[0] if exc.args and type(exc.args[0]) is int else None
    return {"error_type": type(exc).__name__, "database_error_code": code}


def apply_plan(connection, plan, evidence):
    evidence = Path(evidence)
    if plan != plan_fresh(plan.get("before", {}), quote_enabled=plan.get("quote_enabled", False)):
        raise UpgradeError("PLAN_CHANGED")
    journal_path = evidence / "ddl-journal.json"
    if journal_path.exists():
        raise UpgradeError("DDL_JOURNAL_ALREADY_EXISTS")
    journal = {"database": DATABASE, "started_at": time.time(), "status": "STARTED", "steps": []}
    atomic_json(journal_path, journal)
    setting = query(connection, "SELECT @@SESSION.FOREIGN_KEY_CHECKS AS enabled", one=True)
    enabled = setting.get("enabled") if isinstance(setting, dict) else None
    if enabled not in (0, 1):
        raise UpgradeError("FOREIGN_KEY_SETTING_UNVERIFIED")
    journal["foreign_key_checks_before"] = int(enabled)
    atomic_json(journal_path, journal)
    error = None
    try:
        execute(connection, "SET SESSION FOREIGN_KEY_CHECKS=0")
        for number, step in enumerate(plan["steps"], 1):
            row = {"number": number, **step, "status": "STARTED", "at": time.time()}
            journal["steps"].append(row)
            atomic_json(journal_path, journal)
            try:
                execute(connection, step["sql"])
            except BaseException as exc:
                # Treat all failed DDL as uncertain: proxy disconnects can hide a commit.
                row.update(status="UNKNOWN", **safe_failure(exc))
                journal["status"] = "UNKNOWN"
                atomic_json(journal_path, journal)
                raise UpgradeError("DDL_OUTCOME_UNKNOWN") from None
            row.update(status="COMPLETED", completed_at=time.time())
            atomic_json(journal_path, journal)
        journal["status"] = "DDL_COMPLETED"
    except BaseException as exc:
        error = exc
        if journal["status"] == "STARTED":
            journal["status"] = "UNKNOWN"
            journal["failure"] = safe_failure(exc)
    finally:
        try:
            execute(connection, "SET SESSION FOREIGN_KEY_CHECKS=" + str(int(enabled)))
            journal["foreign_key_checks_restored"] = True
        except BaseException as exc:
            journal["foreign_key_checks_restored"] = False
            journal["restore_failure"] = safe_failure(exc)
            journal["status"] = "UNKNOWN"
            if error is None:
                error = UpgradeError("SESSION_RESTORE_OUTCOME_UNKNOWN")
        atomic_json(journal_path, journal)
    if error is not None:
        raise error


class _ReadOnlyDatabase:
    mysql = True
    def __init__(self, connection):
        self.connection = connection
    @contextmanager
    def transaction(self, *, write=False):
        if write:
            raise UpgradeError("HEALTH_WRITES_FORBIDDEN")
        yield Transaction(self.connection, True)


def verify_fresh(connection, *, quote_enabled=False):
    connection.select_db(DATABASE)
    selected = query(connection, "SELECT DATABASE() AS selected", one=True)
    if not selected or selected.get("selected") != DATABASE:
        raise UpgradeError("SELECTED_DATABASE_MISMATCH")
    repository = type("SchemaRepository", (), {"db": _ReadOnlyDatabase(connection)})()
    health = schema_health(repository)
    if not health.get("ready"):
        raise UpgradeError("FRESH_SCHEMA_HEALTH_FAILED")
    rows = query(connection, "SELECT TABLE_NAME,TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (DATABASE,))
    expected = {(table, "BASE TABLE") for table in TABLES}
    if quote_enabled:
        expected |= {(table, "BASE TABLE") for table in QB_TABLES} | {(QB_VIEW, "VIEW")}
    if {(row["TABLE_NAME"], row["TABLE_TYPE"]) for row in rows} != expected:
        raise UpgradeError("FRESH_SCHEMA_OBJECT_SET_MISMATCH")
    if quote_enabled:
        for table, required in quote_columns().items():
            columns = query(connection, "SHOW COLUMNS FROM `xunjia_business`." + identifier(table))
            if not required <= {row["Field"] for row in columns}:
                raise UpgradeError("FRESH_QUOTE_SCHEMA_COLUMNS_MISSING")
        # Fresh deployment does not invent/import any market dictionary rows.
        row = query(connection, "SELECT COUNT(*) AS n FROM `xunjia_business`.`qb_bond_dict`", one=True)
        if not row or row.get("n") != 0:
            raise UpgradeError("FRESH_QUOTE_DICTIONARY_CHANGED_CONCURRENTLY")
        health["quote"] = {"schema_ready": True, "tables": 4, "views": 1,
                           "dictionary_ready": False, "code": "REAL_DICTIONARY_IMPORT_REQUIRED"}
    return health


def run_fresh(values, evidence_dir, *, apply=False, connect=None, service="xunjia-agent.service"):
    options = connection_options(values.get("XUNJIA_DATABASE_URL", ""))
    qb = quote_enabled(values, options)
    if apply:
        require_service_stopped(service)
    evidence = Path(evidence_dir).absolute()
    if evidence.resolve() != evidence:
        raise UpgradeError("EVIDENCE_SYMLINK_FORBIDDEN")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(json.dumps([options["host"], options["port"], DATABASE]).encode()).hexdigest()[:24]
    state_path = evidence.parent / ("database-fresh-" + identity + ".json")
    lock_path = evidence.parent / ("database-fresh-" + identity + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    connection = None
    started = False
    owns_evidence = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpgradeError("DATABASE_FRESH_ALREADY_RUNNING") from None
        if apply and state_path.exists():
            if state_path.is_symlink():
                raise UpgradeError("DATABASE_FRESH_STATE_INVALID")
            prior = json.loads(state_path.read_text())
            if prior.get("status") in ("STARTED", "UNKNOWN"):
                raise UpgradeError("PREVIOUS_DDL_REQUIRES_READONLY_RECONCILIATION")
        try:
            evidence.mkdir(mode=0o700)
            owns_evidence = True
        except FileExistsError:
            raise UpgradeError("FRESH_EVIDENCE_DIRECTORY_REQUIRED") from None
        if connect is None:
            import pymysql
            connect = lambda **kw: pymysql.connect(**kw, cursorclass=pymysql.cursors.DictCursor)
        connection = connect(**options)
        verify_server(connection)
        before = inspect_database(connection)
        atomic_json(evidence / "objects-before.json", before)
        plan = plan_fresh(before, quote_enabled=qb)
        atomic_json(evidence / "plan.json", plan)
        if not apply:
            result = {"status": "CHECK_ONLY", "database": DATABASE, "planned_statements": len(plan["steps"]), "ddl_executed": 0}
        else:
            require_service_stopped(service)
            if inspect_database(connection) != before:
                raise UpgradeError("DATABASE_OBJECTS_CHANGED_BEFORE_APPLY")
            atomic_json(state_path, {"status": "STARTED", "evidence": str(evidence), "at": time.time()})
            started = True
            apply_plan(connection, plan, evidence)
            health = verify_fresh(connection, quote_enabled=qb)
            atomic_json(evidence / "schema-health.json", health)
            result = {"status": "FRESH_SCHEMA_VERIFIED", "database": DATABASE,
                      "ddl_executed": len(plan["steps"]), "tables": len(TABLES), "history_preserved": False}
            if qb:
                result.update(tables=len(TABLES) + len(QB_TABLES), views=1,
                              quote_schema_ready=True, dictionary_ready=False,
                              quote_status="REAL_DICTIONARY_IMPORT_REQUIRED")
            atomic_json(state_path, {"status": "VERIFIED", "evidence": str(evidence), "at": time.time()})
        atomic_json(evidence / "RESULT.json", result)
        return result
    except BaseException as exc:
        if started:
            atomic_json(state_path, {"status": "UNKNOWN", "evidence": str(evidence), "at": time.time(), **safe_failure(exc)})
        safe = str(exc) if isinstance(exc, UpgradeError) else "DATABASE_FRESH_UNAVAILABLE"
        if owns_evidence:
            atomic_json(evidence / "RESULT.json", {"status": "UNKNOWN" if started else "STOPPED", "code": safe, **safe_failure(exc)})
        raise UpgradeError(safe) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--service", default="xunjia-agent.service")
    args = parser.parse_args()
    try:
        prefix=args.config.resolve().parent.parent
        values=effective_env(read_env(args.config),prefix,prefix/'app')
        result = run_fresh(values, args.evidence, apply=args.apply, service=args.service)
    except Exception as exc:
        print(json.dumps({"status": "STOPPED", "code": str(exc) if isinstance(exc, UpgradeError) else "DATABASE_FRESH_UNAVAILABLE"}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
