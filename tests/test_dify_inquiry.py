"""询价入站走 bondtrade-negotiator。假工作流，不访问外网，不发消息。"""
from __future__ import annotations

import json

from imlink.dify_inquiry import (
    DifyClient,
    LabelBook,
    confirm_bonds,
    http_inputs,
    run_pending,
)
from imlink.inquiry_rules import FloorBook, SessionGate
from imlink.monitor_feed import screen_for
from imlink.rfq_status import StatusBook


PRIOR = "1.10Y  244202.SH  25东兴G4"
REPLY = "东兴 1.40 出"


def _row(mid, text, ts, prior=PRIOR):
    return {
        "id": mid,
        "dir": "in",
        "tag": "收到",
        "text": text,
        "time": ts,
        "peer_id": "415011447",
        "peer_name": "合成对手",
        "native_id": "415011447",
        "prior_outbound_text": prior,
        "landed": True,
    }


def test_floor_change_is_sent_on_the_next_dify_call_and_names_come_back(tmp_path):
    floors = FloorBook(tmp_path / "rfq_floors.json")
    floors.set_many(["244202.SH"], -2)
    gate = SessionGate(tmp_path / "rfq_sessions.json")
    labels = LabelBook(tmp_path / "rfq_dify_labels.json")
    seen = []

    def client(inputs):
        seen.append((inputs["floor_bp"], inputs["raw_text"]))
        assert inputs["workflow"] == "bondtrade-negotiator"
        assert inputs["bond_code"] == "244202.SH"
        return {
            "bonds": [{"bond_code": "244202.SH", "bond_name": "25东兴G4"}],
            "reply": "按底价继续",
        }

    rows = [_row("m1", REPLY, 1_000)]
    run_pending(rows, floors=floors, gate=gate, labels=labels, client=client, limit=None)
    floors.widen("244202.SH")
    rows.append(_row("m2", "东兴再让一让", 1_100))
    run_pending(rows, floors=floors, gate=gate, labels=labels, client=client, limit=None)
    assert seen == [(-2, REPLY), (-4, "东兴再让一让")]
    screen = screen_for(rows, {"244202.SH": "询价中"}, labels.all())
    chat = screen["chats"]["415011447"]
    assert chat[0]["bond_state"] == "已识别"
    assert chat[0]["contexts"][0]["name"] == "25东兴G4"
    assert [item for item in chat if item["tag"] == "机器回复"] == []
    labels.mark_dispatch("m1", "uploaded")
    sent = screen_for(rows, {"244202.SH": "询价中"}, labels.all())
    bots = [item for item in sent["chats"]["415011447"] if item["tag"] == "机器回复"]
    assert bots[0]["text"] == "按底价继续"
    assert bots[0]["landed"] is True
    again = []
    run_pending(rows, floors=floors, gate=gate, labels=labels, client=lambda _inputs: again.append(1), limit=None)
    assert again == []


def test_paused_session_and_old_line_do_not_call_dify(tmp_path):
    floors = FloorBook(tmp_path / "rfq_floors.json")
    floors.set_many(["244202.SH"], 3)
    gate = SessionGate(tmp_path / "rfq_sessions.json")
    gate.pause("415011447")
    labels = LabelBook(tmp_path / "rfq_dify_labels.json")
    seen = []
    run_pending(
        [_row("m1", REPLY, 2_000)],
        floors=floors, gate=gate, labels=labels, client=lambda _inputs: seen.append(1), limit=None,
    )
    assert seen == []
    gate.resume("415011447")
    run_pending(
        [_row("old", REPLY, 1)],
        floors=floors, gate=gate, labels=labels, client=lambda _inputs: seen.append(1), limit=None,
    )
    assert seen == []
    run_pending(
        [_row("new", REPLY, 4_000_000_000)],
        floors=floors, gate=gate, labels=labels, client=lambda inputs: seen.append(inputs["floor_bp"]) or {
            "bonds": [{"bond_code": "244202.SH", "bond_name": "25东兴G4"}],
        },
        limit=None,
    )
    assert seen == [3]


def test_screen_stays_recognizing_until_dify_returns_a_short_name():
    import time
    rows = [_row("m1", REPLY, time.time(), prior="")]
    screen = screen_for(rows)
    msg = screen["chats"]["415011447"][0]
    assert msg["bond_state"] == "识别中"
    assert msg["contexts"] == []
    assert msg["bond_name"] == ""


def test_recognizing_hides_after_one_minute_and_cards_use_short_names():
    import time
    now = time.time()
    quote = "43D 012680486.IB 26武汉城建\nSCP002 -- 1.50/--"
    rows = [
        {
            "id": "out", "dir": "out", "tag": "发出", "text": quote, "time": now,
            "peer_id": "415011447", "landed": True, "ext_msg_id": "qd-1",
        },
        _row("old", "收到了", now - 120, prior=""),
        _row("new", "再看看", now, prior=""),
    ]
    screen = screen_for(rows, {"012680486.IB": "询价中"})
    bonds = screen["counterparties"][0]["bonds"]
    assert bonds[0]["name"] == "26武汉城建SCP002"
    assert bonds[0]["code"] == "012680486.IB"
    chat = {item["id"]: item for item in screen["chats"]["415011447"]}
    assert "012680486.IB" not in chat["out"]["text"]
    assert "26武汉城建SCP002" in chat["out"]["text"]
    assert chat["old"]["bond_state"] == ""
    assert chat["new"]["bond_state"] == "识别中"


