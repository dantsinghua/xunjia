"""QB(羿珩 DM)HTTP 请求签名复刻 —— 逆自 quote-web main bundle。

sign = md5( 各 query 参数值按 key 排序后拼接(数组元素直接相连) + device_id )
  - 固定参数:timestamp(毫秒)、v(前端版本号,如 "14260")、qIntl="0"
  - 排序对「键」排序,拼接「值」;sign 本身不参与
  - 盐 = device_id(浏览器生成并持久化在 cookie/localStorage 的设备标识)
device_id 与登录 cookie 均来自安琳本人浏览器,存 ~/.config/qb/(600),不入库/日志。
"""
from __future__ import annotations

import hashlib
import time


def sign_params(params: dict[str, str], device_id: str, v: str = "14260") -> dict[str, str]:
    p = dict(params)
    p.setdefault("timestamp", str(int(time.time() * 1000)))
    p["v"] = v
    p["qIntl"] = "0"
    concat = "".join(str(p[k]) for k in sorted(p) if k != "sign")
    p["sign"] = hashlib.md5((concat + device_id).encode("utf-8")).hexdigest()
    return p


def verify(params: dict[str, str], device_id: str) -> bool:
    """用真实抓到的 (params 含 sign) 反验 device_id 是否为正确的盐。"""
    got = params.get("sign")
    concat = "".join(str(params[k]) for k in sorted(params) if k != "sign")
    calc = hashlib.md5((concat + device_id).encode("utf-8")).hexdigest()
    return got == calc
