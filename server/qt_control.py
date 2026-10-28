"""Shared, page-independent receive-only QTrade collector and authenticated controls."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from fastapi import APIRouter, Body, Request
from collector.qtrade_fix import Session, FixAuthenticationError, FixProtocolError
from . import qt_credentials as credentials
from .qt_quotes import QtQuoteBook, canonical
from .quote_control import _Lease


class QtControl:
    def __init__(self, path, state_dir, *, session_factory=Session, clock=time.time,
                 retry_delays=(5,10,30,60), require_existing=False):
        self.path=Path(path).absolute();self.state_dir=Path(state_dir).absolute()
        self.session_factory=session_factory;self.clock=clock;self.retry_delays=retry_delays
        self.require_existing=require_existing
        self._lock=asyncio.Lock();self._task=None;self._lease=None;self._book=None
        self._seen=set();self._legacy=None;self._cfg=None;self._revision='new'
        self.state='stopped';self.code='QT_STOPPED';self._connected=False
        self.stats={'quotes_received':0,'quotes_written':0,'duplicate_quotes':0,'rejected_quotes':0,
                    'reconnects':0,'frames_received':0,'heartbeats_sent':0,'resend_requests':0,
                    'sequence_gaps':0,'session_duplicates':0,'last_quote_at':None,'last_error_code':None}
        self._reload()

    def _reload(self):
        try:
            self._cfg,self._revision=credentials.load(self.path)
            if self.require_existing and self._revision=='new':
                self.state='configuration_error';self.code='QT_CONFIGURATION_INVALID'
        except (ValueError,OSError):
            self._cfg=None;self._revision='invalid'
            self.state='configuration_error';self.code='QT_CONFIGURATION_INVALID'
        return self._cfg

    @property
    def configured(self):
        cfg=self._cfg
        if cfg is None:return False
        if 'journal_path' in cfg:return bool(cfg.get('enabled'))
        try:credentials.validate(cfg,ready=True)
        except ValueError:return False
        return True

    def _new_book(self,cfg):
        if 'journal_path' in cfg:
            return QtQuoteBook(self.state_dir/'qt-dialogues.sqlite',source_id=cfg['source_id'],
                account_owners=cfg['account_owners'],timezone=cfg['timezone'],clock=self.clock,journal=cfg['journal_path'])
        namespace=hashlib.sha256(canonical([cfg[k] for k in
            ('source_id','host','port','username','sender_comp_id','target_comp_id')]).encode()).hexdigest()[:32]
        return QtQuoteBook(self.state_dir/'qt-fix'/('quotes-'+namespace+'.sqlite'),
            source_id='fix-'+namespace,account_owners=cfg['account_owners'],timezone=cfg['timezone'],clock=self.clock)

    def _book_for_config(self):
        if not self.configured:return None
        self._book=self._new_book(self._cfg)
        self._legacy=self._book if 'journal_path' in self._cfg else None
        return self._book

    def status(self, owner):
        cfg=self._cfg or dict(credentials.DEFAULTS,account_owners={})
        public=credentials.public(cfg,owner,self._revision)
        active=self._task is not None and not self._task.done()
        if self._legacy is not None:
            active=self._legacy._task is not None and not self._legacy._task.done()
        notes={'stopped':'QT供数已停止；历史报价保留',
               'connecting':'正在连接QT供数服务', 'authenticating':'正在进行FIX登录',
               'streaming':'FIX会话在线；收到与入库计数分别显示，供数不依赖页面',
               'recovering':'FIX序号缺口恢复中；旧报价暂不参与最优价',
               'retrying':'QT连接中断，正在有上限的退避重连；不影响Innodealing',
               'auth_failed':'QT认证被拒绝，请核对账号和密码后重新开启',
               'protocol_error':'QT接口合同校验失败；已停止，未跳过校验继续接收',
               'configuration_error':'QT配置不可用；公共行情继续运行',
               'journal':'正在沿用旧日志供数；停止后可在此显式配置FIX连接'}
        return {**public,'configured':self.configured,'collector_active':active,
                'collector_state':self.state,'state':self.state,'code':self.code,'note':notes.get(self.state,self.code),
                'fix_session_connected':self._connected,'scope':'own_received_dialogue_quotes',
                'stats':dict(self.stats)}

    def rows(self, owner):
        book=self._book
        if book is None:return []
        rows=book.rows(owner)
        if self._legacy is not None:
            if self._legacy._task and not self._legacy._task.done() and self._legacy.state=='journal_readable':return rows
        connected=self._connected and self.state=='streaming'
        seen=set(self._seen)
        for row in rows:
            fields=row['qt_fields'];key=(fields.get('OwnQtNO'),fields.get('NegotiationID'),fields.get('Operator'))
            if not connected or key not in seen:
                if row['qt_status']=='ACTIVE':row['qt_status']='FEED_UNCONFIRMED'
                row['qt_compare_reasons']={side:reason or 'FEED_UNCONFIRMED' for side,reason in row['qt_compare_reasons'].items()}
        return rows

    def _lease_path(self):
        credentials.safe_path(self.path)
        return credentials.safe_path(self.path.with_name(self.path.name+'.collector-lock'))

    def _revision_check(self, body):
        cfg,rev=credentials.load(self.path)
        if not isinstance(body,dict) or set(body)!={'expected_revision'} or body['expected_revision']!=rev:
            raise credentials.QtConfigurationError('QT_CONFIG_CHANGED_RELOAD')
        return cfg,rev

    async def save(self,owner,body):
        async with self._lock:
            if self.status(owner)['collector_active']:raise ValueError('QT_STOP_BEFORE_CONFIG_CHANGE')
            with credentials.config_lock(self.path):
                lease=_Lease(self._lease_path());lease.acquire()
                try:
                    old,rev=credentials.load(self.path)
                    values,legacy=credentials.merge(old,body,owner,rev)
                    candidate=self._new_book(values)  # Validate storage before changing shared configuration.
                    if legacy:
                        backup=self.path.with_name(self.path.name+'.journal-backup-'+rev[:12])
                        if not backup.exists():
                            with backup.open('xb') as stream:stream.write(self.path.read_bytes())
                            backup.chmod(0o600)
                    credentials.write(self.path,values)
                    self._reload();self._seen.clear();self._legacy=None;self._book=candidate
                    self.state='stopped';self.code='QT_STOPPED';self._connected=False
                finally:lease.release()
            return self.status(owner)

    async def start(self,owner,body=None,*,persist=True):
        async with self._lock:
            if persist:self._revision_check(body)
            if self.status(owner)['collector_active']:return self.status(owner)
            self._reload()
            if not self.configured:raise ValueError('QT_CONNECTION_AND_ACCOUNT_MAPPING_REQUIRED')
            if 'journal_path' in self._cfg:raise ValueError('QT_SAVE_FIX_CONNECTION_FIRST')
            expected_revision=self._revision
            lease=_Lease(self._lease_path());lease.acquire()
            try:
                with credentials.config_lock(self.path):
                    cfg,rev=credentials.load(self.path)
                    if rev!=expected_revision:raise credentials.QtConfigurationError('QT_CONFIG_CHANGED_RELOAD')
                    if persist:self._revision_check(body)
                    Session(cfg,receive=lambda _:None,update=lambda **_:None).tls_context()
                    candidate=self._new_book(cfg)
                    if persist:
                        credentials.write(self.path,{**cfg,'enabled':True});self._reload()
                    self._book=candidate;self._legacy=None;self._seen.clear()
                self._lease=lease;self.state='connecting';self.code='QT_FIX_CONNECTING'
                self._task=asyncio.create_task(self._supervise(dict(self._cfg)),name='qtrade-fix-collector')
            except BaseException:
                lease.release();self._lease=None;raise
            return self.status(owner)

    async def stop(self,owner,body=None,*,persist=True):
        async with self._lock:
            if persist:
                with credentials.config_lock(self.path):
                    cfg,_=self._revision_check(body)
                    # Prevent one process from pretending to stop another process's socket.
                    if self._lease is None:
                        lease=_Lease(self._lease_path());lease.acquire();lease.release()
                    credentials.write(self.path,{**cfg,'enabled':False});self._reload()
            self._connected=False
            if self._task:
                self._task.cancel()
                with suppress(asyncio.CancelledError):await self._task
                self._task=None
            if self._legacy:await self._legacy.shutdown()
            if self._lease:self._lease.release();self._lease=None
            self.state='stopped';self.code='QT_STOPPED'
            return self.status(owner)

    async def test(self,owner,body):
        async with self._lock:
            self._revision_check(body)
            if self.status(owner)['collector_active']:
                return {'ok':self._connected,'reused':True,'saved':False,'note':'复用当前QT连接状态，未建立第二个会话'}
            self._reload()
            if not self.configured or 'journal_path' in self._cfg:raise ValueError('QT_SAVE_FIX_CONNECTION_FIRST')
            lease=_Lease(self._lease_path());lease.acquire()
            try:
                session=self.session_factory(self._cfg,receive=lambda _:None,update=lambda **_:None)
                await asyncio.wait_for(session.run(probe=True),self._cfg['connect_timeout_seconds']*2+3)
                return {'ok':True,'saved':False,'reused':False,'note':'FIX登录与注销测试通过；未启动持续供数，未写入报价'}
            except FixAuthenticationError:return {'ok':False,'saved':False,'code':'QT_FIX_AUTH_REJECTED','note':'FIX认证被拒绝'}
            except (OSError,ValueError,asyncio.TimeoutError):return {'ok':False,'saved':False,'code':'QT_FIX_PROBE_FAILED','note':'FIX连接测试未通过，请检查地址、协议与证书'}
            finally:lease.release()

    async def startup(self):
        self._reload()
        if not self.configured:return
        try:
            if 'journal_path' in self._cfg:
                self._book_for_config();await self._legacy.startup();self.state='journal';return
            self._book_for_config()
            if self._cfg['enabled']:await self.start('system',persist=False)
        except (ValueError,OSError,sqlite3.Error):
            self.state='configuration_error';self.code='QT_RESUME_FAILED';self._connected=False

    async def shutdown(self):
        await self.stop('system',persist=False)

    def _update(self,**values):
        if 'state' in values:
            self.state=values['state'];self._connected=self.state in ('streaming','recovering')
        if 'code' in values:self.code=values['code']
        for key in self.stats:
            if key in values:self.stats[key]=values[key]

    async def _receive(self,fields):
        self.stats['quotes_received']+=1
        try:
            changed=await asyncio.to_thread(self._book.ingest_fix,fields)
            keys=await asyncio.to_thread(self._book.current_fix_keys,fields)
            self._seen.update(keys)
            self.stats['quotes_written' if changed else 'duplicate_quotes']+=1
            self.stats['last_quote_at']=self.clock()
        except ValueError as exc:
            self.stats['rejected_quotes']+=1
            # Only controlled validation codes, never message text, ids, passwords or vendor Text.
            code=str(exc)
            self.stats['last_error_code']=code if code.startswith('QT_') and code.replace('_','').isalnum() else 'QT_QUOTE_REJECTED'

    async def _supervise(self,cfg):
        attempts=0
        try:
            while True:
                self._connected=False;self._seen.clear()
                try:
                    session=self.session_factory(cfg,receive=self._receive,update=self._update)
                    await session.run()
                    raise ConnectionError('QT_FIX_CONNECTION_ENDED')
                except asyncio.CancelledError:raise
                except FixAuthenticationError:
                    self.state='auth_failed';self.code='QT_FIX_AUTH_REJECTED';return
                except FixProtocolError:
                    self.state='protocol_error';self.code='QT_FIX_PROTOCOL_REJECTED';return
                except (OSError,asyncio.TimeoutError,sqlite3.Error):
                    self.state='retrying';self.code='QT_FIX_CONNECTION_FAILED'
                    self._connected=False;self.stats['reconnects']+=1
                    await asyncio.sleep(self.retry_delays[min(attempts,len(self.retry_delays)-1)])
                    attempts+=1
                except Exception:
                    self.state='protocol_error';self.code='QT_FIX_PROCESSING_FAILED';return
        finally:
            self._connected=False
            if self._lease:self._lease.release();self._lease=None


def qt_router(control):
    router=APIRouter(prefix='/api/im/qt-quote')
    @router.get('')
    async def status(request:Request):return {'ok':True,'data':control.status(request.state.user.user_id)}
    @router.put('')
    async def save(request:Request,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':await control.save(request.state.user.user_id,body)}
    @router.post('/start')
    async def start(request:Request,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':await control.start(request.state.user.user_id,body)}
    @router.post('/stop')
    async def stop(request:Request,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':await control.stop(request.state.user.user_id,body)}
    @router.post('/test')
    async def test(request:Request,body:dict=Body(default_factory=dict)):
        return {'ok':True,'data':await control.test(request.state.user.user_id,body)}
    return router
