import { test } from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../../src/api'
const response=(data:unknown)=>new Response(JSON.stringify(data))
test('交易板保留服务端方向、限价、gap和行情，不推算缺值',async()=>{
 globalThis.fetch=async()=>response({ok:true,data:[{id:'p',name:'对手',bonds:[{code:'000001.IB',name:'券',own_side:'SELL',peer_quote:'-0.05',own_limit:'-0.10',gap_bp:'0.00',market_bid:'1.95',market_ofr:null,quote_at:'2026-10-13T10:00:00+08:00',freshness:'已过期',phase:'等待人工'}]}]})
 const bond=(await api.counterparties()).data[0].bonds[0]
 assert.deepEqual((bond as any).trade,{own_side:'SELL',peer_quote:'-0.05',own_limit:'-0.10',gap_bp:'0.00',market_bid:'1.95',market_ofr:'—',quote_at:'2026-10-13T10:00:00+08:00',freshness:'已过期',phase:'等待人工'})
})
test('XLSX导出发送完整筛选且返回服务端原始文件',async()=>{
 const filter={keyword:'测试',chips:{当前状态:3},prices:{Bid:{lo:'2',hi:'3'}},channel:'broker',broker:'9',groups:['bond-all']}
 let path='';globalThis.fetch=async(url)=>{path=String(url);return new Response(new Uint8Array([80,75,3,4]),{headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':"attachment; filename*=UTF-8''%E8%A1%8C%E6%83%85.xlsx"}})}
 const file=await (api as any).exportBonds(filter)
 assert.deepEqual(JSON.parse(new URL(path,'http://local').searchParams.get('filter')!),filter);assert.equal(file.filename,'行情.xlsx');assert.deepEqual([...new Uint8Array(await file.blob.arrayBuffer())],[80,75,3,4])
})
test('导出返回JSON失败不能下载成伪XLSX',async()=>{
 globalThis.fetch=async()=>new Response(JSON.stringify({ok:false,hint:'导出拒绝'}),{headers:{'Content-Type':'application/json'}})
 await assert.rejects(()=>(api as any).exportBonds({}),/导出拒绝/)
})
test('正式public行情与未配置空态可见，mock仍拒绝',async()=>{
 globalThis.fetch=async()=>response({ok:true,source:'public',data:[{code:'000100.IB',name:'正式行情'}],total:1})
 assert.equal((await api.bonds()).ok,true)
 globalThis.fetch=async()=>response({ok:true,source:'not_configured',data:[],total:0,emptyReason:'尚未配置行情库'})
 const empty=await api.bonds();assert.equal(empty.ok,true);assert.equal(empty.emptyReason,'尚未配置行情库')
 globalThis.fetch=async()=>response({ok:true,source:'mock',data:[{code:'000100.IB',name:'示例'}]})
 assert.equal((await api.bonds()).ok,false)
})
test('仅有出站方向时显示本方，不猜人工或机器人',async()=>{
 globalThis.fetch=async()=>response({ok:true,data:{messages:[{dir:'out',text:'one'},{dir:'outgoing',text:'two'},{dir:'out',role:'bot',text:'three'}]}})
 const rows=(await api.monitorChat('p')).data;assert.equal(rows[0].role,'self');assert.equal(rows[0].tag,'本方');assert.equal(rows[1].role,'self');assert.equal(rows[2].role,'bot')
})
