"""Explicit MySQL gate: pytest tests/acceptance/mysql_contract.py.

Creates and removes only its newly created named container. The isolated data
directory and evidence remain under /tmp; no pre-existing server is contacted.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

import pymysql
import pytest

from server.business import InquiryService
from server.chat_runtime import ChatRunner
from server.dify_client import ChatResult
from server.repository import Repository
from server.runtime import Dispatcher


NAME = "xunjia-mysql-verify-20261013"
NOW = 1_800_000_000


def docker(*arguments, timeout=30, check=True):
    return subprocess.run(["docker", *arguments], text=True, capture_output=True, timeout=timeout, check=check)


@pytest.fixture(scope="session")
def mysql_container():
    if docker("inspect", NAME, check=False).returncode == 0:
        raise AssertionError("The requested MySQL container name already exists; ownership is not assumed")
    image = json.loads(docker("image", "inspect", "mysql:8.0").stdout)[0]
    root = Path(tempfile.mkdtemp(prefix="xunjia-mysql-acceptance-"))
    data = root / "data"
    data.mkdir()
    root_password, password, run_id = secrets.token_hex(24), secrets.token_hex(24), secrets.token_hex(12)
    env_file = root / "synthetic.env"
    env_file.write_text(f"MYSQL_ROOT_PASSWORD={root_password}\nMYSQL_ROOT_HOST=%\nMYSQL_DATABASE=xunjia_qa\nMYSQL_USER=xunjia_qa\nMYSQL_PASSWORD={password}\n")
    env_file.chmod(0o600)
    container_id = docker("run", "--detach", "--name", NAME, "--label", "xunjia.qa.run=" + run_id,
                          "--publish", "127.0.0.1::3306", "--env-file", str(env_file),
                          "--mount", f"type=bind,source={data},target=/var/lib/mysql", image["Id"], timeout=60).stdout.strip()
    evidence = {"container_id": container_id, "name": NAME, "run_id": run_id, "image": image["Id"],
                "image_digests": image.get("RepoDigests"), "data_directory": str(data)}
    print("MySQL evidence:", str(root), flush=True)
    try:
        inspected = json.loads(docker("inspect", container_id).stdout)[0]
        assert inspected["Name"] == "/" + NAME
        assert inspected["Config"]["Labels"]["xunjia.qa.run"] == run_id
        ports = inspected["NetworkSettings"]["Ports"]["3306/tcp"]
        assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
        port = int(ports[0]["HostPort"])
        assert port != 3306
        evidence["endpoint"] = {"host": "127.0.0.1", "port": port}
        (root / "container.json").write_text(json.dumps(evidence, indent=2))
        deadline = time.monotonic() + 180
        last = None
        while time.monotonic() < deadline:
            last = docker("exec", container_id, "mysql", "--protocol=TCP", "--host=127.0.0.1", "--user=xunjia_qa",
                          "--password=" + password, "--database=xunjia_qa", "--batch", "--skip-column-names", "-e", "SELECT 1", timeout=10, check=False)
            if last.returncode == 0 and last.stdout.strip() == "1":
                break
            time.sleep(0.5)
        else:
            raise AssertionError("New isolated MySQL did not become ready: " + str(last.stderr if last else "no response"))
        yield {"host": "127.0.0.1", "port": port, "password": password, "root_password": root_password, "evidence": root}
    finally:
        current = json.loads(docker("inspect", container_id).stdout)[0]
        assert current["Config"]["Labels"]["xunjia.qa.run"] == run_id
        (root / "mysql.log").write_text(docker("logs", container_id, check=False).stdout)
        docker("stop", "--time", "20", container_id, timeout=30)
        docker("rm", container_id)
        evidence["removed_container"] = True
        (root / "container.json").write_text(json.dumps(evidence, indent=2))


@pytest.fixture
def mysql_repo(mysql_container, allow_owned_loopback_endpoint):
    config = mysql_container
    allow_owned_loopback_endpoint(config["host"], config["port"])
    database = "xj_qa_" + secrets.token_hex(8)
    admin = pymysql.connect(host=config["host"], port=config["port"], user="root", password=config["root_password"], autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            cursor.execute(f"GRANT ALL ON `{database}`.* TO 'xunjia_qa'@'%'")
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
    finally:
        admin.close()
    url = f"mysql://xunjia_qa:{quote(config['password'])}@{config['host']}:{config['port']}/{database}"
    repo = Repository(url)
    repo.initialize_schema()
    with (config["evidence"] / "databases.jsonl").open("a") as log:
        log.write(json.dumps({"database": database, "version": version}) + "\n")
    return repo, url


def authorized(repo):
    service = InquiryService(repo, clock=lambda: NOW)
    repo.bind_account("trader", "910001", "synthetic-wb")
    draft = service.preview("trader", "preview", [{"code": "000100.IB", "name": "合成债", "peer_ids": ["peer"],
                           "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50", "limit_yield_pct": "2.40", "step_bp": "2"}])
    return service, draft


class Transport:
    def __init__(self):
        self.sends = []
        self.lock = threading.Lock()

    def send(self, command):
        with self.lock:
            self.sends.append(command["command_id"])
        return {key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "sent", "evidence": "native-test-evidence"}

    def reconcile(self, command):
        return None

    def cancel(self, command):
        return {key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "cancelled", "evidence": "cancel-test-evidence"}


def parallel_five(operation):
    barrier = threading.Barrier(5)

    def call(_):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=5) as pool:
        return list(pool.map(call, range(5)))


def test_mysql_explicit_schema_and_read_committed_connection(mysql_repo):
    repo, _ = mysql_repo
    with repo.db.transaction(write=False) as tx:
        isolation = tx.execute("SELECT @@transaction_isolation AS isolation_level").fetchone()["isolation_level"]
        assert isolation == "READ-COMMITTED"
        tables = tx.execute("SHOW TABLES").fetchall()
    assert {"xj_binding", "xj_batch", "xj_command", "xj_message", "xj_turn"} <= {next(iter(row.values())) for row in tables}


def test_mysql_rollback_does_not_leave_half_written_business_state(mysql_repo):
    repo, _ = mysql_repo
    with pytest.raises(OSError):
        with repo.db.transaction() as tx:
            tx.execute("INSERT INTO xj_audit(audit_id,owner_user_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)",
                       ("rollback-event", "trader", "test", "entity", NOW, "{}"))
            raise OSError("synthetic transaction failure")
    with repo.db.transaction(write=False) as tx:
        assert tx.execute("SELECT audit_id FROM xj_audit WHERE audit_id=?", ("rollback-event",)).fetchone() is None


def test_mysql_account_binding_is_unique_under_concurrent_creation(mysql_repo):
    repo, url = mysql_repo
    counter, lock = [0], threading.Lock()

    def bind():
        with lock:
            counter[0] += 1
            owner = "trader-" + str(counter[0])
        try:
            return Repository(url).bind_account(owner, "910001", "synthetic-wb")
        except ValueError:
            return None

    assert sum(result is not None for result in parallel_five(bind)) == 1


def test_mysql_five_concurrent_confirmations_have_one_command(mysql_repo):
    repo, url = mysql_repo
    service, draft = authorized(repo)
    results = parallel_five(lambda: InquiryService(Repository(url), clock=lambda: NOW).confirm("trader", draft["batch_id"], draft["version"], "same-confirm"))
    assert len({result["commands"][0]["command_id"] for result in results}) == 1
    assert len(service.commands("trader")) == 1


def test_mysql_five_independent_dispatchers_do_not_read_a_stale_claim_snapshot(mysql_repo):
    repo, url = mysql_repo
    service, draft = authorized(repo)
    service.confirm("trader", draft["batch_id"], draft["version"], "confirm")
    transport = Transport()
    parallel_five(lambda: Dispatcher(InquiryService(Repository(url), clock=lambda: NOW), transport, clock=lambda: NOW).run_once())
    assert len(transport.sends) == 1
    assert service.commands("trader")[0]["status"] == "sent"


def test_mysql_duplicate_message_and_turn_claims_remain_unique(mysql_repo):
    repo, url = mysql_repo
    service, draft = authorized(repo)
    service.confirm("trader", draft["batch_id"], draft["version"], "confirm")
    Dispatcher(service, Transport(), clock=lambda: NOW).run_once()
    event = {"event_id": "same-incoming", "account_uid": "910001", "peer_id": "peer", "kind": "private",
             "source_seq": 1, "text": "谢谢", "occurred_at": NOW, "historical": False}
    received = parallel_five(lambda: InquiryService(Repository(url), clock=lambda: NOW).ingest("trader", event))
    assert len({result["message_id"] for result in received}) == 1
    calls, lock = [], threading.Lock()

    def process():
        local = InquiryService(Repository(url), clock=lambda: NOW)

        class Agent:
            def chat(self, **kwargs):
                with lock:
                    calls.append(kwargs)
                ref = kwargs["inputs"]["turn_ref"]
                ctx = runner.context(ref)
                value = {"schema_version": "1", "turn_id": ctx["turn_id"], "conversation_epoch": ctx["conversation_epoch"],
                         "source_message_id": ctx["source_message_id"], "observations": [], "proposed_actions": [{"type": "SOCIAL_REPLY"}],
                         "public_message": "不客气。", "trader_view": {}, "memory_patch": None, "history_request": None, "validation_ref": None}
                value["validation_ref"] = runner.validate(ref, value)["validation_ref"]
                return ChatResult(value, "mysql-test-conversation", "mysql-test-message", "mysql-test-task", "mysql-test-run")

        runner = ChatRunner(local, Agent(), clock=lambda: NOW)
        return runner.process(received[0]["message_id"])

    parallel_five(process)
    assert len(calls) == 1
    assert len(service.commands("trader")) == 2
