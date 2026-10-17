/** 意向询价左侧筛选。
 *  类型 / 期限 / 清算能翻成订阅组,仍交给 groups(REST 跨维度 AND,WS 换组)。
 *  关键字、价格区间、方案、报价类型、标签在已加载行上收窄。
 *  行上若带了券种 / 剩余期限 / 清算,这三项再收一次,避免订阅是超集时漏网。 */

export interface FilterableBond {
  name?: string
  code?: string
  status?: string
  issuer?: string
  term?: string
  val?: string
  ob?: string
  oo?: string
  obv?: string
  oov?: string
  tags?: string[] | string
  bondType?: string
  firmness?: string
  clearing?: string
  scheme?: string
}

export interface PriceBounds {
  lo: string
  hi: string
}

export type PriceMap = Record<string, PriceBounds>

export interface QuoteFilterState {
  keyword: string
  chips: Record<string, number>
  prices: PriceMap
}

export const FILTER_GROUPS: { title: string; chips: string[] }[] = [
  { title: '当前状态', chips: ['全部', '待询价', '询价中', '待回执', '发送失败', '转人工', '已报价', '还价中', '待人工确认', '已成交', '已暂停', '未成交', '已撤价', '已放弃', '待核对', '停止中'] },
  { title: '方案', chips: ['默认方案', 'Z1', 'D1', 'D3', '习情', '我的关注'] },
  { title: '报价类型', chips: ['全部', '实盘', '可部分执行', '意向价'] },
  { title: '清算', chips: ['全部', '+0', '+1', '+N'] },
  { title: '标签', chips: ['全部', '绿色债', '科创债', '公募', '私募'] },
  { title: '类型', chips: ['全部信用', '国债', '政金债', '中票', '短融', 'PPN', '公司债', '企业债', 'ABS', '次级债'] },
  { title: '期限', chips: ['不限', '<1Y', '1–3Y', '3–5Y', '5–7Y', '7–10Y', '10Y+'] },
]

export const PRICE_KEYS = ['Bid', 'Ofr', 'Bid-中债', '中债-Ofr', '买量', '卖量'] as const
export const PRICE_ROWS: [string, string][] = [
  ['Bid', '%'], ['Ofr', '%'], ['Bid-中债', 'BP'], ['中债-Ofr', 'BP'], ['买量', '万'], ['卖量', '万'],
]

/** 后端 _group_predicate 认这些维度。次级债同样是 bond-type_*,能走订阅组。 */
const TYPE_GROUP: Record<string, string> = {
  国债: 'bond-type_国债', 政金债: 'bond-type_政金债', 中票: 'bond-type_中票', 短融: 'bond-type_短融',
  PPN: 'bond-type_PPN', 公司债: 'bond-type_公司债', 企业债: 'bond-type_企业债', ABS: 'bond-type_ABS',
  次级债: 'bond-type_次级债',
}
const TENOR_GROUP: Record<string, string> = {
  '<1Y': 'bond-tenor_lt1', '1–3Y': 'bond-tenor_1to3', '3–5Y': 'bond-tenor_3to5',
  '5–7Y': 'bond-tenor_5to7', '7–10Y': 'bond-tenor_7to10', '10Y+': 'bond-tenor_gt10',
}
const CLEAR_GROUP: Record<string, string> = { '+0': 'bond-clearing_T0', '+1': 'bond-clearing_T1', '+N': 'bond-clearing_TN' }

const TAG_ALIAS: Record<string, string[]> = {
  绿色债: ['绿色债', '绿'],
  科创债: ['科创债', '科创'],
  公募: ['公募'],
  私募: ['私募'],
}

/** 剩余期限档,边界与 mock/db.py TENOR_BUCKETS 对齐:左闭右开,10Y+ 含 10。 */
const TENOR_RANGE: Record<string, [number, number]> = {
  '<1Y': [0, 1],
  '1–3Y': [1, 3],
  '3–5Y': [3, 5],
  '5–7Y': [5, 7],
  '7–10Y': [7, 10],
  '10Y+': [10, Number.POSITIVE_INFINITY],
}

export function emptyPrices(): PriceMap {
  const out: PriceMap = {}
  for (const k of PRICE_KEYS) out[k] = { lo: '', hi: '' }
  return out
}

export function defaultFilterState(): QuoteFilterState {
  return {
    keyword: '',
    chips: Object.fromEntries(FILTER_GROUPS.map(g => [g.title, 0])),
    prices: emptyPrices(),
  }
}

/** 恢复全部 chip、关键字和价格区间。调用方再按 bond-all 重新拉。 */
export function resetFilterState(state: QuoteFilterState): void {
  state.keyword = ''
  for (const g of FILTER_GROUPS) state.chips[g.title] = 0
  for (const bounds of Object.values(state.prices)) {
    bounds.lo = ''
    bounds.hi = ''
  }
}

export function chipValue(chips: Record<string, number>, title: string): string {
  const group = FILTER_GROUPS.find(g => g.title === title)
  if (!group) return ''
  return group.chips[chips[title] ?? 0] ?? ''
}

/** 只读类型 / 期限 / 清算。方案、报价类型、标签没有对应组名。 */
export function groupsFromChips(chips: Record<string, number>): string[] {
  const g: string[] = []
  const pick = (title: string, map: Record<string, string>) => {
    const v = chipValue(chips, title)
    if (v && map[v]) g.push(map[v])
  }
  pick('类型', TYPE_GROUP)
  pick('期限', TENOR_GROUP)
  pick('清算', CLEAR_GROUP)
  return g.length ? g : ['bond-all']
}

