"""组织架构独立规格。

只用合成部门名和合成 uin。不访问对象存储，不读 data/im_link.json。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imlink.catalog import contacts_of, org_of
from imlink.oss_catalog import rows_from_catalog
from imlink.store import Store

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "web" / "src" / "lib" / "imLink.ts"
ORG_TREE = ROOT / "web" / "src" / "components" / "OrgTree.vue"
LINK_BAR = ROOT / "web" / "src" / "components" / "ImLinkBar.vue"

EAST = "wb-lab-east"
WEST = "wb-lab-west"
UID_A = "91000001"
UID_B = "91000002"
UID_BARE = "91000003"
UID_EMPTY = "91000004"
UID_LOOSE = "91000005"

ORG_MISSING = "这个账号还没有组织架构"
CONTACTS_MISSING = "这个账号下还没有可读的联系人"


def _forbid_oss(*_args, **_kwargs):
    raise AssertionError("测试不得访问对象存储")


def _org_a() -> dict:
    return {
        "departments": [
            {"id": "dept-hq", "parent_id": "", "name": "合成资金部", "order": 30},
            {"id": "dept-bond", "parent_id": "dept-hq", "name": "合成现券组", "order": 5},
            {"id": "dept-note", "parent_id": "dept-hq", "name": "合成票据组", "order": 5},
            {"id": "dept-early", "parent_id": "dept-hq", "name": "合成早会组", "order": 1},
            {"id": "dept-fx", "parent_id": "", "name": "合成外汇组", "order": 10},
        ],
        "members": [
            {"uin": "9102001", "name": "合成员工甲", "account": "acct-a", "department_id": "dept-hq", "order": 3},
            {"uin": "9102008", "name": "合成员工丙", "account": "", "department_id": "dept-hq", "order": 2},
            {"uin": "9102009", "name": "合成跨组", "account": "acct-x", "department_id": "dept-hq", "order": 1},
            {"uin": "9102002", "name": "合成员工乙", "account": "acct-b", "department_id": "dept-bond", "order": 1},
            {"uin": "9102009", "name": "合成跨组", "account": "acct-x", "department_id": "dept-bond", "order": 4},
            {"uin": "9102010", "name": "合成员工戊", "account": "", "department_id": "dept-early", "order": 0},
            {"uin": "9102098", "name": "合成游离甲", "account": "", "department_id": "", "order": 0},
            {"uin": "9102099", "name": "合成游离乙", "account": "", "department_id": "", "order": 2},
        ],
    }


def _catalog() -> list:
    org = _org_a()
    return [
        {
            "workbench_id": EAST,
            "updated_at": time.time(),
            "accounts": [
                {
                    "channel": "qidian",
                    "account_uid": UID_A,
                    "account_nick": "合成询价甲",
                    "contacts": [{"uin": "9101001", "name": "合成好友甲"}],
                    "org": org,
                },
                {
                    "channel": "qidian",
                    "account_uid": UID_B,
                    "account_nick": "合成询价乙",
                    "contacts": [{"uin": "9101002", "name": "合成好友乙"}],
                    "org": {
                        "departments": [
                            {"id": "dept-b", "parent_id": "", "name": "合成乙部", "order": 0},
                        ],
                        "members": [
                            {"uin": "9103001", "name": "合成员工丁", "account": "acct-d", "department_id": "dept-b", "order": 0},
                        ],
                    },
                },
                {
                    "channel": "qidian",
                    "account_uid": UID_BARE,
                    "account_nick": "合成询价丙",
                    "contacts": [{"uin": "9101003", "name": "合成好友丙"}],
                },
                {
                    "channel": "qidian",
                    "account_uid": UID_EMPTY,
                    "account_nick": "合成询价丁",
                    "contacts": [],
                    "org": {"departments": [], "members": []},
                },
                {
                    "channel": "qidian",
                    "account_uid": UID_LOOSE,
                    "account_nick": "合成询价戊",
                    "contacts": [],
                    "org": {
                        "departments": [],
                        "members": [
                            {"uin": "9102097", "name": "合成未挂靠", "account": "", "department_id": "", "order": 1},
                        ],
                    },
                },
                {
                    "channel": "wechat",
                    "account_uid": "wxid_synth",
                    "account_nick": "合成微信",
                    "group_native_ids": ["9108chatroom"],
                    "groups": [{"native_id": "9108chatroom", "name": "合成固收群"}],
                    "contacts": [{"uin": "9101999", "name": "合成微信好友"}],
                    "org": {
                        "departments": [
                            {"id": "wx-dept", "parent_id": "", "name": "合成微信部", "order": 0},
                        ],
                        "members": [],
                    },
                },
            ],
        },
        {
            "workbench_id": WEST,
            "updated_at": time.time(),
            "accounts": [
                {
                    "channel": "qidian",
                    "account_uid": UID_A,
                    "account_nick": "合成同号异台",
                    "contacts": [{"uin": "9101888", "name": "合成异台好友"}],
                    "org": {
                        "departments": [
                            {"id": "dept-w", "parent_id": "", "name": "合成西部分部", "order": 0},
                        ],
                        "members": [
                            {"uin": "9103888", "name": "合成西部员工", "account": "", "department_id": "dept-w", "order": 0},
                        ],
                    },
                },
            ],
        },
    ]


def _rows() -> list:
    return rows_from_catalog(_catalog(), workbench_id="wb-unused", now=time.time())


def _account(rows: list, workbench_id: str, account_uid: str) -> dict:
    book = next(row for row in rows if row["workbench_id"] == workbench_id)
    return next(item for item in book["accounts"] if item["account_uid"] == account_uid)


def _mount(tmp_path, rows, monkeypatch):
    monkeypatch.setattr("imlink.oss_catalog.load_live_catalog", _forbid_oss)
    monkeypatch.setattr("imlink.oss_catalog.http_get", _forbid_oss)
    import imlink.routes as routes

    store = Store(tmp_path / "im_link.json", catalog_rows=rows)
    monkeypatch.setattr(routes, "store", store)
    app = FastAPI()
    app.include_router(routes.router)
    return store, TestClient(app)


def _bind(store: Store, workbench_id: str, account_uid: str) -> str:
    link = store.create({
        "name": "合成通道",
        "mode": "oss",
        "oss": {"workbench_id": workbench_id, "account_uid": account_uid},
    })
    store.activate(link["id"])
    return link["id"]


def _assert_shape(org: dict) -> None:
    assert set(org) == {"departments", "members"}
    for dept in org["departments"]:
        assert set(dept) == {"id", "parent_id", "name", "order"}
        assert isinstance(dept["parent_id"], str)
        assert isinstance(dept["order"], int) and not isinstance(dept["order"], bool)
    for member in org["members"]:
        assert set(member) == {"uin", "name", "account", "department_id", "order"}
        assert isinstance(member["department_id"], str)
        assert isinstance(member["order"], int) and not isinstance(member["order"], bool)


def test_catalog_keeps_org_apart_from_private_contacts():
    """目录账号上，组织架构和私人好友分开，两人跨部门保留成两行。"""
    rows = _rows()
    acc = _account(rows, EAST, UID_A)
    assert [item["uin"] for item in acc["contacts"]] == ["9101001"]
    assert "9102001" not in {item["uin"] for item in acc["contacts"]}
    _assert_shape(acc["org"])
    depts = {item["id"]: item for item in acc["org"]["departments"]}
    assert depts["dept-hq"]["parent_id"] == ""
    assert depts["dept-hq"]["name"] == "合成资金部"
    assert depts["dept-hq"]["order"] == 30
    assert depts["dept-fx"]["parent_id"] == ""
    assert depts["dept-bond"]["parent_id"] == "dept-hq"
    assert depts["dept-bond"]["name"] == "合成现券组"
    assert depts["dept-early"]["order"] == 1
    same = [item for item in acc["org"]["members"] if item["uin"] == "9102009"]
    assert sorted(item["department_id"] for item in same) == ["dept-bond", "dept-hq"]
    assert all(item["account"] == "acct-x" for item in same)
    loose = [item for item in acc["org"]["members"] if item["department_id"] == ""]
    assert {item["uin"] for item in loose} == {"9102098", "9102099"}
    named = next(item for item in acc["org"]["members"] if item["uin"] == "9102001")
    assert named["account"] == "acct-a"
    assert named["department_id"] == "dept-hq"

    other = _account(rows, EAST, UID_B)
    assert [item["name"] for item in other["org"]["departments"]] == ["合成乙部"]
    assert "合成资金部" not in {item["name"] for item in other["org"]["departments"]}
    west = _account(rows, WEST, UID_A)
    assert [item["name"] for item in west["org"]["departments"]] == ["合成西部分部"]
    assert [item["uin"] for item in west["contacts"]] == ["9101888"]
    assert all(item["account_uid"] != "wxid_synth" for book in rows for item in book["accounts"])

    parsed = org_of(rows, EAST, UID_A)
    _assert_shape(parsed)
    assert contacts_of(rows, EAST, UID_A)[0]["uin"] == "9101001"
    assert org_of(rows, EAST, UID_B)["departments"][0]["name"] == "合成乙部"
    assert org_of(rows, WEST, UID_A)["members"][0]["uin"] == "9103888"
    assert org_of(rows, EAST, UID_BARE) == {"departments": [], "members": []}
    assert org_of(rows, EAST, UID_EMPTY) == {"departments": [], "members": []}
    loose_only = org_of(rows, EAST, UID_LOOSE)
    assert loose_only["departments"] == []
    assert loose_only["members"][0]["uin"] == "9102097"
    assert loose_only["members"][0]["department_id"] == ""


def test_bound_pair_cannot_read_another_accounts_tree(tmp_path, monkeypatch):
    """公网上报只读当前通道绑定的工作台和企点号。换号或换台都读不到前一棵树。"""
    rows = _rows()
    store, client = _mount(tmp_path, rows, monkeypatch)
    assert store.path.parent == tmp_path
    assert store.path.resolve() != (ROOT / "data" / "im_link.json").resolve()
    link_id = _bind(store, EAST, UID_A)

    first = client.get("/api/im/contacts")
    assert first.status_code == 200
    body = first.json()
    assert body["ok"] is True
    assert body["data"]["source"] == "catalog"
    assert body["data"]["account_uid"] == UID_A
    org = body["data"]["org"]
    _assert_shape(org)
    names = {item["name"] for item in org["departments"]}
    assert names == {"合成资金部", "合成现券组", "合成票据组", "合成早会组", "合成外汇组"}
    assert {item["uin"] for item in body["data"]["contacts"]} == {"9101001"}
    assert "9102001" not in {item["uin"] for item in body["data"]["contacts"]}
    leaked = (
        "合成乙部", "合成西部分部", "合成微信部", "合成固收群", "9108chatroom",
        "9103001", "9103888", "9101888", "9101999", "group_native_ids",
    )
    assert all(token not in first.text for token in leaked)

    store.update(link_id, {"oss": {"workbench_id": EAST, "account_uid": UID_B}})
    store.activate(link_id)
    second = client.get("/api/im/contacts")
    assert second.status_code == 200
    switched = second.json()["data"]
    assert switched["account_uid"] == UID_B
    assert [item["name"] for item in switched["org"]["departments"]] == ["合成乙部"]
    assert [item["uin"] for item in switched["org"]["members"]] == ["9103001"]
    assert [item["uin"] for item in switched["contacts"]] == ["9101002"]
    gone = ("合成资金部", "合成现券组", "9102001", "9102009", "9101001", "合成西部分部")
    assert all(token not in second.text for token in gone)

    store.update(link_id, {"oss": {"workbench_id": WEST, "account_uid": UID_A}})
    store.activate(link_id)
    third = client.get("/api/im/contacts")
    assert third.status_code == 200
    moved = third.json()["data"]
    assert moved["account_uid"] == UID_A
    assert [item["name"] for item in moved["org"]["departments"]] == ["合成西部分部"]
    assert [item["uin"] for item in moved["org"]["members"]] == ["9103888"]
    assert [item["uin"] for item in moved["contacts"]] == ["9101888"]
    assert all(token not in third.text for token in ("合成资金部", "9102001", "9101001", "合成乙部"))


def test_missing_org_is_200_and_empty_tree_not_a_contacts_gap(tmp_path, monkeypatch):
    """缺 org、空数组都是 200 且树为空，不能说成还没有可读的联系人。"""
    store, client = _mount(tmp_path, _rows(), monkeypatch)

    link_id = _bind(store, EAST, UID_BARE)
    bare = client.get("/api/im/contacts")
    assert bare.status_code == 200
    bare_body = bare.json()
    assert bare_body["ok"] is True
    assert bare_body["data"]["org"] == {"departments": [], "members": []}
    assert [item["uin"] for item in bare_body["data"]["contacts"]] == ["9101003"]
    assert CONTACTS_MISSING not in bare.text
    assert "合成资金部" not in bare.text

    store.update(link_id, {"oss": {"workbench_id": EAST, "account_uid": UID_EMPTY}})
    store.activate(link_id)
    empty = client.get("/api/im/contacts")
    assert empty.status_code == 200
    assert empty.json()["ok"] is True
    assert empty.json()["data"]["org"] == {"departments": [], "members": []}
    assert empty.json()["data"]["contacts"] == []
    assert CONTACTS_MISSING not in empty.text

    store.update(link_id, {"oss": {"workbench_id": EAST, "account_uid": UID_LOOSE}})
    store.activate(link_id)
    loose = client.get("/api/im/contacts")
    assert loose.status_code == 200
    loose_org = loose.json()["data"]["org"]
    assert loose_org["departments"] == []
    assert loose_org["members"][0]["department_id"] == ""
    assert loose_org["members"][0]["uin"] == "9102097"
    assert CONTACTS_MISSING not in loose.text


def _book_entries(path: Path) -> list:
    assert path.is_file(), "通讯录应写入单独的 address_book.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"entries"}
    entries = data["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert set(entry) == {"workbench_id", "account_uid", "contacts", "org"}
        assert set(entry["org"]) == {"departments", "members"}
        for person in entry["contacts"]:
            assert set(person) == {"uin", "name", "remark"}
            assert "native_id" not in person
        for dept in entry["org"]["departments"]:
            assert set(dept) == {"id", "parent_id", "name", "order"}
        for member in entry["org"]["members"]:
            assert set(member) == {"uin", "name", "account", "department_id", "order"}
    return entries


def _entry(entries: list, workbench_id: str, account_uid: str) -> dict:
    found = [
        item for item in entries
        if item["workbench_id"] == workbench_id and item["account_uid"] == account_uid
    ]
    assert len(found) == 1
    return found[0]


def test_address_book_file_is_qidian_only_and_leaves_other_files(tmp_path, monkeypatch):
    """通讯录只进单独文件。不改通道、指令和存款微信群配置，换号不覆盖别人。"""
    raw = _catalog()
    store, client = _mount(tmp_path, raw, monkeypatch)
    commands = tmp_path / "oss_commands.json"
    commands.write_text('[{"cmd_id":"synth-keep","op":"send_text"}]\n', encoding="utf-8")
    import imlink.routes as routes
    from imlink.commands import CommandLog
    monkeypatch.setattr(routes, "log", CommandLog(commands))
    deposit = tmp_path / "deposit_wechat.json"
    deposit_body = {
        "workbench_id": EAST,
        "wxid": "wxid_synth",
        "channel": "wechat",
        "group_native_ids": ["9108chatroom"],
        "groups": [{"native_id": "9108chatroom", "name": "合成固收群"}],
        "transport": "oss",
    }
    deposit.write_text(json.dumps(deposit_body, ensure_ascii=False, indent=2), encoding="utf-8")
    real_book = ROOT / "data" / "address_book.json"
    real_existed = real_book.exists()
    real_stamp = real_book.stat().st_mtime_ns if real_existed else None
    link_id = _bind(store, EAST, UID_A)
    link_before = store.path.read_bytes()
    cmd_before = commands.read_bytes()
    deposit_before = deposit.read_bytes()

    first = client.get("/api/im/contacts")
    assert first.status_code == 200
    book = tmp_path / "address_book.json"
    book.resolve().relative_to(tmp_path.resolve())
    assert book.resolve() != real_book.resolve()
    entries = _book_entries(book)
    saved = _entry(entries, EAST, UID_A)
    assert [item["uin"] for item in saved["contacts"]] == ["9101001"]
    assert {item["name"] for item in saved["org"]["departments"]} == {
        "合成资金部", "合成现券组", "合成票据组", "合成早会组", "合成外汇组",
    }
    assert sum(item["uin"] == "9102009" for item in saved["org"]["members"]) == 2
    text = book.read_text(encoding="utf-8")
    for token in ("9108chatroom", "group_native_ids", "wechat", "合成固收群", "native_id", "合成微信", "9101999", "合成微信部"):
        assert token not in text
    assert store.path.read_bytes() == link_before
    assert commands.read_bytes() == cmd_before
    assert deposit.read_bytes() == deposit_before
    link_text = store.path.read_text(encoding="utf-8")
    cmd_text = commands.read_text(encoding="utf-8")
    for token in ("合成资金部", "9101001", "9102001", "9102009", "departments", "members", "合成固收群"):
        assert token not in link_text
        assert token not in cmd_text
    assert json.loads(deposit.read_text(encoding="utf-8"))["groups"] == deposit_body["groups"]
    assert "9102001" not in deposit.read_text(encoding="utf-8")

    store.update(link_id, {"oss": {"workbench_id": EAST, "account_uid": UID_B}})
    store.activate(link_id)
    link_before = store.path.read_bytes()
    second = client.get("/api/im/contacts")
    assert second.status_code == 200
    assert store.path.read_bytes() == link_before
    assert commands.read_bytes() == cmd_before
    assert deposit.read_bytes() == deposit_before
    entries = _book_entries(book)
    assert _entry(entries, EAST, UID_A)["org"]["departments"][0]["id"]
    assert {item["name"] for item in _entry(entries, EAST, UID_A)["org"]["departments"]} == {
        "合成资金部", "合成现券组", "合成票据组", "合成早会组", "合成外汇组",
    }
    other = _entry(entries, EAST, UID_B)
    assert [item["name"] for item in other["org"]["departments"]] == ["合成乙部"]
    assert [item["uin"] for item in other["contacts"]] == ["9101002"]
    assert "合成资金部" not in json.dumps(other, ensure_ascii=False)

    store.update(link_id, {"oss": {"workbench_id": EAST, "account_uid": UID_BARE}})
    store.activate(link_id)
    bare = client.get("/api/im/contacts")
    assert bare.status_code == 200
    assert bare.json()["data"]["org"] == {"departments": [], "members": []}
    bare_saved = _entry(_book_entries(book), EAST, UID_BARE)
    assert bare_saved["org"] == {"departments": [], "members": []}
    assert [item["uin"] for item in bare_saved["contacts"]] == ["9101003"]
    assert _entry(_book_entries(book), EAST, UID_A)["contacts"][0]["uin"] == "9101001"

    store.update(link_id, {"oss": {"workbench_id": WEST, "account_uid": UID_A}})
    store.activate(link_id)
    moved = client.get("/api/im/contacts")
    assert moved.status_code == 200
    entries = _book_entries(book)
    assert [item["uin"] for item in _entry(entries, WEST, UID_A)["contacts"]] == ["9101888"]
    assert _entry(entries, WEST, UID_A)["org"]["departments"][0]["name"] == "合成西部分部"
    assert "合成西部分部" not in json.dumps(_entry(entries, EAST, UID_A), ensure_ascii=False)

    store.save_test(link_id, {
        "passed": True,
        "note": "",
        "steps": [],
        "contacts": [{"uin": "9101001", "name": "合成好友甲"}],
        "contacts_source": "catalog",
        "org": _org_a(),
        "account": {"id": UID_A, "self_uid": UID_A, "label": "合成询价甲"},
        "accounts": [],
    })
    persisted = store.path.read_text(encoding="utf-8")
    for token in ("9101001", "合成好友甲", "合成资金部", "9102001", "departments", "members"):
        assert token not in persisted
    assert commands.read_bytes() == cmd_before
    assert deposit.read_bytes() == deposit_before
    assert real_book.exists() == real_existed
    if real_existed:
        assert real_book.stat().st_mtime_ns == real_stamp


_RUNNER = r"""
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const mod = await import(pathToFileURL(process.argv[2]).href)
for (const name of ['buildOrgTree', 'visibleOrgBranch', 'orgEmptyLabel', 'orgReadNotice']) {
  if (typeof mod[name] !== 'function') {
    console.error('缺少可单测函数 ' + name)
    process.exit(2)
  }
}
const payload = JSON.parse(readFileSync(process.argv[3], 'utf8'))

