<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, type QtSourceState, type QtSourceInput } from '../api'
const state=ref<QtSourceState|null>(null)
const form=reactive({host:'',port:0,username:'',password:'',sender_comp_id:'',target_comp_id:'',
  transport:'tls' as 'tls'|'tcp',plaintext_confirmed:false,server_name:'',ca_file:'',client_cert_file:'',client_key_file:'',
  heartbeat_seconds:60,connect_timeout_seconds:10,encoding:'utf-8',sending_timezone:'Asia/Shanghai',timezone:'Asia/Shanghai',accounts:''})
const busy=ref(false),error=ref(''),notice=ref(''),dirty=ref(false),loadedRevision=ref('')
let timer:number|undefined,disposed=false,reading=false
const labels:Record<string,string>={stopped:'已停止',connecting:'正在连接',authenticating:'FIX登录中',
  streaming:'持续接收中',recovering:'序号恢复中',retrying:'断线重连中',auth_failed:'认证失效',
  protocol_error:'协议校验失败',configuration_error:'配置不可用',journal:'旧日志供数'}
const hints:Record<string,string>={QT_CONFIG_CHANGED_RELOAD:'共享配置已被其他用户修改，请重新读取后保存。',
  QT_ACCOUNT_ALREADY_BOUND:'该QT账号已绑定其他用户，未变更归属。',QT_STOP_BEFORE_CONFIG_CHANGE:'请先停止QT采集再修改配置。',
  QT_CONNECTION_AND_ACCOUNT_MAPPING_REQUIRED:'请完整配置FIX地址、账号、密码、双方CompID和本人QT账号ID。',
  QT_NEW_ACCOUNT_PASSWORD_REQUIRED:'更换FIX登录账号时必须同时提供新密码。',QT_PLAINTEXT_NOT_CONFIRMED:'使用明文TCP须先确认供应商要求和专线保护。'}
