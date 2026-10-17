#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# qidian_msgdata_decode.py —— 企点主消息库 msgData 解码参考实现
# ----------------------------------------------------------------------------
# 只读、纯标准库、零依赖。不 attach、不注入企点进程、不写任何库。
#
# 背景(2026-09-19 实测,样本 = 全库 37009 条消息 / 分层抽样 1099 条 + 测试号 37 条):
#   主库 /data/data/com.tencent.qidian/databases/{uin}.db(WAL,明文 SQLite)。
#   会话分表 mr_friend_{MD5(对端uin).upper()}_New / mr_troop_{MD5(群号).upper()}_New。
#   msgData / senderuin / frienduin / selfuin 逐字节 XOR,密钥 = ASCII 串
#   "02:00:00:00:00:00" 的 17 字节循环(不是 6 个原始 MAC 字节)。
#   _id / issend / istroop / time / uniseq / msgtype / shmsgseq 是明文整数。
#
# XOR 之后 msgData 分三个编码族(靠魔数 + msgtype 即可 100% 分开):
#   ① UTF-8 明文     : -1000(单行) -1051(多行) -1049(含@) -2006(系统串)
#   ② protobuf       : -1035(文本+图片混排) -2000(群图片) -5040 -2018(系统/会话事件)
#   ③ Java 序列化    : -2017(群文件 TroopFileData) -2011(链接卡片),魔数 AC ED 00 05
#
# 基线约定:纯图片/纯文件消息的 text 为空字符串,媒体性质由 type 表达,
#          **不往 text 里塞占位符**;但 -1035 混排消息相反:按 Elem 原顺序把文本段
#          与 "[图片]" 占位符拼进 text(规格 06 §2.9.5 R6-44)。
#          QQ 表情转义不删除,替换为字面量 "[表情]"(R6-46,见 clean_text)。
# 产出边界:读库路只产出文本 —— 返回里的 emit=True 仅限文本族与「有文本段的 -1035」。
#
# 用法:
#   from qidian_msgdata_decode import xor, decode
#   rec = decode(msgtype, blob)                 # blob = 库里原始(未 XOR)的 msgData
#   rec = decode(msgtype, plain, already_xored=True)   # plain = 已 XOR 过的明文
#   python3 qidian_msgdata_decode.py --selftest       # 只读连设备自检(仅测试号私聊)
# ============================================================================
from __future__ import annotations

import argparse
import subprocess
import sys

__all__ = ["xor", "decode", "clean_text", "KEY"]

KEY = b"02:00:00:00:00:00"          # 17 字节 ASCII,循环异或
_KEYLEN = len(KEY)                   # == 17

# 编码族路由表(与设计文档 06 §2.9.5 一致)
_TEXT_TYPES   = (-1000, -1051, -1049)          # 整体 UTF-8 -> type=text
_SYSTEM_TEXT  = (-2006,)                       # 整体 UTF-8 -> type=system
_ELEM_TYPES   = (-1035,)                       # protobuf: repeated Elem(顶层 field 1)
_IMAGE_TYPES  = (-2000,)                       # protobuf: 扁平 CustomFace -> type=image
_SYSTEM_PB    = {-5040: (5,), -2018: (5, 6)}   # protobuf: 扁平,取指定 LEN 字段作正文
_JAVA_FILE    = (-2017,)                       # Java 序列化 -> type=file(群文件)

_JAVA_MAGIC = b"\xac\xed\x00\x05"

# -1035 混排消息里非文本段的占位符(规格 06 §2.9.5 R6-44 / 安琳拍板口径)
_NONTEXT_PLACEHOLDER = "[图片]"

# 图片内容特征(比逆向出的 field 号稳:企点是 QQ 魔改版,字段号可能随版本变)
_IMG_SIG = (b"picplatform", b"/gchatpic_new/", b"/download?appid=", b"/offpic_new/")

