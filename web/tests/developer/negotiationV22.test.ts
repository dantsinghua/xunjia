import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick } from 'vue'
import { api } from '../../src/api'
import Monitor from '../../src/pages/Monitor.vue'
import Rfq from '../../src/components/RfqAssignModal.vue'
import { imLinks } from '../../src/lib/imLink'
import { buildBonds } from './fixtures'

const response=(data:unknown)=>new Response(JSON.stringify(data))
const scopes:ReturnType<typeof effectScope>[]=[]
afterEach(()=>{for(const scope of scopes.splice(0))scope.stop()})
function setup(component:any,props:any={}) {
  const scope=effectScope()
  scopes.push(scope)
  const vm:any=scope.run(()=>component.setup(props,{expose(){},emit(){}}))
  return {vm,scope}
}
const cp={id:'peer',name:'对手',bonds:[{code:'A.IB',name:'甲券',sessionId:'ma',groupId:'ga'},{code:'B.IB',name:'乙券',sessionId:'mb',groupId:'gb'}]}
function deferred<T>() { let resolve!:(v:T)=>void; const promise=new Promise<T>(r=>{resolve=r});return {promise,resolve} }

test('V2消息保留原文、解析水位和多券关联，原文不改写证券代码',async()=>{
  globalThis.fetch=async()=>response({ok:true,data:{revision:12,messages:[{id:'m1',revision:2,projection_revision:12,text:'A.IB 2.20，B.IB 3.04',role:'peer',time:1720000000,analysis_state:'ANALYZED',contexts:[{code:'A.IB',name:'甲券',member_id:'ma',relation:'explicit'},{code:'B.IB',name:'乙券',member_id:'mb',relation:'explicit'}]}]}})
  const r:any=await api.monitorChat('peer')
  assert.equal(r.revision,12);assert.equal(r.data[0].revision,12);assert.equal(r.data[0].analysisState,'ANALYZED')
  assert.equal(r.data[0].contexts[1].memberId,'mb')
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp]
  assert.equal(vm.shownText('A.IB 2.20，B.IB 3.04'),'A.IB 2.20，B.IB 3.04');scope.stop()
})

test('双边价量独立保留来源、最终性和跨日有效性，不把净价显示成收益率',async()=>{
  globalThis.fetch=async()=>response({ok:true,revision:7,data:[{cp_id:'peer',thread_id:'t',name:'对手',bonds:[{session_id:'ma',group_id:'ga',code:'A.IB',revision:3,trade_day:'2026-10-17',analysis_pending:true,quotes:{bid:{side:'bid',price:{kind:'YIELD',value:'2.20'},volume_wan:'5000',revision:4,finality:'FINAL',validity:'ACTIVE',assertion:'QUOTE',confirmed_day:'2026-10-16',evidence:{price:['m1'],volume_wan:['m2']}},ofr:{side:'ofr',price:{kind:'NET_PRICE',value:'34.19'},volume_wan:null,revision:2,finality:'UNKNOWN',validity:'UNCONFIRMED',assertion:'INDICATIVE',confirmed_day:null,evidence:{price:['m3']}}},summary:{final_confirmed_volume_wan:'0',target_volume_wan:'5000',goal_ready:false}}]}]})
  const r:any=await api.counterparties();const b=r.data[0].bonds[0]
  assert.equal(r.revision,7);assert.equal(b.groupId,'ga');assert.equal(b.analysisPending,true)
  assert.equal(b.quotes.bid.volume_wan,'5000');assert.equal(b.quotes.ofr.volume_wan,null)
  assert.deepEqual(b.quotes.bid.evidence.price,['m1']);assert.equal(b.tradeDay,'2026-10-17')
  const {vm,scope}=setup(Monitor)
  assert.equal(vm.quotePrice(b.quotes.ofr),'34.19 净价')
  assert.match(vm.quoteValidity(b.quotes.bid,b.tradeDay),/跨日|待确认/);scope.stop()
})

