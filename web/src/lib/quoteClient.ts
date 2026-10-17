/**
 * 实时行情管线 · 主线程侧统一 API(业务代码只认这个文件)
 *
 * 职责(契约 §2.1 / §4.3 / §6 / §7):
 *   1. 三级降级选传输层:SharedWorker → 专用 Worker → 主线程直连,对外 API 完全一致
 *   2. 每 500ms 向 worker 发一次 PULL;document.hidden 时停,恢复可见时立即拉一次
 *   3. 组订阅的标签内引用计数(多个组件订同一组只向 worker 发一次 JOIN)
 *   4. 埋点:worker→main 延迟、批量条数、分发耗时、dropped;超阈值 console.warn 并计数
 *
 * 不做的事:不拼行、不渲染。主线程拿到 Map<group, QuoteDelta[]> 后自己 upsert(契约 §5)。
 *
 * 主线程、专用及共享Worker使用相同已认证连接与批量分发协议。
 */
import { QuoteCore } from './quoteCore'
import type {
  BondStatic, QuoteDelta, MainToWorkerMsg, WorkerToMainMsg, WorkerStat, QuoteStats, QuoteTransport,
} from './quoteTypes'

export type { BondStatic, QuoteDelta, QuoteStats, QuoteTransport } from './quoteTypes'

export interface QuoteClient {
  /**
   * 订阅若干组。onBatch 每个 pull 周期最多被调一次,只带本订阅涉及的组;窗口内没数据不调。
   * 返回退订函数。最后一个订阅者退订时会强制 flush 一次(见 unsubscribe 的注释)。
   */
  subscribe(groups: string[], onBatch: (b: Map<string, QuoteDelta[]>) => void): () => void
  /** 取静态信息。不传 codes 返回 worker 缓存的整表;传了则只回这些券(缓存缺的会按需补拉)。 */
  getStatic(codes?: string[]): Promise<Map<string, BondStatic>>
  stats(): QuoteStats
  dispose(): void
}

export interface QuoteClientOptions {
  authToken?: string
  userScope?: string
  /** pull 周期,契约定 500ms;测试时可调 */
  pullIntervalMs?: number
  /** 覆盖 worker 推导的 wss?://<host>/ws/quote */
  wsUrl?: string
  /** 覆盖 /api/bond-static */
  staticBase?: string
  /**
   * 降级顺序。默认 ['shared-worker','worker','main-thread'];
   * 想强制走某一级(比如排查 SharedWorker 特有问题)传 ['main-thread'] 即可。
   */
  transports?: QuoteTransport[]
  /** 一级传输层从构造到收到第一条消息的最长等待,超时视为该级不可用 */
  bootTimeoutMs?: number
}

/* ============================ 常量 ============================ */

const DEFAULT_PULL_MS = 500
const DEFAULT_BOOT_TIMEOUT_MS = 8000
/** PULL 发出去这么久没回 BATCH,视为 worker 卡死,放开 inflight 门闩允许下一次 pull */
const PULL_STALL_MS = 5000
/** 契约 §6 阈值 */
const WARN_LATENCY_MS = 2000
const WARN_DISPATCH_MS = 1000
/** 同一指标的 console.warn 最多 5s 一条,行情高峰期不能让告警本身变成负担 */
const WARN_THROTTLE_MS = 5000
const DEFAULT_ORDER: QuoteTransport[] = ['shared-worker', 'worker', 'main-thread']

/* ============================ 传输层抽象 ============================ */

interface Transport {
  kind: QuoteTransport
  post(m: MainToWorkerMsg): void
  dispose(): void
}

interface Subscriber {
  groups: Set<string>
  onBatch: (b: Map<string, QuoteDelta[]>) => void
}

interface PendingCache {
  codes?: string[]
  resolve: (m: Map<string, BondStatic>) => void
}

/* ============================ 实现 ============================ */

