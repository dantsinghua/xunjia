import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, reactive, nextTick } from 'vue'
import Rfq from '../../src/components/RfqAssignModal.vue'
import { buildBonds } from './fixtures'
import { imLinks, imContacts } from '../../src/lib/imLink'
Object.defineProperty(globalThis,'window',{value:{innerWidth:1440},configurable:true})
Object.defineProperty(globalThis,'document',{value:{documentElement:{clientWidth:1440},body:{style:{setProperty(){},removeProperty(){}},classList:{add(){},remove(){}}}},configurable:true})
const response=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status})
const preview={batch_id:'batch-test',version:3,status:'DRAFT',items:[],commands_preview:[{peer_id:'peer1',text:'服务端最终首句'}]}
function setup(){
 imLinks.value=[{id:'link1',active:true,tested:true,account_uid:'owner-account',workbench_id:'wb1'}] as any
 imContacts.value=[{uin:'peer1',name:'合成对手',remark:''}]
 const scope=effectScope(),events:any[]=[]
 const props=reactive({open:true,bonds:buildBonds().slice(0,1),floors:{},savedFloors:{}})
 const vm:any=scope.run(()=>(Rfq as any).setup(props,{expose(){},emit:(...args:any[])=>events.push(args)}))
 vm.drafts.value=[{...vm.seed(props.bonds[0]),uins:['peer1'],our_side:'BUY',volume_wan:'1000',opening_yield_pct:'2.1',limit_yield_pct:'2',step_bp:'2'}]
 return {vm,scope,events}
}
test('源ofr不替本人选择BUY或数量',()=>{const {vm,scope}=setup();const d=vm.seed(buildBonds()[0]);assert.equal(d.our_side,'');assert.equal(d.volume_wan,'');scope.stop()})
test('没有服务端预览时确认入口零发送',async()=>{
 const {vm,scope}=setup();let calls=0;globalThis.fetch=async()=>{calls++;return response({ok:true,data:{batches:[]}})}
 await vm.confirm();assert.equal(calls,0);scope.stop()
})
test('先预览后确认只提交批次版本，重试沿用request_id，queued不报已发',async()=>{
 const {vm,scope,events}=setup();const calls:any[]=[]
 globalThis.fetch=async(url,init)=>{calls.push({url:String(url),body:JSON.parse(String(init?.body))});return response({ok:true,data:String(url).endsWith('/preview')?preview:{batch_id:'batch-test',commands:[{command_id:'cmd1',cmd_id:'cmd1',peer_id:'peer1',status:'queued',text:'服务端最终首句'}]}})}
 const original=vm.drafts.value[0].text
 await vm.requestPreview();assert.equal(calls[0].url,'/api/im/rfq/preview');assert.equal(calls[0].body.items[0].our_side,'BUY');assert.equal(calls[0].body.items[0].text,undefined);assert.equal(vm.drafts.value[0].text,original)
 await vm.confirm();await vm.confirm()
 assert.equal(calls[1].url,'/api/im/rfq/send');assert.equal(calls[1].body.batch_id,'batch-test');assert.equal(calls[1].body.expected_version,3);assert.equal(calls[1].body.items,undefined);assert.equal(calls[1].body.request_id,calls[2].body.request_id)
 assert.equal(events.some(e=>e[0]==='sent'),false);assert.equal(vm.submission.value.commands[0].status,'queued');scope.stop()
})
test('改参数或收件人使已显示预览立即失效',async()=>{
 const {vm,scope}=setup();globalThis.fetch=async()=>response({ok:true,data:preview})
 await vm.requestPreview();assert.ok(vm.preview.value);vm.drafts.value[0].volume_wan='2000';await nextTick();assert.equal(vm.preview.value,null)
 await vm.requestPreview();vm.togglePerson('peer2');await nextTick();assert.equal(vm.preview.value,null);scope.stop()
})
test('预览期间编辑后旧响应不能复活授权',async()=>{
 const {vm,scope}=setup();let resolve!:(r:Response)=>void;globalThis.fetch=async()=>new Promise(r=>{resolve=r})
 const pending=vm.requestPreview();vm.drafts.value[0].step_bp='3';resolve(response({ok:true,data:preview}));await pending
 assert.equal(vm.preview.value,null);scope.stop()
})
test('空方向空量和非有限数字不能请求预览',async()=>{
 const {vm,scope}=setup();let calls=0;globalThis.fetch=async()=>{calls++;return response({ok:true,data:preview})}
 vm.drafts.value[0].our_side='';await vm.requestPreview();vm.drafts.value[0].our_side='BUY';vm.drafts.value[0].volume_wan=' ';await vm.requestPreview();vm.drafts.value[0].volume_wan='1000';vm.drafts.value[0].step_bp='Infinity';await vm.requestPreview();assert.equal(calls,0);scope.stop()
})
test('超1500字服务端首句不能进入确认发送',async()=>{
 const {vm,scope}=setup();let sends=0;globalThis.fetch=async(url)=>{if(String(url).endsWith('/send'))sends++;return response({ok:true,data:{...preview,commands_preview:[{peer_id:'peer1',text:'a'.repeat(1501)}]}})}
 await vm.requestPreview();await vm.confirm();assert.equal(vm.preview.value,null);assert.equal(sends,0);scope.stop()
})
test('确认响应丢失后重试保持原批次和同一个request_id',async()=>{
 const {vm,scope}=setup();const sent:any[]=[]
 globalThis.fetch=async(url,init)=>{if(String(url).endsWith('/preview'))return response({ok:true,data:preview});sent.push(JSON.parse(String(init?.body)));if(sent.length===1)throw Error('lost response');return response({ok:true,data:{batch_id:'batch-test',commands:[{command_id:'c',peer_id:'peer1',status:'pending',text:'x'}]}})}
 await vm.requestPreview();await vm.confirm();assert.ok(vm.preview.value);assert.match(vm.formError.value,/提交结果未确认/);await vm.confirm();assert.deepEqual(sent[0],sent[1]);scope.stop()
})
test('改方向和当前账号都作废授权，空估值不会按0预填',async()=>{
 const {vm,scope}=setup();globalThis.fetch=async()=>response({ok:true,data:preview})
 await vm.requestPreview();vm.drafts.value[0].our_side='SELL';assert.equal(vm.preview.value,null)
 vm.drafts.value[0].our_side='BUY';await vm.requestPreview();imLinks.value=[{id:'link2',active:true,tested:true,account_uid:'different'}] as any;assert.equal(vm.preview.value,null)
 const d=vm.drafts.value[0];d.fields.cnbd='';d.opening_yield_pct='';d.limit_yield_pct='';vm.prefillFromSaved(d);assert.equal(d.opening_yield_pct,'');assert.equal(d.limit_yield_pct,'');scope.stop()
})
test('本人已绑定但通道离线仍可生成零外发预览',async()=>{
 const {vm,scope}=setup();imLinks.value=[{id:'link1',active:true,tested:false,catalog_stale:true,account_uid:'owner-account',workbench_id:'wb1'}] as any
 vm.drafts.value[0].uins=['peer1'];let previewCalls=0
 globalThis.fetch=async(url)=>{if(String(url).endsWith('/preview'))previewCalls++;return response({ok:true,data:preview})}
 assert.equal(vm.canPreview.value,true);await vm.requestPreview();assert.equal(previewCalls,1);scope.stop()
})