test('同消息旧revision快照不能回盖已解析价量和多券归属',async()=>{
  let revision=12
  globalThis.fetch=async()=>response({ok:true,data:{revision,messages:[{id:'m1',revision:1,projection_revision:revision,text:'原文',analysis_state:revision===12?'ANALYZED':'QUEUED',contexts:revision===12?[{code:'A.IB',name:'甲券'},{code:'B.IB',name:'乙券'}]:[]}]}})
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp];vm.selCpId.value='peer';await nextTick()
  await vm.loadChat();revision=9;await vm.loadChat()
  assert.equal(vm.rawChatCache.peer[0].contexts.length,2);assert.equal(vm.rawChatCache.peer[0].analysisState,'ANALYZED');scope.stop()
})

test('监控刷新不让慢结构化摘要阻塞已落库原文显示',async()=>{
  const summary=deferred<Response>();const rawFetched=deferred<void>()
  globalThis.fetch=async(url)=>{
    if(String(url).includes('/summary'))return summary.promise
    if(String(url).includes('/counterparties'))return response({ok:true,data:[{...cp,bonds:[]}]})
    rawFetched.resolve();return response({ok:true,data:{revision:1,messages:[{id:'m',text:'刚收到',analysis_state:'QUEUED'}]}})
  }
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp];vm.selCpId.value='peer';await nextTick();await vm.loadChat()
  delete vm.rawChatCache.peer
  const refresh=vm.reloadDesk()
  await rawFetched.promise;await nextTick();await new Promise(r=>setImmediate(r))
  const displayed=vm.rawChatCache.peer?.[0]?.text
  summary.resolve(response({ok:true,data:{stats:[]}}));await refresh
  assert.equal(displayed,'刚收到');scope.stop()
})

test('券卡人工接管和恢复只提交该券member_id，不提交整个对手线程',async()=>{
  const writes:any[]=[]
  globalThis.fetch=async(url,init)=>{if(init?.method==='POST')writes.push([String(url),JSON.parse(String(init.body))]);return response({ok:true,data:String(url).includes('counterparties')?[]:{messages:[]}})}
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp];vm.selCpId.value='peer';vm.activeBond.value='A.IB'
  await vm.pauseSession('ma');await vm.resumeSession('ma')
  assert.deepEqual(writes.map(w=>w[1]),[{member_id:'ma'},{member_id:'ma'}]);scope.stop()
})

test('停止A券使用group身份且不关闭同线程B券',async()=>{
  const writes:any[]=[]
  globalThis.fetch=async(url,init)=>{if(init?.method==='POST')writes.push([String(url),JSON.parse(String(init.body))]);return response({ok:true,data:String(url).includes('counterparties')?[]:{messages:[]}})}
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp];vm.selCpId.value='peer'
  await vm.markBond('A.IB','已放弃','ga')
  assert.deepEqual(writes,[['/api/negotiation/groups/ga/sending',{allowed:false}]]);scope.stop()
})

test('UNKNOWN不作为失败重发，过期终态清除对应待回执',async()=>{
  let state='unknown',posts=0
  globalThis.fetch=async(url,init)=>{if(init?.method==='POST')posts++;return response(String(url).includes('/receipts/')?{ok:true,data:{cmd_id:'c1',status:state}}:String(url).includes('/monitor/send')?{ok:true,data:{cmd_id:'c1',status:'pending'}}:{ok:true,data:{messages:[]}})}
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp];vm.selCpId.value='peer';vm.chatInput.value='询价'
  await vm.sendChat();assert.equal(vm.sendPhase.value,'unknown');await vm.sendChat();assert.equal(posts,1)
  state='expired';await vm.pollReceipts();assert.equal(vm.sendPhase.value,'failed');assert.equal(vm.pendingReceipts.size,0);scope.stop()
})

