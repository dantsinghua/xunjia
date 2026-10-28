"""Versioned, parse-only WeChat facts over isolated institutional file routes.

This module validates data only. It never changes a command, a receiver fence,
or an UNKNOWN result. Authorization comes from a local grant, not the wire.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from .gateway_paths import CHUNK_BYTES, CONTRACT, MAX_FILE_BYTES
from .protocol import MAX_BYTES, ProtocolError, canonical, digest, token

PURPOSE = "wechat-capture-v1"
CONSUMER_ID = "nanyin-deposit-parser"
SCHEMA_VERSION = 1
LEASE_SECONDS = 60
CONFIG_MAX_BYTES = 128 * 1024
_GROUP = re.compile(r"[A-Za-z0-9_-]{1,128}@chatroom\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_OPS = frozenset({"capture_hello", "capture_ready", "capture_fact", "capture_media",
                  "capture_media_chunk", "capture_attribution"})
_SCOPE = frozenset({"contract", "purpose", "schema_version", "consumer_id", "grant_id",
                    "selection_version", "owner_user_id", "workbench_id", "account_uid", "channel", "op"})


def _fail(code):
    raise ProtocolError("CAPTURE_" + code)


def _integer(value, minimum=0, maximum=2**63-1):
    return type(value) is int and minimum <= value <= maximum


def _group(value):
    return isinstance(value, str) and _GROUP.fullmatch(value) is not None


def _native_id(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value.isprintable()


@dataclass(frozen=True)
class CaptureGrant:
    owner_user_id: str
    workbench_id: str
    account_uid: str
    consumer_id: str = CONSUMER_ID
    selection_version: int = 1
    selected_native_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.owner_user_id, str) or not self.owner_user_id
                or len(self.owner_user_id) > 160 or not self.owner_user_id.isprintable()):
            _fail("OWNER_INVALID")
        token(self.workbench_id)
        token(self.account_uid)
        if self.consumer_id != CONSUMER_ID or not _integer(self.selection_version, 1):
            _fail("GRANT_INVALID")
        groups = self.selected_native_ids
        if (not isinstance(groups, (list, tuple)) or not 1 <= len(groups) <= 200
                or any(not _group(group) for group in groups)):
            _fail("GROUPS_INVALID")
        object.__setattr__(self, "selected_native_ids", tuple(sorted(set(groups))))

    def as_dict(self):
        return {**asdict(self), "selected_native_ids": list(self.selected_native_ids)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            _fail("GRANT_FIELDS_INVALID")
        return cls(**value)

    @property
    def grant_id(self):
        return digest([PURPOSE, self.as_dict()])


def load_grants_file(path, expected_owner):
    """Read an operator-owned grant file; absence disables capture on both ends."""
    if not path:
        return (), ()
    file = Path(path)
    try:
        metadata = file.lstat()
    except FileNotFoundError:
        return (), ()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > CONFIG_MAX_BYTES
            or metadata.st_size == 0):
        _fail("CONFIG_FILE_INVALID")
    if os.name == "posix" and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
        _fail("CONFIG_PERMISSIONS_INVALID")
    # Open without following a replacement symlink; verify the opened inode too.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(file, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail("CONFIG_CHANGED")
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > CONFIG_MAX_BYTES
                or os.name == "posix" and (opened.st_uid != os.geteuid() or opened.st_mode & 0o077)):
            _fail("CONFIG_PERMISSIONS_INVALID")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(CONFIG_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > CONFIG_MAX_BYTES:
        _fail("CONFIG_FILE_INVALID")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        _fail("CONFIG_JSON_INVALID")
    if (not isinstance(value, dict) or set(value) - {"schema_version", "grants", "backfill"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION
            or not isinstance(value.get("grants"), list) or len(value["grants"]) > 16):
        _fail("CONFIG_INVALID")
    grants = tuple(CaptureGrant.from_dict(item) for item in value["grants"])
    scopes = [(g.owner_user_id, g.workbench_id, g.account_uid, g.consumer_id) for g in grants]
    if len(scopes) != len(set(scopes)) or any(g.owner_user_id != expected_owner for g in grants):
        _fail("CONFIG_SCOPE_INVALID")
    backfill = value.get("backfill", [])
    if not isinstance(backfill, list) or len(backfill) > 500:
        _fail("BACKFILL_INVALID")
    by_id = {grant.grant_id: grant for grant in grants}
    seen = set()
    for item in backfill:
        if not isinstance(item, dict) or set(item) != {"grant_id", "native_id", "ext_msg_id"}:
            _fail("BACKFILL_INVALID")
        grant = by_id.get(item["grant_id"])
        if (grant is None or item["native_id"] not in grant.selected_native_ids
                or not _native_id(item["ext_msg_id"])):
            _fail("BACKFILL_SCOPE_INVALID")
        key = (item["grant_id"], item["native_id"], item["ext_msg_id"])
        if key in seen:
            _fail("BACKFILL_DUPLICATE")
        seen.add(key)
    return grants, tuple(dict(item) for item in backfill)


def capture_paths(grant):
    suffix = base64.b32encode(hashlib.sha256(canonical([PURPOSE, grant.grant_id])).digest()).decode("ascii").lower()[:18]
    return {"capture_up": "cu" + suffix, "capture_down": "cd" + suffix}


def peer_for(native_id):
    if not _group(native_id):
        _fail("GROUP_INVALID")
    return "wxpeer-" + base64.urlsafe_b64encode(native_id.encode("utf8")).decode("ascii").rstrip("=")


def capture_id(grant, peer_id, ext_msg_id):
    token(peer_id)
    if not _native_id(ext_msg_id):
        _fail("NATIVE_ID_INVALID")
    # Match the pre-existing deposit ODS identity, including across slot changes.
    return digest(["its-relay/wechat-message/v1", grant.workbench_id,
                   grant.account_uid, peer_id, ext_msg_id])


def make_payload(grant, op, **fields):
    payload = {"contract": CONTRACT, "purpose": PURPOSE, "schema_version": SCHEMA_VERSION,
               "consumer_id": grant.consumer_id, "grant_id": grant.grant_id,
               "selection_version": grant.selection_version, "owner_user_id": grant.owner_user_id,
               "workbench_id": grant.workbench_id, "account_uid": grant.account_uid,
               "channel": "wechat", "op": op, **fields}
    return validate_payload(payload, grant)


def _attachments(items, state):
    if not isinstance(items, list) or len(items) > 20:
        _fail("ATTACHMENTS_INVALID")
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) - {"name", "sha256", "size", "mime", "quality", "content_base64"}
                or not isinstance(item.get("name"), str) or not 1 <= len(item["name"]) <= 256
                or any(ch in item["name"] for ch in "/\\\r\n") or not item["name"].isprintable()
                or not isinstance(item.get("sha256"), str) or not _SHA.fullmatch(item["sha256"])
                or not _integer(item.get("size"), 1, MAX_FILE_BYTES)):
            _fail("ATTACHMENT_INVALID")
        if item["name"] in seen:
            _fail("ATTACHMENT_DUPLICATE")
        seen.add(item["name"])
        if "mime" in item and (not isinstance(item["mime"], str) or not item["mime"].startswith("image/")):
            _fail("ATTACHMENT_MIME_INVALID")
        if item.get("quality", "original") not in ("original", "hd"):
            _fail("ORIGINAL_IMAGE_REQUIRED")
        encoded = item.get("content_base64")
        if encoded is None:
            if state == "ready":
                _fail("ATTACHMENT_CONTENT_REQUIRED")
        else:
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
                _fail("ATTACHMENT_CONTENT_INVALID")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                _fail("ATTACHMENT_CONTENT_INVALID")
            if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                _fail("ATTACHMENT_HASH_MISMATCH")


def _event(event, grant):
    allowed = {"event_id", "peer_id", "ext_msg_id", "kind", "type", "direction", "text", "source_seq",
               "occurred_at", "channel", "native_account_uid", "native_message_id", "identity_epoch",
               "session", "sender", "historical", "is_recalled", "media_state", "media", "attachments", "attribution"}
    if not isinstance(event, dict) or set(event) - allowed:
        _fail("EVENT_FIELDS_INVALID")
    session = event.get("session")
    if (not isinstance(session, dict) or set(session) - {"native_id", "kind", "name", "id"}
            or session.get("kind") != "group" or session.get("native_id") not in grant.selected_native_ids):
        _fail("EVENT_GROUP_INVALID")
    if any(key in session and (not isinstance(session[key], str) or len(session[key]) > 512
                               or bool(session[key]) and not session[key].isprintable()) for key in ("name", "id")):
        _fail("EVENT_METADATA_INVALID")
    sender = event.get("sender", {})
    if (not isinstance(sender, dict) or set(sender) - {"id", "name"}
            or any(value is not None and (not isinstance(value, str) or len(value) > 512
                                         or bool(value) and not value.isprintable()) for value in sender.values())):
        _fail("EVENT_METADATA_INVALID")
    if (event.get("channel") != "wechat" or event.get("native_account_uid") != grant.account_uid
            or event.get("peer_id") != peer_for(session["native_id"])
            or not _native_id(event.get("ext_msg_id"))):
        _fail("EVENT_IDENTITY_INVALID")
    token(event.get("event_id"))
    if (event.get("kind") not in ("message", "edit", "retract") or event.get("type") not in ("text", "image")
            or event.get("direction") not in ("in", "out") or not isinstance(event.get("text"), str)
            or not _integer(event.get("source_seq"), 1) or not _integer(event.get("identity_epoch"))
            or type(event.get("occurred_at")) not in (int, float)
            or not math.isfinite(event["occurred_at"]) or event["occurred_at"] <= 0
            or type(event.get("historical")) is not bool
            or ("is_recalled" in event and type(event["is_recalled"]) is not bool)):
        _fail("EVENT_FACTS_INVALID")
    if event.get("attribution") != {"status": "unresolved"}:
        _fail("ATTRIBUTION_UNPROVED")
    state = event.get("media_state")
    if state not in ("pending", "ready", "chunked"):
        _fail("MEDIA_STATE_INVALID")
    media = event.get("media")
    if not isinstance(media, list) or len(media) > 20:
        _fail("MEDIA_INVALID")
    for item in media:
        if not isinstance(item, dict) or set(item) - {"kind", "state", "sha256", "size", "mime", "quality"}:
            _fail("MEDIA_FIELDS_INVALID")
        if (item.get("kind") != "image" or item.get("state") not in ("pending", "ready", "failed", "skipped_oversize", "skipped_policy")
                or ("sha256" in item and (not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"])))
                or ("size" in item and not _integer(item["size"], 1, MAX_FILE_BYTES))
                or ("mime" in item and (not isinstance(item["mime"], str) or not item["mime"].startswith("image/")))
                or ("quality" in item and item["quality"] not in ("original", "hd", "thumb"))):
            _fail("MEDIA_INVALID")
    _attachments(event.get("attachments"), state)
    if (event["type"] == "image" and state != "pending" and not event.get("is_recalled")
            and not event["attachments"]):
        _fail("ORIGINAL_IMAGE_REQUIRED")


def validate_payload(payload, grant):
    if not isinstance(grant, CaptureGrant) or not isinstance(payload, dict):
        _fail("PAYLOAD_INVALID")
    expected = {"contract": CONTRACT, "purpose": PURPOSE, "schema_version": SCHEMA_VERSION,
                "consumer_id": grant.consumer_id, "grant_id": grant.grant_id,
                "selection_version": grant.selection_version, "owner_user_id": grant.owner_user_id,
                "workbench_id": grant.workbench_id, "account_uid": grant.account_uid, "channel": "wechat"}
    if any(payload.get(key) != value for key, value in expected.items()):
        _fail("SCOPE_MISMATCH")
    if type(payload.get("schema_version")) is not int or type(payload.get("selection_version")) is not int:
        _fail("VERSION_INVALID")
    op = payload.get("op")
    if not isinstance(op, str) or op not in _OPS:
        _fail("OP_UNSUPPORTED")
    fields = {"capture_hello": {"nonce"}, "capture_ready": {"nonce", "lease_until", "active_native_ids"},
              "capture_fact": {"capture_id", "content_revision", "media_revision", "event"},
              "capture_media": {"capture_id", "content_revision", "media_revision", "event"},
              "capture_media_chunk": {"capture_id", "content_revision", "media_revision", "sha256", "index", "total", "size", "data"},
              "capture_attribution": {"capture_id", "peer_id", "ext_msg_id", "source_command_id", "attribution"}}[op]
    if set(payload) != _SCOPE | fields:
        _fail("PAYLOAD_FIELDS_INVALID")
    if op in ("capture_hello", "capture_ready"):
        token(payload["nonce"])
        if op == "capture_ready":
            groups = payload["active_native_ids"]
            if (not isinstance(groups, list) or any(not isinstance(group, str) for group in groups)
                    or len(groups) != len(set(groups))
                    or not set(groups) <= set(grant.selected_native_ids)
                    or type(payload["lease_until"]) not in (int, float)
                    or not math.isfinite(payload["lease_until"]) or payload["lease_until"] <= 0):
                _fail("READY_INVALID")
        return payload
    if not isinstance(payload.get("capture_id"), str) or not _SHA.fullmatch(payload["capture_id"]):
        _fail("ID_INVALID")
    if op == "capture_attribution":
        token(payload["source_command_id"])
        if (payload["attribution"] != {"status": "confirmed"}
                or payload["peer_id"] not in {peer_for(group) for group in grant.selected_native_ids}
                or payload["capture_id"] != capture_id(grant, payload["peer_id"], payload["ext_msg_id"])):
            _fail("ATTRIBUTION_INVALID")
        return payload
    if not _integer(payload.get("content_revision"), 1) or not _integer(payload.get("media_revision")):
        _fail("REVISION_INVALID")
    if op in ("capture_fact", "capture_media"):
        _event(payload["event"], grant)
        if payload["capture_id"] != capture_id(grant, payload["event"]["peer_id"], payload["event"]["ext_msg_id"]):
            _fail("ID_MISMATCH")
    else:
        if (not isinstance(payload["sha256"], str) or not _SHA.fullmatch(payload["sha256"])
                or not _integer(payload["size"], 1, MAX_FILE_BYTES)
                or not _integer(payload["total"], 1, (MAX_FILE_BYTES + CHUNK_BYTES - 1) // CHUNK_BYTES)
                or payload["total"] != (payload["size"] + CHUNK_BYTES - 1) // CHUNK_BYTES
                or not _integer(payload["index"], 0, payload["total"] - 1)
                or not isinstance(payload["data"], str) or len(payload["data"]) > 4 * ((CHUNK_BYTES + 2) // 3)):
            _fail("CHUNK_INVALID")
        try:
            raw = base64.b64decode(payload["data"], validate=True)
        except (ValueError, binascii.Error):
            _fail("CHUNK_INVALID")
        expected_size = min(CHUNK_BYTES, payload["size"] - payload["index"] * CHUNK_BYTES)
        if len(raw) != expected_size:
            _fail("CHUNK_SIZE_INVALID")
    if len(canonical(payload)) > MAX_BYTES - 4096:
        _fail("PAYLOAD_TOO_LARGE")
    return payload
