"""WF1 · 意图分类 + 结构抽取(docs/06)。PoC = 本地规则/关键词分类器,可选 OpenAI 兼容端点兜底。

铁律:只分类 + 抽数,**绝不比大小、不判好坏**。"估值+3可以出" → {还价, 偏离_bp:+3},
是不是拒绝由 engine.py 按方向符号算。

输出(严格按 06):
{
  "intent": "实价|虚价|明确拒绝|还价|非价格条件|反问|挂起|噪声",
  "extracted": {"基准": "估值|绝对|让步|null", "偏离_bp": 5, "价格": 1.86, "量_wan": 3000, "含糊词": [...]},
  "券锚线索": {"命中代码": "102400895", "命中简称片段": "苏科技城", "涉及多券": false},
  "置信": 0.0~1.0,
  "来源": "rules|llm"
}
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

INTENTS = ("实价", "虚价", "明确拒绝", "还价", "非价格条件", "反问", "挂起", "噪声")

# ---------- 词面归一 ----------
_FW = {ord(f): ord(t) for f, t in zip(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ％＋－．，？！",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz%+-.,?!")}


def norm_text(text: str | None) -> str:
    return (text or "").translate(_FW).strip()


# ---------- 券锚(L1 线索) ----------
RE_WINDCODE = re.compile(r"(?<![\d.])(\d{6,9})\.(IB|SH|SZ)\b", re.I)
RE_SHORTCODE = re.compile(r"(?<![\d.])((?:19|2[0-9])\d{4,7})(?![\d.])(?!\s*(?:万|亿|元|w|W|k|K))")
RE_BOND_NAME = re.compile(r"(?<![A-Za-z0-9.])(\d{2}[一-龥A-Z]{2,10}(?:MTN|PPN|SCP|CP|GN)\d{2,3}[AB]?"
                          r"|\d{2}[一-龥]{2,8}[A-Z]{0,2}\d{1,2}"
                          r"|\d{2}[一-龥]{2,8}债)(?![A-Za-z0-9])")


def bond_clues(t: str, session_hint: dict | None = None) -> dict:
    """券锚线索:文本中的代码/简称片段;涉及多券=文本里出现 >1 个不同代码。"""
    codes = [m.group(1) for m in RE_WINDCODE.finditer(t)]
    codes += [m.group(1) for m in RE_SHORTCODE.finditer(t)]
    names = [m.group(1) for m in RE_BOND_NAME.finditer(t)]
    uniq = sorted({c for c in codes})
    hit_name = None
    hint_name = (session_hint or {}).get("简称") or ""
    if names:
        hit_name = names[0]
    elif hint_name:
        # 简称片段:去掉年份/系列后缀的核心词是否出现在文本里(如 24苏科技城MTN001 → 苏科技城)
        core = re.sub(r"^\d{2}|(?:MTN|PPN|SCP|CP|GN)\d{2,3}[AB]?$|\d{1,2}$", "", hint_name)
        if core and core in t:
            hit_name = core
    return {"命中代码": uniq[0] if uniq else None,
            "命中简称片段": hit_name,
            "涉及多券": len(uniq) > 1 or len(set(names)) > 1,
            "全部代码": uniq}


def mask_bonds(t: str) -> str:
    t = RE_WINDCODE.sub("〖券〗", t)
    t = RE_BOND_NAME.sub("〖券〗", t)
    return RE_SHORTCODE.sub("〖券〗", t)


# ---------- 价格 ----------
RE_VAL_REL = re.compile(r"(?:估值|中债|估)\s*([+加上\-减下])\s*(\d+(?:\.\d+)?)\s*(?:个|bp|BP)?")
RE_VAL_HL = re.compile(r"(高|低)估值\s*(\d+(?:\.\d+)?)|估值\s*(高|低)\s*(\d+(?:\.\d+)?)")
RE_BARE_REL = re.compile(r"(?<![\d.\w])([+-])\s*(\d+(?:\.\d+)?)(?![\d.]|\s*(?:万|亿|w|W|k|K))")
RE_CONCEDE = re.compile(r"(?:再)?让\s*(\d+(?:\.\d+)?)\s*(?:个|bp|BP)?")
RE_ABS = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,4})(?:%|行权)?(?![\d.]|\s*(?:万|亿|w|W|k|K|年|Y|y))")

# ---------- 量 ----------
RE_VOL_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(kw|k|e|亿|万|个亿|w)(?![a-zA-Z])", re.I)
RE_VOL_BARE = re.compile(r"(?<![\d.+\-])(\d{3,6})(?![\d.])")
_VOL_WAN = {"kw": 1000, "k": 1000, "e": 10000, "亿": 10000, "个亿": 10000, "万": 1, "w": 1}

# ---------- 关键词 ----------
KW_VAGUE = ("大概", "大约", "参考", "估计", "差不多", "意向", "看看", "左右", "应该", "可能", "也许",
            "问一下", "问下", "问问", "报备", "请示", "回头", "再说", "试试", "不确定", "不一定",
            "大致", "估摸", "先看", "再看", "回你", "确认下", "确认一下", "落实下", "落实一下")
KW_NONPRICE = ("过券", "过桥", "清算", "+0", "+1", "T+0", "T+1", "t+0", "t+1", "含权", "行权",
               "到期口径", "代持", "授信", "结算", "券款对付", "见券付款", "DVP", "dvp", "分笔", "拆单")
KW_ASKBACK = ("你出个价", "你出价", "你出多少", "你什么价", "你们什么价", "你哪家", "你们哪家", "哪家的",
              "你是哪", "你们是哪", "什么量", "多少量", "你要多少", "你们要多少", "能给多少", "你报个",
              "你先出", "你先报", "你收多少", "你想多少", "你什么想法", "你们能收", "你能收", "你能出",
              "你出什么", "你哪里", "报个价", "你的价")
KW_REJECT = ("不行", "做不了", "不出", "出不了", "没有货", "没货", "没量", "没了", "不卖", "不考虑",
             "太低", "太高", "太远", "太差", "算了", "pass", "不搞", "没法", "做不到", "不接受", "达不到",
             "不够", "不合适", "不能", "再高", "再让", "高点", "高一点", "让点", "让一点", "加点", "多给",
             "再加", "差远了", "差太多", "不了", "免谈", "谈不了", "没戏", "不可能", "远了", "再往上")
KW_HOLD = ("等会", "等下", "等一下", "稍等", "一会", "回头回", "等我", "晚点", "过会", "马上回", "稍后",
           "我看下", "我问", "问一下", "问下", "在开会", "开完会")
KW_NOISE = ("你好", "您好", "在吗", "在么", "在的", "哈哈", "嗯", "好的", "收到", "谢谢", "谢啦", "ok", "okay",
            "早", "辛苦", "好嘞", "好呢", "行", "嗯嗯", "哦", "了解", "明白", "客气", "拜拜", "再见", "不客气",
            "[表情]", "[红包]", "[撤回]", "撤回了一条消息", "[动画表情]", "[系统消息]", "/微笑", "/握手", "/抱拳")
KW_ATTACH = ("[图片]", "[文件]", "[截图]", "[语音]", "[视频]")
RE_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")


def _has_any(t: str, kws) -> str | None:
    tl = t.lower()
    for k in kws:
        if k.lower() in tl:
            return k
    return None


def extract_price(t: str) -> tuple[dict | None, str]:
    """返回 (价格结构, 摘除价格后的文本)。价格结构 {基准, 偏离_bp, 价格, raw}。不做数值判断。"""
    m = RE_VAL_REL.search(t)
    if m:
        sign = 1 if m.group(1) in "+加上" else -1
        return ({"基准": "估值", "偏离_bp": sign * float(m.group(2)), "价格": None, "raw": m.group(0)},
                t[:m.start()] + "〖价〗" + t[m.end():])
    m = RE_VAL_HL.search(t)
    if m:
        hl = m.group(1) or m.group(3)
        n = float(m.group(2) or m.group(4))
        return ({"基准": "估值", "偏离_bp": n if hl == "高" else -n, "价格": None, "raw": m.group(0)},
                t[:m.start()] + "〖价〗" + t[m.end():])
    m = RE_CONCEDE.search(t)
    if m:
        return ({"基准": "让步", "偏离_bp": float(m.group(1)), "价格": None, "raw": m.group(0)},
                t[:m.start()] + "〖价〗" + t[m.end():])
    m = RE_ABS.search(t)
    if m:
        return ({"基准": "绝对", "偏离_bp": None, "价格": float(f"{m.group(1)}.{m.group(2)}"), "raw": m.group(0)},
                t[:m.start()] + "〖价〗" + t[m.end():])
    m = RE_BARE_REL.search(t)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return ({"基准": "估值", "偏离_bp": sign * float(m.group(2)), "价格": None, "raw": m.group(0)},
                t[:m.start()] + "〖价〗" + t[m.end():])
    return None, t


def extract_volume(t: str) -> int | None:
    """量 → 万。带单位优先;裸 3~6 位整数(3000/5000)当万。"""
    m = RE_VOL_UNIT.search(t)
    if m:
        return int(round(float(m.group(1)) * _VOL_WAN.get(m.group(2).lower(), 1)))
    m = RE_VOL_BARE.search(t)
    if m:
        return int(m.group(1))
    return None


def _empty() -> dict:
    return {"基准": None, "偏离_bp": None, "价格": None, "量_wan": None, "含糊词": [], "附件": False}


def classify_rules(text: str, ctx: dict | None = None) -> dict:
    """纯规则分类。ctx: {"asked_vol": bool, "session_hint": {...}}。"""
    ctx = ctx or {}
    raw = norm_text(text)
    clues = bond_clues(raw, ctx.get("session_hint"))
    t = mask_bonds(raw)
    ex = _empty()

    def out(intent: str, conf: float) -> dict:
        return {"intent": intent, "extracted": ex, "券锚线索": clues, "置信": conf, "来源": "rules"}

    if not t:
        return out("噪声", 0.95)
    if _has_any(t, KW_ATTACH):
        ex["附件"] = True
        return out("挂起", 0.6)          # 图片/附件报价需 WF2 转录,engine 转人工

    # 1) 非价格条件优先(带不带数字都转人工)
    k = _has_any(t, KW_NONPRICE)
    if k and not (k in ("+0", "+1") and RE_VAL_REL.search(t)):   # "估值+1" 不是清算+1
        ex["含糊词"] = []
        return out("非价格条件", 0.9)

    # 2) 价格/量抽取
    price, rest = extract_price(t)
    vol = extract_volume(rest)
    vague = [k for k in KW_VAGUE if k in t]
    ex["含糊词"] = vague
    if price:
        ex.update({"基准": price["基准"], "偏离_bp": price["偏离_bp"], "价格": price["价格"]})
    ex["量_wan"] = vol

    if price and price["基准"] == "让步":
        return out("还价", 0.75)
    if price:
        if vague:
            return out("虚价", 0.85)
        if vol is not None:
            return out("实价", 0.85)
        return out("还价", 0.85)
    if vol is not None and not vague and ctx.get("asked_vol"):
        return out("实价", 0.8)        # 追问量后给了明确量:升级实价(价格由 engine 用挂起价补)
    if vol is not None and not price:
        return out("虚价" if vague else "还价", 0.5)   # 只给量没给价:拿不准,低置信

    # 3) 无数字:反问 / 拒绝 / 挂起 / 噪声
    if _has_any(t, KW_ASKBACK) or (("?" in t or "?" in t) and "你" in t):
        return out("反问", 0.85)
    if _has_any(t, KW_REJECT):
        return out("明确拒绝", 0.8 if not vague else 0.6)
    if _has_any(t, KW_HOLD) or vague:
        return out("挂起", 0.75)
    stripped = re.sub(r"[\s,,。.!!~～…、/\[\]【】()()]+", "", t)
    stripped = RE_EMOJI.sub("", stripped)
    if not stripped or (_has_any(t, KW_NOISE) and len(stripped) <= 8):
        return out("噪声", 0.9)
    return out("噪声", 0.3)             # 未识别:低置信,engine 按"未拿到实价"保守等待


# ---------- OpenAI 兼容端点兜底(有 OPENAI_BASE/OPENAI_KEY 才调) ----------
_LLM_PROMPT = (
    "你是债券询价对话的意图分类器。只做分类和抽数,不判断好坏、不比较大小。\n"
    "意图只能是:实价|虚价|明确拒绝|还价|非价格条件|反问|挂起|噪声。\n"
    "实价=明确价格+明确量+无模糊词+无附加条件;虚价=大概/参考/估计/差不多/意向/看看/要问一下;"
    "还价=带数字的价格但没给量;非价格条件=清算+0/+1、过券/过桥、含权口径;反问=要我方报价/问我方是谁/什么量;"
    "挂起=等会回你;噪声=寒暄/表情/红包/系统。\n"
    "输出严格 JSON:{\"intent\":..., \"extracted\":{\"基准\":\"估值|绝对|null\",\"偏离_bp\":数或null,"
    "\"价格\":数或null,\"量_wan\":数或null,\"含糊词\":[]}, \"置信\":0~1}"
)


def llm_fallback(text: str, ctx: dict | None = None) -> dict | None:
    base, key = os.environ.get("OPENAI_BASE"), os.environ.get("OPENAI_KEY")
    if not base or not key:
        return None
    body = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0,
        "messages": [{"role": "system", "content": _LLM_PROMPT},
                     {"role": "user", "content": json.dumps({"text": text, "ctx": ctx or {}}, ensure_ascii=False)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("OPENAI_TIMEOUT", "20"))) as r:
            data = json.loads(r.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        obj = json.loads(content)
    except Exception:
        return None
    if obj.get("intent") not in INTENTS:
        return None
    ex = _empty()
    ex.update({k: obj.get("extracted", {}).get(k) for k in ("基准", "偏离_bp", "价格", "量_wan")})
    ex["含糊词"] = list(obj.get("extracted", {}).get("含糊词") or [])
    return {"intent": obj["intent"], "extracted": ex, "置信": float(obj.get("置信", 0.5)), "来源": "llm"}


LLM_THRESHOLD = float(os.environ.get("WF1_LLM_THRESHOLD", "0.6"))


def classify(text: str, ctx: dict | None = None) -> dict:
    """WF1 入口:规则优先;规则置信 < 阈值且配置了 OpenAI 端点 → 兜底,取置信更高者。"""
    res = classify_rules(text, ctx)
    if res["置信"] < LLM_THRESHOLD:
        alt = llm_fallback(text, ctx)
        if alt and alt["置信"] > res["置信"]:
            alt["券锚线索"] = res["券锚线索"]
            return alt
    return res
