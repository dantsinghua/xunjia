#!/usr/bin/env python3
# ============================================================================
# db_reader.py —— 企点消息「旁路读库」读取器(取代 frida hook,§6bis⑥)
# ----------------------------------------------------------------------------
# 与微信 chatlog 同架构:直读解码后的库,零 attach / 零注入 / 零崩溃。
# 2026-09-19 实测:企点消息库 3007373675-IndexQQMsg.db 是明文 SQLite,
#   消息存 FTS4 全文索引(IndexContent=单聊 / TroopIndex=群),
#   建表 compress=qqcompress 但本版 qqcompress 实际就是 Base64(非加密)。
#   FTS 虚表缺 qqcompress 函数无法直接查 → 读底层影子表 *_content(普通表)。
#
# 🔴 两条铁律(与内容是否公开无关):
#   ① 只读,绝不写库 —— 用 adb sqlite3 `.backup` 出一致副本再读,不碰原库、不碰 -wal;
#   ② 只读库文件,不碰企点进程 —— 不 attach / 不 frida(读库价值就在零崩溃)。
#
# 出参对齐 00-共享基线 §7.4 Message(字段名/dir/session.kind 尽量与微信通道统一,
#   便于两通道共用下游 06/02)。
#
# 用法:
#   python3 db_reader.py --snapshot                 # adb .backup+pull 出最新副本
#   python3 db_reader.py --db <副本> --tail 10       # 读最近 10 条(单聊+群)
#   python3 db_reader.py --db <副本> --since-docid N # 增量:docid>N 的新消息
#   python3 db_reader.py --db <副本> --session <ext1> # 指定会话
# ============================================================================
from __future__ import annotations
import argparse, base64, hashlib, json, os, re, struct, subprocess, sqlite3, sys, tempfile, datetime

ADB = os.environ.get("SIDE_A_ADB", "adb")
SERIAL = os.environ.get("SIDE_A_SERIAL", "emulator-5554")
DB_DIR = "/data/data/com.tencent.qidian/databases"


def _b64(s):
    if s is None:
        return b""
    if isinstance(s, bytes):
        try: s = s.decode("ascii")
        except Exception: return s
    try: return base64.b64decode(s)
    except Exception: return b""


def _txt(s):
    try: return _b64(s).decode("utf-8")
    except Exception: return ""


def _ts_from_exts(raw: bytes):
    """exts 前 8 字节大端 uint64 = 时间戳(秒)。返回 (ts, None if 不像时间戳)。"""
    if not raw or len(raw) < 8:
        return None
    v = struct.unpack(">Q", raw[:8])[0]
    return v if 1_400_000_000 <= v <= 2_200_000_000 else None


def _probe_group_ts(row: dict):
    """群表没有单独 exts 列,遍历各 base64 字段找解码后前8字节像时间戳的。"""
    for k, v in row.items():
        raw = _b64(v)
        ts = _ts_from_exts(raw)
        if ts:
            return ts, k
    return None, None


def snapshot(out_path: str, uin: str | None = None):
    """adb sqlite3 .backup 出一致副本 → pull 到 out_path → 清设备副本。只读原库。"""
    # 找库文件(带 uin 前缀)
    listing = subprocess.run([ADB, "-s", SERIAL, "shell",
                              f"ls {DB_DIR} 2>/dev/null | grep IndexQQMsg.db"],
                             capture_output=True, text=True).stdout
    names = [n.strip() for n in listing.replace("\r", "").splitlines() if n.strip().endswith("IndexQQMsg.db")]
    if not names:
        raise RuntimeError("未找到 *-IndexQQMsg.db(需 adb root + 企点已登录)")
    if uin:
        names = [n for n in names if n.startswith(uin)] or names
    src = f"{DB_DIR}/{names[0]}"
    dev_copy = "/data/local/tmp/_qd_msgidx_snap.db"
    r = subprocess.run([ADB, "-s", SERIAL, "shell",
                        f"sqlite3 'file:{src}?mode=ro' \".backup {dev_copy}\" && echo OK"],
                       capture_output=True, text=True)
    if "OK" not in r.stdout:
        raise RuntimeError(f".backup 失败: {r.stdout}{r.stderr}")
    subprocess.run([ADB, "-s", SERIAL, "pull", dev_copy, out_path],
                   capture_output=True, text=True, check=True)
    subprocess.run([ADB, "-s", SERIAL, "shell", f"rm -f {dev_copy}"], capture_output=True, text=True)
    m = re.match(r"(\d+)-IndexQQMsg\.db", names[0])
    return out_path, (m.group(1) if m else None)


# IndexContent_content 影子表列 → 语义(⚠️单聊+群消息【都在此表】,靠 c4ext1 的 ZzZ 后缀区分):
#   c4ext1 = {对端uin或群号}ZzZ{n}:ZzZ0=单聊(前缀=对端 uin)、ZzZ1=群(前缀=群号)。
#   TroopIndex 是【群成员名片索引】(全 type=2、content=null),不是群消息,不用它读消息。
MSG_MAP = dict(text="c1content", sender="c5ext2", conv="c4ext1",
               exts="c13exts", mtype="c0type", oid="c3oid")
