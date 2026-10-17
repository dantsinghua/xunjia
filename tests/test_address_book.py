"""通讯录单独落盘。只用合成部门和合成 uin，不读存款侧配置，不写正式通道文件。"""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.address_book import load_entry, save_entry
from imlink.oss_catalog import rows_from_catalog
from imlink.store import Store

ROOT = Path(__file__).resolve().parents[1]
LIVE_LINK = ROOT / "data" / "im_link.json"
LIVE_COMMANDS = ROOT / "data" / "oss_commands.json"
LIVE_BOOK = ROOT / "data" / "address_book.json"


def _rows() -> list:
    now = time.time()
    return rows_from_catalog([
        {
            "workbench_id": "wb-book-a",
            "updated_at": now,
            "accounts": [
                {
                    "channel": "qidian",
                    "account_uid": "81000001",
                    "account_nick": "合成号甲",
                    "groups": [{"native_id": "g_synth", "name": "合成企点群"}],
                    "group_native_ids": ["g_synth"],
                    "contacts": [{"uin": "8101001", "name": "合成好友甲", "groups": ["不要进通讯录"]}],
                    "org": {
                        "departments": [
                            {"id": "d-root", "parent_id": "", "name": "合成总部", "order": 0},
                            {"id": "d-sub", "parent_id": "d-root", "name": "合成分部", "order": 1},
                        ],
                        "members": [
                            {"uin": "8102001", "name": "合成成员甲", "account": "", "department_id": "d-root", "order": 0},
                            {"uin": "8102002", "name": "合成成员乙", "account": "", "department_id": "d-sub", "order": 0},
                        ],
                        "groups": [{"native_id": "g_org", "name": "组织不进群"}],
                    },
                },
                {
                    "channel": "qidian",
                    "account_uid": "81000002",
                    "account_nick": "合成号乙",
                    "contacts": [{"uin": "8101002", "name": "合成好友乙"}],
                    "org": {
                        "departments": [
                            {"id": "d-b", "parent_id": "", "name": "合成乙部", "order": 0},
                        ],
                        "members": [],
                    },
                },
                {
                    "channel": "wechat",
                    "account_uid": "wxid_synth",
                    "account_nick": "合成微信",
                    "groups": [{"native_id": "room_synth", "name": "合成微信群"}],
                    "contacts": [{"uin": "8101999", "name": "合成微信好友"}],
                },
            ],
        },
    ], workbench_id="wb-book-a", now=now)


def _mount(tmp_path, monkeypatch):
    import imlink.routes as routes

    store = Store(tmp_path / "im_link.json", catalog_rows=_rows())
    monkeypatch.setattr(routes, "store", store)
    app = FastAPI()
    app.include_router(routes.router)
    return store, TestClient(app)


def _bind(store: Store, uid: str) -> str:
    link = store.create({
        "name": "合成通道",
        "mode": "oss",
        "oss": {"workbench_id": "wb-book-a", "account_uid": uid},
    })
    store.activate(link["id"])
    return link["id"]


def _snapshot(path: Path):
    if not path.exists():
        return None
    return path.read_bytes()


