"""15:10 发送缺陷：只写本地、status=submitted、没有上行、没有回执，不能当成功。

不补发 cmd_31e9de7f3eba，不向真实企点发消息。原文 text 原样进入指令。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.commands import CommandLog, _installed_downlink, install_downlink
from imlink.rfq_assign import render_quote
from imlink.store import Store
from imlink.uplink import install_uploader, installed_uploader

ROOT = Path(__file__).resolve().parents[1]
LIVE_COMMANDS = ROOT / "data" / "oss_commands.json"
MODAL = ROOT / "web" / "src" / "components" / "RfqAssignModal.vue"
QUOTE = ROOT / "web" / "src" / "pages" / "Quote.vue"
LEGACY_CMD = "cmd_31e9de7f3eba"
EDITED = "2.0990*ofr\n29.53Y  2600002.IB  左手改过的原文"


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
    previous_down = _installed_downlink()
    previous_up = installed_uploader()
    install_downlink(None)
    install_uploader(None)
    return previous_down, previous_up


def _restore(previous):
    install_downlink(previous[0])
    install_uploader(previous[1])


def _payload(text: str = EDITED):
    return {
        "items": [{
            "code": "2600002.IB",
            "text": text,
            "fields": {
                "price": "9.8765",
                "side": "bid",
                "name": "不会重拼的简称",
                "tenor": "1.00Y",
            },
            "uins": ["9102001"],
        }]
    }


def test_local_submitted_without_uplink_or_receipt_is_not_ok(tmp_path, monkeypatch):
    """指令只在本地、submitted、没有上行、没有回执时，接口不能 ok。"""
    live_before = _snapshot(LIVE_COMMANDS)
    previous = _arm(monkeypatch)
    try:
        log, client = _mount(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json=_payload())
        body = got.json()
        assert body["ok"] is False
        assert body["hint"] == "没有上行，也没有回执"
        batches = body["data"]["batches"]
        assert len(batches) == 1
        assert batches[0]["status"] == "submitted"
        assert batches[0]["status"] != "delivered"
        assert batches[0]["text"] == EDITED
        cmds = log.list_all()
        assert len(cmds) == 1
        cmd = cmds[0]
        assert cmd["status"] == "submitted"
        assert cmd["ack"] is None
        assert cmd["text"] == EDITED
        assert cmd["cmd_id"] != LEGACY_CMD
        assert LEGACY_CMD not in json.dumps(body, ensure_ascii=False)
        rendered = render_quote("2600002.IB", _payload()["items"][0]["fields"])
        assert cmd["text"] != rendered
        assert "不会重拼的简称" not in cmd["text"]
        assert "9.8765" not in cmd["text"]
    finally:
        _restore(previous)
    assert _snapshot(LIVE_COMMANDS) == live_before


def test_edited_text_is_stored_verbatim_and_legacy_cmd_is_not_resent(tmp_path, monkeypatch):
    live_before = _snapshot(LIVE_COMMANDS)
    previous = _arm(monkeypatch)
    seen = []

    def _up(cmd):
        seen.append(cmd["cmd_id"])
        assert cmd["text"] == EDITED
        assert cmd["op"] == "send_text"
        assert cmd["channel"] == "qidian"
        assert cmd["session"] == {"kind": "private", "native_id": "9102001"}
        return False

    install_uploader(_up)
    try:
        log, client = _mount(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json=_payload())
        assert got.json()["ok"] is False
        assert seen
        assert LEGACY_CMD not in seen
        assert all(row["cmd_id"] != LEGACY_CMD for row in log.list_all())
        assert log.list_all()[0]["text"] == EDITED
    finally:
        _restore(previous)
    assert _snapshot(LIVE_COMMANDS) == live_before


def test_upload_without_receipt_is_not_ok(tmp_path, monkeypatch):
    previous = _arm(monkeypatch)
    install_uploader(lambda _cmd: True)
    try:
        log, client = _mount(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json=_payload())
        body = got.json()
        assert body["ok"] is False
        assert body["hint"] == "没有回执"
        assert body["data"]["batches"][0]["status"] == "submitted"
        assert log.list_all()[0]["status"] == "submitted"
        assert log.list_all()[0]["text"] == EDITED
    finally:
        _restore(previous)


def test_receipt_without_upload_is_not_ok(tmp_path, monkeypatch):
    previous = _arm(monkeypatch)

    def _ack(cmd):
        assert cmd["text"] == EDITED
        return {"ok": True, "code": "delivered"}

    install_downlink(_ack)
    try:
        log, client = _mount(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json=_payload())
        body = got.json()
        assert body["ok"] is False
        assert body["hint"] == "没有上行"
        assert log.list_all()[0]["text"] == EDITED
    finally:
        _restore(previous)


def test_upload_and_receipt_mark_delivered_without_qidian(tmp_path, monkeypatch):
    previous = _arm(monkeypatch)
    uploaded = []

    def _up(cmd):
        uploaded.append(cmd["text"])
        return True

    def _ack(cmd):
        assert cmd["text"] == EDITED
        return {"ok": True, "code": "delivered"}

    install_uploader(_up)
    install_downlink(_ack)
    try:
        log, client = _mount(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json=_payload())
        body = got.json()
        assert body["ok"] is True
        assert body["data"]["batches"][0]["status"] == "delivered"
        assert body["data"]["batches"][0]["text"] == EDITED
        assert uploaded == [EDITED]
        assert log.list_all()[0]["status"] == "delivered"
        assert log.list_all()[0]["cmd_id"] != LEGACY_CMD
    finally:
        _restore(previous)


def test_blank_text_is_400_and_not_reassembled(tmp_path, monkeypatch):
    previous = _arm(monkeypatch)
    called = {"n": 0}

    def _up(_cmd):
        called["n"] += 1
        return True

    install_uploader(_up)
    try:
        log, client = _mount(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json=_payload("   \n"))
        assert got.status_code == 400
        assert got.json()["ok"] is False
        assert "正文为空" in got.json()["hint"]
        assert log.list_all() == []
        assert called["n"] == 0
        assert "不会重拼的简称" not in got.text
    finally:
        _restore(previous)


def test_page_does_not_mark_inquiring_on_submitted():
    quote = QUOTE.read_text(encoding="utf-8")
    assert '@failed="onRfqFailed"' in quote
    proc = subprocess.run(
        ["node", "tests/acceptance/quote_status_probe.mjs"], cwd=ROOT / "web",
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    states = json.loads(proc.stdout)
    assert states["modal"]["queued"]["events"] == ["pending"]
    assert states["modal"]["sent"]["events"] == ["sent"]
    assert states["modal"]["failed"]["events"] == ["failed"]
    for result in states["modal"].values():
        assert result["beforePreview"] == 0
        assert result["calls"][-1]["body"]["batch_id"] == "batch-test"
        assert "items" not in result["calls"][-1]["body"]
    # Event labels cannot override the authoritative state, including read failure.
    assert states["onRfqFailed"]["status"] == "询价中"
    assert states["onRfqFailed"]["afterReadFailure"] == "询价中"
    assert states["onRfqPending"]["status"] == "待回执"
    assert states["onRfqSent"]["status"] == "待回执"
