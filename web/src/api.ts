/** 现券询价谈价机器人 · 前端接口封装(正式服务 8120)。
 *  约定:后端一律 { ok, data } 形状(见 docs/02-frontend-contract.md)。
 *  后端字段口径与前端模型不同(如 cp_id / spread_bp / my_yield / value+unit),
 *  这里逐端做 normalize(照 bondtrade web 的 normalizeInstr 套路)。
 *  请求失败保留失败语义，不用演示数据填补正式交易视图。 */
import {
  STRATEGY_DEFAULT,
  type BondRow, type BondStatus, type BondDetail, type ChannelBest, type Counterparty,
  type CpBond, type ChatMsg, type ChatRole, type DetailRow, type Strategy, type QaResult,
  type TraceView, type ChartSpec, type NegotiationQuote, type GroupSummary,
} from './lib/types'

export type {
  BondRow, BondStatus, BondDetail, Counterparty, ChatMsg, DetailRow, Strategy, QaResult, TraceView, ChartSpec,
} from './lib/types'

import type { OrgStatus } from './lib/imLink'
import { authFetch, requestJson } from './lib/http'
import type { BondStatic } from './lib/quoteTypes'
import type { QuoteFilterState } from './lib/quoteFilter'
export interface UnknownCommand {cmd_id:string;peer_id:string;text:string;created_at:number;expected_result_hash:string;conflict:boolean}
export interface SessionResetStatus {
  reset_id: string
  scope: { workbench_id: string; account_uid: string }
  binding_version: number
  account_epoch: number
  state: 'requested' | 'waiting_native' | 'local_applied' | 'complete' | 'blocked'
  code?: string | null
  snapshot_hash?: string
  snapshot?: {
    account_epoch: number
    published_source_highwater: number
    snapshot_at: number
    unresolved_history: unknown[]
    peers: Record<string, {
      source_watermark: number
      receiver_fence_version: number
      receiver_fence_id: string
      min_control_version: number
    }>
  } | null
  created_at: number
  updated_at: number
}
export interface FilterSpec extends QuoteFilterState { channel: string; broker: string; groups: string[] }

type Dict = Record<string, unknown>
type Resp<T> = { ok: boolean; data: T; hint?: string; revision?:number }
/** 批量操作的选择口径:枚举 或 谓词,二选一 */
export type Selection =
  | { windcodes: string[] }
  | { selectAll: true; groups?: string[]; filter: FilterSpec; except?: string[] }
export interface RfqPolicy {
  strict_match?:boolean
  pricing_basis?: 'absolute' | 'bp'
  valuation_snapshot?: {snapshot_id:string; valuation_date:string|null; yield_pct:string; provider:string; source?:'innodealing'|'reference'}
  valuation_warnings?: string[]
  code: string; name: string; our_side: 'BUY' | 'SELL'; volume_wan: string
  opening_yield_pct: string; limit_yield_pct: string; step_bp: string
}
export interface CounterpartyCapacity {
  active: number; limit: number; available: number
  unit: 'distinct_counterparties'; paused_counts: boolean
}
export interface RfqPreview {
  batch_id: string; version: number; status: string
  items: (RfqPolicy & { peer_id: string; text: string })[]
  commands_preview: {peer_id: string; text: string}[]
}
export interface RfqSubmission {
  batch_id: string
  commands: {command_id: string; cmd_id?: string; status: 'queued' | 'pending' | 'unknown' | 'sent' | 'failed' | 'cancelled' | 'expired'; reason?:string; peer_id: string; text: string; session_ids?: string[]}[]
}
export interface BulkResult { count?: number; scope?: string; [k: string]: unknown }
export interface CollectorAccount {
  account_uid: string; account_nick: string; updated_at?: number | null; stale?: boolean
  capabilities?: string[]; contacts_path?: string; available?: boolean; state?: string
}
export interface CollectorWorkbench {
  workbench_id: string; name?: string; updated_at?: number | null; stale?: boolean; accounts: CollectorAccount[]
}
export interface CollectorSelection { context_id?: string; workbench_id: string; account_uid: string; version: number }
export interface CollectorCatalog {
  workbenches: CollectorWorkbench[]; selected: CollectorSelection | null; note?: string; refresh_error?: string; contract?: string; offline_seconds?: number
}
export interface CollectorContacts extends OrgStatus {
  source: string; workbench_id: string; account_uid: string; contacts: import('./lib/imLink').ImPerson[]
  org: import('./lib/imLink').ImOrg; updated_at?: number | null; stale?: boolean; source_path?: string
  friends_updated_at?: number | null; refresh_requested?: boolean
}
export interface CollectorSelectionResult {
  binding: CollectorSelection & { owner_user_id: string; transport: string }; contacts: CollectorContacts
  connected: boolean; note?: string; reset?: boolean; send_ready?: boolean
}
export interface NativeHistoryMedia {
  sha256?: string; name: string; size: number; mime: string; state: 'ready' | 'pending' | 'unavailable'; download_ready: boolean
}
export interface NativeHistoryMessage {
  id: number; native_id: string; peer_id: string; direction: 'in' | 'out'; occurred_at: number
  text: string; type: 'text' | 'image' | 'file'; session_kind: 'private' | 'group'; media: NativeHistoryMedia[]; historical: true
}
export interface NativeHistoryPage {
  workbench_id: string; account_uid: string; binding_version: number; items: NativeHistoryMessage[]
  has_more: boolean; next_cursor: number | null; retention_days: number
}
export interface TransferFile {
  id: number; name: string; extension: string; kind: string; lane: string
  source_created_at: number; received_at: number | null; state: string
  upload_state: string | null; receive_state: string | null
}
export interface TransferFilePage {
  items: TransferFile[]; next_before: number | null; workbench_id: string; account_uid: string
  scope: 'retained_authenticated_files'
}
export interface MqConfig {
  client_cert_file?: string; client_key_file?: string; ca_file?: string
  reset?: boolean
  configured: boolean; applied: boolean; backend: 'file_gateway'; base_url: string; credentials_set: boolean
  account_uid: string | null; workbench_id: string | null; revision: string; note: string
}
export interface MqConfigInput {
  client_cert_file?: string; client_key_file?: string; ca_file?: string
  base_url: string; expected_revision?: string
}
export interface MqConnectionTest {
  passed: boolean; connected: boolean; saved: false; steps: {name: string; ok: boolean; detail: string}[]
}

