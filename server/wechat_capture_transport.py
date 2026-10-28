"""Manifest-only capture routes with a revocable data publication guard."""
from __future__ import annotations

import hashlib

from relay.gateway_paths import HISTORY_SECONDS
from relay.protocol import ProtocolError
from .institution_transport import InstitutionTransport


class CaptureTransport(InstitutionTransport):
    def __init__(self, *args, delivery_guard=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.incoming_kinds = self.outgoing_kinds = frozenset({"manifest"})
        self.delivery_guard = delivery_guard
        self._retry_cursor = 0

    def _permitted(self, data):
        return (isinstance(data, dict) and data.get("kind") == "manifest"
                and (self.delivery_guard is None or self.delivery_guard(data) is True))

    def _publish(self, data, blob):
        if not self._permitted(data):
            raise ProtocolError("CAPTURE_PUBLICATION_NOT_AUTHORIZED")
        return super()._publish(data, blob)

    def retry_data(self, limit=4):
        # Preserve the existing immutable-data retry contract, but recheck the
        # current grant/lease for each packet instead of replaying retired scope.
        with self.store.db() as db:
            sql = ("SELECT rowid AS capture_retry_rowid,* FROM ip_delivery WHERE scope=? AND acked=0 "
                   "AND first_at>? AND last_at<? AND rowid>? ORDER BY rowid LIMIT ?")
            args = (self.store.scope, self.clock()-HISTORY_SECONDS, self.clock()-30)
            rows = db.execute(sql, (*args, self._retry_cursor, limit)).fetchall()
            if not rows and self._retry_cursor:
                self._retry_cursor = 0
                rows = db.execute(sql, (*args, 0, limit)).fetchall()
        if not rows or self.outbound.pending() > 0:
            return
        for row in rows:
            self._retry_cursor = row["capture_retry_rowid"]
            blob = bytes(row["blob"])
            data = self.codec.unpack(blob)
            if not self._safe_retry(data):
                continue
            if not self._permitted(data):
                # The scan cursor provides fairness without changing the retry
                # timestamp: reauthorization can immediately resume an old item.
                continue
            try:
                self.outbound.upload("xj-data-"+hashlib.sha256(blob).hexdigest()[:32]+".bin", blob)
            finally:
                with self.store.db() as db:
                    db.execute("UPDATE ip_delivery SET last_at=?,attempts=attempts+1 WHERE scope=? AND id=?",
                               (self.clock(), self.store.scope, row["id"]))
