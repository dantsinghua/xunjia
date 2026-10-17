"""开始询价规格（2026-10-12 交接第 1、2 节）。

只用合成债券和合成 uin。不访问对象存储，不向真实企点号发消息，
不改正式的 im_link.json、address_book.json，也不写微信群。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.commands import CommandLog, install_downlink
from imlink.rfq_assign import render_quote
from imlink.store import Store

ROOT = Path(__file__).resolve().parents[1]
LIVE_LINK = ROOT / "data" / "im_link.json"
LIVE_BOOK = ROOT / "data" / "address_book.json"
LIVE_COMMANDS = ROOT / "data" / "oss_commands.json"

WB = "wb-rfq-lab"
ACCOUNT = "91000001"
UIN_A = "9102001"
UIN_B = "9102002"
WECHAT_GROUP = "9108chatroom"

EXAMPLE_CODE = "032680366.IB"
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

BOND_A = {
    "code": "SYN001.IB",
    "fields": {
        "price": "1.23",
        "side": "bid",
        "tenor": "1.50Y",
        "name": "合成债甲",
        "placement": "公募",
        "bid": "1.2300",
        "ofr": "1.2500",
        "bid_vol": "2000",
        "ofr_vol": "3000",
        "coupon": "1.20%",
        "issuer_rating": "AAA",
        "bond_rating": "AA",
        "cnbd": "1.2400",
        "balance": "2.00亿",
        "maturity": "2032-01-01",
        "broker": "合成经纪",
        "quoted_at": "10:11:12",
    },
}
BOND_B = {
    "code": "SYN002.IB",
    "fields": {
        "price": "3.05",
        "side": "ofr",
        "tenor": "2.00Y",
        "name": "合成债乙",
        "placement": "私募",
        "bid": "",
        "ofr": "3.0500",
        "bid_vol": "",
        "ofr_vol": "",
        "coupon": "3.00%",
        "issuer_rating": "AA",
        "bond_rating": "",
        "cnbd": "3.0600",
        "balance": "5.00亿",
        "maturity": "2033-02-02",
        "broker": "合成经纪",
        "quoted_at": "11:22:33",
    },
}


def _dash(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return text if text else "--"


def _plain(value: object) -> str:
    return "" if value is None else str(value).strip()


def _money(value: object) -> str:
    text = _dash(value)
    if text == "--":
        return text
    try:
        return f"{float(str(value).replace(',', '')):.2f}"
    except (TypeError, ValueError):
        return text


def spec_text(code: str, fields: dict) -> str:
    """规格正文。报出的价格保留两位小数。"""
    line1 = f"{_money(fields.get('price'))}*{_plain(fields.get('side'))}"
    parts = [
        _plain(fields.get("tenor")),
        _plain(code),
        _plain(fields.get("name")),
        _plain(fields.get("placement")),
        f"{_money(fields.get('bid'))}/{_money(fields.get('ofr'))}",
        f"{_dash(fields.get('bid_vol'))}/{_dash(fields.get('ofr_vol'))}(*)",
        f"票面{_plain(fields.get('coupon'))}",
        f"{_plain(fields.get('issuer_rating'))}/{_dash(fields.get('bond_rating'))}",
        f"中债{_money(fields.get('cnbd'))}",
        f"余额{_plain(fields.get('balance'))}",
        f"到期日{_plain(fields.get('maturity'))}",
        _plain(fields.get("broker")),
        _plain(fields.get("quoted_at")),
    ]
    return line1 + "\n" + "  ".join(parts)


def _forbid_oss(*_args, **_kwargs):
    raise AssertionError("测试不得访问对象存储")


def _rows() -> list:
    return [
        {
            "workbench_id": WB,
            "updated_at": time.time(),
            "accounts": [
                {
                    "channel": "qidian",
                    "account_uid": ACCOUNT,
                    "account_nick": "合成询价号",
                    "contacts": [{"uin": UIN_A, "name": "合成对手甲"}],
                    "org": {"departments": [], "members": []},
                },
                {
                    "channel": "wechat",
                    "account_uid": "wxid_synth",
                    "account_nick": "合成微信",
                    "book": "wechat/wxid_synth/groups.json",
                    "groups": [{"native_id": WECHAT_GROUP, "name": "合成固收群"}],
                    "group_native_ids": [WECHAT_GROUP],
                },
            ],
        }
    ]


def _snapshot(path: Path):
    if not path.exists():
        return None
    return path.read_bytes()


def _groups(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("groups.json"))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    import imlink.commands as commands
    import imlink.routes as routes

    monkeypatch.setattr("imlink.oss_catalog.load_live_catalog", _forbid_oss)
    monkeypatch.setattr("imlink.oss_catalog.http_get", _forbid_oss)
    from imlink.uplink import install_uploader, installed_uploader
    previous = commands._installed_downlink()
    previous_uploader = installed_uploader()
    install_downlink(None)
    install_uploader(None)
    cmd_path = tmp_path / "oss_commands.json"
    cmd_log = CommandLog(cmd_path)
    monkeypatch.setenv("XUNJIA_COMMANDS_PATH", str(cmd_path))
    monkeypatch.setattr(commands, "log", cmd_log)
    monkeypatch.setattr(routes, "log", cmd_log)
    try:
        import imlink.rfq_assign as rfq_assign
    except ImportError:
        rfq_assign = None
    if rfq_assign is not None and hasattr(rfq_assign, "log"):
        monkeypatch.setattr(rfq_assign, "log", cmd_log)

    store = Store(tmp_path / "im_link.json", catalog_rows=_rows())
    monkeypatch.setattr(routes, "store", store)
    link = store.create({
        "name": "合成通道",
        "mode": "oss",
        "oss": {"workbench_id": WB, "account_uid": ACCOUNT},
    })
    store.activate(link["id"])

    book = store.address_book_path
    book.write_text('{"entries":[]}\n', encoding="utf-8")
    groups = tmp_path / "wechat" / "wxid_synth" / "groups.json"
    groups.parent.mkdir(parents=True)
    groups.write_text(
        json.dumps({"groups": [{"native_id": WECHAT_GROUP, "name": "合成固收群"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    held = {
        "live_link": _snapshot(LIVE_LINK),
        "live_book": _snapshot(LIVE_BOOK),
        "live_commands": _snapshot(LIVE_COMMANDS),
        "link": _snapshot(store.path),
        "book": _snapshot(book),
        "groups": _snapshot(groups),
        "group_names": _groups(tmp_path),
    }
    try:
        yield {
            "client": client,
            "store": store,
            "cmd_path": cmd_path,
            "cmd_log": cmd_log,
            "book": book,
            "groups": groups,
            "held": held,
            "tmp_path": tmp_path,
        }
    finally:
        install_downlink(previous)
        install_uploader(previous_uploader)


def _assert_files_unchanged(rig) -> None:
    held = rig["held"]
    assert _snapshot(LIVE_LINK) == held["live_link"]
    assert _snapshot(LIVE_BOOK) == held["live_book"]
    assert _snapshot(LIVE_COMMANDS) == held["live_commands"]
    assert _snapshot(rig["store"].path) == held["link"]
    assert _snapshot(rig["book"]) == held["book"]
    assert _snapshot(rig["groups"]) == held["groups"]
    assert _groups(rig["tmp_path"]) == held["group_names"]
    group_text = rig["groups"].read_text(encoding="utf-8")
    assert WECHAT_GROUP in group_text
    assert "合成固收群" in group_text


def _commands(rig) -> list:
    if not rig["cmd_path"].exists():
        return []
    data = json.loads(rig["cmd_path"].read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def _assert_private_qidian(cmd: dict, uin: str, text: str) -> None:
    assert cmd["channel"] == "qidian"
    assert cmd["op"] == "send_text"
    assert cmd["session"] == {"kind": "private", "native_id": uin}
    assert cmd["text"] == text
    assert cmd["status"] == "submitted"
    blob = json.dumps(cmd, ensure_ascii=False)
    assert "groups.json" not in blob
    assert WECHAT_GROUP not in blob
    assert "合成固收群" not in blob
    assert "wechat" not in blob


def test_example_fields_render_two_lines():
    """示例字段渲染成规格里的两行；空的 bid、债项评级、量都是 --。"""
    assert spec_text(EXAMPLE_CODE, EXAMPLE_FIELDS) == EXAMPLE_TEXT
    assert render_quote(EXAMPLE_CODE, EXAMPLE_FIELDS) == EXAMPLE_TEXT
    assert render_quote(EXAMPLE_CODE, EXAMPLE_FIELDS).split("\n")[1].split("  ")[4:6] == [
        "--/2.17",
        "--/--(*)",
    ]
    assert "AA+/--" in render_quote(EXAMPLE_CODE, EXAMPLE_FIELDS)

    omitted = {
        key: value
        for key, value in EXAMPLE_FIELDS.items()
        if key not in ("bid", "bond_rating", "bid_vol", "ofr_vol")
    }
    assert render_quote(EXAMPLE_CODE, omitted) == EXAMPLE_TEXT

    blanks = dict(EXAMPLE_FIELDS)
    blanks["bid"] = None
    blanks["bond_rating"] = None
    blanks["bid_vol"] = "   "
    blanks["ofr_vol"] = None
    assert render_quote(EXAMPLE_CODE, blanks) == EXAMPLE_TEXT


def test_same_uin_merges_into_one_batch_and_one_command(rig):
    """两只债选同一个 uin，合并成一批，正文用换行连接，只产生一条指令。"""
    first = spec_text(BOND_A["code"], BOND_A["fields"])
    second = spec_text(BOND_B["code"], BOND_B["fields"])
    expected = first + "\n" + second
    response = rig["client"].post("/api/im/rfq/send", json={
        "items": [
            {"code": BOND_A["code"], "fields": BOND_A["fields"], "uins": [UIN_A]},
            {"code": BOND_B["code"], "fields": BOND_B["fields"], "uins": [UIN_A]},
        ]
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert "没有上行" in body["hint"]
    batches = body["data"]["batches"]
    assert len(batches) == 1
    assert batches[0]["uin"] == UIN_A
    assert batches[0]["text"] == expected
    assert batches[0]["status"] == "submitted"
    assert batches[0]["text"].count("\n") == 3
    assert "--/3.05" in batches[0]["text"]
    assert "--/--(*)" in batches[0]["text"]
    assert "AA/--" in batches[0]["text"]

    rows = _commands(rig)
    assert len(rows) == 1
    assert rows[0]["cmd_id"] == batches[0]["cmd_id"]
    _assert_private_qidian(rows[0], UIN_A, expected)
    _assert_files_unchanged(rig)


def test_one_bond_two_uins_makes_two_batches(rig):
    """一只债两个 uin，分成两批，各写一条私聊指令。"""
    expected = spec_text(BOND_A["code"], BOND_A["fields"])
    response = rig["client"].post("/api/im/rfq/send", json={
        "items": [
            {"code": BOND_A["code"], "fields": BOND_A["fields"], "uins": [UIN_A, UIN_B]},
        ]
    })
    assert response.status_code == 200, response.text
    body = response.json()
    # No uploader or execution receipt was installed: submitted is not sent.
    assert body["ok"] is False
    assert "没有上行" in body["hint"]
    batches = body["data"]["batches"]
    assert [item["uin"] for item in batches] == [UIN_A, UIN_B]
    assert [item["text"] for item in batches] == [expected, expected]
    assert [item["status"] for item in batches] == ["submitted", "submitted"]
    assert len({item["cmd_id"] for item in batches}) == 2

    rows = _commands(rig)
    assert len(rows) == 2
    by_uin = {row["session"]["native_id"]: row for row in rows}
    assert set(by_uin) == {UIN_A, UIN_B}
    for batch in batches:
        _assert_private_qidian(by_uin[batch["uin"]], batch["uin"], expected)
        assert by_uin[batch["uin"]]["cmd_id"] == batch["cmd_id"]
    _assert_files_unchanged(rig)


@pytest.mark.parametrize(
    "items",
    [
        pytest.param([{"code": "SYN001.IB", "fields": BOND_A["fields"], "uins": []}], id="empty-uins"),
        pytest.param([{"code": "SYN001.IB", "fields": BOND_A["fields"]}], id="missing-uins"),
        pytest.param([{"code": "SYN001.IB", "fields": BOND_A["fields"], "uins": [""]}], id="blank-uin"),
        pytest.param([{"code": "SYN001.IB", "fields": BOND_A["fields"], "uins": ["   "]}], id="space-uin"),
        pytest.param([{"fields": BOND_A["fields"], "uins": [UIN_A]}], id="missing-code"),
        pytest.param([{"code": "  ", "fields": BOND_A["fields"], "uins": [UIN_A]}], id="blank-code"),
        pytest.param(
            [{"code": "SYN001.IB", "fields": {k: v for k, v in BOND_A["fields"].items() if k != "name"}, "uins": [UIN_A]}],
            id="missing-name",
        ),
        pytest.param(
            [{"code": "SYN001.IB", "fields": {**BOND_A["fields"], "name": ""}, "uins": [UIN_A]}],
            id="blank-name",
        ),
        pytest.param(
            [{"code": "SYN001.IB", "fields": {**BOND_A["fields"], "name": "   "}, "uins": [UIN_A]}],
            id="space-name",
        ),
        pytest.param(
            [
                {"code": BOND_A["code"], "fields": BOND_A["fields"], "uins": [UIN_A]},
                {"code": BOND_B["code"], "fields": {**BOND_B["fields"], "name": ""}, "uins": [UIN_A]},
            ],
            id="second-item-missing-name",
        ),
        pytest.param(
            [
                {"code": BOND_A["code"], "fields": BOND_A["fields"], "uins": [UIN_A]},
                {"code": BOND_B["code"], "fields": BOND_B["fields"], "uins": []},
            ],
            id="second-item-without-uin",
        ),
    ],
)
def test_missing_uin_code_or_name_returns_400_and_writes_no_command(rig, items):
    """没有 uin、缺 code 或缺 name 时返回 400，不写 oss 指令文件。"""
    response = rig["client"].post("/api/im/rfq/send", json={"items": items})
    assert response.status_code == 400, response.text
    assert response.json().get("ok") is not True
    assert not rig["cmd_path"].exists()
    assert _commands(rig) == []
    _assert_files_unchanged(rig)


def test_send_does_not_change_link_book_or_wechat_groups(rig):
    """发送不改 im_link.json，不改 address_book.json，不写微信群。"""
    response = rig["client"].post("/api/im/rfq/send", json={
        "items": [
            {"code": BOND_A["code"], "fields": BOND_A["fields"], "uins": [UIN_A]},
        ]
    })
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert "没有上行" in response.json()["hint"]
    blob = json.dumps(response.json(), ensure_ascii=False)
    assert WECHAT_GROUP not in blob
    assert "合成固收群" not in blob
    assert "groups.json" not in blob
    rows = _commands(rig)
    assert len(rows) == 1
    _assert_private_qidian(rows[0], UIN_A, spec_text(BOND_A["code"], BOND_A["fields"]))
    _assert_files_unchanged(rig)
    link_text = rig["store"].path.read_text(encoding="utf-8")
    book_text = rig["book"].read_text(encoding="utf-8")
    assert "合成债甲" not in link_text
    assert "SYN001.IB" not in link_text
    assert book_text == '{"entries":[]}\n'
    assert "groups" not in book_text
