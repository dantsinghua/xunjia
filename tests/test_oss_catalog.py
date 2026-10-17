"""公网目录：假封套、假传输。不连接对象存储，不读取真实密钥。"""
from __future__ import annotations

import base64
import json
import os
from urllib.request import Request

from imlink.crypto import seal
from imlink.oss_catalog import (
    CatalogError,
    authorization,
    catalog_body,
    decode_seal_key,
    http_get,
    index_object_name,
    load_live_catalog,
    load_qidian_book,
    object_url,
    open_envelope,
)
from imlink.store import Store

_KEY = bytes.fromhex("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
_OTHER = b"fedcba9876543210fedcba9876543210"
_ACCESS_ID = "AKIDunitcatalog"
_ACCESS_SECRET = "unit-secret-not-real"
_UID = "3007378246"


def _catalog() -> dict:
    return {
        "workbench_id": "anlin-dev",
        "updated_at": 1_760_000_000,
        "accounts": [
            {
                "channel": "qidian",
                "account_uid": _UID,
                "account_nick": "企点示例",
                "label": "不要出现的备注",
                "contacts": [{"uin": "41501", "name": "王磊"}],
            },
            {"channel": "wechat", "account_uid": "wxid_demo", "account_nick": "微信示例"},
        ],
    }


def _envelope(xunjia_plain: bytes, deposit_plain: bytes = b'{"workbench_id":"other"}') -> bytes:
    body = {
        "v": 1,
        "seals": {
            "deposit": base64.b64encode(seal(_OTHER, deposit_plain)).decode("ascii"),
            "xunjia": base64.b64encode(seal(_KEY, xunjia_plain)).decode("ascii"),
        },
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _transport(status: int, body: bytes, seen: list):
    def get(url: str, headers: dict, timeout: float):
        seen.append({"url": url, "headers": headers, "timeout": timeout})
        req = Request(url, headers=headers, method="GET")
        assert req.get_method() == "GET"
        return status, body
    return get


def _use_env():
    old = {name: os.environ.get(name) for name in (
        "OSSBOX_KEY_XUNJIA", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET",
        "OSS_ENDPOINT", "OSS_BUCKET", "OSS_WORKBENCH_ID",
    )}
    os.environ["OSSBOX_KEY_XUNJIA"] = _KEY.hex()
    os.environ["OSS_ACCESS_KEY_ID"] = _ACCESS_ID
    os.environ["OSS_ACCESS_KEY_SECRET"] = _ACCESS_SECRET
    os.environ["OSS_ENDPOINT"] = "https://oss-cn-hangzhou.aliyuncs.com"
    os.environ["OSS_BUCKET"] = "qtrade-nyoss-lab"
    os.environ["OSS_WORKBENCH_ID"] = "anlin-dev"
    return old


def _restore(old: dict) -> None:
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_hex_key_decodes_and_bad_length_hides_the_text():
    assert decode_seal_key(_KEY.hex()) == _KEY
    try:
        decode_seal_key("zz")
    except CatalogError as exc:
        assert "zz" not in str(exc)
        assert "长度无效" in str(exc)
    else:
        raise AssertionError("短密钥应当拒绝")


def test_envelope_keeps_only_qidian_on_the_named_workbench():
    opened = open_envelope(_envelope(json.dumps(_catalog(), ensure_ascii=False).encode("utf-8")), _KEY)
    assert opened["workbench_id"] == "anlin-dev"
    assert opened["accounts"][1]["channel"] == "wechat"


def test_wrong_key_does_not_return_plaintext():
    blob = _envelope(json.dumps(_catalog(), ensure_ascii=False).encode("utf-8"))
    try:
        open_envelope(blob, _OTHER)
    except CatalogError as exc:
        text = str(exc)
        assert _UID not in text
        assert "企点示例" not in text
        assert "解不开" in text
    else:
        raise AssertionError("错误密钥应当失败")


def test_live_loader_uses_get_and_hides_secrets():
    seen = []
    old = _use_env()
    try:
        rows = load_live_catalog(
            transport=_transport(200, _envelope(json.dumps(_catalog()).encode()), seen),
            now=1_760_000_100,
        )
    finally:
        _restore(old)
    assert len(seen) == 1
    assert seen[0]["url"] == object_url(
        "https://oss-cn-hangzhou.aliyuncs.com", "qtrade-nyoss-lab", "wb/anlin-dev/index.json",
    )
    assert seen[0]["headers"]["Authorization"].startswith("OSS " + _ACCESS_ID + ":")
    assert "PUT" not in seen[0]["url"]
    dumped = json.dumps(rows, ensure_ascii=False)
    assert _UID in dumped
    assert rows[0]["workbench_id"] == "anlin-dev"
    assert [a["account_uid"] for a in rows[0]["accounts"]] == [_UID]
    assert "wxid_demo" not in dumped
    assert "不要出现的备注" not in dumped
    assert _KEY.hex() not in dumped
    assert _ACCESS_SECRET not in dumped
    assert _ACCESS_ID not in dumped
    body = catalog_body(rows, "", key_ready=True)
    assert body["ok"] is True
    assert "hint" not in body
    rendered = json.dumps(body, ensure_ascii=False)
    assert _KEY.hex() not in rendered
    assert _ACCESS_SECRET not in rendered


def test_选号只读总文件不下载微信群和企点通讯录():
    seen: list[str] = []
    index = {
        "workbench_id": "anlin-dev",
        "updated_at": 1_760_000_000,
        "accounts": [
            {
                "channel": "qidian",
                "account_uid": _UID,
                "account_nick": "企点示例",
                "book": "qd90/qidian/3007378246/contacts.json",
            },
            {
                "channel": "wechat",
                "account_uid": "wxid_demo",
                "account_nick": "微信示例",
                "book": "wx01/wechat/wxid_demo/groups.json",
            },
        ],
    }
    old = _use_env()
    try:
        rows = load_live_catalog(
            transport=_transport(200, _envelope(json.dumps(index).encode()), seen),
            now=1_760_000_100,
        )
    finally:
        _restore(old)
    assert len(seen) == 1
    assert seen[0]["url"].endswith("/" + index_object_name("anlin-dev"))
    assert "groups.json" not in seen[0]["url"]
    assert "contacts.json" not in seen[0]["url"]
    assert [item["account_uid"] for item in rows[0]["accounts"]] == [_UID]


def test_download_failure_is_specific_and_not_a_fake_workbench():
    old = _use_env()
    try:
        try:
            load_live_catalog(transport=_transport(404, b"", []), now=1)
        except CatalogError as exc:
            assert str(exc) == "工作台账号列表还没上报"
        else:
            raise AssertionError("404 应当失败")
        try:
            load_live_catalog(transport=_transport(403, b"<Error>secret</Error>", []), now=1)
        except CatalogError as exc:
            assert str(exc) == "对象存储拒绝读取目录"
            assert "secret" not in str(exc)
        else:
            raise AssertionError("403 应当失败")
    finally:
        _restore(old)
    body = catalog_body([], "目录对象还不存在", key_ready=True)
    assert body["ok"] is True
    assert body["data"]["workbenches"] == []
    assert body["hint"] == "目录对象还不存在"
    assert "wb-nanjing" not in json.dumps(body)


def test_missing_key_stays_empty_and_injected_catalog_does_not_fetch(tmp_path, monkeypatch):
    monkeypatch.delenv("OSSBOX_KEY_XUNJIA", raising=False)

    def boom():
        raise AssertionError("不应访问对象存储")

    monkeypatch.setattr("imlink.oss_catalog.load_live_catalog", boom)
    bare = Store(tmp_path / "bare.json")
    assert bare.catalog_rows() == []
    assert bare.catalog_error() == ""
    monkeypatch.setenv("OSSBOX_KEY_XUNJIA", _KEY.hex())
    injected = Store(tmp_path / "inj.json", catalog_rows=[])
    assert injected.catalog_rows() == []
    assert injected.catalog_error() == ""
    no_key = catalog_body([], "", key_ready=False)
    assert no_key["hint"] == "还没有配置公网连接，不能选择工作台"


def test_signature_covers_get_and_object_key():
    date = "Mon, 12 Oct 2026 00:00:00 GMT"
    header = authorization(_ACCESS_ID, _ACCESS_SECRET, date, "qtrade-nyoss-lab", "wb/anlin-dev/catalog.json")
    assert header.startswith("OSS " + _ACCESS_ID + ":")
    again = authorization(_ACCESS_ID, _ACCESS_SECRET, date, "qtrade-nyoss-lab", "wb/anlin-dev/catalog.json")
    assert header == again
    other = authorization(_ACCESS_ID, _ACCESS_SECRET, date, "qtrade-nyoss-lab", "wb/anlin-dev/other.json")
    assert other != header


def test_saved_oss_account_becomes_monitor_current(tmp_path):
    """目录里有已保存的企点号时，监控直接接入该号，不停留在未测通。"""
    import time

    rows = [{
        "workbench_id": "anlin-dev",
        "updated_at": time.time(),
        "accounts": [{
            "channel": "qidian",
            "account_uid": _UID,
            "account_nick": "",
            "groups": [],
        }],
    }]
    path = tmp_path / "im_link.json"
    path.write_text(json.dumps({
        "active_id": "",
        "links": [{
            "id": "lk_real",
            "name": "杭州联调",
            "mode": "oss",
            "account_id": "",
            "oss": {
                "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
                "bucket": "qtrade-nyoss-lab",
                "workbench_id": "anlin-dev",
                "account_uid": _UID,
            },
        }],
    }, ensure_ascii=False), encoding="utf-8")
    view = Store(path, catalog_rows=rows).list_public()
    assert view["active_id"] == "lk_real"
    link = view["links"][0]
    assert link["tested"] is True
    assert link["active"] is True
    assert link["account_uid"] == _UID
    assert link["workbench_id"] == "anlin-dev"
    assert "南银" not in json.dumps(link, ensure_ascii=False)


def test_http_get_reads_a_body_larger_than_two_megabytes(monkeypatch):
    payload = b'{"v":1}' + (b"x" * 2_500_000)

    def fake_urlopen(req, timeout=0):
        class Resp:
            status = 200

            def read(self, n=-1):
                if not hasattr(self, "_off"):
                    self._off = 0
                if self._off >= len(payload):
                    return b""
                take = len(payload) - self._off if n is None or n < 0 else min(n, len(payload) - self._off)
                chunk = payload[self._off:self._off + take]
                self._off += take
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Resp()

    monkeypatch.setattr("imlink.oss_catalog._open_request", fake_urlopen)
    status, body = http_get("https://example.invalid/wb/anlin-dev/catalog.json", {"Date": "x"}, 5)
    assert status == 200
    assert body == payload
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["method"] = req.get_method()
        captured["timeout"] = timeout

        class Resp:
            status = 200

            def read(self, n=-1):
                if getattr(self, "_done", False):
                    return b""
                self._done = True
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Resp()

    monkeypatch.setattr("imlink.oss_catalog._open_request", fake_urlopen)
    status, body = http_get("https://example.invalid/wb/anlin-dev/catalog.json", {"Date": "x"}, 5)
    assert status == 200 and body == b"{}"
    assert captured["method"] == "GET"


def test_读取通讯录只下载当前企点号且不碰微信群():
    """总索引不含组织正文。读取时只下载这个企点号的 contacts.json。"""
    index = {
        "v": 1,
        "workbench_id": "anlin-dev",
        "accounts": [
            {
                "channel": "qidian",
                "account_uid": _UID,
                "book": "qidian/3007378246/contacts.json",
            },
            {
                "channel": "wechat",
                "account_uid": "wxid_demo",
                "book": "wechat/wxid_demo/groups.json",
            },
        ],
    }
    book = {
        "v": 1,
        "channel": "qidian",
        "account_uid": _UID,
        "workbench_id": "anlin-dev",
        "contacts": [{"uin": "9101001", "name": "合成甲"}],
        "org": {
            "departments": [{"id": "d1", "parent_id": "", "name": "合成资金部", "order": 1}],
            "members": [{"uin": "9102001", "name": "合成乙", "account": "", "department_id": "d1", "order": 1}],
        },
    }
    seen: list[str] = []

    def transport(url, headers, timeout):
        seen.append(url)
        if url.endswith("/index.json"):
            return 200, _envelope(json.dumps(index).encode())
        if url.endswith("/qidian/3007378246/contacts.json"):
            return 200, _envelope(json.dumps(book).encode())
        raise AssertionError("不应下载其它对象")

    old = _use_env()
    try:
        rows = load_live_catalog(transport=transport, now=1)
        assert rows[0]["accounts"][0]["book"] == "qidian/3007378246/contacts.json"
        assert rows[0]["accounts"][0]["org"]["departments"] == []
        loaded = load_qidian_book("anlin-dev", _UID, rows[0]["accounts"][0]["book"], transport=transport)
        try:
            load_qidian_book("anlin-dev", _UID, "wechat/wxid_demo/groups.json", transport=transport)
        except CatalogError as exc:
            assert "不是当前企点号" in str(exc)
        else:
            raise AssertionError("微信群路径应当拒绝")
    finally:
        _restore(old)
    assert loaded["org"]["departments"][0]["id"] == "d1"
    assert loaded["contacts"][0]["uin"] == "9101001"
    assert any(url.endswith("/qidian/3007378246/contacts.json") for url in seen)
    assert all("groups.json" not in url for url in seen)
    assert all("wxid_demo" not in url for url in seen)
