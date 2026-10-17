"""公网信箱第一期：绑定校验、未归券消息保留、拦截不产生指令。

不访问网络，不连接 nyoss.yingmi.com，也不读取真实密钥。

在仓库根目录运行::

    .venv/bin/python -m unittest tests.test_oss_phase1 -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from imlink.catalog import builtin_rows, probe_link
from imlink.commands import CommandLog, prepare_send, receipt_view
from imlink.monitor_feed import merge_monitor_messages
from imlink.store import Store


def _oss(name: str, workbench_id: str, account_uid: str) -> dict:
    return {
        "name": name,
        "mode": "oss",
        "oss": {"workbench_id": workbench_id, "account_uid": account_uid, "account_nick": "调用方备注"},
    }


class BindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "im_link.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_catalog_cannot_save(self):
        store = Store(self.path, catalog_rows=[])
        with self.assertRaises(ValueError) as caught:
            store.create(_oss("甲", "wb-nanjing-a", "30088001"))
        self.assertIn("不能保存", str(caught.exception))
        self.assertEqual(store.list_public()["links"], [])

    def test_workbench_without_qidian_cannot_save(self):
        store = Store(self.path, catalog_rows=builtin_rows())
        with self.assertRaises(ValueError) as caught:
            store.create(_oss("空台", "wb-empty", "wxid_demo"))
        self.assertIn("没有企点号", str(caught.exception))

    def test_same_qidian_cannot_bind_two_mailboxes(self):
        store = Store(self.path, catalog_rows=builtin_rows())
        store.create(_oss("甲", "wb-nanjing-a", "30088001"))
        with self.assertRaises(ValueError) as caught:
            store.create(_oss("乙", "wb-nanjing-a", "30088001"))
        self.assertIn("不能再绑", str(caught.exception))
        self.assertEqual(len(store.list_public()["links"]), 1)

    def test_saved_binding_uses_catalog_nick_not_remark(self):
        store = Store(self.path, catalog_rows=builtin_rows())
        link = store.create(_oss("甲", "wb-nanjing-a", "30088001"))
        self.assertEqual(link["oss"]["account_nick"], "南银理财-现券A")
        self.assertNotEqual(link["oss"]["account_nick"], "调用方备注")
        view = next(x for x in store.list_public()["links"] if x["id"] == link["id"])
        self.assertEqual(view["account_uid"], "30088001")
        self.assertEqual(view["account_nick"], "南银理财-现券A")
        self.assertEqual(view["account_id"], "")
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("qd01", dumped)
        self.assertNotIn('"label"', dumped)

    def test_probe_does_not_leak_key(self):
        token = "unit-test-not-a-real-key"
        old = os.environ.get("OSSBOX_KEY_XUNJIA")
        os.environ["OSSBOX_KEY_XUNJIA"] = token
        try:
            result = probe_link(
                {"mode": "oss", "oss": {"workbench_id": "wb-nanjing-a", "account_uid": "30088001"}},
                builtin_rows(),
                key_ready=True,
            )
        finally:
            if old is None:
                os.environ.pop("OSSBOX_KEY_XUNJIA", None)
            else:
                os.environ["OSSBOX_KEY_XUNJIA"] = old
        self.assertTrue(result["passed"], result)
        self.assertNotIn(token, json.dumps(result, ensure_ascii=False))
        missing = probe_link(
            {"mode": "oss", "oss": {"workbench_id": "wb-nanjing-a", "account_uid": "30088001"}},
            builtin_rows(),
            key_ready=False,
        )
        self.assertFalse(missing["passed"])
        self.assertIn("密钥缺失", json.dumps(missing["steps"], ensure_ascii=False))


class MonitorFeedTest(unittest.TestCase):
    def test_messages_without_bond_are_kept(self):
        thread = [
            {"id": "t1", "text": "有券", "bond": "102584288.IB", "role": "peer"},
            {"id": "t2", "text": "线程里就没券", "bond": None, "role": "peer"},
        ]
        account = [
            {"id": "a1", "text": "信箱未归券", "role": "peer"},
            {"id": "a2", "text": "空字符串债券", "bond": "", "role": "peer"},
            {"id": "t1", "text": "有券", "bond": "102584288.IB", "role": "peer"},
        ]
        dropped = [m for m in thread + account if m.get("bond")]
        self.assertNotIn("信箱未归券", [m["text"] for m in dropped])
        merged = merge_monitor_messages(thread, account)
        texts = [m["text"] for m in merged]
        self.assertEqual(texts, ["有券", "线程里就没券", "信箱未归券", "空字符串债券"])
        self.assertEqual(merged[0]["bond"], "102584288.IB")
        self.assertEqual(merged[1]["bond"], "")
        self.assertEqual(merged[2]["bond"], "")
        self.assertEqual(merged[3]["bond"], "")

    def test_real_account_without_messages_is_empty(self):
        from imlink.monitor_feed import inbox_for, screen_for

        rows = inbox_for("3007378246")
        self.assertEqual(rows, [])
        screen = screen_for(rows)
        blob = json.dumps(screen, ensure_ascii=False)
        self.assertEqual(screen["counterparties"], [])
        self.assertEqual(screen["stats"][0]["value"], 0)
        self.assertEqual(screen["stats"][1]["value"], 0)
        for word in ("王磊", "中信证券", "宜春", "先打个招呼", "南银理财"):
            self.assertNotIn(word, blob)

    def test_collector_rows_do_not_mix_in_demo(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from imlink.monitor_feed import read_collector, screen_for

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "agent.db")
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE accounts (id TEXT, channel TEXT, self_uid TEXT);
            CREATE TABLE sessions (
              id TEXT, account_id TEXT, channel TEXT, native_id TEXT, name TEXT, kind TEXT
            );
            CREATE TABLE messages (
              id INTEGER, ext_msg_id TEXT, account_id TEXT, channel TEXT, session_id TEXT,
              dir TEXT, text TEXT, ts_ms INTEGER, is_self INTEGER
            );
            INSERT INTO accounts VALUES ('qd90','qidian','3007378246');
            INSERT INTO sessions VALUES ('s1','qd90','qidian','415011447','对手甲','private');
            INSERT INTO messages VALUES (1,'e1','qd90','qidian','s1','in','真实来信', 1000, 0);
            """
        )
        con.commit()
        con.close()
        rows = read_collector(path, "3007378246")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "真实来信")
        self.assertEqual(rows[0]["peer_name"], "对手甲")
        screen = screen_for(rows)
        blob = json.dumps(screen, ensure_ascii=False)
        self.assertEqual(screen["counterparties"][0]["name"], "对手甲")
        self.assertEqual(screen["counterparties"][0]["bonds"], [])
        for word in ("王磊", "中信证券", "宜春", "南银理财"):
            self.assertNotIn(word, blob)
        self.assertEqual(read_collector(path, "000"), [])


class CommandGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = CommandLog(Path(self.tmp.name) / "oss_commands.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _send(self, text: str, *, allow_write: bool = True, key: str = "k1") -> dict:
        return prepare_send(
            self.log,
            text,
            allow_write=allow_write,
            workbench_id="wb-nanjing-a",
            account_uid="30088001",
            native_id="41501",
            idempotency_key=key,
        )

    def test_blocked_text_writes_no_command(self):
        for text in ("我们成交", "底价不能说"):
            got = self._send(text, key="blocked-" + text)
            self.assertFalse(got["ok"])
            self.assertTrue(got["blocked"])
            self.assertNotIn(text, got["reason"])
        self.assertEqual(self.log.list_all(), [])

    def test_allowed_text_is_submitted_and_receipt_hides_body(self):
        body = "宜春估值加点能看下吗"
        got = self._send(body, key="ok-1")
        self.assertTrue(got["ok"], got)
        cmd = got["command"]
        self.assertEqual(cmd["status"], "submitted")
        self.assertEqual(cmd["op"], "send_text")
        self.assertEqual(cmd["channel"], "qidian")
        self.assertEqual(len(self.log.list_all()), 1)
        again = self._send(body, key="ok-1")
        self.assertEqual(again["command"]["cmd_id"], cmd["cmd_id"])
        self.assertEqual(len(self.log.list_all()), 1)
        view = self.log.pull_receipt(cmd["cmd_id"], stale=False, present=True)
        self.assertEqual(view["status"], "submitted")
        self.assertNotEqual(view["status"], "delivered")
        dumped = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(body, dumped)
        self.assertNotIn("text", view)
        failed = self._send("再发一条", key="ok-2")
        fail_view = self.log.pull_receipt(
            failed["command"]["cmd_id"],
            stale=False,
            present=True,
            demo_ack={"ok": False, "code": "failed", "reason": "对端未确认"},
        )
        self.assertEqual(fail_view["status"], "failed")
        self.assertEqual(fail_view["reason"], "对端未确认")
        self.assertNotIn("再发一条", json.dumps(fail_view, ensure_ascii=False))
        pending = receipt_view({"cmd_id": "x", "status": "submitted", "ack": None, "text": body})
        self.assertEqual(pending["status"], "submitted")
        self.assertNotIn("text", pending)


if __name__ == "__main__":
    unittest.main()


    def test_mail_and_oss_cannot_bind_same_qidian(self):
        store = Store(self.path, catalog_rows=builtin_rows())
        store.create(_oss("甲", "wb-nanjing-a", "30088001"))
        with self.assertRaises(ValueError) as caught:
            store.create({
                "name": "乙",
                "mode": "mail",
                "mail": {
                    "workbench_id": "wb-nanjing-a",
                    "account_uid": "30088001",
                    "mail_host": "127.0.0.1",
                    "mail_port": 3143,
                    "mail_ssl": False,
                    "mail_user": "quote",
                    "mail_password": "not-a-real-password",
                },
            })
        self.assertIn("不能同时", str(caught.exception))

    def test_mail_link_hides_password(self):
        store = Store(self.path, catalog_rows=builtin_rows())
        link = store.create({
            "name": "邮件甲",
            "mode": "mail",
            "mail": {
                "workbench_id": "wb-nanjing-a",
                "account_uid": "30088001",
                "mail_host": "127.0.0.1",
                "mail_port": 3143,
                "mail_user": "quote",
                "mail_password": "not-a-real-password",
            },
        })
        view = store.list_public()
        row = next(x for x in view["links"] if x["id"] == link["id"])
        dumped = __import__("json").dumps(row, ensure_ascii=False)
        self.assertNotIn("not-a-real-password", dumped)
        self.assertEqual(row["mode"], "mail")
        self.assertTrue(row["form"]["mail"]["secret_set"])
