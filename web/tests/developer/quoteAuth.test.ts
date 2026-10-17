import { test } from 'node:test'
import assert from 'node:assert/strict'
import { QuoteCore } from '../../src/lib/quoteCore'
class Socket {
 static OPEN=1;static instances:Socket[]=[]
 readyState=1; sent:any[]=[];onopen:any;onmessage:any;onerror:any;onclose:any
 constructor(readonly url:string){Socket.instances.push(this)}
 send(text:string){this.sent.push(JSON.parse(text))} close(){}
}
const tick=()=>new Promise(r=>setTimeout(r,0))
test('worker静态请求带认证，WS先认证成功再订阅且stat不泄露token',async()=>{
 Object.defineProperty(globalThis,'WebSocket',{value:Socket,configurable:true})
 let header:Headers|undefined
 globalThis.fetch=async(_url,init)=>{header=new Headers(init?.headers);return new Response(JSON.stringify({ok:true,data:[]}))}
 const core=new QuoteCore();const received:any[]=[];const port=core.connectPort({postMessage:m=>received.push(m)})
 try {
 port.onMessage({type:'INIT_CONNECTIONS',identifiers:{clientId:'x',userScope:'userA',authToken:'secret-a',wsUrl:'ws://localhost/ws/quote'}} as any)
 port.onMessage({type:'JOIN_GROUP',groups:['bond-all']})
 const ws=Socket.instances.at(-1)!;ws.onopen();await tick()
 assert.equal(header?.get('Authorization'),'Bearer secret-a')
 assert.deepEqual(ws.sent,[{op:'auth',token:'secret-a'}])
 assert.equal(received.some(m=>m.type==='OPEN'),false)
 ws.onmessage({data:JSON.stringify({op:'auth',ok:true})})
 assert.deepEqual(ws.sent.at(-1),{op:'sub',groups:['bond-all']})
 assert.equal(received.some(m=>m.type==='OPEN'),true)
 assert.equal(JSON.stringify(core.snapshot()).includes('secret-a'),false)
 }finally{core.dispose()}
})
test('不同用户不得加入已有worker缓存',()=>{
 Object.defineProperty(globalThis,'WebSocket',{value:Socket,configurable:true})
 globalThis.fetch=async()=>new Response(JSON.stringify({ok:true,data:[]}))
 const core=new QuoteCore();const first=core.connectPort({postMessage(){}});const output:any[]=[];const second=core.connectPort({postMessage:m=>output.push(m)})
 try {
 first.onMessage({type:'INIT_CONNECTIONS',identifiers:{clientId:'a',userScope:'userA',authToken:'a',wsUrl:'ws://localhost/ws/quote'}} as any)
 second.onMessage({type:'INIT_CONNECTIONS',identifiers:{clientId:'b',userScope:'userB',authToken:'b'}} as any)
 assert.equal(output.some(m=>m.type==='ERROR'),true)
 assert.equal(core.snapshot().ports,1)
 }finally{core.dispose()}
})
test('未初始化的worker端口不能读取其他端口缓存或加入组',()=>{
 const core=new QuoteCore();const output:any[]=[];const port=core.connectPort({postMessage:m=>output.push(m)})
 try {port.onMessage({type:'JOIN_GROUP',groups:['bond-all']});port.onMessage({type:'PULL',requestTime:0});assert.deepEqual(core.snapshot().groups,[]);assert.equal(output.length,0)}finally{core.dispose()}
})
test('静态delta更新已有缓存，旧flow与迟到REST静态快照不能回退',async()=>{
 Object.defineProperty(globalThis,'WebSocket',{value:Socket,configurable:true})
 let finish!:(r:Response)=>void;globalThis.fetch=async()=>new Promise(r=>{finish=r})
 const core=new QuoteCore();const output:any[]=[];const port=core.connectPort({postMessage:m=>output.push(m)})
 try {
 port.onMessage({type:'INIT_CONNECTIONS',identifiers:{clientId:'a',userScope:'userA',authToken:'a',wsUrl:'ws://localhost/ws/quote'}} as any)
 port.onMessage({type:'JOIN_GROUP',groups:['bond-all']});const ws=Socket.instances.at(-1)!;ws.onopen();ws.onmessage({data:JSON.stringify({op:'auth',ok:true})})
 for(const [f,name] of [[2,'新券名'],[1,'旧券名']] as const) ws.onmessage({data:JSON.stringify({op:'batch',group:'bond-all',items:[{u:'u1',f,t:0,s:{bond_uni_code:'u1',windcode:'000001.IB',sec_name:name}}]})})
 finish(new Response(JSON.stringify({ok:true,data:[{bond_uni_code:'u1',windcode:'000001.IB',sec_name:'旧快照'}]})));await tick()
 port.onMessage({type:'CACHE_REQUEST',reqId:1,codes:['u1']});await tick()
 const cache=output.findLast(m=>m.type==='CACHE_RESPONSE');assert.equal(cache.data.get('u1').sec_name,'新券名')
 }finally{core.dispose()}
})
