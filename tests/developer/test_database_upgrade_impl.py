"""Synthetic fresh-install contract; no real database connections."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from deploy import database_upgrade as fresh


def snapshot():
    legacy = ["xj_audit", "xj_batch", "xj_binding", "xj_command", "xj_conversation",
              "xj_cursor", "xj_message", "xj_session", "jx_turn"]
    return {"database_exists": True, "objects": [{"name": n, "type": "BASE TABLE"} for n in legacy],
            "external_references": [], "routines": [], "events": []}


def test_old_nine_removed_inside_exact_database_and_sixteen_created():
    plan = fresh.plan_fresh(snapshot())
    assert len(plan["steps"]) == 25
    assert [x["kind"] for x in plan["steps"]] == ["drop_table"] * 9 + ["create_table"] * 16
    assert all("`xunjia_business`." in step["sql"] for step in plan["steps"])
    assert all(not re.match(r"(?:DROP DATABASE|INSERT|UPDATE|TRUNCATE|DELETE)\b", step["sql"], re.I) for step in plan["steps"])
    assert plan["preserve_history"] is False


def test_views_removed_before_tables_and_new_database_created_when_missing():
    before = snapshot()
    before["objects"].append({"name": "old_quote_view", "type": "VIEW"})
    assert fresh.plan_fresh(before)["steps"][0]["kind"] == "drop_view"
    before.update(database_exists=False, objects=[])
    plan = fresh.plan_fresh(before)
    assert plan["steps"][0]["kind"] == "create_database"
    assert len(plan["steps"]) == 17


@pytest.mark.parametrize("key,value", [
    ("external_references", [{"schema": "deposit", "table": "foreign_owner"}]),
    ("routines", ["old_procedure"]), ("events", ["old_schedule"]),
    ("objects", [{"name": "bad;table", "type": "BASE TABLE"}]),
    ("objects", [{"name": "old_sequence", "type": "SEQUENCE"}]),
])
def test_unsafe_objects_rejected_before_any_statement(key, value):
    before = snapshot()
    before[key] = value
    with pytest.raises(fresh.UpgradeError):
        fresh.plan_fresh(before)


@pytest.mark.parametrize("dsn", [
    "mysql://user:secret@db:3306/xunjia_business",
    "oceanbase://user:secret@db:3306/deposit",
    "oceanbase://user:secret@db:3306/xunjia_other",
    "oceanbase://user:secret@db:3306/xunjia_business;DROP",
    "oceanbase://user:secret@db:3306/xunjia_business?database=deposit",
    "oceanbase://user:secret@db:3306/xunjia_business#secret",
    "oceanbase://user@db:3306/xunjia_business",
])
def test_only_exact_oceanbase_database_allowed_without_secret_echo(dsn):
    with pytest.raises(fresh.UpgradeError) as exc:
        fresh.connection_options(dsn)
    assert "secret" not in str(exc.value)


def test_encoded_tenant_credentials_only_in_connection_options():
    options = fresh.connection_options("oceanbase://deposit%40tenant%23cluster:p%40ss%23word@db:3306/xunjia_business")
    assert options["user"] == "deposit@tenant#cluster"
    assert options["password"] == "p@ss#word"
    assert "database" not in options


class Connection:
    def __init__(self, fail_after=None):
        self.executed = []
        self.fail_after = fail_after
        self.enabled = 1
    def cursor(self):
        return self
    def execute(self, sql, args=()):
        self.executed.append(sql)
        if self.fail_after is not None and len(self.executed) == self.fail_after:
            raise TimeoutError("secret must not reach evidence")
    def fetchone(self):
        return {"enabled": self.enabled}
    def close(self):
        pass


def test_each_ddl_durable_and_timeout_unknown_without_retry(tmp_path):
    connection = Connection(fail_after=4)
    plan = fresh.plan_fresh(snapshot())
    with pytest.raises(fresh.UpgradeError, match="DDL_OUTCOME_UNKNOWN"):
        fresh.apply_plan(connection, plan, tmp_path)
    journal = json.loads((tmp_path / "ddl-journal.json").read_text())
    assert [x["status"] for x in journal["steps"]] == ["COMPLETED", "UNKNOWN"]
    assert len([x for x in connection.executed if x.startswith("DROP")]) == 2
    assert connection.executed[-1] == "SET SESSION FOREIGN_KEY_CHECKS=1"
    assert "secret" not in (tmp_path / "ddl-journal.json").read_text()
    assert (tmp_path / "ddl-journal.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(fresh.UpgradeError, match="DDL_JOURNAL_ALREADY_EXISTS"):
        fresh.apply_plan(connection, plan, tmp_path)


def test_modified_sql_plan_cannot_clear_another_database(tmp_path):
    plan = fresh.plan_fresh(snapshot())
    plan["steps"][0]["sql"] = "DROP TABLE `deposit`.`quotes`"
    connection = Connection()
    with pytest.raises(fresh.UpgradeError, match="PLAN_CHANGED"):
        fresh.apply_plan(connection, plan, tmp_path)
    assert connection.executed == []


def test_session_fk_setting_restored_to_original_zero(tmp_path):
    connection = Connection()
    connection.enabled = 0
    plan = fresh.plan_fresh({**snapshot(), "objects": []})
    fresh.apply_plan(connection, plan, tmp_path)
    assert connection.executed[-1] == "SET SESSION FOREIGN_KEY_CHECKS=0"
    assert all("GLOBAL" not in x for x in connection.executed)
    assert all(x["status"] == "COMPLETED" for x in json.loads((tmp_path / "ddl-journal.json").read_text())["steps"])


@pytest.mark.parametrize("output,code", [
    ("LoadState=loaded\nActiveState=active\nMainPID=45\n", 0),
    ("LoadState=loaded\nActiveState=inactive\nMainPID=45\n", 0),
    ("", 1), ("LoadState=loaded\nActiveState=activating\nMainPID=0\n", 0),
])
def test_active_or_unverified_service_not_treated_as_stopped(monkeypatch, output, code):
    monkeypatch.setattr(fresh.shutil, "which", lambda name: "/bin/systemctl")
    monkeypatch.setattr(fresh.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stdout=output))
    with pytest.raises(fresh.UpgradeError):
        fresh.require_service_stopped("xunjia-agent.service")


def test_stopped_service_proven_without_starting_or_stopping(monkeypatch):
    calls = []
    monkeypatch.setattr(fresh.shutil, "which", lambda name: "/bin/systemctl")
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=inactive\nMainPID=0\n")
    monkeypatch.setattr(fresh.subprocess, "run", run)
    fresh.require_service_stopped("xunjia-agent.service")
    assert len(calls) == 1 and calls[0][1] == "show"


def test_other_service_cannot_be_offline_proof():
    with pytest.raises(fresh.UpgradeError, match="OTHER_SERVICE_FORBIDDEN"):
        fresh.require_service_stopped("njcb-all.service")


class MetadataConnection:
    """Small stateful DB protocol double, with real product schema_health."""
    def __init__(self, *, fail_drop=False, version="4.3.5.0 OceanBase_CE", mode="MYSQL"):
        self.tables = {row["name"] for row in snapshot()["objects"]}
        self.views = set()
        self.missing_quote_column = None
        self.dictionary_count = 0
        self.selected = None
        self.executed = []
        self.fail_drop, self.version, self.mode = fail_drop, version, mode
        self.closed = False
    def cursor(self):
        outer = self
        class Cursor:
            def execute(self, sql, args=()):
                from server.health import contract
                spec = contract(True)
                outer.executed.append(sql)
                self.rows = []
                if sql == "SELECT VERSION() AS version":
                    self.rows = [{"version": outer.version}]
                elif sql.startswith("SHOW VARIABLES"):
                    self.rows = [{"Variable_name": "ob_compatibility_mode", "Value": outer.mode}]
                elif "information_schema.SCHEMATA" in sql:
                    self.rows = [{"SCHEMA_NAME": fresh.DATABASE}]
                elif sql == "SELECT DATABASE() AS selected":
                    self.rows = [{"selected": outer.selected}]
                elif "information_schema.TABLES" in sql:
                    self.rows = [{"TABLE_NAME": name, "TABLE_TYPE": "BASE TABLE"} for name in sorted(outer.tables)]
                    self.rows += [{"TABLE_NAME": name, "TABLE_TYPE": "VIEW"} for name in sorted(outer.views)]
                elif sql == "SELECT @@SESSION.FOREIGN_KEY_CHECKS AS enabled":
                    self.rows = [{"enabled": 1}]
                elif sql.startswith("DROP TABLE"):
                    if outer.fail_drop:
                        raise TimeoutError("do not expose private connection data")
                    outer.tables.remove(re.findall(r"`([^`]+)`", sql)[-1])
                elif sql.startswith("CREATE TABLE"):
                    outer.tables.add(re.findall(r"`([^`]+)`", sql)[1])
                elif sql.startswith("CREATE OR REPLACE VIEW"):
                    outer.views.add(re.findall(r"`([^`]+)`", sql)[1])
                elif sql.startswith("SELECT COUNT(*) AS n"):
                    self.rows = [{"n": outer.dictionary_count}]
                elif sql == "SHOW TABLES":
                    self.rows = [{"Tables_in_xunjia_business": name} for name in sorted(outer.tables)]
                elif sql.startswith("SHOW COLUMNS FROM "):
                    if "`" in sql:
                        table = re.findall(r"`([^`]+)`", sql)[-1]
                        self.rows = [{"Field": key, "Type": "unused"} for key in fresh.quote_columns()[table]
                                     if key != outer.missing_quote_column]
                    else:
                        table = sql.split()[-1]
                        self.rows = [{"Field": key, "Type": typ} for key, typ in spec[table]["columns"].items()]
                elif sql.startswith("SHOW INDEX FROM "):
                    table = sql.split()[-1]
                    for n, columns in enumerate(spec[table]["unique"]):
                        self.rows.extend({"Key_name": "unique_" + str(n), "Seq_in_index": i + 1,
                                          "Column_name": col, "Non_unique": 0} for i, col in enumerate(columns))
                    for name, columns in spec[table]["secondary"].items():
                        self.rows.extend({"Key_name": name, "Seq_in_index": i + 1,
                                          "Column_name": col, "Non_unique": 1} for i, col in enumerate(columns))
            def fetchone(self):
                return self.rows[0] if self.rows else None
            def fetchall(self):
                return self.rows
            def close(self):
                pass
        return Cursor()
    def select_db(self, name):
        self.selected = name
    def close(self):
        self.closed = True


VALUES = {"XUNJIA_DATABASE_URL": "oceanbase://site:p%40ss@db:3306/xunjia_business"}


def test_check_mode_only_reads_and_retains_old_objects(tmp_path):
    db = MetadataConnection()
    result = fresh.run_fresh(VALUES, tmp_path / "check", connect=lambda **kw: db)
    assert result["status"] == "CHECK_ONLY" and result["ddl_executed"] == 0
    assert db.tables == {row["name"] for row in snapshot()["objects"]}
    assert all(not re.match(r"(?:DROP|CREATE|ALTER|SET)\b", sql) for sql in db.executed)
    assert db.closed


def test_complete_fresh_flow_uses_real_schema_health_and_clears_old_tables(tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(fresh, "require_service_stopped", lambda service: stopped.append(service))
    db = MetadataConnection()
    result = fresh.run_fresh(VALUES, tmp_path / "apply", apply=True, connect=lambda **kw: db)
    assert result == {"status": "FRESH_SCHEMA_VERIFIED", "database": "xunjia_business",
                      "ddl_executed": 25, "tables": 16, "history_preserved": False}
    assert db.tables == set(fresh.TABLES)
    assert len(stopped) == 2
    assert json.loads((tmp_path / "apply/schema-health.json").read_text())["ready"]
    assert db.closed


def test_unknown_persists_across_new_evidence_directory_and_cannot_reapply(tmp_path, monkeypatch):
    monkeypatch.setattr(fresh, "require_service_stopped", lambda service: None)
    connections = []
    def connect(**kwargs):
        db = MetadataConnection(fail_drop=True)
        connections.append(db)
        return db
    with pytest.raises(fresh.UpgradeError, match="DDL_OUTCOME_UNKNOWN"):
        fresh.run_fresh(VALUES, tmp_path / "first", apply=True, connect=connect)
    before = (tmp_path / "first/RESULT.json").read_bytes()
    with pytest.raises(fresh.UpgradeError, match="PREVIOUS_DDL_REQUIRES_READONLY_RECONCILIATION"):
        fresh.run_fresh(VALUES, tmp_path / "second", apply=True, connect=connect)
    assert len(connections) == 1 and not (tmp_path / "second").exists()
    assert (tmp_path / "first/RESULT.json").read_bytes() == before
    assert all("p@ss" not in p.read_text() for p in tmp_path.rglob("*.json"))


def test_existing_evidence_is_never_overwritten(tmp_path):
    evidence = tmp_path / "existing"
    evidence.mkdir()
    (evidence / "RESULT.json").write_text('{"keep":true}')
    with pytest.raises(fresh.UpgradeError, match="FRESH_EVIDENCE_DIRECTORY_REQUIRED"):
        fresh.run_fresh(VALUES, evidence, connect=lambda **kw: pytest.fail("must not connect"))
    assert (evidence / "RESULT.json").read_text() == '{"keep":true}'


@pytest.mark.parametrize("version,mode", [("8.0.0 MySQL", "MYSQL"), ("4.3 OceanBase", "ORACLE")])
def test_other_database_engines_rejected_before_ddl(tmp_path, monkeypatch, version, mode):
    monkeypatch.setattr(fresh, "require_service_stopped", lambda service: None)
    db = MetadataConnection(version=version, mode=mode)
    with pytest.raises(fresh.UpgradeError, match="REAL_OCEANBASE_MYSQL_MODE_REQUIRED"):
        fresh.run_fresh(VALUES, tmp_path / "wrong-engine", apply=True, connect=lambda **kw: db)
    assert all(not re.match(r"(?:DROP|CREATE|ALTER|SET)\b", sql) for sql in db.executed)
    assert db.closed


QB_VALUES = {**VALUES, "XUNJIA_QUOTE_SOURCE": "qb", "QB_MYSQL_HOST": "db", "QB_MYSQL_PORT": "3306",
             "QB_MYSQL_USER": "site", "QB_MYSQL_PASSWORD": "p@ss", "QB_MYSQL_DB": "xunjia_business"}


def test_qb_config_must_name_the_same_explicit_database_endpoint_and_credentials():
    assert fresh.quote_enabled(QB_VALUES, fresh.connection_options(VALUES["XUNJIA_DATABASE_URL"]))
    assert not fresh.quote_enabled(VALUES, fresh.connection_options(VALUES["XUNJIA_DATABASE_URL"]))


@pytest.mark.parametrize("key,value", [
    ("QB_MYSQL_HOST", "other-db"), ("QB_MYSQL_PORT", "2881"),
    ("QB_MYSQL_USER", "other-user"), ("QB_MYSQL_PASSWORD", "other-secret"),
    ("QB_MYSQL_DB", "deposit"), ("QB_MYSQL_DB", ""),
])
def test_qb_mismatch_rejected_before_any_connection(tmp_path, key, value):
    values = {**QB_VALUES, key: value}
    with pytest.raises(fresh.UpgradeError, match="QUOTE_DATABASE_MUST_MATCH_BUSINESS"):
        fresh.run_fresh(values, tmp_path / "wrong-qb", connect=lambda **kw: pytest.fail("must not connect"))


def test_qb_schema_uses_current_fixed_four_tables_and_one_view():
    plan = fresh.plan_fresh(snapshot(), quote_enabled=True)
    assert len(plan["steps"]) == 30
    extras = plan["steps"][-5:]
    assert [x["kind"] for x in extras] == ["create_table"] * 4 + ["create_view"]
    assert {x["object"] for x in extras} == set(fresh.QB_TABLES) | {fresh.QB_VIEW}
    for step in extras:
        assert "`xunjia_business`." in step["sql"]
        assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|GRANT)\b", step["sql"], re.I)
    view = extras[-1]["sql"]
    assert "FROM `xunjia_business`.`qb_live_best_quote`" in view
    assert "JOIN `xunjia_business`.`qb_bond_dict`" in view


def test_qb_ddl_rejects_additional_statements_or_external_reference(monkeypatch):
    real = fresh.APP
    sql = (real / "collector/schema_qb_live.sql").read_text()
    with pytest.raises(fresh.UpgradeError, match="QUOTE_SCHEMA_SOURCE_INVALID"):
        fresh.quote_statements(sql + "; DROP TABLE deposit.quotes;")
    with pytest.raises(fresh.UpgradeError, match="QUOTE_SCHEMA_SOURCE_INVALID"):
        fresh.quote_statements(sql.replace("FROM qb_live_best_quote q", "FROM deposit.quotes q"))
    with pytest.raises(fresh.UpgradeError, match="QUOTE_SCHEMA_SOURCE_INVALID"):
        fresh.quote_statements(sql.replace("    cnbd_date       DATE NULL COMMENT '来源明确的估值日，未知留空',\n", ""))


def test_qb_addition_never_changes_non_qb_sixteen_table_contract():
    plain = fresh.plan_fresh({**snapshot(), "objects": []})
    quoted = fresh.plan_fresh({**snapshot(), "objects": []}, quote_enabled=True)
    assert plain["steps"] == quoted["steps"][:16]


def test_complete_qb_fresh_schema_is_twenty_tables_one_view_but_dictionary_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(fresh, "require_service_stopped", lambda service: None)
    db = MetadataConnection()
    result = fresh.run_fresh(QB_VALUES, tmp_path / "qb-apply", apply=True, connect=lambda **kw: db)
    assert result["status"] == "FRESH_SCHEMA_VERIFIED"
    assert result["tables"] == 20 and result["views"] == 1 and result["ddl_executed"] == 30
    assert result["quote_schema_ready"] is True and result["dictionary_ready"] is False
    assert result["quote_status"] == "REAL_DICTIONARY_IMPORT_REQUIRED"
    assert db.tables == set(fresh.TABLES) | set(fresh.QB_TABLES) and db.views == {fresh.QB_VIEW}
    health = json.loads((tmp_path / "qb-apply/schema-health.json").read_text())
    assert health["ready"] is True and health["quote"]["dictionary_ready"] is False


@pytest.mark.parametrize("missing,count,code", [
    ("cnbd_date", 0, "FRESH_QUOTE_SCHEMA_COLUMNS_MISSING"),
    (None, 1, "FRESH_QUOTE_DICTIONARY_CHANGED_CONCURRENTLY"),
])
def test_qb_final_structure_and_empty_dictionary_are_verified(tmp_path, monkeypatch, missing, count, code):
    monkeypatch.setattr(fresh, "require_service_stopped", lambda service: None)
    db = MetadataConnection()
    db.missing_quote_column, db.dictionary_count = missing, count
    with pytest.raises(fresh.UpgradeError, match=code):
        fresh.run_fresh(QB_VALUES, tmp_path / "qb-failed-verify", apply=True, connect=lambda **kw: db)
    assert json.loads((tmp_path / "qb-failed-verify/RESULT.json").read_text())["status"] == "UNKNOWN"
