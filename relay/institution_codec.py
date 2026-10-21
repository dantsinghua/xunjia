"""Explicit owner projection at the institutional transport boundary only.
V2.2 business payload semantics, IDs, watermarks and native evidence stay intact.
The server assigns the current authenticated workspace; collectors use the
institution identity, not an invented employee mapping.
"""
from __future__ import annotations
from copy import deepcopy
from .protocol import Binding, Codec, ProtocolError, digest, session_reset_hash

def retarget(data, owner):
    value = deepcopy(data)
    value.pop("signature", None)
    value["owner_user_id"] = owner
    payload = value["payload"]
    if "owner_user_id" in payload:
        payload["owner_user_id"] = owner
    if value["kind"] in ("session_reset", "session_reset_ack"):
        payload["request_hash"] = session_reset_hash(payload)
    value["payload_hash"] = digest(payload)
    if value["kind"] in ("event", "catalog", "contacts", "manifest", "session_reset", "session_reset_ack"):
        value["content_hash"] = value["payload_hash"]
    return value

class InstitutionCodec(Codec):
    """Only a configured, selected account can translate a verified wire owner.
    Never accepts a request-supplied destination/owner or changes message text.
    """
    def __init__(self, binding, institution_owner, **kwargs):
        super().__init__(binding, **kwargs)
        if not isinstance(binding, Binding):
            raise ProtocolError("Institution codec requires a selected account")
        self.wire_binding = Binding(institution_owner, binding.account_uid, binding.workbench_id, binding.seal_key, binding.sign_key)
        self.wire = Codec(self.wire_binding, **kwargs)

    def pack(self, data):
        self._validate(data)
        return self.wire.pack(retarget(data, self.wire_binding.owner_user_id))

    def unpack(self, blob):
        wire = self.wire.unpack(blob)
        local = retarget(wire, self.binding.owner_user_id)
        self._validate(local)
        return local

    def _authenticated(self, blob):
        value = self.wire._authenticated(blob)
        for key in ("owner_user_id", "account_uid", "workbench_id"):
            if value.get(key) != getattr(self.wire_binding, key):
                raise ProtocolError("Institution wire identity mismatch")
        return retarget(value, self.binding.owner_user_id)
