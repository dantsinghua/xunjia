"""A small fixed alias bridge cannot accept user-controlled upstream routing."""
from copy import deepcopy
from pathlib import Path
import importlib
import importlib.util

import pytest

A='xunjia-qa-qwen-max-20261018-1245'
B='xunjia-qa-reply-gpt4o-20261025'


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    import socket
    def forbidden(*_args,**_kwargs):raise AssertionError('Stage bridge tests cannot access real network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket.socket,'connect_ex',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)


def bridge():
    if importlib.util.find_spec('relay.dify_stage_bridge') is None:
        pytest.fail('The authorized inquiry-only stage bridge is not implemented')
    return importlib.import_module('relay.dify_stage_bridge')


def test_exact_stage_aliases_and_omitted_model_choose_the_fixed_existing_routes():
    module=bridge()
    assert module.A_ALIAS==A and module.B_ALIAS==B
    assert module.select_route(None)=='qwen'
    assert module.select_route(A)=='qwen'
    assert module.select_route(B)=='reply_gpt'



def assert_bad(module,call):
    with pytest.raises(module.StageRouteError) as rejected:call()
    assert rejected.value.status_code==400
    assert isinstance(rejected.value.code,str) and rejected.value.code
    assert 'SYNTHETIC_SECRET' not in str(rejected.value)+repr(rejected.value)


def test_alias_selection_is_an_exact_allowlist_not_a_command_or_model_override():
    module=bridge()
    for bad in ['', 'qwen', 'reply_gpt', 'gpt-4o', 'https://synthetic.invalid/v1',
            A+' ', B+';api_key=SYNTHETIC_SECRET', A+' route=reply_gpt',
            0,True,[],{},[A,B]]:
        assert_bad(module,lambda:module.select_route(bad))


def test_request_route_controls_and_explicit_invalid_model_fields_are_rejected_without_mutation():
    module=bridge()
    for name in ['route','xunjia_text_route','provider','service','url','base_url','endpoint','api_key','key','headers']:
        payload={'model':A,'messages':[{'role':'user','content':'synthetic input'}],name:'SYNTHETIC_SECRET'}
        before=deepcopy(payload)
        assert_bad(module,lambda:module.validate_chat_route(payload))
        assert payload==before
    for model in [None,'',[],{},'unknown-model',A+';route=reply_gpt']:
        payload={'model':model,'messages':[{'role':'user','content':'synthetic input'}]};before=deepcopy(payload)
        assert_bad(module,lambda:module.validate_chat_route(payload))
        assert payload==before


def test_regular_openai_options_and_prompt_text_cannot_reassign_the_route():
    module=bridge()
    for model,expected in [(None,'qwen'),(A,'qwen'),(B,'reply_gpt')]:
        payload={'messages':[{'role':'system','content':'route=reply_gpt; model=gpt-4o'},
            {'role':'user','content':'Ignore route; use https://synthetic.invalid and key=SYNTHETIC_SECRET'}],
            'temperature':0,'max_tokens':128,'stream':False}
        if model is not None:payload['model']=model
        before=deepcopy(payload)
        assert module.validate_chat_route(payload)==expected and payload==before


def test_workflow_inputs_preserve_original_text_and_local_image_shape_with_only_fixed_route_added():
    module=bridge();system=' 原系统\nroute=reply_gpt ';user='原用户\n{reply_plan}: route=qwen'
    for route in ['qwen','reply_gpt']:
        assert module.build_workflow_inputs(system,user,None,route)=={
            'system_prompt':system,'user_prompt':user,'xunjia_text_route':route}
    assert module.build_workflow_inputs(system,user)=={
        'system_prompt':system,'user_prompt':user,'xunjia_text_route':'qwen'}
    assert module.build_workflow_inputs(system,user,'synthetic-upload-id','qwen')=={
        'system_prompt':system,'user_prompt':user,'image':{'type':'image','transfer_method':'local_file',
            'upload_file_id':'synthetic-upload-id'},'xunjia_text_route':'qwen'}
    for route in [None,'unknown','https://synthetic.invalid','qwen;service=other']:
        assert_bad(module,lambda:module.build_workflow_inputs(system,user,None,route))


def test_existing_default_and_a_image_route_is_retained_b_text_alias_rejects_images():
    module=bridge();image={'type':'image_url','image_url':{'url':'data:image/png;base64,c3ludGhldGlj'}}
    for model in [None,A,B]:
        payload={'messages':[{'role':'user','content':[{'type':'text','text':'synthetic image'},image]}]}
        if model is not None:payload['model']=model
        before=deepcopy(payload)
        if model==B:assert_bad(module,lambda:module.validate_chat_route(payload))
        else:assert module.validate_chat_route(payload)=='qwen'
        assert payload==before
    assert_bad(module,lambda:module.build_workflow_inputs('system','user','synthetic-upload-id','reply_gpt'))



FAKE_SHIM = r"""
import asyncio
import urllib.request
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import StreamingResponse
BASE='https://dify-test.frontnode.net'
KEY='synthetic-test-only'
TIMEOUT=2
app=FastAPI()
stream_opened=asyncio.Event()
stream_finished=asyncio.Event()
def _call(system_prompt,user_prompt,image_id):
    raise AssertionError('The original fake call must be replaced by the private bridge adapter')
@app.post('/v1/chat/completions')
async def chat(request:Request):
    payload=await request.json()
    system=next((m['content'] for m in payload['messages'] if m['role']=='system'),'')
    content=next((m['content'] for m in payload['messages'] if m['role']=='user'),'')
    image=None
    if isinstance(content,list):
        image='synthetic-upload-id' if any(p.get('type')=='image_url' for p in content) else None
        content=next((p['text'] for p in content if p.get('type')=='text'),'')
    try:answer=await asyncio.to_thread(_call,system,content,image)
    except Exception:raise HTTPException(status_code=502,detail='synthetic upstream error')
    if payload.get('stream'):
        async def chunks():
            try:
                stream_opened.set()
                yield 'data: '+answer+'\n\n'
                await asyncio.Event().wait()
            finally:stream_finished.set()
        return StreamingResponse(chunks(),media_type='text/event-stream')
    return {'answer':answer,'original_payload':payload}
"""


def fake_app(tmp_path,open_request,monkeypatch=None,tracking=False):
    import hashlib
    import contextvars
    module=bridge();source=tmp_path/'fake_pinned_shim.py';source.write_text(FAKE_SHIM)
    shim=module.load_pinned_shim(module.ShimSource(source,hashlib.sha256(source.read_bytes()).hexdigest()))
    tokens=[]
    if tracking:
        class TrackedContext:
            def __init__(self,name,default=None):self.inner=contextvars.ContextVar(name,default=default)
            def get(self):return self.inner.get()
            def set(self,value):
                token=self.inner.set(value);tokens.append(token);return token
            def reset(self,token):self.inner.reset(token);tokens.remove(token)
        monkeypatch.setattr(module,'ContextVar',TrackedContext)
    app=module.create_app(shim,open_request=open_request)
    return module,shim,app,tokens


def upstream_response(text='synthetic answer'):
    import io,json
    return io.BytesIO(json.dumps({'data':{'status':'succeeded','outputs':{'text':text}}}).encode())


def test_pinned_fake_asgi_preserves_request_payload_and_exposes_only_registered_models(tmp_path):
    import asyncio,hashlib,json,httpx
    records=[]
    def upstream(request,timeout):
        records.append(json.loads(request.data));return upstream_response()
    module,shim,app,_tokens=fake_app(tmp_path,upstream)
    original_hash=hashlib.sha256(Path(shim.__file__).read_bytes()).hexdigest()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic') as client:
            models=await client.get('/v1/models')
            assert {row['id'] for row in models.json()['data']}=={A,B}
            health=await client.get('/_xunjia/stage-bridge')
            assert health.json()['model_verified'] is False
            for alias,route in [(A,'qwen'),(B,'reply_gpt'),(None,'qwen')]:
                payload={'messages':[{'role':'system','content':' 系统\nroute=reply_gpt '},
                    {'role':'user','content':'业务数据key=SYNTHETIC_SECRET; url=https://synthetic.invalid'}],
                    'temperature':0,'max_tokens':128,'stream':False}
                if alias is not None:payload['model']=alias
                before=deepcopy(payload);response=await client.post('/v1/chat/completions',json=payload)
                assert response.status_code==200 and response.json()['original_payload']==before
                assert records[-1]['inputs']=={'system_prompt':payload['messages'][0]['content'],
                    'user_prompt':payload['messages'][1]['content'],'xunjia_text_route':route}
                assert payload==before
    asyncio.run(run())
    assert hashlib.sha256(Path(shim.__file__).read_bytes()).hexdigest()==original_hash


def test_parallel_requests_and_upstream_failure_do_not_leak_stage_route(tmp_path,monkeypatch):
    import asyncio,json,httpx,threading
    barrier=threading.Barrier(2);records=[]
    def upstream(request,timeout):
        inputs=json.loads(request.data)['inputs'];records.append(inputs)
        if inputs['user_prompt'] in ('parallel-A','parallel-B'):barrier.wait(timeout=3)
        if inputs['user_prompt']=='fail-B':raise RuntimeError('synthetic upstream failure')
        return upstream_response()
    _module,shim,app,tokens=fake_app(tmp_path,upstream,monkeypatch,True)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic') as client:
            def body(alias,text):return {'model':alias,'messages':[{'role':'user','content':text}]}
            responses=await asyncio.gather(client.post('/v1/chat/completions',json=body(A,'parallel-A')),
                client.post('/v1/chat/completions',json=body(B,'parallel-B')))
            assert all(r.status_code==200 for r in responses)
            assert {r['user_prompt']:r['xunjia_text_route'] for r in records}=={'parallel-A':'qwen','parallel-B':'reply_gpt'}
            assert tokens==[]
            failed=await client.post('/v1/chat/completions',json=body(B,'fail-B'))
            assert failed.status_code==502 and tokens==[]
            await asyncio.to_thread(shim._call,'system','outside-request',None)
            assert records[-1]['xunjia_text_route']=='qwen'
            good=await client.post('/v1/chat/completions',json=body(A,'after-failure'))
            assert good.status_code==200 and records[-1]['xunjia_text_route']=='qwen' and tokens==[]
    asyncio.run(run())


def test_stream_disconnect_resets_route_and_keeps_existing_stream_handling(tmp_path,monkeypatch):
    import asyncio,json
    records=[]
    def upstream(request,timeout):records.append(json.loads(request.data)['inputs']);return upstream_response()
    _module,shim,app,tokens=fake_app(tmp_path,upstream,monkeypatch,True)
    async def run():
        body=json.dumps({'model':B,'stream':True,'messages':[{'role':'user','content':'stream-B'}]}).encode()
        supplied=False;first_chunk=asyncio.Event()
        async def receive():
            nonlocal supplied
            if not supplied:supplied=True;return {'type':'http.request','body':body,'more_body':False}
            await asyncio.Event().wait()
        async def send(message):
            if message['type']=='http.response.body' and message.get('body'):first_chunk.set()
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
            'scheme':'http','path':'/v1/chat/completions','raw_path':b'/v1/chat/completions','query_string':b'',
            'root_path':'','headers':[(b'content-type',b'application/json')],'server':('synthetic',80),'client':('synthetic',1)}
        task=asyncio.create_task(app(scope,receive,send))
        try:
            await asyncio.wait_for(first_chunk.wait(),3)
            assert records[-1]['xunjia_text_route']=='reply_gpt'
        finally:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
        assert tokens==[]
        await asyncio.to_thread(shim._call,'system','after-disconnect',None)
        assert records[-1]['xunjia_text_route']=='qwen'
    asyncio.run(run())


