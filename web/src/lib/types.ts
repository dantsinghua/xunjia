/** 工作台业务DTO与未保存表单的建议初始值。这里不生成行情或业务结果。 */
export type BondStatus = '待询价' | '询价中' | '待回执' | '发送失败' | '转人工' | '已报价' | '还价中' | '待人工确认' | '已成交' | '已暂停' | '未成交' | '已撤价' | '已放弃' | '待核对' | '停止中'

export interface BondRow {
  bondUniCode?: string   // 行身份(契约 §1.2);推送 delta.u 对的是它
  quotedAt?: number
  flowId?: number        // 行版本
  i: number
  name: string
  code: string
  term: string
  val: string // "估值 / 净价"
  dur: string
  coupon: string
  ibv: string; ib: string; io: string; iov: string // 内部意向:bid量/bid底价/ofr底价/ofr量
  obs: string; obv: string; ob: string; oo: string; oov: string; oos: string // 外部:最优bid方/bid量/最优bid/最优ofr/ofr量/最优ofr方
  status: BondStatus
  tags: string[]
  isNew: boolean
  issuer: string; issuerRating: string; implied: string; industry: string; prov: string; nature: string
  gLimit: string; ngLimit: string; zz: string
  buyMgr: string; sellMgr: string
  valDate: string // 估值日(中债日终发布,盘中看前一日),悬浮卡「中债估值 09-14」的后缀
  bondType?: string  // 券种。类型筛选能走订阅组,行上有值时再收一次
  firmness?: string  // 报价类型:实盘 / 可部分执行 / 意向价
  clearing?: string  // 清算:+0 / +1 / +N
  scheme?: string    // 方案组。没有对应订阅组,只在已加载行上比
}

export interface ChannelBest {
  label: string
  bid: string; ofr: string; bidVol: string; ofrVol: string; bidSide: string; ofrSide: string
}
export interface TradeRow { t: string; ch: string; dir: string; p: string; v: string }
export interface SeriesLine { max: string; min: string; d0?: string; d1?: string; d2?: string; [k: string]: unknown }
export interface ModalCp { id: string; name: string; stage: string; spread: string; updated: string }
export interface BondDetail {
  bond: BondRow
  channels: ChannelBest[]
  trades: TradeRow[]
  hist: { avg: string; zz: string; max: string; min: string; d0: string; d1: string; d2: string }
  intra: { bid: string; ofr: string; zz: string; max: string; min: string }
  modalCps: ModalCp[]
}

export interface TradeBoard {
  own_side:string; peer_quote:string; own_limit:string; gap_bp:string; market_bid:string; market_ofr:string; quote_at:string; freshness:string; phase:string
}
export interface CpBond {
  sessionId?: string
  groupId?: string
  revision?: number
  groupRevision?: number
  analysisPending?: boolean
  sendAllowed?: boolean
  groupSendAllowed?: boolean
  pendingCommandId?: string
  commandState?: string
  responseState?: string
  tradeDay?: string
  needsReview?: boolean
  quotes?: {bid: NegotiationQuote; ofr: NegotiationQuote}
  summary?: GroupSummary
  trade?: TradeBoard
  name: string; code?: string; quote: BondStatus; stage: string; step: number
  spread: string; updated: string; ours: string; theirs: string; room: string; used: number | null
}
export interface Counterparty {
  id: string; name: string; org: string; username?: string
  threadId?: string
  hist: string[]; avgSpread: string; avgReply: string; winRate: string; pref: string; termPref: string
  bonds: CpBond[]
}
export type ChatRole = 'bot' | 'cp' | 'peer' | 'sys' | 'human' | 'self' | 'unknown'
export interface NegotiationQuote {
  side: 'bid' | 'ofr'
  price: {kind: string; value: string; computed_yield?: string | null} | null
  volume_wan: string | null
  assertion: string
  validity: string
  finality: string
  confirmed_day: string | null
  revision: number
  evidence: Record<string, string[]>
}
export interface GroupSummary {
  reported_volume_wan: string
  price_eligible_volume_wan: string
  final_confirmed_volume_wan: string
  target_volume_wan: string
  final_shortfall_wan: string
  goal_ready: boolean
  whole_lots: {member_id:string; quote_revision:number; volume_wan:string}[]
}
export interface ChatMsg { id?: string; revision?: number; ingestRevision?:number; analysisState?:string; occurredAt?:number; receivedAt?:number; kind?:string; targetId?:string; cmd_id?: string; trace?: TraceView; role: ChatRole; text: string; tag: string; time: string; bond: string; contexts?: { code: string; name: string; memberId?:string; relation?:string }[]; bond_state?: string; bond_name?: string }

export interface DetailRow {
  date: string; name: string; code: string; term: string; dir: string
  val: string; theirs: string; theirVol: string; ours: string; ourVol: string
  spread: string; trader: string; pm: string; st: string
}

export interface Strategy {
  templateId?: string
  configured?: boolean // false表示本地未保存草稿，不构成询价授权
  startDev: number   // 起始偏离 BP
  boundary: number   // 探价边界 BP
  step: number       // 步幅 BP
  strict: boolean    // 严格匹配报量
  roundLot: number   // S1 询价量取整档位(万)
  crossDay: number   // S2 跨日阈值(BP)
}
export const STRATEGY_DEFAULT: Strategy = { templateId:'personal', configured:false, startDev: 10, boundary: 5, step: 2, strict: true, roundLot: 1000, crossDay: 3 }

export interface ChartSpec {
  type: 'bar' | 'line' | 'scatter'
  title?: string
  categories?: string[]
  series?: { name: string; color?: string; data: number[] }[]
  points?: { x: number; y: number; label?: string }[]
  xLabel?: string; yLabel?: string
}
export interface QaResult { answer: string; chart?: ChartSpec }

export interface TraceStep { title: string; desc: string; chips: { k: string; v: string }[]; cost: string; reached?: boolean }
export interface TraceView { id: string; text: string; status: string; summary: string; dirLabel: string; steps: TraceStep[]; meta: { k: string; v: string }[] }