test('50人上限按不同对手计数，同一人多券不重复',()=>{
 const {vm,scope}=setup()
 const draft=vm.drafts.value[0]
 const people=Array.from({length:50},(_,i)=>'synthetic-peer-'+i)
 vm.drafts.value=[{...draft,uins:[...people]},{...draft,code:'SYN-B',uins:[...people]}]
 assert.equal(vm.outgoingCount.value,50)
 assert.equal(vm.validationError(),'')
 vm.drafts.value[1].uins.push('synthetic-51')
 assert.equal(vm.outgoingCount.value,51)
 assert.match(vm.validationError(),/50个不同对手/)
 scope.stop()
})
test('账号改变后迟到人数摘要不能覆盖新账号',async()=>{
 let first!:(r:Response)=>void
 let calls=0
 globalThis.fetch=async(url)=>{
  if(!String(url).endsWith('/negotiation/capacity'))return response({ok:true,data:{}})
  calls++
  if(calls===1)return new Promise(r=>{first=r})
  return response({ok:true,data:{active:0,limit:50,available:50,unit:'distinct_counterparties',paused_counts:true}})
 }
 const {vm,scope}=setup()
 assert.equal(calls,1)
 imLinks.value=[{id:'link-capacity-new',active:true,tested:true,account_uid:'new-account',workbench_id:'wb-new'}] as any
 await nextTick();await new Promise(r=>setTimeout(r,0))
 assert.equal(vm.capacity.value?.active,0)
 first(response({ok:true,data:{active:50,limit:50,available:0,unit:'distinct_counterparties',paused_counts:true}}))
 await new Promise(r=>setTimeout(r,0))
 assert.equal(vm.capacity.value?.active,0)
 scope.stop()
})