function numOf(raw: string | number | null | undefined): number | null {
  if (raw == null) return null
  const t = String(raw).trim().replace(/,/g, '').replace(/万/g, '').replace(/%/g, '').replace(/\s/g, '')
  if (!t || t === '—' || t === '-' || t === '–' || t === '+') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

function inRange(value: number | null, bounds: PriceBounds | undefined): boolean {
  if (!bounds) return true
  const lo = numOf(bounds.lo)
  const hi = numOf(bounds.hi)
  if (lo == null && hi == null) return true
  if (value == null) return false
  if (lo != null && value < lo) return false
  if (hi != null && value > hi) return false
  return true
}

function valYield(row: FilterableBond): number | null {
  const head = (row.val || '').split('/')[0]
  return numOf(head)
}

function bidCnbdBp(row: FilterableBond): number | null {
  const bid = numOf(row.ob)
  const y = valYield(row)
  if (bid == null || y == null) return null
  return (bid - y) * 100
}

function cnbdOfrBp(row: FilterableBond): number | null {
  const ofr = numOf(row.oo)
  const y = valYield(row)
  if (ofr == null || y == null) return null
  return (y - ofr) * 100
}

function matchKeyword(row: FilterableBond, keyword: string): boolean {
  const q = keyword.trim().toLowerCase()
  if (!q) return true
  return [row.name, row.code, row.issuer].some(s => (s || '').toLowerCase().includes(q))
}

function matchScheme(row: FilterableBond, chip: string): boolean {
  if (!chip || chip === '默认方案') return true
  return (row.scheme || '').trim() === chip
}

function matchFirmness(row: FilterableBond, chip: string): boolean {
  if (!chip || chip === '全部') return true
  return (row.firmness || '').trim() === chip
}

function tagList(row: FilterableBond): string[] {
  const raw = row.tags
  if (Array.isArray(raw)) return raw.map(t => String(t).trim()).filter(Boolean)
  if (typeof raw === 'string' && raw.trim().startsWith('[')) {
    try {
      const parsed = JSON.parse(raw) as unknown
      if (Array.isArray(parsed)) return parsed.map(t => String(t).trim()).filter(Boolean)
    } catch { /* 坏 JSON 当没有标签 */ }
  }
  return []
}

function matchTag(row: FilterableBond, chip: string): boolean {
  if (!chip || chip === '全部') return true
  const aliases = TAG_ALIAS[chip] || [chip]
  const tags = tagList(row)
  if (tags.length) return tags.some(t => aliases.includes(t))
  const name = row.name || ''
  return aliases.some(a => name.includes(a))
}

function matchType(row: FilterableBond, chip: string): boolean {
  if (!chip || chip === '全部信用' || chip === '全部') return true
  const t = (row.bondType || '').trim()
  if (!t) return true
  return t === chip
}

function tenorYears(term: string | undefined): number | null {
  if (!term) return null
  const m = term.match(/(\d+(?:\.\d+)?)\s*Y/i)
  if (!m) return null
  const n = Number(m[1])
  return Number.isFinite(n) ? n : null
}

function matchTenor(row: FilterableBond, chip: string): boolean {
  if (!chip || chip === '不限' || chip === '全部') return true
  const range = TENOR_RANGE[chip]
  if (!range) return true
  const y = tenorYears(row.term)
  if (y == null) return true
  return y >= range[0] && y < range[1]
}

function matchStatus(row: FilterableBond, chip: string): boolean {
  if (!chip || chip === '全部') return true
  return (row.status || '待询价') === chip
}

function matchClearing(row: FilterableBond, chip: string): boolean {
  if (!chip || chip === '全部') return true
  const c = (row.clearing || '').trim()
  if (!c) return true
  return c === chip
}

export function rowMatchesQuoteFilter(row: FilterableBond, state: QuoteFilterState): boolean {
  if (!matchKeyword(row, state.keyword)) return false
  const prices = state.prices
  if (!inRange(numOf(row.ob), prices.Bid)) return false
  if (!inRange(numOf(row.oo), prices.Ofr)) return false
  if (!inRange(bidCnbdBp(row), prices['Bid-中债'])) return false
  if (!inRange(cnbdOfrBp(row), prices['中债-Ofr'])) return false
  if (!inRange(numOf(row.obv), prices['买量'])) return false
  if (!inRange(numOf(row.oov), prices['卖量'])) return false
  const chips = state.chips
  if (!matchScheme(row, chipValue(chips, '方案'))) return false
  if (!matchFirmness(row, chipValue(chips, '报价类型'))) return false
  if (!matchTag(row, chipValue(chips, '标签'))) return false
  if (!matchType(row, chipValue(chips, '类型'))) return false
  if (!matchTenor(row, chipValue(chips, '期限'))) return false
  if (!matchClearing(row, chipValue(chips, '清算'))) return false
  if (!matchStatus(row, chipValue(chips, '当前状态'))) return false
  return true
}

export function filterBondRows<T extends FilterableBond>(rows: readonly T[], state: QuoteFilterState): T[] {
  return rows.filter(row => rowMatchesQuoteFilter(row, state))
}
