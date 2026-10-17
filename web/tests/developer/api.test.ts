import { test } from 'node:test'
import assert from 'node:assert/strict'
import { api, traceOf } from '../../src/api'
import { buildBonds } from './fixtures'
const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'Content-Type':'application/json' } })
test('读取网络故障保留失败语义', async () => {
  globalThis.fetch = async () => { throw new Error('offline') }
  assert.equal((await api.monitorChat('A')).ok, false)
})
test('写失败不可伪装成成功', async () => {
  globalThis.fetch = async () => response({ ok:false, hint:'denied' })
  assert.equal((await api.rfqStop({ windcodes:['A.IB'] })).ok, false)
  assert.equal((await api.saveStrategy({startDev:10,boundary:5,step:2,strict:true,roundLot:1000,crossDay:3})).ok, false)
})
test('策略服务端模板显式转换并提交DTO', async () => {
  const dto = { templates:[{template_id:'s1',start_offset_bp:9,boundary_bp:3,step_bp:2,strict_match:false}],active_template_id:'s1',system:{s1_round_lot_wan:500,s2_crossday_bp:4} }
  let sent:any
  globalThis.fetch = async (_url, init) => { if(init?.body) sent=JSON.parse(String(init.body)); return response({ok:true,data:dto}) }
  const r = await api.getStrategy()
  assert.equal(r.data.startDev,9)
  assert.equal(r.data.strict,false)
  await api.saveStrategy(r.data)
  assert.equal(sent.template_id,'s1')
  assert.equal(sent.start_offset_bp,9)
  assert.equal(sent.system.s1_round_lot_wan,500)
})
test('债券详情缺成交/曲线时不造值', async () => {
  const bond=buildBonds()[0]
  globalThis.fetch=async(url)=> response(String(url).includes('/api/bond/')?{ok:true,data:{bond,quotes:[]}}:{ok:true,data:[]})
  const r=await api.bond(bond.code,bond)
  assert.deepEqual(r.data.trades,[])
  assert.equal(r.data.hist.avg,'')
  assert.equal(r.data.intra.bid,'')
})
test('没有审计证据时追踪明确缺失',()=>{
  const r=traceOf({role:'human',text:'hello',tag:'',time:'10:00',bond:'A.IB',idx:0,cp:'A'})
  assert.equal(r.steps.length,0)
  assert.equal(r.status,'无审计记录')
})
test('聊天归属保留券code不换成简称',async()=>{
  globalThis.fetch=async()=>response({ok:true,data:{bonds:[{windcode:'A.IB',name:'同名债'}],messages:[{role:'human',text:'hi',bond:'A.IB',time:1}]}})
  assert.equal((await api.monitorChat('A')).data[0].bond,'A.IB')
})
test('所有HTTP携带询价独立token，HTTP失败不当业务成功',async()=>{
 const store=new Map([['xj_auth_token','token-x']])
 Object.defineProperty(globalThis,'localStorage',{value:{getItem:(k:string)=>store.get(k)??null,removeItem:(k:string)=>store.delete(k)},configurable:true})
 let headers:Headers|undefined
 globalThis.fetch=async(_url,init)=>{headers=new Headers(init?.headers);return new Response(JSON.stringify({hint:'unauthorized'}),{status:401})}
 assert.equal((await api.rfqStop({windcodes:['A.IB']})).ok,false)
 assert.equal(headers?.get('Authorization'),'Bearer token-x')
 assert.equal(headers?.get('X-Auth-Token'),'token-x')
})
test('新账号策略是未保存草稿，首次保存显式personal且不自动写入',async()=>{
 let writes=0;let sent:any
 globalThis.fetch=async(_url,init)=>{
  if(init?.method==='POST'){writes++;sent=JSON.parse(String(init.body));return response({ok:true,data:{configured:true,templates:[{...sent}],active_template_id:sent.template_id,system:sent.system}})}
  return response({ok:true,data:{configured:false,templates:[],active_template_id:null,system:{}}})
 }
 const draft=await api.getStrategy()
 assert.equal(draft.ok,true);assert.equal(draft.data.configured,false);assert.equal(writes,0)
 assert.equal(draft.data.templateId,'personal')
 const saved=await api.saveStrategy(draft.data)
 assert.equal(saved.ok,true);assert.equal(saved.data.configured,true);assert.equal(sent.template_id,'personal');assert.equal(writes,1)
})
test('按session归档使用明确POST且失败保留',async()=>{
 let url='';let method=''
 globalThis.fetch=async(u,init)=>{url=String(u);method=init?.method||'';return response({ok:false,hint:'仍有未决指令'})}
 const r=await api.archiveSession('session/one')
 assert.equal(url,'/api/sessions/session%2Fone/archive');assert.equal(method,'POST');assert.equal(r.ok,false)
})
test('问答图表缺失值不能渲染为零',async()=>{
 globalThis.fetch=async()=>response({ok:true,data:{answer:'Ofr缺失',chart:{type:'bar',categories:['Bid','Ofr'],series:[{name:'收益率',data:[2.4,null]}]}}})
 const chart=(await api.qa('报价 230210.IB')).data.chart
 assert.deepEqual(chart?.categories,['Bid']);assert.deepEqual(chart?.series?.[0]?.data,[2.4])
})
test('监控导出可选peer明确传递，不携带任意owner',async()=>{
 let url=''
 globalThis.fetch=async(u)=>{url=String(u);return new Response('xlsx',{headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}})}
 await api.exportMonitor('2026-10-01','2026-10-13','peer/a')
 const query=new URL(url,'https://local.test').searchParams
 assert.equal(query.get('peer'),'peer/a');assert.equal(query.has('owner'),false)
})