# CustomFace 类结构里的媒体元数据字段(-1035 的 Elem 子 message 与 -2000 顶层共用同一布局)
#   field -> (输出键名, 期望 wiretype: 0=varint 2=LEN)
_META_FIELDS = {2: ("file_id", 0), 6: ("md5", 2), 24: ("width", 0),
                25: ("height", 0), 26: ("size", 0)}


# --------------------------------------------------------------------------- #
# XOR
# --------------------------------------------------------------------------- #
def xor(b: bytes) -> bytes:
    """逐字节异或 KEY 的 17 字节循环。自反:xor(xor(x)) == x。

    同一把 key 也解 senderuin / frienduin / selfuin 三列(实测已验证)。
    """
    if not b:
        return b""
    return bytes(v ^ KEY[i % _KEYLEN] for i, v in enumerate(b))


# --------------------------------------------------------------------------- #
# 最小 protobuf 解析(只做线格式遍历,不需要 .proto)
# --------------------------------------------------------------------------- #
def _varint(b: bytes, i: int):
    val = shift = 0
    n = len(b)
    while i < n:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint overflow / truncated")


def _walk(b: bytes, depth: int = 0, maxdepth: int = 8):
    """返回 [(field, wiretype, value, raw)] 或 None(不是合法 protobuf)。

    wiretype 2 时 value 是子节点列表(无法再解时为 None),raw 是原始字节。
    """
    out, i, n = [], 0, len(b)
    while i < n:
        try:
            key, i = _varint(b, i)
        except ValueError:
            return None
        field, wire = key >> 3, key & 7
        if field == 0:
            return None
        if wire == 0:
            try:
                val, i = _varint(b, i)
            except ValueError:
                return None
            out.append((field, 0, val, None))
        elif wire == 2:
            try:
                ln, i = _varint(b, i)
            except ValueError:
                return None
            if ln < 0 or i + ln > n:
                return None
            raw = b[i:i + ln]
            i += ln
            sub = _walk(raw, depth + 1, maxdepth) if (depth < maxdepth and ln >= 2) else None
            out.append((field, 2, sub, raw))
        elif wire == 5:
            if i + 4 > n:
                return None
            out.append((field, 5, int.from_bytes(b[i:i + 4], "little"), None))
            i += 4
        elif wire == 1:
            if i + 8 > n:
                return None
            out.append((field, 1, int.from_bytes(b[i:i + 8], "little"), None))
            i += 8
        else:                                    # 3/4 已废弃,6/7 非法
            return None
    return out


# QQ 表情在"纯文本"里的转义:0x14 + 紧随其后的 1 个【字符】(索引)。
# 实测(2026-09-19,全库文本族 36013 条):0x14 共 114 次,后继字符码位 U+0000~U+00B8;
# 索引是 UTF-8 编码的【字符】而非裸字节(见 `14 c2 b8` = 0x14 + U+00B8),
# 因此必须在字符层面剥离——按字节剥会切断多字节序列、破坏 UTF-8。
# 同批实测:除 0x14 外出现的控制字节(0x00×7 / 0x03×6 / 0x08×42)100% 都是 0x14 的后继,
# 故"成对剥离"之后不会有 <0x20 残留;下面第二条分支是纯防御。
_CTRL_KEEP = frozenset("\t\n\r")

# QQ 表情不删、替换成字面量(规格 06 §2.9.5 R6-46 / 安琳拍板):
# 全库 87 条含表情的行里有 17 条【整条只由表情组成】,删掉会得到 text=""——
# 要么摆渡成空邮件,要么被下游顺手过滤而静默丢掉客户的真实回复。写成 [表情] 两头都不发生。
_EMOJI_PLACEHOLDER = "[表情]"


