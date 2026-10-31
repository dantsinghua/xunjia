"""Protected per-stage Dify settings, inherited from existing deployment env."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import threading
from fastapi import APIRouter,Body,Request,HTTPException
from fastapi.responses import FileResponse
from .qa_workflow import default_config_path,_url
from .institution_settings import atomic_write
from .negotiation.chatflow import ChatflowClient,FlowUnavailable


class DifySettings:
    def __init__(self,path=None,environ=None):
        candidate=default_config_path()
        self.path=Path(path) if path is not None else candidate.with_name('dify-workflows.json') if candidate else None
        self.env=dict(os.environ if environ is None else environ)

    def load(self):
        value={}
        if self.path and self.path.exists():
            if self.path.resolve()!=self.path.absolute():
                raise ValueError('Dify配置文件权限或路径无效')
            with os.fdopen(os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>32768:
                    raise ValueError('Dify配置文件权限或路径无效')
                value=json.loads(stream.read(32769))
            if not isinstance(value,dict) or set(value)-{'analysis','reply'}:raise ValueError('Dify配置格式无效')
        result={}
        for stage,key in [('analysis','XUNJIA_DIFY_ANALYSIS_APP_KEY'),('reply','XUNJIA_DIFY_REPLY_APP_KEY')]:
            override=value.get(stage,{})
            if not isinstance(override,dict) or set(override)-{'base_url','app_key','revision'}:raise ValueError('Dify配置格式无效')
            item={'base_url':self.env.get('XUNJIA_DIFY_URL',''),'app_key':self.env.get(key,''),'revision':'',**override}
            if any(not isinstance(v,str) for v in item.values()) or len(item['app_key'])>512 or any(ord(c)<33 for c in item['app_key']):
                raise ValueError('Dify配置格式无效')
            if item['base_url']:item['base_url']=_url(item['base_url'],api=True)
            result[stage]=item
        return result

    def status(self,stage):
        if stage not in ('analysis','reply'):raise ValueError('未知Dify流程')
        value=self.load()[stage]
        return {'stage':stage,'available':self.path is not None,'base_url':value['base_url'],
                'app_key_set':bool(value['app_key']),'configured':bool(value['app_key'] and value['base_url']),
                'revision':value['revision'],'dsl_version':'0.6.0'}

    def save(self,stage,body):
        self.status(stage)
        if self.path is None:raise ValueError('尚未确定安装目录')
        if not isinstance(body,dict) or set(body)!={'base_url','app_key','expected_revision'}:raise ValueError('配置字段无效')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.parent.resolve()!=self.path.parent.absolute():raise ValueError('Dify配置目录无效')
        fd=os.open(self.path.parent/'.dify-workflows.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            values=self.load();old=values[stage]
            if body['expected_revision']!=old['revision']:raise ValueError('配置已经变化，请重新读取')
            key=body['app_key']
            if not isinstance(key,str) or len(key)>512 or any(ord(c)<33 for c in key):raise ValueError('应用Key格式无效')
            key=key or old['app_key']
            if not key:raise ValueError('请填写该流程的应用Key')
            values[stage]={'base_url':_url(body['base_url'],api=True),'app_key':key,'revision':secrets.token_hex(16)}
            atomic_write(self.path,(json.dumps(values,ensure_ascii=False)+'\n').encode())
        finally:os.close(fd)
        return self.status(stage)


class ConfiguredChatflows:
    def __init__(self,settings):
        self.settings=settings;self._lock=threading.RLock();self._clients={}
    @property
    def configured(self):
        try:return all(v['base_url'] and v['app_key'] for v in self.settings.load().values())
        except Exception:return False
    def client(self,stage):
        value=self.settings.load()[stage]
        key=(stage,value['base_url'],value['app_key'])
        with self._lock:
            if not value['base_url'] or not value['app_key']:raise FlowUnavailable('DIFY_NOT_CONFIGURED')
            if key not in self._clients:
                self._clients={k:v for k,v in self._clients.items() if k[0]!=stage}
                self._clients[key]=ChatflowClient(value['base_url'],value['app_key'],stage=stage,
                    timeout=float(self.settings.env.get('XUNJIA_DIFY_TIMEOUT_SECONDS','90')))
            return self._clients[key]
    def analyze(self,snapshot):return self.client('analysis').call(snapshot)
    def decide(self,context):return self.client('reply').call(context)


def dify_settings_router(settings,on_saved=lambda:None):
    router=APIRouter(prefix='/api/dify')
    @router.get('/{stage}/config')
    def status(request:Request,stage:str):return {'ok':True,'data':settings.status(stage)}
    @router.put('/{stage}/config')
    def save(request:Request,stage:str,body:dict=Body(default_factory=dict)):
        value=settings.save(stage,body)
        on_saved()
        return {'ok':True,'data':value}
    @router.get('/{stage}/dsl')
    def dsl(request:Request,stage:str):
        settings.status(stage)
        path=Path(__file__).resolve().parents[1]/'dify/site_workflows'/('analysis.yml' if stage=='analysis' else 'reply.yml')
        if not path.is_file() or path.is_symlink():raise HTTPException(404,'流程DSL尚未随版本安装')
        return FileResponse(path,media_type='application/yaml',filename='xunjia-'+stage+'-0.6.0.yml')
    return router
