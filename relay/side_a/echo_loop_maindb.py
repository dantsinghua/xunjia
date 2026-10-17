#!/usr/bin/env python3
"""企点 主库实时读→回复 回环测试 v2(临时验证脚本,2026-09-19)。

v1(echo_loop_test.py)读的是 *-IndexQQMsg.db —— 那是【滞后的全文索引】,实测落库延迟 13~36 s。
v2 读【主消息库】{uin}.db(WAL,消息到达即写):
  * 单聊表 mr_friend_{MD5(对端uin)大写}_New;群表 mr_troop_{MD5(群号)大写}_New;
  * 文本列 msgData / senderuin 等做了逐字节 XOR,密钥 = 安卓默认 MAC "02:00:00:00:00:00"(循环);
  * 增量游标 = 表内自增 _id。
读取方式:设备上 sqlite3 只读打开(mode=ro)只查增量行,hex() 出来在宿主解码;不 attach、不注入企点进程。
外发边界同 v1:只回 --peer 这一个单聊;固定模板;限条数/限时;「停」即停;发送后读回确认,不一致即停。
"""
import argparse, base64, hashlib, json, subprocess, time, datetime

ADB, SERIAL = "adb", "emulator-5554"
PKG = "com.tencent.qidian"
KEY = b"02:00:00:00:00:00"
C_INPUT, C_SENDBTN = ("320", "1123"), ("647", "1086")   # 720x1280@320dpi


def sh(*a):
    return subprocess.run([ADB, "-s", SERIAL, "shell", *a], capture_output=True, text=True).stdout


def xor(b: bytes) -> bytes:
    return bytes(v ^ KEY[i % len(KEY)] for i, v in enumerate(b))


def log(**kw):
    kw["t"] = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def query(db, table, since_id):
    sql = (f'select _id||"|"||issend||"|"||time||"|"||msgtype||"|"||hex(msgData) '
           f'from "{table}" where _id>{int(since_id)} order by _id;')
    out = sh(f"sqlite3 'file:{db}?mode=ro' '{sql}'")
    rows = []
    for ln in out.replace("\r", "").splitlines():
        p = ln.split("|")
        if len(p) != 5 or not p[0].isdigit():
            continue
        rows.append(dict(id=int(p[0]), out=p[1] == "1", ts=int(p[2]), mtype=int(p[3]),
                         text=xor(bytes.fromhex(p[4])).decode("utf-8", "replace")))
    return rows


def send(text):
    if PKG not in sh("dumpsys", "window"):
        raise RuntimeError("前台不是企点,拒绝发送")
    sh("input", "tap", *C_INPUT); time.sleep(0.5)
    sh("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", base64.b64encode(text.encode()).decode())
    time.sleep(0.7)
    sh("input", "tap", *C_SENDBTN)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", required=True); ap.add_argument("--uin", required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--minutes", type=float, default=15); ap.add_argument("--max-replies", type=int, default=10)
    ap.add_argument("--since-id", type=int, default=None, help="从该 _id 之后开始(用于补回积压);默认从当前最大 _id 起")
    a = ap.parse_args()
    db = f"/data/data/{PKG}/databases/{a.uin}.db"
    table = f"mr_friend_{hashlib.md5(a.peer.encode()).hexdigest().upper()}_New"
    rows = query(db, table, 0)
    since = max((r["id"] for r in rows), default=0) if a.since_id is None else a.since_id
    log(ev="start", table=table, since_id=since)
    deadline, n = time.time() + a.minutes * 60, 0
    pending = {}                                   # tag -> (发送开始时刻, 对端消息 ts);读回确认不阻塞主循环
    while time.time() < deadline and n < a.max_replies:
        time.sleep(a.interval)
        t0 = time.time(); new = query(db, table, since); cost = round(time.time() - t0, 2)
        for m in new:
            since = m["id"]
            if m["out"]:
                for tag in [k for k in pending if m["text"].startswith(k)]:
                    t1, peer_ts = pending.pop(tag)
                    log(ev="sent_readback", ok=True, tag=tag, peer_to_reply_sent_s=m["ts"] - peer_ts,
                        send_to_db_visible_s=round(time.time() - t1, 1))
                continue
            lat = round(time.time() - m["ts"], 1)
            log(ev="read", id=m["id"], mtype=m["mtype"], text=m["text"][:60], read_latency_s=lat, poll_cost_s=cost)
            if m["mtype"] != -1000:
                continue
            if m["text"].strip() in ("停", "stop"):
                log(ev="stop_by_peer"); return
            n += 1
            tag = f"[自动回复·m{m['id']}]"                 # 用对端消息 _id 做唯一标记,读回只认它
            t1 = time.time(); send(f"{tag} 已读到:「{m['text'][:24]}」 读取延迟{lat}s")
            pending[tag] = (t1, m["ts"])
            log(ev="sent", tag=tag, send_cost_s=round(time.time() - t1, 2))
        stale = [k for k, (t1, _) in pending.items() if time.time() - t1 > 40]
        if stale:
            log(ev="ABORT", why="发送后 40 s 未在目标会话表读回", tags=stale); return
    log(ev="end", replies=n)


if __name__ == "__main__":
    main()
