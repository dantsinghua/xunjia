"""人工介入必须走可注入的下行，目录在线不能当成已送达。

不访问网络，不连接对象存储，也不连接 17650。

在仓库根目录运行::

    .venv/bin/python -m pytest tests/test_oss_downlink.py -q
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from imlink.commands import CommandLog, install_downlink, prepare_send


class DownlinkReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = CommandLog(Path(self.tmp.name) / "oss_commands.json")
        install_downlink(None)

    def tearDown(self):
        install_downlink(None)
        self.tmp.cleanup()

    def _send(self, text: str, *, key: str, downlink=None, allow_write: bool = True) -> dict:
        kwargs = dict(
            allow_write=allow_write,
            workbench_id="anlin-dev",
            account_uid="3007378246",
            native_id="415011447",
            idempotency_key=key,
        )
        if downlink is not None:
            kwargs["downlink"] = downlink
        return prepare_send(self.log, text, **kwargs)

    def test_submit_without_downlink_is_not_delivered(self):
        body = "hello"
        got = self._send(body, key="no-downlink")
        self.assertTrue(got["ok"], got)
        cmd = got["command"]
        self.assertEqual(cmd["status"], "submitted")
        self.assertNotEqual(cmd["status"], "delivered")
        view = self.log.pull_receipt(cmd["cmd_id"], stale=False, present=True)
        self.assertNotEqual(view["status"], "delivered")
        self.assertEqual(view["status"], "submitted")
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(body, dumped)
        self.assertNotIn("text", view)

    def test_delivered_only_after_injected_downlink_succeeds(self):
        body = "hello"
        calls = []

        def send(cmd):
            calls.append(dict(cmd))
            self.assertNotEqual(cmd.get("status"), "delivered")
            self.assertEqual(cmd.get("text"), body)
            return {"ok": True, "code": "delivered"}

        got = self._send(body, key="down-ok", downlink=send)
        self.assertTrue(got["ok"], got)
        self.assertEqual(len(calls), 1)
        self.assertEqual(got["command"]["status"], "delivered")
        view = self.log.pull_receipt(got["command"]["cmd_id"], stale=False, present=True)
        self.assertEqual(view["status"], "delivered")
        self.assertTrue(view["ok"])
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(body, dumped)
        self.assertNotIn("text", view)
        again = self._send(body, key="down-ok", downlink=send)
        self.assertEqual(again["command"]["cmd_id"], got["command"]["cmd_id"])
        self.assertEqual(len(calls), 1)

    def test_installed_downlink_is_used_when_send_omits_argument(self):
        body = "再问一句"
        calls = []

        def send(cmd):
            calls.append(cmd.get("text"))
            return {"ok": True, "code": "delivered"}

        install_downlink(send)
        got = self._send(body, key="installed")
        self.assertEqual(calls, [body])
        self.assertEqual(got["command"]["status"], "delivered")
        view = self.log.pull_receipt(got["command"]["cmd_id"], stale=False, present=True)
        self.assertEqual(view["status"], "delivered")
        self.assertNotIn(body, json.dumps(view, ensure_ascii=False))

    def test_downlink_failure_receipt_hides_body(self):
        body = "hello"
        calls = []

        def send(cmd):
            calls.append(cmd.get("cmd_id"))
            return {"ok": False, "code": "downlink_failed", "reason": "下行失败 " + body}

        got = self._send(body, key="down-fail", downlink=send)
        self.assertTrue(got["ok"], got)
        self.assertEqual(len(calls), 1)
        self.assertEqual(got["command"]["status"], "failed")
        view = self.log.pull_receipt(got["command"]["cmd_id"], stale=False, present=True)
        self.assertEqual(view["status"], "failed")
        self.assertFalse(view["ok"])
        self.assertEqual(view["code"], "downlink_failed")
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(body, dumped)
        self.assertNotIn("text", view)
        self.assertNotIn("OSSBOX", dumped)

    def test_downlink_exception_is_failure_and_hides_body(self):
        body = "hello"

        def send(cmd):
            raise RuntimeError("发送中断 " + body)

        got = self._send(body, key="down-exc", downlink=send)
        self.assertEqual(got["command"]["status"], "failed")
        view = self.log.pull_receipt(got["command"]["cmd_id"], stale=False, present=True)
        self.assertEqual(view["status"], "failed")
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(body, dumped)
        self.assertNotIn("发送中断", dumped)
