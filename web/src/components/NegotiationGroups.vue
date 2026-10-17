<script setup lang="ts">
import {ref,onMounted,onScopeDispose} from 'vue'
import {requestJson} from '../lib/http'
import {imBoundAccount,setImBinding} from '../lib/imLink'
import {newerProjection,type V22State,type V22Price} from '../lib/negotiation'
const emit=defineEmits<{(e:'changed'):void}>()
const state=ref<V22State>({engine:'v22',revision:0,send_allowed:false,groups:[],threads:[]})
const error=ref(''),busy=ref(false),bound=ref(false),account=ref(''),workbench=ref('')
let disposed=false,generation=0
defineExpose({refresh})
function price(value:V22Price|null){return value ? `${value.value}${value.kind==='YIELD'?'%':value.kind==='NET_PRICE'?' 净价':' 全价'}` : '未报价'}
async function refresh(){
 const ticket=++generation
 try{
  const result=await requestJson<{ok:boolean;data:V22State}>('/api/negotiation/state')
  if(disposed||ticket!==generation)return
  if(result.ok&&newerProjection(state.value.revision,result.data.revision)){state.value=result.data;error.value=''}
 }catch(e){if(!disposed)error.value=e instanceof Error?e.message:'报价状态读取失败，保留上次数据'}
}
async function changed(){await refresh();if(!disposed)emit('changed')}
async function bind(){
 busy.value=true
 try{
  const result=await requestJson<{ok:boolean;data:unknown;hint?:string}>('/api/im/binding',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_uid:account.value,workbench_id:workbench.value,transport:'oss'})})
  if(!result.ok)throw new Error(result.hint||'绑定失败')
  setImBinding(result.data,account.value)
  bound.value=true;await changed()
 }catch(e){error.value=e instanceof Error?e.message:'绑定失败'}finally{busy.value=false}
}
async function sending(id:string,group:boolean,allowed:boolean){
 busy.value=true
 try{
  const result=await requestJson<{ok:boolean;hint?:string}>(`/api/negotiation/${group?'groups':'members'}/${encodeURIComponent(id)}/sending`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({allowed})})
  if(!result.ok)throw new Error(result.hint||'发送状态未更新')
  await changed()
 }catch(e){error.value=e instanceof Error?e.message:'发送状态未更新'}finally{busy.value=false}
}
onMounted(async()=>{
 try{const me=await requestJson<{data:{binding:unknown}}>('/api/auth/me');bound.value=!!me.data.binding}catch{}
 if(disposed)return
 await refresh()
})
onScopeDispose(()=>{disposed=true;generation++})
</script>
<template>
 <section class="v22-groups" data-testid="v22-groups">
  <header><b>券组目标与对手独立价量</b><span>V2.2 · 修订 {{state.revision}} · 停止只关闭发送，消息与分析继续</span></header>
  <p v-if="error" role="alert">{{error}}</p>
  <form v-if="!bound && !imBoundAccount" @submit.prevent="bind" class="binding">
   <label>本人企点账号 <input v-model="account" required autocomplete="off" /></label>
   <label>采集工作台ID <input v-model="workbench" required autocomplete="off" /></label>
   <button :disabled="busy">绑定当前登录人</button><small>仅建立业务归属，不启动采集、不自动发送</small>
  </form>
  <p v-if="!state.groups.length">尚无券组。完成账号绑定后，从询价页预览并确认目标。</p>
  <details v-for="g in state.groups.filter(v=>!v.archived)" :key="g.group_id" :open="true" :data-group-id="g.group_id">
   <summary><b>{{g.plan.name||g.bond_code}} {{g.bond_code}}</b> · {{g.our_side==='BUY'?'我方收':'我方出'}} · {{g.trade_day}} · {{g.state==='HANDOFF'?'待人工确认':!state.send_allowed?'全局停止发送':g.send_allowed?'谈判中':'已停止发送'}}<strong v-if="g.review_needed"> · 原交付结果已变化，需复核</strong></summary>
   <div class="totals">
    <span>目标 {{g.target_volume}} 万</span><span>已报 {{g.summary.reported_volume_wan}} 万</span>
    <span>到价量 {{g.summary.price_eligible_volume_wan}} 万</span><span>最终确认 {{g.summary.final_confirmed_volume_wan}} 万</span>
    <span>内部边界 {{g.boundary?.value??'待最新估值'}} {{g.boundary?.kind==='YIELD'?'%':''}}</span>
    <button :disabled="busy || g.state==='HANDOFF'" @click="sending(g.group_id,true,!g.send_allowed)">{{g.send_allowed?'停止本券发送':'恢复本券发送'}}</button>
   </div>
   <div class="table-wrap"><table><thead><tr><th>对手</th><th>买价 / 收量</th><th>卖价 / 出量</th><th>谈判空间</th><th>分析与条件</th><th>发送控制</th></tr></thead>
    <tbody><tr v-for="p in g.peers" :key="p.member_id" :data-member-id="p.member_id">
      <td>{{p.peer_uid}}</td>
      <td>{{price(p.quotes.bid.price)}} / {{p.quotes.bid.volume_wan??'未报'}} 万<small>{{p.quotes.bid.validity}} · {{p.quotes.bid.confirmed_day??'当日待确认'}}</small></td>
      <td>{{price(p.quotes.ofr.price)}} / {{p.quotes.ofr.volume_wan??'未报'}} 万<small>{{p.quotes.ofr.validity}} · {{p.quotes.ofr.confirmed_day??'当日待确认'}}</small></td>
      <td>{{p.quotes[g.our_side==='BUY'?'ofr':'bid'].finality==='FINAL'?'上下文判断最终价':p.quotes[g.our_side==='BUY'?'ofr':'bid'].finality==='NEGOTIABLE'?'仍可争取':'尚未明确'}}</td>
      <td><span v-if="p.analysis_pending">新消息解析中，显示上一版事实</span><span v-else>分析已完成</span><small v-if="p.reconfirm_day">须向本人确认 {{p.reconfirm_day}} 报价</small><small>{{p.response_state}} {{p.command_state||''}}</small></td>
      <td><button :disabled="busy || g.state==='HANDOFF'" @click="sending(p.member_id,false,!p.send_allowed)">{{p.send_allowed?'暂停此对手本券':'恢复此对手本券'}}</button></td>
    </tr></tbody></table></div>
   <p v-if="g.summary.goal_ready">合格最终确认量达到目标，已交人工。展示各家整量，不自动拆量、锁券或成交。</p>
  </details>
 </section>
</template>
<style scoped>
.v22-groups{flex:0 1 auto;max-height:30vh;overflow:auto}
.v22-groups{border:1px solid var(--border,#ddd);padding:14px;margin:12px 0;border-radius:8px;background:var(--panel,#fff)}header,.totals{display:flex;flex-wrap:wrap;align-items:center;gap:16px}header span,small{font-size:12px;opacity:.8}details{margin-top:12px;border-top:1px solid var(--border,#ddd);padding-top:10px}summary{cursor:pointer}.totals{padding:10px 0}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px;border-bottom:1px solid var(--border,#ddd);text-align:left;vertical-align:top}.table-wrap{overflow:auto}small{display:block;margin-top:4px}.binding{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:10px 0}input{display:block;padding:6px}button{cursor:pointer;padding:5px 10px}button:disabled{cursor:not-allowed;opacity:.5}
</style>
