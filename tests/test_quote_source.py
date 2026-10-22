"""行情凭据不回显。不连 innodealing，不用真实 cookie。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.quote_credentials import public_view, save_quote

_COOKIE = "sid=unit-test-sid-value; other=1"
_SID = "unit-test-sid-value"
_DEVICE = "unit-test-device"


class QuoteSourceTest(unittest.TestCase):
    def test_secrets_are_not_returned_and_blank_keeps_old(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            saved = save_quote({
                "account": "tester",
                "sid": _SID,
                "cookie": _COOKIE,
                "device_id": _DEVICE,
            }, folder)
            dumped = str(saved)
            self.assertNotIn(_COOKIE, dumped)
            self.assertNotIn(_SID, dumped)
            self.assertNotIn(_DEVICE, dumped)
            self.assertEqual(saved["account"], "tester")
            self.assertTrue(saved["sid_set"] and saved["cookie_set"] and saved["device_id_set"])
            again = save_quote({"account": "tester-2", "cookie": ""}, folder)
            self.assertEqual(again["account"], "tester-2")
            self.assertTrue(again["cookie_set"])
            self.assertEqual((folder / "cookie.txt").read_text(encoding="utf-8").strip(), _COOKIE)
