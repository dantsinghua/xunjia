<script setup lang="ts">
/** 屏1 · 意向询价:左筛选栏 + 中行情主表(13 子列,sticky 首末列) +
 *  勾选浮条(开始/停止询价 + 底价统一调整 ±BP) + 债券详情弹窗 + 询价策略弹窗。
 *  还原设计稿 isQuote 屏。 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import { App } from 'ant-design-vue'
import { useRouter } from 'vue-router'
import { api, applyBondStatic, type Selection, type FilterSpec } from '../api'
import { imBoundAccount } from '../lib/imLink'
import { authToken, authUserId } from '../lib/http'
import { createQuoteClient } from '../lib/quoteClient'
import type { QuoteDelta } from '../lib/quoteTypes'
import PipelineStats, { type PipelineStat } from '../components/PipelineStats.vue'
import { STATUS_COLOR } from '../theme'
import type { BondRow, BondStatus, Strategy } from '../lib/types'
import { STRATEGY_DEFAULT } from '../lib/types'
import {
  FILTER_GROUPS, PRICE_KEYS, PRICE_ROWS,
  emptyPrices, groupsFromChips, resetFilterState, rowMatchesQuoteFilter,
} from '../lib/quoteFilter'
import BondModal from '../components/BondModal.vue'
import RfqAssignModal from '../components/RfqAssignModal.vue'
import StrategyModal from '../components/StrategyModal.vue'
import ImLinkBar from '../components/ImLinkBar.vue'

const { message } = App.useApp()
const router = useRouter()

/* shallowRef + 冻结行对象:几十万条量级下绝不能让每行都变成深层 Proxy。
   行数据是只读展示数据,覆盖态(勾选/状态/底价)另存在旁边的 map 里。 */
const bonds = shallowRef<BondRow[]>([])
function freezeRows(list: BondRow[]): BondRow[] { return list.map(b => Object.freeze(b)) }
/* 行状态一律以 code 为键。绝不能用 b.i:后端不返回 i,api.normBonds 取的是
   「本次响应数组下标」,分页每页都从 0 开始;WS 走 normBond 更是恒为 0。
   用 i 会让 ~408 行共用一个键,勾一条串一片,且 :key 大量重复把 diff 拖成 O(n²)。 */
/* 选择模型:几十万行下「全选」不能去写 N 个键。
   用 selectAll 标志 + 例外集:全选 O(1),取消某行只是往例外集加一个 code。 */
const selectAll = ref(false)
const picked = reactive(new Set<string>())   // selectAll=false 时:被选中的
const except = reactive(new Set<string>())   // selectAll=true  时:被排除的
function isPicked(code: string) { return selectAll.value ? !except.has(code) : picked.has(code) }
const serverStatus = reactive<Record<string, BondStatus>>({})
const sideOpen = ref(true)
const intentMode = ref<'yield' | 'price' | 'bp'>('yield')
const chanTab = ref(0)
const CHAN = ['best', 'qt', 'broker', 'mm'] as const
const brokerPick = ref('best')
const brokerOpen = ref(false)
const SIX_BROKERS = [
  { id: 1, name: '国际' },
  { id: 2, name: '中诚' },
  { id: 3, name: '平安' },
  { id: 7, name: '上田' },
  { id: 8, name: '信唐' },
  { id: 9, name: '国利' },
]
const brokerList = ref(SIX_BROKERS.map(b => ({ ...b })))
const brokerMenu = ref<HTMLElement | null>(null)
const channelNote = ref('')
const beforeUni = ref(0)
const bpAdj = ref(2)
const strategyOpen = ref(false)
const strategy = ref<Strategy>({ ...STRATEGY_DEFAULT })
const modalBond = ref<BondRow | null>(null)
const tip = ref<{ bond: BondRow; x: number; y: number } | null>(null)

/* 每页 50 行服务端筛选；翻页保留选择，筛选与账号变更清除选择。 */
const PAGE = 50
const total = ref(0)
const hasMore = ref(true)
const loading = ref(false)
const liveCount = ref(0) // 本次会话 WS 灌进来的条数
const feedHint = ref('正在读取公共行情…')
let nextBefore = 0       // keyset 游标:下一页取 seq < nextBefore 的
const scroller = ref<HTMLElement | null>(null)
let stopStream: (() => void) | null = null

const pageIndex = ref(0)
const pageCursors: { before:number; uni:number }[] = [{ before:0, uni:0 }]
let generation = 0
async function readPage(index: number, cursor = pageCursors[index], resetScroll = true): Promise<boolean> {
  if (!cursor || feedStopped) return false
  const request = ++generation
  const key = scopeKey()
  loading.value = true
  try {
    const r = await quoteBonds(cursor.before, cursor.uni)
    if (request !== generation || key !== scopeKey() || feedStopped) return false
    if (!r.ok) { feedHint.value = r.hint || '行情读取失败，保留上次成功内容'; return false }
    pageIndex.value = index
    pageCursors[index] = cursor
    takePage(r, 'replace')
    if (resetScroll && scroller.value) { scroller.value.scrollTop = 0; syncViewport() }
    return true
  } finally { if (request === generation) loading.value = false }
}
async function loadMore() {
  if (loading.value || !hasMore.value || channelNote.value) return
  await readPage(pageIndex.value + 1, { before:nextBefore, uni:beforeUni.value })
}
async function previousPage() { if (!loading.value && pageIndex.value > 0) await readPage(pageIndex.value - 1) }

/* ---------------- 虚拟滚动 ----------------
 * 只渲染视口 ±OVERSCAN 行,DOM 与总行数脱钩:几十万行时渲染的仍是 ~40 行。
 * 行是 nowrap 单行等高,所以固定行高 + 上下占位行即可,表格结构不破坏,
 * sticky 表头/左右列照常工作。 */
const ROW_H = 42
const OVERSCAN = 10
const scrollTop = ref(0)
const viewH = ref(800)

const winStart = computed(() => Math.max(0, Math.floor(scrollTop.value / ROW_H) - OVERSCAN))
const winEnd = computed(() => Math.min(rows.value.length, Math.ceil((scrollTop.value + viewH.value) / ROW_H) + OVERSCAN))
const visRows = computed(() => rows.value.slice(winStart.value, winEnd.value))
const padTop = computed(() => winStart.value * ROW_H)
const padBottom = computed(() => Math.max(0, (rows.value.length - winEnd.value) * ROW_H))
const padTopStyle = computed(() => ({ height: padTop.value + 'px' }))
const padBotStyle = computed(() => ({ height: padBottom.value + 'px' }))

function syncViewport() {
  const el = scroller.value
  if (!el) return
  scrollTop.value = el.scrollTop
  viewH.value = el.clientHeight
}

function onScroll() { syncViewport() }

/* ---------------- 实时灌入:QuoteClient(SharedWorker + 500ms pull) ----------------
 * 改造前是「WS 推一条→前端缓冲 300ms→prepend」。现在照 DM:
 *   网关每组 200ms 成批并服务端去重 → SharedWorker 按组累积 → 主线程每 500ms 主动 pull
 * 后台标签不 pull,消息压根不进主线程。
 *
 * 关键设计:**更新在原位闪烁,不重排**。
 * 同一只券的新报价是「同 key 新版本」,原地改字段即可;只有从没见过的券才进列表头部。
 * 这样滚动锚定问题对"更新"彻底消失 —— 而更新占了几十万条里的绝大多数。 */
let quote: ReturnType<typeof createQuoteClient> | null = null
let unsub: (() => void) | null = null
const idx = new Map<string, number>()            // bondUniCode -> bonds 数组下标
/* code -> {字段: 1涨 / -1跌 / 0持平}。只记四个价格字段,量和报价方变化不闪 ——
   少建合成层,也少让眼睛被无关变化拽走。 */
const blinking = reactive(new Map<string, Record<string, number>>())
let blinkTimer: ReturnType<typeof setTimeout> | null = null
/* 新报价标记(参考 Choice):更新过的行保留 RECENT_MS 的浅红底 + 变动格右上角红三角,到期统一摘掉。
   全局只挂一个 1s 定时器扫过期,不按条建定时器(300/s 下会堆出上百个回调)。 */
const RECENT_MS = 5000
const blinkAt = new Map<string, number>()
function scheduleBlinkExpire() {
  if (blinkTimer !== null) return
  blinkTimer = setTimeout(() => {
    blinkTimer = null
    const now = Date.now()
    for (const [c, t] of blinkAt) if (now - t >= RECENT_MS) { blinkAt.delete(c); blinking.delete(c) }
    if (blinkAt.size) scheduleBlinkExpire()
  }, 1000)
}

const stat = ref<PipelineStat>({ latencyMs: 0, batchSize: 0, dispatchMs: 0, dropped: 0,
                                 transport: '-', rendered: 0, held: 0 })