def clean_text(s: str) -> str:
    """正文清洗(规格 06 §2.9.5 R6-46)。在字符层面自左向右、不重叠扫描:

    ① 遇 U+0014,连同其紧随其后的 1 个【字符】一起替换为 "[表情]" ——
       后继是什么都一并消耗(**包括 \t \n \r**);
       U+0014 位于串尾、没有后继时,只丢掉它自己,不写占位。
    ② 未被 ① 消耗的其余 < U+0020 字符逐个丢弃;\t \n \r 一律保留。

    索引以 UTF-8 编码(实测 `14 c2 b8` = U+0014 + U+00B8),故必须在 str 层面扫描:
    按字节做会切断多字节序列、破坏 UTF-8。
    """
    if not s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\x14":
            if i + 1 < n:                     # 前导 + 索引 -> 占位符,两个字符一起消耗
                out.append(_EMOJI_PLACEHOLDER)
                i += 2
            else:                             # 串尾孤立的 U+0014:只丢自己,不写占位
                i += 1
            continue
        if ord(c) < 0x20 and c not in _CTRL_KEEP:
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_strip_ctrl = clean_text          # 旧名别名(R6-46 前的调用方)


def _text_or_none(raw):
    """判一段 LEN 是否为正文。含 U+0014(表情)的文本【算】正文;
    除表情外仍带控制字符的不算。通过则返回清洗后的文本。"""
    if not raw:
        return None
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\x14":
            i += 2
            continue
        if ord(c) < 0x20 and c not in _CTRL_KEEP:
            return None
        i += 1
    return clean_text(s)


def _lossy(raw: bytes) -> str:
    """正文字段:UTF-8 解码(坏字节替换为 U+FFFD)后按 clean_text 清洗。"""
    return clean_text(raw.decode("utf-8", "replace")) if raw else ""


def _strict(raw):
    """猜某段 LEN 是不是文本时使用:必须完整 UTF-8 且无控制字符(\\n\\r\\t 除外)。"""
    if not raw:
        return None
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for ch in s:
        if ord(ch) < 0x20 and ch not in "\n\r\t":
            return None
    return s


def _media_meta(nodes) -> dict:
    """从 CustomFace 类结构的节点列表里挑出媒体元数据。"""
    meta = {}
    for field, wire, val, raw in nodes or ():
        spec = _META_FIELDS.get(field)
        if not spec:
            continue
        name, want = spec
        if want == 0 and wire == 0:
            meta[name] = val
        elif want == 2 and wire == 2:
            s = _strict(raw)
            if s:
                meta[name] = s
    return meta


