"""Short transactions on OceanBase/MySQL; SQLite is explicit for local tests."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from urllib.parse import unquote, urlsplit


class Transaction:
    def __init__(self, connection, mysql: bool):
        self.connection = connection
        self.mysql = mysql

    def execute(self, sql: str, parameters=()):
        if self.mysql:
            cursor = self.connection.cursor()
            cursor.execute(sql.replace("?", "%s"), parameters)
            return cursor
        return self.connection.execute(sql, parameters)


class Database:
    def __init__(self, url: str):
        self.mysql = url.startswith(("mysql://", "oceanbase://"))
        self._lock = threading.RLock()
        self._memory = None
        if self.mysql:
            parsed = urlsplit(url)
            if not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError("业务数据库配置无效")
            self._options = dict(host=parsed.hostname, port=parsed.port or 3306,
                                 user=unquote(parsed.username or ""), password=unquote(parsed.password or ""),
                                 database=parsed.path.lstrip("/"), charset="utf8mb4",
                                 connect_timeout=10, read_timeout=20, write_timeout=20,
                                 autocommit=False, init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        elif url.startswith("sqlite:///"):
            self.path = url[len("sqlite:///"):]
            if not self.path:
                raise ValueError("测试数据库路径不能为空")
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            else:
                self._memory = self._sqlite_connection()
        else:
            raise ValueError("请配置OceanBase/MySQL业务数据库；测试须显式指定sqlite路径")

    def _sqlite_connection(self):
        connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _connect(self):
        if self.mysql:
            import pymysql
            return pymysql.connect(**self._options, cursorclass=pymysql.cursors.DictCursor)
        return self._memory or self._sqlite_connection()

    @contextmanager
    def transaction(self, *, write: bool = True):
        # Only the explicit in-memory test database shares a connection.
        lock = self._lock if self._memory else _NoLock()
        with lock:
            connection = self._connect()
            try:
                if self.mysql:
                    connection.begin()
                else:
                    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield Transaction(connection, self.mysql)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                if connection is not self._memory:
                    connection.close()

    def initialize(self, statements: list[str]):
        with self.transaction() as tx:
            for statement in statements:
                tx.execute(statement)


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

# Export uses a server-side cursor for MySQL; a bounded fetchmany loop for SQLite.
def stream_rows(database,sql,parameters=(),batch_size=500):
    if not 1<=batch_size<=2000:raise ValueError('读取批量无效')
    with database.transaction(write=False) as tx:
        if database.mysql:
            import pymysql
            cursor=tx.connection.cursor(pymysql.cursors.SSDictCursor)
            cursor.execute(sql.replace('?', '%s'),parameters)
        else:cursor=tx.execute(sql,parameters)
        try:
            while True:
                rows=cursor.fetchmany(batch_size)
                if not rows:break
                for row in rows:yield dict(row)
        finally:cursor.close()