test('迟到旧SENT不清除新命令的pending或新草稿',async()=>{
  let n=0,oldDone=false
  globalThis.fetch=async(url,init)=>{
    if(init?.method==='POST')return response({ok:true,data:{cmd_id:'c'+(++n),status:'pending'}})
    if(String(url).includes('/receipts/'))return response({ok:true,data:{cmd_id:String(url).endsWith('c1')?'c1':'c2',status:String(url).endsWith('c1')&&oldDone?'sent':'pending'}})
    return response({ok:true,data:{messages:[]}})
  }
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp];vm.selCpId.value='peer';vm.chatInput.value='第一条';await vm.sendChat()
  vm.chatInput.value='第二条';await vm.sendChat();oldDone=true;await vm.pollReceipts()
  assert.equal(vm.sendPhase.value,'submitted');assert.equal(vm.chatInput.value,'第二条');assert.equal(vm.pendingReceipts.has('c2'),true)
  assert.ok(vm.sendAttempt?.id);assert.equal(vm.sendAttempt.id,vm.pendingReceipts.get('c2')?.requestId);scope.stop()
})

test('新核心preview状态可确认且BP先加减完整估值再四舍五入',async()=>{
  imLinks.value=[{id:'x',active:true,account_uid:'acct',workbench_id:'bench'}] as any
  globalThis.fetch=async()=>response({ok:true,data:{batch_id:'v22',version:1,status:'preview',items:[],commands_preview:[{peer_id:'peer',text:'A.IB 3.04%收'}]}})
  const bond={...buildBonds()[0],code:'A.IB',val:'2.9872/100'}
  const props={open:false,bonds:[bond],savedFloors:{'A.IB':{kind:'bp' as const,value:5}},strategy:{configured:true,startDev:10,step:1}}
  const {vm,scope}=setup(Rfq,props);props.open=true
  const d=vm.seed(bond);d.our_side='BUY';d.uins=['peer'];d.volume_wan='5000';vm.prefillFromSaved(d)
  assert.equal(d.limit_yield_pct,'3.04');assert.equal(d.opening_yield_pct,'3.09')
  vm.drafts.value=[d];await vm.requestPreview();assert.equal(vm.preview.value?.batch_id,'v22');scope.stop()
})

test('增量通知接受跨chunk的SSE并只推进新revision，重连携带已读水位',async()=>{
  let path=''
  const frames=['id: 13\nevent: changed\nda','ta: {"revision":13}\n\n',': keepalive\n\n','id: 12\nevent: changed\ndata: {"revision":12}\n\n','id: 14\r\nevent: changed\r\ndata: {"revision":14}\r\n\r\n']
  globalThis.fetch=async(url)=>{path=String(url);return new Response(new ReadableStream({start(c){for(const text of frames)c.enqueue(new TextEncoder().encode(text));c.close()}}),{headers:{'Content-Type':'text/event-stream'}})}
  const revisions:number[]=[];let opened=0
  await (api as any).negotiationUpdates(11,new AbortController().signal,(r:number)=>revisions.push(r),()=>opened++)
  assert.equal(path,'/api/negotiation/updates?after=11');assert.deepEqual(revisions,[13,14]);assert.equal(opened,1)
})

test('连接断线后按最新水位重连并触发全页补拉',async()=>{
  const {vm,scope}=setup(Monitor);let opened=0;let refreshes=0
  globalThis.fetch=async(url)=>{
    if(String(url).includes('/negotiation/updates')) {opened++;return new Response(new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('event: changed\ndata: {"revision":20}\n\n'));c.close()}}),{headers:{'Content-Type':'text/event-stream'}})}
    refreshes++;return response({ok:true,data:String(url).includes('/counterparties')?[]:{stats:[],messages:[]}})
  }
  await vm.connectUpdates();vm.stopUpdates();await nextTick()
  assert.equal(vm.lastEventRevision,20);assert.ok(refreshes>=2);assert.equal(opened,1);scope.stop()
})

test('同账号返回较旧结构化快照不撤销已更新券组',async()=>{
  let revision=20
  globalThis.fetch=async(url)=>response(String(url).includes('/counterparties')?{ok:true,revision,data:[{id:'peer',name:'对手',bonds:[{code:'A.IB',session_id:'ma',group_id:'ga',revision,stage:revision===20?'待人工确认':'询价中'}]}]}:{ok:true,data:{revision,stats:[],messages:[]}})
  const {vm,scope}=setup(Monitor);await vm.reloadDesk();revision=10;await vm.reloadDesk()
  assert.equal(vm.cps.value[0].bonds[0].stage,'待人工确认');scope.stop()
})