function failure(value?:string){return hints[value||'']||value||'QT操作未完成'}
function fill(data:QtSourceState){
  for(const k of ['host','port','username','sender_comp_id','target_comp_id','transport','plaintext_confirmed','server_name',
    'ca_file','client_cert_file','client_key_file','heartbeat_seconds','connect_timeout_seconds','encoding','sending_timezone','timezone'] as const){
    Object.assign(form,{[k]:data[k]})
  }
  form.password='';form.accounts=data.qt_account_ids.join('\n');loadedRevision.value=data.revision;dirty.value=false
}
async function refresh(reload=false){
  if(reading||busy.value)return
  reading=true
  try{
    const r=await api.qtSource()
    if(!r.ok||!r.data||typeof r.data.revision!=='string')throw Error(failure(r.hint))
    if(disposed)return
    state.value=r.data
    if(reload||!loadedRevision.value)fill(r.data)
    else if(!dirty.value&&loadedRevision.value!==r.data.revision)fill(r.data)
  }catch(e){if(!disposed)error.value=failure(e instanceof Error?e.message:'QT状态读取失败')}
  finally{reading=false}
}
async function act(kind:'save'|'start'|'stop'|'test'){
  if(busy.value||!state.value)return
  busy.value=true;error.value='';notice.value=''
  try{
    if(kind==='save'){
      const {accounts,...values}=form
      const body:QtSourceInput={...values,expected_revision:loadedRevision.value,
        qt_account_ids:accounts.split(/[\s,，]+/).filter(Boolean)}
      const r=await api.saveQtSource(body)
      if(!r.ok)throw Error(failure(r.hint))
      if(disposed)return
      state.value=r.data;fill(r.data)
      notice.value='QT共享连接配置已保存，尚未开启采集。新增QT账号仅绑定当前登录用户；原归属保持不变。'
    }else{
      if(kind!=='stop'&&dirty.value)throw Error('请先保存或重新读取配置；不会用未保存内容建立连接。')
      const revision=state.value.revision
      if(kind==='test'){
        const r=await api.testQtSource(revision)
        if(!r.ok)throw Error(failure(r.hint))
        if(disposed)return
        if(!r.data.ok)throw Error(r.data.note)
        notice.value=r.data.note
      }else{
        const r=await (kind==='start'?api.startQtSource(revision):api.stopQtSource(revision))
        if(!r.ok)throw Error(failure(r.hint))
        if(disposed)return
        state.value=r.data;if(!dirty.value)fill(r.data)
        notice.value=kind==='start'?'后台启动请求已接受；关页或退出登录不停止供数。':'已停止QT供数；Innodealing和历史报价不受影响。'
      }
    }
  }catch(e){if(!disposed)error.value=failure(e instanceof Error?e.message:'QT操作失败')}
  finally{busy.value=false}
}
onMounted(()=>{void refresh();timer=window.setInterval(()=>void refresh(),3000)})
onBeforeUnmount(()=>{disposed=true;if(timer)clearInterval(timer);form.password=''})
</script>
<template>
  <section class="qt-source" data-testid="qt-config" aria-labelledby="qt-title">
    <h3 id="qt-title">QTrade FIX 报价数据源</h3>
    <p>与Innodealing并列的机构级后台服务。连接配置和启停状态共用；QT明细仍按本方QT账号与登录用户的映射隔离，不进入公共广播。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="state">
      <div class="summary" data-testid="qt-state">
        <span>配置：{{ state.configured ? '已配置' : '待配置' }}</span>
        <span>供数：{{ labels[state.collector_state]||state.collector_state }}</span>
        <span>FIX会话：{{ state.fix_session_connected ? '已连接' : '未连接' }}</span>
      </div>
      <p>{{ state.note }}</p>
      <p v-if="dirty && loadedRevision!==state.revision" class="warning" data-testid="qt-config-conflict">共享配置已更新。你的未保存输入已保留，请重新读取后再提交。</p>
      <form @submit.prevent="act('save')" @input="dirty=true" @change="dirty=true" autocomplete="off">
        <fieldset :disabled="busy || state.collector_active">
          <label>供数服务器<input v-model="form.host" name="qt-host" autocomplete="off" placeholder="供应商提供的域名或IP" /></label>
          <label>端口<input v-model.number="form.port" name="qt-port" type="number" min="1" max="65535" /></label>
          <label>FIX登录账号ID（Username）<input v-model="form.username" name="qt-username" autocomplete="off" /></label>
          <label>FIX密码<input v-model="form.password" name="qt-password" type="password" autocomplete="new-password" :placeholder="state.password_set?'已保存，留空保留':'填写供应商密码'" /></label>
          <label>本方SenderCompID<input v-model="form.sender_comp_id" name="qt-sender" /></label>
          <label>对方TargetCompID<input v-model="form.target_comp_id" name="qt-target" /></label>
          <label>传输方式<select v-model="form.transport" name="qt-transport"><option value="tls">TLS（校验证书）</option><option value="tcp">TCP（供应商专线）</option></select></label>
          <label>中文编码<select v-model="form.encoding" name="qt-encoding"><option value="utf-8">UTF-8</option><option value="gb18030">GB18030</option><option value="ascii">ASCII</option></select></label>
          <label v-if="form.transport==='tcp'" class="wide check"><input v-model="form.plaintext_confirmed" type="checkbox" name="qt-plaintext" />已确认供应商要求明文TCP，并由受控专线保护登录密码</label>
          <template v-if="form.transport==='tls'">
            <label>TLS服务器名<input v-model="form.server_name" name="qt-server-name" placeholder="留空使用供数服务器名" /></label>
            <label>CA证书路径<input v-model="form.ca_file" name="qt-ca" placeholder="留空使用系统信任库" /></label>
            <label>客户端证书路径<input v-model="form.client_cert_file" name="qt-cert" placeholder="仅供应商要求mTLS时填写" /></label>
            <label>客户端私钥路径<input v-model="form.client_key_file" name="qt-key" placeholder="仅填服务器文件路径，不填私钥正文" /></label>
          </template>
          <label>心跳间隔（秒）<input v-model.number="form.heartbeat_seconds" name="qt-heartbeat" type="number" min="1" max="120" /></label>
          <label>连接超时（秒）<input v-model.number="form.connect_timeout_seconds" name="qt-timeout" type="number" min="1" max="30" /></label>
          <label>报文发送时区<select v-model="form.sending_timezone" name="qt-sending-zone"><option value="Asia/Shanghai">北京时间（手册4.3）</option><option value="UTC">UTC（经供应商确认）</option></select></label>
          <label>业务时间时区<select v-model="form.timezone" name="qt-business-zone"><option value="Asia/Shanghai">北京时间</option><option value="UTC">UTC（经供应商确认）</option></select></label>
          <label class="wide">本人QTrade账号ID（OwnQtNO，每行一个）<textarea v-model="form.accounts" name="qt-accounts" rows="3" /></label>
        </fieldset>
        <p class="hint">FIX登录账号不等于本方QT号，必须分别填写供应商分配的实际值。新增QT账号由服务端绑定当前登录用户；不能认领他人已绑定的账号，删除文本也不会转移或删除历史绑定。密码不回显、不存入浏览器。</p>
        <p class="hint">手册约定重连后下行序号重置，业务去重独立持久化。只收报价，向FIX仅发送登录、心跳、恢复和注销消息，不发送交易指令。编码、时区与传输方式需与供应商配置一致。</p>
        <div class="controls">
          <button type="submit" class="btn-primary" data-testid="qt-save" :disabled="busy||state.collector_active">保存共享配置</button>
          <button type="button" class="btn-ghost" data-testid="qt-test" :disabled="busy||!state.configured" @click="act('test')">测试FIX连接</button>
          <button type="button" class="btn-primary" data-testid="qt-start" :disabled="busy||!state.configured||state.collector_active" @click="act('start')">开启后台供数</button>
          <button type="button" class="btn-ghost" data-testid="qt-stop" :disabled="busy||!state.collector_active" @click="act('stop')">停止QT供数</button>
          <button type="button" class="btn-ghost" data-testid="qt-reload" :disabled="busy" @click="refresh(true)">重新读取共享配置</button>
        </div>
      </form>
      <div class="counts" data-testid="qt-counts">
        <span>收到报价 {{ state.stats.quotes_received }}</span><span>写入变更 {{ state.stats.quotes_written }}</span>
        <span>去重 {{ state.stats.duplicate_quotes }}</span><span>拒收 {{ state.stats.rejected_quotes }}</span>
        <span>重连 {{ state.stats.reconnects }}</span><span>本用户已映射 {{ state.qt_account_ids.length }} 个QT账号</span>
      </div>
      <p v-if="state.stats.last_error_code" class="warning">最近拒收原因：{{ state.stats.last_error_code }}。未归属账号不会展示给任意用户。</p>
    </template>
  </section>
</template>
<style scoped>
.qt-source{margin-top:24px;border-top:1px solid #e8e2dc;padding-top:18px}h3{margin:0 0 10px;color:#2b2320}p{color:#70665e;line-height:1.7}.summary,.counts,.controls{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}.summary span,.counts span{background:#f6f1ec;border-radius:6px;padding:6px 10px}fieldset{border:0;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:flex;flex-direction:column;gap:6px;color:#4c433d}.wide{grid-column:1/-1}input,select,textarea{box-sizing:border-box;min-width:0;max-width:100%;border:1px solid #d9cfc5;border-radius:6px;padding:8px;font:inherit}button{padding:8px 12px;font:inherit;border-radius:6px}button:disabled{opacity:.5;cursor:not-allowed}.hint{font-size:12px}.error{color:#a91f28}.warning{color:#8b5e0a}.check{flex-direction:row;align-items:center}.check input{width:auto}@media(max-width:700px){fieldset{grid-template-columns:1fr}}
</style>
