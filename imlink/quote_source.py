"""innodealing 行情凭据。

账号可以回显。sid、cookie、device_id 只写不读：响应里只有有没有配过。
空字符串表示不修改已保存的机密。不连接 innodealing。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_FILES = {
    "account": "account.txt",
    "sid": "sid.txt",
    "cookie": "cookie.txt",
    "device_id": "device_id.txt",
}
_SECRETS = ("sid", "cookie", "device_id")


def config_dir() -> Path:
    raw = os.environ.get("QB_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".config" / "qb"


def _read(directory: Path, key: str) -> str:
    path = directory / _FILES[key]
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _write(directory: Path, key: str, value: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / _FILES[key]
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def public_view(directory: Path | None = None) -> dict[str, Any]:
    folder = directory or config_dir()
    return {
        "account": _read(folder, "account"),
        "sid_set": bool(_read(folder, "sid")),
        "cookie_set": bool(_read(folder, "cookie")),
        "device_id_set": bool(_read(folder, "device_id")),
    }


def load_for_probe(directory: Path | None = None) -> tuple[str, str, str]:
    folder = directory or config_dir()
    return _read(folder, "cookie"), _read(folder, "device_id"), _read(folder, "sid")


def save_quote(body: Any, directory: Path | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("配置必须是对象")
    folder = directory or config_dir()
    if "account" in body:
        _write(folder, "account", str(body.get("account") or "").strip())
    for key in _SECRETS:
        if key not in body:
            continue
        raw = body.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        _write(folder, key, str(raw).strip())
    return public_view(folder)