def test_sent_line_is_not_drawn_again_as_machine_reply():
    import time
    text = "这个价我这边走下内部流程确认,稍等,落实了马上叫您"
    rows = [{
        "id": "out1", "dir": "out", "tag": "发出", "text": text, "time": time.time(),
        "peer_id": "415011447", "landed": True, "ext_msg_id": "qd-9",
    }]
    screen = screen_for(rows, {}, {"out1": {"reply": text, "bonds": []}})
    tags = [item.get("tag") for item in screen["chats"]["415011447"]]
    assert tags == ["发出"]


def test_anchor_output_is_accepted_as_short_name():
    from imlink.inquiry_rules import bonds_from_dify_outputs
    bonds = bonds_from_dify_outputs({
        "券锚线索": {"命中代码": "244202.SH", "命中简称片段": "25东兴G4", "涉及多券": False},
    })
    assert bonds == [{"code": "244202.SH", "name": "25东兴G4"}]


def test_receipt_promotes_pending_and_timeout_fails_without_touching_give_up():
    now = 1_900_000_000.0
    fresh = "2026-10-12T19:00:00"
    # created_at 解析成本地时间。这里只验证有回执升询价中、无回执且超时变失败。
    mapping = {"244202.SH": "待回执", "520172.SZ": "待回执", "240202.IB": "已放弃"}
    commands = [
        {"text": PRIOR, "created_at": fresh, "status": "delivered"},
        {"text": "1.00Y  520172.SZ  26岳湖01", "created_at": "2000-01-01T00:00:00", "status": "delivered"},
        {"text": "1.00Y  240202.IB  保持", "created_at": "2000-01-01T00:00:00", "status": "delivered"},
    ]
    promoted = confirm_bonds(mapping, commands, [
        {"text": PRIOR, "ext_msg_id": "qd-1"},
    ], now)
    assert promoted["244202.SH"] == "询价中"
    assert promoted["520172.SZ"] == "发送失败"
    assert promoted["240202.IB"] == "已放弃"
    assert "qd-1" not in json.dumps(promoted, ensure_ascii=False)


def test_status_book_follows_receipt_confirmation(tmp_path):
    book = StatusBook(tmp_path / "rfq_status.json")
    book.mark(["244202.SH"], "待回执")
    from imlink.dify_inquiry import sync_receipts
    sync_receipts(book, [
        {"text": PRIOR, "created_at": "2026-10-12T19:00:00"},
    ], [
        {"dir": "out", "id": "qd-9", "tag": "发出", "landed": True, "text": PRIOR},
    ], 1_800_000_000)
    assert book.mapping()["244202.SH"] == "询价中"


def test_http_body_carries_floor_and_hides_the_key():
    captured = {}

    class Resp:
        def read(self):
            return json.dumps({"data": {"outputs": {
                "text": json.dumps({"bonds": [{"bond_code": "244202.SH", "bond_name": "25东兴G4"}]}, ensure_ascii=False),
            }}}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Resp()

    client = DifyClient("https://dify.example", "app-test", opener=opener)
    outputs = client({
        "workflow": "bondtrade-negotiator",
        "raw_text": REPLY,
        "prior_outbound_text": PRIOR,
        "bond_code": "244202.SH",
        "bond_codes": "244202.SH",
        "floor_bp": -4,
    })
    assert captured["url"].endswith("/v1/workflows/run")
    assert captured["auth"] == "Bearer app-test"
    sent = captured["body"]["inputs"]
    assert set(sent) == {"message", "context_json"}
    assert sent["message"] == REPLY
    ctx = json.loads(sent["context_json"])
    assert ctx["floor_bp"] == -4
    assert ctx["remaining_bp"] == 4
    assert ctx["bond_code"] == "244202.SH"
    assert ctx["prior_outbound_text"] == PRIOR
    assert "workflow" not in sent
    assert "app-test" not in json.dumps(captured["body"])
    assert outputs["bonds"][0]["bond_name"] == "25东兴G4"


def test_quota_failure_is_not_a_successful_result():
    class Resp:
        def read(self):
            return json.dumps({"data": {"status": "failed", "error": "429 insufficient_quota"}}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    client = DifyClient("https://dify.example", "app-test", opener=lambda req, timeout: Resp())
    try:
        client({"raw_text": REPLY, "floor_bp": -2})
    except RuntimeError as exc:
        assert str(exc) == "dify quota"
    else:
        raise AssertionError("额度用尽也当成识别成功")
