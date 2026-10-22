"""Admission for a NEW send, independent of historical result lookup."""
from __future__ import annotations

class ChannelUnavailable(ValueError):
    submitted=False
    def __init__(self, code, hint):
        self.code=code;self.hint=hint
        super().__init__(hint)

class ChannelAdmission:
    def __init__(self, service, institution, transport, enabled, runtime):
        self.service,self.institution,self.transport=service,institution,transport
        self.enabled,self.runtime=enabled,runtime
    def report(self, owner):
        if not self.enabled:return {'ready':False,'code':'DISPATCH_DISABLED','hint':'通道不可用：发送后台未启动，本次未提交'}
        workers=self.runtime.get('workers',{})
        if not all(workers.get(name,{}).get('running') for name in ('dispatch','inbox')):
            return {'ready':False,'code':'DISPATCH_NOT_RUNNING','hint':'通道不可用：收发后台未运行，本次未提交'}
        errors=self.runtime.get('background_errors',{})
        if any(name in errors for name in ('dispatch','inbox')):
            return {'ready':False,'code':'CHANNEL_WORKER_FAILED','hint':'通道不可用：收发后台异常，本次未提交'}
        binding=self.service.repo.binding_for(owner)
        if not binding:return {'ready':False,'code':'ACCOUNT_NOT_SELECTED','hint':'通道不可用：尚未选择企点号，本次未提交'}
        if self.institution is not None:
            inst=self.institution
            if not inst.configured or getattr(inst,'changing',False):
                return {'ready':False,'code':'CONNECTION_NOT_READY','hint':'通道不可用：连接配置尚未加载，本次未提交'}
            adapter=inst.current_adapter(owner)
            if adapter is None:return {'ready':False,'code':'BINDING_NOT_READY','hint':'通道不可用：请保存当前工作台与企点号选择，本次未提交'}
            channel=inst.availability(binding['workbench_id'],binding['account_uid'])
            if not channel['ready']:
                return {**channel,'hint':'通道不可用：'+channel['hint']+'；本次未提交'}
            if not adapter.context_ready:
                return {'ready':False,'code':'CONTEXT_NOT_READY','hint':'通道不可用：新账号会话尚未接通，本次未提交'}
        elif self.transport is None:
            return {'ready':False,'code':'TRANSPORT_NOT_CONFIGURED','hint':'通道不可用：尚未配置收发通道，本次未提交'}
        return {'ready':True,'code':'READY','hint':'收发后台已运行'}
    def __call__(self, owner):
        state=self.report(owner)
        if not state['ready']:raise ChannelUnavailable(state['code'],state['hint'])
        if self.institution is not None:
            adapter=self.institution.current_adapter(owner)
            try:adapter.outbound.pending()
            except Exception:raise ChannelUnavailable('GATEWAY_UNAVAILABLE','通道不可用：文件网关连接失败，本次未提交') from None
