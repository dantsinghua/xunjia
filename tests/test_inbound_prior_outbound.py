"""询价回复入站带上同一会话最近一条本方发出的原文。不调用 Dify。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from imlink.inbound_context import attach_prior_outbound
from imlink.monitor_feed import inbox_for, read_collector, remember_messages

ROOT = Path(__file__).resolve().parents[1]
INBOUND = ROOT / "imlink" / "inbound_context.py"


def test_reply_carries_latest_outbound_original_in_same_session():
    rows = [
        {"native_id": "9102001", "dir": "out", "text": "消息甲\n原样", "ts_ms": 10, "role": "bot"},
        {"native_id": "9102001", "dir": "out", "text": "消息乙", "ts_ms": 20, "role": "bot"},
        {"native_id": "9102999", "dir": "out", "text": "另一会话", "ts_ms": 15, "role": "bot"},
        {"native_id": "9102001", "dir": "in", "text": "对方回复", "ts_ms": 30, "role": "peer"},
        {"native_id": "9102999", "dir": "in", "text": "另一边的回复", "ts_ms": 16, "role": "peer"},
        {"native_id": "9102001", "dir": "in", "text": "更早的回复", "ts_ms": 5, "role": "peer"},
    ]
    got = attach_prior_outbound(rows)
    by_text = {item["text"]: item for item in got}
    assert by_text["对方回复"]["prior_outbound_text"] == "消息乙"
    assert by_text["另一边的回复"]["prior_outbound_text"] == "另一会话"
    assert by_text["更早的回复"]["prior_outbound_text"] == ""
    assert "prior_outbound_text" not in by_text["消息甲\n原样"]
    assert by_text["消息甲\n原样"]["text"] == "消息甲\n原样"


def test_outbound_text_is_not_reassembled():
    original = "  已改过的一行\n第二行  "
    got = attach_prior_outbound([
        {"peer_id": "s1", "is_self": 1, "text": original, "time": 1},
        {"peer_id": "s1", "dir": "in", "text": "收到", "time": 2},
    ])
    assert got[1]["prior_outbound_text"] == original


def test_module_does_not_call_dify():
    """字段停在询价入站。这个模块不发起 Dify 调用。"""
    source = INBOUND.read_text(encoding="utf-8")
    assert "call_workflow" not in source
    assert "worker_dify" not in source
    assert "httpx" not in source
    assert "workflows/run" not in source


def test_collector_inbound_attaches_prior_outbound(tmp_path):
    path = tmp_path / "agent.db"
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
        INSERT INTO accounts VALUES ('qd90','qidian','9100001');
        INSERT INTO sessions VALUES ('s1','qd90','qidian','9102001','对手甲','private');
        INSERT INTO messages VALUES (1,'e1','qd90','qidian','s1','out','本方先发', 1000, 1);
        INSERT INTO messages VALUES (2,'e2','qd90','qidian','s1','in','对方后回', 2000, 0);
        """
    )
    con.commit()
    con.close()
    rows = read_collector(str(path), "9100001")
    assert [row["text"] for row in rows] == ["本方先发", "对方后回"]
    assert rows[1]["prior_outbound_text"] == "本方先发"
    assert "prior_outbound_text" not in rows[0]


def test_remembered_inbox_attaches_prior_outbound():
    uid = "9100001-prior-unit"
    remember_messages(uid, [
        {"native_id": "9102001", "dir": "out", "role": "bot", "text": "先发出去", "time": 1},
        {"native_id": "9102001", "dir": "in", "role": "peer", "text": "再回一句", "time": 2},
    ])
    rows = inbox_for(uid)
    assert rows[1]["prior_outbound_text"] == "先发出去"
    assert rows[1]["text"] == "再回一句"
