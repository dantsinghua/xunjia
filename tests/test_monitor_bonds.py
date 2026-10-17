"""监控展示：一条发出正文里的两只代码只出一个气泡，债券列表要有这两只。

只用合成库，不读真实采集库，不向企点发消息。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from imlink.monitor_feed import detail_for_cp, read_collector, screen_for

CODE_A = "032680366.IB"
CODE_B = "102280109.IB"
OLD_TEXT = "没有代码的旧消息"
SENT_TEXT = (
    "2.17*ofr\n"
    f"4.98Y  {CODE_A}  合成债甲  私募  --/2.1700  --/--(*)  "
    "票面2.14%  AA+/--  中债2.1763  余额13.00亿  到期日2031-06-26  合成经纪  09:00:34\n"
    "1.58*bid\n"
    f"2.84Y  {CODE_B}  合成债乙  公募  1.5800/1.5600  3000/2000(*)  "
    "票面1.50%  AAA/--  中债1.5742  余额3.00亿  到期日2029-08-08  合成经纪  10:00:00"
)
PEER = "415011447"


class MonitorBondDisplayTest(unittest.TestCase):
    def test_one_outbound_with_two_codes_is_one_bubble(self):
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
            INSERT INTO sessions VALUES ('s1','qd90','qidian','415011447','合成对手','private');
            """
        )
        con.execute(
            "INSERT INTO messages VALUES (1,'e-old','qd90','qidian','s1','in',?, 1000, 0)",
            (OLD_TEXT,),
        )
        con.execute(
            "INSERT INTO messages VALUES (2,'e-new','qd90','qidian','s1','out',?, 2000, 1)",
            (SENT_TEXT,),
        )
        con.commit()
        con.close()

        rows = read_collector(path, "3007378246")
        self.assertEqual([row["text"] for row in rows], [OLD_TEXT, SENT_TEXT])
        self.assertTrue(all(row["bond"] == "" for row in rows))

        screen = screen_for(rows)
        cp = screen["counterparties"][0]
        chats = screen["chats"][PEER]
        sent = [msg for msg in chats if msg["tag"] == "发出"]
        self.assertEqual(cp["id"], PEER)
        self.assertEqual(len(chats), 2)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], SENT_TEXT)
        self.assertEqual([msg["text"] for msg in chats], [OLD_TEXT, SENT_TEXT])
        self.assertEqual([bond["code"] for bond in cp["bonds"]], [CODE_A, CODE_B])
        self.assertEqual([bond["name"] for bond in cp["bonds"]], ["合成债甲", "合成债乙"])
        self.assertTrue(all(bond["stage"] == "询价中" for bond in cp["bonds"]))

        detail = detail_for_cp(screen, PEER)
        self.assertEqual([row["code"] for row in detail], [CODE_A, CODE_B])
        self.assertEqual([row["st"] for row in detail], ["询价中", "询价中"])
        for row in detail:
            self.assertEqual(row["trader"], "")
            self.assertEqual(row["pm"], "")
            self.assertEqual(row["val"], "")
            self.assertEqual(row["ours"], "")
            self.assertEqual(row["theirs"], "")

        blob = json.dumps(screen, ensure_ascii=False)
        for word in ("王磊", "中信证券", "宜春", "南银理财"):
            self.assertNotIn(word, blob)

    def test_other_counterparty_detail_stays_separate(self):
        screen = screen_for([
            {
                "id": "a",
                "text": f"请看 {CODE_A}",
                "tag": "发出",
                "bond": "",
                "peer_id": PEER,
                "peer_name": "合成对手",
            },
            {
                "id": "b",
                "text": "没有代码的旧消息",
                "tag": "收到",
                "peer_id": "900000001",
                "peer_name": "另一对手",
            },
        ])
        self.assertEqual(len(screen["chats"][PEER]), 1)
        self.assertEqual(screen["chats"][PEER][0]["tag"], "发出")
        self.assertEqual(len(screen["chats"]["900000001"]), 1)
        self.assertEqual(screen["counterparties"][1]["bonds"], [])
        self.assertEqual([row["code"] for row in detail_for_cp(screen, PEER)], [CODE_A])
        self.assertEqual(detail_for_cp(screen, "900000001"), [])


    def test_no_receipt_after_one_minute_is_send_failure_and_hides_bonds(self):
        """没有企点读库回执，超过 1 分钟不能标已发出，债也不能标询价中。"""
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
            INSERT INTO sessions VALUES ('s1','qd90','qidian','415011447','合成对手','private');
            """
        )
        con.execute(
            "INSERT INTO messages VALUES (2,'','qd90','qidian','s1','out',?, ?, 1)",
            (SENT_TEXT, 1_000_000),
        )
        con.commit()
        con.close()
        rows = read_collector(path, "3007378246")
        self.assertEqual(rows[0]["tag"], "发送失败")
        self.assertFalse(rows[0]["landed"])
        screen = screen_for(rows, {CODE_A: "询价中", CODE_B: "询价中"})
        self.assertEqual(screen["counterparties"][0]["bonds"], [])
        self.assertEqual(screen["chats"][PEER][0]["tag"], "发送失败")

    def test_receipt_keeps_only_inquiry_bonds(self):
        from imlink.monitor_feed import screen_for as build
        screen = build([
            {
                "id": "a",
                "text": f"请看 {CODE_A} 和 {CODE_B}",
                "tag": "发出",
                "landed": True,
                "bond": "",
                "peer_id": PEER,
                "peer_name": "合成对手",
            },
        ], {CODE_A: "询价中", CODE_B: "发送失败"})
        self.assertEqual([bond["name"] for bond in screen["counterparties"][0]["bonds"]], [CODE_A])
        self.assertEqual(screen["counterparties"][0]["bonds"][0]["stage"], "询价中")


    def test_inbound_logs_dify_not_called_and_hides_text(self):
        import logging
        from imlink import monitor_feed
        monitor_feed._LOGGED_INBOUND.clear()
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
            INSERT INTO sessions VALUES ('s1','qd90','qidian','415011447','合成对手','private');
            """
        )
        con.execute(
            "INSERT INTO messages VALUES (9,'e-in','qd90','qidian','s1','in',?, 3000, 0)",
            ("东兴 1.40 出",),
        )
        con.commit()
        con.close()
        with self.assertLogs("xunjia.monitor", level="INFO") as caught:
            read_collector(path, "3007378246")
        blob = "\n".join(caught.output)
        self.assertIn("询价入站未调用Dify", blob)
        self.assertIn("text_len=", blob)
        self.assertNotIn("东兴", blob)


    def test_reply_without_code_uses_bond_short_name(self):
        """没有代码的回复，用简称对上同一会话里的债。"""
        from imlink.monitor_feed import annotate_bond_context
        quote = (
            "1.5100*ofr\n"
            "1.10Y  244202.SH  25东兴G4  --  1.5300/1.5100\n"
            "2.30Y  102680353.IB  26镇江产投\n"
            "MTN003  --  1.7000/--"
        )
        rows = annotate_bond_context([
            {"text": quote, "peer_id": "415011447", "tag": "发出"},
            {"text": "东兴 1.40 出", "peer_id": "415011447", "tag": "收到"},
            {"text": "东兴 1.40 可以出哈，老板看看", "peer_id": "415011447", "tag": "收到"},
            {"text": "hellol", "peer_id": "415011447", "tag": "发出"},
        ])
        self.assertEqual(rows[0]["contexts"][0], {"code": "244202.SH", "name": "25东兴G4"})
        self.assertEqual(rows[0]["contexts"][1]["code"], "102680353.IB")
        self.assertEqual(rows[0]["contexts"][1]["name"], "26镇江产投MTN003")
        self.assertEqual(rows[1]["bond"], "244202.SH")
        self.assertEqual(rows[1]["bond_name"], "25东兴G4")
        self.assertEqual(rows[2]["bond_name"], "25东兴G4")
        self.assertEqual(rows[3]["bond"], "")
        self.assertEqual(rows[3]["contexts"], [])
