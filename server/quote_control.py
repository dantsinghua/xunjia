"""Explicit Innodealing control. Public prices remain on the V2.2 market pipeline."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import suppress
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from imlink.quote_source import config_dir, public_view, merged, save_quote
_REQUIRED=('QB_MYSQL_HOST','QB_MYSQL_USER','QB_MYSQL_PASSWORD','QB_MYSQL_DB')
def database_options():
    if not all(os.environ.get(k,'').strip() for k in _REQUIRED):
        raise ValueError('尚未配置行情数据库；请配置 QB_MYSQL_HOST、USER、PASSWORD、DB')
    return dict(host=os.environ['QB_MYSQL_HOST'],port=int(os.environ.get('QB_MYSQL_PORT','3306')),
                user=os.environ['QB_MYSQL_USER'],password=os.environ['QB_MYSQL_PASSWORD'],
                database=os.environ['QB_MYSQL_DB'],charset='utf8mb4',autocommit=True,
                connect_timeout=5,read_timeout=8,write_timeout=8)
def check_database(options=None):
    """Read-only dependency check: never creates tables, migrates or truncates data."""
    try:
        options=database_options() if options is None else options
    except (ValueError,KeyError):
        return {'ok':False,'code':'QUOTE_DATABASE_NOT_CONFIGURED','note':'行情数据库参数尚未完整配置'}
    try:
        import pymysql
        from collector.store import Q_COLS,T_COLS
        with pymysql.connect(**options) as db:
            with db.cursor() as c:
                required={'qb_live_best_quote':set(Q_COLS)|{'id'},'qb_live_trade':set(T_COLS)|{'id'},
                          'qb_bond_dict':{'bond_uni_code','bond_code','bond_short_name'},'qb_broker':{'broker_id','broker_name'}}
                for table,columns in required.items():
                    c.execute('SHOW COLUMNS FROM '+table)
                    missing=columns-{r[0] for r in c.fetchall()}
                    if missing:
                        return {'ok':False,'code':'QUOTE_SCHEMA_MIGRATION_REQUIRED','note':'行情表缺列：'+table+'.'+','.join(sorted(missing))+'；须执行对应显式迁移，不能直接跳过'}
                c.execute('SELECT 1 FROM qb_bond_dict LIMIT 1')
                if c.fetchone() is None:
                    return {'ok':False,'code':'QUOTE_DICTIONARY_EMPTY','note':'债券代码／简称字典为空；须先导入真实字典，不能用虚构代码填充'}
        return {'ok':True,'code':'QUOTE_DATABASE_READY','note':'行情数据库、报价表和真实债券字典可用'}
    except Exception as exc:
        code=getattr(exc,'args',(None,))[0] if getattr(exc,'args',()) else None
        if code==1146:
            return {'ok':False,'code':'QUOTE_TABLE_MISSING','note':'行情数据库缺少必要表，请先应用行情建表脚本'}
        if code in (1044,1045):
            return {'ok':False,'code':'QUOTE_DATABASE_AUTH_FAILED','note':'行情数据库鉴权或访问权限不足'}
        return {'ok':False,'code':'QUOTE_DATABASE_UNAVAILABLE','note':'行情数据库连接失败，请核对数据库服务、地址和网络'}
class _Lease:
    def __init__(self,path):self.path=path;self.file=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.path,os.O_RDWR|os.O_CREAT,0o600)
        self.file=os.fdopen(fd,'r+b')
        try:
            if os.name=='nt':
                import msvcrt
                if self.path.stat().st_size==0:self.file.write(b'0');self.file.flush()
                self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.release();raise ValueError('已有行情采集实例占用此配置，请先停止原实例') from None
    def release(self):
        if self.file is not None:self.file.close();self.file=None
class QuoteControl:
    def __init__(self,*,directory=None,reader=None,managers=None,probe=None,session=None,db_check=None):
        self.directory=Path(directory) if directory else config_dir()
        self.reader=reader
        self.managers=set(managers if managers is not None else (v.strip() for v in os.environ.get('XUNJIA_QUOTE_MANAGERS','').split(',') if v.strip()))
        self._probe=probe;self._session=session;self._db_check=db_check or (lambda:check_database(getattr(getattr(self.reader,"source",None),"connection_options",None)))
        self._lock=asyncio.Lock();self._task=None;self._lease=None
        self._state='stopped';self._note='采集尚未启动';self._code='QUOTE_STOPPED'
        self._stats={'quotes_received':0,'quotes_written':0,'trades_written':0,'decode_errors':0,'last_quote_at':None,'last_frame_at':None}
        self._reader_health={'ok':False,'note':'行情读取尚未开始','last_poll_at':None}
        self._last_probe=None;self._database=None
    def _database_options(self):
        options=getattr(getattr(self.reader,"source",None),"connection_options",None)
        return options if options is not None else database_options()
    def can_manage(self,user):return user in self.managers
    def require_manager(self,user):
        if not self.can_manage(user):raise PermissionError('当前用户没有公共行情管理权限；请配置 XUNJIA_QUOTE_MANAGERS')
    def status(self,user):
        data=public_view(self.directory)
        if not self.can_manage(user):data['account']=''
        active=self._task is not None and not self._task.done()
        return {**data,'configured':bool((data['cookie_set'] or data['sid_set']) and data['device_id_set']),
                'managed':True,'can_manage':self.can_manage(user),'source':'innodealing',
                'reader_configured':self.reader is not None,'collector_state':self._state,'collector_active':active,
                'note':self._note,'code':self._code,'stats':dict(self._stats),
                'database':self._database,'reader':dict(self._reader_health),'last_test':self._last_probe}
    def reader_result(self,*,ok,note):
        self._reader_health={'ok':ok,'note':note,'last_poll_at':time.time()}
    async def save(self,user,body):
        self.require_manager(user)
        async with self._lock:
            if self._task and not self._task.done():raise ValueError('请先停止采集，再修改行情凭据；保存不会隐式启动采集')
            save_quote(body,self.directory)
            self._last_probe=None
            return self.status(user)
    async def test(self,user,body):
        self.require_manager(user)
        async with self._lock:
            if self._task and not self._task.done():
                if any(isinstance(v,str) and v.strip() for k,v in body.items() if k!='account'):
                    raise ValueError('采集正在运行，请先停止再测试新的凭据')
                return {'ok':self._state=='streaming','wss_open':self._state=='streaming','note':'已复用当前采集状态，未再建立第二条行情连接','database':self._database}
            values=merged(body,self.directory)
            if self._probe:
                result=await self._probe(values)
            else:
                from imlink.quote_probe import probe_quote,market_connect
                from collector.qb_gateway import authenticate,WS
                from collector.qb_ws import ws_url
                result=await probe_quote(creds=lambda:(values['cookie'],values['device_id'],values['sid']),authenticate=authenticate,connect=market_connect,ws_url=ws_url,ws_base=WS)
            self._database=await asyncio.to_thread(self._db_check)
            self._last_probe={**result,'at':time.time()}
            return {**result,'database':self._database,'reader_configured':self.reader is not None,'saved':False}
    def _desired(self,value):
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,p=tempfile.mkstemp(prefix='.collector-',dir=self.directory)
        try:
            with os.fdopen(fd,'w') as f:json.dump({'enabled':value},f);f.flush();os.fsync(f.fileno())
            os.chmod(p,0o600);os.replace(p,self.directory/'collector-state.json')
        finally:
            if os.path.exists(p):os.unlink(p)
    async def start(self,user,*,persist=True):
        self.require_manager(user)
        async with self._lock:
            if self._task and not self._task.done():return self.status(user)
            values=merged({},self.directory)
            if not (values['cookie'] or values['sid']) or not values['device_id']:raise ValueError('请先保存有效的 Cookie／SID 和设备标识')
            if self.reader is None:raise ValueError('行情读取未配置：须启用 XUNJIA_QUOTE_SOURCE=qb、行情数据库和 XUNJIA_QUOTE_CACHE；仅连接WebSocket不能让报价进入工作台')
            self._database=await asyncio.to_thread(self._db_check)
            if not self._database['ok']:raise ValueError(self._database['note'])
            lease=_Lease(self.directory/'collector.lock');lease.acquire()
            try:
                if persist:self._desired(True)
                self._lease=lease;self._state='starting';self._code='QUOTE_STARTING';self._note='正在认证并建立行情连接'
                self._task=asyncio.create_task(self._supervise(values),name='innodealing-collector')
            except BaseException:lease.release();raise
            return self.status(user)
    async def stop(self,user,*,persist=True):
        self.require_manager(user)
        async with self._lock:
            if persist:self._desired(False)
            if self._task:
                self._task.cancel()
                with suppress(asyncio.CancelledError):await self._task
                self._task=None
            if self._lease:self._lease.release();self._lease=None
            self._state='stopped';self._code='QUOTE_STOPPED';self._note='采集已停止；历史行情保留，未删除数据'
            return self.status(user)
    async def startup(self):
        if os.environ.get('XUNJIA_QUOTE_AUTOSTART')!='1' or not self.managers:return
        try:
            saved=json.loads((self.directory/'collector-state.json').read_text())
            if saved.get('enabled') is True:await self.start(sorted(self.managers)[0],persist=False)
        except FileNotFoundError:pass
        except Exception:
            self._state='error';self._code='QUOTE_RESUME_FAILED';self._note='行情恢复未完成，请在采集通道检查凭据和数据库后重新启动'
    async def shutdown(self):
        if self.managers:await self.stop(sorted(self.managers)[0],persist=False)
    def _update(self,**values):
        for key in self._stats:
            if key in values:self._stats[key]=values[key]
        if 'state' in values:self._state=values['state']
        if self._state=='streaming':self._code='QUOTE_STREAM_OPEN';self._note='行情连接已打开；是否收到报价请查看实际入库计数'
    async def _supervise(self,values):
        attempt=0
        try:
            while True:
                try:
                    self._state='connecting'
                    if self._session:await self._session(values,self._update)
                    else:
                        from collector.managed_session import collect
                        await collect(values,self._update,self._database_options())
                    attempt=0
                except asyncio.CancelledError:raise
                except Exception as exc:
                    import httpx
                    denied=isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in (401,403)
                    rejected=denied or type(exc).__name__=='QuoteAuthenticationError'
                    self._code='QUOTE_AUTH_REJECTED' if rejected else 'QUOTE_CONNECTION_FAILED'
                    self._note='行情认证失效，请更新Cookie／SID和设备标识后重试' if rejected else '行情连接或入库中断，正在有界退避重连；错误详情不含凭据'
                    self._state='auth_failed' if rejected else 'retrying'
                    if rejected:return
                    attempt+=1
                    await asyncio.sleep((5,10,30,60)[min(attempt-1,3)])
        finally:
            if self._lease:self._lease.release();self._lease=None
def quote_router(control):
    r=APIRouter(prefix='/api/im/quote')
    @r.get('')
    def status(request:Request):return {'ok':True,'data':control.status(request.state.user.user_id)}
    @r.put('')
    async def save(request:Request,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':await control.save(request.state.user.user_id,body)}
    @r.post('/test')
    async def test(request:Request,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':await control.test(request.state.user.user_id,body)}
    @r.post('/start')
    async def start(request:Request):return {'ok':True,'data':await control.start(request.state.user.user_id)}
    @r.post('/stop')
    async def stop(request:Request):return {'ok':True,'data':await control.stop(request.state.user.user_id)}
    return r