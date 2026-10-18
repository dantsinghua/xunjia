"""Real HTTP timeouts exercise production retry persistence and command gates."""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler
import json
from threading import Event

from server.negotiation.application_store import ApplicationStore
from server.negotiation.chatflow import ChatflowClient, Chatflows
from server.negotiation.engine import Engine
from server.negotiation.prices import Price
from server.negotiation.service import Inquiry
from test_chatflow_redirect_security import server
from test_production_flow import OWNER, desk, raw
from test_acceptance_contract import current, setup_groups


@contextmanager
def flaky_http(fail_stage):
    calls, release = [], Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            context = json.loads(payload["inputs"]["context_json"])
            stage = context["stage"]
            calls.append({"stage": stage, "context": deepcopy(context)})
            first = sum(row["stage"] == stage for row in calls) == 1
            if stage == fail_stage and first:
                # The client times out against a real owned socket. No sleep or
                # monkeypatch of the production client/engine simulates failure.
                release.wait(timeout=5)
            if stage == "analysis":
                observations, scopes = [], {}
                for msg in context["messages"]:
                    hits = [p for p in context["members"] if p["bond_code"] in msg["text"]]
                    scopes[msg["message_id"]] = [p["member_id"] for p in hits]
                    if msg["direction"] != "in":
                        continue
                    for member in hits:
                        price, volume = ("3.11", "600") if "3.11" in msg["text"] else ("3.10", "500")
                        changes = {"price": Price("YIELD", price).dump(), "volume_wan": volume,
                                   "confirmed_day": "2026-10-17", "validity": "ACTIVE", "finality": "NEGOTIABLE"}
                        observations.append({"member_id": member["member_id"], "side": "ofr", "changes": changes,
                                             "evidence": {key: [msg["message_id"]] for key in changes}})
                value = {"schema_version": "2.2", "scopes": scopes, "observations": observations, "unresolved": {}}
            else:
                volume = context["quotes"]["ofr"]["volume_wan"]
                value = {"schema_version": "2.2", "action": "SEND", "proposal": {
                    "kind": "REPLY", "text": "3.19%收" + volume + "万", "our_price": Price("YIELD", "3.19").dump()}}
            data = json.dumps({"answer": json.dumps(value), "message_id": "synthetic-http-" + str(len(calls))}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    with server(Handler) as endpoint:
        base = "http://127.0.0.1:" + str(endpoint.server_port)
        flows = Chatflows(ChatflowClient(base, "synthetic-retry-key", stage="analysis", timeout=1),
                          ChatflowClient(base, "synthetic-retry-key", stage="reply", timeout=1))
        try:
            yield flows, calls, release
        finally:
            release.set()


def latest_row(store, table):
    assert table in ("xj2_analysis", "xj2_reply_job")
    order = "through_revision DESC" if table == "xj2_analysis" else "created_at DESC"
    with store.db.transaction(write=False) as tx:
        return dict(tx.execute("SELECT * FROM " + table + " ORDER BY " + order + " LIMIT 1").fetchone())


def restart_engine(app, flows, clock, tmp_path):
    store = ApplicationStore("sqlite:///" + str(tmp_path / "current.db"), clock=clock)
    service = Inquiry(store)
    old = app.state.negotiation_engine
    recovered = Engine(service, flows, app.state.bond_tools, aggregation=old.aggregation)
    app.state.negotiation_engine = recovered
    return recovered


def reply_commands(app):
    return [c for c in app.state.inquiry.commands(OWNER) if json.loads(c["body_json"])["kind"] == "REPLY"]


def test_a_http_timeout_recovers_same_snapshot_after_restart_without_duplicate_facts(desk, tmp_path):
    app, client, _, transport, clock = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    first = raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    with flaky_http("analysis") as (flows, calls, release):
        engine = app.state.negotiation_engine
        engine.client = flows
        result = engine.analyze_thread(OWNER, peer["thread_id"])
        release.set()
        assert result["facts_committed"] is False
        assert flows.analysis.last_evidence["error_code"] == "DIFY_TIMEOUT"
        job = latest_row(app.state.repository, "xj2_analysis")
        assert job["state"] == "RETRY" and job["attempts"] == 1
        assert current(app, peer).revision == 0
        message = next(m for m in client.get("/api/negotiation/messages/" + peer["thread_id"]).json()["data"]["messages"] if m["message_id"] == first)
        assert message["analysis_state"] == "QUEUED"
        assert engine.analyze_thread(OWNER, peer["thread_id"]) is None
        assert len(calls) == 1
        later = raw(app, peer["thread_id"], "SYN-A 3.11出600万", seq=2)
        engine = restart_engine(app, flows, clock, tmp_path)
        clock.value = job["lease_until"] + 0.01
        saved = engine.analyze_thread(OWNER, peer["thread_id"])
        assert saved["facts_committed"] and not saved["rejected"]
        recovered = latest_row(app.state.repository, "xj2_analysis")
        assert recovered["analysis_id"] == job["analysis_id"] and recovered["attempts"] == 2
        assert recovered["lease_token"] != job["lease_token"]
        assert calls[0]["context"]["messages"] == calls[1]["context"]["messages"]
        assert later not in {m["message_id"] for m in calls[1]["context"]["messages"]}
        assert current(app, peer).revision == 1 and current(app, peer).price.value == "3.10"
        assert app.state.repository.snapshot(OWNER)["threads"][0]["analysis_pending"]
        saved = engine.analyze_thread(OWNER, peer["thread_id"])
        assert saved["facts_committed"] and not saved["rejected"]
        assert current(app, peer).revision == 2 and current(app, peer).volume_wan == "600"
        assert engine.analyze_thread(OWNER, peer["thread_id"]) is None
        queued = engine.decide_member(OWNER, peer["member_id"])
        assert queued["state"] == "QUEUED"
        assert engine.decide_member(OWNER, peer["member_id"]) is None
        app.state.dispatcher.run_once()
        app.state.dispatcher.run_once()
        assert len(reply_commands(app)) == 1 and len(transport.sent) == 2


def test_b_http_timeout_preserves_a_revision_and_regenerates_one_native_command(desk, tmp_path):
    app, _, _, transport, clock = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    with flaky_http("reply") as (flows, calls, release):
        engine = app.state.negotiation_engine
        engine.client = flows
        engine.run_once()
        release.set()
        assert flows.reply.last_evidence["error_code"] == "DIFY_TIMEOUT"
        fact = current(app, peer).dump()
        assert fact["revision"] == 1 and fact["price"]["value"] == "3.10"
        job = latest_row(app.state.repository, "xj2_reply_job")
        assert job["state"] == "RETRY" and job["attempts"] == 1
        assert not reply_commands(app) and len(transport.sent) == 1
        assert engine.decide_member(OWNER, peer["member_id"]) is None
        assert len(calls) == 2
        engine = restart_engine(app, flows, clock, tmp_path)
        clock.value = job["lease_until"] + 0.01
        result = engine.decide_member(OWNER, peer["member_id"])
        assert result["state"] == "QUEUED"
        recovered = latest_row(app.state.repository, "xj2_reply_job")
        assert recovered["job_id"] == job["job_id"] and recovered["attempts"] == 2
        assert recovered["lease_token"] != job["lease_token"]
        assert current(app, peer).dump() == fact
        assert [row["stage"] for row in calls] == ["analysis", "reply", "reply"]
        assert engine.decide_member(OWNER, peer["member_id"]) is None
        app.state.dispatcher.run_once()
        app.state.dispatcher.run_once()
        assert len(reply_commands(app)) == 1 and len(transport.sent) == 2
        assert current(app, peer).dump() == fact


def test_stop_during_reply_backoff_prevents_retry_send_but_keeps_new_analysis(desk):
    app, client, _, transport, clock = desk
    peer = setup_groups(desk, stopped=False)["SYN-A"]["peers"][0]
    raw(app, peer["thread_id"], "SYN-A 3.10出500万")
    with flaky_http("reply") as (flows, calls, release):
        engine = app.state.negotiation_engine
        engine.client = flows
        engine.run_once()
        release.set()
        assert latest_row(app.state.repository, "xj2_reply_job")["state"] == "RETRY"
        assert client.post("/api/bot/toggle", json={"status": "停止"}).status_code == 200
        clock.value += 6
        raw(app, peer["thread_id"], "SYN-A 3.11出600万", seq=2)
        engine.run_once()
        app.state.dispatcher.run_once()
        assert current(app, peer).price.value == "3.11" and current(app, peer).volume_wan == "600"
        assert [row["stage"] for row in calls] == ["analysis", "reply", "analysis"]
        assert not reply_commands(app) and len(transport.sent) == 1
