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
def resolve_device_id(cookie: str, device_id: str, *, explicit: bool = True) -> str:
    """A Cookie device overrides an implicit saved value, never an explicit conflict."""
    found=None;seen=set()
    for part in cookie.split(';'):
        name,separator,value=part.strip().partition('=')
        name=name.strip()
        if not separator:
            if name=='device_id':raise ValueError('Cookie 中设备标识格式无效，请重新复制 Cookie')
            continue
        if name in seen:raise ValueError('Cookie 中存在重复字段，请重新复制 Cookie')
        seen.add(name)
        if name!='device_id':continue
        value=value.strip()
        if len(value)>=2 and value.startswith('"') and value.endswith('"'):value=value[1:-1]
        # Cookie octets are ASCII; preserve the actual salt without URL decoding.
        if not value or len(value)>256 or any(ord(c)<33 or ord(c)>126 or c in '",;\\' for c in value):
            raise ValueError('Cookie 中设备标识格式无效，请重新复制 Cookie')
        found=value
    device_id=device_id.strip()
    if found is not None:
        if explicit and device_id and found!=device_id:
            raise ValueError('填写的设备标识与 Cookie 不一致，请使用同一次登录的凭据')
        return found
    return device_id
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
    explicit=isinstance(body.get('device_id'),str) and bool(body['device_id'].strip())
    result['device_id']=resolve_device_id(result['cookie'],result['device_id'],explicit=explicit)
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
    values=merged({},directory)
    return values['cookie'],values['device_id'],values['sid']
def save_quote(body: Any, directory: Path | None = None) -> dict[str,Any]:
    with _LOCK:
        values=merged(body,directory)  # Validate all fields before any write.
        folder=directory or config_dir()
        keys=[key for key in body if key not in _SECRETS or body[key] and str(body[key]).strip()]
        if values['device_id'] and values['device_id']!=_read(folder,'device_id') and 'device_id' not in keys:
            keys.append('device_id')
        for key in keys:_write(folder,key,values[key])
        return public_view(folder)
