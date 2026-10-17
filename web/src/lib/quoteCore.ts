/**
 * 实时行情管线 · worker 侧核心逻辑(环境无关)
 *
 * 为什么单独抽成一个模块而不直接写在 quoteWorker.ts 里:
 *   三级降级(契约 §7)的第 3 级是"主线程直连 WS,保留 500ms pull 语义"。
 *   如果累积/去重/背压/引用计数这套逻辑只活在 worker 入口文件里,主线程兜底就得再抄一份,
 *   两份代码迟早分叉。所以把核心做成一个只依赖 WebSocket/fetch/setTimeout 的类,
 *   worker 入口(SharedWorker / Worker)和主线程兜底都 new 同一个 QuoteCore,
 *   区别仅在于"端口"是真 MessagePort 还是进程内的假端口。
 *
 * 本文件禁止碰 window / document —— 它会被 Vite 打进 worker bundle。
 */
import type {
  BondStatic, QuoteDelta, GatewayInMsg, GatewayOutMsg,
  MainToWorkerMsg, WorkerToMainMsg, WorkerStat, ConnectionIdentifiers,
} from './quoteTypes'

/** 契约 §4.2:每端口每组的窗口上限,超出丢最旧并累加 dropped */
export const MAX_PENDING_PER_GROUP = 5000
/** 心跳间隔;超过 2 个心跳没收到任何网关消息视为半开连接,主动断掉走重连 */
export const HEARTBEAT_MS = 30_000
/** 断线指数退避:1s→2s→4s→8s→上限 15s;连上后归零 */
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000]
/** 静态表整表拉取失败后的重试退避,同一张表 */
const STATIC_RETRY_MS = BACKOFF_MS

/** worker 往主线程发消息的最小抽象:真 MessagePort、DedicatedWorkerGlobalScope、进程内假端口都满足 */
export interface CorePort {
  postMessage(msg: WorkerToMainMsg): void
}

/** connectPort 返回的句柄:入口把收到的主线程消息喂给 onMessage,端口消失时调 close */
export interface PortHandle {
  onMessage(msg: MainToWorkerMsg): void
  close(): void
}

/** 每个端口(= 每个标签页)的私有状态 */
interface PortState {
  id: number
  initialized: boolean
  port: CorePort
  /** 该端口订阅的组 */
  groups: Set<string>
  /**
   * 该端口的累积窗口:group -> (bond_uni_code -> 最新 delta)。
   * 为什么按端口而不是全局只留一份:各标签页各自 pull、各自清窗口。
   * 如果窗口共享,A 标签 pull 完清空,B 标签这一轮就丢数据了。
   * 代价是后台标签(不 pull)的窗口会一直涨 —— 这正是背压上限存在的理由。
   */
  pending: Map<string, Map<string, QuoteDelta>>
  /** 本窗口内因背压丢弃的条数,随 BATCH 回给主线程后清零 */
  dropped: number
}

export class QuoteCore {
  private ports = new Map<number, PortState>()
  private nextPortId = 1

  /** 组 -> 持有该组的端口 id 集合。引用计数就是 Set 的 size。 */
  private groupRefs = new Map<string, Set<number>>()

  private ws: WebSocket | null = null
  private wsState: WorkerStat['wsState'] = 'idle'
  private wsUrl: string | null = null
  private staticBase = '/api/bond-static'
  private authToken = ''
  private userScope = ''
  private authenticated = false
  private staticGeneration = 0
  private retry = 0
  private reconnects = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private lastRx = 0
  private closed = false

  private rxBatches = 0
  private rxItems = 0
  private staleItems = 0

  private staticMap = new Map<string, BondStatic>()
  private staticVersions = new Map<string, number>()
  private staticState: WorkerStat['staticState'] = 'idle'
  private staticRetry = 0
  private staticTimer: ReturnType<typeof setTimeout> | null = null
  /** 静态表首轮加载结束(成功或失败)前到达的 CACHE_REQUEST 先排队 */
  private staticWaiters: Array<() => void> = []
  private staticWarned = false

  /* ------------------------------------------------------------------ */
  /*                            端口生命周期                              */
  /* ------------------------------------------------------------------ */

  connectPort(port: CorePort): PortHandle {
    const st: PortState = { id: this.nextPortId++, initialized:false, port, groups: new Set(), pending: new Map(), dropped: 0 }
    this.ports.set(st.id, st)
    return {
      onMessage: (msg) => this.handle(st, msg),
      close: () => this.detachPort(st),
    }
  }