test('采集健康变差不会切换账号身份或抹掉已接收原文',async()=>{
  imLinks.value=[{id:'x',active:true,tested:true,account_uid:'acct',workbench_id:'bench'}] as any
  globalThis.fetch=async()=>response({ok:false,hint:'offline'})
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp];vm.selCpId.value='peer';await nextTick()
  vm.rawChatCache.peer=[{id:'keep',text:'已落库原文'}]
  imLinks.value=[{id:'x',active:true,tested:false,account_uid:'acct',workbench_id:'bench'}] as any
  await nextTick();vm.stopUpdates()
  assert.equal(vm.rawChatCache.peer?.[0].id,'keep');scope.stop()
})

test('切换对手后返回UNKNOWN线程不能再次发送同一未决内容',async()=>{
  let posts=0
  globalThis.fetch=async(url,init)=>{if(init?.method==='POST')posts++;return response(String(url).includes('/receipts/')?{ok:true,data:{cmd_id:'c1',status:'unknown'}}:String(url).includes('/monitor/send')?{ok:true,data:{cmd_id:'c1',status:'pending'}}:{ok:true,data:{messages:[]}})}
  const {vm,scope}=setup(Monitor);vm.cps.value=[cp,{id:'other',name:'其他',bonds:[]}];vm.selCpId.value='peer';vm.chatInput.value='询价';await vm.sendChat()
  vm.selCpId.value='other';await nextTick();vm.selCpId.value='peer';await nextTick();await vm.sendChat()
  assert.equal(posts,1);scope.stop()
})

test('批次已提交后只查同一命令回执，失败取消和UNKNOWN分别展示',async()=>{
  const {vm,scope}=setup(Rfq,{open:false,bonds:[]});let writes=0
  vm.submission.value={batch_id:'batch',commands:[{command_id:'a',status:'queued'},{command_id:'b',status:'pending'},{command_id:'c',status:'unknown'}]}
  globalThis.fetch=async(url,init)=>{if(init?.method==='POST')writes++;const id=String(url).split('/').pop();return response({ok:true,data:{cmd_id:id,status:id==='a'?'sent':id==='b'?'cancelled':'unknown',reason:id==='b'?'stopped':''}})}
  await vm.pollSubmission()
  assert.deepEqual(vm.submission.value.commands.map((c:any)=>c.status),['sent','cancelled','unknown']);assert.equal(writes,0);scope.stop()
})

test('更早批次回执不能覆盖新批次且错误cmd_id不改变状态',async()=>{
  const {vm,scope}=setup(Rfq,{open:false,bonds:[]});const pending=deferred<Response>()
  vm.submission.value={batch_id:'old',commands:[{command_id:'old-c',status:'pending'}]}
  globalThis.fetch=async()=>pending.promise
  const polling=vm.pollSubmission();vm.submission.value={batch_id:'new',commands:[{command_id:'new-c',status:'pending'}]}
  pending.resolve(response({ok:true,data:{cmd_id:'old-c',status:'sent'}}));await polling
  assert.equal(vm.submission.value.commands[0].status,'pending')
  globalThis.fetch=async()=>response({ok:true,data:{cmd_id:'wrong',status:'sent'}})
  await vm.pollSubmission();assert.equal(vm.submission.value.commands[0].status,'pending');scope.stop()
})

test('首批券卡与默认展开状态同时出现，之后刷新不反转用户折叠选择',async()=>{
  globalThis.fetch=async(url)=>response({ok:true,data:String(url).includes('/counterparties')?[{id:'peer',name:'对手',bonds:[{code:'A.IB',session_id:'ma'}]}]:{stats:[],messages:[]}})
  const {vm,scope}=setup(Monitor);await vm.reloadDesk()
  assert.equal(vm.activeBond.value,'A.IB');assert.equal(vm.openBonds['A.IB'],true)
  vm.toggleBond('A.IB');await vm.reloadDesk();assert.notEqual(vm.openBonds['A.IB'],true);scope.stop()
})
