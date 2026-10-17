/**
 * 实时行情管线 · 类型定义(唯一口径:docs/09-实时行情管线契约.md)
 *
 * 本文件只放类型,零运行时代码 —— 它同时被主线程 bundle 和 worker bundle 引用,
 * 任何运行时副作用(比如碰 document)都会让 worker 端直接崩掉。
 */

/* ============================ §1.1 静态主数据 ============================ */

/** `bond_static` 表的一行。只读,worker 启动时整表拉一次缓存在内存 Map 里。 */
export interface BondStatic {
  bond_uni_code: string          // 关联键,推送里只带这个
  windcode: string
  sec_name: string
  bond_type?: string | null
  residual_maturity?: string | null
  residual_days?: number | null
  coupon?: number | null
  bond_rating?: string | null
  issuer_rating?: string | null
  implied_rating?: string | null
  issuer?: string | null
  nature?: string | null
  industry?: string | null
  prov?: string | null
  is_public?: number | null
  is_guaranteed?: number | null
  is_subordinated?: number | null
  is_perpetual?: number | null
  is_udic?: number | null
  embedded_option?: string | null
  /** 库里是 JSON 字符串,worker 收到后统一 parse 成数组 */
  tags?: string[] | null
  val_yield?: number | null
  net_price?: number | null
  mod_dur?: number | null
  val_date?: string | null
}

/* ============================ §2.2 推送增量 ============================ */

/**
 * `QuoteDelta` —— 只有动态字段。
 * 行身份 = `u`(bond_uni_code),行版本 = `f`(flow_id,全局单调递增)。
 * 字段名缩写是为省传输:几十万条/天,每条省 ~60 字节。
 */
export interface QuoteDelta {
  s?: BondStatic  // 同一flow的完整公共静态白名单，用于刷新主数据与筛选归属
  f: number      // flow_id  ← 行版本
  u: string      // bond_uni_code ← 行身份
  t: number      // issue_time(服务端产生时间 ms)
  c?: number     // QB 源:触发该版本的数据到达本机(入库)的时刻 ms,用于端到端延迟埋点
  bb?: number | null; bbv?: number | null; bbs?: string | null   // best bid / vol / side
  bo?: number | null; bov?: number | null; bos?: string | null   // best ofr / vol / side
  fm?: string | null; cl?: string | null; st?: string | null     // firmness / clearing / status
  ib?: number | null; ibv?: number | null; io?: number | null; iov?: number | null
}

/* ============================ §2.2 网关 ↔ worker ============================ */

export type GatewayOutMsg =
  | { op: 'auth'; token: string }
  | { op: 'sub'; groups: string[] }
  | { op: 'unsub'; groups: string[] }
  | { op: 'ping' }

export type GatewayInMsg =
  | { op: 'auth'; ok: boolean }
  | { op: 'batch'; group: string; sendTime: number; items: QuoteDelta[] }
  | { op: 'pong' }

/* ============================ §2.1 主线程 ↔ worker ============================ */

export type WorkerMsgType =
  | 'INIT_CONNECTIONS'    // main→worker 首次建连,带 identifiers
  | 'JOIN_GROUP'          // main→worker 订阅组(差集)
  | 'LEAVE_GROUP'         // main→worker 退订(引用计数归零才发)
  | 'PULL'                // main→worker 每 500ms 拉一次,带 requestTime
  | 'BATCH'               // worker→main 窗口内聚合结果
  | 'CACHE_REQUEST'       // main→worker 要静态信息
  | 'CACHE_RESPONSE'
  | 'STAT'                // worker→main 埋点
  | 'OPEN' | 'CLOSE' | 'ERROR'

/** INIT_CONNECTIONS 携带的标识。SharedWorker 下第一个到达的 INIT 决定连接参数,后续标签只登记端口。 */
export interface ConnectionIdentifiers {
  authToken: string
  userScope: string
  /** 标签页/客户端实例 id,仅用于日志与埋点归因 */
  clientId: string
  /** 覆盖默认的 `wss?://<host>/ws/quote`;不传则 worker 按自身 origin 推导 */
  wsUrl?: string
  /** 覆盖默认的 `/api/bond-static` 前缀 */
  staticBase?: string
}

export type MainToWorkerMsg =
  | { type: 'INIT_CONNECTIONS'; identifiers: ConnectionIdentifiers }
  | { type: 'JOIN_GROUP'; groups: string[] }
  | { type: 'LEAVE_GROUP'; groups: string[] }
  | { type: 'PULL'; requestTime: number }
  | { type: 'CACHE_REQUEST'; reqId: number; codes?: string[] }
  /**
   * main→worker:本端口即将离开(pagehide / dispose)。
   * SharedWorker 没有可靠的"对端端口已关闭"事件(Chrome 122+ 才有 MessagePort close 事件),
   * 所以主线程主动告知,worker 据此释放该端口的全部组引用 —— 否则引用计数永远归不了零,网关侧永远 unsub 不掉。
   */
  | { type: 'CLOSE' }

/** worker 侧运行态快照,随 STAT 下发,供 stats() / 调试面板读 */
export interface WorkerStat {
  at: number
  wsState: 'idle' | 'connecting' | 'open' | 'closed'
  reconnects: number
  /** 收到的网关 batch 条数 / 累计 item 数 / 因版本号不新而忽略的 item 数 */
  rxBatches: number
  rxItems: number
  staleItems: number
  /** 当前向网关订阅着的组(引用计数 > 0) */
  groups: string[]
  /** 连着的端口(标签页)数;专用 Worker / 主线程模式恒为 1 */
  ports: number
  staticState: 'idle' | 'loading' | 'ready' | 'failed'
  staticSize: number
}

export type WorkerToMainMsg =
  /** PULL 响应体 —— 照 DM,按组聚合成 Map */
  | {
      type: 'BATCH'
      requestTime: number          // 回传 main 发起时刻,用于算 worker→main 延迟
      workerSendTime: number
      notification: Map<string, QuoteDelta[]>   // groupName -> 该窗口内的增量
      dropped: number              // 本窗口因背压丢弃的条数
    }
  | { type: 'CACHE_RESPONSE'; reqId: number; data: Map<string, BondStatic>; complete: boolean }
  | { type: 'STAT'; stat: WorkerStat }
  | { type: 'OPEN'; at: number }
  | { type: 'CLOSE'; at: number; code?: number; reason?: string; retryInMs: number }
  | { type: 'ERROR'; at: number; message: string }

/* ============================ 主线程对外 API ============================ */

/** 三级降级里当前实际在用的传输层 */
export type QuoteTransport = 'shared-worker' | 'worker' | 'main-thread'

export interface QuoteStats {
  /** `BATCH.workerSendTime - requestTime`,最近一次(契约 §6,阈值 2s) */
  latencyMs: number
  /** 最近一次批量条数 `Σ notification.values().length` */
  batchSize: number
  /** 最近一次分发耗时:调用所有订阅者 onBatch(含其内部同步 upsert+render)前后的 performance.now() 差 */
  dispatchMs: number
  /** 背压丢弃累计(>0 即告警) */
  dropped: number
  transport: QuoteTransport
  /** 超阈值告警计数,按指标分 */
  warnings: { latency: number; dispatch: number; dropped: number }
  /** 网关连接是否打开(由 worker 的 OPEN/CLOSE 事件维护) */
  connected: boolean
  /** worker 侧最近一次 STAT 快照,未收到时为 null */
  worker: WorkerStat | null
}