  /** 端口离开:释放它持有的全部组引用,归零的组向网关 unsub;没端口了就把连接也收掉 */
  private detachPort(st: PortState) {
    if (!this.ports.has(st.id)) return
    this.ports.delete(st.id)
    this.releaseGroups(st, [...st.groups])
    st.pending = new Map()
    if (this.ports.size === 0) {
      // 没人听了还挂着长连接只是白占网关一个 fd。下一个端口 INIT 时会重新连。
      this.teardownSocket()
      this.staticGeneration++; this.staticMap.clear(); this.staticVersions.clear(); this.staticState = 'idle'
      this.authToken = ''; this.userScope = ''
      if (this.staticTimer) { clearTimeout(this.staticTimer); this.staticTimer = null }
    }
  }

  /** 整个核心销毁(主线程兜底模式 dispose 时用;真 worker 由浏览器直接杀,不需要) */
  dispose() {
    this.closed = true
    for (const st of [...this.ports.values()]) this.detachPort(st)
    this.teardownSocket()
    if (this.staticTimer) { clearTimeout(this.staticTimer); this.staticTimer = null }
  }

  /* ------------------------------------------------------------------ */
  /*                        主线程 → worker 消息                          */
  /* ------------------------------------------------------------------ */

  private handle(st: PortState, msg: MainToWorkerMsg) {
    if (!this.ports.has(st.id) || (!st.initialized && msg.type !== 'INIT_CONNECTIONS' && msg.type !== 'CLOSE')) return
    switch (msg.type) {
      case 'INIT_CONNECTIONS':
        this.init(st, msg.identifiers)
        break
      case 'JOIN_GROUP':
        this.joinGroups(st, msg.groups)
        break
      case 'LEAVE_GROUP':
        this.releaseGroups(st, msg.groups)
        break
      case 'PULL':
        this.pull(st, msg.requestTime)
        break
      case 'CACHE_REQUEST':
        this.cacheRequest(st, msg.reqId, msg.codes)
        break
      case 'CLOSE':
        this.detachPort(st)
        break
    }
  }

  private init(st: PortState, ids: ConnectionIdentifiers) {
    if (!ids.authToken || !ids.userScope || (this.userScope && this.userScope !== ids.userScope)) {
      st.port.postMessage({ type:'ERROR', at:Date.now(), message:'行情身份不匹配或未登录' })
      this.detachPort(st)
      return
    }
    if (this.authToken !== ids.authToken) {
      this.teardownSocket()
      this.authToken = ids.authToken
      this.staticGeneration++; this.staticState = 'idle'
      if (this.staticTimer) { clearTimeout(this.staticTimer); this.staticTimer = null }
    }
    st.initialized = true
    this.userScope = ids.userScope
    if (!this.wsUrl) {
      this.wsUrl = ids.wsUrl || defaultWsUrl()
      if (ids.staticBase) this.staticBase = ids.staticBase
    }
    this.ensureSocket()
    this.ensureStatic()
    // 立刻回一条 STAT 当握手:主线程用"收到第一条消息"判定这一级传输层可用(见 quoteClient 的三级降级)
    st.port.postMessage({ type: 'STAT', stat: this.snapshot() })
  }

  /* ------------------------------------------------------------------ */
  /*                       组订阅 · 引用计数                              */
  /* ------------------------------------------------------------------ */

  /**
   * 为什么要引用计数:SharedWorker 下一条 WS 服务 N 个标签页。
   * 标签 A、B 都看 bond-all,A 关掉时若直接向网关 unsub,B 的行情就断了。
   * 所以组的订阅状态 = "还有没有任何端口要它",最后一个端口退订才真正发 unsub。
   */
  private joinGroups(st: PortState, groups: string[]) {
    const fresh: string[] = []
    for (const g of groups) {
      if (st.groups.has(g)) continue
      st.groups.add(g)
      let refs = this.groupRefs.get(g)
      if (!refs) { refs = new Set(); this.groupRefs.set(g, refs); fresh.push(g) }
      refs.add(st.id)
    }
    if (fresh.length) this.send({ op: 'sub', groups: fresh })
  }

  private releaseGroups(st: PortState, groups: string[]) {
    const gone: string[] = []
    for (const g of groups) {
      if (!st.groups.delete(g)) continue
      // 退订的组在本端口窗口里残留的增量没人要了,顺手清掉,免得下次再订同一组时先吐一堆陈旧数据
      st.pending.delete(g)
      const refs = this.groupRefs.get(g)
      if (!refs) continue
      refs.delete(st.id)
      if (refs.size === 0) { this.groupRefs.delete(g); gone.push(g) }
    }
    if (gone.length) this.send({ op: 'unsub', groups: gone })
  }