export type BondsResp = Resp<BondRow[]> & { total: number; hasMore: boolean; nextBefore: number; nextUni?: number; source?: string; emptyReason?: string | null }

function jget<T>(url: string): Promise<T> { return requestJson<T>(url) }
function jpost<T>(url: string, body: unknown): Promise<T> {
  return requestJson<T>(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) })
}
function emptySummary(): { botStatus: string; botEnabled?: boolean; stats: { k: string; v: string; d: string }[] } {
  return {
    botStatus: '未知',
    stats: [
      { k: '活跃对话', v: '0', d: '个' },
      { k: '今日询价', v: '0', d: '笔' },
      { k: '已获报价', v: '0', d: '' },
      { k: '平均回复', v: '0s', d: '' },
      { k: '转人工', v: '0', d: '' },
    ],
  }
}

/** 失败保留失败语义；空形状仅供调用方安全读取，不能当成服务端真值。 */
async function result<T>(call: () => Promise<Resp<T>>, empty: T): Promise<Resp<T>> {
  try {
    const r = await call()
    if (r && r.ok === true && r.data != null) return r
    return { ok: false, data: empty, hint: r?.hint || '服务没有返回有效数据' }
  } catch (e) {
    return { ok: false, data: empty, hint: e instanceof Error ? e.message : '请求失败' }
  }
}