/** 整行闪烁 class:该行本轮有任何变化(或新上屏)就整行亮一下;价格上行走绿,其余走红。 */
function rowCls(code: string) {
  const c = blinking.get(code)
  if (!c) return ''
  return (c.ob === 1 || c.oo === 1) ? 'row-chg up' : 'row-chg'
}

/** 单元格闪烁 class。只有该格字段本轮变了才加;涨走 up(绿),跌/持平走默认(红)。 */
function cellCls(code: string, field: string) {
  const c = blinking.get(code)
  if (!c || c[field] === undefined) return ''
  return c[field] === 1 ? 'chg up' : 'chg'
}

/* 行身份 = bondUniCode(契约 §1.2),推送 delta.u 也是它;老 mock 里它恰好等于 code,
   真实源(QB)两者不同,按 code 建索引会让所有推送对不上号。没有 bondUniCode 的老行回落到 code。 */
function reindex() { idx.clear(); bonds.value.forEach((b, i) => idx.set(b.bondUniCode ?? b.code, i)) }

/** delta 的缩写字段 → 表格行字段。契约 §2.2 定的缩写,这里是唯一的映射点。 */
function applyDelta(row: BondRow, d: QuoteDelta): { row: BondRow; chg: Record<string, number> } {
  const next: any = d.s?.bond_uni_code === d.u ? applyBondStatic(row, d.s) : { ...row }
  const chg: Record<string, number> = {}
  const cmp = (k: string, old: string, nv: number) => {
    const p = parseFloat(old)
    chg[k] = Number.isNaN(p) || p === nv ? 0 : nv > p ? 1 : -1
  }
  for (const [source, target] of [['bb','ob'],['bo','oo']] as const) {
    if (!(source in d)) continue
    const value = d[source]
    if (value == null) { next[target] = '—'; chg[target] = 0 }
    else { cmp(target, row[target], value); next[target] = value.toFixed(4) }
  }
  for (const [source, target] of [['bbv','obv'],['bov','oov'],['bbs','obs'],['bos','oos'],['fm','firmness'],['cl','clearing']] as const) {
    if (source in d) next[target] = d[source] == null ? '—' : String(d[source])
  }

  if ('t' in d) next.quotedAt = d.t
  next.flowId = d.f
  return { row: Object.freeze(next), chg }
}

/* 端到端延迟埋点:数据到达本机(入库) → 本批被应用到行,保留最近 500 个样本;
   window.__qbE2E 供 Playwright 读取,埋点面板显示 P50/P95。 */
const e2eLat: number[] = []
function e2eStat() {
  if (e2eLat.length > 500) e2eLat.splice(0, e2eLat.length - 500)
  const s = e2eLat.slice().sort((a, b) => a - b)
  ;(window as any).__qbE2E = e2eLat
  return s.length ? { p50: s[Math.floor(s.length / 2)], p95: s[Math.max(0, Math.ceil(s.length * 0.95) - 1)] } : null
}

function onBatch(b: Map<string, QuoteDelta[]>) {
  if (!isAggregate()) return
  const t0 = performance.now()
  let n = 0
  let unseen = 0
  const arr = bonds.value.slice()   // shallowRef:换引用才触发渲染
  let mutated = false

  for (const deltas of b.values()) {
    for (const d of deltas) {
      n++
      const i = idx.get(d.u)
      if (i === undefined) { unseen++; continue }  // 没见过的券:不凭空造行,交给 headSync 走 REST 拿完整行
      const cur = arr[i]
      if ((cur as any).flowId != null && d.f < (cur as any).flowId) continue // 乱序旧版本丢弃
      if (d.c) e2eLat.push(Date.now() - d.c)
      const { row, chg } = applyDelta(cur, d)
      // 券名格也闪:价/量/报价方任一变化都算(价格列常在视口右侧,光闪价格格用户看不到「有更新」)
      if (row.ob !== cur.ob || row.oo !== cur.oo || row.obv !== cur.obv || row.oov !== cur.oov
          || row.obs !== cur.obs || row.oos !== cur.oos) chg.name = 0
      arr[i] = row
      if (Object.keys(chg).length) { blinking.set(cur.code, chg); blinkAt.set(cur.code, Date.now()) }   // 闪烁按展示行 code 取(cellCls 用 r.code)
      mutated = true
    }
  }
  if (mutated) bonds.value = arr
  if (unseen || mutated) scheduleHeadSync()

  const s = quote?.stats()
  stat.value = {
    latencyMs: s?.latencyMs ?? 0, batchSize: n, dispatchMs: Math.round(performance.now() - t0),
    dropped: s?.dropped ?? 0, transport: s?.transport ?? '-',
    rendered: visRows.value.length, held: bonds.value.length,
    e2e: e2eStat(),
  }
  // 闪烁清理:全局只留一个在途定时器。每批都 setTimeout 会在 300/s 下堆出
  // 上百个待触发回调,白烧 CPU。
  if (mutated) scheduleBlinkExpire()
}

/* WS负责当前页字段更新；REST补完整快照与筛选成员，单经纪商也定时刷新。 */
const HEAD_SYNC_MS = 1000
let headTimer: ReturnType<typeof setTimeout> | null = null
function scheduleHeadSync() {
  if (headTimer !== null) return
  headTimer = setTimeout(headSync, HEAD_SYNC_MS)
}
async function headSync() {
  try {
    if (loading.value || feedStopped) return
    await readPage(pageIndex.value, pageCursors[pageIndex.value], false)
    await refreshBusiness()
  } finally { headTimer = null }
}
let marketTimer: ReturnType<typeof setInterval> | null = null

let feedTimer: ReturnType<typeof setTimeout> | null = null
let feedStopped = false
/** 首屏只接受正式公共行情；未配置显示服务端空态，不补示例券。 */
async function loadFirst(): Promise<boolean> { return readPage(0, { before:0, uni:0 }) }
function scheduleFeedRetry() {
  if (feedStopped || feedTimer) return
  feedTimer = setTimeout(async () => {
    feedTimer = null
    if (feedStopped) return
    if (!(await loadFirst())) scheduleFeedRetry()
  if (feedStopped) return
  }, 1000)
}
onMounted(async () => {
  if (!(await loadFirst())) scheduleFeedRetry()
  api.brokers().then(res => { if (res.ok) brokerList.value = res.data || [] }).catch(() => {})
  const sr = await api.getStrategy()
  if (sr.ok) strategy.value = sr.data
  else message.error(sr.hint || '策略读取失败')
  await refreshBusiness()
  if (feedStopped) return
  marketTimer = setInterval(() => void headSync(), 3000)
  await nextTick(); syncViewport()
  if (feedStopped) return
  document.addEventListener('click', onDocClick)
  quote = createQuoteClient({authToken:authToken(),userScope:authUserId()})
  unsub = quote.subscribe(activeGroups.value, onBatch)
})
onUnmounted(() => { feedStopped = true; generation++; if (marketTimer) clearInterval(marketTimer); if (filterTimer) clearTimeout(filterTimer); if (blinkTimer) clearTimeout(blinkTimer); document.removeEventListener('click', onDocClick); if (feedTimer) clearTimeout(feedTimer); unsub?.(); quote?.dispose(); if (headTimer) clearTimeout(headTimer) })

function onDocClick(ev: MouseEvent) {
  const node = ev.target
  if (brokerMenu.value && node instanceof Node && !brokerMenu.value.contains(node)) brokerOpen.value = false
}
function selectChan(i: number) {
  brokerOpen.value = false
  if (chanTab.value === i) return
  chanTab.value = i
  void reloadNow()
}
function selectBroker(id: string) {
  brokerOpen.value = false
  if (brokerPick.value === id) return
  brokerPick.value = id
  void reloadNow()
}
const brokerLabel = computed(() => {
  if (brokerPick.value === 'best') return '经纪商最优'
  return brokerList.value.find(b => String(b.id) === brokerPick.value)?.name || '经纪商最优'
})

/* ---- 筛选 ----
 * 类型 / 期限 / 清算翻成订阅组,REST + WS 仍走 groups。
 * 所有条件共用完整 FilterSpec，由服务端对全量报价先筛选再分页。
 * 关键字和价格区间点「筛选行情」或搜索框回车后才生效;chip 一点就生效。 */
const activeChip = reactive<Record<string, number>>(Object.fromEntries(FILTER_GROUPS.map(g => [g.title, 0])))
const keyword = ref('')
const appliedKeyword = ref('')
const priceDraft = reactive(emptyPrices())
const priceApplied = reactive(emptyPrices())