class QuoteClientImpl implements QuoteClient {
  private readonly pullMs: number
  private readonly bootTimeoutMs: number
  private readonly order: QuoteTransport[]
  private readonly wsUrl?: string
  private readonly staticBase?: string
  private readonly authToken: string
  private readonly userScope: string
  private readonly clientId = makeClientId()

  private transport: Transport | null = null
  private transportReady = false
  private booting = false
  private disposed = false

  private subs = new Set<Subscriber>()
  /** 标签内组引用计数:多个组件订同一组,只向 worker JOIN 一次;全部退了才 LEAVE */
  private groupRefs = new Map<string, number>()

  private timer: ReturnType<typeof setTimeout> | null = null
  /** 在途 PULL 的发起时刻;null 表示没有在途。用来防止 worker 慢时 PULL 堆积。 */
  private inflightAt: number | null = null

  private cacheSeq = 0
  private cacheReqs = new Map<number, PendingCache>()

  private stat: QuoteStats = {
    latencyMs: 0, batchSize: 0, dispatchMs: 0, dropped: 0,
    transport: 'main-thread',
    warnings: { latency: 0, dispatch: 0, dropped: 0 },
    connected: false,
    worker: null,
  }
  private lastWarnAt: Record<string, number> = {}

  constructor(opts: QuoteClientOptions) {
    this.authToken = opts.authToken || ''
    this.userScope = opts.userScope || ''
    this.pullMs = opts.pullIntervalMs ?? DEFAULT_PULL_MS
    this.bootTimeoutMs = opts.bootTimeoutMs ?? DEFAULT_BOOT_TIMEOUT_MS
    this.order = opts.transports?.length ? [...opts.transports] : DEFAULT_ORDER
    this.wsUrl = opts.wsUrl
    this.staticBase = opts.staticBase
    document.addEventListener('visibilitychange', this.onVisibility)
    window.addEventListener('pagehide', this.onPageHide)
    void this.boot(0)
  }

  /* ---------------------------- 对外 API ---------------------------- */

  subscribe(groups: string[], onBatch: (b: Map<string, QuoteDelta[]>) => void): () => void {
    if (this.disposed) return () => {}
    const sub: Subscriber = { groups: new Set(groups), onBatch }
    this.subs.add(sub)
    const fresh: string[] = []
    for (const g of sub.groups) {
      const n = (this.groupRefs.get(g) ?? 0) + 1
      this.groupRefs.set(g, n)
      if (n === 1) fresh.push(g)
    }
    // 只发差集:worker 那边按端口去重,但少发一条是一条
    if (fresh.length && this.transportReady) this.post({ type: 'JOIN_GROUP', groups: fresh })
    this.schedule()
    let done = false
    return () => { if (!done) { done = true; this.unsubscribe(sub) } }
  }

  private unsubscribe(sub: Subscriber) {
    if (!this.subs.delete(sub)) return
    const gone: string[] = []
    for (const g of sub.groups) {
      const n = (this.groupRefs.get(g) ?? 1) - 1
      if (n <= 0) { this.groupRefs.delete(g); gone.push(g) } else this.groupRefs.set(g, n)
    }
    if (this.subs.size === 0) {
      // 最后一个订阅者走了:强制 flush 一次再停循环。
      // flush 的意义不是把数据交给谁(已经没人要了),而是让 worker 把本端口的窗口和 dropped 计数结清回报,
      // 否则这段窗口里的背压丢弃就静默消失,stats().dropped 会少算;PULL 排在 LEAVE_GROUP 前面,消息顺序保证先结清再退订。
      this.pullNow(true)
      this.stop()
    }
    if (gone.length && this.transportReady) this.post({ type: 'LEAVE_GROUP', groups: gone })
  }

  getStatic(codes?: string[]): Promise<Map<string, BondStatic>> {
    if (this.disposed) return Promise.resolve(new Map())
    return new Promise((resolve) => {
      const reqId = ++this.cacheSeq
      this.cacheReqs.set(reqId, { codes, resolve })
      // 传输层还没就绪就先记着,ready 时统一补发
      if (this.transportReady) this.post({ type: 'CACHE_REQUEST', reqId, codes })
    })
  }

