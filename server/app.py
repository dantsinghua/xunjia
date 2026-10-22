"""Production V2.2 entrypoint. All mutable business state is in xj2_* tables.
Old business runtime is never selected by a flag or used as a silent fallback.
"""
from __future__ import annotations
import asyncio
import json
import os
from contextlib import asynccontextmanager,suppress
from pathlib import Path
from fastapi import Body,FastAPI,Request,WebSocket
from fastapi.responses import JSONResponse
from .auth import AuthConfig,AuthError,AuthService
from .negotiation.application_store import ApplicationStore
from .negotiation.service import Inquiry
from .negotiation.engine import Engine
from .negotiation.runtime import Inbox,Dispatcher
from .negotiation.settings import Settings,Aggregation
def create_app(*,database_url=None,auth_config=None,enable_background=None,transport=None,
               chat_client=None,chatflows=None,market=None,reference_provider=None,calendar=None,
               agent_service_token=None,aggregation=None,stage_budget_seconds=None,quote_control=None,quote_source=None,submission_guard=None):
    retired=[name for name in ('XUNJIA_TRANSPORT_CONFIG','XUNJIA_MQ_CONNECTION_CONFIG')
             if os.environ.get(name,'').strip()]
    if retired:
        raise ValueError('旧询价传输入口已停用：'+', '.join(retired)+'；仅使用XUNJIA_INSTITUTION_CONFIG和XUNJIA_STATE_DIR，旧账本不自动迁移或删除')
    state_dir=os.environ.get('XUNJIA_STATE_DIR','').strip()
    institution_config=os.environ.get('XUNJIA_INSTITUTION_CONFIG','').strip()
    if not institution_config and state_dir and transport is None:
        institution_config=str(Path(state_dir)/'institution-connection.json')
    if institution_config:
        if not state_dir:raise ValueError('机构模式必须配置XUNJIA_STATE_DIR')
        if transport is not None:raise ValueError('机构模式不能同时注入其它传输实例')
    url=database_url or os.environ.get('XUNJIA_DATABASE_URL','')
    if not url:raise ValueError('尚未配置XUNJIA_DATABASE_URL')
    auth=AuthService(auth_config or AuthConfig.from_env())
    store=ApplicationStore(url,clock=auth.config.clock,settings=Settings.from_env(os.environ))
    if not store.db.mysql:store.initialize()  # Explicit test SQLite; never auto-DDL on OceanBase.
    enabled=os.environ.get('XUNJIA_RUN_BACKGROUND','0')=='1' if enable_background is None else enable_background
    if type(enabled) is not bool:raise ValueError('后台开关必须为布尔值')
    if aggregation is None and os.environ.get('XUNJIA_AGGREGATION_PROFILE'):
        value=json.loads(Path(os.environ['XUNJIA_AGGREGATION_PROFILE']).read_text(encoding='utf8'))
        aggregation=Aggregation(**value)
    from .bond_tools import BondTools
    from .market import MarketService
    from .quote_runtime import QuoteHub,QBMarket
    source=quote_source
    if reference_provider is None and os.environ.get('XUNJIA_REFERENCE_CONFIG'):
        from .negotiation.reference import HTTPReferenceProvider
        reference_provider=HTTPReferenceProvider.from_file(os.environ['XUNJIA_REFERENCE_CONFIG'])
    if calendar is None and reference_provider and callable(getattr(reference_provider,'trading_day',None)):
        calendar=reference_provider
    if market is None and source is None and os.environ.get('XUNJIA_QUOTE_SOURCE','').lower()=='qb':
        if not all(os.environ.get(k) for k in ('QB_MYSQL_HOST','QB_MYSQL_USER','QB_MYSQL_PASSWORD','QB_MYSQL_DB','XUNJIA_QUOTE_CACHE')):
            raise ValueError('行情数据源和本地缓存未完整配置')
        from .quote_control import database_options
        source=QBMarket(os.environ['XUNJIA_QUOTE_CACHE'],connection_options=database_options())
    if market is None and source is not None:
        market=MarketService(store,rows_provider=source.rows,static_provider=source.db.static_all,detail_provider=source.source.detail,brokers_provider=source.source.broker_choices)
    from .quote_control import QuoteControl,quote_router
    quote_control=quote_control or QuoteControl(reader=source)
    market=market or MarketService(store)
    market.repo=store
    client=chatflows or chat_client
    if client is not None and not all(callable(getattr(client,k,None)) for k in ('analyze','decide')):
        raise ValueError('V2.2 requires analysis/reply Chatflows, not the legacy AgentTurn client')
    if client is None and os.environ.get('XUNJIA_DIFY_URL'):
        a=os.environ.get('XUNJIA_DIFY_ANALYSIS_APP_KEY');b=os.environ.get('XUNJIA_DIFY_REPLY_APP_KEY')
        if bool(a)!=bool(b):raise ValueError('解析和回复两个已发布 Chatflow 的 App API Key 必须同时配置')
        if a and b:
            from .negotiation.chatflow import Chatflows,ChatflowClient
            timeout=float(os.environ.get('XUNJIA_DIFY_TIMEOUT_SECONDS','90'))
            client=Chatflows(ChatflowClient(os.environ['XUNJIA_DIFY_URL'],a,stage='analysis',timeout=timeout),
                            ChatflowClient(os.environ['XUNJIA_DIFY_URL'],b,stage='reply',timeout=timeout))
    institution=None
    if institution_config:
        from .transports import TransportRouter
        from .institution_runtime import InstitutionRuntime
        transport=TransportRouter({})
        institution=InstitutionRuntime(store,transport,institution_config,os.environ['XUNJIA_STATE_DIR'],clock=store.clock)
        institution.background_enabled=enabled
    service=Inquiry(store,reference_provider=reference_provider,calendar=calendar,
                    valuation_source=getattr(source,'valuation',None))
    from .session_reset import SessionReset
    session_reset=SessionReset(service,transport)
    service.session_reset=session_reset
    refresh_seconds=float(os.environ.get('XUNJIA_VALUATION_REFRESH_SECONDS','2'))
    if not 1<=refresh_seconds<=30:raise ValueError('估值恢复轮询间隔必须为1..30秒')
    tools=BondTools(provider=reference_provider)
    stage_budget=float(os.environ.get('XUNJIA_DIFY_STAGE_BUDGET_SECONDS','150')) if stage_budget_seconds is None else stage_budget_seconds
    engine=Engine(service,client,tools,aggregation=aggregation,stage_budget_seconds=stage_budget)
    inbox=Inbox(service,transport)
    dispatcher=Dispatcher(service,transport)
    hub=QuoteHub();runtime={'engine':'v22','workers':{},'background_errors':{},'analysis_enabled':client is not None and aggregation is not None,'send_enabled':bool(enabled and transport is not None)}
    from .channel_readiness import ChannelAdmission
    admission=ChannelAdmission(service,institution,transport,enabled,runtime)
    service.submit_guard=admission if submission_guard is None else submission_guard
    def channel_operation(operation):
        # Selection/reset and transport I/O cannot interleave in this process.
        if institution:
            with institution._lock:return operation()
        return operation()
    async def repeated(name,operation,delay):
        runtime['workers'][name]={'running':True}
        while True:
            try:
                await asyncio.to_thread(channel_operation,operation) if name in ('inbox','dispatch','session_reset') else await asyncio.to_thread(operation)
                runtime['background_errors'].pop(name,None)
                service.diagnostics.record(name)
            except asyncio.CancelledError:
                runtime['workers'][name]['running']=False
                raise
            except Exception as exc:
                runtime['background_errors'][name]=type(exc).__name__
                service.diagnostics.record(name,code='BACKGROUND_TASK_FAILED',error=exc)
            await asyncio.sleep(delay)
    def rollover_once():
        result=service.refresh_valuations()
        runtime['valuation_refresh']=result
        return result
    async def market_loop():
        while True:
            try:
                for delta,groups in await asyncio.to_thread(source.poll):hub.publish(delta,groups)
                quote_control.reader_result(ok=True,note="行情读取正常；空表表示尚无当日有效报价")
                if getattr(source,'valuation_changed',False):
                    runtime['valuation_refresh']=await asyncio.to_thread(service.refresh_valuations,refresh_reference=False)
            except asyncio.CancelledError:raise
            except Exception as exc:
                quote_control.reader_result(ok=False,note='行情读取失败，请检查数据库连接、表结构及字典')
                service.diagnostics.record('market',code='MARKET_POLL_FAILED',error=exc)
            await asyncio.sleep(.5)
    @asynccontextmanager
    async def lifespan(app):
        tasks=[]
        try:
            if enabled and not auth.configured:raise ValueError('后台处理启动前必须配置本系统登录密钥')
            if institution and enabled:await asyncio.to_thread(institution.start)
            if enabled:
                if transport:
                    tasks.append(asyncio.create_task(repeated('inbox',inbox.poll_once,.5)))
                # Expiry needs no transport; reconciliation continues while sending is paused.
                tasks.append(asyncio.create_task(repeated('dispatch',dispatcher.run_once,.5)))
                tasks.append(asyncio.create_task(repeated('session_reset',session_reset.run_once,.5)))
                if client and aggregation:tasks.append(asyncio.create_task(repeated('analysis',engine.run_once,.25)))
                tasks.append(asyncio.create_task(repeated('day_rollover',rollover_once,refresh_seconds)))
            if source:tasks.append(asyncio.create_task(market_loop()))
            await quote_control.startup()
            yield
        finally:
            try:
                await quote_control.shutdown()
            finally:
                for worker in runtime['workers'].values():worker['running']=False
                for task in tasks:task.cancel()
                try:
                    for task in tasks:
                        with suppress(asyncio.CancelledError,Exception):await task
                finally:
                    if institution:await asyncio.to_thread(institution.close)
    app=FastAPI(title='现券询价工作台 V2.2',lifespan=lifespan)
    app.state.repository=store;app.state.auth=auth;app.state.inquiry=service
    app.state.transport=transport;app.state.chat_client=client;app.state.enable_background=enabled
    app.state.institution_runtime=app.state.institution=institution
    app.state.market=market;app.state.bond_tools=tools;app.state.quote_hub=hub
    app.state.chat_runner=app.state.negotiation_engine=engine
    app.state.dispatcher=dispatcher;app.state.inbox=inbox;app.state.runtime_status=runtime
    app.state.channel_admission=admission
    app.state.session_reset=session_reset
    app.state.business_engine='v22';app.state.rollover_once=rollover_once
    app.state.quote_control=quote_control
    from .institution_settings import connection_settings_router
    from .institution_runtime import InstitutionSettings
    mq_settings=InstitutionSettings(institution)
    app.state.mq_settings=mq_settings
    app.include_router(connection_settings_router(mq_settings))
    collector_selection=None
    if institution:
        from .institution_selection import InstitutionSelection
        collector_selection=InstitutionSelection(store,transport,institution)
    app.state.collector_selection=collector_selection
    @app.exception_handler(PermissionError)
    async def forbidden(_req,exc):return JSONResponse({'ok':False,'hint':str(exc)},status_code=403)
    @app.exception_handler(ValueError)
    async def invalid(_req,exc):
        result={'ok':False,'hint':str(exc)}
        if hasattr(exc,'submitted'):result.update(submitted=exc.submitted,code=getattr(exc,'code','SUBMISSION_REJECTED'))
        return JSONResponse(result,status_code=503 if getattr(exc,'code','') in ('DISPATCH_DISABLED','DISPATCH_NOT_RUNNING','GATEWAY_UNAVAILABLE','COLLECTOR_HEARTBEAT_STALE','COLLECTOR_NOT_READY','COLLECTOR_NOT_DISCOVERED','COLLECTOR_IDENTITY_CONFLICT','CONTEXT_NOT_READY','CHANNEL_WORKER_FAILED') else 400)
    def headers(response,path):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
        if path.startswith('/api/') or path in ('/','/quote','/monitor','/chat','/link','/assets/build-version.json'):
            response.headers['Cache-Control']='no-store'
        return response
    @app.middleware('http')
    async def authenticated(request:Request,call_next):
        path=request.url.path
        public=path.startswith(('/api/agent/tools/','/api/tools/')) or path in ('/api/auth/sso-config','/api/auth/sso','/api/health','/api/health/live','/api/health/ready','/api/health/automation')
        if path.startswith('/api/') and not public:
            supplied=request.headers.get('Authorization','')
            token=supplied[7:] if supplied.startswith('Bearer ') else request.headers.get('X-Auth-Token','')
            user=auth.verify_token(token)
            if user is None:return headers(JSONResponse({'ok':False,'hint':'未登录或登录已过期'},status_code=401),path)
            request.state.user=user
        return headers(await call_next(request),path)
    from .health import health_report,release_identity
    identity=release_identity();app.state.release_identity=identity
    def report():
        result=health_report(store,auth,identity,enabled=enabled,transport=transport,chat=client)
        blockers=[]
        if client is None:blockers.append('DIFY_V22_CHATFLOWS_NOT_CONFIGURED')
        if aggregation is None:blockers.append('AGGREGATION_MEASUREMENT_REQUIRED')
        result.update(engine='v22',analysis_ready=not blockers,analysis_blockers=blockers,
                      context_messages=store.settings.context_messages,calendar_configured=calendar is not None,
                      live_transport_enabled=bool(enabled and transport is not None))
        return result
    @app.get('/api/health')
    def health():return {'ok':True,'data':report()}
    @app.get('/api/health/live')
    def live_health():return {'ok':True,'data':{'service':'xunjia','engine':'v22','alive':True,**identity}}
    @app.get('/api/health/ready')
    def ready():
        data=report();return JSONResponse({'ok':data['ready'],'data':data},status_code=200 if data['ready'] else 503)
    @app.get('/api/health/automation')
    def auto_ready():
        data=report()
        workers=runtime.get('workers',{})
        running=bool(enabled and all(workers.get(n,{}).get('running') for n in ('dispatch','inbox','analysis')))
        data.update(automation_ready=running,automation_state='workers_running' if running else 'workers_not_running',live_chain_verified=False)
        return JSONResponse({'ok':running,'data':data},status_code=200 if running else 503)
    @app.get('/api/runtime/status')
    def runtime_status(request:Request):
        return {'ok':True,'data':{**runtime,'readiness':report(),'sending':admission.report(request.state.user.user_id),'components':service.diagnostics.snapshot(request.state.user.user_id)}}
    @app.get("/api/auth/sso-config")
    def sso_config():
        dev = auth.config.dev_login is True and auth.configured
        return {"ok": True, "dev_login": dev, "password_login": False,
                "sso_configured": auth.configured, "ts_skew_seconds": auth.config.sso_skew_seconds,
                "dev_user_name": auth.config.dev_user_name if dev else "",
                "dev_nick_name": auth.config.dev_nick_name if dev else ""}
    @app.post('/api/auth/sso')
    def sso(body:dict=Body(default_factory=dict)):
        try:user=auth.verify_sso(body)
        except AuthError as exc:return JSONResponse({'ok':False,'hint':str(exc)},status_code=401 if auth.configured else 503)
        return {'ok':True,'token':auth.issue_token(user),'user_id':user.user_id,'display_name':user.display_name}
    @app.get('/api/auth/me')
    def me(request:Request):
        u=request.state.user;b=store.binding_for(u.user_id)
        return {'ok':True,'data':{'user_id':u.user_id,'display_name':u.display_name,'binding':b,'account_uid':b['account_uid'] if b else None,'engine':'v22'}}
    from .negotiation.http import router
    from .qa_routes import qa_router
    app.include_router(quote_router(quote_control))
    app.include_router(router(service,market,tools,transport,collector_selection=collector_selection))
    app.include_router(qa_router(service,market,tools))
    if institution:
        from .institution_http import institution_router
        app.include_router(institution_router(collector_selection))
    @app.post('/internal/agent/{old_endpoint}')
    def retired_contract(old_endpoint:str):
        return JSONResponse({'ok':False,'code':'V22_CHATFLOW_CONTRACT_REQUIRED','hint':'使用两阶段 V2.2 Chatflow；旧 turn_ref 回调合同不再执行任何业务写入'},status_code=410)
    @app.websocket('/ws/quote')
    async def quotes(ws:WebSocket):await hub.websocket(ws,auth,tuple(x.strip() for x in os.environ.get('XUNJIA_ALLOWED_ORIGINS','').split(',') if x.strip()))
    web_dist=os.environ.get('XUNJIA_WEB_DIST')
    if web_dist:
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse
        built=Path(web_dist).resolve()
        if not (built/'index.html').is_file() or not (built/'assets').is_dir():raise ValueError('前端构建目录无效')
        app.mount('/assets',StaticFiles(directory=built/'assets'),name='assets')
        # Public branding is shipped at the distribution root, not under assets.
        async def web_logo():
            logo=built/'logo.png'
            if not logo.is_file():return JSONResponse({'detail':'Not Found'},status_code=404)
            return FileResponse(logo,media_type='image/png')
        app.add_api_route('/logo.png',web_logo,methods=['GET','HEAD'],include_in_schema=False)
        async def web_page():return FileResponse(built/'index.html')
        for path in ('/','/quote','/monitor','/chat','/link'):app.add_api_route(path,web_page,methods=['GET'],include_in_schema=False)
    return app
