import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick } from 'vue'
import Monitor from '../../src/pages/Monitor.vue'
import Quote from '../../src/pages/Quote.vue'
import Rfq from '../../src/components/RfqAssignModal.vue'
import Link from '../../src/pages/Link.vue'
import { buildBonds } from './fixtures'
const response=(data:unknown)=>new Response(JSON.stringify(data))
const idle=()=>new Promise(r=>setTimeout(r,15))
function setup(component:any,props:any={}) { const scope=effectScope(); const vm:any=scope.run(()=>component.setup(props,{expose(){},emit(){}})); return {vm,scope} }
test('监控对象刷新不产生watch请求环',async()=>{
 let count=0
 globalThis.fetch=async(url)=> { count++; if(count>15)throw Error('request loop'); return response({ok:true,data:String(url).includes('counterparties')?[{id:'A',name:'A',bonds:[]}]:String(url).includes('summary')?{stats:[]}:{messages:[]}}) }
 const {vm,scope}=setup(Monitor); await vm.reloadDesk(); await idle(); scope.stop(); assert.ok(count<=8,`requests=${count}`)
})
test('delta显式null清价并更新盘面清算和时间',()=>{
 const {vm,scope}=setup(Quote); const row=buildBonds()[0]
 const out=vm.applyDelta(row,{u:row.code,f:10,t:1500,bb:null,bo:2,fm:'意向价',cl:'+1'}).row
 assert.equal(out.ob,'—');assert.equal(out.firmness,'意向价');assert.equal(out.clearing,'+1');assert.equal(out.quotedAt,1500);scope.stop()
})
test('询价空yield不得变成0写入',async()=>{
 let posted=0;globalThis.fetch=async()=>{posted++;return response({ok:true,data:{}})}
 const {vm,scope}=setup(Quote);vm.bonds.value=buildBonds().slice(0,1); vm.toggle(vm.bonds.value[0].code);vm.floorMode.value='yield';vm.yieldText.value='  ';await vm.applyBp();assert.equal(posted,0);scope.stop()
})
test('旧items正文不能绕过批次预览进入发送',async()=>{
 let posted=0;globalThis.fetch=async()=>{posted++;return response({ok:true,data:{batches:[]}})}
 const {vm,scope}=setup(Rfq,{open:false,bonds:[],floors:{'A.IB':'2'}})
 vm.drafts.value=[{code:'A.IB',text:'a'.repeat(1501),uins:['A'],fields:{}}]
 await vm.confirm();assert.equal(posted,0);scope.stop()
})
test('通道fill刷新保留测试诊断步骤',()=>{
 const {vm,scope}=setup(Link);vm.steps.value=[{name:'probe',ok:false,detail:'offline'}]
 vm.fill({id:'a',name:'a',mode:'oss',form:{},account_id:'',workbench_id:'',account_uid:''})
 assert.equal(vm.steps.value.length,1);scope.stop()
})
test('翻页替换50行而不丢第2001行，前后页保留选择',async()=>{
 const {vm,scope}=setup(Quote);const base=buildBonds()[0]
 const page=(offset:number)=>({ok:true,data:Array.from({length:50},(_,i)=>({...base,code:`${offset+i}.IB`,name:`券${offset+i}`})),total:2100,hasMore:true,nextBefore:offset+50,nextUni:0})
 vm.takePage(page(0),'replace');vm.toggle('0.IB')
 for(let n=50;n<=2000;n+=50)vm.takePage(page(n),'more')
 assert.equal(vm.bonds.value.length,50);assert.equal(vm.bonds.value[0].code,'2000.IB');assert.equal(vm.isPicked('0.IB'),true);scope.stop()
})
test('全选绑定完整filter，切换筛选立即清选择',async()=>{
 const {vm,scope}=setup(Quote);vm.keyword.value='合成';vm.appliedKeyword.value='合成';vm.toggleAll()
 assert.equal(vm.selection().filter.keyword,'合成')
 vm.pickChip('当前状态',2);assert.equal(vm.checkedCount.value,0);scope.stop()
})
test('反转首屏请求响应只保留最新筛选',async()=>{
 const pending:Array<(r:Response)=>void>=[]
 globalThis.fetch=async()=>new Promise(r=>pending.push(r))
 const {vm,scope}=setup(Quote);const base=buildBonds()[0]
 const first=vm.loadFirst();vm.appliedKeyword.value='新';const second=vm.reloadByFilter()
 pending[1](response({ok:true,data:[{...base,name:'新',code:'NEW.IB'}],total:1}));await second
 pending[0](response({ok:true,data:[{...base,name:'旧',code:'OLD.IB'}],total:1}));await first
 assert.equal(vm.bonds.value[0].code,'NEW.IB');scope.stop()
})
test('发送回执期间切对手不清新输入，不拼第二份聊天',async()=>{
 let resolveReceipt!:(r:Response)=>void
 globalThis.fetch=async(url)=>String(url).includes('/receipts/')?new Promise(r=>{resolveReceipt=r}):response({ok:true,data:{cmd_id:'cmd1',status:'submitted'}})
 const {vm,scope}=setup(Monitor);vm.cps.value=[{id:'A',name:'A',bonds:[]},{id:'B',name:'B',bonds:[]}];vm.selCpId.value='A';vm.chatInput.value='first'
 const sending=vm.sendChat();await idle();vm.selCpId.value='B';vm.chatInput.value='new draft';await nextTick()
 resolveReceipt(response({ok:true,data:{status:'delivered'}}));await sending
 assert.equal(vm.chatInput.value,'new draft');assert.equal(vm.chatMsgs.value.length,0);scope.stop()
})
test('单经纪商定时刷新使用本人选定broker并替换价格',async()=>{
 const {vm,scope}=setup(Quote);vm.chanTab.value=2;vm.brokerPick.value='9';const base=buildBonds()[0];let url=''
 globalThis.fetch=async(u)=>{url=String(u).startsWith('/api/bonds?')?String(u):url;return response(String(u).startsWith('/api/bonds?')?{ok:true,data:[{...base,ob:3.1}],total:1}:{ok:true,data:{items:[]}})}
 await vm.headSync();assert.equal(new URL(url,'http://local').searchParams.get('broker'),'9');assert.equal(vm.bonds.value[0].ob,'3.1000');scope.stop()
})
test('服务端删除询价和底价后本地显示清除旧快照',async()=>{
 const {vm,scope}=setup(Quote);vm.serverStatus['A.IB']='询价中';vm.floorKind['A.IB']='bp';vm.floorValue['A.IB']=2
 globalThis.fetch=async()=>response({ok:true,data:{items:[]}})
 await vm.refreshBusiness();assert.equal(vm.serverStatus['A.IB'],undefined);assert.equal(vm.floorValue['A.IB'],undefined);scope.stop()
})
test('人工发送未决回执在轮询送达后收敛，不生成本地聊天副本',async()=>{
 let delivered=false
 globalThis.fetch=async(url)=>response(String(url).includes('/receipts/')?{ok:true,data:{status:delivered?'delivered':'unknown',cmd_id:'pending-1'}}:String(url).includes('/monitor/send')?{ok:true,data:{cmd_id:'pending-1',status:'submitted'}}:{ok:true,data:{messages:[]}})
 const {vm,scope}=setup(Monitor);vm.cps.value=[{id:'A',name:'A',bonds:[]}];vm.selCpId.value='A';vm.chatInput.value='awaiting'
 await vm.sendChat();assert.equal(vm.chatInput.value,'awaiting');delivered=true
 await vm.pollReceipts();assert.equal(vm.sendPhase.value,'delivered');assert.equal(vm.chatInput.value,'');assert.equal(vm.chatMsgs.value.length,0);scope.stop()
})
test('生产sent回执使人工发送状态收敛',async()=>{
 globalThis.fetch=async(url)=>response(String(url).includes('/receipts/')?{ok:true,data:{status:'sent',cmd_id:'production-1'}}:String(url).includes('/monitor/send')?{ok:true,data:{cmd_id:'production-1',status:'pending'}}:{ok:true,data:{messages:[]}})
 const {vm,scope}=setup(Monitor);vm.cps.value=[{id:'A',name:'A',bonds:[]}];vm.selCpId.value='A';vm.chatInput.value='message';await vm.sendChat();assert.equal(vm.sendPhase.value,'delivered');scope.stop()
})
test('已有券收到静态变更即更新券种期限并移出旧筛选',()=>{
 const {vm,scope}=setup(Quote);const base={...buildBonds()[0],bondType:'中票',term:'2Y'}
 vm.activeChip['类型']=3;vm.bonds.value=[base];assert.equal(vm.rows.value.length,1)
 const out=vm.applyDelta(base,{u:base.bondUniCode||base.code,f:22,t:1000,s:{bond_uni_code:base.bondUniCode||base.code,windcode:base.code,sec_name:'更新券名',bond_type:'国债',residual_maturity:'7Y',val_yield:2.5,net_price:101,val_date:'2026-10-13'}}).row
 vm.bonds.value=[out];assert.equal(out.name,'更新券名');assert.equal(out.term,'7Y');assert.equal(out.val,'2.5/101');assert.equal(vm.rows.value.length,0);scope.stop()
})