def _elem_nontext(inner) -> dict:
    """判一个非文本 Elem 的性质并取其元数据。inner = Elem 的节点列表。"""
    blob = b"".join(raw or b"" for _f, w, _v, raw in inner if w == 2 and raw)
    kind = "image" if any(sig in blob for sig in _IMG_SIG) else None
    meta = {}
    for field, wire, sub, _raw in inner:
        if field == 1 or wire != 2 or not isinstance(sub, list):
            continue
        got = _media_meta(sub)
        if got:
            meta = got
            break
    if kind is None:
        # 兜底:Elem field 4 = notOnlineImage(logcat ElemStrs 已独立佐证)
        kind = "image" if any(f == 4 for f, _w, _v, _r in inner) else "other"
    out = {"kind": kind}
    out.update(meta)
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def decode(msgtype: int, msgdata_xored_or_raw: bytes, *, already_xored: bool = False) -> dict:
    """解一条 msgData。

    参数
      msgtype               : 库里的明文整数列 msgtype
      msgdata_xored_or_raw  : 默认传库里原始(仍被 XOR 的)BLOB;
                              already_xored=True 时传已经 XOR 还原过的明文字节
    返回
      {"type":   "text"|"image"|"file"|"system"|"unknown",   # 本实现解得出什么
       "text":   str,
       "origin": dict,                                       # 媒体元数据等;无则为 {}
       "emit":   bool}                                       # 规格(06 §2.9.5 R6-44)让不让它进库

    `type` 回答「解得出什么」,`emit` 回答「规格让不让产出 Message」——两者独立:
    读库路只产出文本,故 emit=True 仅限文本族与「有文本段的 -1035」。
    emit=False 的行仍返回已解出的 type/text/origin 供排查,调用方应丢弃并照常推进水位。
    """
    blob = msgdata_xored_or_raw or b""
    b = blob if already_xored else xor(blob)

    # ① 魔数优先:Java 序列化 (AC ED 00 05)
    if b[:4] == _JAVA_MAGIC:
        return {"type": "file" if msgtype in _JAVA_FILE else "unknown",
                "text": "", "origin": {"encoding": "java-serialized"}, "emit": False}

    # ② 整体 UTF-8 明文
    if msgtype in _TEXT_TYPES:
        return {"type": "text", "text": _lossy(b), "origin": {}, "emit": True}
    if msgtype in _SYSTEM_TEXT:
        return {"type": "system", "text": _lossy(b), "origin": {}, "emit": False}

    # ③ protobuf 族
    nodes = _walk(b)
    if not nodes:                                 # None=非法 protobuf;[]=空 blob,均视为异常
        return {"type": "unknown", "text": "", "origin": {}, "emit": False}

    if msgtype in _ELEM_TYPES:
        # 顶层 = repeated Elem(field 1);Elem.1.1 = Text.str。
        # text 按 Elem 的【原顺序】拼接:文本段取正文,非文本段写 [图片] 占位符。
        # 顺序不能省——实测可产出的 85 条里,[图片] 在最前的 67 条(MT 64 + MMT 3)、
        # 在最后的 18 条(TM);若把占位符一律追加到末尾,会把 79% 的行前后关系写反。
        parts, media, has_text = [], [], False
        for field, wire, inner, _raw in nodes:
            if field != 1 or wire != 2 or not isinstance(inner, list):
                parts.append(_NONTEXT_PLACEHOLDER)
                media.append({"kind": "other"})
                continue
            tnode = next((x for x in inner if x[0] == 1 and x[1] == 2), None)
            seg = None                        # None = 不是文本段
            if tnode is not None:
                if isinstance(tnode[2], list):
                    strnode = next((y for y in tnode[2] if y[0] == 1 and y[1] == 2), None)
                    if strnode is not None:
                        seg = _lossy(strnode[3])
                if seg is None:               # fallback:Elem.1 直接是 bytes
                    seg = _text_or_none(tnode[3])
            if seg is not None:               # 是文本段就算数 ——
                parts.append(seg)             # 清洗后只剩 "[表情]"(甚至空串)也一样
                has_text = True
                continue
            media.append(_elem_nontext(inner))
            parts.append(_NONTEXT_PLACEHOLDER)
        origin = {"media": media} if media else {}
        if has_text:                              # 混排:产出,占位符留在正文里
            return {"type": "text", "text": "".join(parts), "origin": origin, "emit": True}
        return {"type": "image", "text": "", "origin": origin, "emit": False}

    if msgtype in _IMAGE_TYPES:
        meta = _media_meta(nodes)
        return {"type": "image", "text": "",
                "origin": {"media": [dict(kind="image", **meta)]} if meta else {},
                "emit": False}

    if msgtype in _SYSTEM_PB:
        byfield = {f: raw for f, w, _v, raw in nodes if w == 2}
        text = "".join(_lossy(byfield[f]) for f in _SYSTEM_PB[msgtype] if f in byfield)
        return {"type": "system", "text": text, "origin": {}, "emit": False}

    return {"type": "unknown", "text": "", "origin": {}, "emit": False}


# --------------------------------------------------------------------------- #
# --selftest:只读连设备,仅跑测试号私聊那一张表
# --------------------------------------------------------------------------- #
_SELFTEST_UIN   = "3007373675"
_SELFTEST_TABLE = "mr_friend_14B2EAB788FA7A11CC1DA282CB8B7163_New"   # 对端 415011447
_EXPECT_ID      = 37
_EXPECT_TYPE    = "text"
_EXPECT_TEXT    = "继续测试这里有表情包你是否能正常读取[图片]"
_EXPECT_EMIT    = True


