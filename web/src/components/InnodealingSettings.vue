<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
interface SourceState {
  account?: string; sid_set?: boolean; cookie_set?: boolean; device_id_set?: boolean
  configured?: boolean; can_manage?: boolean; reader_configured?: boolean
  collector_state?: string; collector_active?: boolean; note?: string; code?: string
  stats?: { quotes_received?:number; quotes_written?:number; trades_written?:number; decode_errors?:number; last_quote_at?:number|null }
  database?: {ok:boolean;note:string}|null
  reader?: {ok:boolean;note:string;last_poll_at:number|null}
}
interface Probe {ok:boolean;wss_open:boolean;note:string;database?:{ok:boolean;note:string};saved?:boolean}
const state=ref<SourceState|null>(null)
const form=reactive({account:'',sid:'',cookie:'',device_id:''})
const busy=ref(false),loading=ref(false),error=ref(''),notice=ref('')
const tested=ref<Probe|null>(null)
let timer:number|undefined,disposed=false,loaded=false
const labels:Record<string,string>={stopped:'已停止',starting:'正在启动',connecting:'正在连接',streaming:'已连接',retrying:'断线重试中',auth_failed:'认证失效',error:'启动失败'}
function clearSecrets(){form.sid='';form.cookie='';form.device_id=''}
function when(value?:number|null){return value?new Date(value*1000).toLocaleString():'尚未收到'}
async function refresh(quiet=false){
  if(loading.value || busy.value)return
  loading.value=true
  try{
    const response=await api.quoteSource() as unknown as {ok:boolean;data:SourceState;hint?:string}
    if(!response.ok || !response.data || Array.isArray(response.data))throw Error(response.hint||'行情配置读取失败')
    if(disposed)return
    state.value=response.data
    if(!loaded){form.account=response.data.account||'';loaded=true}
    if(!quiet)error.value=''
  }catch(e){if(!disposed)error.value=e instanceof Error?e.message:'行情配置读取失败'}
  finally{loading.value=false}
}
async function act(kind:'save'|'test'|'start'|'stop'){
  if(busy.value || !state.value?.can_manage)return
  busy.value=true;error.value='';notice.value=''
  try{
    const body={...form}
    if(kind==='test'){
      const response=await api.testQuoteSource(body) as {ok:boolean;data?:Probe;hint?:string}
      if(!response.ok || !response.data)throw Error(response.hint||'连接测试没有返回结果')
      tested.value=response.data
      notice.value='测试不会保存新凭据，也不会启动常驻采集。'
    }else{
      const response=await (kind==='save'?api.saveQuoteSource(body):kind==='start'?api.startQuoteSource():api.stopQuoteSource()) as {ok:boolean;data?:SourceState;hint?:string}
      if(!response.ok || !response.data)throw Error(response.hint||'行情操作未完成')
      state.value=response.data
      if(kind==='save'){clearSecrets();tested.value=null;notice.value='行情配置已保存。请测试连接后启动采集；保存本身不会启动。'}
      else notice.value=kind==='start'?'启动请求已接受，请查看连接状态及真实入库计数。':'采集已停止，历史报价和账号数据保持不变。'
    }
  }catch(e){error.value=e instanceof Error?e.message:'行情操作失败'}
  finally{busy.value=false}
}
onMounted(()=>{void refresh();timer=window.setInterval(()=>void refresh(true),3000)})
onBeforeUnmount(()=>{disposed=true;if(timer)clearInterval(timer);clearSecrets()})
</script>
<template>
  <section class="quote-source" aria-labelledby="inno-title" data-testid="inno-config">
    <h3 id="inno-title">Innodealing 实时报价数据源</h3>
    <p>沿用原行情账号、SID、Cookie 和设备标识。此处管理公共行情，与企点／QQ／微信收发通道、工作台登录密钥无关。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="state">
      <p v-if="!state.can_manage" class="warning">当前用户只能查看行情状态。配置与启停权限由 XUNJIA_QUOTE_MANAGERS 指定。</p>
      <div class="summary" data-testid="inno-state">
        <span>凭据：{{ state.configured ? '已保存' : '未完整配置' }}</span>
        <span>采集：{{ labels[state.collector_state||''] || '尚未启动' }}</span>
        <span>行情读取：{{ !state.reader_configured ? '未配置' : state.reader?.ok ? '正常' : '待就绪' }}</span>
      </div>
      <p>{{ state.note }}</p>
      <p v-if="!state.reader_configured" class="warning">行情读取尚未接入。服务端须配置 XUNJIA_QUOTE_SOURCE=qb、QB_MYSQL_* 和 XUNJIA_QUOTE_CACHE；连接测试通过不等于行情已进入工作台。</p>
      <form @submit.prevent="act('save')" autocomplete="off">
        <fieldset :disabled="busy || !state.can_manage || !!state.collector_active">
          <label>Innodealing 账号（配置标记）<input v-model="form.account" name="inno-account" maxlength="256" autocomplete="off" /></label>
          <label>SID<input v-model="form.sid" name="inno-sid" type="password" autocomplete="new-password" :placeholder="state.sid_set ? '已保存，留空不修改' : '可选；Cookie已包含sid时无需重复填写'" /></label>
          <label class="wide">Cookie<input v-model="form.cookie" name="inno-cookie" type="password" autocomplete="new-password" :placeholder="state.cookie_set ? '已保存，留空不修改' : '填写Innodealing已登录会话的Cookie'" /></label>
          <label class="wide">设备标识 device_id<input v-model="form.device_id" name="inno-device" type="password" autocomplete="new-password" :placeholder="state.device_id_set ? '已保存，留空不修改' : '填写该行情登录会话对应的设备标识'" /></label>
        </fieldset>
        <p class="hint">凭据只写入服务端受保护配置，不回显、不存入浏览器。运行中修改凭据须先停止采集；测试新凭据不会覆盖已保存配置。</p>
        <div class="controls">
          <button class="btn-primary" type="submit" :disabled="busy || !state.can_manage || !!state.collector_active" data-testid="inno-save">保存配置</button>
          <button class="btn-ghost" type="button" :disabled="busy || !state.can_manage" @click="act('test')" data-testid="inno-test">测试连接</button>
          <button class="btn-primary" type="button" :disabled="busy || !state.can_manage || !!state.collector_active || !state.configured || !state.reader_configured" @click="act('start')" data-testid="inno-start">启动采集</button>
          <button class="btn-ghost" type="button" :disabled="busy || !state.can_manage || !state.collector_active" @click="act('stop')" data-testid="inno-stop">停止采集</button>
          <button class="btn-ghost" type="button" :disabled="busy || loading" @click="refresh()">刷新行情状态</button>
        </div>
      </form>
      <div v-if="tested" class="probe" :class="{error:!tested.ok}" data-testid="inno-test-result" role="status">
        <b>{{ tested.ok ? '连接测试通过' : '连接测试未通过' }}</b>：{{ tested.note }}
        <p v-if="tested.database">数据库：{{ tested.database.note }}</p>
      </div>
      <div class="counts" v-if="state.stats">
        <span>本连接收到报价：{{ state.stats.quotes_received ?? 0 }}</span>
        <span>实际入库报价：{{ state.stats.quotes_written ?? 0 }}</span>
        <span>实际入库成交：{{ state.stats.trades_written ?? 0 }}</span>
        <span>最近报价：{{ when(state.stats.last_quote_at) }}</span>
      </div>
      <p v-if="state.database && !state.database.ok" class="error">{{ state.database.note }}</p>
      <p v-if="state.reader_configured && state.reader">{{ state.reader.note }}</p>
    </template>
    <button v-else class="btn-ghost" type="button" :disabled="loading" @click="refresh()">重新读取行情配置</button>
  </section>
</template>
<style scoped>
.quote-source{border-top:1px solid #e8e2dc;margin-top:24px;padding-top:16px}
h3{margin:8px 0 14px}p{line-height:1.7;color:#70665e}.summary,.counts{display:flex;flex-wrap:wrap;gap:14px;padding:12px;background:#faf8f5;border-radius:8px}
fieldset{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:0;border:0;margin:18px 0}
label{display:flex;flex-direction:column;gap:8px;font-weight:600}.wide{grid-column:1/-1}
input{box-sizing:border-box;width:100%;min-height:42px;border:1px solid #d9d0c8;border-radius:7px;padding:10px 12px;background:white;font:inherit;font-weight:400}input:focus{outline:2px solid #b01c24;outline-offset:1px}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}button{min-height:36px;padding:7px 14px;border-radius:8px;font:inherit;line-height:1.5;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}
.error{color:#a02e2e}.warning{color:#885600}.hint{font-size:13px}.probe{padding:12px;border:1px solid #ded7cf;border-radius:8px;margin:12px 0}.counts{font-size:13px}
@media(max-width:720px){fieldset{grid-template-columns:1fr}}
fieldset>label{min-width:0}p,.summary span,.counts span{overflow-wrap:anywhere}
</style>