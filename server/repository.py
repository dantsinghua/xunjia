"""Account-scoped business persistence. Public projections never contain secrets."""
from __future__ import annotations

import json
import sqlite3

from .db import Database
from .legacy_schema import statements


class Repository:
    def __init__(self, database_url: str, *, initialize: bool | None = None):
        self.db = Database(database_url)
        should_initialize = not self.db.mysql if initialize is None else initialize
        if should_initialize:
            self.initialize_schema()

    def initialize_schema(self):
        """Explicit production migration; never runs ALTER on customer tables."""
        self.db.initialize(statements(self.db.mysql))

    def bind_account(self, owner_user_id: str, account_uid: str, workbench_id: str,
                     transport: str = "oss") -> dict:
        if transport not in ("oss", "mail"):
            raise ValueError("不支持的正式通道")
        for value, length in ((owner_user_id, 160), (account_uid, 80), (workbench_id, 160)):
            if not isinstance(value, str) or not value.strip() or len(value) > length:
                raise ValueError("账号归属配置无效")
        with self.db.transaction() as tx:
            occupied = tx.execute("SELECT owner_user_id FROM xj_binding WHERE account_uid=?", (account_uid,)).fetchone()
            if occupied and occupied["owner_user_id"] != owner_user_id:
                raise ValueError("企点号已有所属交易员")
            current = tx.execute("SELECT * FROM xj_binding WHERE owner_user_id=?", (owner_user_id,)).fetchone()
            if current:
                if current["account_uid"] != account_uid:
                    raise ValueError("账号归属变更必须先完成停用和在途对账")
                if current["workbench_id"] != workbench_id or current["transport"] != transport:
                    raise ValueError("通道变化必须通过受控切换")
            else:
                try:
                    tx.execute("INSERT INTO xj_binding(owner_user_id,account_uid,workbench_id,transport,version,bot_enabled,config_json) VALUES(?,?,?,?,?,?,?)",
                               (owner_user_id, account_uid, workbench_id, transport, 1, 0, "{}"))
                except Exception as exc:
                    if isinstance(exc, sqlite3.IntegrityError) or type(exc).__name__ == "IntegrityError":
                        raise ValueError("账号归属冲突") from None
                    raise
        return self.binding_for(owner_user_id)

    def binding_for(self, owner_user_id: str) -> dict | None:
        with self.db.transaction(write=False) as tx:
            row = tx.execute("SELECT owner_user_id,account_uid,workbench_id,transport,version,bot_enabled FROM xj_binding WHERE owner_user_id=?", (owner_user_id,)).fetchone()
        return dict(row) if row else None

    def preferences(self, owner_user_id: str) -> dict:
        with self.db.transaction(write=False) as tx:
            row = tx.execute("SELECT config_json FROM xj_binding WHERE owner_user_id=?", (owner_user_id,)).fetchone()
        if not row:
            raise PermissionError("尚未配置本人企点账号")
        data = json.loads(row["config_json"])
        if not isinstance(data, dict):
            raise ValueError("业务配置损坏")
        return data
