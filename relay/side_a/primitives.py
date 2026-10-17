"""RPA 元操作(04 §2):adb 之上的读/写原语。

读:uiautomator dump → 节点列表(resource-id / content-desc / text / bounds),screencap 截图取证。
写:tap / swipe / back / keyevent,中文文本走 ADBKeyboard IME 广播(adb input text 不支持中文)。
一律「动作 → sleep → 读态验证 → 重试」,不假设即时生效。
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger("side_a.rpa")

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class Node:
    rid: str
    text: str
    desc: str
    cls: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    order: int  # 文档序,列表内即视觉从上到下

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def inside(self, other: "Node") -> bool:
        x1, y1, x2, y2 = self.bounds
        ox1, oy1, ox2, oy2 = other.bounds
        return x1 >= ox1 and y1 >= oy1 and x2 <= ox2 and y2 <= oy2


class Adb:
    def __init__(self, serial: str = config.ADB_SERIAL, adb: str = config.ADB):
        self.serial = serial
        self.adb = adb

    # ------------------------------------------------------------ 底层
    def run(self, *args: str, timeout: float = 20, check: bool = False) -> subprocess.CompletedProcess:
        cmd = [self.adb, "-s", self.serial, *args]
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=check)

    def shell(self, *args: str, timeout: float = 20) -> str:
        r = self.run("shell", *args, timeout=timeout)
        return r.stdout.decode("utf-8", "replace")

    def connect(self) -> bool:
        if not self.serial.startswith("emulator-"):
            subprocess.run([self.adb, "connect", self.serial], capture_output=True, timeout=15)
        for _ in range(10):
            if self.shell("getprop", "sys.boot_completed").strip() == "1":
                return True
            time.sleep(1)
        return False

    # ------------------------------------------------------------ 读类(零扰动)
    def dump(self, retries: int = 3) -> list[Node]:
        """uiautomator dump → 节点列表。空树/失败重试(04 §2a 空读重试 3 次)。"""
        last_err = None
        for i in range(retries):
            try:
                r = self.run("exec-out", "uiautomator", "dump", "/dev/tty", timeout=30)
                raw = r.stdout.decode("utf-8", "replace")
                end = raw.rfind("</hierarchy>")
                if end < 0:
                    raise RuntimeError(f"dump 无 hierarchy: {raw[-200:]!r}")
                nodes = _parse(raw[: end + len("</hierarchy>")])
                if nodes:
                    return nodes
                last_err = "empty tree"
            except Exception as e:  # noqa: BLE001
                last_err = e
            time.sleep(0.6 * (i + 1))
        raise RuntimeError(f"uiautomator dump 失败: {last_err}")

    def screencap(self, tag: str) -> Path:
        """截图取证,落 logs/shots/<ts>-<tag>.png,返回路径。失败不抛(取证不能反噬主流程)。"""
        d = config.LOG_DIR / "shots"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png"
        try:
            r = self.run("exec-out", "screencap", "-p", timeout=30)
            if r.returncode == 0 and r.stdout:
                p.write_bytes(r.stdout)
        except Exception as e:  # noqa: BLE001
            log.warning("screencap 失败: %s", e)
        return p

    def foreground_package(self) -> str:
        out = self.shell("dumpsys", "window", "|", "grep", "-E", "'mCurrentFocus|mFocusedApp'")
        m = re.search(r"([A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+", out)
        return m.group(1) if m else ""

    # ------------------------------------------------------------ 写类(有扰动)
    def tap(self, x: int, y: int, settle: float = 0.5) -> None:
        self.shell("input", "tap", str(x), str(y))
        time.sleep(settle)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300, settle: float = 0.6) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))
        time.sleep(settle)

    def keyevent(self, code: int | str, settle: float = 0.4) -> None:
        self.shell("input", "keyevent", str(code))
        time.sleep(settle)

    def back(self) -> None:
        self.keyevent("KEYCODE_BACK", settle=0.7)

    def launch(self, activity: str) -> None:
        self.shell("am", "start", "-n", activity)
        time.sleep(1.5)

    def broadcast(self, action: str, component: str | None = None, **extras: str) -> str:
        """am broadcast(测试钩子/IME 用)。Android 8+ 隐式广播到不了 manifest 注册的 receiver,
        测试钩子必须带显式 component(-n pkg/.Receiver);IME 广播是动态注册的,不用。
        extra 直接 --es,adb shell 对中文需加引号(_q)。"""
        args = ["am", "broadcast", "-a", action]
        if component:
            args += ["-n", component]
        for k, v in extras.items():
            args += ["--es", k, _q(v)]
        return self.shell(*args)

    # ------------------------------------------------------------ 文本输入(ADBKeyboard)
    def ensure_ime(self) -> bool:
        ime = config.ADBKEYBOARD_IME
        if ime not in self.shell("ime", "list", "-s"):
            self.shell("ime", "enable", ime)
        self.shell("ime", "set", ime)
        time.sleep(0.5)
        return ime in self.shell("settings", "get", "secure", "default_input_method")

    def input_text(self, text: str) -> None:
        """中文/特殊字符走 ADBKeyboard 的 B64 广播(不经过 shell 转义)。"""
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self.shell("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64)
        time.sleep(0.5)

    def clear_text(self) -> None:
        self.shell("am", "broadcast", "-a", "ADB_CLEAR_TEXT")
        time.sleep(0.3)


# ---------------------------------------------------------------- 节点查询工具
def find(nodes: list[Node], rid: str | None = None, desc: str | None = None, text: str | None = None,
         text_prefix: str | None = None, within: Node | None = None) -> list[Node]:
    out = []
    for n in nodes:
        if rid is not None and n.rid != rid:
            continue
        if desc is not None and n.desc != desc:
            continue
        if text is not None and n.text != text:
            continue
        if text_prefix is not None and not n.text.startswith(text_prefix):
            continue
        if within is not None and not n.inside(within):
            continue
        out.append(n)
    return out


def first(nodes: list[Node], **kw) -> Node | None:
    r = find(nodes, **kw)
    return r[0] if r else None


def _parse(xml_text: str) -> list[Node]:
    root = ET.fromstring(xml_text)
    out: list[Node] = []
    for i, el in enumerate(root.iter("node")):
        m = _BOUNDS_RE.match(el.get("bounds", ""))
        if not m:
            continue
        out.append(Node(
            rid=el.get("resource-id", ""),
            text=el.get("text", ""),
            desc=el.get("content-desc", ""),
            cls=el.get("class", ""),
            bounds=tuple(int(v) for v in m.groups()),  # type: ignore[arg-type]
            clickable=el.get("clickable") == "true",
            order=i,
        ))
    return out


def _q(s: str) -> str:
    """adb shell 会再经一层远端 sh 解析,单引号包裹并转义内部单引号。"""
    return "'" + s.replace("'", "'\\''") + "'"
