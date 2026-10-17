/* 意向询价页 · 真实数据源(QB)端到端 + 渲染性能测试(Playwright + CDP)。
 *
 * 前置:后端 XUNJIA_QUOTE_SOURCE=qb 跑在 8120,前端 vite 跑在 5190,DM 页面 + 旁路在持续灌数。
 * 运行:NODE_PATH=<装了 playwright 的 node_modules> node web/tests/qb-live-perf.cjs [稳态秒数]
 *
 * 测什么(每项都给阈值,超了判 FAIL):
 *   1. 首屏:打开 /quote 到表格出行的耗时
 *   2. 稳态:实时推送下 N 秒,主线程占用率(CDP TaskDuration/墙钟)、浏览器全部进程 CPU%(/proc)、
 *      JS 堆增长、DOM 节点数、长任务(>50ms)次数、渲染行数恒定、推送确实在更新单元格
 *   3. 筛选:类型/期限/清算 chip 逐个点,点击→表格重绘完成的延迟(含设计上的 300ms 防抖),
 *      并校验筛选结果正确(如「国债」下可见行名称都含国债)
 *   4. 滚动:连续滚到底触发 keyset 翻页,期间长任务、渲染行数不随数据量增长
 *   5. 详情:点券名打开弹窗的延迟
 */
const { chromium } = require('playwright')
const fs = require('fs')
const path = require('path')

const BASE = process.env.XUNJIA_WEB || 'http://127.0.0.1:5190'
const API = process.env.XUNJIA_API || 'http://127.0.0.1:8120'
const STEADY_S = Number(process.argv[2] || 60)
const LIMIT = {                    // 阈值
  firstRowsMs: 3000,
  mainThreadPct: 15,               // 主线程忙碌占比
  browserCpuPct: 30,               // 浏览器全部进程 CPU(单核百分比)
  heapGrowMB: 15,
  longTasksSteady: 3,
  filterMs: 1200,                  // 含 300ms 防抖
  scrollLongTasks: 5,
  renderedRowsMax: 60,
  modalMs: 1500,
}

const results = []
const ok = (name, pass, detail) => { results.push({ name, pass, detail }); console.log(`${pass ? 'PASS' : 'FAIL'} ${name} — ${detail}`) }

function chromePids() {
  const out = []
  for (const d of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(d)) continue
    try {
      const cmd = fs.readFileSync(`/proc/${d}/cmdline`, 'utf8')
      if (cmd.includes('ms-playwright') && (cmd.includes('chrome') || cmd.includes('headless_shell'))) out.push(Number(d))
    } catch {}
  }
  return out
}
function cpuTicks(pids) {
  let t = 0, rss = 0
  for (const p of pids) {
    try {
      const f = fs.readFileSync(`/proc/${p}/stat`, 'utf8').split(') ')[1].split(' ')
      t += Number(f[11]) + Number(f[12])            // utime + stime
      rss += Number(f[21]) * 4096                   // rss 页
    } catch {}
  }
  return { t, rss }
}
const HZ = 100
const sleep = ms => new Promise(r => setTimeout(r, ms))
const metrics = async cdp => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m => [m.name, m.value]))
const devStats = async () => (await (await fetch(`${API}/api/dev/stats`)).json()).data