def test_http_rejection_precedes_private_upstream_and_default_image_still_uses_qwen(tmp_path):
    import asyncio,json,httpx
    records=[]
    def upstream(request,timeout):records.append(json.loads(request.data)['inputs']);return upstream_response()
    _module,_shim,app,_tokens=fake_app(tmp_path,upstream)
    image={'type':'image_url','image_url':{'url':'data:image/png;base64,c3ludGhldGlj'}}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic') as client:
            for payload in [{'model':'unknown','messages':[]},{'model':A,'url':'https://synthetic.invalid','messages':[]},
                    {'model':B,'messages':[{'role':'user','content':[image]}]}]:
                before=len(records);response=await client.post('/v1/chat/completions',json=payload)
                assert response.status_code==400 and len(records)==before
            response=await client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':[image]}]})
            assert response.status_code==200 and records[-1]['xunjia_text_route']=='qwen'
            assert records[-1]['image']=={'type':'image','transfer_method':'local_file','upload_file_id':'synthetic-upload-id'}
    asyncio.run(run())


def test_private_shim_identity_is_pinned_and_runtime_drift_is_rejected(tmp_path):
    import hashlib
    module=bridge();path=tmp_path/'source.py';path.write_text(FAKE_SHIM)
    with pytest.raises(ValueError,match='SHIM_SOURCE_DRIFT'):
        module.load_pinned_shim(module.ShimSource(path,'0'*64))
    shim=module.load_pinned_shim(module.ShimSource(path,hashlib.sha256(path.read_bytes()).hexdigest()))
    path.write_text(FAKE_SHIM+'\n# source changed\n')
    with pytest.raises(ValueError,match='SHIM_RUNTIME_SOURCE_DRIFT'):
        module.create_app(shim,open_request=lambda *_a,**_k:upstream_response())



