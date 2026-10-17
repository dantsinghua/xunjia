"""当前企点号的私人好友。只在配置了采集端地址时才去读，读不到就当没有。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def merge_people(current: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """好友补进通讯录。已有的 uin 不重复，也不改组织架构。"""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in list(current or []) + list(extra or []):
        if not isinstance(row, dict):
            continue
        uin = str(row.get("uin") or "").strip()
        if not uin or uin in seen:
            continue
        seen.add(uin)
        found.append({
            "uin": uin,
            "name": str(row.get("name") or uin).strip() or uin,
            "remark": str(row.get("remark") or "").strip(),
        })
    return found


def private_friends(account_uid: str) -> list[dict[str, str]]:
    """读采集端上这个企点号的好友。没配地址、对不上号、接口失败都回空。"""
    base = os.environ.get("QTRADE_AGENT_URL", "").strip().rstrip("/")
    uid = str(account_uid or "").strip()
    if not base or not uid:
        return []
    token = os.environ.get("QTRADE_AGENT_TOKEN", "").strip()
    if not token:
        path = os.environ.get("QTRADE_AGENT_TOKEN_FILE", "").strip()
        if path and Path(path).is_file():
            token = Path(path).read_text(encoding="utf-8").strip()
    headers = {"Accept": "application/json", "X-QT-Api-Min": "1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        status, listing = _get(base + "/api/v1/accounts?channel=qidian", headers)
        if status != 200:
            return []
        rows = listing.get("data") if isinstance(listing, dict) else None
        match = next(
            (row for row in rows or [] if isinstance(row, dict) and str(row.get("self_uid") or "") == uid),
            None,
        )
        if not match or not match.get("id"):
            return []
        status, book = _get(base + "/api/v1/accounts/" + str(match["id"]) + "/contacts", headers)
        if status != 200:
            return []
        people = book.get("data") if isinstance(book, dict) else None
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return []
    return merge_people([], people or [])


def _get(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = {}
    return status, parsed
