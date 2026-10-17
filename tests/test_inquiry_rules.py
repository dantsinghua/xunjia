"""底价、会话接管、消息状态、无回执询价中降级。不调用 Dify，不发消息。"""
from __future__ import annotations

from imlink.inquiry_rules import (
    FloorBook,
    SessionGate,
    downgrade_unreceipted_inquiring,
    message_delivery,
    widen_floor_bp,
)


def test_message_without_receipt_becomes_failure_after_one_minute():
    assert message_delivery("qd-1", 1_000_000, 2_000) == "已发出"
    assert message_delivery("", 1_000, 1_030) == "待回执"
    assert message_delivery("", 1_000, 1_061) == "发送失败"


def test_dify_inputs_carry_floor_and_bond_codes_and_outputs_are_short_names():
    from imlink.inquiry_rules import bonds_from_dify_outputs, dify_inquiry_inputs
    payload = dify_inquiry_inputs(
        reply="东兴 1.40 出",
        prior="1.10Y  244202.SH  25东兴G4",
        bond_codes=["244202.SH"],
        floor_bp=-2,
    )
    assert payload["workflow"] == "bondtrade-negotiator"
    assert payload["bond_code"] == "244202.SH"
    assert payload["floor_bp"] == -2
    assert "东兴 1.40 出" == payload["raw_text"]
    bonds = bonds_from_dify_outputs({
        "bonds": [
            {"bond_code": "244202.SH", "bond_name": "25东兴G4"},
            {"bond_code": "102680353.IB", "bond_name": "26镇江产投MTN003"},
        ]
    })
    assert [item["name"] for item in bonds] == ["25东兴G4", "26镇江产投MTN003"]


def test_widen_floor_subtracts_two_bp():
    assert widen_floor_bp(10) == 8
    assert widen_floor_bp(-2) == -4


def test_floor_persists_and_widen_needs_an_existing_floor(tmp_path):
    book = FloorBook(tmp_path / "rfq_floors.json")
    book.set_many(["244202.SH", "102680353.IB"], -2)
    try:
        FloorBook(tmp_path / "rfq_floors.json").widen("000000.IB")
    except KeyError as exc:
        assert "还没有底价" in str(exc)
    else:
        raise AssertionError("没有底价也放宽了")
    before, after = FloorBook(tmp_path / "rfq_floors.json").widen("244202.SH")
    assert before == -2
    assert after == -4
    assert FloorBook(tmp_path / "rfq_floors.json").get("102680353.IB") == -2


def test_unreceipted_inquiry_becomes_send_failure_without_resend():
    mapping = {"244202.SH": "询价中", "520172.SZ": "发送失败", "240202.IB": "询价中"}
    got = downgrade_unreceipted_inquiring(mapping)
    assert got == {"244202.SH": "发送失败", "520172.SZ": "发送失败", "240202.IB": "发送失败"}
    assert mapping["244202.SH"] == "询价中"


def test_pause_is_the_whole_session_and_resume_waits_for_next_inbound(tmp_path):
    gate = SessionGate(tmp_path / "rfq_sessions.json")
    assert gate.allow_reply("415011447") is True
    gate.pause("415011447")
    assert SessionGate(tmp_path / "rfq_sessions.json").allow_reply("415011447") is False
    SessionGate(tmp_path / "rfq_sessions.json").resume("415011447")
    assert SessionGate(tmp_path / "rfq_sessions.json").allow_reply("415011447") is True
    assert SessionGate(tmp_path / "rfq_sessions.json").allow_reply("415011447") is True
    SessionGate(tmp_path / "rfq_sessions.json").pause("415011447")
    assert SessionGate(tmp_path / "rfq_sessions.json").allow_reply("3004238268") is True
