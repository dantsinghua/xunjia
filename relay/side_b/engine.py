"""规则引擎(docs/05 状态机)。比大小/让步/停不停/叫不叫人 全在这里,不给模型。

handle_inbound(store, env) -> outbound 信封(docs/07 下行契约 + meta 观察字段)。
"""
from __future__ import annotations

import time

from . import phrase, wf1
from .sessions import ASK_VOL, CLOSED_NOPRICE, ROUND_SENT, TO_HUMAN, Session, SessionStore, sign_of

LOW_CONF = 0.5


def _favorable(direction: str, a: float, b: float) -> bool:
    """a 是否对我方 **不劣于** b(签名偏离)。bid:收益率越高越有利 → a>=b;ofr 镜像。"""
    return a >= b - 1e-9 if direction == "bid" else a <= b + 1e-9


def _peer_signed_bp(s: Session, ex: dict) -> float | None:
    """对方价 → 签名偏离(BP)。估值基准直接用;绝对价需会话有估值;让步基准=当前报价往对方方向让 N。"""
    base = ex.get("基准")
    if base == "估值" and ex.get("偏离_bp") is not None:
        return float(ex["偏离_bp"])
    if base == "绝对" and ex.get("价格") is not None:
        if s.valuation is None:
            return None
        return round((float(ex["价格"]) - s.valuation) * 100, 2)
    if base == "让步" and ex.get("偏离_bp") is not None:
        return s.signed_current - sign_of(s.direction) * float(ex["偏离_bp"])
    return None


def _price_desc(s: Session, ex: dict) -> str:
    parts = []
    if ex.get("基准") == "估值" and ex.get("偏离_bp") is not None:
        parts.append(f"估值{ex['偏离_bp']:+g}")
    elif ex.get("基准") == "绝对" and ex.get("价格") is not None:
        parts.append(f"{ex['价格']}")
    elif ex.get("基准") == "让步":
        parts.append(f"让{ex['偏离_bp']:g}")
    if ex.get("量_wan") is not None:
        parts.append(f"{ex['量_wan']}万")
    return "/".join(parts)


class Decision(dict):
    """{action, text, reason, state, note}"""


def _to_human(s: Session, reason: str, ex: dict) -> Decision:
    s.state, s.state_reason = TO_HUMAN, reason
    return Decision(action="to_human", text=phrase.HOLD_TEXT, reason=reason,
                    note=phrase.human_note(s.direction, s.windcode, s.简称, s.vol_wan, _price_desc(s, ex), reason))


def _next_round_or_close(s: Session, why: str) -> Decision:
    if s.at_boundary():
        s.state, s.state_reason = CLOSED_NOPRICE, f"{why},已到探价边界未获实价"
        return Decision(action="send", text=phrase.CLOSE_TEXT, reason=s.state_reason)
    s.concede()
    s.state, s.state_reason = ROUND_SENT, f"{why},让步至估值{sign_of(s.direction) * s.current_bp:+g}"
    return Decision(action="send", text=phrase.round_text(s.direction, s.windcode, s.简称, s.vol_wan, s.current_bp),
                    reason=s.state_reason)


def _ask_vol_or_human(s: Session, ex: dict, why: str) -> Decision:
    if s.asked_vol:
        return _to_human(s, f"{why},追问量后仍模糊", ex)
    s.asked_vol, s.state = True, ASK_VOL
    s.pending_price = {k: ex.get(k) for k in ("基准", "偏离_bp", "价格")}
    s.state_reason = f"{why},追问量"
    return Decision(action="send", text=phrase.ask_vol_text(s.简称), reason=s.state_reason)


