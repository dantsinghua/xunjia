"""A 侧配置:adb 目标、目标 APP 的定位表(profile)、轮询节奏、状态目录。

定位一律优先 resource-id / content-desc,坐标只作最后退路(04 §1)。
换真实企点时只需新增一个 profile(把 resource-id 填进去),RPA 其它层不动。
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
LOG_DIR = HERE / "logs"
APK_DIR = HERE / "apk"

ADB = os.environ.get("SIDE_A_ADB", "adb")   # macOS:宿主 adb 起不来时可指到 relay/side_a/vadb.sh(经 colima VM 转发);Linux/WSL 直接用 apt 的 adb
# redroid 发布在 5555,adb server 会自动扫 localhost:5555 并按端口命名为 emulator-5554(console 端口=5555-1);
# 这是 adb 客户端自身行为(transport_local.cpp PollAllLocalPortsForEmulator),macOS / Linux / WSL 一致——
# 2026-09-16 在 WSL x86 用假监听实测:adb server 一起来就每 ~1.2s 向 127.0.0.1:5555 发 CNXN,无需 adb connect。
# 不要用 `adb connect 127.0.0.1:5555`,会和 emulator 扫描互相卡死(同一 adbd 注册成两个 transport)。
ADB_SERIAL = os.environ.get("SIDE_A_SERIAL", "emulator-5554")
TARGET = os.environ.get("SIDE_A_TARGET", "mockqidian")

POLL_INTERVAL = float(os.environ.get("SIDE_A_INTERVAL", "4"))   # 主循环节奏(秒)
SEND_MIN_GAP = 1.5                                               # 外发限速 ≥1.5s(04 §5)
CURSOR_K = 3                                                     # 游标锚块长度(04 §3.6)
ALARM_RATE_S = 3600                                              # UNKNOWN 告警限频(04 §3.1)

ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"

# 会话 → 券锚的静态映射(session_hint 兜底;首选从我方历史外发的券锚文本里回读)
THREAD_SESSIONS: dict[str, dict] = {
    "张三-华泰固收": {"windcode": "102400895.IB", "简称": "24苏科技城MTN001"},
}

PROFILES: dict[str, dict] = {
    # 自建 mock 目标(relay/side_a/mock_app),原生 View,resource-id 完整
    "mockqidian": {
        "package": "com.xunjia.mockqidian",
        "activity": "com.xunjia.mockqidian/.MainActivity",
        "list_root": "com.xunjia.mockqidian:id/thread_list",
        "thread_name": "com.xunjia.mockqidian:id/thread_name",
        "thread_unread": "com.xunjia.mockqidian:id/thread_unread",
        "chat_title": "com.xunjia.mockqidian:id/chat_title",
        "msg_list": "com.xunjia.mockqidian:id/msg_list",
        "msg_text": "com.xunjia.mockqidian:id/msg_text",
        "input": "com.xunjia.mockqidian:id/input",
        "send": "com.xunjia.mockqidian:id/send",
        "login_marker": None,   # mock 无登录页
        # 发送方判定:content-desc == "me" 为我方,其余视为对方(截图失败保守判对方,04 §2c)
        "me_desc": "me",
    },
    # 真实企点:Flutter 自绘,resource-id 需实机 dump 后填入;读消息大概率要截图+多模态兜底(04 §1)
    "qidian": {
        "package": "com.tencent.qidian",
        "activity": None,
        "list_root": None,
        "thread_name": None,
        "thread_unread": None,
        "chat_title": None,
        "msg_list": None,
        "msg_text": None,
        "input": None,
        "send": None,
        "login_marker": None,
        "me_desc": "me",
    },
}


def profile(name: str | None = None) -> dict:
    p = PROFILES[name or TARGET]
    missing = [k for k in ("list_root", "thread_name", "chat_title", "msg_text", "input", "send") if not p.get(k)]
    if missing:
        raise RuntimeError(f"profile {name or TARGET} 缺定位项 {missing},需实机 uiautomator dump 后补齐")
    return p
