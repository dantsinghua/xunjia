"""Portable authenticated xunjia envelopes; compatible nonce/ciphertext/tag AES-GCM wire.

The collector must opt in to this version. Legacy unsigned acknowledgements are
not execution evidence for this protocol. Secrets never appear in repr/errors.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NAMESPACE = "xunjia"
VERSION = 2
MAX_BYTES = 4 * 1024 * 1024
KINDS = frozenset({"command", "cancel", "query", "receipt", "event", "catalog", "contacts", "manifest"})
RESULTS = frozenset({"accepted", "pending", "sent", "failed", "cancelled", "unknown"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    owner_user_id: str
    account_uid: str
    workbench_id: str
    seal_key: bytes = field(repr=False)
    sign_key: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.owner_user_id,str) or not self.owner_user_id or len(self.owner_user_id)>160 or not self.owner_user_id.isprintable():
            raise ProtocolError('业务用户身份无效')
        for value in (self.account_uid, self.workbench_id):token(value)
        if len(self.seal_key) not in (16, 24, 32) or len(self.sign_key) < 32:
            raise ProtocolError("协议密钥配置无效")


def token(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in (".", ".."):
        raise ProtocolError("协议标识无效")
    return value


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ProtocolError("协议内容不能序列化") from None


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def envelope(binding: Binding, kind: str, payload: dict, *, message_id: str,
             peer_id: str = "", command_id: str = "", content_hash: str = "",
             control_version: int = 0, created_at: float, expires_at: float) -> dict:
    return dict(version=VERSION, namespace=NAMESPACE, kind=kind, message_id=message_id,
                owner_user_id=binding.owner_user_id, account_uid=binding.account_uid,
                workbench_id=binding.workbench_id, peer_id=peer_id, command_id=command_id,
                content_hash=content_hash or digest(payload), payload_hash=digest(payload), control_version=control_version,
                created_at=created_at, expires_at=expires_at, payload=payload)


def validate_command(command: dict, binding: Binding) -> None:
    if not isinstance(command, dict):
        raise ProtocolError("命令格式无效")
    for name in ("owner_user_id", "account_uid", "workbench_id"):
        if command.get(name) != getattr(binding, name):
            raise ProtocolError("命令身份与固定绑定不符")
    if command.get('kind','send_text') not in ('send_text','manual_send') or command.get('op','send_text')!='send_text':
        raise ProtocolError('协议只允许已授权文本发送')
    for name in ("command_id", "peer_id"):
        token(command.get(name))
    if not isinstance(command.get("content_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", command["content_hash"]):
        raise ProtocolError("命令摘要无效")
    if type(command.get("control_version")) is not int or command["control_version"] < 1:
        raise ProtocolError("命令控制版本无效")
    if not isinstance(command.get("text"), str) or not command["text"] or len(command["text"]) > 1500:
        raise ProtocolError("命令正文为空或超过1500字")
    if isinstance(command.get("expires_at"), bool) or not isinstance(command.get("expires_at"), (int, float)) or not math.isfinite(command["expires_at"]):
        raise ProtocolError("命令有效期无效")
    if command.get("transport") not in ("oss", "mail"):
        raise ProtocolError("命令主通道无效")
    if 'business_version' in command:
        if command['business_version']!='2.2' or type(command.get('source_watermark')) is not int or command['source_watermark']<0:
            raise ProtocolError('Unsupported business version or missing source watermark')
    validate_fence(command, 'receiver_fence_id', 'receiver_fence_version', allow_empty=True)


def validate_fence(value, id_key, version_key, *, allow_empty=False):
    if id_key not in value and version_key not in value:return
    identity, version=value.get(id_key),value.get(version_key)
    if allow_empty and identity=='' and type(version) is int and version==0:return
    token(identity)
    if type(version) is not int or version<1:raise ProtocolError('人工屏障版本须为正整数')


def result(command: dict, status: str, evidence: str) -> dict:
    if status not in RESULTS or not isinstance(evidence, str) or not evidence:
        raise ProtocolError("执行结果缺少状态或证据")
    return {**{key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")},
            "status": status, "evidence": evidence}


class Codec:
    def __init__(self, binding: Binding, *, clock=time.time, max_bytes=MAX_BYTES, clock_skew=60):
        self.binding, self.clock, self.max_bytes, self.clock_skew = binding, clock, max_bytes, clock_skew

    def _validate(self, data):
        if not isinstance(data, dict) or data.get("version") != VERSION or data.get("namespace") != NAMESPACE or data.get("kind") not in KINDS:
            raise ProtocolError("不支持或缺失认证协议版本")
        for key in ("owner_user_id", "account_uid", "workbench_id"):
            if data.get(key) != getattr(self.binding, key):
                raise ProtocolError("信封身份不匹配")
        token(data.get("message_id"))
        if not isinstance(data.get("payload"), dict):
            raise ProtocolError("信封载荷无效")
        if data.get('payload_hash') != digest(data['payload']):
            raise ProtocolError('载荷摘要与正文不符')
        if not isinstance(data.get('content_hash'), str) or not re.fullmatch(r'[a-f0-9]{64}', data['content_hash']):
            raise ProtocolError('业务内容摘要无效')
        if data['kind'] in ('event', 'catalog', 'contacts', 'manifest') and data['content_hash'] != digest(data['payload']):
            raise ProtocolError('事件或目录摘要不符')
        created, expires = data.get("created_at"), data.get("expires_at")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (created, expires)):
            raise ProtocolError("信封时间无效")
        if created > self.clock() + self.clock_skew or expires <= created or expires <= self.clock():
            raise ProtocolError("信封过期或时间无效")
        if data["kind"] in ("command", "cancel", "query", "receipt"):
            token(data.get("command_id")); token(data.get("peer_id"))
            if type(data.get('control_version')) is not int or data['control_version']<1:raise ProtocolError('控制版本无效')
            if not re.fullmatch(r"[a-f0-9]{64}", str(data.get("content_hash", ""))):
                raise ProtocolError("信封正文摘要无效")
        if data["kind"] in ("command", "cancel", "query"):
            validate_command(data["payload"], self.binding)
            for key in ("command_id", "peer_id", "content_hash", "control_version"):
                if data[key] != data["payload"][key]:
                    raise ProtocolError("命令与信封身份不符")
            if data['kind']=='command' and data['expires_at']!=data['payload']['expires_at']:
                raise ProtocolError('命令有效期与信封不符')
        if data['kind']=='receipt':
            if 'text' in data['payload']:raise ProtocolError('回执不得携带业务正文')
            native_id=data['payload'].get('native_id')
            if native_id is not None and (not isinstance(native_id,str) or not native_id or len(native_id)>512 or '\x00' in native_id):raise ProtocolError('回执原生ID无效')
            for key in ('command_id','account_uid','peer_id','content_hash'):
                if data['payload'].get(key)!=data.get(key):raise ProtocolError('回执内部身份不一致')
            result(data['payload'],data['payload'].get('status'),data['payload'].get('evidence'))
        for key in ('owner_user_id','account_uid','workbench_id'):
            if key in data['payload'] and data['payload'][key]!=data[key]:raise ProtocolError('载荷绑定身份不一致')
        if 'transport_replay' in data and (data['kind']!='event' or type(data['transport_replay']) is not bool):raise ProtocolError('重放标记只适用于事件传输')
        if 'manual_fence_id' in data or 'manual_fence_version' in data:
            if data['kind']!='event':raise ProtocolError('人工屏障只适用于原生事件')
            validate_fence(data,'manual_fence_id','manual_fence_version')
            if data['payload'].get('peer_id')!=data.get('peer_id'):raise ProtocolError('人工屏障与事件对手身份不一致')
            if data['payload'].get('direction',data['payload'].get('dir')) not in ('out','outgoing') or data['payload'].get('source_command_id'):
                raise ProtocolError('人工屏障不得附于入站或已关联机器人事件')
        if data['kind']=='event':
            payload=data['payload']
            if 'manual_fence_id' in payload or 'manual_fence_version' in payload:raise ProtocolError('人工屏障须置于签封顶层')
            if 'event_id' in payload and payload['event_id']!=data['message_id']:raise ProtocolError('事件稳定身份不一致')
            if 'source_seq' in payload and (type(payload['source_seq']) is not int or payload['source_seq']<1):raise ProtocolError('源序号无效')
            if 'historical' in payload and type(payload['historical']) is not bool:raise ProtocolError('历史标记无效')
            if payload.get('historical') is True:
                occurred=payload.get('occurred_at')
                if type(occurred) not in (int,float) or not math.isfinite(occurred) or occurred>self.clock()+self.clock_skew:raise ProtocolError('历史事件缺少真实秒级occurred_at')
            if payload.get('source_command_id'):
                token(payload['source_command_id'])
                if payload.get('dir',payload.get('direction')) not in ('out','outgoing'):raise ProtocolError('原命令关联只适用于原生出站事件')

    def pack(self, data: dict) -> bytes:
        data = dict(data)
        data.pop("signature", None)
        self._validate(data)
        data["signature"] = "hmac-sha256=" + hmac.new(self.binding.sign_key, canonical(data), hashlib.sha256).hexdigest()
        plain = canonical(data)
        if len(plain) + 28 > self.max_bytes:
            raise ProtocolError("信封超过大小限制")
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(self.binding.seal_key).encrypt(nonce, plain, None)

    def unpack(self, blob: bytes) -> dict:
        try:
            if not isinstance(blob, bytes) or not 28 <= len(blob) <= self.max_bytes:
                raise ValueError()
            plain = AESGCM(self.binding.seal_key).decrypt(blob[:12], blob[12:], None)
            def pairs(items):
                out = {}
                for key, value in items:
                    if key in out:
                        raise ValueError()
                    out[key] = value
                return out
            data = json.loads(plain.decode("utf-8"), object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            signature = data.pop("signature", "")
            expected = "hmac-sha256=" + hmac.new(self.binding.sign_key, canonical(data), hashlib.sha256).hexdigest()
            if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
                raise ValueError()
            self._validate(data)
            return data
        except Exception:
            raise ProtocolError("信封认证、身份或有效期校验失败") from None
