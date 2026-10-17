"""询价中 / 发送失败落在状态表。不碰正式指令，不补发，不打开回执。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.commands import CommandLog, _installed_downlink, install_downlink
from imlink.rfq_status import StatusBook, statuses_from_commands
from imlink.store import Store
from imlink.uplink import install_uploader, installed_uploader

ROOT = Path(__file__).resolve().parents[1]
LIVE_COMMANDS = ROOT / "data" / "oss_commands.json"
LIVE_STATUS = ROOT / "data" / "rfq_status.json"
LIVE_BOOK = ROOT / "data" / "address_book.json"
MARKER = "状态表不该出现的正文MARK"
ACK_MARKER = "回执MARK不落盘"


def _snapshot(path: Path):
    if not path.exists():
        return None
    return path.read_bytes()


def _rows(updated_at: float) -> list[dict]:
    return [{
        "workbench_id": "wb-synth",
        "updated_at": updated_at,
        "accounts": [{
            "channel": "qidian",
            "account_uid": "9100001",
            "account_nick": "合成询价号",
            "contacts": [],
            "org": {"departments": [], "members": []},
        }],
    }]


def _forbid(*_args, **_kwargs):
    raise AssertionError("不应连接公网或企点")


def _mount(tmp_path, monkeypatch):
    import imlink.routes as routes

    store = Store(tmp_path / "im_link.json", catalog_rows=_rows(time.time()))
    log = CommandLog(tmp_path / "oss_commands.json")
    monkeypatch.setattr(routes, "store", store)
    monkeypatch.setattr(routes, "log", log)
    app = FastAPI()
    app.include_router(routes.router)
    link = store.create({
        "name": "合成公网",
        "mode": "oss",
        "oss": {"workbench_id": "wb-synth", "account_uid": "9100001"},
    })
    store.activate(link["id"])
    return log, TestClient(app)


def _arm(monkeypatch):
    monkeypatch.setattr("imlink.client.send_text", _forbid)
    monkeypatch.setattr("imlink.oss_catalog.http_get", _forbid)
    monkeypatch.setattr("imlink.oss_catalog.load_live_catalog", _forbid)
    previous = (_installed_downlink(), installed_uploader())
    install_downlink(None)
    install_uploader(None)
    return previous


def _restore(previous):
    install_downlink(previous[0])
    install_uploader(previous[1])


def _payload(code: str, text: str | None = None):
    body = text if text is not None else f"2.1000*ofr\n1.00Y  {code}  {MARKER}"
    return {
        "items": [{
            "code": code,
            "text": body,
            "fields": {"name": "不落盘的简称", "price": "2.1000", "side": "ofr"},
            "uins": ["9102001"],
        }]
    }


def _live():
    return _snapshot(LIVE_COMMANDS), _snapshot(LIVE_STATUS), _snapshot(LIVE_BOOK)


def test_failed_and_inquiring_survive_reload(tmp_path):
    path = tmp_path / "rfq_status.json"
    book = StatusBook(path)
    book.mark(["520172.SZ"], "发送失败")
    book.mark(["240202.IB"], "询价中")
    again = StatusBook(path)
    assert again.mapping() == {"520172.SZ": "发送失败", "240202.IB": "询价中"}
    raw = path.read_text("utf-8")
    assert MARKER not in raw
    assert "address_book" not in path.name
    assert path.name == "rfq_status.json"


def test_delivery_replaces_failed_and_delete_removes_only_that_code(tmp_path):
    path = tmp_path / "rfq_status.json"
    book = StatusBook(path)
    book.mark(["520172.SZ", "245878.SH"], "发送失败")
    book.mark(["520172.SZ"], "询价中")
    assert StatusBook(path).mapping() == {"520172.SZ": "询价中", "245878.SH": "发送失败"}
    book.delete("245878.SH")
    assert StatusBook(path).mapping() == {"520172.SZ": "询价中"}


def test_seed_uses_command_status_not_receipt_body(tmp_path):
    """失败补发送失败，送达只记待回执。提交不补。不把正文或回执写进状态表。"""
    commands = [
        {
            "status": "failed",
            "text": f"2.10*ofr\n1.00Y  520199.SZ  {MARKER}",
            "ack": {"reason": ACK_MARKER},
        },
        {"status": "submitted", "text": "1.00*bid\n1.00Y  133301.SZ  提交不入表"},
        {"status": "failed", "text": f"1.20*bid\n1.00Y  240202.IB  {MARKER}"},
        {"status": "delivered", "text": f"1.30*bid\n1.00Y  240202.IB  {MARKER}"},
        {"status": "delivered", "text": "ping"},
    ]
    assert statuses_from_commands(commands) == {"520199.SZ": "发送失败", "240202.IB": "待回执"}
    path = tmp_path / "rfq_status.json"
    book = StatusBook(path)
    assert book.ensure(commands) == {"520199.SZ": "发送失败", "240202.IB": "待回执"}
    book.delete("520199.SZ")
    assert StatusBook(path).ensure(commands) == {"240202.IB": "待回执"}
    raw = path.read_text("utf-8")
    assert MARKER not in raw
    assert ACK_MARKER not in raw
    assert "133301.SZ" not in raw
    assert "提交不入表" not in raw


def test_send_failure_and_delivery_persist_without_touching_live_files(tmp_path, monkeypatch):
    before = _live()
    previous = _arm(monkeypatch)
    try:
        log, client = _mount(tmp_path, monkeypatch)
        failed = client.post("/api/im/rfq/send", json=_payload("245878.SH"))
        assert failed.json()["ok"] is False
        listed = client.get("/api/im/rfq/status")
        assert listed.status_code == 200
        assert MARKER not in listed.text
        items = {item["code"]: item["status"] for item in listed.json()["data"]["items"]}
        assert items["245878.SH"] == "发送失败"

        def _up(cmd):
            assert cmd["cmd_id"] != "cmd_31e9de7f3eba"
            return True

        def _ack(cmd):
            return {"ok": True, "code": "delivered"}

        install_uploader(_up)
        install_downlink(_ack)
        sent = client.post("/api/im/rfq/send", json=_payload("245878.SH"))
        assert sent.json()["ok"] is True
        install_uploader(None)
        install_downlink(None)
        other = client.post("/api/im/rfq/send", json=_payload("520172.SZ"))
        assert other.json()["ok"] is False

        again = client.get("/api/im/rfq/status").json()["data"]["items"]
        mapping = {item["code"]: item["status"] for item in again}
        assert mapping["245878.SH"] == "待回执"
        assert mapping["520172.SZ"] == "发送失败"
        removed = client.delete("/api/im/rfq/status/520172.SZ")
        assert removed.status_code == 200
        left = {item["code"]: item["status"] for item in removed.json()["data"]["items"]}
        assert left == {"245878.SH": "待回执"}
        assert StatusBook(tmp_path / "rfq_status.json").mapping() == left
        blob = (tmp_path / "rfq_status.json").read_text("utf-8")
        assert MARKER not in blob
        assert "不落盘的简称" not in blob
        assert json.loads(blob)["codes"]["245878.SH"] == {"status": "待回执"}
        assert all(row.get("cmd_id") != "cmd_31e9de7f3eba" for row in log.list_all())
    finally:
        _restore(previous)
    assert _live() == before


def test_quoted_status_persists_too(tmp_path):
    path = tmp_path / "rfq_status.json"
    book = StatusBook(path)
    book.mark(["240202.IB"], "已报价")
    book.mark(["520172.SZ"], "发送失败")
    assert StatusBook(path).mapping() == {"240202.IB": "已报价", "520172.SZ": "发送失败"}