  stats(): QuoteStats {
    return { ...this.stat, warnings: { ...this.stat.warnings } }
  }

  dispose() {
    if (this.disposed) return
    this.disposed = true
    this.stop()
    document.removeEventListener('visibilitychange', this.onVisibility)
    window.removeEventListener('pagehide', this.onPageHide)
    this.releasePortRefs()
    this.transport?.dispose()
    this.transport = null
    this.transportReady = false
    this.subs.clear()
    this.groupRefs.clear()
    for (const r of this.cacheReqs.values()) r.resolve(new Map())
    this.cacheReqs.clear()
  }

  /* ---------------------------- pull 循环(契约 §4.3) ---------------------------- */

  private active(): boolean {
    return !this.disposed && this.transportReady && this.subs.size > 0 && !document.hidden
  }

  /**
   * 递归 setTimeout 而不是 setInterval:
   * setInterval 在主线程被长任务(比如一次大渲染)卡住时会把错过的 tick 攒起来连发,
   * 正好在最忙的时候多拉几次火上浇油;setTimeout 是"上一轮做完再等 500ms",天然自适应。
   */
  private schedule() {
    if (this.timer || !this.active()) return
    this.timer = setTimeout(() => {
      this.timer = null
      if (!this.active()) return
      this.pullNow()
      this.schedule()
    }, this.pullMs)
  }

  private stop() {
    if (this.timer) { clearTimeout(this.timer); this.timer = null }
  }

  /** @param force 忽略 hidden 判断(退订 flush 用);inflight 门闩仍然生效 */
  private pullNow(force = false) {
    if (!this.transportReady || this.disposed) return
    if (!force && document.hidden) return
    const now = Date.now()
    if (this.inflightAt != null) {
      if (now - this.inflightAt < PULL_STALL_MS) return // 上一次还没回,不堆积
      this.warn('stall', `[quote] PULL ${now - this.inflightAt}ms 未响应,worker 可能卡死`)
      this.inflightAt = null
    }
    this.inflightAt = now
    // requestTime 用 Date.now() 而不是 performance.now():后者在 worker 里是另一个时间原点,跨线程没法相减
    this.post({ type: 'PULL', requestTime: now })
  }

  private readonly onVisibility = () => {
    if (document.hidden) {
      // 后台标签零消耗的关键:不拉就不渲染;worker 那边只是内存里多攒几条,还有背压兜底
      this.stop()
    } else if (this.active()) {
      // 切回来先立刻拉一次把攒下的窗口一次吃掉,再回到 500ms 节奏
      this.pullNow()
      this.schedule()
    }
  }

  private readonly onPageHide = (e: PageTransitionEvent) => {
    // 页面真的要卸载(非 bfcache):主动告诉 worker 释放本端口引用。
    // SharedWorker 没有可靠的对端关闭事件,不发 CLOSE 的话共享的引用计数永远归不了零,网关那边这组就一直订着。
    if (e.persisted) { this.stop(); return }
    this.releasePortRefs()
  }

  private releasePortRefs() {
    if (!this.transportReady) return
    const groups = [...this.groupRefs.keys()]
    if (groups.length) this.post({ type: 'LEAVE_GROUP', groups })
    this.post({ type: 'CLOSE' })
  }

  /* ---------------------------- worker → main ---------------------------- */

  private onMessage(m: WorkerToMainMsg) {
    if (this.disposed) return
    switch (m.type) {
      case 'BATCH': this.onBatch(m); break
      case 'CACHE_RESPONSE': {
        const req = this.cacheReqs.get(m.reqId)
        if (req) { this.cacheReqs.delete(m.reqId); req.resolve(m.data) }
        break
      }
      case 'STAT': this.stat.worker = m.stat; this.stat.connected = m.stat.wsState === 'open'; break
      case 'OPEN': this.stat.connected = true; break
      case 'CLOSE': this.stat.connected = false; break
      case 'ERROR': this.warn('ws', `[quote] 网关连接错误: ${m.message}`); break
    }
  }

