<script setup lang="ts">
import {ref,watch} from 'vue'
import {App} from 'ant-design-vue'
import {api,type UnknownCommand} from '../api'
import {imBoundAccount} from '../lib/imLink'
const props=defineProps<{open:boolean}>()
const emit=defineEmits<{close:[];resolved:[]}>()
const {message}=App.useApp()
const items=ref<UnknownCommand[]>([])
const selected=ref<UnknownCommand|null>(null)
const result=ref('')
const evidence=ref('')
const confirmed=ref(false)
const busy=ref(false)
let generation=0
function choose(item:UnknownCommand){selected.value=item;result.value='';evidence.value='';confirmed.value=false}
async function reload(){
  const current=++generation;selected.value=null;items.value=[]
  try{const response=await api.unknownCommands();if(current!==generation)return;if(!response.ok)throw Error(response.hint||'读取失败');items.value=response.data}
  catch(error){if(current===generation)message.error(error instanceof Error?error.message:'读取失败')}
}
async function save(){
  if(!selected.value||!result.value||evidence.value.trim().length<8||!confirmed.value||busy.value)return
  const current=generation;busy.value=true
  try{
    const response=await api.reconcileCommand(selected.value.cmd_id,{status:result.value,evidence:evidence.value.trim(),expected_result_hash:selected.value.expected_result_hash,confirmed:confirmed.value})
    if(current!==generation)return
    if(!response.ok)throw Error(response.hint||'核对结果未保存')
    message.success('已记录人工核对结果；未重发消息，未恢复自动谈价')
    await reload();emit('resolved')
  }catch(error){if(current===generation)message.error(error instanceof Error?error.message:'保存失败')}
  finally{busy.value=false}
}
watch(()=>[props.open,imBoundAccount.value?.account_uid],()=>{generation++;items.value=[];selected.value=null;if(props.open)void reload()},{immediate:true})
</script>
<template>
  <a-modal :open="open" title="核对发送结果未知的指令" :footer="null" width="780px" @cancel="emit('close')">
    <p>先核对采集端原生消息或执行账本。此操作只登记核对结果，不重发，不成交，也不会自动交回机器人。</p>
    <p v-if="!items.length">当前没有待核对指令。</p>
    <div class="pending-list"><button v-for="item in items" :key="item.cmd_id" type="button" :class="{chosen:selected?.cmd_id===item.cmd_id}" @click="choose(item)"><b>{{item.peer_id}}</b> · {{item.conflict?'回执冲突':'结果未知'}}<br>{{item.text}}<small>{{item.cmd_id}}</small></button></div>
    <form v-if="selected" @submit.prevent="save">
      <label>实际核对结果<select v-model="result" required><option value="" disabled>请选择</option><option value="sent">已查到原生发送记录</option><option value="failed">已确认发送失败</option><option value="cancelled">已确认取消且未执行</option></select></label>
      <label>核对凭证引用<textarea v-model="evidence" required minlength="8" maxlength="1000" placeholder="填写原生消息ID、执行账本记录或核对凭证位置" /></label>
      <label class="checked"><input v-model="confirmed" type="checkbox">我已核对原始记录，确认以上结果</label>
      <button class="btn-primary" type="submit" :disabled="busy||!result||evidence.trim().length<8||!confirmed">保存人工核对结果</button>
    </form>
  </a-modal>
</template>
<style scoped>
.pending-list{max-height:240px;overflow:auto}.pending-list button{display:block;width:100%;text-align:left;padding:10px;margin-bottom:8px;border:1px solid #ddd;background:#faf8f5;border-radius:6px;cursor:pointer}.pending-list .chosen{border-color:#b01c24}small{display:block;color:#777}form{border-top:1px solid #eee;padding-top:14px}label{display:block;margin-bottom:12px}select,textarea{display:block;width:100%;margin-top:6px;padding:8px;border:1px solid #ddd;border-radius:5px}textarea{min-height:72px}.checked{display:flex;gap:8px}
</style>