function isAggregate() {
  return chanTab.value === 0 || (chanTab.value === 2 && brokerPick.value === 'best')
}
function filterSpec(): FilterSpec {
  return { keyword:appliedKeyword.value, chips:{...activeChip}, prices:JSON.parse(JSON.stringify(priceApplied)), groups:[...activeGroups.value], channel:CHAN[chanTab.value], broker:chanTab.value === 2 ? brokerPick.value : '' }
}
function scopeKey() { const binding = imBoundAccount.value; return JSON.stringify([filterSpec(), binding?.workbench_id, binding?.account_uid, binding?.version ?? 0]) }
function quoteBonds(before: number, uni = 0) {
  const filter = filterSpec()
  return api.bonds(before, PAGE, filter.groups, filter.channel, filter.broker, uni, filter)
}
function takePage(r: Awaited<ReturnType<typeof api.bonds>>, mode: 'replace' | 'more') {
  if (r.emptyReason) {
    bonds.value = []
    total.value = 0
    hasMore.value = false
    nextBefore = 0
    beforeUni.value = 0
    channelNote.value = r.emptyReason
    feedHint.value = ''
    reindex()
    return
  }
  channelNote.value = ''
  const rows = freezeRows(r.data).filter(hasRealName)
  bonds.value = rows
  nextBefore = r.nextBefore
  beforeUni.value = r.nextUni ?? 0
  total.value = r.total
  hasMore.value = r.hasMore
  reindex()
  feedHint.value = bonds.value.length ? '' : '当前筛选范围没有公共报价'
}
const activeGroups = computed<string[]>(() => groupsFromChips(activeChip))

/** 换订阅组:REST 重拉首屏 + WS 换组。debounce 300ms,避免连点 chip 打穿后端。 */
let filterTimer: ReturnType<typeof setTimeout> | null = null
function scheduleReload() {
  if (filterTimer) clearTimeout(filterTimer)
  filterTimer = setTimeout(() => { filterTimer = null; void reloadByFilter() }, 300)
}
watch(scopeKey, () => {
  generation++; loading.value = false
  if (hasChecked.value) message.info('筛选范围已变化，已清除原选择')
  clearSel(); pageIndex.value = 0; pageCursors.splice(0, pageCursors.length, {before:0,uni:0})
  scheduleReload()
}, { flush:'sync' })
function reloadNow() {
  if (filterTimer) { clearTimeout(filterTimer); filterTimer = null }
  return reloadByFilter()
}
function pickChip(title: string, i: number) {
  if (activeChip[title] === i) return
  activeChip[title] = i

}
function applyFilters() {
  appliedKeyword.value = keyword.value.trim()
  for (const k of PRICE_KEYS) {
    priceApplied[k].lo = priceDraft[k].lo.trim()
    priceApplied[k].hi = priceDraft[k].hi.trim()
  }
  return reloadNow()
}
function clearKeyword() {
  keyword.value = ''
  appliedKeyword.value = ''
}
function resetFilters() {
  resetFilterState({ keyword: keyword.value, chips: activeChip, prices: priceDraft })
  keyword.value = ''
  appliedKeyword.value = ''
  for (const k of PRICE_KEYS) {
    priceApplied[k].lo = ''
    priceApplied[k].hi = ''
  }
  return reloadNow()
}

async function reloadByFilter() {
  await loadFirst()
  blinking.clear()
  const next = quote?.subscribe(activeGroups.value, onBatch)
  unsub?.(); unsub = next ?? null
}
const CHAN_TABS = [
  { key: 'best', label: '最优' },
  { key: 'qt', label: 'QT意向' },
  { key: 'broker', label: '经纪商' },
  { key: 'mm', label: '做市商' },
]
const INTENT_MODES: [string, 'yield' | 'price' | 'bp'][] = [['收益率', 'yield'], ['估值±BP', 'bp']]

const SUBCOLS = [
  { label: '剩余期限', g: 0 }, { label: '中债推荐估值', g: 0 },
  { label: 'bid-vol', g: 1 }, { label: 'bid 底价', g: 1 }, { label: 'ofr 底价', g: 1 }, { label: 'ofr-vol', g: 1 },
  { label: '最优 bid 方', g: 2 }, { label: 'bid-vol', g: 2 }, { label: '最优 bid', g: 2 }, { label: '最优 ofr', g: 2 }, { label: 'ofr-vol', g: 2 }, { label: '最优 ofr 方', g: 2 },
]
function subColColor(g: number) { return g === 1 ? '#B01C24' : g === 2 ? '#7A5A2E' : '#6E6258' }
function subColBl(i: number) { return (i === 2 || i === 6) ? '1px solid rgba(176, 28, 36,.18)' : 'none' }

function tagStyle(t: string): [string, string] {
  const M: Record<string, [string, string]> = { 绿: ['rgba(46,125,79,.14)', '#1F6B3E'], 科创: ['rgba(30,95,180,.12)', '#1E5FB4'], 公募: ['rgba(198,141,88,.2)', '#7A5A2E'], 私募: ['rgba(110,98,88,.14)', '#5C5148'] }
  return M[t] || ['rgba(110,98,88,.14)', '#5C5148']
}
function sideColor(s: string): [string, string] {
  if (s.startsWith('经纪商')) return ['rgba(198,141,88,.18)', '#7A5A2E']
  if (s.startsWith('做市商')) return ['rgba(30,95,180,.1)', '#1E5FB4']
  if (s === 'X-Bond') return ['rgba(46,125,79,.12)', '#1F6B3E']
  return ['rgba(110,98,88,.12)', '#5C5148']
}

/* ---- 行情行(应用 intentMode / bpOverride / serverStatus) ---- */
function hasRealName(b: BondRow): boolean {
  const name = (b.name || '').trim()
  const code = (b.code || '').trim()
  return !!name && !name.startsWith('未入字典') && !!code && !code.startsWith('UNI') && code !== '—'
}
const rows = computed(() => bonds.value.filter(hasRealName).filter(b => rowMatchesQuoteFilter({
  ...b, status: serverStatus[b.code] || b.status,
}, {
  keyword: appliedKeyword.value, chips: activeChip, prices: priceApplied,
})).map(b => {
  const status = serverStatus[b.code] || b.status
  const val = parseFloat(String(b.val || '').split('/')[0])
  const kind = floorKind[b.code]
  const saved = floorValue[b.code]
  const ov = kind === 'bp' && saved != null ? saved : undefined
  let ib = b.ib, io = b.io
  const blank = (v: string) => !v || v === '—' || v === '--' || v === '-'
  if (kind === 'yield' && saved != null && Number.isFinite(saved)) {
    ib = saved.toFixed(2)
    io = saved.toFixed(2)
  } else if (ov != null && Number.isFinite(ov)) {
    if (Number.isFinite(val)) {
      ib = (val + ov / 100).toFixed(2)
      io = (val - ov / 100).toFixed(2)
    } else {
      const sign = ov >= 0 ? '+' : '−'
      const label = `估值${sign}${Math.abs(ov)}BP`
      if (blank(ib)) ib = label
      if (blank(io)) io = label
    }
  }
  const fmtBp = (v: string) => { if (v === '—') return '—'; const d = (parseFloat(v) - val) * 100; return `估值${d >= 0 ? '+' : '−'}${Math.abs(d).toFixed(1)}BP` }
  if (intentMode.value === 'bp') { ib = fmtBp(ib); io = fmtBp(io) }
  else if (intentMode.value === 'price') {
    const px = parseFloat(b.val.split('/')[1]), dur = parseFloat(b.dur) || 1
    const toPx = (v: string) => v === '—' ? '—' : (px - (parseFloat(v) - val) * dur).toFixed(4)
    ib = toPx(ib); io = toPx(io)
  }
  const obc = sideColor(b.obs), ooc = sideColor(b.oos), stc = STATUS_COLOR[status] || STATUS_COLOR['待询价']
  return { ...b, dispIb: ib, dispIo: io, status, obsBg: obc[0], obsColor: obc[1], oosBg: ooc[0], oosColor: ooc[1], stBg: stc[0], stColor: stc[1], stGlow: stc[2] }
}))

const checkedCount = computed(() => selectAll.value ? Math.max(0, total.value - except.size) : picked.size)
const hasChecked = computed(() => checkedCount.value > 0)
const allChecked = computed(() => selectAll.value && except.size === 0)

function toggle(code: string) {
  if (selectAll.value) { except.has(code) ? except.delete(code) : except.add(code) }
  else { picked.has(code) ? picked.delete(code) : picked.add(code) }
}
/** O(1):不遍历任何行 */
function toggleAll() {
  const on = !allChecked.value
  selectAll.value = on
  picked.clear(); except.clear()
}
function clearSel() { selectAll.value = false; picked.clear(); except.clear() }

