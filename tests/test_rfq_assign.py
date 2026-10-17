"""开始询价：两行正文、按 uin 换行合并、公网指令。只用合成券和合成 uin。"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.commands import CommandLog, _installed_downlink, install_downlink
from imlink.rfq_assign import build_batches, render_quote
from imlink.store import Store

ROOT = Path(__file__).resolve().parents[1]
LIVE_LINK = ROOT / "data" / "im_link.json"
LIVE_COMMANDS = ROOT / "data" / "oss_commands.json"
LIVE_BOOK = ROOT / "data" / "address_book.json"
TS_LIB = ROOT / "web" / "src" / "lib" / "rfqAssign.ts"
QUOTE = ROOT / "web" / "src" / "pages" / "Quote.vue"
MODAL = ROOT / "web" / "src" / "components" / "RfqAssignModal.vue"

EXAMPLE_FIELDS = {
    "price": "2.17",
    "side": "ofr",
    "tenor": "4.98Y",
    "name": "26安吉两山PPN002",
    "placement": "私募",
    "bid": "",
    "ofr": "2.1700",
    "bid_vol": "",
    "ofr_vol": "",
    "coupon": "2.14%",
    "issuer_rating": "AA+",
    "bond_rating": "",
    "cnbd": "2.1763",
    "balance": "13.00亿",
    "maturity": "2031-06-26",
    "broker": "上田",
    "quoted_at": "09:00:34",
}
EXAMPLE_TEXT = (
    "2.17*ofr\n"
    "4.98Y  032680366.IB  26安吉两山PPN002  私募  --/2.17  --/--(*)  "
    "票面2.14%  AA+/--  中债2.18  余额13.00亿  到期日2031-06-26  上田  09:00:34"
)

_RUNNER = r"""
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const mod = await import(pathToFileURL(process.argv[2]).href)
const payload = JSON.parse(readFileSync(process.argv[3], 'utf8'))
if (typeof mod.renderQuote !== 'function' || typeof mod.mergeByUin !== 'function') {
  console.error('缺少渲染函数')
  process.exit(2)
}
const example = mod.renderQuote(payload.example.code, payload.example.fields)
const merged = mod.mergeByUin(payload.items)
console.log(JSON.stringify({ example, merged }))
"""


def _snapshot(path: Path):
    if not path.exists():
        return None
    return path.read_bytes()


def _fields(name: str, **extra: str) -> dict:
    base = {
        "price": "1.80",
        "side": "bid",
        "tenor": "2.10Y",
        "name": name,
        "placement": "公募",
        "bid": "1.8000",
        "ofr": "",
        "bid_vol": "1000",
        "ofr_vol": "",
        "coupon": "1.50%",
        "issuer_rating": "AAA",
        "bond_rating": "",
        "cnbd": "1.7900",
        "balance": "5.00亿",
        "maturity": "2028-01-01",
        "broker": "国际",
        "quoted_at": "10:00:00",
    }
    base.update(extra)
    return base


def test_spec_example_renders_two_lines():
    text = render_quote("032680366.IB", EXAMPLE_FIELDS)
    assert text == EXAMPLE_TEXT
    assert "  " in text.split("\n", 1)[1]
    from relay.side_b.phrase import guard
    assert guard(text).allowed


def test_empty_fields_become_dashes():
    text = render_quote("SYN.IB", {"name": "合成券"})
    assert text == (
        "--*--\n"
        "--  SYN.IB  合成券  --  --/--  --/--(*)  票面--  --/--  "
        "中债--  余额--  到期日--  --  --"
    )


def test_same_uin_merges_with_newline_and_order_follows_first_seen():
    first = render_quote("SYN001.IB", _fields("合成债甲", side="ofr", price="2.17", ofr="2.1700", bid=""))
    second = render_quote("SYN002.IB", _fields("合成债乙"))
    batches = build_batches([
        {"code": "SYN001.IB", "fields": _fields("合成债甲", side="ofr", price="2.17", ofr="2.1700", bid=""), "uins": ["9102001", "9102001", "9102002"]},
        {"code": "SYN002.IB", "fields": _fields("合成债乙"), "uins": ["9102001"]},
    ])
    assert [item["uin"] for item in batches] == ["9102001", "9102002"]
    assert batches[0]["text"] == first + "\n" + second
    assert batches[0]["codes"] == ["SYN001.IB", "SYN002.IB"]
    assert batches[1]["text"] == first
    assert batches[1]["codes"] == ["SYN001.IB"]
    assert "\n\n" not in batches[0]["text"]


def test_missing_recipient_code_or_name_raises_before_any_text():
    import pytest
    from imlink.rfq_assign import RfqAssignError

    with pytest.raises(RfqAssignError, match="没有收件人"):
        build_batches([])
    with pytest.raises(RfqAssignError, match="没有收件人"):
        build_batches([{"code": "SYN001.IB", "fields": _fields("合成债甲"), "uins": []}])
    with pytest.raises(RfqAssignError, match="缺市场代码"):
        build_batches([{"code": "  ", "fields": _fields("合成债甲"), "uins": ["9102001"]}])
    with pytest.raises(RfqAssignError, match="缺简称"):
        build_batches([{"code": "SYN001.IB", "fields": _fields(""), "uins": ["9102001"]}])


def test_frontend_render_matches_backend(tmp_path):
    payload = {
        "example": {"code": "032680366.IB", "fields": EXAMPLE_FIELDS},
        "items": [
            {"code": "SYN001.IB", "fields": _fields("合成债甲"), "uins": ["9102001", "9102002"]},
            {"code": "SYN002.IB", "fields": _fields("合成债乙"), "uins": ["9102001"]},
        ],
    }
    src = tmp_path / "payload.json"
    runner = tmp_path / "run.mts"
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    runner.write_text(_RUNNER, encoding="utf-8")
    proc = subprocess.run(
        ["node", "--experimental-strip-types", str(runner), str(TS_LIB), str(src)],
        cwd=str(ROOT / "web"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    got = json.loads(proc.stdout)
    assert got["example"] == render_quote("032680366.IB", EXAMPLE_FIELDS)
    assert got["merged"] == build_batches(payload["items"])


def test_quote_page_only_opens_the_modal():
    quote = QUOTE.read_text(encoding="utf-8")
    start = quote.split("function startRfq()", 1)[1].split("function onRfqSent", 1)[0]
    assert "rfqStart" not in start
    assert "询价中" not in start
    assert "rfqOpen.value = true" in start
    proc = subprocess.run(
        ["node", "tests/acceptance/quote_status_probe.mjs"], cwd=ROOT / "web",
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    states = json.loads(proc.stdout)
    assert states["onRfqSent"]["status"] == "待回执"
    assert states["onRfqSent"]["afterReadFailure"] == "待回执"
    assert "/api/im/rfq/status" in states["onRfqSent"]["requests"]
    modal = MODAL.read_text(encoding="utf-8")
    assert "imContacts" in modal and "imOrg" in modal
    assert "imRfqSend" in modal
    assert "groups.json" not in modal
    assert "微信群" not in modal
    assert "rfqStart" not in modal
    route = (ROOT / "imlink" / "routes.py").read_text(encoding="utf-8")
    body = route.split("def rfq_send", 1)[1].split("\ndef ", 1)[0]
    assert "_write_oss" in body
    assert "send_text" not in body
    assert "client." not in body


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


def _mount(tmp_path, monkeypatch, rows):
    import imlink.routes as routes

    store = Store(tmp_path / "im_link.json", catalog_rows=rows)
    log = CommandLog(tmp_path / "oss_commands.json")
    monkeypatch.setattr(routes, "store", store)
    monkeypatch.setattr(routes, "log", log)
    app = FastAPI()
    app.include_router(routes.router)
    return store, log, TestClient(app)


def _forbid(*_args, **_kwargs):
    raise AssertionError("不应连接公网或企点")


def _arm(monkeypatch):
    monkeypatch.setattr("imlink.client.send_text", _forbid)
    monkeypatch.setattr("imlink.oss_catalog.http_get", _forbid)
    monkeypatch.setattr("imlink.oss_catalog.load_live_catalog", _forbid)
    from imlink.uplink import install_uploader, installed_uploader
    previous = (_installed_downlink(), installed_uploader())
    install_downlink(None)
    install_uploader(None)
    return previous


def _items():
    return {
        "items": [
            {"code": "SYN001.IB", "fields": _fields("合成债甲", side="ofr", price="2.17", ofr="2.1700", bid=""), "uins": ["9102001", "9102002"]},
            {"code": "SYN002.IB", "fields": _fields("合成债乙"), "uins": ["9102001"]},
        ]
    }


def test_route_rejects_without_writing(tmp_path, monkeypatch):
    live_link = _snapshot(LIVE_LINK)
    live_commands = _snapshot(LIVE_COMMANDS)
    live_book = _snapshot(LIVE_BOOK)
    previous = _arm(monkeypatch)
    try:
        _store, log, client = _mount(tmp_path, monkeypatch, _rows(time.time()))
        missing = client.post("/api/im/rfq/send", json={"items": [{"code": "", "fields": _fields("合成债甲"), "uins": ["9102001"]}]})
        assert missing.status_code == 400
        assert missing.json()["ok"] is False
        nobody = client.post("/api/im/rfq/send", json={"items": [{"code": "SYN001.IB", "fields": _fields("合成债甲"), "uins": []}]})
        assert nobody.status_code == 400
        assert "没有收件人" in nobody.json()["hint"]
        noname = client.post("/api/im/rfq/send", json={"items": [{"code": "SYN001.IB", "fields": _fields(""), "uins": ["9102001"]}]})
        assert noname.status_code == 400
        assert "缺简称" in noname.json()["hint"]
        assert log.list_all() == []
    finally:
        install_downlink(previous[0])
        from imlink.uplink import install_uploader
        install_uploader(previous[1])
    assert _snapshot(LIVE_LINK) == live_link
    assert _snapshot(LIVE_COMMANDS) == live_commands
    assert _snapshot(LIVE_BOOK) == live_book


def test_oss_send_merges_and_does_not_touch_live_files(tmp_path, monkeypatch):
    live_link = _snapshot(LIVE_LINK)
    live_commands = _snapshot(LIVE_COMMANDS)
    live_book = _snapshot(LIVE_BOOK)
    previous = _arm(monkeypatch)
    try:
        store, log, client = _mount(tmp_path, monkeypatch, _rows(time.time()))
        link = store.create({
            "name": "合成公网",
            "mode": "oss",
            "oss": {"workbench_id": "wb-synth", "account_uid": "9100001"},
        })
        store.activate(link["id"])
        got = client.post("/api/im/rfq/send", json=_items())
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["ok"] is False
        assert "没有上行" in body["hint"]
        batches = body["data"]["batches"]
        assert [item["uin"] for item in batches] == ["9102001", "9102002"]
        assert batches[0]["text"].count("\n") == 3
        assert "合成债甲" in batches[0]["text"] and "合成债乙" in batches[0]["text"]
        assert batches[0]["codes"] == ["SYN001.IB", "SYN002.IB"]
        assert batches[1]["text"].startswith("2.17*ofr\n")
        assert "合成债乙" not in batches[1]["text"]
        cmds = log.list_all()
        assert len(cmds) == 2
        assert {item["session"]["native_id"] for item in cmds} == {"9102001", "9102002"}
        assert {item["status"] for item in cmds} == {"submitted"}
        assert {item["op"] for item in cmds} == {"send_text"}
        assert {item["channel"] for item in cmds} == {"qidian"}
        assert {item["session"]["kind"] for item in cmds} == {"private"}
        by_uin = {item["session"]["native_id"]: item for item in cmds}
        assert by_uin["9102001"]["text"] == batches[0]["text"]
        assert by_uin["9102001"]["cmd_id"] == batches[0]["cmd_id"]
        assert all(item["idempotency_key"].startswith("rfq-") for item in cmds)
        assert len({item["idempotency_key"] for item in cmds}) == 2
        assert uuid.UUID(cmds[0]["idempotency_key"].removeprefix("rfq-"))
    finally:
        install_downlink(previous[0])
        from imlink.uplink import install_uploader
        install_uploader(previous[1])
    assert _snapshot(LIVE_LINK) == live_link
    assert _snapshot(LIVE_COMMANDS) == live_commands
    assert _snapshot(LIVE_BOOK) == live_book


def test_redline_and_stale_workbench_write_nothing(tmp_path, monkeypatch):
    previous = _arm(monkeypatch)
    try:
        store, log, client = _mount(tmp_path, monkeypatch, _rows(time.time()))
        link = store.create({
            "name": "合成公网",
            "mode": "oss",
            "oss": {"workbench_id": "wb-synth", "account_uid": "9100001"},
        })
        store.activate(link["id"])
        blocked = client.post("/api/im/rfq/send", json={"items": [{
            "code": "SYN001.IB",
            "fields": _fields("合成债甲", broker="我们成交"),
            "uins": ["9102001"],
        }]})
        assert blocked.status_code == 400
        payload = blocked.json()
        assert payload["ok"] is False
        assert payload["blocked"] is True
        assert "合成债甲" not in blocked.text
        assert log.list_all() == []

        stale_store, stale_log, stale_client = _mount(tmp_path / "stale", monkeypatch, _rows(time.time() - 1000))
        stale_link = stale_store.create({
            "name": "合成过期",
            "mode": "oss",
            "oss": {"workbench_id": "wb-synth", "account_uid": "9100001"},
        })
        stale_store.activate(stale_link["id"])
        stale = stale_client.post("/api/im/rfq/send", json=_items())
        assert stale.status_code == 400
        assert stale.json()["ok"] is False
        assert stale.json().get("blocked") is not True
        assert "工作台未上报" in stale.json()["hint"]
        assert stale_log.list_all() == []
        assert log.list_all() == []
    finally:
        install_downlink(previous[0])
        from imlink.uplink import install_uploader
        install_uploader(previous[1])


def test_local_mode_does_not_call_qidian(tmp_path, monkeypatch):
    previous = _arm(monkeypatch)
    called = {"n": 0}

    def _boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("不应直连企点")

    monkeypatch.setattr("imlink.client.send_text", _boom)
    try:
        store, log, client = _mount(tmp_path, monkeypatch, [])
        link = store.create({"name": "合成本机", "mode": "local", "account_id": "qd-synth"})
        store.save_test(link["id"], {
            "passed": True,
            "account": {"id": "qd-synth", "self_uid": "9100001", "label": "合成"},
            "note": "",
            "steps": [],
            "accounts": [],
        })
        store.activate(link["id"])
        got = client.post("/api/im/rfq/send", json=_items())
        assert got.status_code == 400
        assert got.json()["ok"] is False
        assert "公网信箱" in got.json()["hint"]
        assert called["n"] == 0
        assert log.list_all() == []
        _empty, empty_log, empty_client = _mount(tmp_path / "empty", monkeypatch, _rows(time.time()))
        missing = empty_client.post("/api/im/rfq/send", json=_items())
        assert missing.status_code == 409
        assert empty_log.list_all() == []
    finally:
        install_downlink(previous[0])
        from imlink.uplink import install_uploader
        install_uploader(previous[1])


def test_edited_text_is_sent_as_written():
    """整段改过的正文按原样发出，不再按字段重拼。"""
    batches = build_batches([{
        "code": "2600002.IB",
        "text": "2.0990*ofr\n29.53Y  2600002.IB  已改过的正文",
        "fields": {"name": "不会重拼"},
        "uins": ["9102001"],
    }])
    assert batches[0]["text"] == "2.0990*ofr\n29.53Y  2600002.IB  已改过的正文"
