"""Fixed institutional file routes; labels never become remote paths."""
from __future__ import annotations
import base64
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from .protocol import Binding, canonical, token

CONTRACT = "its-relay-path-v1"
DIRECTORY_PATH = "xj_dir_v1"
ORGANIZATION_PATH = "xj_org_v1"
HEARTBEAT_SECONDS = 10
OFFLINE_SECONDS = 60
HISTORY_SECONDS = 7 * 86400
MAX_FILE_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
MAX_DIRECTORY_PENDING = 128

def validate_path(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,20}", value) is None:
        raise ValueError("FILE_GATEWAY_PATH_INVALID")
    return value

def account_paths(workbench_id, account_uid):
    token(workbench_id); token(account_uid)
    raw = hashlib.sha256(canonical([CONTRACT, workbench_id, "qidian", account_uid])).digest()
    suffix = base64.b32encode(raw).decode("ascii").lower()[:18]
    return {"uplink_path": "xu" + suffix, "downlink_path": "xd" + suffix, "friends_path": "xf" + suffix}

@dataclass(frozen=True)
class InstitutionKeys:
    institution_id: str
    owner_id: str
    seal_key: bytes = field(repr=False)
    sign_key: bytes = field(repr=False)

    @classmethod
    def from_auth(cls, auth):
        # Domain-separated HKDF-SHA256; credentials never enter envelopes.
        salt = hashlib.sha256((CONTRACT + "\x00" + auth.key).encode("utf8")).digest()
        prk = hmac.new(salt, auth.sm4_key, hashlib.sha256).digest()
        def expand(label):
            return hmac.new(prk, (CONTRACT + "/" + label).encode("ascii") + b"\x01", hashlib.sha256).digest()
        identity = hashlib.sha256((CONTRACT + "/institution/" + auth.key).encode("utf8")).hexdigest()[:32]
        return cls(identity, "institution-" + identity, expand("seal"), expand("sign"))

    def binding(self, workbench_id, account_uid, owner_id=None):
        return Binding(owner_id or self.owner_id, account_uid, workbench_id, self.seal_key, self.sign_key)
