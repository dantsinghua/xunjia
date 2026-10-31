<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,reactive,ref} from 'vue'
import {api,type DifyFlowState} from '../api'
const props=defineProps<{stage:'analysis'|'reply'}>()
const title=computed(()=>props.stage==='analysis'?'事实解析工作流':'谈判回复工作流')
const state=ref<DifyFlowState|null>(null),busy=ref(false),error=ref(''),notice=ref('')
const form=reactive({base_url:'',app_key:''})
let disposed=false,controller:AbortController|undefined
async function act(save=false){
  if(busy.value||disposed||save&&!state.value?.available)return
  busy.value=true;error.value='';notice.value='';controller=new AbortController()
  const timer=setTimeout(()=>controller?.abort(),20000)
  try{
    const r=save?await api.saveDifyFlow(props.stage,{...form,expected_revision:state.value?.revision||''},controller.signal):await api.difyFlow(props.stage,controller.signal)
    if(disposed)return
    if(!r.ok||!r.data)throw Error(r.hint||'配置操作失败')
    state.value=r.data;form.base_url=r.data.base_url;form.app_key=''
    if(save)notice.value='已保存，后续流程调用使用新配置。正在执行的调用使用开始时的配置。'
  }catch(e){if(!disposed)error.value=e instanceof Error?e.message:'配置操作失败'}
  finally{clearTimeout(timer);busy.value=false;form.app_key=''}
}
async function download(){
  error.value=''
  try{await api.difyFlowDsl(props.stage)}catch(e){error.value=e instanceof Error?e.message:'下载失败'}
}
onMounted(()=>void act())
onBeforeUnmount(()=>{disposed=true;controller?.abort()})
</script>
<template>
  <section :data-testid="'dify-'+stage+'-settings'">
    <h3>{{title}}</h3>
    <p>{{stage==='analysis'?'解析采集消息中的债券、方向、报价与数量。':'根据已核实事实和询价策略生成谈判回复。'}}实际消息发送仍遵循现有询价授权与策略。</p>
    <p>下载 DSL 0.6.0，在对应 Dify 应用中导入、选择已配置模型并发布。原应用内导入时可继续使用原 Key。</p>
    <p v-if="state" role="status">{{state.configured?'已配置，密钥不回显。':'尚未完整配置。'}}已有环境配置会自动沿用。</p>
    <form @submit.prevent="act(true)">
      <label>Dify API 地址<input v-model="form.base_url" autocomplete="off" placeholder="https://dify.example/v1" :disabled="busy" /></label>
      <label>App API Key<input v-model="form.app_key" type="password" autocomplete="new-password" :placeholder="state?.app_key_set?'已保存，留空保留':'填写该应用的 API Key'" :disabled="busy" /></label>
      <div class="settings-actions">
        <button class="btn-primary" :disabled="busy||!state?.available" type="submit">保存配置</button>
        <button class="btn-ghost" :disabled="busy" type="button" @click="act()">重新读取</button>
        <button class="btn-ghost" type="button" @click="download">下载{{title}}</button>
      </div>
    </form>
    <p v-if="error" role="alert" class="error">{{error}}</p><p v-if="notice" role="status">{{notice}}</p>
  </section>
</template>
<style scoped>
p{font-size:13px;line-height:1.7;color:#70665e;overflow-wrap:anywhere}form{display:grid;gap:16px}label{display:grid;gap:7px;font-size:13px}input{padding:10px 12px;border:1px solid #d8cfc5;border-radius:7px;font:inherit}button{padding:9px 15px;border-radius:8px;font:inherit;cursor:pointer}button:disabled{opacity:.5}.error{color:#a02e2e}
</style>
