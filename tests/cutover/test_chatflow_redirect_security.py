"""Dify app credentials remain at the explicitly configured HTTP origin.

Both HTTP servers are owned loopback fixtures and the key is synthetic.
The production urllib opener and production ChatflowClient execute unchanged.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from server.negotiation.chatflow import ChatflowClient, FlowUnavailable


ANSWER = {"schema_version": "2.2", "scopes": {}, "observations": [], "unresolved": {}}


def respond(handler):
    body = json.dumps({"answer": json.dumps(ANSWER), "message_id": "synthetic-message",
                       "workflow_run_id": "synthetic-workflow"}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@contextmanager
def server(handler):
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield http
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "Owned loopback fixture thread did not stop"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_cross_origin_redirect_is_rejected_without_forwarding_app_key(status):
    target_authorized, source_authorized = [], []
    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            target_authorized.append(bool(self.headers.get("Authorization")))
            respond(self)
        do_POST = do_GET
        def log_message(self, *args):
            pass
    with server(Target) as target:
        class Source(BaseHTTPRequestHandler):
            def do_POST(self):
                source_authorized.append(self.headers.get("Authorization") == "Bearer synthetic-localhost-app-key")
                self.send_response(status)
                self.send_header("Location", "http://127.0.0.1:" + str(target.server_port) + "/redirect-target")
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        with server(Source) as source:
            client = ChatflowClient("http://127.0.0.1:" + str(source.server_port), "synthetic-localhost-app-key",
                                    stage="analysis", timeout=3)
            error = None
            try:
                client.call({"thread_id": "synthetic-thread", "messages": []})
            except FlowUnavailable as caught:
                error = caught
            assert source_authorized == [True]
            assert target_authorized == [], "App Authorization must never be sent to a redirected origin"
            assert error is not None, "Redirect rejection must be an observable Chatflow failure"
            assert error.status == status
            assert client.last_evidence["stage"] == "analysis"
            assert client.last_evidence["error_code"] == error.code
            assert client.last_evidence["http_status"] == status


def test_direct_200_keeps_authenticated_stage_contract_and_run_evidence():
    authorized = []
    class Direct(BaseHTTPRequestHandler):
        def do_POST(self):
            authorized.append(self.headers.get("Authorization") == "Bearer synthetic-localhost-app-key")
            respond(self)
        def log_message(self, *args):
            pass
    with server(Direct) as endpoint:
        client = ChatflowClient("http://127.0.0.1:" + str(endpoint.server_port), "synthetic-localhost-app-key",
                                stage="analysis", timeout=3)
        assert client.call({"thread_id": "synthetic-thread", "messages": []}) == ANSWER
        assert authorized == [True]
        assert client.last_evidence["message_id"] == "synthetic-message"
        assert client.last_evidence["workflow_run_id"] == "synthetic-workflow"
        assert "error_code" not in client.last_evidence