def test_current_account_is_saved_alone_and_other_account_stays(tmp_path, monkeypatch):
    """读通讯录只更新当前号，文件里只有好友和 org，权限 0600。"""
    live_link = _snapshot(LIVE_LINK)
    live_commands = _snapshot(LIVE_COMMANDS)
    live_book = _snapshot(LIVE_BOOK)
    store, client = _mount(tmp_path, monkeypatch)
    assert store.address_book_path == tmp_path / "address_book.json"
    assert store.address_book_path.resolve() != LIVE_BOOK.resolve()

    link_id = _bind(store, "81000001")
    first = client.get("/api/im/contacts")
    assert first.status_code == 200
    body = first.json()["data"]
    on_disk = load_entry(store.address_book_path, "wb-book-a", "81000001")
    assert body["contacts"] == on_disk["contacts"]
    assert body["org"] == on_disk["org"]
    assert [item["uin"] for item in on_disk["contacts"]] == ["8101001"]
    assert [item["name"] for item in on_disk["org"]["departments"]] == ["合成总部", "合成分部"]
    assert {item["uin"] for item in on_disk["org"]["members"]} == {"8102001", "8102002"}
    mode = stat.S_IMODE(store.address_book_path.stat().st_mode)
    assert mode == 0o600

    store.update(link_id, {"oss": {"workbench_id": "wb-book-a", "account_uid": "81000002"}})
    store.activate(link_id)
    second = client.get("/api/im/contacts")
    assert second.status_code == 200
    assert [item["name"] for item in second.json()["data"]["org"]["departments"]] == ["合成乙部"]
    kept = load_entry(store.address_book_path, "wb-book-a", "81000001")
    assert [item["name"] for item in kept["org"]["departments"]] == ["合成总部", "合成分部"]
    book = json.loads(store.address_book_path.read_text(encoding="utf-8"))
    assert set(book) == {"entries"}
    assert len(book["entries"]) == 2
    text = store.address_book_path.read_text(encoding="utf-8")
    for token in ("groups", "group_native_ids", "合成微信群", "合成企点群", "wxid_synth", "imap", "deposit"):
        assert token not in text
    for row in book["entries"]:
        assert set(row) == {"workbench_id", "account_uid", "contacts", "org"}
        assert set(row["org"]) == {"departments", "members"}

    link_text = (tmp_path / "im_link.json").read_text(encoding="utf-8")
    assert "合成总部" not in link_text
    assert "8102001" not in link_text
    assert "8101001" not in link_text
    saved_link = json.loads(link_text)
    for link in saved_link["links"]:
        last = link.get("last_test") or {}
        assert "contacts" not in last
        assert "org" not in last
        assert "groups" not in last
    assert _snapshot(LIVE_LINK) == live_link
    assert _snapshot(LIVE_COMMANDS) == live_commands
    assert _snapshot(LIVE_BOOK) == live_book


def test_dirty_fields_and_test_result_do_not_land_in_the_channel_file(tmp_path):
    """测试结果里带上的好友、部门和群，不能写进通道文件。"""
    path = tmp_path / "im_link.json"
    store = Store(path, catalog_rows=_rows())
    link = store.create({
        "name": "合成通道",
        "mode": "oss",
        "oss": {"workbench_id": "wb-book-a", "account_uid": "81000001"},
    })
    store.save_test(link["id"], {
        "passed": True,
        "contacts": [{"uin": "8101001", "name": "合成好友甲"}],
        "contacts_source": "catalog",
        "org": {"departments": [{"id": "d-root", "name": "合成总部"}], "members": []},
        "groups": [{"native_id": "g_synth", "name": "合成企点群"}],
        "account": {"id": "81000001", "self_uid": "81000001", "label": "合成号甲"},
        "steps": [],
    })
    text = path.read_text(encoding="utf-8")
    assert "合成好友甲" not in text
    assert "合成总部" not in text
    assert "合成企点群" not in text
    assert "group_native_ids" not in text
    last = json.loads(text)["links"][0]["last_test"]
    assert "contacts" not in last
    assert "org" not in last

    book = tmp_path / "address_book.json"
    save_entry(
        book,
        "wb-book-a",
        "81000001",
        [{"uin": "8101001", "name": "合成好友甲", "remark": "", "groups": ["g_synth"]}],
        {
            "departments": [{"id": "d-root", "parent_id": "", "name": "合成总部", "order": 0}],
            "members": [{"uin": "8102001", "name": "合成成员甲", "account": "", "department_id": "d-root", "order": 0}],
            "groups": [{"native_id": "room_synth", "name": "合成微信群"}],
            "group_native_ids": ["room_synth"],
        },
    )
    saved = json.loads(book.read_text(encoding="utf-8"))
    assert "groups" not in json.dumps(saved, ensure_ascii=False)
    assert saved["entries"][0]["org"]["members"][0]["uin"] == "8102001"
    assert "group_native_ids" not in json.dumps(saved, ensure_ascii=False)