  /** 埋点全在这里(契约 §6):延迟 / 条数 / 分发耗时 / dropped */
  private onBatch(m: Extract<WorkerToMainMsg, { type: 'BATCH' }>) {
    this.inflightAt = null
    const latency = m.workerSendTime - m.requestTime
    let size = 0
    for (const arr of m.notification.values()) size += arr.length

    this.stat.latencyMs = latency
    this.stat.batchSize = size
    if (m.dropped > 0) {
      this.stat.dropped += m.dropped
      this.stat.warnings.dropped++
      this.warn('dropped', `[quote] 背压丢弃 ${m.dropped} 条(累计 ${this.stat.dropped}),主线程消费跟不上或标签长时间后台`)
    }
    if (latency > WARN_LATENCY_MS) {
      this.stat.warnings.latency++
      this.warn('latency', `[quote] worker→main 延迟 ${latency}ms(阈值 ${WARN_LATENCY_MS}ms),主线程被长任务卡住`)
    }

    // 分发耗时把订阅者回调整个包进去:契约要求的"upsert+render 前后"就是这段 —— 前提是订阅者在回调里同步做 upsert
    const t0 = performance.now()
    if (size > 0) this.dispatch(m.notification)
    const dispatchMs = performance.now() - t0
    this.stat.dispatchMs = dispatchMs
    if (dispatchMs > WARN_DISPATCH_MS) {
      this.stat.warnings.dispatch++
      this.warn('dispatch', `[quote] 分发耗时 ${dispatchMs.toFixed(0)}ms(阈值 ${WARN_DISPATCH_MS}ms,本批 ${size} 条),渲染层要减负`)
    }
  }

  private dispatch(notification: Map<string, QuoteDelta[]>) {
    for (const sub of this.subs) {
      // 只给订阅者它订的组;多数场景一个订阅者订全部组,这时直接复用同一个 Map 省一次拷贝
      let view: Map<string, QuoteDelta[]> | null = null
      let all = true
      for (const g of notification.keys()) {
        if (!sub.groups.has(g)) { all = false; break }
      }
      if (all) view = notification
      else {
        view = new Map()
        for (const [g, arr] of notification) if (sub.groups.has(g)) view.set(g, arr)
        if (view.size === 0) continue
      }
      try { sub.onBatch(view) } catch (e) {
        // 一个订阅者抛错不能连累其他订阅者,也不能把 pull 循环搞停
        this.warn('subscriber', `[quote] 订阅者 onBatch 抛错: ${String(e)}`)
      }
    }
  }

  private warn(key: string, msg: string) {
    const now = Date.now()
    if (now - (this.lastWarnAt[key] ?? 0) < WARN_THROTTLE_MS) return
    this.lastWarnAt[key] = now
    console.warn(msg)
  }

  /* ---------------------------- 三级降级(契约 §7) ---------------------------- */

  /**
   * 从 order[from] 开始逐级尝试,直到某一级"构造成功且在超时内收到第一条消息"。
   * 判定标准为什么是"收到第一条消息"而不是"构造没抛异常":
   *   SharedWorker 构造在很多不支持的场景下并不抛(比如 Firefox 老版本不支持 module 型 SharedWorker,
   *   构造成功但脚本加载失败只在 onerror 里报;某些国产化浏览器有 SharedWorker 构造函数但实现是空壳),
   *   只有 worker 真跑起来并回话才算数。QuoteCore 收到 INIT 后立即回 STAT,就是这个握手。
   */
  private async boot(from: number) {
    if (this.booting || this.disposed) return
    this.booting = true
    try {
      for (let i = from; i < this.order.length; i++) {
        const kind = this.order[i]
        try {
          const t = await this.open(kind, i)
          if (this.disposed) { t.dispose(); return }
          this.transport = t
          this.transportReady = true
          this.stat.transport = kind
          console.info(`[quote] 传输层: ${kind}`)
          this.afterReady()
          return
        } catch (e) {
          console.warn(`[quote] 传输层 ${kind} 不可用,降级: ${String(e)}`)
        }
      }
      console.error('[quote] 三级传输层全部不可用,行情不会更新')
    } finally {
      this.booting = false
    }
  }

