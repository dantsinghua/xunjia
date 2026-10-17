"""Explicit per-account transport configuration; secrets are environment references."""
import base64
import json
import os
from pathlib import Path


def load_transports(repository,config_path,state_path):
    from relay.protocol import Binding
    from .transports import OSSTransport,MailTransport,MailConfig,TransportRouter
    from imlink.oss_http import OSSHTTP
    from imlink.sts_credentials import STSCredentials,DEFAULT_STS_URL,MQ_STS_URL
    path=Path(config_path)
    if path.stat().st_mode & 0o077:raise ValueError('通道配置文件须仅部署用户可读（0600）')
    profiles=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(profiles,list) or len(profiles)>5:raise ValueError('通道配置须为最多5个本人账号档案')
    state=Path(state_path);state.mkdir(parents=True,exist_ok=True);state.chmod(0o700)
    def secret(name):
        if not isinstance(name,str) or not name or name not in os.environ:raise ValueError('缺少通道机密环境变量')
        return os.environ[name]
    adapters={}
    for profile in profiles:
        if not isinstance(profile,dict):raise ValueError('通道档案无效')
        try:
            seal=base64.b64decode(secret(profile['seal_key_env']),validate=True)
            sign=base64.b64decode(secret(profile['sign_key_env']),validate=True)
            binding=Binding(profile['owner_user_id'],profile['account_uid'],profile['workbench_id'],seal,sign)
        except (KeyError,ValueError):raise ValueError('通道身份或密钥配置无效') from None
        current=repository.binding_for(binding.owner_user_id)
        if not current or any(current[k]!=getattr(binding,k) for k in ('account_uid','workbench_id')):raise ValueError('通道档案与业务库绑定不一致')
        shared_path=state/(binding.account_uid+'.db')
        if profile.get('oss'):
            config=profile['oss']
            if not isinstance(config,dict) or not config.get('expected_bucket'):
                raise ValueError('生产 OSS 档案必须设置 expected_bucket，防止 STS 跨环境写错桶')
            sts_url=config.get('sts_url',DEFAULT_STS_URL)
            if sts_url == MQ_STS_URL and os.environ.get('XUNJIA_ALLOW_INSECURE_MQ_HTTP') != '1':
                raise ValueError('正式服务默认禁止明文 MQ STS；仅受控开发环境可显式启用')
            provider=STSCredentials(auth_key=secret(config['auth_key_env']),sm4_key=secret(config['sm4_key_env']).encode(),url=sts_url)
            adapter=OSSTransport(binding,OSSHTTP(endpoint=config['endpoint'],bucket=config['expected_bucket'],credentials=provider),shared_path)
            key=(binding.owner_user_id,binding.account_uid,'oss')
            if key in adapters:raise ValueError('通道档案重复')
            adapters[key]=adapter
        if profile.get('mail'):
            config=dict(profile['mail'])
            config['smtp_password']=secret(config.pop('smtp_password_env'))
            config['imap_password']=secret(config.pop('imap_password_env'))
            for field in ('outbound_to','inbound_from','inbound_to'):config[field]=tuple(config[field])
            key=(binding.owner_user_id,binding.account_uid,'mail')
            if key in adapters:raise ValueError('通道档案重复')
            adapters[key]=MailTransport(binding,MailConfig(**config),shared_path)
    return TransportRouter(adapters)