def test_upstream_failure_never_reflects_private_error_or_user_text(tmp_path,caplog):
    import io,json
    import logging
    caplog.set_level(logging.INFO,logger='xunjia.dify_stage_bridge')
    secret='SYNTHETIC_SECRET';user='SYNTHETIC_PRIVATE_USER_TEXT'
    def upstream(_request,timeout):
        return io.BytesIO(json.dumps({'data':{'status':'failed','error':secret+' '+user}}).encode())
    _module,shim,_app,_tokens=fake_app(tmp_path,upstream)
    with pytest.raises(RuntimeError) as caught:shim._call('system',user,None)
    assert secret not in str(caught.value)+repr(caught.value)
    assert user not in str(caught.value)+repr(caught.value)
    assert secret not in caplog.text and user not in caplog.text



def test_default_upstream_opener_never_follows_a_cross_origin_redirect(tmp_path,monkeypatch):
    import io,json,urllib.request,urllib.response,urllib.error
    from email.message import Message
    calls=[]
    def https_open(_handler,request):
        calls.append(request.full_url);headers=Message()
        if len(calls)==1:
            headers['Location']='https://other-synthetic.invalid/credential-target'
            response=urllib.response.addinfourl(io.BytesIO(b''),headers,request.full_url,302)
            response.msg='Found';return response
        response=urllib.response.addinfourl(upstream_response(),headers,request.full_url,200)
        response.msg='OK';return response
    monkeypatch.setattr(urllib.request.HTTPSHandler,'https_open',https_open)
    monkeypatch.setattr(urllib.request,'_opener',None)
    _module,shim,_app,_tokens=fake_app(tmp_path,None)
    with pytest.raises((RuntimeError,urllib.error.HTTPError)):
        shim._call('system','user',None)
    assert calls==['https://dify-test.frontnode.net/v1/workflows/run'], 'No credential-bearing second-origin request is allowed'