def decide(s: Session, res: dict) -> Decision:
    intent, ex, conf = res["intent"], res["extracted"], float(res.get("置信", 0))

    if not s.is_open():
        return _to_human(s, f"会话已{ '转人工' if s.state == TO_HUMAN else '结束' },新消息转人工", ex)
    if ex.get("附件"):
        return _to_human(s, "对方发图片/附件报价,需 WF2 转录", ex)

    if intent == "噪声" and conf >= LOW_CONF:
        return Decision(action="none", reason="噪声,忽略")
    if s.state == ASK_VOL and intent in ("噪声", "挂起", "虚价", "明确拒绝"):
        return _to_human(s, f"追问量后仍模糊({intent})", ex)     # docs/05 §2:追问量后仍模糊 → 交易员判
    if intent == "噪声":
        return Decision(action="none", reason="未识别(低置信),按未拿到实价保持等待")
    if intent == "挂起":
        return Decision(action="none", reason="对方挂起,保持等待不追问")
    if intent == "非价格条件":
        return _to_human(s, "非价格条件(清算/过券过桥/含权口径)", ex)
    if intent == "反问":
        return _to_human(s, "反问(要我方报价/问量/问身份)", ex)
    if conf < LOW_CONF:
        return _to_human(s, f"意图{intent}置信{conf:.2f}过低", ex)

    vol = ex.get("量_wan")
    if vol is not None and s.investable_wan is not None and vol > s.investable_wan:
        return _to_human(s, f"加量{vol}万超本方可投量", ex)

    if intent == "实价":
        if ex.get("基准") is None and s.pending_price:      # 追问量后只回了量:补挂起价
            ex = {**ex, **{k: v for k, v in s.pending_price.items() if v is not None}}
        return _to_human(s, "拿到实价", ex)

    if intent == "虚价":
        return _ask_vol_or_human(s, ex, "虚价/模糊")

    if intent == "明确拒绝":
        return _next_round_or_close(s, "明确拒绝")

    if intent == "还价":
        if s.state == ASK_VOL:
            return _to_human(s, "追问量后仍未给量", ex)
        peer = _peer_signed_bp(s, ex)
        if peer is None:
            return _to_human(s, "还价无法换算(绝对价缺估值基准或只给量)", ex)
        if _favorable(s.direction, peer, s.signed_current):
            s.agreed_bp = peer
            return _ask_vol_or_human(s, ex, f"对方接受当前报价(估值{peer:+g})未给量")
        if _favorable(s.direction, peer, s.signed_boundary):
            s.agreed_bp = peer
            s.current_bp = abs(peer)
            return _ask_vol_or_human(s, ex, f"还价估值{peer:+g}在探价边界内")
        return _next_round_or_close(s, f"还价估值{peer:+g}劣于边界=拒绝")

    return _to_human(s, f"未知意图 {intent}", ex)


def handle_inbound(store: SessionStore, env: dict) -> dict | None:
    """一条 inbound → 解析 → 归属 → 决策 → 出口闸 → outbound 信封。重复 msg_id 返回 None。"""
    msg_id = str(env.get("msg_id") or "")
    if msg_id and msg_id in store.processed:
        store.trace(kind="dup_skip", msg_id=msg_id)
        return None
    thread = str(env.get("thread") or "")
    hint = env.get("session_hint") or {}
    text = env.get("text") or ""

    out = {"v": 1, "type": "outbound", "in_reply_to": msg_id, "thread": thread,
           "session": {"windcode": hint.get("windcode")}, "action": "none", "text": "", "reason": "",
           "ts": time.time(), "meta": {}}

    # 归属:L1 信封券锚;缺则 L3(该对手只有 1 个未闭合会话);否则归属不明转人工
    s = None
    level = None
    if hint.get("windcode"):
        s, level = store.get_or_create(thread, hint), "L1"
    else:
        opens = store.open_sessions_of(thread)
        if len(opens) == 1:
            s, level = opens[0], "L3"
            out["session"]["windcode"] = s.windcode
    res = wf1.classify(text, {"asked_vol": bool(s and s.state == ASK_VOL), "session_hint": hint})
    out["meta"].update(intent=res["intent"], extracted=res["extracted"], 券锚线索=res["券锚线索"],
                       置信=res["置信"], wf1_source=res.get("来源"), route=level)

    if s is None:
        out.update(action="to_human", text=phrase.HOLD_TEXT, reason="归属不明(无券锚且对手在途会话≠1)")
        dec = Decision(action="to_human", reason=out["reason"])
    else:
        clues = res["券锚线索"]
        own = s.windcode.split(".")[0]
        others = [c for c in clues.get("全部代码", []) if c != own and not own.startswith(c)]
        if others or clues.get("涉及多券"):
            dec = _to_human(s, f"对方发其他券/多券报价 {others or ''}", res["extracted"])
        else:
            dec = decide(s, res)
        s.history.append({"ts": env.get("ts"), "msg_id": msg_id, "text": text, "intent": res["intent"],
                          "extracted": res["extracted"], "route": level, "决策": dict(dec)})
        s.updated_ts = time.time()
        out.update(action=dec["action"], text=dec.get("text", ""), reason=dec.get("reason", ""))
        out["meta"].update(state=s.state, round=s.round, current_bp=sign_of(s.direction) * s.current_bp,
                           max_rounds=s.max_rounds, direction=s.direction)
        if dec.get("note"):
            out["meta"]["human_note"] = dec["note"]

    # 出口闸:所有对外话术必过(to_human 的对手方话术也过)
    if out["text"]:
        g = phrase.guard(out["text"])
        if not g.allowed:
            out["text"] = g.text
            out["meta"]["blocked"] = {"hit": g.hit, "reason": g.reason}
    if out["action"] != "send":
        out["text"] = out["text"] if out["action"] == "to_human" else ""

    if msg_id:
        store.processed.add(msg_id)
    store.trace(kind="round", msg_id=msg_id, thread=thread, inbound_text=text, wf1=res,
                session=(s.key if s else None), decision=dict(dec), outbound=out)
    store.save()
    return out