;(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--disable-gpu'] })
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } })
  await ctx.addInitScript(() => {
    window.__lt = []
    try {
      new PerformanceObserver(l => { for (const e of l.getEntries()) window.__lt.push({ t: e.startTime, d: e.duration }) })
        .observe({ type: 'longtask', buffered: true })
    } catch {}
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  const cdp = await ctx.newCDPSession(page)
  await cdp.send('Performance.enable')

  // ---------------- 1. 首屏
  const t0 = Date.now()
  await page.goto(`${BASE}/quote`, { waitUntil: 'domcontentloaded' })
  await page.locator('tr.mkt-row').first().waitFor({ timeout: 15000 })
  const firstMs = Date.now() - t0
  const firstRows = await page.locator('tr.mkt-row').count()
  ok('首屏出行', firstMs <= LIMIT.firstRowsMs, `${firstMs}ms,渲染 ${firstRows} 行`)
  await sleep(3000) // 等 worker 建连、订阅

  // ---------------- 2. 稳态
  const lt0 = await page.evaluate(() => window.__lt.length)
  const m0 = await metrics(cdp)
  const pids = chromePids()
  const c0 = cpuTicks(pids)
  const s0 = await devStats()
  const w0 = Date.now()
  const snapFirst = await page.locator('tr.mkt-row').evaluateAll(rs => rs.slice(0, 30).map(r => r.innerText).join('\n'))
  let blinkSeen = 0, rowsMax = 0
  const heap = []
  for (let i = 0; i < STEADY_S; i += 5) {
    await sleep(5000)
    blinkSeen += await page.locator('td.chg').count()
    rowsMax = Math.max(rowsMax, await page.locator('tr.mkt-row').count())
    heap.push((await metrics(cdp)).JSHeapUsedSize / 1048576)
  }
  const wall = (Date.now() - w0) / 1000
  const m1 = await metrics(cdp)
  const c1 = cpuTicks(pids)
  const s1 = await devStats()
  const lt1 = await page.evaluate(() => window.__lt.length)
  const snapLast = await page.locator('tr.mkt-row').evaluateAll(rs => rs.slice(0, 30).map(r => r.innerText).join('\n'))
  const mainPct = (m1.TaskDuration - m0.TaskDuration) / wall * 100
  const cpuPct = (c1.t - c0.t) / HZ / wall * 100
  const flows = s1.producer.produced - s0.producer.produced
  const pushed = s1.hub.sentItems - s0.hub.sentItems
  ok('稳态·主线程占用', mainPct <= LIMIT.mainThreadPct,
    `${mainPct.toFixed(1)}%(脚本 ${((m1.ScriptDuration - m0.ScriptDuration) / wall * 100).toFixed(1)}% / 布局 ${((m1.LayoutDuration - m0.LayoutDuration) / wall * 100).toFixed(1)}% / 样式 ${((m1.RecalcStyleDuration - m0.RecalcStyleDuration) / wall * 100).toFixed(1)}%)`)
  ok('稳态·浏览器全部进程 CPU', cpuPct <= LIMIT.browserCpuPct, `${cpuPct.toFixed(1)}%(单核),进程 ${pids.length} 个,RSS 合计 ${(c1.rss / 1048576).toFixed(0)}MB`)
  const heapGrow = heap[heap.length - 1] - heap[0]
  ok('稳态·JS 堆', heapGrow <= LIMIT.heapGrowMB, `${heap[0].toFixed(1)} → ${heap[heap.length - 1].toFixed(1)}MB(峰 ${Math.max(...heap).toFixed(1)}),DOM 节点 ${m1.Nodes}`)
  ok('稳态·长任务', lt1 - lt0 <= LIMIT.longTasksSteady, `${lt1 - lt0} 次 >50ms`)
  ok('稳态·渲染行数恒定', rowsMax <= LIMIT.renderedRowsMax, `视口内最多 ${rowsMax} 行(持有全部数据,只渲染视口)`)
  ok('稳态·实时数据确在更新', flows > 0 && pushed > 0 && (blinkSeen > 0 || snapFirst !== snapLast),
    `${wall.toFixed(0)}s 内后端产出 ${flows} 次最优价变动(${(flows / wall).toFixed(1)}/s),网关推送 ${pushed} 条,采样到闪烁单元格 ${blinkSeen} 个`)

  // ---------------- 3. 筛选
  const chip = (grp, txt) => page.locator('.f-group', { hasText: grp }).locator('.chip', { hasText: new RegExp(`^${txt.replace(/[+]/g, '\\+')}$`) })
  async function timedFilter(grp, txt, check) {
    const ltb = await page.evaluate(() => window.__lt.length)
    const resp = page.waitForResponse(r => r.url().includes('/api/bonds') && r.request().method() === 'GET', { timeout: 10000 })
    await page.evaluate(() => { window.__f0 = performance.now() })
    await chip(grp, txt).click()
    const r = await resp
    const total = (await r.json()).total
    const ms = await page.evaluate(() => new Promise(res => requestAnimationFrame(() => requestAnimationFrame(() => res(performance.now() - window.__f0)))))
    const names = await page.locator('tr.mkt-row .name').allInnerTexts()
    const lt = (await page.evaluate(() => window.__lt.length)) - ltb
    const correct = check ? names.every(check) : true
    ok(`筛选·${grp}=${txt}`, ms <= LIMIT.filterMs && correct && lt === 0,
      `${ms.toFixed(0)}ms(含 300ms 防抖),命中 ${total} 只,可见 ${names.length} 行${check ? (correct ? ',结果校验通过' : ',结果校验失败:' + names.find(n => !check(n))) : ''},长任务 ${lt}`)
  }
  await timedFilter('类型', '国债', n => n.includes('国债'))
  await timedFilter('类型', '政金债', n => /国开|农发|进出|口行/.test(n))
  await timedFilter('类型', '中票', n => n.includes('MTN'))
  await timedFilter('期限', '1–3Y')
  await timedFilter('清算', '+0')
  await timedFilter('清算', '全部')
  await timedFilter('期限', '不限')
  await timedFilter('类型', '全部信用')

  // ---------------- 4. 滚动翻页
  {
    const ltb = await page.evaluate(() => window.__lt.length)
    const before = await page.evaluate(() => document.querySelectorAll('tr.mkt-row').length)
    const t = Date.now()
    let maxRows = 0
    for (let i = 0; i < 25; i++) {
      await page.locator('.table-scroll').evaluate(el => { el.scrollTop = el.scrollHeight })
      await sleep(250)
      maxRows = Math.max(maxRows, await page.locator('tr.mkt-row').count())
    }
    const lt = (await page.evaluate(() => window.__lt.length)) - ltb
    const held = await page.locator('.ps-tab').innerText().catch(() => '')
    ok('滚动·翻页到底', lt <= LIMIT.scrollLongTasks && maxRows <= LIMIT.renderedRowsMax,
      `${((Date.now() - t) / 1000).toFixed(1)}s 滚 25 次,长任务 ${lt},渲染行数 ${before}→最多 ${maxRows},埋点角标「${held}」`)
    await page.locator('.table-scroll').evaluate(el => { el.scrollTop = 0 })
    await sleep(500)
  }

  // ---------------- 5. 详情弹窗
  {
    const name = page.locator('tr.mkt-row .name').first()
    const txt = await name.innerText()
    const resp = page.waitForResponse(r => r.url().includes('/api/bond/'), { timeout: 10000 })
    await page.evaluate(() => { window.__m0 = performance.now() })
    await name.click()
    const r = await resp
    const body = await r.json()
    const ms = await page.evaluate(() => new Promise(res => requestAnimationFrame(() => requestAnimationFrame(() => res(performance.now() - window.__m0)))))
    ok('详情弹窗', ms <= LIMIT.modalMs && body.ok, `${txt}:${ms.toFixed(0)}ms,各经纪商报价 ${body.data?.quotes?.length ?? 0} 条,今日成交 ${body.data?.trades?.length ?? 0} 笔,盘中点 ${body.data?.intra?.bid?.length ?? 0}`)
    await page.screenshot({ path: path.join(__dirname, 'out', 'qb-modal.png') })
    await page.keyboard.press('Escape')
  }

  await page.screenshot({ path: path.join(__dirname, 'out', 'qb-quote.png') })
  ok('页面无 JS 报错', errors.length === 0, errors.length ? errors.slice(0, 3).join(' | ') : '0 条')

  await browser.close()
  const fail = results.filter(r => !r.pass).length
  fs.writeFileSync(path.join(__dirname, 'out', 'qb-live-perf.json'), JSON.stringify({ at: new Date().toISOString(), steadyS: STEADY_S, limit: LIMIT, results }, null, 1))
  console.log(`\n合计 ${results.length} 项,失败 ${fail}`)
  process.exit(fail ? 1 : 0)
})().catch(e => { console.error(e); process.exit(2) })