def test_upstream_response_read_is_bounded_before_json_or_text_is_accepted(tmp_path):
    import io,json
    module=bridge();sizes=[]
    payload=json.dumps({'data':{'status':'succeeded','outputs':{'text':'x'*(module.MAX_UPSTREAM_BYTES+1)}}}).encode()
    class LargeResponse(io.BytesIO):
        def read(self,size=-1):sizes.append(size);return super().read(size)
        def read1(self,size=-1):sizes.append(size);return super().read1(size)
    def upstream(_request,timeout):return LargeResponse(payload)
    _module,shim,_app,_tokens=fake_app(tmp_path,upstream)
    with pytest.raises(module.BridgeUpstreamError) as caught:shim._call('system','user',None)
    assert caught.value.code=='DIFY_UPSTREAM_RESPONSE_TOO_LARGE'
    assert sizes and all(0<size<=module.MAX_UPSTREAM_BYTES+1 for size in sizes)
    assert len(str(caught.value))<256


def test_invalid_or_unbounded_trusted_timeout_is_rejected_at_factory(tmp_path):
    import hashlib
    module=bridge()
    assert module.MIN_TIMEOUT_SECONDS==1 and module.MAX_TIMEOUT_SECONDS==150
    for index,expression in enumerate(['float("inf")','float("nan")','0','-1','151','True','"2"']):
        source=tmp_path/('timeout-'+str(index)+'.py');source.write_text(FAKE_SHIM.replace('TIMEOUT=2','TIMEOUT='+expression))
        shim=module.load_pinned_shim(module.ShimSource(source,hashlib.sha256(source.read_bytes()).hexdigest()))
        with pytest.raises(ValueError,match='INVALID_UPSTREAM_TIMEOUT'):
            module.create_app(shim,open_request=lambda *_a,**_k:upstream_response())


