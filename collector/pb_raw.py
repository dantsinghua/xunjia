"""无 schema 的 protobuf 线格式读取器(按需解码,不猜类型)。

fields(b) -> {字段号: [原始值…]}:varint → int;fixed64 → bytes(8);fixed32 → bytes(4);
length-delimited → bytes。由调用方按已知结构决定取 str / double / 子消息,
避免「可打印字节串被误当子消息」一类歧义。另附 dump() 供逆向时通览。
"""
from __future__ import annotations

import struct


class F64(bytes):
    """fixed64 原始 8 字节(与同长 length-delimited 区分)。"""


class F32(bytes):
    """fixed32 原始 4 字节。"""


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        if i >= len(b):
            raise ValueError("varint 越界")
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if x < 0x80:
            return r, i
        s += 7
        if s > 70:
            raise ValueError("varint 过长")


def fields(b: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        fno, wt = key >> 3, key & 7
        if fno == 0:
            raise ValueError("字段号 0")
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = F64(b[i:i + 8]), i + 8
        elif wt == 5:
            v, i = F32(b[i:i + 4]), i + 4
        elif wt == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                raise ValueError("长度越界")
            v, i = b[i:i + n], i + n
        else:
            raise ValueError(f"不支持的 wire type {wt}")
        if i > len(b):
            raise ValueError("越界")
        out.setdefault(fno, []).append(v)
    return out


def as_str(v) -> str | None:
    return v.decode("utf-8") if isinstance(v, bytes) and not isinstance(v, (F64, F32)) else None


def as_double(v) -> float | None:
    if isinstance(v, F64):
        return struct.unpack("<d", v)[0]
    return None


def as_sint(v: int) -> int:
    """int64 负数以 10 字节补码 varint 编码,折回有符号。"""
    return v - (1 << 64) if isinstance(v, int) and v >= 1 << 63 else v


def dump(b: bytes, depth: int = 0) -> dict:
    """逆向用:递归通览(启发式:能整段解成消息就当消息,否则 UTF-8,否则 hex)。"""
    d: dict = {}
    for f, vs in fields(b).items():
        for v in vs:
            if isinstance(v, int):
                x = as_sint(v)
            elif isinstance(v, F64):
                x = as_double(v)
            elif isinstance(v, F32):
                x = struct.unpack("<f", v)[0]
            elif _is_msg(v) and depth < 10:
                x = dump(v, depth + 1)
            else:
                try:
                    x = v.decode("utf-8")
                except UnicodeDecodeError:
                    x = "0x" + v.hex()
            d.setdefault(f, []).append(x)
    return d


def _is_msg(v: bytes) -> bool:
    try:
        s = v.decode("utf-8")
        if s.isprintable():
            return False
    except UnicodeDecodeError:
        pass
    try:
        return bool(fields(v))
    except ValueError:
        return False
