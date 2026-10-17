"""Retired pre-V2 application, retained only for historical regression tests. NOT a deployment entrypoint."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import Body, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from .auth import AuthConfig, AuthError, AuthService
from .business import InquiryService
from .repository import Repository
from .routes import business_router


def create_app(*, database_url: str | None = None, auth_config: AuthConfig | None = None,
               enable_background: bool | None = None, transport=None, chat_client=None, market=None,
               reference_provider=None, agent_service_token=None) -> FastAPI:
    url=database_url or os.environ.get('XUNJIA_DATABASE_URL','')
    if not url:raise ValueError('尚未配置XUNJIA_DATABASE_URL')
    repository=Repository(url)
    auth=AuthService(auth_config or AuthConfig.from_env())
    enabled=os.environ.get('XUNJIA_RUN_BACKGROUND','0')=='1' if enable_background is None else enable_background
    from .market import MarketService
    from .market_routes import market_router
    from .bond_tools import BondTools
    from .quote_runtime import QuoteHub,QBMarket
    from .runtime import Dispatcher
    from .chat_runtime import ChatRunner
    from .agent_routes import agent_router
    quote_source=None
    if market is None and os.environ.get('XUNJIA_QUOTE_SOURCE','').lower()=='qb':
        if not all(os.environ.get(k) for k in ('QB_MYSQL_HOST','QB_MYSQL_USER','QB_MYSQL_PASSWORD','QB_MYSQL_DB','XUNJIA_QUOTE_CACHE')):
            raise ValueError('实时行情须显式配置独立数据源及缓存，不能使用开发机默认值')
        quote_source=QBMarket(os.environ['XUNJIA_QUOTE_CACHE'])
        market=MarketService(repository,rows_provider=quote_source.rows,static_provider=quote_source.db.static_all,
                             detail_provider=quote_source.source.detail,brokers_provider=quote_source.source.broker_choices)
    if chat_client is None and os.environ.get('XUNJIA_DIFY_URL') and os.environ.get('XUNJIA_DIFY_APP_KEY'):
        from .dify_client import DifyChatClient
        chat_client=DifyChatClient(os.environ['XUNJIA_DIFY_URL'],os.environ['XUNJIA_DIFY_APP_KEY'])
    if transport is None and os.environ.get('XUNJIA_TRANSPORT_CONFIG'):
        from .transport_config import load_transports
        state_dir=os.environ.get('XUNJIA_STATE_DIR')
        if not state_dir:raise ValueError('须显式配置传输状态目录XUNJIA_STATE_DIR')
        transport=load_transports(repository,os.environ['XUNJIA_TRANSPORT_CONFIG'],state_dir)
    service=InquiryService(repository,clock=auth.config.clock,reference_provider=reference_provider)
    bond_tools=BondTools(provider=reference_provider)
    hub=QuoteHub()
    runner=ChatRunner(service,chat_client,bond_tools,clock=auth.config.clock)
    dispatcher=Dispatcher(service,transport,clock=auth.config.clock)
    from .inbox import Inbox
    inbox=Inbox(service,transport)
    state={'background_errors':{},'market_ready':quote_source is not None}

    async def repeated(name,operation,delay):
        while True:
            try:
                await asyncio.to_thread(operation)
                state['background_errors'].pop(name,None)
                service.diagnostics.record(name)
            except asyncio.CancelledError:raise
            except Exception as exc:
                state['background_errors'][name]='服务暂不可用，等待下一轮'
                service.diagnostics.record(name,code='BACKGROUND_TASK_FAILED',error=exc)
            await asyncio.sleep(delay)

    def chat_pending():
        if chat_client is None:return
        with repository.db.transaction(write=False) as tx:
            expired=tx.execute("SELECT turn_ref FROM xj_turn WHERE state='running' AND expires_at<=?",(service.clock(),)).fetchall()
            rows=tx.execute('''SELECT m.message_id FROM xj_message m
                JOIN xj_conversation c ON c.conversation_id=m.conversation_id
                JOIN xj_binding b ON b.owner_user_id=m.owner_user_id
                WHERE m.processing='queued' AND m.source_seq>c.resume_seq AND c.mode='AUTO' AND c.social_authorized=1 AND b.bot_enabled=1
                AND NOT EXISTS (SELECT 1 FROM xj_turn t WHERE t.conversation_id=c.conversation_id AND t.state='running')
                AND NOT EXISTS (SELECT 1 FROM xj_command x WHERE x.conversation_id=c.conversation_id AND x.status IN ('queued','pending','unknown'))
                AND NOT EXISTS (SELECT 1 FROM xj_message prior WHERE prior.conversation_id=c.conversation_id AND prior.processing='queued' AND prior.source_seq<m.source_seq AND prior.source_seq>c.resume_seq)
                ORDER BY m.occurred_at,m.message_id LIMIT 5''').fetchall()
        for row in expired:runner._fail(row['turn_ref'])
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5,thread_name_prefix='xunjia-chat') as pool:
            list(pool.map(lambda row:runner.process(row['message_id']),rows))

    async def poll_market():
        while True:
            try:
                for delta,groups in await asyncio.to_thread(quote_source.poll):hub.publish(delta,groups)
                state['background_errors'].pop('market',None)
                service.diagnostics.record('market')
            except asyncio.CancelledError:raise
            except Exception as exc:
                state['background_errors']['market']='行情更新失败，当前报价可能已过期'
                service.diagnostics.record('market',code='MARKET_POLL_FAILED',error=exc)
            await asyncio.sleep(.5)

    @asynccontextmanager
    async def lifespan(app):
        tasks=[]
        if enabled:
            if not auth.configured:raise ValueError('生产后台启动前须配置登录密钥')
            if transport is not None:
                tasks.append(asyncio.create_task(repeated('dispatch',dispatcher.run_once,.5)))
                tasks.append(asyncio.create_task(repeated('inbox',inbox.poll_once,1)))
            if chat_client is not None:tasks.append(asyncio.create_task(repeated('chat',chat_pending,.5)))
            if quote_source is not None:tasks.append(asyncio.create_task(poll_market()))
        try:yield
        finally:
            for task in tasks:task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError,Exception):await task

    app=FastAPI(title='现券询价工作台',lifespan=lifespan)
    app.state.repository=repository;app.state.auth=auth;app.state.inquiry=service
    app.state.transport=transport;app.state.chat_client=chat_client;app.state.enable_background=enabled
    app.state.market=market or MarketService(repository);app.state.bond_tools=bond_tools
    app.state.quote_hub=hub;app.state.chat_runner=runner;app.state.dispatcher=dispatcher
    app.state.runtime_status=state;app.state.inbox=inbox

    @app.exception_handler(PermissionError)
    async def forbidden(_request,exc):return JSONResponse({'ok':False,'hint':str(exc)},status_code=403)

    @app.exception_handler(ValueError)
    async def invalid(_request,exc):return JSONResponse({'ok':False,'hint':str(exc)},status_code=400)

    def secure(response,path):
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        if path.startswith(('/api/','/internal/')):response.headers['Cache-Control']='no-store'
        return response

    @app.middleware('http')
    async def authenticated(request:Request,call_next):
        path=request.url.path
        service_tool=path.startswith('/api/agent/tools/')
        if path.startswith('/api/') and path not in ('/api/auth/sso','/api/health','/api/health/live','/api/health/ready','/api/health/automation') and not service_tool:
            bearer=request.headers.get('Authorization','')
            token=bearer[7:] if bearer.startswith('Bearer ') else request.headers.get('X-Auth-Token','')
            user=auth.verify_token(token)
            if user is None:return secure(JSONResponse({'ok':False,'hint':'未登录或登录已过期'},status_code=401),path)
            request.state.user=user
        return secure(await call_next(request),path)

    from .legacy_health import health_report, release_identity
    identity=release_identity()
    app.state.release_identity=identity
    def readiness():
        return health_report(repository,auth,identity,enabled=enabled,transport=transport,chat=chat_client)
    @app.get('/api/health')
    def health():
        # Compatibility summary: HTTP 200 means the API answered, not that it is ready.
        return {'ok':True,'data':readiness()}
    @app.get('/api/health/live')
    def live():
        # Never opens the business database or contacts an external service.
        return {'ok':True,'data':{'service':'xunjia','alive':True,**identity}}
    @app.get('/api/health/ready')
    def ready():
        value=readiness()
        return JSONResponse({'ok':value['ready'],'data':value},status_code=200 if value['ready'] else 503)
    @app.get('/api/health/automation')
    def automation_readiness():
        value=readiness()
        return JSONResponse({'ok':False,'data':value},status_code=503)
    @app.get('/api/runtime/status')
    def runtime_status(request:Request):
        return {'ok':True,'data':{'background_enabled':enabled,'chat_configured':chat_client is not None,'transport_configured':transport is not None,'market_configured':app.state.market.rows_provider is not None,'components':service.diagnostics.snapshot(request.state.user.user_id)}}

    @app.post('/api/auth/sso')
    def sso(body:dict=Body(default_factory=dict)):
        try:user=auth.verify_sso(body)
        except AuthError as exc:return JSONResponse({'ok':False,'hint':str(exc)},status_code=401 if auth.configured else 503)
        return {'ok':True,'token':auth.issue_token(user),'user_id':user.user_id,'display_name':user.display_name}

    @app.get('/api/auth/me')
    def me(request:Request):
        user=request.state.user;binding=repository.binding_for(user.user_id)
        return {'ok':True,'data':{'user_id':user.user_id,'display_name':user.display_name,'account_uid':binding['account_uid'] if binding else None,'binding':binding}}

    from .qa_routes import qa_router
    app.include_router(qa_router(service,app.state.market,bond_tools))
    app.include_router(market_router(app.state.market,service,bond_tools))
    app.include_router(business_router(service))
    from .link_routes import link_router
    app.include_router(link_router(service,transport))
    app.include_router(agent_router(runner,service_token=agent_service_token))

    @app.websocket('/ws/quote')
    async def quotes(ws:WebSocket):
        await hub.websocket(ws,auth,tuple(x.strip() for x in os.environ.get('XUNJIA_ALLOWED_ORIGINS','').split(',') if x.strip()))
    web_dist=os.environ.get('XUNJIA_WEB_DIST')
    if web_dist:
        from pathlib import Path
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse
        built=Path(web_dist).resolve()
        if not (built/'index.html').is_file() or not (built/'assets').is_dir():raise ValueError('前端构建目录无效')
        app.mount('/assets',StaticFiles(directory=built/'assets'),name='assets')
        async def web_page():return FileResponse(built/'index.html')
        for path in ('/','/quote','/monitor','/chat','/link'):
            app.add_api_route(path,web_page,methods=['GET'],include_in_schema=False)
    return app