  /** 传输层就绪后把主线程已有的状态同步过去:当前订阅的组、排队的静态请求、pull 循环 */
  private afterReady() {
    const groups = [...this.groupRefs.keys()]
    if (groups.length) this.post({ type: 'JOIN_GROUP', groups })
    for (const [reqId, req] of this.cacheReqs) this.post({ type: 'CACHE_REQUEST', reqId, codes: req.codes })
    this.inflightAt = null
    if (this.active()) { this.pullNow(); this.schedule() }
  }

  /** 传输层跑起来之后又挂了(worker 脚本运行时异常等):当前级视为不可用,直接降到下一级 */
  private onFatal(kind: QuoteTransport, index: number, err: unknown) {
    if (this.disposed || !this.transport || this.transport.kind !== kind) return
    console.warn(`[quote] 传输层 ${kind} 运行中出错,降级: ${String(err)}`)
    this.transport.dispose()
    this.transport = null
    this.transportReady = false
    this.stat.connected = false
    this.stop()
    void this.boot(index + 1)
  }

  private open(kind: QuoteTransport, index: number): Promise<Transport> {
    switch (kind) {
      case 'shared-worker': return this.openSharedWorker(index)
      case 'worker': return this.openWorker(index)
      case 'main-thread': return this.openMainThread()
    }
  }

  /**
   * 等第一条消息的通用握手。构造成功后发 INIT,worker 立即回 STAT。
   * settle 之前 onerror / 超时 → reject;settle 之后 onerror → onFatal 降级。
   */
  private handshake(kind: QuoteTransport, index: number, attach: (onMsg: (m: WorkerToMainMsg) => void, onErr: (e: unknown) => void) => Transport): Promise<Transport> {
    return new Promise<Transport>((resolve, reject) => {
      let settled = false
      let t: Transport | null = null
      const timer = setTimeout(() => {
        if (settled) return
        settled = true
        t?.dispose()
        reject(new Error(`${this.bootTimeoutMs}ms 内未收到 worker 握手`))
      }, this.bootTimeoutMs)
      const onMsg = (m: WorkerToMainMsg) => {
        if (!settled) {
          settled = true
          clearTimeout(timer)
          resolve(t!)
        }
        this.onMessage(m)
      }
      const onErr = (e: unknown) => {
        if (!settled) {
          settled = true
          clearTimeout(timer)
          t?.dispose()
          reject(e instanceof Error ? e : new Error(errorText(e)))
        } else {
          this.onFatal(kind, index, errorText(e))
        }
      }
      try {
        t = attach(onMsg, onErr)
      } catch (e) {
        settled = true
        clearTimeout(timer)
        reject(e)
        return
      }
      t.post({ type: 'INIT_CONNECTIONS', identifiers: { clientId: this.clientId, authToken:this.authToken, userScope:this.userScope, wsUrl: this.wsUrl, staticBase: this.staticBase } })
    })
  }

