"""采集端通道：多机多账号，以及同一企点号不能两台同时在线。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from imlink.client import probe
from imlink.hmac_sign import auth_headers, canonical_string, sign
from imlink.store import Store


def _passed(uid: str, account_id: str = "qd02") -> dict:
    return {
        "passed": True,
        "note": "",
        "steps": [],
        "account": {"id": account_id, "label": "02", "self_uid": uid, "state": "running"},
        "accounts": [{"id": account_id, "label": "02", "self_uid": uid, "state": "running"}],
        "contacts": [{"uin": "100", "name": "李静", "remark": ""}],
        "contacts_source": "contacts",
        "adopted_account_id": account_id,
    }


class SeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "im_link.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _lan(self, name: str, host: str, account: str) -> str:
        link = self.store.create({
            "name": name,
            "mode": "lan",
            "account_id": account,
            "lan": {"scheme": "http", "host": host, "port": 17600, "bearer_token": "tok-" + name, "callback_url": ""},
        })
        return link["id"]

    def test_same_uid_on_second_machine_kicks_the_first(self):
        b = self._lan("B机", "192.168.1.12", "qd02")
        a = self._lan("A机", "192.168.1.11", "qd02")
        self.store.save_test(b, _passed("30002"))
        self.store.activate(b)
        saved = self.store.save_test(a, _passed("30002"))
        self.assertEqual(saved["last_test"]["displaced"], ["B机"])
        view = self.store.list_public()
        by_name = {x["name"]: x for x in view["links"]}
        self.assertTrue(by_name["A机"]["tested"])
        self.assertFalse(by_name["B机"]["tested"])
        self.assertEqual(by_name["B机"]["kicked_by"], "A机")
        self.assertIn("踢下线", by_name["B机"]["test_note"])
        self.assertEqual(view["active_id"], "")
        with self.assertRaises(ValueError):
            self.store.activate(b)

    def test_different_uids_both_stay_online(self):
        a = self._lan("A机", "192.168.1.11", "qd01")
        b = self._lan("B机", "192.168.1.12", "qd02")
        self.store.save_test(a, _passed("30001", "qd01"))
        self.store.save_test(b, _passed("30002", "qd02"))
        view = {x["name"]: x for x in self.store.list_public()["links"]}
        self.assertTrue(view["A机"]["tested"])
        self.assertTrue(view["B机"]["tested"])
        self.assertEqual(view["A机"]["kicked_by"], "")

    def test_secret_is_kept_and_not_returned(self):
        link_id = self._lan("A机", "192.168.1.11", "qd01")
        again = self.store.update(link_id, {"name": "A机", "mode": "lan", "lan": {"host": "192.168.1.11", "bearer_token": ""}})
        self.assertEqual(again["lan"]["bearer_token"], "tok-A机")
        pub = next(x for x in self.store.list_public()["links"] if x["id"] == link_id)
        self.assertNotIn("tok-A机", json.dumps(pub))
        self.assertTrue(pub["form"]["lan"]["secret_set"])

    def test_blank_token_does_not_invalidate_test(self):
        link_id = self._lan("A机", "192.168.1.11", "qd01")
        self.store.save_test(link_id, _passed("30001", "qd01"))
        self.store.update(link_id, {"lan": {"host": "192.168.1.11", "bearer_token": ""}})
        self.assertTrue(next(x for x in self.store.list_public()["links"] if x["id"] == link_id)["tested"])


class SignTest(unittest.TestCase):
    def test_canonical_matches_collector_rule(self):
        body = b'{"text":"hi"}'
        canonical = canonical_string("POST", "/api/v1/accounts/qd01/send", "", body, "1700000000", "abc123abc123abcd")
        self.assertEqual(
            canonical,
            "POST\n/api/v1/accounts/qd01/send\n\n" + __import__("hashlib").sha256(body).hexdigest() + "\n1700000000\nabc123abc123abcd",
        )
        self.assertEqual(len(sign("secret", canonical)), 64)
        headers = auth_headers("app", "secret", "GET", "/api/v1/system/version", "", None)
        self.assertTrue(headers["X-QT-Signature"].startswith("v1="))
        self.assertEqual(headers["X-QT-AppId"], "app")


class ProbeTest(unittest.TestCase):
    def test_single_account_adopts_and_falls_back_to_sessions(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.setdefault("paths", []).append(self.path)
                auth = self.headers.get("Authorization")
                if self.path.endswith("/system/health"):
                    self._json({"ok": True})
                elif self.path.endswith("/system/version"):
                    if auth != "Bearer tok":
                        self._json({"ok": False, "code": "UNAUTHORIZED"}, 401)
                        return
                    self._json({"ok": True, "api_version": "1.0", "agent": {"version": "t"}})
                elif self.path.startswith("/api/v1/accounts?"):
                    self._json({"ok": True, "data": [{
                        "id": "qd01", "channel": "qidian", "label": "01", "self_uid": "30001", "state": "running",
                    }]})
                elif self.path.endswith("/contacts"):
                    self._json({"ok": False}, 404)
                elif self.path.startswith("/api/v1/sessions"):
                    self._json({"ok": True, "data": [{
                        "id": "qd01:4150", "native_id": "4150", "name": "李静", "kind": "private",
                    }]})
                else:
                    self._json({"ok": False}, 404)

            def _json(self, obj, code=200):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe({
                "mode": "lan",
                "account_id": "",
                "lan": {"scheme": "http", "host": "127.0.0.1", "port": port, "bearer_token": "tok"},
            })
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["adopted_account_id"], "qd01")
        self.assertEqual(result["contacts_source"], "sessions")
        self.assertEqual(result["contacts"][0]["name"], "李静")
        self.assertTrue(any(p.endswith("/contacts") for p in seen["paths"]))


if __name__ == "__main__":
    unittest.main()