  /* ------------------------------------------------------------------ */
  /*                     累积 · 去重 · 背压(契约 §4.2)                    */
  /* ------------------------------------------------------------------ */

  private onGatewayBatch(group: string, items: QuoteDelta[]) {
    for (const d of items) {
      if (d.s?.bond_uni_code === d.u && d.f >= (this.staticVersions.get(d.u) ?? -1)) {
        this.putStatic(d.s); this.staticVersions.set(d.u, d.f)
      }
    }
    this.rxBatches++
    this.rxItems += items.length
    const refs = this.groupRefs.get(group)
    if (!refs) return // 刚 unsub 完网关还在推的尾巴,直接丢
    for (const pid of refs) {
      const st = this.ports.get(pid)
      if (!st) continue
      let pend = st.pending.get(group)
      if (!pend) { pend = new Map(); st.pending.set(group, pend) }
      for (const d of items) {
        const cur = pend.get(d.u)
        if (cur) {
          // 同一只券已在窗口里:只保留最大 flow_id(last-write-wins)。
          // 版本不新的直接忽略 —— 这是去重,不是背压丢弃,不计入 dropped。
          if (cur.f >= d.f) { this.staleItems++; continue }
          // 先 delete 再 set,让这只券挪到 Map 尾部(最新)。
          // 这样 Map 的迭代顺序 = "最久没更新 → 最近更新",背压淘汰时 keys().next() 拿到的就是最该丢的那只。
          pend.delete(d.u)
          // 字段级合并而非整体替换:delta 字段都是可选的,若新一条恰好缺某字段,整体替换会把上一条的值抹掉。
          pend.set(d.u, { ...cur, ...d })
          continue
        }
        if (pend.size >= MAX_PENDING_PER_GROUP) {
          // 背压:窗口满了(典型场景是后台标签长时间不 pull)。
          // 策略是丢"最旧"而不是丢"最新"或拒收:行情是状态量不是事件流,
          // 用户切回来时要的是每只券的最新价,最旧那条大概率早已被更新的行覆盖了语义;
          // 丢最新则会让用户看到过期价格。丢掉的条数累计给主线程,>0 即告警(契约 §6)。
          const oldest = pend.keys().next().value as string
          pend.delete(oldest)
          st.dropped++
        }
        pend.set(d.u, d)
      }
    }
  }

  /**
   * 为什么是主线程 pull 而不是 worker push:
   *   push 的节奏由行情决定,高峰期每秒几十批,主线程每收一批就得跑一次 upsert+render,
   *   这正是事故根源(每条 WS 一次渲染 → 130% CPU)。
   *   pull 把节奏交给消费者:主线程每 500ms 拿一次"这段时间每只券的最终状态",
   *   渲染频率被硬性封顶在 2Hz;标签页隐藏时干脆不拉,worker 这边只是内存里多攒几条,主线程零开销。
   */
  private pull(st: PortState, requestTime: number) {
    const notification = new Map<string, QuoteDelta[]>()
    for (const [g, pend] of st.pending) {
      if (pend.size) notification.set(g, [...pend.values()])
    }
    // 回完即清空窗口。用新建 Map 代替 clear():发出去的 notification 与新窗口零共享,
    // 主线程兜底模式(无结构化克隆,对象按引用传递)下也不会出现"主线程还在遍历、worker 已在往里写"的交叉。
    st.pending = new Map()
    const dropped = st.dropped
    st.dropped = 0
    st.port.postMessage({ type: 'BATCH', requestTime, workerSendTime: Date.now(), notification, dropped })
  }

  /* ------------------------------------------------------------------ */
  /*                        网关 WebSocket 连接                           */
  /* ------------------------------------------------------------------ */

  private ensureSocket() {
    if (this.closed || this.ws || this.reconnectTimer || !this.wsUrl) return
    this.connect()
  }

