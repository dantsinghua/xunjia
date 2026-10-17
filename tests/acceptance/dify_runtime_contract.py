"""Explicit integration gate for the newly created isolated Dify 1.17.1 project."""
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import urllib.request

from server.dify_client import DifyChatClient


ENVIRONMENT = Path("/tmp/xunjia-dify-verify-20261013")
PROJECT = "xunjia-dify-verify-20261013"


def container_identity(name):
    row = json.loads(subprocess.check_output(["docker", "inspect", name], text=True, timeout=30))[0]
    labels = row["Config"]["Labels"]
    assert labels["com.docker.compose.project"] == PROJECT
    assert row["State"]["Running"] is True
    return {"id": row["Id"], "started_at": row["State"]["StartedAt"], "image": row["Image"], "ports": row["NetworkSettings"]["Ports"]}


def nested_gaps(value):
    found = []
    if isinstance(value, dict):
        if "raw_gap_bp" in value:
            try:
                found.append(Decimal(str(value["raw_gap_bp"])))
            except InvalidOperation:
                pass
        for child in value.values():
            found.extend(nested_gaps(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(nested_gaps(child))
    elif isinstance(value, str):
        value = value.removeprefix("tool response: ")
        if value[:1] not in ("{", "["):
            return found
        try:
            # The plugin's trace concatenates its text and JSON result blocks
            # as "tool response: {json}.{json}"; parse the first complete JSON.
            decoded, _ = json.JSONDecoder().raw_decode(value)
        except ValueError:
            return found
        found.extend(nested_gaps(decoded))
    return found


def test_isolated_dify_performs_real_tool_calls_and_uses_new_inputs_on_second_turn(allow_owned_loopback_endpoint):
    before = {name: container_identity(PROJECT + "-" + name + "-1") for name in ("api", "fixture")}
    bindings = before["api"]["ports"]["5001/tcp"]
    assert bindings == [{"HostIp": "127.0.0.1", "HostPort": "32771"}]
    allow_owned_loopback_endpoint("127.0.0.1", 32771)
    key_path = ENVIRONMENT / "app-key.json"
    assert key_path.stat().st_mode & 0o077 == 0
    key = json.loads(key_path.read_text())["token"]
    evidence = Path(tempfile.mkdtemp(prefix="xunjia-dify-independent-"))
    events_path = ENVIRONMENT / "fixture-events.jsonl"
    start = events_path.stat().st_size
    raw_chunks = []
    safe_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class Recorded:
        def __init__(self, response):
            self.response, self.status, self.headers = response, response.status, response.headers

        def read1(self, size):
            block = self.response.read1(size)
            raw_chunks.append(block)
            return block

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.response.close()

    def opener(request, timeout):
        assert request.full_url == "http://127.0.0.1:32771/v1/chat-messages"
        return Recorded(safe_opener.open(request, timeout=timeout))

    client = DifyChatClient("http://127.0.0.1:32771", key, opener=opener, timeout=90, total_timeout=180)
    conversation, turns = None, []
    user = "independent-acceptance-" + secrets.token_hex(10)
    for number in (1, 2):
        raw_chunks.clear()
        result = client.chat(query="独立验收：先调用债券计算工具核对价差，再询问可做数量。", user=user, conversation_id=conversation,
                             inputs={"schema_version": "1", "context_epoch": str(number), "turn_ref": "verify-turn-ref-" + str(number),
                                     "service_capability": "fixture-capability-only"})
        raw = b"".join(raw_chunks)
        target = evidence / f"turn-{number}.sse"
        target.write_bytes(raw)
        target.chmod(0o600)
        proposal = result.proposal
        assert proposal["turn_id"] == "verify-turn-" + str(number)
        assert proposal["source_message_id"] == "verify-message-" + str(number)
        assert proposal["conversation_epoch"] == number
        assert proposal["public_message"] == "请问这只券目前可做多少量？"
        assert proposal["validation_ref"] == "fixture-validated-" + str(number)
        if conversation is not None:
            assert result.conversation_id == conversation
        conversation = result.conversation_id
        parsed = []
        for block in raw.decode().split("\n\n"):
            if block.startswith("data: "):
                try:
                    parsed.append(json.loads(block[6:]))
                except ValueError:
                    pass
        assert Decimal(30) in nested_gaps(parsed), "A real Dify tool result must carry the independently known 30 BP result"
        turns.append({"epoch": number, "conversation_id": conversation, "message_id": result.message_id,
                      "workflow_run_id": result.workflow_run_id, "turn_id": proposal["turn_id"]})
    with events_path.open() as stream:
        stream.seek(start)
        events = [json.loads(line) for line in stream if line.strip()]
    for number in ("1", "2"):
        scoped = [entry["event"] for entry in events if entry.get("turn") == number]
        for name in ("context", "tool", "validate_action", "validate_text"):
            assert name in scoped
    assert not any(entry["event"] in ("fixture_tool_failed", "public_context_leak") for entry in events)
    after = {name: container_identity(PROJECT + "-" + name + "-1") for name in before}
    assert after == before, "An environment restart invalidates the two-turn evidence"
    report = {"project": PROJECT, "environment": before, "turns": turns, "events": events,
              "scope": "Real Dify 1.17.1 graph and tool HTTP; synthetic model and synthetic data only"}
    (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("Independent Dify evidence:", evidence, flush=True)


def test_existing_root_integration_has_real_persisted_validation_and_budget_without_touching_old_containers():
    root_backend = container_identity(PROJECT + "-root_backend-1")
    instance = json.loads((ENVIRONMENT / "root-instance.json").read_text())
    database = ENVIRONMENT / Path(instance["database"]).name
    assert database.name.startswith("root-instance-") and database.suffix == ".sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        turns = [dict(row) for row in connection.execute("SELECT turn_id,state,data_json FROM xj_turn")]
        commands = [dict(row) for row in connection.execute("SELECT command_id,status,body_json FROM xj_command")]
    finally:
        connection.close()
    completed = [row for row in turns if row["state"] == "done" and json.loads(row["data_json"]).get("command_id")]
    assert completed
    proved = []
    for turn in completed:
        state = json.loads(turn["data_json"])
        assert state["tool_calls"] >= 1
        assert state["validation"]["ref"] and state["validation"]["digest"]
        matched = [command for command in commands if command["command_id"] == state["command_id"]]
        assert len(matched) == 1 and matched[0]["status"] == "sent"
        body = json.loads(matched[0]["body_json"])
        assert body["turn_id"] == turn["turn_id"]
        assert body["text"] == "请问这只券目前可做多少量？"
        proved.append({"turn_id": turn["turn_id"], "tool_calls": state["tool_calls"], "command_id": matched[0]["command_id"], "status": "sent"})
    exhausted = [row for row in turns if json.loads(row["data_json"]).get("tool_calls") == 6]
    assert exhausted
    assert all(not any(json.loads(command["body_json"]).get("turn_id") == row["turn_id"] for command in commands) for row in exhausted)
    baseline = json.loads((ENVIRONMENT / "baseline-containers.json").read_text())
    template = '{"id":{{json .Id}},"started_at":{{json .State.StartedAt}},"running":{{json .State.Running}}}'
    current = subprocess.check_output(["docker", "inspect", "--format", template, *[row["id"] for row in baseline]], text=True, timeout=30)
    inspected = {row["id"]: row for row in map(json.loads, current.splitlines())}
    for row in baseline:
        assert inspected[row["id"]]["started_at"] == row["started_at"]
        assert inspected[row["id"]]["running"] is True
    evidence = Path(tempfile.mkdtemp(prefix="xunjia-dify-root-readonly-"))
    (evidence / "result.json").write_text(json.dumps({"root_backend": root_backend, "database": str(database),
        "validated_sent_turns": proved, "budget_six_without_command": [row["turn_id"] for row in exhausted],
        "unchanged_running_baseline_containers": len(baseline)}, indent=2))
    print("Independent root integration evidence:", evidence, flush=True)
