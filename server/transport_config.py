"""Explicit per-account transport configuration; secrets are environment references."""
import base64
import json
import os
from pathlib import Path


def scoped_queues(connection,workbench_id,account_uid):
    """Keep the original route for its collector; explicitly scope every other pair."""
    from relay.protocol import token
    token(workbench_id);token(account_uid)
    suffix='' if (workbench_id,account_uid)==(connection.get('workbench_id'),connection.get('account_uid')) else '/'+workbench_id+'/'+account_uid
    return {key:connection[key].rstrip('/')+suffix for key in ('uplink_url','downlink_url')}


def profile_binding(profile):
    from relay.protocol import Binding
    try:
        seal=base64.b64decode(os.environ[profile['seal_key_env']],validate=True)
        sign=base64.b64decode(os.environ[profile['sign_key_env']],validate=True)
        return Binding(profile['owner_user_id'],profile['account_uid'],profile['workbench_id'],seal,sign)
    except (KeyError,ValueError):raise ValueError('通道身份或密钥配置无效') from None


def mail_adapter(binding,config,path):
    from .transports import MailTransport,MailConfig
    config=dict(config)
    for name in ('smtp_password','imap_password'):
        variable=config.pop(name+'_env')
        if not isinstance(variable,str) or not os.environ.get(variable):raise ValueError('缺少通道机密环境变量')
        config[name]=os.environ[variable]
    for name in ('outbound_to','inbound_from','inbound_to'):config[name]=tuple(config[name])
    return MailTransport(binding,MailConfig(**config),path)


def load_transports(repository,config_path,state_path):
    from .transports import MailTransport,MailConfig,TransportRouter
    path=Path(config_path)
    if path.stat().st_mode & 0o077:raise ValueError('通道配置文件须仅部署用户可读（0600）')
    profiles=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(profiles,list) or len(profiles)>5:raise ValueError('通道配置须为最多5个本人账号档案')
    state=Path(state_path);state.mkdir(parents=True,exist_ok=True);state.chmod(0o700)
    def secret(name):
        if not isinstance(name,str) or not name or name not in os.environ:raise ValueError('缺少通道机密环境变量')
        return os.environ[name]
    adapters={}
    gateway_queues=set()
    for profile in profiles:
        if not isinstance(profile,dict):raise ValueError('通道档案无效')
        allowed={'owner_user_id','account_uid','workbench_id','seal_key_env','sign_key_env','file_gateway','mail'}
        if set(profile)-allowed:raise ValueError('通道档案含不支持字段')
        binding=profile_binding(profile)
        current=repository.binding_for(binding.owner_user_id)
        if not current or any(current[k]!=getattr(binding,k) for k in ('account_uid','workbench_id')):raise ValueError('通道档案与业务库绑定不一致')
        shared_path=state/(binding.account_uid+'.db')
        if 'file_gateway' in profile:
            from .file_gateway_transport import FileGatewayTransport
            adapter=FileGatewayTransport.from_config(binding,profile['file_gateway'],shared_path,role='service',config_dir=path.parent)
            identities={adapter.inbound.queue_identity,adapter.outbound.queue_identity}
            if gateway_queues.intersection(identities):raise ValueError('FILE_GATEWAY_QUEUE_HAS_ANOTHER_BINDING')
            gateway_queues.update(identities)
            key=(binding.owner_user_id,binding.account_uid,'oss')
            if key in adapters:raise ValueError('FILE_GATEWAY_PROFILE_DUPLICATE')
            adapters[key]=adapter
        if profile.get('mail'):
            key=(binding.owner_user_id,binding.account_uid,'mail')
            if key in adapters:raise ValueError('通道档案重复')
            adapters[key]=mail_adapter(binding,profile['mail'],shared_path)
    return TransportRouter(adapters)
