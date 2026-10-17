#!/usr/bin/env python3
"""企点 读库→回复 回环测试(临时验证脚本,2026-09-19)。

只做一件事:轮询旁路读库,发现【指定单聊对端】发来的新消息,就在【当前已打开的该聊天页】里回一条。
外发边界(写死,不可配宽):
  * 只回 --peer 这一个单聊(session.id == {account}:{peer});群/其它会话只读不回;
  * 回复内容固定模板;最多 --max-replies 条;最长 --minutes 分钟;收到「停」立即停;
  * 每次发送后用读库读回,确认落在目标会话(dir=out 且 session.id 一致),不一致立即停。
前提:企点已登录、已 adb root、屏幕已停在与 peer 的聊天页(由人/总控先核对标题)。
"""
import argparse, base64, json, subprocess, sys, time, datetime

import db_reader as R

ADB, SERIAL = R.ADB, R.SERIAL
C_INPUT, C_SENDBTN = ("320", "1123"), ("647", "1086")   # 720x1280@320dpi,与 qidian_cold_start.sh 一致
PKG = "com.tencent.qidian"
SNAP = "/tmp/qd_echo_snap.db"


def sh(*a):
    return subprocess.run([ADB, "-s", SERIAL, "shell", *a], capture_output=True, text=True).stdout


def log(**kw):
    kw["t"] = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def poll(uin, account, since):
    db, _ = R.snapshot(SNAP, uin)
    return R.read_messages(db, uin, account_id=account, since_docid=since)


def send(text):
    if PKG not in sh("dumpsys", "window"):          # 弱校验:前台仍是企点
        raise RuntimeError("前台不是企点,拒绝发送")
    sh("input", "tap", *C_INPUT); time.sleep(0.6)
    b64 = base64.b64encode(text.encode()).decode()
    sh("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64); time.sleep(0.8)
    sh("input", "tap", *C_SENDBTN)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", required=True)
    ap.add_argument("--uin", required=True)
    ap.add_argument("--account", default="qd01")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--max-replies", type=int, default=10)
    a = ap.parse_args()
    target = f"{a.account}:{a.peer}"

    msgs = poll(a.uin, a.account, None)
    since = max((m["_docid"] for m in msgs), default=0)
    log(ev="start", target=target, since_docid=since)
    deadline, replies = time.time() + a.minutes * 60, 0

    while time.time() < deadline and replies < a.max_replies:
        time.sleep(a.interval)
        t0 = time.time()
        new = poll(a.uin, a.account, since)
        if not new:
            continue
        since = max(m["_docid"] for m in new)
        for m in new:
            seen = time.time()
            ts = datetime.datetime.fromisoformat(m["ts"]).timestamp() if m["ts"] else None
            lat = round(seen - ts, 1) if ts else None
            log(ev="read", docid=m["_docid"], session=m["session"]["id"], kind=m["session"]["kind"],
                dir=m["dir"], text=m["text"][:60], msg_ts=m["ts"], read_latency_s=lat,
                poll_cost_s=round(seen - t0, 2))
            if m["dir"] != "in" or m["session"]["id"] != target or m["session"]["kind"] != "private":
                continue                                   # 只读不回
            if m["text"].strip() in ("停", "stop"):
                log(ev="stop_by_peer"); return
            reply = f"[自动回复] 已读到:「{m['text'][:40]}」 延迟{lat}s"
            t1 = time.time(); send(reply); replies += 1
            # 读回确认:等我方这条落库且会话一致
            ok = False
            for _ in range(10):
                time.sleep(1.0)
                back = [x for x in poll(a.uin, a.account, since) if x["dir"] == "out"]
                if back:
                    since = max(since, max(x["_docid"] for x in back))
                    ok = all(x["session"]["id"] == target for x in back)
                    log(ev="sent_readback", ok=ok, session=back[-1]["session"]["id"],
                        text=back[-1]["text"][:60], send_to_readback_s=round(time.time() - t1, 1))
                    break
            if not ok:
                log(ev="ABORT", why="发送后未读回或会话不一致"); return
    log(ev="end", replies=replies)


if __name__ == "__main__":
    main()
