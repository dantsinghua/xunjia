"""询价指令上行。未安装上传函数时不访问网络，也不连接企点。

返回 False 表示没有把指令交到公网 OSS。只有上传成功且采集端回执成功，
发送接口才能算送达。
"""
from __future__ import annotations

import threading
from typing import Any, Optional

_lock = threading.Lock()
_uploader: Any = None


def install_uploader(fn: Any) -> None:
    """安装本进程的上传函数。传入 None 表示没有上行。"""
    global _uploader
    with _lock:
        _uploader = fn


def installed_uploader() -> Any:
    with _lock:
        return _uploader


def upload_command(cmd: dict[str, Any]) -> bool:
    """交给已安装的上传函数。没有安装、或上传函数失败，都视为没有上行。"""
    fn = installed_uploader()
    if fn is None:
        return False
    try:
        return bool(fn(cmd))
    except Exception:
        return False


def upload_live(cmd: dict[str, Any]) -> bool:
    """密封上传并等待采集端回执。只有回执成功才算上行完成。"""
    from .oss_send import deliver_uploaded
    from .routes import log as command_log

    result = deliver_uploaded(command_log, cmd)
    return bool(isinstance(result, dict) and result.get("ok"))
