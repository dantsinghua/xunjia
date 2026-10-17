"""开始询价的报价正文。空字段写成 --，同一 uin 的多只债用换行合成一条。"""
from __future__ import annotations

from typing import Any

EMPTY = "--"
_BLANK = {"", "--", "—", "–", "-"}


class RfqAssignError(ValueError):
    """请求不合法。调用方应返回 400，且不要写指令。"""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _missing(value: Any) -> bool:
    return _text(value) in _BLANK


def money2(value: Any) -> str:
    text = dash(value)
    if text == "--":
        return text
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return text
    return f"{number:.2f}"


def dash(value: Any) -> str:
    """空着的字段一律写成两个短横线。"""
    text = _text(value)
    if text in _BLANK:
        return EMPTY
    return text


def render_quote(code: str, fields: dict[str, Any] | None) -> str:
    """两行报价。第二行字段之间是两个空格。"""
    src = fields if isinstance(fields, dict) else {}
    line1 = f"{money2(src.get('price'))}*{dash(src.get('side'))}"
    parts = [
        dash(src.get("tenor")),
        dash(code),
        dash(src.get("name")),
        dash(src.get("placement")),
        f"{money2(src.get('bid'))}/{money2(src.get('ofr'))}",
        f"{dash(src.get('bid_vol'))}/{dash(src.get('ofr_vol'))}(*)",
        f"票面{dash(src.get('coupon'))}",
        f"{dash(src.get('issuer_rating'))}/{dash(src.get('bond_rating'))}",
        f"中债{money2(src.get('cnbd'))}",
        f"余额{dash(src.get('balance'))}",
        f"到期日{dash(src.get('maturity'))}",
        dash(src.get("broker")),
        dash(src.get("quoted_at")),
    ]
    return line1 + "\n" + "  ".join(parts)


def _uins(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for item in raw:
        uin = _text(item)
        if not uin or uin in seen:
            continue
        seen.add(uin)
        found.append(uin)
    return found


def build_batches(items: Any) -> list[dict[str, Any]]:
    """按 uin 出现顺序合并。同一人只留一批，正文用换行接起来。"""
    if not isinstance(items, list) or not items:
        raise RfqAssignError("没有收件人")
    pending: list[tuple[str, str, list[str]]] = []
    for item in items:
        if not isinstance(item, dict):
            raise RfqAssignError("没有收件人")
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        code = _text(item.get("code"))
        name = _text(fields.get("name"))
        if _missing(code):
            raise RfqAssignError("缺市场代码")
        if "text" in item:
            raw = item.get("text")
            body = "" if raw is None else str(raw)
            if not body.strip():
                raise RfqAssignError("正文为空")
        else:
            if _missing(name):
                raise RfqAssignError("缺简称")
            body = render_quote(code, fields)
            if _missing(body):
                raise RfqAssignError("没有报价正文")
        uins = _uins(item.get("uins"))
        if not uins:
            raise RfqAssignError("没有收件人")
        pending.append((code, body, uins))
    order: list[str] = []
    texts: dict[str, list[str]] = {}
    codes: dict[str, list[str]] = {}
    for code, text, uins in pending:
        for uin in uins:
            if uin not in texts:
                order.append(uin)
                texts[uin] = []
                codes[uin] = []
            texts[uin].append(text)
            if code not in codes[uin]:
                codes[uin].append(code)
    return [
        {"uin": uin, "text": "\n".join(texts[uin]), "codes": codes[uin]}
        for uin in order
    ]