/** 把当前选择打包成后端认的口径。全选发谓词(O(1)),否则发枚举。 */
function selection(): Selection {
  return selectAll.value
    ? { selectAll: true, groups: activeGroups.value, filter:filterSpec(), except: [...except] }
    : { windcodes: [...picked] }
}
/** 选中数:全选时是"当前筛选命中数 − 例外",不枚举 */
function selCount() { return selectAll.value ? Math.max(0, total.value - except.size) : picked.size }

const exporting = ref(false)
async function exportCurrent() {
  if (exporting.value) return
  const scope = scopeKey()
  const filter = filterSpec()
  exporting.value = true
  try {
    const file = await api.exportBonds(filter)
    if (feedStopped || scope !== scopeKey()) { message.info('筛选或账号已变化，请重新发起导出'); return }
    const url = URL.createObjectURL(file.blob)
    const anchor = document.createElement('a')
    anchor.href = url; anchor.download = file.filename
    document.body.appendChild(anchor); anchor.click(); anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { message.error(e instanceof Error ? e.message : '导出失败，未下载') }
  finally { exporting.value = false }
}

const qGrid = computed(() => sideOpen.value ? '292px 10px minmax(0,1fr)' : '10px minmax(0,1fr)')

const rfqOpen = ref(false)
const rfqBonds = ref<BondRow[]>([])
async function resolveSelected(): Promise<BondRow[] | null> {
  const key = scopeKey()
  const frozenSelection = JSON.stringify(selection())
  try {
    const r = await api.resolveBonds(selection())
    if (key !== scopeKey() || frozenSelection !== JSON.stringify(selection())) return null
    if (!r.ok) { message.error(r.hint || '选择范围解析失败'); return null }
    return r.data
  } catch (e) { message.error(e instanceof Error ? e.message : '选择范围解析失败'); return null }
}
async function startRfq() {
  const selected = await resolveSelected()
  if (!selected?.length) return
  rfqBonds.value = selected
  await refreshBusiness()
  rfqOpen.value = true
}
async function onRfqSent(_codes: string[]) { rfqOpen.value = false; await refreshBusiness() }
async function onRfqPending(_codes: string[]) { await refreshBusiness() }
async function onRfqFailed(_codes: string[]) { await refreshBusiness() }
async function stopRfq() {
  const r = await api.rfqStop(selection())
  if (!r.ok) { message.error(r.hint || '停止失败'); return }
  message.success(`停止请求已确认，共 ${r.data.count ?? selCount()} 只债券`)
  await refreshBusiness()
}
const floorMode = ref<'bp' | 'yield'>('bp')
const yieldText = ref('')
const floorKind = reactive<Record<string, 'bp' | 'yield'>>({})
const floorValue = reactive<Record<string, number>>({})
const savedFloors = computed(() => Object.fromEntries(Object.keys(floorValue).map(code => [code,{kind:floorKind[code],value:floorValue[code]}])))
const floorLabels = computed(() => {
  const out: Record<string, string> = {}
  for (const code of Object.keys(floorValue)) {
    const value = floorValue[code]
    if (value == null || Number.isNaN(value)) continue
    out[code] = floorKind[code] === 'yield'
      ? `绝对收益 ${value.toFixed(2)}`
      : `估值${value >= 0 ? '+' : ''}${value}BP`
  }
  return out
})
let businessGeneration = 0
async function refreshBusiness() {
  const request = ++businessGeneration
  const requestScope = scopeKey()
  try {
    const [status, floors] = await Promise.all([api.imRfqStatus(), api.getFloors()])
    if (request !== businessGeneration || requestScope !== scopeKey() || feedStopped) return
    if (status.ok) {
      for (const code of Object.keys(serverStatus)) delete serverStatus[code]
      for (const item of status.data?.items || []) serverStatus[item.code] = item.status as BondStatus
    }
    if (floors.ok) {
      for (const code of Object.keys(floorValue)) { delete floorValue[code]; delete floorKind[code] }
      const invalid: string[] = []
      for (const item of floors.data.items) {
        const raw: unknown = item.value
        const value = typeof raw === 'number' ? raw : typeof raw === 'string' && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw.trim()) ? Number(raw) : NaN
        if (!Number.isFinite(value) || !['bp', 'yield'].includes(item.kind)) { invalid.push(item.code); continue }
        floorKind[item.code] = item.kind; floorValue[item.code] = value
      }
      if (invalid.length) message.error(`底价数据含无效数值，未作为本批参考：${invalid.join('、')}`)
    }
    if (!status.ok || !floors.ok) message.error(status.hint || floors.hint || '业务状态刷新失败')
  } catch (e) { message.error(e instanceof Error ? e.message : '业务状态刷新失败') }
}
async function applyBp() {
  if (!hasChecked.value) { message.warning('先勾选要改底价的债'); return }
  if (floorMode.value === 'yield' && !yieldText.value.trim()) { message.error('绝对收益不能为空'); return }
  const value = floorMode.value === 'yield' ? Number(yieldText.value) : bpAdj.value
  if (!Number.isFinite(value)) { message.error('底价要写成数字'); return }
  const selected = await resolveSelected()
  if (!selected?.length) return
  try {
    const r = await api.setFloor(selected.map(b => b.code), value, floorMode.value)
    if (!r.ok) { message.error(r.hint || '底价没有写上'); return }
    await refreshBusiness()
    message.success(`已对 ${selected.length} 只债券保存底价`)
  } catch (e) { message.error(e instanceof Error ? e.message : '底价保存失败') }
}

/* ---------------- 悬浮卡:定位与显隐 ----------------
 * 四条规则:①悬在简称上→显示 ②悬在卡内→保持 ③简称→卡片途中→不消失 ④移到别处→消失
 *
 * 卡片保持 pointer-events:none(与设计稿一致)。不能给它加 auto:卡片 640×330 会盖住
 * 下方五六行,那些行就 hover 不到也点不了(实测撞到)。所以 ②③ 不走 DOM 事件,
 * 改用 document mousemove 按坐标判定:指针在 简称 / 卡片 / 两者之间的竖直通道 内就留着。*/
const TIP_W = 640
const TIP_H = 340 // 卡片实高约 331,留点余量
const TIP_GAP = 6 // 贴着简称,通道也窄
const TIP_PAD = 8 // 判定时把卡片四周放宽一点,容忍手抖
let tipTimer: ReturnType<typeof setTimeout> | null = null
let tipAnchor: HTMLElement | null = null
const tipEl = ref<HTMLElement | null>(null)

function cancelTipClose() { if (tipTimer) { clearTimeout(tipTimer); tipTimer = null } }
function closeTipNow() {
  cancelTipClose()
  tip.value = null
  tipAnchor = null
  document.removeEventListener('mousemove', onTipMove)
}
/** 延迟关:留给鼠标穿过缝隙的时间,途中若判定回到安全区会被撤销 */
function scheduleTipClose(ms = 120) {
  cancelTipClose()
  tipTimer = setTimeout(closeTipNow, ms)
}

function inRect(x: number, y: number, r: DOMRect, pad = 0) {
  return x >= r.left - pad && x <= r.right + pad && y >= r.top - pad && y <= r.bottom + pad
}

function onTipMove(e: MouseEvent) {
  if (!tip.value || !tipAnchor) return
  const ar = tipAnchor.getBoundingClientRect()
  if (inRect(e.clientX, e.clientY, ar, 2)) { cancelTipClose(); return } // ① 还在简称上
  const card = tipEl.value
  if (card) {
    const cr = card.getBoundingClientRect()
    if (inRect(e.clientX, e.clientY, cr, TIP_PAD)) { cancelTipClose(); return } // ② 在卡内
    // ③ 简称与卡片之间那条竖直通道(卡在下方或上方两种情况)
    const inX = e.clientX >= cr.left - TIP_PAD && e.clientX <= cr.right + TIP_PAD
    const band = cr.top >= ar.bottom
      ? e.clientY >= ar.bottom && e.clientY <= cr.top
      : e.clientY >= cr.bottom && e.clientY <= ar.top
    if (inX && band) { cancelTipClose(); return }
  }
  scheduleTipClose() // ④ 其他区域
}

function openModal(b: BondRow) { modalBond.value = b; closeTipNow() }

function showTip(e: MouseEvent, b: BondRow) {
  cancelTipClose() // 从一只券直接滑到另一只券:换内容,不闪
  const el = e.currentTarget as HTMLElement
  const r = el.getBoundingClientRect()
  let y = r.bottom + TIP_GAP
  if (y + TIP_H > window.innerHeight - 8) y = Math.max(8, r.top - TIP_H - TIP_GAP) // 下方放不下就翻上方
  const x = Math.max(8, Math.min(r.left, window.innerWidth - TIP_W - 8))
  tipAnchor = el
  tip.value = { bond: b, x, y }
  document.addEventListener('mousemove', onTipMove)
}
onUnmounted(closeTipNow)
const DASH = '—'
const or = (v: string | null | undefined) => (v && v !== 'undefined' ? v : DASH)

