"""确认发送必须密封上行，并等到采集端回执。假对象存储，不连公网，不发企点。"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.commands import CommandLog, install_downlink
from imlink.crypto import open_sealed, seal
from imlink.oss_send import install_uplink
from imlink.uplink import install_uploader, upload_live
from imlink.rfq_assign import render_quote
from imlink.store import Store

XUNJIA_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
DEPOSIT_KEY = bytes.fromhex("ffeeddccbbaa99887766554433221100")
WB = "wb-synth"
ACCOUNT = "9100001"


class FakeOss:
    """内存里的对象存储。ack 为 None 时只留下行对象，不给回执。"""

    def __init__(self, key: bytes, *, ack: bool | None = True, reason: str = ""):
        self.key = key
        self.ack = ack
        self.reason = reason
        self.puts: list[tuple[str, bytes]] = []
        self.objects: dict[str, bytes] = {}

    def put(self, name: str, body: bytes) -> None:
        self.puts.append((name, bytes(body)))
        self.objects[name] = bytes(body)
        if self.ack is None or "/in/xunjia/" not in name:
            return
        cmd_id = name.rsplit("/", 1)[-1][:-4]
        workbench = name.split("/")[1]
        payload: dict[str, object] = {
            "cmd_id": cmd_id,
            "ok": bool(self.ack),
            "code": "delivered" if self.ack else "failed",
        }
        if not self.ack:
            payload["reason"] = self.reason or "采集端发送失败"
        blob = seal(self.key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        self.objects[f"wb/{workbench}/ack/{cmd_id}.bin"] = blob

    def get(self, name: str):
        return self.objects.get(name)


def install_fake(monkeypatch, box: FakeOss) -> None:
    monkeypatch.setenv("OSSBOX_KEY_XUNJIA", XUNJIA_KEY.hex())
    monkeypatch.setenv("OSSBOX_KEY_DEPOSIT", DEPOSIT_KEY.hex())
    monkeypatch.setenv("XUNJIA_ACK_TIMEOUT_S", "0")
    install_downlink(None)
    install_uploader(upload_live)
    install_uplink(box)


def _rows() -> list[dict]:
    return [{
        "workbench_id": WB,
        "updated_at": 10**12,
        "accounts": [{
            "channel": "qidian",
            "account_uid": ACCOUNT,
            "account_nick": "合成询价号",
            "contacts": [],
            "org": {"departments": [], "members": []},
        }],
    }]


def _client(tmp_path, monkeypatch):
    import imlink.routes as routes

    store = Store(tmp_path / "im_link.json", catalog_rows=_rows())
    log = CommandLog(tmp_path / "oss_commands.json")
    monkeypatch.setattr(routes, "store", store)
    monkeypatch.setattr(routes, "log", log)
    link = store.create({
        "name": "合成公网",
        "mode": "oss",
        "oss": {"workbench_id": WB, "account_uid": ACCOUNT},
    })
    store.activate(link["id"])
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), log


def _plain(blob: bytes) -> dict:
    return json.loads(open_sealed(XUNJIA_KEY, blob).decode("utf-8"))


def test_local_file_without_ack_is_not_success(tmp_path, monkeypatch):
    """只把密封包写在本地、没有采集端成功回执，接口不算发送成功。"""
    box = FakeOss(XUNJIA_KEY, ack=None)
    install_fake(monkeypatch, box)
    folder = tmp_path / "local-only"
    folder.mkdir()
    original_put = box.put

    def put(name: str, body: bytes) -> None:
        original_put(name, body)
        (folder / name.replace("/", "_")).write_bytes(body)

    box.put = put  # type: ignore[method-assign]
    try:
        client, log = _client(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json={"items": [{
            "code": "SYN001.IB",
            "text": "只写本地不算发出",
            "fields": {"name": "合成债甲", "price": "1.00", "side": "ofr"},
            "uins": ["9102001"],
        }]})
        body = got.json()
        assert body["ok"] is False
        assert body["hint"]
        assert "只写本地不算发出" not in body["hint"]
        batches = body["data"]["batches"]
        assert batches[0]["status"] != "delivered"
        assert log.list_all()[0]["status"] != "delivered"
        assert list(folder.iterdir())
        assert "in/deposit" not in box.puts[0][0]
    finally:
        install_uplink(None)
        install_uploader(None)
        install_downlink(None)


def test_edited_text_is_sealed_verbatim_for_one_uin(tmp_path, monkeypatch):
    """同一 uin 只上行一条。正文用请求原文，不按字段重拼，且只用询价密钥。"""
    box = FakeOss(XUNJIA_KEY, ack=True)
    install_fake(monkeypatch, box)
    first = "2.0990*ofr\n29.53Y  2600001.IB  已改过的甲"
    second = "3.0100*bid\n1.20Y  2600002.IB  已改过的乙"
    fields = {"name": "不会重拼", "price": "9.99", "side": "bid"}
    rendered = render_quote("2600001.IB", fields)
    try:
        client, log = _client(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json={"items": [
            {"code": "2600001.IB", "text": first, "fields": fields, "uins": ["9102001"]},
            {"code": "2600002.IB", "text": second, "fields": fields, "uins": ["9102001"]},
        ]})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["ok"] is True
        batches = body["data"]["batches"]
        assert len(batches) == 1
        assert batches[0]["uin"] == "9102001"
        assert batches[0]["status"] == "delivered"
        assert batches[0]["codes"] == ["2600001.IB", "2600002.IB"]
        assert len(box.puts) == 1
        name, blob = box.puts[0]
        assert name == f"wb/{WB}/in/xunjia/{batches[0]['cmd_id']}.bin"
        assert "/in/deposit/" not in name
        try:
            open_sealed(DEPOSIT_KEY, blob)
        except ValueError:
            pass
        else:
            raise AssertionError("存款密钥不应解开询价指令")
        payload = _plain(blob)
        assert payload["text"] == first + "\n" + second
        assert payload["text"] != rendered
        assert "不会重拼" not in payload["text"]
        assert "9.99" not in payload["text"]
        assert payload["channel"] == "qidian"
        assert payload["op"] == "send_text"
        assert payload["session"] == {"kind": "private", "native_id": "9102001"}
        assert payload["account_uid"] == ACCOUNT
        assert len(log.list_all()) == 1
        assert log.list_all()[0]["status"] == "delivered"
    finally:
        install_uplink(None)
        install_uploader(None)
        install_downlink(None)


def test_deposit_key_alone_does_not_upload(tmp_path, monkeypatch):
    box = FakeOss(XUNJIA_KEY, ack=True)
    monkeypatch.delenv("OSSBOX_KEY_XUNJIA", raising=False)
    monkeypatch.setenv("OSSBOX_KEY_DEPOSIT", DEPOSIT_KEY.hex())
    monkeypatch.setenv("XUNJIA_ACK_TIMEOUT_S", "0")
    install_downlink(None)
    install_uploader(upload_live)
    install_uplink(box)
    try:
        client, log = _client(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json={"items": [{
            "code": "SYN001.IB",
            "text": "没有询价密钥不能发",
            "fields": {"name": "合成债甲"},
            "uins": ["9102001"],
        }]})
        body = got.json()
        assert body["ok"] is False
        assert body["hint"] == "没有询价密钥"
        assert box.puts == []
        assert log.list_all()[0]["status"] != "delivered"
    finally:
        install_uplink(None)
        install_uploader(None)
        install_downlink(None)


def test_failed_ack_is_not_delivered_and_hides_text(tmp_path, monkeypatch):
    body_text = "回执失败时不要把这句带回页面原因"
    box = FakeOss(XUNJIA_KEY, ack=False, reason="会话不存在 " + body_text)
    install_fake(monkeypatch, box)
    try:
        client, _log = _client(tmp_path, monkeypatch)
        got = client.post("/api/im/rfq/send", json={"items": [{
            "code": "SYN009.IB",
            "text": body_text,
            "fields": {"name": "合成债甲"},
            "uins": ["9102001"],
        }]})
        body = got.json()
        assert body["ok"] is False
        assert body["data"]["batches"][0]["status"] != "delivered"
        assert "会话不存在" in body["hint"]
        assert body_text not in body["hint"]
    finally:
        install_uplink(None)
        install_uploader(None)
        install_downlink(None)


def test_redline_and_stale_do_not_upload(tmp_path, monkeypatch):
    box = FakeOss(XUNJIA_KEY, ack=True)
    install_fake(monkeypatch, box)
    try:
        client, log = _client(tmp_path, monkeypatch)
        blocked = client.post("/api/im/rfq/send", json={"items": [{
            "code": "SYN001.IB",
            "text": "我们成交",
            "fields": {"name": "合成债甲"},
            "uins": ["9102001"],
        }]})
        assert blocked.status_code == 400
        assert blocked.json()["ok"] is False
        assert blocked.json()["blocked"] is True
        assert box.puts == []
        assert log.list_all() == []

        import imlink.routes as routes

        stale = Store(tmp_path / "stale.json", catalog_rows=[{
            **_rows()[0],
            "updated_at": 1,
        }])
        monkeypatch.setattr(routes, "store", stale)
        link = stale.create({
            "name": "过期",
            "mode": "oss",
            "oss": {"workbench_id": WB, "account_uid": ACCOUNT},
        })
        stale.activate(link["id"])
        app = FastAPI()
        app.include_router(routes.router)
        stale_got = TestClient(app).post("/api/im/rfq/send", json={"items": [{
            "code": "SYN001.IB",
            "text": "工作台过期不能发",
            "fields": {"name": "合成债甲"},
            "uins": ["9102001"],
        }]})
        assert stale_got.status_code == 400
        assert "工作台未上报" in stale_got.json()["hint"]
        assert box.puts == []
    finally:
        install_uplink(None)
        install_uploader(None)
        install_downlink(None)


def test_put_rejection_logs_status_without_secret(caplog, monkeypatch):
    """对象存储拒绝时，日志留下状态码和 Code，不留下凭证和正文。"""
    import logging
    from datetime import datetime, timedelta, timezone
    from imlink.oss_send import LiveUplink, OssSendError
    from imlink.sts_credentials import CredentialSet

    credentials = CredentialSet("kid", "SECRETVALUE", "synthetic-security-token",
                                datetime.now(timezone.utc) + timedelta(hours=1), "bkt")
    uplink = LiveUplink(credentials=credentials, put_transport=lambda *args: (
        403, b"<Error><Code>AccessDenied</Code><Message>denied</Message></Error>"))
    with caplog.at_level(logging.WARNING, logger="xunjia.oss_send"):
        try:
            uplink.put("wb/anlin-dev/in/xunjia/cmd_trace.bin", b"sealed-bytes")
        except OssSendError as exc:
            assert str(exc) == "指令没有上行到公网信箱"
        else:
            raise AssertionError("拒绝上传应失败")
    assert "branch=rejected" in caplog.text
    assert "status=403" in caplog.text
    assert "oss_code=AccessDenied" in caplog.text
    assert "SECRETVALUE" not in caplog.text
    assert "sealed-bytes" not in caplog.text


def test_written_file_is_not_called_upload_failure(tmp_path, monkeypatch, caplog):
    """文件已经写入时，不能再报没有上行。断点只记对象名，不记正文。"""
    import logging
    box = FakeOss(XUNJIA_KEY, ack=True)
    install_fake(monkeypatch, box)
    quote = "2.0990*ofr\n29.53Y  2600001.IB  断点不该出现的正文"
    try:
        client, _log = _client(tmp_path, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="xunjia.oss_send"):
            got = client.post("/api/im/rfq/send", json={"items": [
                {"code": "2600001.IB", "text": quote, "fields": {"name": "甲"}, "uins": ["9102001"]},
            ]})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["ok"] is True
        assert "没有上行" not in got.text
        assert "branch=written" in caplog.text
        assert "询价回执断点" in caplog.text
        assert "断点不该出现的正文" not in caplog.text
        assert len(box.puts) == 1
    finally:
        install_uplink(None)
        install_uploader(None)
        install_downlink(None)