  private connect() {
    if (this.closed || !this.wsUrl) return
    this.wsState = 'connecting'
    let ws: WebSocket
    try {
      ws = new WebSocket(this.wsUrl)
    } catch (e) {
      this.broadcast({ type: 'ERROR', at: Date.now(), message: String(e) })
      this.scheduleReconnect()
      return
    }
    this.ws = ws
    ws.onopen = () => {
      if (this.ws !== ws) return
      this.authenticated = false
      this.send({ op:'auth', token:this.authToken })
    }
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return
      this.lastRx = Date.now()
      let m: GatewayInMsg
      try { m = JSON.parse(String(ev.data)) as GatewayInMsg } catch { return } // 脏包丢掉,不影响流
      if (m.op === 'auth') {
        if (!m.ok) { ws.close(); return }
        this.authenticated = true; this.wsState = 'open'; this.retry = 0
        const groups = [...this.groupRefs.keys()]
        if (groups.length) this.send({op:'sub',groups})
        this.startHeartbeat()
        this.broadcast({type:'OPEN',at:Date.now()}); this.broadcastStat()
        return
      }
      if (!this.authenticated) return
      if (m.op === 'batch' && Array.isArray(m.items)) this.onGatewayBatch(m.group, m.items)
      // pong 只用来刷新 lastRx,没别的事
    }
    ws.onerror = () => {
      // 浏览器出于安全不给 error 细节,统一在 onclose 里处理;这里只保证 close 一定触发
      if (this.ws === ws) ws.close()
    }
    ws.onclose = (ev) => {
      if (this.ws !== ws) return
      this.ws = null
      this.authenticated = false
      this.wsState = 'closed'
      this.stopHeartbeat()
      const retryInMs = this.closed || this.ports.size === 0 ? 0 : this.scheduleReconnect()
      this.broadcast({ type: 'CLOSE', at: Date.now(), code: ev.code, reason: ev.reason, retryInMs })
      this.broadcastStat()
    }
  }

  /** 返回下一次重连的等待毫秒数 */
  private scheduleReconnect(): number {
    if (this.closed || this.reconnectTimer) return 0
    const wait = BACKOFF_MS[Math.min(this.retry, BACKOFF_MS.length - 1)]
    this.retry++
    this.reconnects++
    // 退避而不是固定 3s 狂连:后端没起(另一个 agent 并行开发中)时,这里每 15s 才试一次,控制台不会被刷屏
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.connect() }, wait)
    return wait
  }

  private teardownSocket() {
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    this.stopHeartbeat()
    this.authenticated = false
    const ws = this.ws
    this.ws = null
    this.wsState = 'idle'
    if (ws) {
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null
      try { ws.close() } catch { /* 已关 */ }
    }
  }

  private send(m: GatewayOutMsg) {
    // 没连上时不用排队:onopen 会按 groupRefs 全量补 sub,unsub 的组既然已经不在 groupRefs 里自然也不会补
    if (this.ws && this.ws.readyState === WebSocket.OPEN && (m.op === 'auth' || this.authenticated)) {
      try { this.ws.send(JSON.stringify(m)) } catch { /* 发送失败等 onclose 走重连 */ }
    }
  }

  private startHeartbeat() {
    this.stopHeartbeat()
    this.heartbeatTimer = setInterval(() => {
      if (!this.ws) return
      if (Date.now() - this.lastRx > HEARTBEAT_MS * 2) {
        // 半开连接:TCP 没断但对端已死,不主动踢的话 onclose 永远不来
        this.ws.close()
        return
      }
      this.send({ op: 'ping' })
      this.broadcastStat()
    }, HEARTBEAT_MS)
  }

  private stopHeartbeat() {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null }
  }

  /* ------------------------------------------------------------------ */
  /*                     静态信息缓存(契约 §2.3)                          */
  /* ------------------------------------------------------------------ */

  private ensureStatic() {
    if (this.staticState !== 'idle') return
    this.loadStaticAll()
  }

  private async loadStaticAll() {
    const request = this.staticGeneration
    const versions = new Map(this.staticVersions)
    this.staticState = 'loading'
    try {
      const r = await fetch(`${this.staticBase}/all`, {headers:this.authHeaders()})
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const body = await r.json() as { ok?: boolean; data?: unknown }
      if (!body || body.ok !== true || !Array.isArray(body.data)) throw new Error('bad shape')
      if (request !== this.staticGeneration || this.closed || !this.ports.size) return
      for (const raw of body.data) {
        const code = (raw as BondStatic)?.bond_uni_code
        if (this.staticVersions.get(code) === versions.get(code)) this.putStatic(raw)
      }
      this.staticState = 'ready'
      this.staticRetry = 0
      this.flushStaticWaiters()
    } catch (e) {
      if (request !== this.staticGeneration || this.closed || !this.ports.size) return
      this.staticState = 'failed'
      if (!this.staticWarned) {
        this.staticWarned = true
        console.warn('[quote] 静态表 /api/bond-static/all 拉取失败,将退避重试:', String(e))
      }
      // 首轮失败也要放行等待者:让主线程拿到空 Map 先渲染,别把页面卡死在"等静态表"
      this.flushStaticWaiters()
      const wait = STATIC_RETRY_MS[Math.min(this.staticRetry++, STATIC_RETRY_MS.length - 1)]
      this.staticTimer = setTimeout(() => { this.staticTimer = null; if (!this.closed) void this.loadStaticAll() }, wait)
    }
  }

  private putStatic(raw: unknown) {
    if (!raw || typeof raw !== 'object') return
    const r = raw as Record<string, unknown>
    const code = r.bond_uni_code
    if (typeof code !== 'string' || !code) return
    const row = { ...r } as unknown as BondStatic
    // 库里 tags 是 JSON 字符串,统一在这里 parse 一次,主线程拿到就是数组
    if (typeof r.tags === 'string') {
      try { row.tags = JSON.parse(r.tags) as string[] } catch { row.tags = [] }
    }
    this.staticMap.set(code, row)
  }

  private flushStaticWaiters() {
    const ws = this.staticWaiters
    this.staticWaiters = []
    for (const w of ws) w()
  }

  private cacheRequest(st: PortState, reqId: number, codes?: string[]) {
    const respond = () => this.respondCache(st, reqId, codes)
    if (this.staticState === 'idle' || this.staticState === 'loading') this.staticWaiters.push(respond)
    else void respond()
  }

  private authHeaders() { return { Authorization:`Bearer ${this.authToken}`, 'X-Auth-Token':this.authToken } }

  private async respondCache(st: PortState, reqId: number, codes?: string[]) {
    const request = this.staticGeneration
    let data: Map<string, BondStatic>
    if (!codes) {
      // 整表:拷一份而不是直接发 staticMap —— 主线程兜底模式下没有结构化克隆,直接发等于把内部缓存暴露出去被改
      data = new Map(this.staticMap)
    } else {
      data = new Map()
      const missing: string[] = []
      for (const c of codes) {
        const row = this.staticMap.get(c)
        if (row) data.set(c, row); else missing.push(c)
      }
      // 缓存里没有的(新上市的券)按需补拉一次;失败就只回已有的
      if (missing.length && this.staticState === 'ready') {
        try {
          const r = await fetch(`${this.staticBase}?codes=${encodeURIComponent(missing.join(','))}`, {headers:this.authHeaders()})
          const body = await r.json() as { ok?: boolean; data?: unknown }
          if (request !== this.staticGeneration || this.closed || !this.ports.has(st.id)) return
          if (r.ok && body?.ok === true && Array.isArray(body.data)) {
            for (const raw of body.data) {
              if (!this.staticVersions.has((raw as BondStatic)?.bond_uni_code)) this.putStatic(raw)
            }
            for (const c of missing) { const row = this.staticMap.get(c); if (row) data.set(c, row) }
          }
        } catch { /* 网络抖动,回已有的 */ }
      }
    }
    if (!this.ports.has(st.id)) return
    st.port.postMessage({ type: 'CACHE_RESPONSE', reqId, data, complete: this.staticState === 'ready' })
  }

  /* ------------------------------------------------------------------ */
  /*                              埋点                                  */
  /* ------------------------------------------------------------------ */

  snapshot(): WorkerStat {
    return {
      at: Date.now(),
      wsState: this.wsState,
      reconnects: this.reconnects,
      rxBatches: this.rxBatches,
      rxItems: this.rxItems,
      staleItems: this.staleItems,
      groups: [...this.groupRefs.keys()],
      ports: this.ports.size,
      staticState: this.staticState,
      staticSize: this.staticMap.size,
    }
  }

  private broadcastStat() {
    this.broadcast({ type: 'STAT', stat: this.snapshot() })
  }

  private broadcast(msg: WorkerToMainMsg) {
    for (const st of this.ports.values()) {
      try { st.port.postMessage(msg) } catch { /* 端口已死,等 CLOSE / close 事件清理 */ }
    }
  }
}

/** worker 里 `location` 是 worker 脚本的 URL,同源,所以 host 就是页面的 host */
function defaultWsUrl(): string {
  const loc = (globalThis as { location?: { protocol: string; host: string } }).location
  if (!loc) return 'ws://127.0.0.1:8120/ws/quote'
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${loc.host}/ws/quote`
}