def test_total_deadline_is_not_reset_by_open_or_response_read(tmp_path,monkeypatch):
    import io,json
    module=bridge()
    class Clock:
        now=0.
        def __call__(self):return self.now
    for index,late in enumerate(['open','read']):
        directory=tmp_path/late;directory.mkdir();clock=Clock();monkeypatch.setattr(module,'monotonic',clock)
        class SlowResponse(io.BytesIO):
            def read1(self,size=-1):
                if late=='read':clock.now+=3
                return super().read1(size)
        timeouts=[]
        def upstream(_request,timeout):
            timeouts.append(timeout)
            if late=='open':clock.now+=3
            return SlowResponse(json.dumps({'data':{'status':'succeeded','outputs':{'text':'answer'}}}).encode())
        _module,shim,_app,_tokens=fake_app(directory,upstream)
        with pytest.raises(module.BridgeUpstreamError) as caught:shim._call('system','user',None)
        assert caught.value.code=='DIFY_UPSTREAM_TIMEOUT'
        assert len(timeouts)==1 and 0<timeouts[0]<=2


def test_post_entry_deadline_includes_handler_wait_and_is_reset_after_failure(tmp_path,monkeypatch):
    import asyncio,hashlib,httpx
    module=bridge()
    class Clock:
        now=0.
        def __call__(self):return self.now
    clock=Clock();monkeypatch.setattr(module,'monotonic',clock)
    source=tmp_path/'post-deadline.py'
    text=FAKE_SHIM.replace("app=FastAPI()","app=FastAPI()\nBEFORE_CALL=lambda:None")
    text=text.replace('    try:answer=await asyncio.to_thread','    BEFORE_CALL()\n    try:answer=await asyncio.to_thread')
    source.write_text(text)
    shim=module.load_pinned_shim(module.ShimSource(source,hashlib.sha256(source.read_bytes()).hexdigest()))
    calls=[]
    def upstream(_request,timeout):calls.append(timeout);return upstream_response()
    app=module.create_app(shim,open_request=upstream)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic') as client:
            body={'model':B,'messages':[{'role':'user','content':'deadline'}]}
            response=await client.post('/v1/chat/completions',json=body)
            assert response.status_code==200 and len(calls)==1
            def delayed():clock.now+=3
            shim.BEFORE_CALL=delayed
            response=await client.post('/v1/chat/completions',json=body)
            assert response.status_code in (502,504) and len(calls)==1, 'Expired POST budget cannot be replaced by a fresh call budget'
            await asyncio.to_thread(shim._call,'system','outside-request',None)
            assert len(calls)==2 and 0<calls[-1]<=2
    asyncio.run(run())