/* ============================ normalize 工具 ============================ */
const s = (v: unknown, dash = '—'): string => (v == null || v === '' ? dash : String(v))
/** 筛选字段:空和占位符不当成已填,避免「—」被当成报价类型。 */
const optText = (v: unknown): string | undefined => {
  if (v == null || v === '') return undefined
  const t = String(v).trim()
  return t && t !== '—' ? t : undefined
}
const num2 = (v: unknown): string => (v == null ? '—' : String(v))
/** 收益率统一 4 位小数(与推送路径 applyDelta 的 toFixed(4) 一致,否则同一列 REST 行与推送行位数不同) */
const y4 = (v: unknown): string => (v == null ? '—' : typeof v === 'number' ? v.toFixed(4) : String(v))
function wan(v: unknown): string { if (v == null) return '—'; const n = Number(v); return n.toLocaleString('en-US') + ' 万' }
function pctStr(v: unknown): string { if (v == null) return '—'; return Number(v).toFixed(2) + '%' }
function hms(epoch: unknown): string {
  if (epoch == null) return '—'
  const d = new Date(Number(epoch) * 1000), p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
function bpStr(v: unknown): string { if (v == null) return '—'; const n = Number(v); return `${n >= 0 ? '+' : ''}${n.toFixed(1)}bp` }

const STAGE_STEP: Record<string, number> = { 待询价: 0, 询价中: 1, 还价中: 1, 已报价: 1, 挂起: 1, 已暂停: 0, 转人工: 2, 待人工确认: 2, 已成交: 2 }

function normBond(r: Dict): BondRow {
  return {
    i: Number(r.i ?? 0),
    name: s(r.name), code: s(r.code), term: s(r.term), val: s(r.val),
    dur: num2(r.dur), coupon: typeof r.coupon === 'number' ? pctStr(r.coupon) : s(r.coupon),
    ibv: num2(r.ibv), ib: y4(r.ib), io: y4(r.io), iov: num2(r.iov),
    obs: s(r.obs), obv: num2(r.obv), ob: y4(r.ob), oo: y4(r.oo), oov: num2(r.oov), oos: s(r.oos),
    status: (s(r.status, '待询价') as BondStatus),
    tags: Array.isArray(r.tags) ? (r.tags as string[]) : [],
    isNew: !!r.isNew,
    issuer: s(r.issuer), issuerRating: s(r.issuerRating ?? r.bondRating), implied: s(r.implied),
    industry: s(r.industry), prov: s(r.prov), nature: s(r.nature),
    gLimit: typeof r.gLimit === 'number' ? wan(r.gLimit) : s(r.gLimit),
    ngLimit: typeof r.ngLimit === 'number' ? wan(r.ngLimit) : s(r.ngLimit),
    zz: s(r.zz),
    buyMgr: s(r.buyMgr), sellMgr: s(r.sellMgr),
    valDate: s(r.valDate),
    bondUniCode: r.bondUniCode != null ? String(r.bondUniCode) : undefined,
    flowId: typeof r.flowId === 'number' ? r.flowId : undefined,
    quotedAt: r.issueTime == null && r.quotedAt == null ? undefined : Number(r.issueTime ?? r.quotedAt),
    bondType: optText(r.bondType),
    firmness: optText(r.firmness),
    clearing: optText(r.clearing),
    scheme: optText(r.scheme),
  }
}
function normBonds(arr: Dict[]): BondRow[] { return arr.map((r, i) => ({ ...normBond(r), i: r.i != null ? Number(r.i) : i })) }

/** 静态增量只映射公共主数据字段，绝不覆盖本人底价和询价状态。 */
export function applyBondStatic(row: BondRow, master: BondStatic): BondRow {
  const next: Record<string, unknown> = {...row}
  for (const [source,target] of [['sec_name','name'],['windcode','code'],['bond_type','bondType'],['residual_maturity','term'],['mod_dur','dur'],['val_date','valDate'],['issuer_rating','issuerRating'],['implied_rating','implied'],['issuer','issuer'],['nature','nature'],['industry','industry'],['prov','prov']] as const) {
    if (source in master) next[target] = s(master[source])
  }
  if ('coupon' in master) next.coupon = pctStr(master.coupon)
  if ('tags' in master) next.tags = Array.isArray(master.tags) ? master.tags : []
  if ('val_yield' in master || 'net_price' in master) {
    const [oldYield = '—', oldPrice = '—'] = row.val.split('/')
    next.val = `${'val_yield' in master ? num2(master.val_yield) : oldYield}/${'net_price' in master ? num2(master.net_price) : oldPrice}`
  }
  return next as unknown as BondRow
}

function normCp(r: Dict): Counterparty {
  const bonds: CpBond[] = Array.isArray(r.bonds) ? (r.bonds as Dict[]).map(b => {
    const stage = s(b.stage ?? b.quote ?? b.phase ?? b.state, '—')
    return {
      sessionId: optText(b.session_id),
      groupId:optText(b.group_id),revision:typeof b.revision==='number'?b.revision:undefined,
      groupRevision:typeof b.group_revision==='number'?b.group_revision:undefined,
      analysisPending:b.analysis_pending===true,sendAllowed:b.send_allowed==null?undefined:!!b.send_allowed,
      groupSendAllowed:b.group_send_allowed==null?undefined:!!b.group_send_allowed,
      memberSendAllowed:typeof b.member_send_allowed==='boolean'?b.member_send_allowed:undefined,groupState:optText(b.state),
      pendingCommandId:optText(b.pending_command_id),commandState:optText(b.command_state),responseState:optText(b.response_state),
      tradeDay:optText(b.trade_day),needsReview:!!b.needs_review,
      quotes:b.quotes && typeof b.quotes==='object'?b.quotes as {bid:NegotiationQuote;ofr:NegotiationQuote}:undefined,
      summary:b.summary && typeof b.summary==='object'?b.summary as GroupSummary:undefined,
      trade: { own_side:s(b.own_side), peer_quote:s(b.peer_quote), own_limit:s(b.own_limit), gap_bp:s(b.gap_bp), peer_volume_wan:s(b.peer_volume_wan), quote_at:s(b.quote_at), freshness:s(b.freshness), phase:s(b.phase) },
      name: s(b.name), code: s(b.code, ''), quote: (s(b.quote ?? b.stage, '待询价') as BondStatus), stage,
      step: STAGE_STEP[stage] ?? 0,
      spread: b.spread != null ? s(b.spread) : bpStr(b.spread_bp),
      updated: b.updated != null && typeof b.updated !== 'number' ? s(b.updated) : hms(b.updated),
      ours: b.ours != null ? s(b.ours) : num2(b.my_yield),
      theirs: b.theirs != null ? s(b.theirs) : num2(b.peer_yield),
      room: s(b.room),
      used: b.used != null && b.used !== '' && Number.isFinite(Number(b.used)) ? Number(b.used) : null,
    }
  }) : []
  const org = r.org != null ? s(r.org) : `${s(r.channel, 'QT')} · ${r.is_broker ? '经纪商' : '交易对手'}`
  return {
    id: s(r.id ?? r.cp_id),
    threadId:optText(r.thread_id),
    name: s(r.name), org,
    username: s(r.username),
    hist: Array.isArray(r.hist) ? (r.hist as string[]) : [],
    avgSpread: s(r.avgSpread), avgReply: s(r.avgReply), winRate: s(r.winRate), pref: s(r.pref), termPref: s(r.termPref),
    bonds,
  }
}

function chatContexts(m: Dict): ChatMsg['contexts'] {
  if (!Array.isArray(m.contexts)) return undefined
  return (m.contexts as Dict[])
    .map(c => ({ code: s(c.code,''), name: s(c.name,''), memberId:optText(c.member_id),relation:optText(c.relation) }))
    .filter(c => c.name || c.code)
}

function normChat(data: unknown): ChatMsg[] {
  // 后端形状 { cp, bonds:[{windcode,name}], messages:[{role,text,tag,time,bond(windcode)}] };
  // 也兼容直接返回 ChatMsg[] 的形状。简称放在 contexts，没有结果时保留识别中。
  const roleOf = (m: Dict): ChatRole => {
    if (['human','bot','sys','cp','peer','self'].includes(String(m.role))) return m.role as ChatRole
    const direction = String(m.dir ?? m.direction ?? '')
    return ['out','outgoing'].includes(direction) ? 'self' : ['in','incoming'].includes(direction) ? 'cp' : 'unknown'
  }
  const one = (m: Dict, bond: string): ChatMsg => ({
    revision:typeof m.projection_revision==='number'?m.projection_revision:typeof m.revision==='number'?m.revision:undefined,
    ingestRevision:typeof m.revision==='number'?m.revision:undefined,
    analysisState:optText(m.analysis_state),occurredAt:typeof m.time==='number'?m.time:undefined,
    receivedAt:typeof m.received_at==='number'?m.received_at:undefined,kind:optText(m.kind),targetId:optText(m.target_id),
    role: roleOf(m),
    text: s(m.text, ''),
    tag: s(m.tag, roleOf(m) === 'self' ? '本方' : roleOf(m) === 'unknown' ? '来源未标明' : ''),
    time: typeof m.time === 'number' ? hms(m.time) : s(m.time, ''),
    bond,
    bond_name: s(m.bond_name, ''),
    bond_state: s(m.bond_state, ''),
    contexts: chatContexts(m),
    id: optText(m.id ?? m.message_id), cmd_id: optText(m.cmd_id),
    trace: m.trace && typeof m.trace === 'object' && !Array.isArray(m.trace) ? m.trace as unknown as TraceView : undefined,
  })
  if (Array.isArray(data)) return (data as Dict[]).map(m => one(m, s(m.bond, '')))
  const d = data as Dict
  const code2name: Record<string, string> = {}
  if (Array.isArray(d.bonds)) (d.bonds as Dict[]).forEach(b => { code2name[s(b.windcode ?? b.code)] = s(b.name) })
  const msgs = Array.isArray(d.messages) ? (d.messages as Dict[]) : []
  return msgs.map(m => {
    const bondRaw = s(m.bond, '')
    return { ...one({...m,projection_revision:m.projection_revision??d.revision}, bondRaw), bond_name: s(m.bond_name, code2name[bondRaw] || '') }
  })
}

function normSummary(d: Dict): { botStatus: string; botEnabled?: boolean; stats: { k: string; v: string; d: string }[] } {
  const bot = (d.bot as Dict) || {}
  const stats = Array.isArray(d.stats) ? (d.stats as Dict[]).map(x => {
    const unit = s(x.unit, ''), val = x.value
    const v = unit === '秒' ? `${val}s` : String(val ?? '—')
    return { k: s(x.label ?? x.k), v, d: s(x.sub ?? x.d, unit && unit !== '秒' ? unit : '') }
  }) : []
  return { botStatus: s(bot.status ?? d.botStatus, '未知'), ...(typeof bot.enabled === 'boolean' ? {botEnabled:bot.enabled}:{}), stats }
}

function normDetailRows(arr: Dict[]): DetailRow[] {
  return arr.map(r => {
    if (r.term != null && r.trader != null && r.date != null) return r as unknown as DetailRow // 已是前端形状
    const rawDir = s(r.dir ?? r.my_direction, ''); const dir = rawDir === 'SELL' || rawDir.includes('卖') ? '卖出' : rawDir === 'BUY' || rawDir.includes('买') ? '买入' : '—'
    return {
      date: s(r.date), name: s(r.name), code: s(r.code ?? r.windcode), term: s(r.term ?? r.residual),
      dir, val: num2(r.val ?? r.cnbd_val), theirs: num2(r.theirs ?? r.peer_final_yield), theirVol: num2(r.theirVol ?? r.peer_vol_wan),
      ours: num2(r.ours ?? r.my_floor), ourVol: num2(r.ourVol ?? r.my_vol_wan),
      spread: r.spread != null ? s(r.spread) : bpStr(r.spread_to_floor_bp),
      trader: s(r.trader), pm: s(r.pm ?? r.pm_name), st: s(r.st ?? r.state, '询价中'),
    }
  })
}

function normChart(c: Dict | undefined): ChartSpec | undefined {
  if (!c) return undefined
  const COLORS = ['#B01C24', '#C68D58', '#1E5FB4', '#1F6B3E', '#8F6412']
  const type = s(c.type, 'bar') as ChartSpec['type']
  const data = (c.data as Dict) || {}
  // 不把缺失值交给SVG算术转换为0；仅展示所有系列均有实数的类别。
  if (Array.isArray(c.categories) && Array.isArray(c.series)) {
    const chart = c as unknown as ChartSpec
    const series = chart.series!
    if (!series.length || series.some(se => !Array.isArray(se.data))) return undefined
    const indices = chart.categories!.map((_, i) => i).filter(i => series.every(se => typeof se.data[i] === 'number' && Number.isFinite(se.data[i])))
    if (!indices.length) return undefined
    return { ...chart, categories:indices.map(i => chart.categories![i]), series:series.map(se => ({...se, data:indices.map(i => se.data[i]!)})) }
  }
  if (Array.isArray(c.points)) return c as unknown as ChartSpec
  if (Array.isArray(data.points)) return { type: 'scatter', title: s(c.title, ''), xLabel: s(c.xLabel, ''), yLabel: s(c.yLabel, ''), points: data.points as ChartSpec['points'] }
  const series = Array.isArray(data.series) ? (data.series as Dict[]).map((se, i) => ({ name: s(se.name, `系列${i + 1}`), color: COLORS[i % COLORS.length], data: (se.values ?? se.data ?? []) as number[] })) : []
  return { type, title: s(c.title, ''), xLabel: s(c.xLabel, ''), yLabel: s(c.yLabel, ''), categories: (data.labels ?? c.categories ?? []) as string[], series }
}

/* ============================ 债券详情组装 ============================ */
async function assembleDetail(code: string, bond?: BondRow): Promise<BondDetail> {
  const r = await jget<Resp<Dict>>(`/api/bond/${encodeURIComponent(code)}`)
  if (!r.ok || !r.data) throw new Error(r.hint || '债券详情读取失败')
  const d = r.data
  const nb = d.bond ? normBond(d.bond as Dict) : bond
  if (!nb) throw new Error('缺少债券资料')
  const channels: ChannelBest[] = Array.isArray(d.quotes) ? (d.quotes as Dict[]).map(q => ({
    label: s(q.channel), bid: num2(q.best_bid), ofr: num2(q.best_ofr),
    bidVol: num2(q.best_bid_vol), ofrVol: num2(q.best_ofr_vol), bidSide: s(q.best_bid_side), ofrSide: s(q.best_ofr_side),
  })) : []
  const cps = await api.counterparties()
  return {
    bond: nb, channels,
    trades: Array.isArray(d.trades) ? d.trades as BondDetail['trades'] : [],
    hist: { avg:'', zz:'', max:'—', min:'—', d0:'', d1:'', d2:'', ...((d.hist as object) || {}) },
    intra: { bid:'', ofr:'', zz:'', max:'—', min:'—', ...((d.intra as object) || {}) },
    modalCps: cps.ok ? cps.data.flatMap(cp => cp.bonds.filter(cb => cb.code === nb.code).map(cb => ({ id:cp.id, name:cp.name, stage:cb.stage, spread:cb.spread, updated:cb.updated }))) : [],
  }
}

function normStrategy(d: Dict): Strategy {
  const templates = Array.isArray(d.templates) ? d.templates as Dict[] : []
  if (d.configured === false && templates.length === 0) return { ...STRATEGY_DEFAULT }
  const t = templates.find(t => t.template_id === d.active_template_id)
  if (!t) throw new Error('缺少当前策略模板')
  const sys = (d.system || {}) as Dict
  return { templateId:String(t.template_id), configured:true, startDev:Number(t.start_offset_bp), boundary:Number(t.boundary_bp), step:Number(t.step_bp), strict:t.strict_match === true, roundLot:Number(sys.s1_round_lot_wan), crossDay:Number(sys.s2_crossday_bp) }
}

async function downloadXlsx(path:string):Promise<{blob:Blob;filename:string}> {
    const r = await authFetch(path, {headers:{Accept:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}})
    if (!r.headers.get('Content-Type')?.split(';')[0].includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) {
      const error = await r.clone().json().catch(() => ({}))
      throw new Error(error.hint || error.detail || '服务端未返回XLSX文件，未下载')
    }
    const disposition = r.headers.get('Content-Disposition') || ''
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1]
    const plain = /filename="?([^";]+)"?/i.exec(disposition)?.[1]
    let filename = '询价行情.xlsx'
    try { filename = (encoded ? decodeURIComponent(encoded) : plain) || filename } catch { /* Use the safe default for malformed headers. */ }
    filename = filename.replace(/[\/\\\r\n]/g,'_')
    if (!filename.toLowerCase().endsWith('.xlsx')) filename = '询价行情.xlsx'
    const blob = await r.blob()
    if (!blob.size) throw new Error('服务端返回空文件，未下载')
    return {blob,filename}
}