  /**
   * 第 1 级:SharedWorker,跨标签共享一条 WS。
   *
   * Vite 6 的坑,必须写成这个字面量形状:
   *   new SharedWorker(new URL('./quoteWorker.ts', import.meta.url), { type: 'module', ... })
   * Vite 靠静态分析识别 `new (Shared)Worker(new URL(字面量, import.meta.url), {字面量对象})`,
   * URL 提到变量里、或 options 不是对象字面量,worker 就不会被单独打包,运行时只会拿到一个普通资源地址。
   *
   * `type: 'module'` 在 dev 下是必需的(Vite dev 直接以 ESM 形式提供 worker 源码,里面有 import),
   * 但 module 型 SharedWorker 的浏览器支持比 module 型 Worker 更窄(Firefox 直到 2024 年底才支持,
   * Safari 16 才重新有 SharedWorker),不支持的浏览器要么构造即抛、要么静默 onerror ——
   * 两种都被 handshake 兜住并降到下一级。生产构建时 Vite 默认把 worker 打成自包含的 iife 单文件,
   * 以 module 方式加载一个没有 import 的脚本没有任何问题,所以 type:'module' 在生产也保留。
   */
  private openSharedWorker(index: number): Promise<Transport> {
    if (typeof SharedWorker !== 'function') return Promise.reject(new Error('浏览器没有 SharedWorker'))
    return this.handshake('shared-worker', index, (onMsg, onErr) => {
      const w = new SharedWorker(new URL('./quoteWorker.ts', import.meta.url), { type: 'module', name: `xunjia-quote:${this.userScope}` })
      w.onerror = (e) => onErr(e)
      w.port.onmessage = (ev: MessageEvent<WorkerToMainMsg>) => onMsg(ev.data)
      w.port.start()
      return {
        kind: 'shared-worker',
        post: (m) => w.port.postMessage(m),
        // 只能关自己这一端的端口;SharedWorker 本体由浏览器在最后一个端口消失后回收,别的标签还在用
        dispose: () => { w.port.onmessage = null; w.onerror = null; w.port.close() },
      }
    })
  }

  /** 第 2 级:专用 Worker,每标签一条 WS,但收包/解析/累积仍不占主线程 */
  private openWorker(index: number): Promise<Transport> {
    if (typeof Worker !== 'function') return Promise.reject(new Error('浏览器没有 Worker'))
    return this.handshake('worker', index, (onMsg, onErr) => {
      const w = new Worker(new URL('./quoteWorker.ts', import.meta.url), { type: 'module', name: `xunjia-quote:${this.userScope}` })
      w.onerror = (e) => onErr(e)
      w.onmessage = (ev: MessageEvent<WorkerToMainMsg>) => onMsg(ev.data)
      return {
        kind: 'worker',
        post: (m) => w.postMessage(m),
        dispose: () => { w.onmessage = null; w.onerror = null; w.terminate() },
      }
    })
  }

  /**
   * 第 3 级:主线程直连 WS。
   * 和 quoteStream 的本质区别:WS 消息不再直达业务回调,而是进同一个 QuoteCore 的累积窗口,
   * 主线程仍按 500ms pull —— 渲染频率上限、去重、背压、隐藏暂停全都保留,只是解析 JSON 这点开销回到了主线程。
   * "端口"是进程内的假端口:postMessage 用 queueMicrotask 异步派发,模拟真 worker 的消息语义(不会在调用栈内重入)。
   */
  private openMainThread(): Promise<Transport> {
    return this.handshake('main-thread', this.order.length, (onMsg) => {
      const core = new QuoteCore()
      const handle = core.connectPort({ postMessage: (m) => queueMicrotask(() => onMsg(m)) })
      return {
        kind: 'main-thread',
        post: (m) => queueMicrotask(() => handle.onMessage(m)),
        dispose: () => { handle.close(); core.dispose() },
      }
    })
  }

  private post(m: MainToWorkerMsg) {
    try { this.transport?.post(m) } catch (e) { this.warn('post', `[quote] postMessage 失败: ${String(e)}`) }
  }
}

/* ============================ 工厂 ============================ */

export function createQuoteClient(opts: QuoteClientOptions = {}): QuoteClient {
  return new QuoteClientImpl(opts)
}

/* ============================ 工具 ============================ */

function makeClientId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (c?.randomUUID) return c.randomUUID()
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

function errorText(e: unknown): string {
  if (e instanceof Error) return e.message
  if (e && typeof e === 'object') {
    const ev = e as { message?: unknown; filename?: unknown; lineno?: unknown }
    if (typeof ev.message === 'string' && ev.message) return `${ev.message}${ev.filename ? ` @ ${String(ev.filename)}:${String(ev.lineno)}` : ''}`
    // SharedWorker 加载失败时 ErrorEvent 往往是空的(浏览器出于安全不给细节)
    return '(worker error,浏览器未给出细节)'
  }
  return String(e)
}
