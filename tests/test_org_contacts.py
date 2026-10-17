"""组织架构通讯录。只用合成部门和合成 uin，不连接对象存储。"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.oss_catalog import rows_from_catalog
from imlink.store import Store


def _catalog() -> dict:
    return {
        "workbench_id": "wb-synth",
        "updated_at": time.time(),
        "accounts": [
            {
                "channel": "qidian",
                "account_uid": "80001001",
                "account_nick": "合成号甲",
                "contacts": [{"uin": "81001", "name": "合成好友甲"}],
                "org": {
                    "departments": [
                        {"id": "d-sub", "parent_id": "d-root", "name": "合成分部", "order": 0},
                        {"id": "d-root", "parent_id": "", "name": "合成总部", "order": 2},
                        {"id": "d-side", "parent_id": "", "name": "合成别部", "order": 1},
                    ],
                    "members": [
                        {"uin": "82002", "name": "合成成员乙", "account": "", "department_id": "d-sub", "order": 0},
                        {"uin": "82001", "name": "合成成员甲", "account": "synth-a", "department_id": "d-root", "order": 1},
                        {"uin": "82009", "name": "合成跨部门", "account": "", "department_id": "d-root", "order": 0},
                        {"uin": "82009", "name": "合成跨部门", "account": "", "department_id": "d-sub", "order": 2},
                        {"uin": "82003", "name": "合成未分组", "account": "", "department_id": "", "order": 0},
                    ],
                },
            },
            {
                "channel": "qidian",
                "account_uid": "80001002",
                "account_nick": "合成号乙",
                "contacts": [],
                "org": {
                    "departments": [
                        {"id": "d-b", "parent_id": "", "name": "合成乙部", "order": 0},
                    ],
                    "members": [
                        {"uin": "83001", "name": "合成成员丁", "account": "", "department_id": "d-b", "order": 0},
                    ],
                },
            },
        ],
    }


def _other_workbench() -> dict:
    return {
        "workbench_id": "wb-other",
        "updated_at": time.time(),
        "accounts": [
            {
                "channel": "qidian",
                "account_uid": "80001009",
                "account_nick": "合成号旁路",
                "contacts": [],
                "org": {
                    "departments": [
                        {"id": "d-x", "parent_id": "", "name": "旁路部门", "order": 0},
                    ],
                    "members": [
                        {"uin": "89001", "name": "旁路成员", "account": "", "department_id": "d-x", "order": 0},
                    ],
                },
            },
        ],
    }


def _rows() -> list:
    now = time.time()
    return rows_from_catalog([_catalog(), _other_workbench()], workbench_id="wb-synth", now=now)


def _client(tmp_path, rows):
    import imlink.routes as routes
    routes.store = Store(tmp_path / "im_link.json", catalog_rows=rows)
    app = FastAPI()
    app.include_router(routes.router)
    return routes, TestClient(app)


def _bind(store: Store, uid: str) -> str:
    link = store.create({
        "name": "合成通道",
        "mode": "oss",
        "oss": {"workbench_id": "wb-synth", "account_uid": uid},
    })
    store.activate(link["id"])
    return link["id"]


def test_parser_keeps_two_levels_and_members_under_different_departments():
    """合成目录能解析出上下级部门，成员按各自的部门号分开挂。"""
    rows = _rows()
    books = {row["workbench_id"]: row for row in rows}
    acc = next(item for item in books["wb-synth"]["accounts"] if item["account_uid"] == "80001001")
    depts = {item["id"]: item for item in acc["org"]["departments"]}
    assert depts["d-root"]["parent_id"] == ""
    assert depts["d-root"]["name"] == "合成总部"
    assert depts["d-root"]["order"] == 2
    assert depts["d-sub"]["parent_id"] == "d-root"
    assert depts["d-sub"]["name"] == "合成分部"
    assert depts["d-side"]["parent_id"] == ""
    hung: dict[str, list[str]] = {}
    for member in acc["org"]["members"]:
        hung.setdefault(member["department_id"], []).append(member["uin"])
    assert hung["d-root"] == ["82001", "82009"]
    assert hung["d-sub"] == ["82002", "82009"]
    assert hung[""] == ["82003"]
    assert acc["org"]["members"][0]["account"] == ""
    named = next(item for item in acc["org"]["members"] if item["uin"] == "82001")
    assert named["account"] == "synth-a"
    other = next(item for item in books["wb-synth"]["accounts"] if item["account_uid"] == "80001002")
    assert [item["name"] for item in other["org"]["departments"]] == ["合成乙部"]
    assert "合成总部" not in [item["name"] for item in other["org"]["departments"]]


def test_other_account_uid_cannot_read_the_previous_tree(tmp_path):
    """换一个企点号之后，接口里不再出现上一个号的部门和成员。"""
    import imlink.routes as routes
    old = routes.store
    try:
        routes, client = _client(tmp_path, _rows())
        link_id = _bind(routes.store, "80001001")
        first = client.get("/api/im/contacts")
        assert first.status_code == 200
        body = first.json()
        assert body["ok"] is True
        assert body["data"]["account_uid"] == "80001001"
        assert body["data"]["source"] == "catalog"
        names = [item["name"] for item in body["data"]["org"]["departments"]]
        assert names == ["合成分部", "合成总部", "合成别部"]
        assert "合成乙部" not in names
        assert "旁路部门" not in names
        uins = [item["uin"] for item in body["data"]["org"]["members"]]
        assert "83001" not in uins
        assert "89001" not in uins
        assert body["data"]["contacts"] == [{"uin": "81001", "name": "合成好友甲", "remark": ""}]

        routes.store.update(link_id, {"oss": {"workbench_id": "wb-synth", "account_uid": "80001002"}})
        routes.store.activate(link_id)
        second = client.get("/api/im/contacts")
        assert second.status_code == 200
        org = second.json()["data"]["org"]
        assert [item["name"] for item in org["departments"]] == ["合成乙部"]
        assert [item["uin"] for item in org["members"]] == ["83001"]
        dumped = second.text
        assert "合成总部" not in dumped
        assert "合成分部" not in dumped
        assert "82001" not in dumped
        assert "82002" not in dumped
        assert "旁路部门" not in dumped
    finally:
        routes.store = old


def test_missing_org_returns_200_and_empty_tree(tmp_path):
    """账号上没有 org，或部门成员都是空数组时，接口仍是 200，树为空。"""
    import imlink.routes as routes
    old = routes.store
    try:
        missing = {
            "workbench_id": "wb-synth",
            "updated_at": time.time(),
            "accounts": [{
                "channel": "qidian",
                "account_uid": "80001003",
                "account_nick": "合成号丙",
                "contacts": [{"uin": "81003", "name": "合成好友丙"}],
            }],
        }
        rows = rows_from_catalog(missing, workbench_id="wb-synth", now=time.time())
        assert rows[0]["accounts"][0]["org"] == {"departments": [], "members": []}
        routes, client = _client(tmp_path, rows)
        _bind(routes.store, "80001003")
        got = client.get("/api/im/contacts")
        assert got.status_code == 200
        body = got.json()
        assert body["ok"] is True
        assert body["data"]["org"] == {"departments": [], "members": []}
        assert body["data"]["contacts"][0]["uin"] == "81003"

        empty = {
            "workbench_id": "wb-synth",
            "updated_at": time.time(),
            "accounts": [{
                "channel": "qidian",
                "account_uid": "80001004",
                "account_nick": "合成号丁",
                "contacts": [],
                "org": {"departments": [], "members": []},
            }],
        }
        empty_rows = rows_from_catalog(empty, workbench_id="wb-synth", now=time.time())
        assert empty_rows[0]["accounts"][0]["org"] == {"departments": [], "members": []}
        routes.store = Store(tmp_path / "empty.json", catalog_rows=empty_rows)
        _bind(routes.store, "80001004")
        again = client.get("/api/im/contacts")
        assert again.status_code == 200
        assert again.json()["data"]["org"] == {"departments": [], "members": []}
    finally:
        routes.store = old
