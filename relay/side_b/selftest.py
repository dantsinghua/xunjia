"""B 侧自测:构造 inbound → send('b') → process_once → poll('a') 校验 outbound 信封。

隔离:邮件目录与状态目录都指到 state/selftest/ 下,不动 relay/mail/ 与 A 侧收件箱。
运行(仓库根目录):.venv/bin/python -m relay.side_b.selftest
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX = HERE / "state" / "selftest"
shutil.rmtree(SANDBOX, ignore_errors=True)
os.environ["B_STATE_DIR"] = str(SANDBOX / "state")
os.environ.pop("B_TEMPLATE", None)
os.environ.pop("OPENAI_BASE", None)          # 自测只验规则路径

from relay import mailbus  # noqa: E402

mailbus.MAIL = SANDBOX / "mail"               # 隔离收件箱(不改 mailbus.py)

from relay.side_b import phrase  # noqa: E402
from relay.side_b.run_b import process_once  # noqa: E402
from relay.side_b.sessions import SessionStore  # noqa: E402

HINT = {"windcode": "102400895.IB", "简称": "24苏科技城MTN001", "估值": 1.80}
_n = 0
FAILS: list[str] = []


def inbound(thread: str, text: str, hint: dict | None = HINT, msg_id: str | None = None) -> str:
    global _n
    _n += 1
    mid = msg_id or f"t{_n}"
    env = {"v": 1, "type": "inbound", "msg_id": mid, "channel": "qidian", "thread": thread, "text": text,
           "ts": 1789450000.0 + _n, "cursor": ""}
    if hint is not None:
        env["session_hint"] = hint
    mailbus.send("b", env)
    return mid


def drain(store: SessionStore) -> dict[str, dict]:
    process_once(store, log=lambda *a, **k: None)
    outs = {}
    for env, ack in mailbus.poll("a"):
        outs[env["in_reply_to"]] = env
        ack()
    return outs


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}  {detail}")
    if not cond:
        FAILS.append(name)


def main() -> None:
    store = SessionStore(Path(os.environ["B_STATE_DIR"]))

    # 1) 还价劣于边界 → 逐轮让步 → 钳到边界 → 结束
    ids = [inbound("cp还价", "估值+3可以出") for _ in range(4)]
    outs = drain(store)
    texts = [outs[i]["text"] for i in ids]
    check("还价→第2轮 +8", texts[0] == "bid 102400895.IB 24苏科技城MTN001 3000W 估值+8能出么", texts[0])
    check("还价→第3轮 +6", texts[1] == "bid 102400895.IB 24苏科技城MTN001 3000W 估值+6能出么", texts[1])
    check("还价→第4轮钳到边界 +5", texts[2] == "bid 102400895.IB 24苏科技城MTN001 3000W 估值+5能出么", texts[2])
    check("到边界仍拒→CLOSED_NOPRICE", outs[ids[3]]["meta"]["state"] == "CLOSED_NOPRICE"
          and outs[ids[3]]["action"] == "send" and outs[ids[3]]["text"] == phrase.CLOSE_TEXT,
          f"{outs[ids[3]]['meta']['state']} {outs[ids[3]]['text']}")
    check("所有回复 action=send 且带 in_reply_to", all(outs[i]["action"] == "send" and outs[i]["in_reply_to"] == i for i in ids))
    check("轮次上限派生=4", outs[ids[0]]["meta"]["max_rounds"] == 4, str(outs[ids[0]]["meta"]["max_rounds"]))

    # 2) 实价 → to_human
    i = inbound("cp实价", "1.86能出3000")
    o = drain(store)[i]
    check("实价→to_human", o["action"] == "to_human" and "拿到实价" in o["reason"], f"{o['action']} {o['reason']}")
    check("实价 human_note 含 1.86/3000", "1.86" in o["meta"]["human_note"] and "3000万" in o["meta"]["human_note"],
          o["meta"]["human_note"])
    check("实价 对手方话术=请示", o["text"] == phrase.HOLD_TEXT, o["text"])

    # 3) 虚价 → 追问量(一次) → 给量 → 实价 to_human
    i = inbound("cp虚价", "大概1.86吧")
    o = drain(store)[i]
    check("虚价→追问量", o["action"] == "send" and o["text"] == "24苏科技城MTN001 这个价能出多少量?" and o["meta"]["state"] == "ASK_VOL",
          f"{o['action']} {o['text']} {o['meta']['state']}")
    i = inbound("cp虚价", "3000")
    o = drain(store)[i]
    check("追问量后给量→实价 to_human", o["action"] == "to_human" and "拿到实价" in o["reason"] and "1.86" in o["meta"]["human_note"],
          f"{o['reason']} | {o['meta'].get('human_note')}")

    # 3b) 虚价 → 追问量 → 仍模糊 → to_human
    i = inbound("cp虚价2", "参考估值+6")
    o = drain(store)[i]
    check("虚价2→追问量", o["action"] == "send" and o["meta"]["state"] == "ASK_VOL", o["text"])
    i = inbound("cp虚价2", "我再问一下吧")
    o = drain(store)[i]
    check("追问量后仍模糊→to_human", o["action"] == "to_human", f"{o['action']} {o['reason']}")

    # 4) 非价格条件 / 反问 / 挂起 / 噪声
    i1, i2, i3, i4 = (inbound("cp过券", "要过券"), inbound("cp反问", "你出个价"),
                      inbound("cp挂起", "等会回你"), inbound("cp噪声", "[表情]"))
    o = drain(store)
    check("过券→to_human", o[i1]["action"] == "to_human" and "非价格条件" in o[i1]["reason"], o[i1]["reason"])
    check("反问→to_human", o[i2]["action"] == "to_human" and "反问" in o[i2]["reason"], o[i2]["reason"])
    check("挂起→none", o[i3]["action"] == "none" and o[i3]["text"] == "", o[i3]["reason"])
    check("噪声→none", o[i4]["action"] == "none" and o[i4]["meta"]["intent"] == "噪声", o[i4]["reason"])

    # 5) 还价落在边界内(+7) → 追问量 → 给量 → to_human
    i = inbound("cp边界内", "估值+7可以出")
    o = drain(store)[i]
    check("还价在边界内→追问量", o["action"] == "send" and o["meta"]["state"] == "ASK_VOL" and o["meta"]["current_bp"] == 7,
          f"{o['text']} {o['meta']['current_bp']}")
    i = inbound("cp边界内", "有3000")
    o = drain(store)[i]
    check("边界内+量→实价 to_human", o["action"] == "to_human" and "估值+7" in o["meta"]["human_note"], o["meta"].get("human_note"))

    # 6) 明确拒绝(无数字)→ 下一轮
    i = inbound("cp拒绝", "不行,做不了")
    o = drain(store)[i]
    check("明确拒绝→下一轮 +8", o["text"].endswith("估值+8能出么"), o["text"])

    # 7) 加量超可投量 → to_human
    i = inbound("cp超投", "1.86能出8000", hint={**HINT, "可投量_wan": 3000})
    o = drain(store)[i]
    check("加量超投→to_human", o["action"] == "to_human" and "超本方可投量" in o["reason"], o["reason"])

    # 8) 其他券报价 → to_human
    i = inbound("cp他券", "102400896.IB 估值+8有货")
    o = drain(store)[i]
    check("其他券→to_human", o["action"] == "to_human" and "其他券" in o["reason"], o["reason"])

    # 9) 归属不明(无券锚、对手无在途会话)→ to_human;有且仅 1 个在途会话 → L3 归属
    i = inbound("cp无锚", "估值+3可以出", hint=None)
    o = drain(store)[i]
    check("归属不明→to_human", o["action"] == "to_human" and "归属不明" in o["reason"], o["reason"])
    i = inbound("cp拒绝", "估值+3可以出", hint=None)
    o = drain(store)[i]
    check("无锚但单会话→L3 归属让步 +6", o["meta"]["route"] == "L3" and o["text"].endswith("估值+6能出么"), o["text"])

    # 10) 幂等:同 msg_id 重投不再回
    inbound("cp实价", "1.86能出3000", msg_id="t2")
    o = drain(store)
    check("重复 msg_id 不回复", "t2" not in o, str(list(o)))

    # 11) 卖出镜像:ofr 起始 估值-10,对方 -3 劣于边界 → 让到 -8
    i = inbound("cp卖出", "估值-3可以收", hint={**HINT, "方向": "ofr"})
    o = drain(store)[i]
    check("ofr 镜像让步 -8", o["text"] == "ofr 102400895.IB 24苏科技城MTN001 3000W 估值-8能收么", o["text"])

    # 12) 已转人工会话再来消息 → to_human
    i = inbound("cp实价", "那估值+8呢")
    o = drain(store)[i]
    check("已转人工会话新消息→to_human", o["action"] == "to_human" and "已转人工" in o["reason"], o["reason"])

    # 13) 红线闸
    g = phrase.guard("bid 102400895.IB 24苏科技城MTN001 3000W 我们要了成交")
    check("红线闸拦要约/成交词", not g.allowed and g.text == phrase.HOLD_TEXT, f"hit={g.hit}")
    g = phrase.guard("含担保可投量5000")
    check("红线闸拦内部字段", not g.allowed, f"hit={g.hit}")
    g = phrase.guard("bid 102400895.IB 24苏科技城MTN001 3000W 估值+8能出么")
    check("红线闸放行正常询价", g.allowed)

    # 状态落盘
    st = Path(os.environ["B_STATE_DIR"])
    check("状态落盘 sessions.json/trace.jsonl", (st / "sessions.json").exists() and (st / "trace.jsonl").exists())

    print(f"\n{'ALL PASS' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  (sandbox: {SANDBOX})")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