/** 悬浮卡九宫格,顺序与配色照设计稿 现券询价机器人.dc.html(6 列一行,9 项排 2 行)。
 *  中证估值 v1.0 已取消估值双源,格子保留、取不到显示 —,便于口径回切。 */
function tipMetrics(b: BondRow) {
  const d = b.valDate ? ` ${b.valDate}` : ''
  return [
    { k: '剩余期限', v: or(b.term), c: '#2B2320' },
    { k: '修正久期', v: or(b.dur), c: '#2B2320' },
    { k: '票面利率', v: or(b.coupon), c: '#B01C24' },
    { k: `中债估值${d}`, v: or(b.val), c: '#B01C24' },
    { k: `中证估值${d}`, v: or(b.zz), c: '#B01C24' },
    { k: '债项评级', v: or(b.issuerRating), c: '#2B2320' },
    { k: '隐含评级', v: b.implied ? b.implied + ' 中债' : DASH, c: '#2B2320' },
    { k: '内部担保额度', v: or(b.gLimit), c: '#1F6B3E' },
    { k: '非担保额度', v: or(b.ngLimit), c: '#1F6B3E' },
  ]
}
function tipIssuerMeta(b: BondRow) {
  return [
    { k: '主体评级', v: or(b.issuerRating) },
    { k: '行业', v: or(b.industry) },
    { k: '省', v: or(b.prov) },
    { k: '性质', v: or(b.nature) },
  ]
}
function onJump(cpId: string, bondName: string) {
  modalBond.value = null
  router.push({ path: '/monitor', query: { cp: cpId, bond: bondName } })
}
</script>

