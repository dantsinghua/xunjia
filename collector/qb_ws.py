"""QB(羿珩 DM)onshore-ws-gateway-apollo 自定义 ws 协议 —— packet 编解码 + 订阅。

逆自 quote-web SharedWorker(97302 chunk):
Packet(protobuf):
  field1 tag8  int32   type       # e8: PONG=0 PING=1 CONNECT=2 DISCONNECT=3 EVENT=4 ACK=5 ERROR=6
  field2 tag16 uint32  eventType  # I.b 事件枚举(NONE=0, CONNECT_NOTIFICATION/GROUP_MESSAGE_*/JOIN_GROUP…数值待实连确认)
  field3 tag26 bytes   data       # 载荷(JoinGroupRequest / 各 Notification 的 protobuf)
  field4 tag32 uint64  ackId      # "0"=无 ack
JoinGroupRequest(protobuf): field1 tag10 repeated string groupNames
CONNECT_NOTIFICATION 的 data 为 zlib 压缩;GROUP_MESSAGE_* 的 data 内层 messageContent 即旁路已解的报价/成交 protobuf。
握手:url = base?terminalType=1&timestamp=<ms>&sign=md5("1"+ms+token);token 走 Sec-WebSocket-Protocol。
"""
from __future__ import annotations

import hashlib
import struct
import time
import zlib


# ---------------- protobuf 最小编解码 ----------------
def _varint(v: int) -> bytes:
    out = b""
    while True:
        b = v & 0x7F
        v >>= 7
        out += bytes([b | 0x80]) if v else bytes([b])
        if not v:
            return out


def encode_packet(ptype: int, event_type: int = 0, data: bytes = b"", ack_id: int = 0) -> bytes:
    out = b""
    if ptype:
        out += _varint(8) + _varint(ptype)          # field1 int32
    if event_type:
        out += _varint(16) + _varint(event_type)     # field2 uint32
    if data:
        out += _varint(26) + _varint(len(data)) + data  # field3 bytes
    if ack_id:
        out += _varint(32) + _varint(ack_id)          # field4 uint64
    return out


def encode_join_group(group_names: list[str]) -> bytes:
    out = b""
    for g in group_names:
        gb = g.encode("utf-8")
        out += _varint(10) + _varint(len(gb)) + gb    # field1 repeated string
    return out


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7F) << s
        if x < 0x80:
            return r, i
        s += 7


def decode_packet(b: bytes) -> dict:
    o = {"type": 0, "eventType": 0, "data": b"", "ackId": 0}
    i = 0
    while i < len(b):
        key, i = _read_varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(b, i)
            o["type" if f == 1 else "eventType" if f == 2 else "ackId"] = v
        elif wt == 2:
            n, i = _read_varint(b, i)
            if f == 3:
                o["data"] = b[i:i + n]
            i += n
        else:
            break
    return o


def maybe_inflate(data: bytes) -> bytes:
    if data[:2] == b"\x78\x9c" or data[:1] == b"\x78":
        try:
            return zlib.decompress(data)
        except zlib.error:
            pass
    return data


# ---------------- 握手 ----------------
PING, EVENT = 1, 4


def ws_url(base: str, token: str) -> str:
    ms = int(time.time() * 1000)
    sign = hashlib.md5(f"1{ms}{token}".encode()).hexdigest()
    return f"{base}?terminalType=1&timestamp={ms}&sign={sign}"


# ---------------- 事件枚举(I.b) ----------------
EV_CONNECT_NOTIFICATION = 7
EV_JOIN_GROUP = 8
EV_LEAVE_GROUP = 9
EV_GROUP_MESSAGE_NOTIFICATION = 16
# packet type(e8)
PT_PONG, PT_PING, PT_EVENT = 0, 1, 4


def decode_group_message(data: bytes) -> dict:
    """GroupMessageNotification: f3(tag26) groupName, f4(tag32) messageType, f5(tag42) messageContent bytes。"""
    o = {"groupName": "", "messageType": 0, "messageContent": b""}
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(data, i)
            if f == 4:
                o["messageType"] = v
        elif wt == 2:
            n, i = _read_varint(data, i)
            seg = data[i:i + n]; i += n
            if f == 3:
                o["groupName"] = seg.decode("utf-8", "replace")
            elif f == 5:
                o["messageContent"] = seg
        else:
            break
    return o
