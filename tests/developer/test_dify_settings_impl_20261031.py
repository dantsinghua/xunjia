import json
from pathlib import Path
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.dify_settings import DifySettings,ConfiguredChatflows,dify_settings_router
from dify.site_workflows.build import build


@pytest.fixture
def settings(tmp_path):
    return DifySettings(tmp_path/'config/dify-workflows.json',{
        'XUNJIA_DIFY_URL':'https://dify.example/v1',
        'XUNJIA_DIFY_ANALYSIS_APP_KEY':'analysis-secret',
        'XUNJIA_DIFY_REPLY_APP_KEY':'reply-secret'})


def test_save_preserves_blank_key_and_other_flow_and_never_returns_secret(settings):
    value=settings.save('analysis',{'base_url':'https://new.example/v1','app_key':'','expected_revision':''})
    assert value['configured'] and value['app_key_set']
    assert 'secret' not in json.dumps(value)
    assert settings.load()['analysis']['app_key']=='analysis-secret'
    assert settings.load()['reply']['app_key']=='reply-secret'
    assert settings.load()['reply']['base_url']=='https://dify.example/v1'
    assert settings.path.stat().st_mode&0o077==0
    with pytest.raises(ValueError,match='已经变化'):
        settings.save('analysis',{'base_url':'https://stale.example/v1','app_key':'','expected_revision':''})


def test_new_calls_reload_config_without_changing_existing_call(settings,monkeypatch):
    class Client:
        def __init__(self,url,key,**kwargs):self.value=(url,key)
        def call(self,context):return self.value
    monkeypatch.setattr('server.dify_settings.ChatflowClient',Client)
    flows=ConfiguredChatflows(settings);old=flows.client('analysis')
    settings.save('analysis',{'base_url':'https://new.example/v1','app_key':'new-secret','expected_revision':''})
    assert flows.analyze({})==('https://new.example/v1','new-secret')
    assert old.call({})==('https://dify.example/v1','analysis-secret')
    assert flows.decide({})==('https://dify.example/v1','reply-secret')


@pytest.mark.parametrize('stage',['analysis','reply'])
def test_download_is_current_060_contract_and_save_updates_readiness(settings,stage):
    app=FastAPI();events=[]
    app.include_router(dify_settings_router(settings,lambda:events.append('saved')))
    with TestClient(app) as client:
        response=client.get('/api/dify/'+stage+'/dsl')
        assert response.status_code==200
        doc=yaml.safe_load(response.content)
        assert doc==build(stage)
        assert doc['version']=='0.6.0' and doc['app']['mode']=='advanced-chat'
        assert not doc['workflow']['environment_variables']
        for node in doc['workflow']['graph']['nodes']:
            if node['data']['type']=='code':compile(node['data']['code'],'dsl','exec')
        saved=client.put('/api/dify/'+stage+'/config',json={
            'base_url':'https://new.example/v1','app_key':'','expected_revision':''})
        assert saved.status_code==200 and events==['saved']
        assert 'secret' not in saved.text


def test_symlink_parent_rejected(settings,tmp_path):
    real=tmp_path/'real';real.mkdir();(tmp_path/'alias').symlink_to(real,target_is_directory=True)
    settings.path=tmp_path/'alias/dify-workflows.json'
    with pytest.raises(ValueError,match='目录无效'):
        settings.save('analysis',{'base_url':'https://new.example/v1','app_key':'','expected_revision':''})