function packTree(nodes) {
  return nodes.map((node) => ({
    id: node.id,
    name: node.name,
    memberUins: (node.members || []).map((item) => item.uin),
    children: packTree(node.children || []),
  }))
}

function packVisible(nodes) {
  return nodes.map((node) => ({
    id: node.id,
    name: node.name,
    childCount: node.childCount,
    memberCount: node.memberCount,
    memberUins: (node.members || []).map((item) => item.uin),
    children: packVisible(node.children || []),
  }))
}

const tree = mod.buildOrgTree(payload.org)
const hqId = tree.find((node) => node.name === '合成资金部')?.id
const bond = tree.flatMap((node) => node.children || []).find((node) => node.name === '合成现券组')
console.log(JSON.stringify({
  tree: packTree(tree),
  closed: packVisible(mod.visibleOrgBranch(tree, [])),
  openHq: packVisible(mod.visibleOrgBranch(tree, hqId ? [hqId] : [])),
  openBond: packVisible(mod.visibleOrgBranch(tree, [hqId, bond?.id].filter(Boolean))),
  emptyLabel: mod.orgEmptyLabel(payload.empty),
  nullLabel: mod.orgEmptyLabel(null),
  looseLabel: mod.orgEmptyLabel(payload.looseOnly),
  filledLabel: mod.orgEmptyLabel(payload.org),
  emptyNotice: mod.orgReadNotice(payload.empty),
  looseNotice: mod.orgReadNotice(payload.looseOnly),
  oneNotice: mod.orgReadNotice(payload.oneDept),
}))
"""


def _flat_members(nodes: list) -> list[str]:
    found = []
    for node in nodes:
        found.extend(node["memberUins"])
        found.extend(_flat_members(node["children"]))
    return found


def test_frontend_tree_sorts_and_renders_only_the_open_branch(tmp_path):
    """前端按部门层级挂人，用 order 排序；没收起的那一支不把成员交出去渲染。"""
    org = _org_a()
    crowd = []
    for index in range(40):
        crowd.append({
            "uin": f"9105{index:03d}",
            "name": f"合成现券员{index}",
            "account": "",
            "department_id": "dept-bond",
            "order": 10 + index,
        })
    org["members"] = [*org["members"], *crowd]
    payload = {
        "org": org,
        "empty": {"departments": [], "members": []},
        "looseOnly": {
            "departments": [],
            "members": [
                {"uin": "9102097", "name": "合成未挂靠", "account": "", "department_id": "", "order": 1},
            ],
        },
        "oneDept": {
            "departments": [
                {"id": "dept-only", "parent_id": "", "name": "合成单独部", "order": 0},
            ],
            "members": [],
        },
    }
    org_path = tmp_path / "org.json"
    runner = tmp_path / "run.mts"
    org_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    runner.write_text(_RUNNER, encoding="utf-8")
    # The application uses bundler-style imports; bundle the real module just
    # as Vite does instead of relying on Node's TypeScript import resolution.
    bundled = tmp_path / "imLink.mjs"
    build = subprocess.run(
        [str(ROOT / "web" / "node_modules" / ".bin" / "esbuild"), str(LIB),
         "--bundle", "--platform=node", "--format=esm", "--define:import.meta.env={}",
         "--outfile=" + str(bundled)],
        cwd=str(ROOT / "web"), capture_output=True, text=True, timeout=30, check=False,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    proc = subprocess.run(
        ["node", "--experimental-strip-types", str(runner), str(bundled), str(org_path)],
        cwd=str(ROOT / "web"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    got = json.loads(proc.stdout)
    roots = got["tree"]
    assert [item["name"] for item in roots] == ["合成外汇组", "合成资金部", "未分组"]
    fx, hq, loose = roots
    assert fx["children"] == []
    assert fx["memberUins"] == []
    assert [item["name"] for item in hq["children"]] == ["合成早会组", "合成现券组", "合成票据组"]
    assert hq["memberUins"] == ["9102009", "9102008", "9102001"]
    early, bond, note = hq["children"]
    assert early["memberUins"] == ["9102010"]
    assert note["memberUins"] == []
    assert bond["memberUins"][:2] == ["9102002", "9102009"]
    assert bond["memberUins"][2:] == [f"9105{index:03d}" for index in range(40)]
    assert loose["name"] == "未分组"
    assert loose["memberUins"] == ["9102098", "9102099"]
    assert "9101001" not in _flat_members(roots)

    closed_members = _flat_members(got["closed"])
    assert closed_members == []
    assert got["closed"][1]["childCount"] == 3
    assert got["closed"][1]["memberCount"] == 3
    assert got["closed"][1]["children"] == []

    open_hq_members = _flat_members(got["openHq"])
    assert open_hq_members == ["9102009", "9102008", "9102001"]
    assert [item["name"] for item in got["openHq"][1]["children"]] == ["合成早会组", "合成现券组", "合成票据组"]
    assert "9105000" not in open_hq_members

    open_bond_members = _flat_members(got["openBond"])
    assert "9102002" in open_bond_members
    assert "9105000" in open_bond_members
    assert "9102010" not in open_bond_members

    assert got["emptyLabel"] == ORG_MISSING
    assert got["nullLabel"] == ORG_MISSING
    assert got["looseLabel"] == ""
    assert got["filledLabel"] == ""
    assert got["emptyNotice"] == ORG_MISSING
    assert got["looseNotice"] == "已读取组织架构"
    assert got["oneNotice"] == "已读取组织架构，1 个部门"
    for text in (
        got["emptyLabel"], got["nullLabel"], got["looseLabel"], got["filledLabel"],
        got["emptyNotice"], got["looseNotice"], got["oneNotice"],
    ):
        assert CONTACTS_MISSING not in text

    tree_view = ORG_TREE.read_text(encoding="utf-8")
    bar = LINK_BAR.read_text(encoding="utf-8")
    assert "visibleOrgBranch" in tree_view
    assert "orgEmptyLabel" in bar
    assert "orgReadNotice" in bar
    assert CONTACTS_MISSING in bar
