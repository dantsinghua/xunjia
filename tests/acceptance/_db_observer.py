"""QA-only cursor observation; keeps the real database and query execution."""
from contextlib import contextmanager
import re
import threading


class HistoryReadObserver:
    def __init__(self, monkeypatch, database):
        self.reads = []
        self.lock = threading.Lock()
        original = database.transaction
        observer = self

        class Cursor:
            def __init__(self, inner, sql=""):
                self.inner, self.sql = inner, sql

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def execute(self, sql, params=()):
                self.sql = sql
                return self.inner.execute(sql, params)

            def fetchall(self):
                if "from xj_message" in self.sql.lower():
                    assert re.search(r"\bLIMIT\s", self.sql, re.I), "History fetchall must be a bounded page, never the entire account history"
                    rows = self.inner.fetchall()
                    assert len(rows) <= 201, "History page may materialize at most the 200-row limit plus lookahead"
                    with observer.lock:
                        observer.reads.append({"method": "fetchall", "count": len(rows)})
                    return rows
                return self.inner.fetchall()

            def fetchmany(self, size=None):
                if "from xj_message" in self.sql.lower():
                    assert size is not None and size <= 500
                rows = self.inner.fetchmany(size) if size is not None else self.inner.fetchmany()
                if "from xj_message" in self.sql.lower():
                    with observer.lock:
                        observer.reads.append({"method": "fetchmany", "count": len(rows), "requested": size})
                return rows

        class Connection:
            def __init__(self, inner):
                self.inner = inner

            def cursor(self, *args, **kwargs):
                return Cursor(self.inner.cursor(*args, **kwargs))

            def __getattr__(self, name):
                return getattr(self.inner, name)

        class Transaction:
            def __init__(self, inner):
                self.inner = inner
                self.connection = Connection(inner.connection)

            def execute(self, sql, params=()):
                return Cursor(self.inner.execute(sql, params), sql)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        @contextmanager
        def observed(*args, **kwargs):
            with original(*args, **kwargs) as transaction:
                yield Transaction(transaction)

        monkeypatch.setattr(database, "transaction", observed)
