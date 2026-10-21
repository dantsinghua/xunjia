"""Innodealing credentials. Write-only secrets; empty secret fields retain saved values."""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
_FILES = {'account':'account.txt','sid':'sid.txt','cookie':'cookie.txt','device_id':'device_id.txt'}
_SECRETS = ('sid','cookie','device_id')
_LOCK = threading.RLock()
def config_dir() -> Path:
    raw = os.environ.get('QB_CONFIG_DIR', '').strip()
    return Path(raw).expanduser() if raw else Path.home()/'.config'/'qb'
def _read(directory: Path, key: str) -> str:
    path=directory/_FILES[key]
    if not path.is_file(): return ''
    if path.stat().st_size>65536: raise ValueError('行情凭据文件过大，请检查配置')
    return path.read_text(encoding='utf-8-sig').strip()
def snapshot(directory: Path | None = None) -> dict[str,str]:
    with _LOCK:
        folder=directory or config_dir()
        return {key:_read(folder,key) for key in _FILES}
def merged(body: Any, directory: Path | None = None) -> dict[str,str]:
    if not isinstance(body,dict) or set(body)-set(_FILES):
        raise ValueError('行情配置只接受账号、SID、Cookie和设备标识')
    result=snapshot(directory)
    for key,raw in body.items():
        if raw is None and key in _SECRETS: continue
        if not isinstance(raw,str): raise ValueError('行情配置字段必须是文本')
        if any(ord(c)<32 or ord(c)==127 for c in raw): raise ValueError('行情配置不能包含换行或控制字符')
        value=raw.strip()
        if len(value.encode('utf-8'))>(32768 if key=='cookie' else 4096 if key=='sid' else 256):
            raise ValueError('行情配置字段过长')
        if key not in _SECRETS or value: result[key]=value
    return result
def _write(directory: Path, key: str, value: str) -> None:
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(directory,0o700)
    fd,name=tempfile.mkstemp(prefix='.'+key+'-',dir=directory)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(value+'\n');f.flush();os.fsync(f.fileno())
        os.chmod(name,0o600)
        os.replace(name,directory/_FILES[key])
    finally:
        if os.path.exists(name):os.unlink(name)
def public_view(directory: Path | None = None) -> dict[str,Any]:
    values=snapshot(directory)
    return {'account':values['account'],**{key+'_set':bool(values[key]) for key in _SECRETS}}
def load_for_probe(directory: Path | None = None) -> tuple[str,str,str]:
    values=snapshot(directory)
    return values['cookie'],values['device_id'],values['sid']
def save_quote(body: Any, directory: Path | None = None) -> dict[str,Any]:
    with _LOCK:
        values=merged(body,directory)  # Validate all fields before any write.
        folder=directory or config_dir()
        for key in body:
            if key not in _SECRETS or body[key] and str(body[key]).strip(): _write(folder,key,values[key])
        return public_view(folder)