export const api = {
  unknownCommands: () => jget<Resp<UnknownCommand[]>>('/api/im/commands/unknown'),
  reconcileCommand: (id:string,body:{status:string;evidence:string;expected_result_hash:string;confirmed:boolean}) => jpost<Resp<{status:string}>>('/api/im/receipts/'+encodeURIComponent(id)+'/reconcile',body),
  /* ---------- 屏1 意向询价 ---------- */
  /** 行情分页:keyset 游标。before=0 取最新一页,之后传上一页的 nextBefore。
   *  不用 offset —— WS 往头部灌新券会让 offset 整体右移,翻页重复取行。
   *  失败不回退示例券:列表只接受 innodealing 实时源,接口不通就空表。 */
  async bonds(before = 0, limit = 0, groups: string[] = [], channel = 'best', broker = '', beforeUni = 0, filter?: FilterSpec): Promise<BondsResp> {
    const empty = (hint: string): BondsResp => ({ ok: false, data: [], total: 0, hasMore: false, nextBefore: 0, hint })
    try {
      const p = new URLSearchParams()
      if (filter) p.set('filter', JSON.stringify(filter))
      if (limit) { p.set('before', String(before)); p.set('limit', String(limit)) }
      if (beforeUni) p.set('beforeUni', String(beforeUni))
      if (groups.length) p.set('groups', groups.join(','))
      if (channel && channel !== 'best') p.set('channel', channel)
      if (broker) p.set('broker', broker)
      const qs = p.toString() ? `?${p}` : ''
      const r = await jget<Resp<Dict[]> & { total?: number; hasMore?: boolean; nextBefore?: number; nextUni?: number; source?: string; emptyReason?: string | null }>(`/api/bonds${qs}`)
      if (r?.ok === true && r.data != null) {
        if (r.source && !['innodealing', 'public', 'not_configured'].includes(r.source)) return empty('后端未返回正式公共行情来源')
        const data = normBonds(r.data)
        return { ok: true, data, total: r.total ?? data.length, hasMore: r.hasMore ?? false, nextBefore: r.nextBefore ?? 0, nextUni: r.nextUni ?? 0, source: r.source, emptyReason: r.emptyReason }
      }
      return empty('行情接口没有返回报价')
    } catch {
      return empty('连不上询价服务')
    }
  },
  exportBonds: (filter:FilterSpec) => downloadXlsx('/api/bonds/export?' + new URLSearchParams({filter:JSON.stringify(filter)})),
  exportMonitor: (start:string,end:string,peer?:string) => downloadXlsx('/api/monitor/export?' + new URLSearchParams({start,end,...(peer ? {peer} : {})})),
  resolveBonds: (selection: Selection) => result(async () => {
    const r = await jpost<Resp<Dict[]>>('/api/bonds/resolve', { selection })
    return { ...r, data: r.ok ? normBonds(r.data) : [] }
  }, [] as BondRow[]),
  getFloors: () => jget<Resp<{items:{code:string;kind:'bp'|'yield';value:number|string;revision:number}[]}>>('/api/im/inquiry/floor'),
  brokers: () => jget<{ ok: boolean; data: { id: number; name: string }[] }>('/api/brokers'),

  bond: async (windcode: string, bond?: BondRow): Promise<Resp<BondDetail>> => ({ ok: true, data: await assembleDetail(windcode, bond) }),
  /* 批量操作的"选择"有两种形态,见后端 _resolve_selection:
     ① 枚举 { windcodes }            —— 用户手点了几只
     ② 谓词 { selectAll, groups, except } —— 用户点了全选
     全选必须走谓词:几十万行下枚举会把前端 O(1) 的全选打回原形,请求体也能到几 MB。 */
  rfqStart: (sel: Selection) => result(() => jpost<Resp<BulkResult>>('/api/rfq/start', sel), { started: [], count: 0 }),
  rfqStop: (sel: Selection) => result(() => jpost<Resp<BulkResult>>('/api/rfq/stop', sel), { stopped: [], count: 0 }),
  floor: (sel: Selection, bp: number) => result(() => jpost<Resp<BulkResult>>('/api/instruction/floor', { ...sel, bp }), { applied: [], bp }),
  getStrategy: () => result(async () => {
    const r = await jget<Resp<Dict>>('/api/strategy')
    return { ...r, data: r.ok ? normStrategy(r.data) : { ...STRATEGY_DEFAULT } }
  }, { ...STRATEGY_DEFAULT }),
  saveStrategy: (st: Strategy) => result(async () => {
    const r = await jpost<Resp<Dict>>('/api/strategy', { template_id: st.templateId || 'personal',
      start_offset_bp:st.startDev, boundary_bp:st.boundary, step_bp:st.step, strict_match:st.strict,
      system:{s1_round_lot_wan:st.roundLot,s2_crossday_bp:st.crossDay} })
    return { ...r, data:r.ok ? normStrategy(r.data) : st }
  }, st),

  /* ---------- 屏2 机器人监控 ---------- */
  async negotiationUpdates(after:number,signal:AbortSignal,onRevision:(revision:number)=>void,onOpen:()=>void):Promise<void> {
    // EventSource cannot attach this application's bearer token; use the shared authenticated transport.
    const response=await authFetch('/api/negotiation/updates?'+new URLSearchParams({after:String(after)}),{signal,headers:{Accept:'text/event-stream'}})
    if(!response.headers.get('Content-Type')?.includes('text/event-stream')||!response.body)throw new Error('增量通知暂不可用，定时补拉仍继续')
    onOpen()
    const reader=response.body.getReader(),decoder=new TextDecoder()
    let buffer='',revision=after
    try {
      while(!signal.aborted) {
        const chunk=await reader.read()
        if(chunk.done)break
        buffer+=decoder.decode(chunk.value,{stream:true})
        buffer=buffer.replace(/\r\n/g,'\n')
        if(buffer.length>65536)throw new Error('增量通知格式无效')
        let end:number
        while((end=buffer.indexOf('\n\n'))>=0){
          const frame=buffer.slice(0,end);buffer=buffer.slice(end+2)
          if(!frame.split('\n').some(line=>line.trim()==='event: changed'))continue
          const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trim()).join('\n')
          const next=JSON.parse(data).revision
          if(Number.isSafeInteger(next)&&next>revision){revision=next;onRevision(next)}
        }
      }
    } finally {await reader.cancel().catch(()=>{});reader.releaseLock()}
  },
  summary: () => result(async () => {
    const r = await jget<Resp<Dict>>('/api/monitor/summary')
    return { ...r, revision:typeof r.data?.revision==='number'?r.data.revision:r.revision,data: normSummary(r.data ?? {}) } as Resp<{ botStatus: string; botEnabled?: boolean; stats: { k: string; v: string; d: string }[] }>
  }, emptySummary()),
  botToggle: (on: boolean) => result(() => jpost<Resp<{ botStatus: string }>>('/api/bot/toggle', { status: on ? '正常' : '停止' }), { botStatus: on ? '正常' : '停止' }),
  counterparties: () => result(async () => {
    const r = await jget<Resp<Dict[]>>('/api/monitor/counterparties')
    return { ...r, data: (r.data ?? []).map(normCp) } as Resp<Counterparty[]>
  }, [] as Counterparty[]),
  async monitorChat(cp: string, cursor = ''): Promise<Resp<ChatMsg[]> & { hint?: string; nextCursor?: string | null; hasMore?: boolean; revision?:number }> {
    try {
      const r = await jget<Resp<unknown> & { hint?: string }>(`/api/monitor/chat?cp=${encodeURIComponent(cp)}${cursor ? "&cursor=" + encodeURIComponent(cursor) : ""}`)
      if (r && r.ok === true && r.data != null) { const page=r.data as Dict; return { ok: true, data: normChat(r.data), revision:typeof page.revision==='number'?page.revision:undefined, nextCursor:typeof page.nextCursor==='string'?page.nextCursor:null, hasMore:page.hasMore===true } }
      if (r && r.ok === false) return { ok: false, data: [], hint: r.hint || '消息拉取失败' }
      return { ok: false, data: [], hint: '消息拉取失败' }
    } catch {
      return { ok: false, data: [], hint: '消息拉取失败' }
    }
  },
  async monitorSend(cp: string, bond: string, text: string, requestId: string = crypto.randomUUID()): Promise<{
    ok: boolean
    blocked?: boolean
    hint?: string
    data: { blocked: boolean; status: string; cmd_id: string; reason: string }
  }> {
    const empty = { blocked: false, status: '', cmd_id: '', reason: '' }
    try {
      const r = await jpost<Resp<Dict> & { blocked?: boolean; hint?: string }>('/api/monitor/send', {
        cp, bond, text, idempotency_key: requestId, request_id: requestId,
      })
      const data = (r?.data || {}) as Dict
      if (r && (r.blocked || data.blocked)) {
        return { ok: false, blocked: true, hint: r.hint, data: { ...empty, blocked: true, reason: String(r.hint || '') } }
      }
      if (r && r.ok === true) {
        return {
          ok: true,
          blocked: false,
          data: {
            blocked: false,
            status: String(data.status || ''),
            cmd_id: String(data.cmd_id || ''),
            reason: String(data.reason || ''),
          },
        }
      }
      return { ok: false, blocked: false, hint: r?.hint || '未发送', data: { ...empty, reason: String(r?.hint || '') } }
    } catch {
      return { ok: false, blocked: false, hint: '发送请求没有完成', data: { ...empty, reason: '发送请求没有完成' } }
    }
  },
  setFloor: (codes: string[], value: number, kind: 'bp' | 'yield' = 'bp') => jpost<Resp<{ bp?: number; value?: number }>>('/api/im/inquiry/floor', kind === 'yield' ? { codes, kind, value } : { codes, kind, bp: value }),
  widenFloor: (code: string) => result(() => jpost<Resp<{ before: number; after: number }>>('/api/im/inquiry/floor/widen', { code }), {} as { before: number; after: number }),
  pauseSession: (member_id: string) => result(() => jpost<Resp<{ member_id: string }>>('/api/im/inquiry/session/pause', { member_id }), {} as { member_id: string }),
  resumeSession: (member_id: string) => result(() => jpost<Resp<{ member_id: string }>>('/api/im/inquiry/session/resume', { member_id }), {} as { member_id: string }),
  memberSending: (memberId:string,allowed:boolean) => result(()=>jpost<Resp<Dict>>('/api/negotiation/members/'+encodeURIComponent(memberId)+'/sending',{allowed}),{}),
  groupSending: (groupId:string,allowed:boolean) => result(()=>jpost<Resp<Dict>>('/api/negotiation/groups/'+encodeURIComponent(groupId)+'/sending',{allowed}),{}),
  markInquiry: (code: string, status: string) => result(() => jpost<Resp<{ code: string; status: string }>>('/api/im/inquiry/status', { code, status }), {} as { code: string; status: string }),
  takeover: (id: string) => result(() => jpost<Resp<{ ok: boolean }>>(`/api/session/${encodeURIComponent(id)}/takeover`, {}), { ok: true }),
  abandon: (id: string) => result(() => jpost<Resp<{ ok: boolean }>>(`/api/session/${encodeURIComponent(id)}/abandon`, {}), { ok: true }),
  detail: (cp: Counterparty) => result(async () => {
    const r = await jget<Resp<Dict[]>>(`/api/monitor/detail?cp=${encodeURIComponent(cp.id)}`)
    return { ...r, data: normDetailRows(r.data ?? []) } as Resp<DetailRow[]>
  }, [] as DetailRow[]),

  /* ---------- 屏3 智能问答 ---------- */
  /* ---------- 采集端通道 ---------- */
  imHistory: (before?: number, peer?: string) => jget<Resp<NativeHistoryPage>>('/api/im/history?limit=50' + (before ? '&before=' + before : '') + (peer ? '&peer=' + encodeURIComponent(peer) : '')),
  imFiles: (before?: number) => jget<Resp<TransferFilePage>>('/api/im/files?limit=50' + (before ? '&before=' + before : '')),
  sessionReset: () => jget<Resp<SessionResetStatus | null>>('/api/im/session-reset'),
  startSessionReset: (requestId: string, bindingVersion: number) =>
    jpost<Resp<SessionResetStatus>>('/api/im/session-reset', {
      request_id: requestId, expected_binding_version: bindingVersion, confirmed: true,
    }),
  mqConfig: () => jget<Resp<MqConfig>>('/api/im/mq-config'),
  saveMqConfig: (body: MqConfigInput) => requestJson<Resp<MqConfig>>('/api/im/mq-config', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }),
  testMqConfig: (body: MqConfigInput) => jpost<Resp<MqConnectionTest>>('/api/im/mq-config/test', body),
  imLinks: () => jget<{ ok: boolean; data: { active_id: string; links: Dict[] }; hints: Record<string, string> }>('/api/im/links'),
  imSave: (id: string | null, body: Dict) => id
    ? requestJson('/api/im/links/' + id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    : jpost('/api/im/links', body),
  imDelete: (id: string) => requestJson('/api/im/links/' + id, { method: 'DELETE' }),
  imTest: (id: string, accountId?: string) => jpost('/api/im/links/' + id + '/test', accountId ? { account_id: accountId } : {}),
  imActivate: (id: string, version?:number) => jpost('/api/im/links/' + id + '/activate', {expected_version:version}),
  imContacts: (refresh = false) => jget<{ ok: boolean; hint?: string; data: Dict }>('/api/im/contacts' + (refresh ? '?refresh=1' : '')),
  imSend: (uin: string, text: string, requestId: string = crypto.randomUUID()) => jpost<{ ok: boolean; blocked?: boolean; hint?: string; data: Dict }>('/api/im/send', { uin, text, idempotency_key: requestId, request_id:requestId }),
  negotiationCapacity: () => jget<Resp<CounterpartyCapacity>>('/api/negotiation/capacity'),
  imRfqPreview: (items: (RfqPolicy & {peer_ids:string[]})[], requestId: string) =>
    jpost<Resp<RfqPreview>>('/api/im/rfq/preview', {request_id:requestId, items}),
  imRfqSendDirect: (items: (RfqPolicy & {peer_ids:string[]})[], requestId: string) =>
    jpost<Resp<RfqSubmission>>('/api/im/rfq/send-direct', {request_id:requestId, items}),
  imRfqSend: (batchId: string, version: number, requestId: string) =>
    jpost<Resp<RfqSubmission>>('/api/im/rfq/send', {batch_id:batchId, expected_version:version, request_id:requestId}),
  imRfqStatus: () => jget<{ ok: boolean; hint?: string; data?: { items?: { code: string; status: string; quote?: Record<string, string | number> | null }[] } }>('/api/im/rfq/status'),
  archiveSession: (id: string) => result(() => jpost<Resp<Dict>>('/api/sessions/' + encodeURIComponent(id) + '/archive', {}), {}),
  imCatalog: () => jget<Resp<CollectorCatalog>>('/api/im/catalog'),
  refreshImCatalog: () => jpost<Resp<CollectorCatalog>>('/api/im/catalog/refresh', {}),
  selectImBinding: (body: CollectorSelection) => jpost<Resp<CollectorSelectionResult>>('/api/im/binding/select', {
    workbench_id: body.workbench_id, account_uid: body.account_uid, expected_version: body.version,
  }),
  startQuoteSource: () => jpost('/api/im/quote/start', {}),
  stopQuoteSource: () => jpost('/api/im/quote/stop', {}),
  quoteSource: () => jget<{ ok: boolean; hint?: string; data: { account: string; sid_set: boolean; cookie_set: boolean; device_id_set: boolean } }>('/api/im/quote'),
  saveQuoteSource: (body: { account: string; sid: string; cookie: string; device_id: string }) =>
    requestJson('/api/im/quote', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  testQuoteSource: (body: { account: string; sid: string; cookie: string; device_id: string }) =>
    requestJson('/api/im/quote/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  imReceipt: (cmdId: string) => jget<{ ok: boolean; hint?: string; data: { cmd_id: string; status: string; ok: boolean; code: string; reason: string } }>('/api/im/receipts/' + encodeURIComponent(cmdId)),

  qa: (question: string) => result(async () => {
    const r = await jpost<Resp<Dict>>('/api/qa', { question })
    const d = r.data || {}
    return { ...r, data: { answer: s(d.answer, ''), chart: normChart(d.chart as Dict | undefined) } } as Resp<QaResult>
  }, { answer: '没有可用的问答结果', chart: undefined }),
}

/** 链路追踪:后端消息若带 trace 用后端,缺少服务端审计证据时明确标注缺失。 */
export function traceOf(m: ChatMsg & { idx: number; cp: string; trace?: TraceView }): TraceView {
  return m.trace && !Array.isArray(m.trace) ? m.trace : { id: m.id || '—', text:m.text, status:'无审计记录', summary:'服务端尚未提供此消息的审计证据', dirLabel:['human','bot','self'].includes(m.role) ? '发送' : ['cp','peer'].includes(m.role) ? '接收' : '未提供', steps:[], meta:[] }
}
