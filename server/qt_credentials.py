"""Protected institution-wide QTrade settings; CAS writes and write-only password.

Connection configuration is shared. Account-to-user bindings are server-owned:
browser registration can only add unclaimed QT ids to the authenticated caller.
Existing bindings cannot be moved or removed by another user's save operation.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

DEFAULTS = {'mode':'fix', 'enabled':False, 'host':'', 'port':0, 'username':'', 'password':'',
            'sender_comp_id':'', 'target_comp_id':'', 'transport':'tls', 'plaintext_confirmed':False,
            'server_name':'', 'ca_file':'', 'client_cert_file':'', 'client_key_file':'',
            'heartbeat_seconds':60, 'connect_timeout_seconds':10, 'encoding':'utf-8',
            'sending_timezone':'Asia/Shanghai', 'timezone':'Asia/Shanghai', 'account_owners':{},
            'source_id':'qtrade'}
EDITABLE = frozenset(DEFAULTS) - {'enabled','mode','account_owners','source_id'}
MAX_CONFIG = 65536


class QtConfigurationError(ValueError):
    pass


def safe_path(path):
    path=Path(path).absolute()
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise QtConfigurationError('QT_CONFIG_LINK_FORBIDDEN')
    return path


def load(path):
    path=safe_path(path)
    if not path.exists():return dict(DEFAULTS,account_owners={}), 'new'
    if not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size>MAX_CONFIG:
        raise QtConfigurationError('QT_CONFIG_MUST_BE_PRIVATE')
    raw=path.read_bytes()
    try:
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out:raise ValueError()
                out[k]=v
            return out
        data=json.loads(raw,object_pairs_hook=unique)
    except (ValueError,UnicodeError):raise QtConfigurationError('QT_CONFIG_INVALID') from None
    if not isinstance(data,dict):raise QtConfigurationError('QT_CONFIG_INVALID')
    if 'mode' not in data and 'journal_path' in data:
        # Read-only compatibility: migration occurs only on a complete explicit save.
        if set(data)!={'enabled','source_id','account_owners','timezone','journal_path'}:
            raise QtConfigurationError('QT_CONFIG_INVALID')
        if type(data['enabled']) is not bool or not isinstance(data['account_owners'],dict):
            raise QtConfigurationError('QT_CONFIG_INVALID')
        return data, hashlib.sha256(raw).hexdigest()
    if set(data)-set(DEFAULTS) or data.get('mode')!='fix':raise QtConfigurationError('QT_CONFIG_INVALID')
    values={**DEFAULTS,**data}
    validate(values,ready=False)
    return values,hashlib.sha256(raw).hexdigest()


def validate(values, *, ready):
    for name in ('enabled','plaintext_confirmed'):
        if type(values.get(name)) is not bool:raise QtConfigurationError('QT_BOOLEAN_INVALID')
    for name in ('host','username','password','sender_comp_id','target_comp_id','server_name','source_id',
                 'ca_file','client_cert_file','client_key_file','encoding','timezone','sending_timezone','transport'):
        v=values.get(name)
        if not isinstance(v,str) or len(v)>4096 or any(ord(c)<32 or ord(c)==127 for c in v):
            raise QtConfigurationError('QT_CONFIG_TEXT_INVALID')
    for name in ('sender_comp_id','target_comp_id','source_id'):
        v=values[name]
        if v and not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',v):raise QtConfigurationError('QT_COMP_ID_INVALID')
    for name in ('host','server_name'):
        v=values[name]
        if not v:continue
        try:ipaddress.ip_address(v)
        except ValueError:
            if len(v)>253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',v) or '..' in v:
                raise QtConfigurationError('QT_HOST_INVALID')
    for name,minimum,maximum in [('port',0,65535),('heartbeat_seconds',1,120),('connect_timeout_seconds',1,30)]:
        if type(values[name]) is not int or not minimum<=values[name]<=maximum:raise QtConfigurationError('QT_CONFIG_NUMBER_INVALID')
    if values['transport'] not in ('tls','tcp'):raise QtConfigurationError('QT_TRANSPORT_INVALID')
    if values['transport']=='tcp' and not values['plaintext_confirmed']:raise QtConfigurationError('QT_PLAINTEXT_NOT_CONFIRMED')
    if values['encoding'] not in ('utf-8','gb18030','ascii'):raise QtConfigurationError('QT_ENCODING_INVALID')
    for name in ('timezone','sending_timezone'):
        if values[name] not in ('Asia/Shanghai','UTC'):raise QtConfigurationError('QT_TIMEZONE_INVALID')
        ZoneInfo(values[name])
    if bool(values['client_cert_file'])!=bool(values['client_key_file']):raise QtConfigurationError('QT_CLIENT_CERT_PAIR_REQUIRED')
    for name in ('ca_file','client_cert_file','client_key_file'):
        if values[name] and not Path(values[name]).is_absolute():raise QtConfigurationError('QT_TLS_ABSOLUTE_PATH_REQUIRED')
    owners=values.get('account_owners')
    if not isinstance(owners,dict) or len(owners)>100 or any(
        not isinstance(k,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,160}',k)
        or not isinstance(v,str) or not v or len(v)>160 or any(ord(c)<32 for c in v)
        for k,v in owners.items()):raise QtConfigurationError('QT_ACCOUNT_MAPPING_INVALID')
    if ready and (not all(values[k] for k in ('host','port','username','password','sender_comp_id','target_comp_id')) or not owners):
        raise QtConfigurationError('QT_CONNECTION_AND_ACCOUNT_MAPPING_REQUIRED')


def merge(saved, body, owner, revision):
    if not isinstance(body,dict) or set(body)-EDITABLE-{'expected_revision','qt_account_ids'}:
        raise QtConfigurationError('QT_CONFIG_FIELDS_INVALID')
    if body.get('expected_revision')!=revision:raise QtConfigurationError('QT_CONFIG_CHANGED_RELOAD')
    legacy='journal_path' in saved
    result={**DEFAULTS,**{k:v for k,v in saved.items() if k in DEFAULTS},'enabled':False,'mode':'fix'}
    result['account_owners']=dict(result['account_owners'])
    for key in EDITABLE & body.keys():
        if key=='password' and body[key]=='':continue
        result[key]=body[key]
    if result['username']!=saved.get('username','') and not body.get('password'):
        raise QtConfigurationError('QT_NEW_ACCOUNT_PASSWORD_REQUIRED')
    accounts=body.get('qt_account_ids',[])
    if not isinstance(accounts,list) or len(accounts)>100 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,160}',x) for x in accounts):
        raise QtConfigurationError('QT_ACCOUNT_IDS_INVALID')
    for account in accounts:
        if result['account_owners'].get(account,owner)!=owner:raise QtConfigurationError('QT_ACCOUNT_ALREADY_BOUND')
        result['account_owners'][account]=owner
    validate(result,ready=True)
    return result,legacy


def public(values, owner, revision):
    legacy='journal_path' in values
    out={key:values.get(key,DEFAULTS.get(key)) for key in EDITABLE if key!='password'}
    out.update(revision=revision,mode='journal' if legacy else 'fix',shared=True,
               password_set=bool(values.get('password')),enabled=values.get('enabled',False),
               qt_account_ids=sorted(k for k,v in values.get('account_owners',{}).items() if v==owner),
               mapped=owner in values.get('account_owners',{}).values(),
               mapped_account_count=len(values.get('account_owners',{})))
    return out


def write(path, values):
    if 'journal_path' in values:
        if set(values)!={'enabled','source_id','account_owners','timezone','journal_path'} or type(values['enabled']) is not bool:
            raise QtConfigurationError('QT_CONFIG_INVALID')
    else:validate(values,ready=False)
    path=safe_path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw=(json.dumps(values,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    if len(raw)>MAX_CONFIG:raise QtConfigurationError('QT_CONFIG_TOO_LARGE')
    fd,name=tempfile.mkstemp(prefix='.qt-config-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


@contextmanager
def config_lock(path):
    from .quote_control import _Lease
    path=safe_path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lease=_Lease(safe_path(path.with_name(path.name+'.config-lock')))
    lease.acquire()
    try:yield
    finally:lease.release()
