/** 意向询价筛选。纯函数,不连行情、不发消息。 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  FILTER_GROUPS, PRICE_KEYS,
  defaultFilterState, filterBondRows, groupsFromChips, resetFilterState,
  type FilterableBond,
} from '../src/lib/quoteFilter.ts'

const ROWS: FilterableBond[] = [
  { name: '22润泽MTN001', code: '102280111.IB', issuer: '润泽集团', ob: '2.80', oo: '2.70', obv: '2000', oov: '1000', val: '2.75/100.10', firmness: '实盘', tags: ['公募'], bondType: '中票', term: '2.10Y', clearing: '+0', scheme: 'Z1' },
  { name: '24国开02', code: '240202.IB', issuer: '国家开发银行', ob: '1.90', oo: '1.80', obv: '3000', oov: '3000', val: '1.85/101.00', firmness: '意向价', tags: ['公募'], bondType: '政金债', term: '7.20Y', clearing: '+1' },
  { name: '26杭州银行CD036', code: '112610036.IB', issuer: '杭州银行股份有限公司', ob: '1.70', oo: '1.65', obv: '5000', oov: '2000', val: '1.68/99.80', firmness: '可部分执行', tags: [], bondType: '存单', term: '0.40Y', clearing: '+0' },
  { name: '23普通债', code: '22润001.IB', issuer: '其他发行人', ob: '3.10', oo: '3.00', obv: '1000', oov: '1000', val: '3.00/100.00', firmness: '实盘', tags: ['私募'], bondType: '公司债', term: '3.50Y', clearing: '+N' },
  { name: '普通公司债', code: '102300001.IB', issuer: '22润发集团', ob: '2.20', oo: '2.10', obv: '4000', oov: '4000', val: '2.15/100.20', firmness: '实盘', tags: ['绿色债'], bondType: '公司债', term: '4.00Y', clearing: '+1', scheme: 'D1' },
]

test('关键字「22润」不会留下名称和代码都不含它的券', () => {
  const state = defaultFilterState()
  state.keyword = '22润'
  const out = filterBondRows(ROWS, state)
  assert.ok(out.length > 0)
  for (const row of out) {
    const name = row.name || ''
    const code = row.code || ''
    const issuer = row.issuer || ''
    const byNameOrCode = name.includes('22润') || code.includes('22润')
    const byIssuer = issuer.includes('22润')
    assert.ok(byNameOrCode || byIssuer, `${name} ${code} 不应留下`)
    if (!byNameOrCode) assert.ok(byIssuer)
  }
  assert.equal(out.some(r => r.name === '22润泽MTN001'), true)
  assert.equal(out.some(r => r.code === '22润001.IB'), true)
  assert.equal(out.some(r => r.issuer === '22润发集团'), true)
  assert.equal(out.some(r => r.name === '24国开02'), false)
  assert.equal(out.some(r => r.name === '26杭州银行CD036'), false)
  assert.equal(out.some(r => !(r.name || '').includes('22润') && !(r.code || '').includes('22润') && !(r.issuer || '').includes('22润')), false)
})

test('重置后条件清空', () => {
  const state = defaultFilterState()
  state.keyword = '22润'
  state.chips['方案'] = 1
  state.chips['报价类型'] = 1
  state.chips['清算'] = 2
  state.chips['标签'] = 2
  state.chips['类型'] = FILTER_GROUPS.find(g => g.title === '类型')!.chips.indexOf('次级债')
  state.chips['期限'] = 1
  state.prices.Bid.lo = '1.5'
  state.prices.Bid.hi = '3'
  state.prices['卖量'].hi = '3000'
  const narrowed = filterBondRows(ROWS, state)
  assert.equal(narrowed.some(r => r.name === '24国开02'), false)

  resetFilterState(state)

  assert.equal(state.keyword, '')
  for (const g of FILTER_GROUPS) assert.equal(state.chips[g.title], 0, g.title)
  for (const k of PRICE_KEYS) {
    assert.equal(state.prices[k].lo, '', k)
    assert.equal(state.prices[k].hi, '', k)
  }
  assert.deepEqual(groupsFromChips(state.chips), ['bond-all'])
  const restored = filterBondRows(ROWS, state)
  assert.equal(restored.some(r => r.name === '24国开02'), true)
  assert.equal(restored.some(r => r.name === '26杭州银行CD036'), true)
})

test('方案报价类型标签不进订阅组,选了就要滤行', () => {
  const state = defaultFilterState()
  state.chips['方案'] = FILTER_GROUPS.find(g => g.title === '方案')!.chips.indexOf('Z1')
  assert.deepEqual(groupsFromChips(state.chips), ['bond-all'])
  const z1 = filterBondRows(ROWS, state)
  assert.deepEqual(z1.map(r => r.name), ['22润泽MTN001'])

  const firm = defaultFilterState()
  firm.chips['报价类型'] = FILTER_GROUPS.find(g => g.title === '报价类型')!.chips.indexOf('实盘')
  const firmRows = filterBondRows(ROWS, firm)
  assert.ok(firmRows.length > 0)
  assert.ok(firmRows.every(r => r.firmness === '实盘'))
  assert.equal(firmRows.some(r => r.name === '24国开02'), false)

  const tag = defaultFilterState()
  tag.chips['标签'] = FILTER_GROUPS.find(g => g.title === '标签')!.chips.indexOf('绿色债')
  const tagged = filterBondRows(ROWS, tag)
  assert.deepEqual(tagged.map(r => r.name), ['普通公司债'])
})

test('价格区间空着不限制,填了按 Bid 收益率收窄', () => {
  const state = defaultFilterState()
  state.prices.Bid.lo = '2.5'
  state.prices.Bid.hi = '3.5'
  const out = filterBondRows(ROWS, state)
  assert.deepEqual(out.map(r => r.name).sort(), ['22润泽MTN001', '23普通债'].sort())
})

test('次级债走订阅组', () => {
  const state = defaultFilterState()
  state.chips['类型'] = FILTER_GROUPS.find(g => g.title === '类型')!.chips.indexOf('次级债')
  state.chips['期限'] = FILTER_GROUPS.find(g => g.title === '期限')!.chips.indexOf('1–3Y')
  state.chips['清算'] = FILTER_GROUPS.find(g => g.title === '清算')!.chips.indexOf('+0')
  assert.deepEqual(groupsFromChips(state.chips), ['bond-type_次级债', 'bond-tenor_1to3', 'bond-clearing_T0'])
})

test('当前状态筛发送失败时待询价不留下', () => {
  const state = defaultFilterState()
  state.chips['当前状态'] = FILTER_GROUPS.find(g => g.title === '当前状态')!.chips.indexOf('发送失败')
  const rows = filterBondRows([
    { name: '甲', code: '1.IB', status: '发送失败' },
    { name: '乙', code: '2.IB', status: '待询价' },
  ], state)
  assert.deepEqual(rows.map(r => r.code), ['1.IB'])
})
