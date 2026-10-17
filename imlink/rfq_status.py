"""询价状态按债券代码落盘。

文件里只有代码和询价状态。不写报价正文、密钥、回执，也不写行情价格。
和通讯录、微信群配置分开，放在指令记录旁边的 rfq_status.json。
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

_CODE = re.compile(r"[0-9A-Za-z]{4,12}\.(?:IB|SH|SZ|BJ)")
_INQUIRING = "询价中"
_FAILED = "发送失败"
_PENDING = "待回执"
_ALLOWED = {
    "询价中", "待回执", "发送失败", "转人工", "已报价", "还价中",
    "待人工确认", "已成交", "已暂停", "未成交", "已撤价", "已放弃",
}

_books_lock = threading.Lock()
_books: dict[str, "StatusBook"] = {}


def statuses_from_commands(rows: list[Any]) -> dict[str, str]:
    """按指令顺序抽出仍要记住的状态。

    失败记为发送失败。公网信箱送达只记待回执，读库回执才升询价中。
    不读 ack，不打开回执。只提交、尚未失败或送达的指令不计入。
    """
    state: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        text = row.get("text") if isinstance(row.get("text"), str) else ""
        codes = _CODE.findall(text)
        if status == "failed":
            for code in codes:
                state[code] = _FAILED
        elif status == "delivered":
            for code in codes:
                state[code] = _PENDING
    return state


class StatusBook:
    """询价状态表。每次读写都落盘，换一个实例仍能读到。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _blank(self) -> dict[str, Any]:
        return {"seeded": False, "codes": {}}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._blank()
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return self._blank()
        if not isinstance(data, dict):
            return self._blank()
        raw = data.get("codes")
        codes: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                code = str(key or "").strip()
                if not code or not isinstance(value, dict):
                    continue
                status = str(value.get("status") or "")
                if status in _ALLOWED:
                    codes[code] = status
        return {"seeded": bool(data.get("seeded")), "codes": codes}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seeded": bool(data.get("seeded")),
            "codes": {
                code: {"status": status}
                for code, status in data.get("codes", {}).items()
                if str(code).strip() and status in _ALLOWED
            },
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def ensure(self, commands: list[Any]) -> dict[str, str]:
        """首次读取时补历史状态。补过之后不再扫指令，删除才不会被旧记录加回。"""
        with self._lock:
            data = self._read()
            if not data.get("seeded"):
                for code, status in statuses_from_commands(commands).items():
                    data["codes"].setdefault(code, status)
                data["seeded"] = True
                self._write(data)
            return dict(data["codes"])

    def mark(self, codes: list[Any], status: str) -> dict[str, str]:
        if status not in _ALLOWED:
            raise ValueError("状态无效")
        with self._lock:
            data = self._read()
            for raw in codes:
                code = str(raw or "").strip()
                if code:
                    data["codes"][code] = status
            data["seeded"] = True
            self._write(data)
            return dict(data["codes"])

    def delete(self, code: str) -> dict[str, str]:
        with self._lock:
            data = self._read()
            data["codes"].pop(str(code or "").strip(), None)
            data["seeded"] = True
            self._write(data)
            return dict(data["codes"])

    def mapping(self) -> dict[str, str]:
        with self._lock:
            return dict(self._read()["codes"])


def book_for(command_path: Path) -> StatusBook:
    """和指令记录同目录、不同文件。测试把指令指到临时目录时，不会写正式数据。"""
    path = Path(command_path).with_name("rfq_status.json")
    key = str(path)
    with _books_lock:
        book = _books.get(key)
        if book is None:
            book = StatusBook(path)
            _books[key] = book
        return book