<template>
  <div class="quote-wrap fade-up">
    <ImLinkBar class="im-slot" :show-address-book="false" />
    <main class="board glass" :style="{ gridTemplateColumns: qGrid }">
      <!-- 筛选栏 -->
      <aside v-if="sideOpen" class="filter">
        <div class="f-head">
          <span class="f-title">方案筛选 / FILTER</span>
          <div class="f-search">
            <input class="ny-input" v-model="keyword" placeholder="债券代码 / 简称 / 发行人" @keydown.enter.prevent="applyFilters" />
            <button type="button" class="btn-ghost tiny" @click="clearKeyword">清空</button>
          </div>
        </div>

        <div class="f-body">
        <div v-for="g in FILTER_GROUPS" :key="g.title" class="f-group">
          <span class="f-label">{{ g.title }}</span>
          <div class="chips">
            <span v-for="(c, i) in g.chips" :key="c" class="chip" :class="{ on: activeChip[g.title] === i }" @click="pickChip(g.title, i)">{{ c }}</span>
          </div>
        </div>
        <div class="f-divider" />
        <div class="price-grid">
          <template v-for="p in PRICE_ROWS" :key="p[0]">
            <span class="p-label">{{ p[0] }}</span>
            <input class="ny-input mono" :placeholder="p[1]" v-model="priceDraft[p[0]].lo" @keydown.enter.prevent="applyFilters" />
            <span class="p-dash">–</span>
            <input class="ny-input mono" :placeholder="p[1]" v-model="priceDraft[p[0]].hi" @keydown.enter.prevent="applyFilters" />
          </template>
        </div>
        </div>

        <div class="f-foot">
          <div class="f-actions">
            <button type="button" class="btn-primary do" @click="applyFilters">筛选行情</button>
            <button type="button" class="btn-ghost reset" @click="resetFilters">重置</button>
          </div>
          <button class="save-scheme" disabled title="尚未接入筛选方案持久化">保存筛选为方案组（未开放）</button>
        </div>
      </aside>

      <!-- 折叠把手 -->
      <div class="handle" :title="sideOpen ? '收起筛选栏' : '展开筛选栏'" @click="sideOpen = !sideOpen">
        <svg :style="{ transform: `rotate(${sideOpen ? 0 : 180}deg)` }" width="8" height="12" viewBox="0 0 8 12" fill="#B01C24"><path d="M7 1 1 6l6 5Z" /></svg>
      </div>

      <!-- 主表 -->
      <section class="main-col">
        <div class="toolbar">
          <div class="tb-l">
            <button class="strat-btn" type="button" data-testid="quote-export" :disabled="exporting" @click="exportCurrent">{{ exporting ? '正在导出…' : '导出当前筛选 XLSX' }}</button>
            <button class="strat-btn" @click="strategyOpen = true">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M2 14h4M10 8h4M18 16h4" /></svg>
              询价策略
            </button>
            <div class="mode">
              <span class="mode-lb">价格形式</span>
              <div class="seg">
                <span v-for="im in INTENT_MODES" :key="im[1]" class="seg-i" :class="{ on: intentMode === im[1] }" @click="intentMode = im[1]">{{ im[0] }}</span>
              </div>
            </div>
          </div>
          <div class="tb-r">
            <span v-for="(t, i) in CHAN_TABS" :key="t.key" class="chan-tab" :class="{ on: chanTab === i }" :data-testid="'chan-' + t.key" @click="selectChan(i)">{{ t.label }}</span>
            <div v-if="chanTab === 2" ref="brokerMenu" class="broker-pick" data-testid="broker-select">
              <button type="button" class="broker-btn" data-testid="broker-current" @click.stop="brokerOpen = !brokerOpen">
                <span>{{ brokerLabel }}</span>
                <svg width="10" height="6" viewBox="0 0 10 6" fill="currentColor"><path d="M1 1l4 4 4-4" /></svg>
              </button>
              <div v-if="brokerOpen" class="broker-menu">
                <button type="button" class="broker-opt" :class="{ on: brokerPick === 'best' }" data-testid="broker-option" data-broker-id="best" @click="selectBroker('best')">经纪商最优</button>
                <button v-for="b in brokerList" :key="b.id" type="button" class="broker-opt" :class="{ on: brokerPick === String(b.id) }" data-testid="broker-option" :data-broker-id="b.id" @click="selectBroker(String(b.id))">{{ b.name }}</button>
              </div>
            </div>
          </div>
        </div>

        <div v-if="feedHint" class="feed-hint">{{ feedHint }}</div>
        <div ref="scroller" class="table-scroll" @scroll.passive="onScroll">
          <table class="mkt">
            <colgroup>
              <col style="width:260px"><col style="width:104px"><col style="width:150px">
              <col style="width:76px"><col style="width:92px"><col style="width:92px"><col style="width:76px">
              <col style="width:108px"><col style="width:76px"><col style="width:92px"><col style="width:92px">
              <col style="width:76px"><col style="width:108px"><col style="width:100px">
            </colgroup>
            <thead>
              <tr>
                <th rowspan="2" class="sticky-l group-name">
                  <div class="th-name">
                    <input type="checkbox" class="ny" :checked="allChecked" title="全选" @change="toggleAll" />
                    债券简称
                    <span v-if="hasChecked" class="sel-badge">已选 {{ checkedCount }}</span>
                  </div>
                </th>
                <th colspan="2" class="grp base">债券基本信息</th>
                <th colspan="4" class="grp intent">内部意向</th>
                <th colspan="6" class="grp ext">外部报价</th>
                <th rowspan="2" class="sticky-r">当前状态</th>
              </tr>
              <tr>
                <th v-for="(c, i) in SUBCOLS" :key="i" class="subcol" :style="{ color: subColColor(c.g), borderLeft: subColBl(i) }">{{ c.label }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="padTop" class="pad-row" :style="padTopStyle"><td :colspan="14" /></tr>
              <tr v-if="channelNote && !bonds.length" class="empty-row"><td colspan="14" class="chan-empty" data-testid="channel-note">{{ channelNote }}</td></tr>
              <tr v-for="r in visRows" :key="r.code" class="mkt-row" :class="[{ sel: isPicked(r.code) }, rowCls(r.code)]">
                <td class="sticky-l cell-name" :class="cellCls(r.code, 'name')">
                  <div class="name-cell">
                    <input type="checkbox" class="ny" :checked="isPicked(r.code)" @change="toggle(r.code)" />
                    <span v-for="t in r.tags" :key="t" class="mini-tag" :style="{ background: tagStyle(t)[0], color: tagStyle(t)[1] }">{{ t }}</span>
                    <span class="name" :title="r.quotedAt ? `报价时间 ${new Date(r.quotedAt).toLocaleString()}` : '报价时间未提供'" @mouseenter="showTip($event, r)" @mouseleave="scheduleTipClose()" @click="openModal(r)">{{ r.name }}</span>
                    <span v-if="r.isNew" class="new-badge">新</span>
                  </div>
                </td>
                <td class="muted" :title="r.term">{{ r.term }}</td>
                <td class="mono muted" :title="r.val">{{ r.val }}</td>
                <td class="mono muted intent-bl">{{ r.ibv }}</td>
                <td class="mono b red" :class="cellCls(r.code, 'ib')">{{ r.dispIb }}</td>
                <td class="mono b green" :class="cellCls(r.code, 'io')">{{ r.dispIo }}</td>
                <td class="mono muted">{{ r.iov }}</td>
                <td class="ext-bl"><span class="side-pill" :title="r.obs" :style="{ background: r.obsBg, color: r.obsColor }">{{ r.obs }}</span></td>
                <td class="mono muted">{{ r.obv }}</td>
                <td class="mono b red" :class="cellCls(r.code, 'ob')">{{ r.ob }}</td>
                <td class="mono b green" :class="cellCls(r.code, 'oo')">{{ r.oo }}</td>
                <td class="mono muted">{{ r.oov }}</td>
                <td><span class="side-pill" :title="r.oos" :style="{ background: r.oosBg, color: r.oosColor }">{{ r.oos }}</span></td>
                <td class="sticky-r status-cell"><span class="status" :style="{ background: r.stBg, color: r.stColor, boxShadow: '0 0 10px ' + r.stGlow }">{{ r.status }}</span></td>
              </tr>
              <tr v-if="padBottom" class="pad-row" :style="padBotStyle"><td :colspan="14" /></tr>
            </tbody>
          </table>
        <div class="load-more">
            <template v-if="loading"><span class="spin" />正在加载…</template>
            <template v-else-if="channelNote" />
            <template v-else-if="hasMore">
              本页 <b>{{ bonds.length }}</b> / <b>{{ total }}</b> 只<template v-if="rows.length !== bonds.length"> · 筛选后 <b>{{ rows.length }}</b> 只</template>
            </template>
            <template v-else>本页 <b>{{ bonds.length }}</b> / {{ total }} 只<template v-if="rows.length !== bonds.length"> · 筛选后 <b>{{ rows.length }}</b> 只</template><template v-if="liveCount"> · 其中实时灌入 <b>{{ liveCount }}</b> 只</template></template>
            <button class="btn-ghost" :disabled="loading || pageIndex === 0" @click="previousPage">上一页</button>
            <span>第 {{ pageIndex + 1 }} 页 · 每页 {{ PAGE }} 行</span>
            <button class="btn-ghost" :disabled="loading || !hasMore" @click="loadMore">下一页</button>
          </div>
        </div>
      </section>
    </main>

    <!-- 底部浮条 -->
    <div v-if="hasChecked" class="dock-wrap">
      <div class="dock pop-in">
        <span class="d-cnt">已选 <b class="mono">{{ checkedCount }}</b> 只</span>
        <span class="d-div" />
        <button class="btn-primary d-start" @click="startRfq">开始询价</button>
        <button class="btn-ghost d-stop" @click="stopRfq">停止询价</button>
        <span class="d-div" />
        <span class="d-lb">底价统一调整</span>
        <button class="btn-ghost d-apply" type="button" @click="floorMode = floorMode === 'bp' ? 'yield' : 'bp'">{{ floorMode === 'bp' ? '相对估值' : '绝对收益' }}</button>
        <div v-if="floorMode === 'bp'" class="bp-box">
          <span class="bp-base">估值</span>
          <button class="bp-btn" @click="bpAdj--">−</button>
          <span class="bp-val mono">{{ bpAdj >= 0 ? '+' : '−' }}{{ Math.abs(bpAdj) }} BP</span>
          <button class="bp-btn" @click="bpAdj++">+</button>
        </div>
        <input v-else v-model="yieldText" class="ny-input" placeholder="1.86" style="width:72px" />
        <button class="btn-ghost d-apply" @click="applyBp">应用到已选</button>
        <button class="d-clear" title="取消选择" @click="clearSel"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg></button>
      </div>
    </div>

    <!-- 悬浮详情卡 -->
    <Teleport to="body">
    <div v-if="tip" ref="tipEl" class="tip-card pop-in" :style="{ left: tip.x + 'px', top: tip.y + 'px' }">
      <div class="tip-hd">
        <b>{{ tip.bond.name }}</b>
        <span v-for="t in tip.bond.tags" :key="t" class="mini-tag" :style="{ background: tagStyle(t)[0], color: tagStyle(t)[1] }">{{ t }}</span>
      </div>
      <div class="tip-metrics">
        <div v-for="(m, i) in tipMetrics(tip.bond)" :key="i" class="tm">
          <div class="tm-k">{{ m.k }}</div>
          <div class="tm-v mono" :style="{ color: m.c }">{{ m.v }}</div>
        </div>
      </div>
      <div class="tip-issuer">
        <b>{{ tip.bond.issuer || '—' }}</b>
        <span v-for="im in tipIssuerMeta(tip.bond)" :key="im.k" class="im"><span class="im-k">{{ im.k }}</span><b>{{ im.v }}</b></span>
      </div>
      <div class="tip-sec">当前内部意向指令</div>
      <div class="tip-instr">
        <div class="ti bid">
          <div class="ti-hd">买方向 · BID</div>
          <div class="ti-bd">
            <div><div class="ti-k">投资经理</div><b>{{ or(tip.bond.buyMgr) }}</b></div>
            <div><div class="ti-k">报 bid</div><b class="mono px">{{ or(tip.bond.ib) }}</b></div>
            <div><div class="ti-k">bid 量(万)</div><b class="mono">{{ or(tip.bond.ibv) }}</b></div>
          </div>
        </div>
        <div class="ti ofr">
          <div class="ti-hd">卖方向 · OFR</div>
          <div class="ti-bd">
            <div><div class="ti-k">投资经理</div><b>{{ or(tip.bond.sellMgr) }}</b></div>
            <div><div class="ti-k">报 ofr</div><b class="mono px">{{ or(tip.bond.io) }}</b></div>
            <div><div class="ti-k">ofr 量(万)</div><b class="mono">{{ or(tip.bond.iov) }}</b></div>
          </div>
        </div>
      </div>
    </div>
    </Teleport>

    <PipelineStats :stat="stat" />
    <BondModal :bond="modalBond" @close="modalBond = null" @jump="onJump" />
    <StrategyModal :open="strategyOpen" :value="strategy" @close="strategyOpen = false" @saved="s => strategy = s" />
    <RfqAssignModal :saved-floors="savedFloors" :strategy="strategy.templateId ? strategy : undefined" :floors="floorLabels" :open="rfqOpen" :bonds="rfqBonds" :picked-count="checkedCount" @close="rfqOpen = false" @sent="onRfqSent" @pending="onRfqPending" @failed="onRfqFailed" />
  </div>
</template>

<style scoped>
/* 整屏吃满视口高度(顶栏 66px),表格在 .table-scroll 内部滚,
   否则 12 只券时下方会空一大片(安琳 2026-09-19 提) */
.quote-wrap { position: relative; height: calc(100vh - 66px); display: flex; flex-direction: column; min-height: 0; }
.im-slot { margin: 12px 24px 0; }
.board { flex: 1; min-height: 0; margin: 18px 24px 18px; border-radius: 18px; display: grid; align-items: stretch; overflow: hidden; box-shadow: 0 10px 36px rgba(43,35,32,.07); }

/* 筛选栏 */
/* 筛选栏三段:标题+搜索固定顶 / 条件区随窗高滚动 / 操作按钮固定底 */
.filter { padding: 14px 14px 16px; display: flex; flex-direction: column; gap: 11px; min-width: 0; min-height: 0; overflow: hidden; }
.f-head { flex: none; display: flex; flex-direction: column; gap: 11px; }
.f-body { flex: 1 1 auto; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 11px; padding-right: 4px; margin-right: -4px; }
.f-foot { flex: none; display: flex; flex-direction: column; gap: 11px; padding-top: 12px; border-top: 1px solid rgba(176, 28, 36,.12); }
.f-title { font-size: 12px; font-weight: 700; letter-spacing: 3px; color: #8A7F76; }
.f-search { display: flex; gap: 6px; }
.f-search .ny-input { flex: 1; }
.tiny { padding: 4px 10px; border-radius: 6px; font-size: 11.5px; white-space: nowrap; }
.f-group { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 6px; align-items: start; }
.f-label { font-size: 11.5px; color: #6E6258; padding-top: 4px; text-align: right; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { cursor: pointer; padding: 3px 8px; border-radius: 5px; font-size: 11.5px; line-height: 1.3; border: 1px solid rgba(176, 28, 36,.16); background: rgba(255,255,255,.6); color: #5C5148; transition: all .18s; white-space: nowrap; }
.chip:hover { border-color: rgba(176, 28, 36,.5); }
.chip.on { background: linear-gradient(135deg, #C2333B, #8E1620); color: #fff; border-color: transparent; font-weight: 600; }
.f-divider { height: 1px; background: rgba(176, 28, 36,.12); margin: 2px 0; }
.price-grid { display: grid; grid-template-columns: 52px 1fr 8px 1fr; gap: 5px 6px; align-items: center; }
.p-label { font-size: 11.5px; color: #6E6258; text-align: right; }
.p-dash { text-align: center; color: #B0A69B; font-size: 11px; }
.f-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 4px; }
.do { padding: 8px; border-radius: 8px; font-size: 12.5px; font-weight: 600; letter-spacing: 2px; }
.reset { padding: 7px; border-radius: 8px; font-size: 12.5px; letter-spacing: 2px; }
.save-scheme { cursor: pointer; padding: 7px; border-radius: 8px; font-size: 12px; color: #B01C24; background: rgba(176, 28, 36,.05); border: 1px dashed rgba(176, 28, 36,.4); transition: all .2s; }
.save-scheme:hover { background: rgba(176, 28, 36,.1); }

.handle { cursor: pointer; position: relative; width: 10px; display: flex; align-items: center; justify-content: center; background: #F6F1EC; transition: background .2s; }
.handle:hover { background: rgba(176, 28, 36,.12); }
.handle svg { transition: transform .25s; }

/* 主表列 */
.main-col { min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.toolbar { position: relative; z-index: 40; display: flex; align-items: center; justify-content: space-between; gap: 10px 12px; flex-wrap: wrap; padding: 12px 16px; background: #F6F1EC; border-bottom: 1px solid rgba(176, 28, 36,.14); }
.tb-l { display: flex; align-items: center; gap: 12px; min-width: 0; flex-wrap: wrap; }
.strat-btn { cursor: pointer; display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 7px; font-family: inherit; font-size: 12px; font-weight: 600; color: #B01C24; background: rgba(176, 28, 36,.07); border: 1px solid rgba(176, 28, 36,.3); transition: all .2s; }
.strat-btn:hover { background: rgba(176, 28, 36,.12); box-shadow: 0 0 12px rgba(176, 28, 36,.25); }
.mode { display: flex; align-items: center; gap: 6px; }
.mode-lb { font-size: 11.5px; color: #8A7F76; }
.seg { display: flex; gap: 2px; padding: 2px; border-radius: 7px; background: rgba(255,255,255,.7); border: 1px solid rgba(176, 28, 36,.14); }
.seg-i { cursor: pointer; padding: 3px 9px; border-radius: 5px; font-size: 11.5px; font-weight: 600; color: #8A7F76; transition: all .18s; }
.seg-i.on { background: rgba(176, 28, 36,.1); color: #B01C24; border: 1px solid rgba(176, 28, 36,.45); }
.tb-r { display: flex; gap: 4px; }
.chan-tab { cursor: pointer; padding: 5px 10px; border-radius: 6px; font-size: 11.5px; font-weight: 600; background: rgba(255,255,255,.6); color: #5C5148; border: 1px solid rgba(176, 28, 36,.14); transition: all .2s; }
.chan-tab.on { background: rgba(176, 28, 36,.1); color: #B01C24; border-color: rgba(176, 28, 36,.5); box-shadow: 0 0 12px rgba(176, 28, 36,.25); }
.tb-r { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.broker-pick { position: relative; z-index: 41; }
.broker-btn { cursor: pointer; min-width: 132px; height: 30px; padding: 0 10px; border-radius: 8px; display: inline-flex; align-items: center; justify-content: space-between; gap: 8px; font-family: inherit; font-size: 12.5px; font-weight: 650; color: #B01C24; background: #fff; border: 1px solid rgba(176, 28, 36,.35); box-shadow: 0 0 0 3px rgba(176, 28, 36,.06); }
.broker-menu { position: absolute; top: calc(100% + 6px); right: 0; z-index: 41; min-width: 168px; padding: 6px; border-radius: 12px; background: rgba(255,252,249,.98); border: 1px solid rgba(176, 28, 36,.18); box-shadow: 0 12px 32px rgba(43,35,32,.14); }
.broker-opt { display: block; width: 100%; text-align: left; margin: 0; padding: 8px 10px; border: 0; border-radius: 8px; background: transparent; cursor: pointer; font-family: inherit; font-size: 13px; color: #2B2320; }
.broker-opt:hover, .broker-opt.on { background: rgba(176, 28, 36,.08); color: #B01C24; font-weight: 650; }
.chan-empty { height: 160px; text-align: center; color: #8A7F76; font-size: 13.5px; }

.table-scroll { overflow: auto; flex: 1; min-height: 0; overflow-anchor: none; }
/* fixed 布局:列宽由 colgroup 定死,O(1);auto 布局会因虚拟滚动换行而抖动 */
.mkt { width: 100%; table-layout: fixed; border-collapse: separate; border-spacing: 0; font-size: 13px; min-width: 1502px; }
.mkt th, .mkt td { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pad-row td { padding: 0; border: none; background: transparent; }
.grp { position: sticky; top: 0; z-index: 6; text-align: left; padding: 8px 14px; font-size: 11.5px; font-weight: 700; letter-spacing: 2px; background: #F6F1EC; border-bottom: 1px solid rgba(176, 28, 36,.1); }
.grp.base { color: #8A7F76; font-weight: 600; }
.grp.intent { color: #B01C24; border-left: 1px solid rgba(176, 28, 36,.18); }
.grp.ext { color: #7A5A2E; border-left: 1px solid rgba(176, 28, 36,.18); }
.subcol { position: sticky; top: 36px; z-index: 6; text-align: left; padding: 8px; font-size: 12px; font-weight: 600; background: #F6F1EC; border-bottom: 1px solid rgba(176, 28, 36,.16); }
.sticky-l { position: sticky; left: 0; top: 0; z-index: 8; background: #F6F1EC; text-align: left; padding: 11px 16px; font-size: 12.5px; font-weight: 700; color: #B01C24; letter-spacing: 1px; border-bottom: 1px solid rgba(176, 28, 36,.16); border-left: 1px solid rgba(176, 28, 36,.16); border-right: 1px solid rgba(176, 28, 36,.16); min-width: 260px; border-right: 1px solid rgba(176, 28, 36,.18); }
.sticky-r { position: sticky; right: 0; top: 0; z-index: 8; background: #F6F1EC; text-align: center; padding: 11px 16px; font-size: 12.5px; font-weight: 700; color: #B01C24; letter-spacing: 1px; border-bottom: 1px solid rgba(176, 28, 36,.16); border-left: 1px solid rgba(176, 28, 36,.16); min-width: 96px; border-left: 1px solid rgba(176, 28, 36,.18); }
.th-name { display: flex; align-items: center; gap: 10px; }
.sel-badge { font-size: 11px; font-weight: 600; color: #fff; background: #B01C24; padding: 1px 7px; border-radius: 999px; }

.mkt-row:hover { transition: background .18s ease; }
.mkt-row:hover { background: rgba(176, 28, 36,.055); }
.mkt-row.fresh { background: rgba(176, 28, 36,.035); }
.mkt-row.sel { background: rgba(176, 28, 36,.07); }
.mkt td { height: 42px; padding: 0 8px; border-bottom: 1px solid rgba(176, 28, 36,.08); }
.cell-name { z-index: 3; background: #FBF9F6; padding: 11px 16px; border-left: 1px solid rgba(176, 28, 36,.16); border-right: 1px solid rgba(176, 28, 36,.14); border-right: 1px solid rgba(176, 28, 36,.14); }
.mkt-row.sel .cell-name, .mkt-row.sel .status-cell { background: #F5E7E4; }
.mkt-row.fresh .cell-name, .mkt-row.fresh .status-cell { background: #FAF2F0; }
.status-cell { z-index: 3; background: #FBF9F6; text-align: center; border-left: 1px solid rgba(176, 28, 36,.14); border-left: 1px solid rgba(176, 28, 36,.14); }
.name-cell { min-width: 0; display: flex; align-items: center; gap: 6px; }
.mini-tag { flex: none; padding: 1px 6px; border-radius: 5px; font-size: 10.5px; font-weight: 700; letter-spacing: .5px; }
.name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; font-weight: 700; color: #B01C24; border-bottom: 1px dashed rgba(176, 28, 36,.4); transition: all .2s; }
.name:hover { color: #C2333B; text-shadow: 0 0 12px rgba(176, 28, 36,.35); }
.new-badge { padding: 1px 6px; border-radius: 999px; font-size: 10px; font-weight: 700; color: #fff; background: linear-gradient(135deg, #C2333B, #8E1620); box-shadow: 0 0 10px rgba(176, 28, 36,.5); }
.mono { font-family: var(--font-ui); font-size: 12.5px; }
.muted { color: #6E6258; }
.b { font-weight: 700; font-size: 13px; }
.red { color: #B01C24; }
.green { color: #1F6B3E; }
.intent-bl { border-left: 1px solid rgba(176, 28, 36,.12); }
.ext-bl { border-left: 1px solid rgba(176, 28, 36,.12); }
.side-pill { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; vertical-align: middle; padding: 2px 8px; border-radius: 6px; font-size: 11.5px; font-weight: 600; }
.status { padding: 4px 11px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }

/* 浮条 */
.dock-wrap { position: fixed; left: 0; right: 0; bottom: 18px; z-index: 85; display: flex; justify-content: center; pointer-events: none; padding: 0 24px; }
.dock { pointer-events: auto; max-width: 100%; padding: 10px 12px 10px 16px; border-radius: 18px; background: rgba(250,248,244,.86); backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px); border: 1px solid rgba(176, 28, 36,.2); box-shadow: 0 14px 40px rgba(43,35,32,.16), 0 0 24px rgba(176, 28, 36,.18); display: flex; gap: 14px; align-items: center; flex-wrap: wrap; justify-content: center; }
.d-cnt { font-size: 13px; color: #6E6258; }
.d-cnt b { color: #B01C24; font-size: 15px; }
.d-div { width: 1px; height: 26px; background: rgba(176, 28, 36,.16); }
.d-start { padding: 9px 20px; border-radius: 11px; font-size: 13px; font-weight: 600; letter-spacing: 2px; }
.d-stop, .d-apply { padding: 8px 16px; border-radius: 11px; font-size: 13px; font-weight: 600; }
.d-lb { font-size: 12.5px; color: #6E6258; }
.bp-box { display: flex; align-items: center; gap: 6px; padding: 4px 6px 4px 12px; border-radius: 11px; background: rgba(255,255,255,.8); border: 1px solid rgba(176, 28, 36,.18); }
.bp-base { font-size: 13px; color: #2B2320; }
.bp-btn { cursor: pointer; width: 26px; height: 26px; border-radius: 7px; border: 1px solid rgba(176, 28, 36,.25); background: rgba(255,255,255,.9); color: #B01C24; font-size: 16px; line-height: 1; display: grid; place-items: center; transition: all .18s; }
.bp-btn:hover { background: rgba(176, 28, 36,.1); }
.bp-val { font-weight: 700; font-size: 15px; color: #B01C24; min-width: 54px; text-align: center; }
.d-clear { cursor: pointer; width: 30px; height: 30px; border-radius: 50%; border: 1px solid rgba(176, 28, 36,.2); background: rgba(255,255,255,.7); color: #8A7F76; display: grid; place-items: center; transition: all .2s; }
.d-clear:hover { background: rgba(176, 28, 36,.08); color: #B01C24; }

.feed-hint { margin: 0 0 8px; padding: 8px 12px; border-radius: 8px; font-size: 12.5px; color: #B01C24; background: rgba(176, 28, 36,.08); border: 1px solid rgba(176, 28, 36,.16); }
.load-more { position: sticky; left: 0; display: flex; align-items: center; justify-content: center; gap: 8px; padding: 16px 12px 20px; font-size: 12.5px; color: #8A7F76; }
.load-more b { color: #B01C24; font-family: var(--mono); }
.spin { width: 14px; height: 14px; border-radius: 50%; border: 2px solid rgba(176, 28, 36,.2); border-top-color: #B01C24; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* 变化闪烁 —— 只闪变化的单元格,且走合成层。
 *
 * 实测教训(300/s,CDP Performance):用 background-color 做整行闪烁,
 * CPU 12.9% / 样式重算 4.85%;关掉后 8.3% / 1.0% —— 闪烁一项就吃掉 4.6 个百分点。
 * 原因:background-color 不可合成,每帧都要重绘;整行 15 个 td 一起动更是放大 15 倍。
 *
 * 改法:① 不再整行闪,只闪真正变了的格子
 *      ② 用 ::after 叠一层,只动 opacity —— opacity 由合成器接管,不进主线程
 *      ③ 用 contain: paint 把重绘范围锁死在单元格内 */
.mkt td.chg { position: relative; contain: paint; }
.mkt td.chg::after {
  content: ''; position: absolute; inset: 0; pointer-events: none;
  background: rgba(176, 28, 36,.22); opacity: 0;
  animation: cellFade .9s ease-out;
}
.mkt td.chg.up::after { background: rgba(31,107,62,.24); }
/* 新报价样式(安琳 2026-09-21:参考 Choice)—— 更新过的行整行浅红底保留 5s,变动的格子右上角红三角。
 * 都是加/摘 class 时各重绘一次的静态样式,没有逐帧动画;sticky 首末列本身有底色,单独覆盖。 */
.mkt tbody tr.row-chg > td { background-color: #FBEDEA; }
.mkt tbody tr.row-chg.up > td { background-color: #EDF6EF; }
.mkt tbody tr.row-chg > td.cell-name, .mkt tbody tr.row-chg > td.status-cell { background-color: #F8E3DF; }
.mkt tbody tr.row-chg.up > td.cell-name, .mkt tbody tr.row-chg.up > td.status-cell { background-color: #E3F1E7; }
.mkt tbody td:not(.sticky-l):not(.sticky-r) { position: relative; }
.mkt td.chg::before {
  content: ''; position: absolute; top: 0; right: 0; pointer-events: none;
  border-style: solid; border-width: 0 7px 7px 0; border-color: transparent #C8322B transparent transparent;
}
.mkt td.chg.up::before { border-color: transparent #1F6B3E transparent transparent; }
/* 首列是 sticky,不能被 .chg 的 position:relative 覆盖(否则横滚时首列不再冻结);sticky 本身就能当 ::after 的定位容器 */
.mkt td.cell-name.chg { position: sticky; }
@keyframes cellFade { 0% { opacity: 1; } 100% { opacity: 0; } }
/* 拖滚动条时不闪,否则满屏乱跳还白烧 */
.table-scroll.scrolling .mkt td.chg::after { animation: none; }

/* 悬浮卡 —— 逐值对照设计稿 现券询价机器人.dc.html 的 hasTip 块,勿随手改数值 */
.tip-card { position: fixed; z-index: 150; width: 640px; pointer-events: none; border-radius: 16px; padding: 16px 18px; background: rgba(255,255,255,.9); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border: 1px solid rgba(176, 28, 36,.22); box-shadow: 0 18px 50px rgba(43,35,32,.18), 0 0 28px rgba(176, 28, 36,.18); }
.tip-hd { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tip-hd b { font-size: 15px; color: #B01C24; }
.tip-hd .mono { font-size: 12px; color: #8A7F76; }
.tip-card .mini-tag { padding: 1px 7px; border-radius: 5px; font-size: 10.5px; font-weight: 700; }
.tip-metrics { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px 14px; margin-top: 12px; }
.tm-k { font-size: 11px; color: #8A7F76; white-space: nowrap; }
.tm-v { font-size: 13.5px; font-weight: 700; margin-top: 3px; white-space: nowrap; }
.tip-issuer { display: flex; align-items: center; gap: 14px; margin-top: 12px; padding-top: 10px; border-top: 1px dashed rgba(176, 28, 36,.18); font-size: 12px; color: #6E6258; flex-wrap: wrap; }
.tip-issuer > b { color: #2B2320; font-size: 13px; }
.tip-issuer .im-k { color: #A89C90; margin-right: 4px; }
.tip-issuer .im b { color: #2B2320; }
.tip-sec { margin-top: 12px; font-size: 11.5px; font-weight: 600; letter-spacing: 1px; color: #8A7F76; }
.tip-instr { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 6px; }
.ti { border-radius: 10px; overflow: hidden; }
.ti.bid { border: 1px solid rgba(176, 28, 36,.2); }
.ti.ofr { border: 1px solid rgba(46,125,79,.3); }
.ti-hd { color: #fff; text-align: center; font-weight: 700; padding: 5px; font-size: 12.5px; letter-spacing: 2px; }
.ti.bid .ti-hd { background: linear-gradient(135deg, #C2333B, #8E1620); }
.ti.ofr .ti-hd { background: linear-gradient(135deg, #2E8B57, #1F6B3E); }
.ti-bd { display: grid; grid-template-columns: 1fr 1fr 1fr; text-align: center; padding: 8px 6px; font-size: 12px; }
.ti.bid .ti-bd { background: rgba(176, 28, 36,.04); }
.ti.ofr .ti-bd { background: rgba(46,125,79,.05); }
.ti-k { color: #A89C90; font-size: 10.5px; }
.ti.bid .px { color: #B01C24; }
.ti.ofr .px { color: #1F6B3E; }

</style>