TYPE_MAP = {"1": "text"}  # 实测 IndexContent 全 type=1=text;非文本消息(图片/文件)不在此索引库


def _fingerprint(session, sender, ts, text):
    return hashlib.sha256(f"{session}|{sender}|{ts}|{text}".encode()).hexdigest()[:16]


def _to_message(docid, row, login_uin, account_id):
    text = _txt(row[MSG_MAP["text"]])
    sender = _txt(row[MSG_MAP["sender"]])
    ext1 = _txt(row[MSG_MAP["conv"]])                 # {对端uin或群号}ZzZ{0单聊|1群}
    ts = _ts_from_exts(_b64(row[MSG_MAP["exts"]]))
    mtype = TYPE_MAP.get(_txt(row[MSG_MAP["mtype"]]), "unknown")
    if ext1.endswith("ZzZ1"):
        kind, conv = "group", ext1[:-4]               # 群:conv=群号
    elif ext1.endswith("ZzZ0"):
        kind, conv = "private", ext1[:-4]             # 单聊:conv=对端 uin
    else:
        kind, conv = "unknown", ext1
    # 会话原生 ID(基线 §6 / §15g R6-21 逐字定死):单聊=<对端uin>、群=g_<群号>。
    # ZzZ 后缀只判 kind、不进 id;群加 g_ 与 QQ 通道同惯例 —— 对端 uin 与群号是两个号段、数值可相撞,
    # 不加前缀则同号的单聊与群会并成一个会话。
    native = f"g_{conv}" if kind == "group" else conv
    is_out = (sender == login_uin)
    return {
        "id": None,                                   # 内部 msg_ULID 由上层分配
        "ext_msg_id": f"qdidx:{docid}",               # docid 稳定唯一,作原生 id
        "account_id": account_id,
        "channel": "qidian",
        "session": {"id": f"{account_id}:{native}", "name": None,
                    "kind": kind},                    # private | group
        "dir": "out" if is_out else "in",
        "type": mtype,
        "text": text,
        "text_len": len(text),
        "fingerprint": _fingerprint(native, sender, ts, text),
        "sender": {"id": sender, "name": None},
        "self": is_out,
        "ts": datetime.datetime.fromtimestamp(ts).isoformat() if ts else None,
        "source": "qidian_db",                        # 旁路读库(对标 chatlog)
        "revoked": False,
        "_docid": docid,                              # 增量游标用
        "_conv": conv,                                # 裸 uin/群号(过滤用;入库一律用 session.id)
        "_native": native,
    }


def read_messages(db_path, login_uin, account_id="qd01",
                  since_docid=None, session=None, since_ts=None, until_ts=None, limit=None):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = []
    # 只读 IndexContent_content(单聊+群消息都在此表);TroopIndex 是成员索引、不读
    q = "SELECT * FROM IndexContent_content"
    conds, args = [], []
    if since_docid is not None:
        conds.append("docid > ?"); args.append(since_docid)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY docid"
    if limit:
        q += f" LIMIT {int(limit)}"
    for r in con.execute(q, args):
        row = {k: r[k] for k in r.keys()}
        msg = _to_message(r["docid"], row, login_uin, account_id)
        # session 过滤在 python 端(解码后比对,避免 base64 padding 匹配脆):
        # 支持传 群号/对端uin(_conv)、完整 session.id、或原始 ext1(带 ZzZ 后缀)
        if session and session not in (msg["_conv"], msg["_native"], msg["session"]["id"],
                                       msg["_conv"] + "ZzZ0", msg["_conv"] + "ZzZ1"):
            continue
        if since_ts and (not msg["ts"] or msg["ts"] < since_ts):
            continue
        if until_ts and (not msg["ts"] or msg["ts"] > until_ts):
            continue
        out.append(msg)
    con.close()
    out.sort(key=lambda x: (x["ts"] or "", x["_docid"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true", help="adb .backup+pull 出最新副本")
    ap.add_argument("--db", help="已有副本路径(不传则 --snapshot 出临时副本)")
    ap.add_argument("--uin", help="登录 uin(默认从库文件名解析)")
    ap.add_argument("--account", default="qd01")
    ap.add_argument("--since-docid", type=int)
    ap.add_argument("--session")
    ap.add_argument("--tail", type=int, help="只看最近 N 条")
    args = ap.parse_args()

    login_uin = args.uin
    db = args.db
    if args.snapshot or not db:
        db = db or os.path.join(tempfile.gettempdir(), "qd_msgidx_snap.db")
        db, uin = snapshot(db, args.uin)
        login_uin = login_uin or uin
        print(f"[snapshot] {db}  login_uin={login_uin}", file=sys.stderr)
    if not login_uin:
        m = re.search(r"(\d+)-IndexQQMsg", db) or re.search(r"(\d{6,})", os.path.basename(db))
        login_uin = m.group(1) if m else "0"
    msgs = read_messages(db, login_uin, args.account,
                         since_docid=args.since_docid, session=args.session)
    if args.tail:
        msgs = msgs[-args.tail:]
    for m in msgs:
        print(json.dumps(m, ensure_ascii=False))
    print(f"[total] {len(msgs)}  max_docid={max((m['_docid'] for m in msgs), default=None)}", file=sys.stderr)


if __name__ == "__main__":
    main()