def _offline_cases() -> list:
    """不读设备的自造用例(规格 06 §2.9.5 R6-46 清洗规则)。返回 failures 列表。"""
    fails = []

    def chk(name, got, want):
        if got != want:
            fails.append(f"{name}: 得到 {got!r},期望 {want!r}")

    r = decode(-1000, "收到\x148谢谢".encode(), already_xored=True)
    chk("表情夹在正文中间", r["text"], "收到[表情]谢谢")
    r = decode(-1000, "\x14@".encode(), already_xored=True)
    chk("整条只有一个表情.text", r["text"], "[表情]")
    chk("整条只有一个表情.emit", r["emit"], True)
    r = decode(-1000, "a\x14\tb".encode(), already_xored=True)
    chk("索引恰为 \\t 也一并消耗", r["text"], "a[表情]b")
    r = decode(-1000, "abc\x14".encode(), already_xored=True)
    chk("串尾孤立 U+0014 只丢自己", r["text"], "abc")
    return fails


def _selftest(serial: str = "emulator-5554") -> int:
    db = f"/data/data/com.tencent.qidian/databases/{_SELFTEST_UIN}.db"
    sql = (f'select _id||"|"||msgtype||"|"||hex(msgData) '
           f'from "{_SELFTEST_TABLE}" order by _id;')
    proc = subprocess.run(
        ["adb", "-s", serial, "shell", f"sqlite3 'file:{db}?mode=ro' '{sql}'"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[FAIL] adb/sqlite3 失败: {proc.stderr.strip()}", file=sys.stderr)
        return 2

    offline = _offline_cases()
    print(f"# 离线用例(clean_text / R6-46): {'PASS' if not offline else 'FAIL'}"
          f"  共 4 条")
    print(f"# table={_SELFTEST_TABLE}  (测试号 415011447 私聊;不读任何群表)")
    print(f"{'_id':>5} {'msgtype':>8} {'type':>8} {'emit':>6} {'len(text)':>10}")
    rows = hit = emit_true = 0
    failures = list(offline)
    for line in proc.stdout.replace("\r", "").splitlines():
        parts = line.split("|")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        mid, mtype, hexdata = int(parts[0]), int(parts[1]), parts[2]
        try:
            raw = bytes.fromhex(hexdata) if hexdata else b""
        except ValueError:
            failures.append(f"_id={mid} hex 解析失败")
            continue
        rec = decode(mtype, raw)
        rows += 1
        emit_true += 1 if rec["emit"] else 0
        print(f"{mid:>5} {mtype:>8} {rec['type']:>8} {str(rec['emit']):>6} {len(rec['text']):>10}")
        if mid == _EXPECT_ID:
            hit = 1
            if rec["type"] != _EXPECT_TYPE:
                failures.append(f"_id={mid} type={rec['type']!r},期望 {_EXPECT_TYPE!r}")
            if rec["text"] != _EXPECT_TEXT:
                failures.append(f"_id={mid} text={rec['text']!r},期望 {_EXPECT_TEXT!r}")
            if rec["emit"] is not _EXPECT_EMIT:
                failures.append(f"_id={mid} emit={rec['emit']!r},期望 {_EXPECT_EMIT!r}")
            if not failures:
                print(f"\n[ok] _id={mid} type={rec['type']} emit={rec['emit']} "
                      f"text={rec['text']!r}\n     origin={rec['origin']}")

    print(f"\n# rows decoded = {rows}   emit=True {emit_true} / emit=False {rows - emit_true}")
    if not hit:
        failures.append(f"未找到 _id={_EXPECT_ID}(库可能已变动)")
    if failures:
        for f in failures:
            print(f"[FAIL] {f}", file=sys.stderr)
        return 1
    print("[PASS] selftest 通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="企点 msgData 解码参考实现(只读)")
    ap.add_argument("--selftest", action="store_true",
                    help="只读连设备,对测试号私聊表全部行跑一遍并校验 _id=37")
    ap.add_argument("--serial", default="emulator-5554", help="adb 序列号")
    args = ap.parse_args()
    if args.selftest:
        return _selftest(args.serial)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
