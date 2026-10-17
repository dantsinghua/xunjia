/* 意向询价页 · 以真实 innodealing(DM)盘中数据为准的端到端测试(Playwright + CDP)。
 *
 * 链路:DM 页面 SharedWorker 推送 → 油猴旁路 → qb_ingest → MySQL(capture_time)
 *       → xunjia 后端 qb_source(0.5s 轮询) → 网关 200ms 成批 → SharedWorker → 主线程 500ms pull → 表格
 * 本脚本只负责浏览器侧采样,落 out/qb-live-e2e.raw.json;延迟与正确性由 qb_live_e2e_check.py 对库计算。
 *
 * 采样:
 *   - MutationObserver 记录视口内每行 最优bid/最优ofr/报价方 的每次变化(按券代码去重,首次出现记为「上屏」)
 *   - CDP Performance 指标(主线程占用)、/proc 浏览器全进程 CPU/RSS、JS 堆、长任务、DOM 节点
 *   - 结束时视口快照(供对库核对数值)
 *   - 真实数据下的筛选操作延迟
 * 运行:NODE_PATH=<playwright node_modules> node web/tests/qb-live-e2e.cjs [采样秒数]
 */
const { chromium } = require('playwright')
const fs = require('fs')
const path = require('path')

const BASE = process.env.XUNJIA_WEB || 'http://127.0.0.1:5190'
const API = process.env.XUNJIA_API || 'http://127.0.0.1:8120'
const SECS = Number(process.argv[2] || 180)
const OUT = path.join(__dirname, 'out')
fs.mkdirSync(OUT, { recursive: true })

function chromePids() {
  return fs.readdirSync('/proc').filter(d => /^\d+$/.test(d)).map(Number).filter(p => {
    try { const c = fs.readFileSync(`/proc/${p}/cmdline`, 'utf8'); return c.includes('ms-playwright') } catch { return false }
  })
}
function cpu(pids) {
  let t = 0, rss = 0
  for (const p of pids) {
    try { const f = fs.readFileSync(`/proc/${p}/stat`, 'utf8').split(') ')[1].split(' '); t += +f[11] + +f[12]; rss += +f[21] * 4096 } catch {}
  }
  return { t, rss }
}
const sleep = ms => new Promise(r => setTimeout(r, ms))
const metrics = async cdp => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m => [m.name, m.value]))
const stats = async () => (await (await fetch(`${API}/api/dev/stats`)).json()).data

;(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--disable-gpu'] })
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } })
  await ctx.addInitScript(() => {
    window.__lt = []
    try { new PerformanceObserver(l => { for (const e of l.getEntries()) window.__lt.push(e.duration) }).observe({ type: 'longtask', buffered: true }) } catch {}
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  const cdp = await ctx.newCDPSession(page)
  await cdp.send('Performance.enable')

  const t0 = Date.now()
  await page.goto(`${BASE}/quote`)
  await page.locator('tr.mkt-row').first().waitFor({ timeout: 15000 })
  const firstMs = Date.now() - t0
  await sleep(3000)

  // 视口观测:列序 0简称 1代码 2期限 3估值 4-7内部意向 8bid方 9bid量 10最优bid 11最优ofr 12ofr量 13ofr方 14状态
  await page.evaluate(() => {
    window.__ev = []
    const last = new Map()
    const scan = () => {
      const now = Date.now()
      for (const tr of document.querySelectorAll('tr.mkt-row')) {
        const td = tr.querySelectorAll('td')
        if (td.length < 14) continue
        const code = td[1].innerText.trim()
        const v = { ob: td[10].innerText.trim(), oo: td[11].innerText.trim(), obs: td[8].innerText.trim(), oos: td[13].innerText.trim() }
        const p = last.get(code)
        if (!p) { window.__ev.push({ t: now, code, kind: 'appear', ...v }) }
        else {
          for (const k of ['ob', 'oo']) if (p[k] !== v[k]) window.__ev.push({ t: now, code, kind: k, from: p[k], to: v[k], side: k === 'ob' ? v.obs : v.oos })
        }
        last.set(code, v)
      }
    }
    scan()
    new MutationObserver(scan).observe(document.querySelector('tbody') || document.body, { subtree: true, childList: true, characterData: true })
  })

  const m0 = await metrics(cdp), pids = chromePids(), c0 = cpu(pids), s0 = await stats(), lt0 = await page.evaluate(() => window.__lt.length)
  const w0 = Date.now()
  const heap = []
  for (let i = 0; i < SECS; i += 10) { await sleep(10000); heap.push((await metrics(cdp)).JSHeapUsedSize / 1048576) }
  const wall = (Date.now() - w0) / 1000
  const m1 = await metrics(cdp), c1 = cpu(pids), s1 = await stats(), lt1 = await page.evaluate(() => window.__lt.length)

  // 结束快照(对库核对),拍完立刻记时间
  const snapT = Date.now()
  const snapshot = await page.locator('tr.mkt-row').evaluateAll(rs => rs.map(tr => {
    const td = tr.querySelectorAll('td')
    return { name: td[0].innerText.trim(), code: td[1].innerText.trim(), obs: td[8].innerText.trim(), obv: td[9].innerText.trim(),
             ob: td[10].innerText.trim(), oo: td[11].innerText.trim(), oov: td[12].innerText.trim(), oos: td[13].innerText.trim() }
  }))
  const events = await page.evaluate(() => window.__ev)
  await page.screenshot({ path: path.join(OUT, 'qb-live-e2e.png') })

  // 真实数据下的筛选
  const filters = []
  const chip = (g, t) => page.locator('.f-group', { hasText: g }).locator('.chip', { hasText: new RegExp(`^${t.replace(/[+]/g, '\\+')}$`) })
  for (const [g, t] of [['类型', '国债'], ['类型', '政金债'], ['期限', '1–3Y'], ['期限', '不限'], ['类型', '全部信用']]) {
    const lb = await page.evaluate(() => window.__lt.length)
    const resp = page.waitForResponse(r => r.url().includes('/api/bonds') && r.url().includes('limit=') && !r.url().includes('before=0&limit=50&groups=' + 'x'), { timeout: 10000 })
    await page.evaluate(() => { window.__f0 = performance.now() })
    await chip(g, t).click()
    const total = (await (await resp).json()).total
    const ms = await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(performance.now() - window.__f0)))))
    filters.push({ filter: `${g}=${t}`, ms: Math.round(ms), total, longTasks: (await page.evaluate(() => window.__lt.length)) - lb })
    await sleep(800)
  }
  await browser.close()

  const raw = {
    at: new Date().toISOString(), secs: wall, firstMs, errors,
    perf: {
      mainThreadPct: (m1.TaskDuration - m0.TaskDuration) / wall * 100,
      scriptPct: (m1.ScriptDuration - m0.ScriptDuration) / wall * 100,
      layoutPct: (m1.LayoutDuration - m0.LayoutDuration) / wall * 100,
      browserCpuPct: (c1.t - c0.t) / 100 / wall * 100, rssMB: c1.rss / 1048576,
      heapMB: heap, domNodes: m1.Nodes, longTasks: lt1 - lt0,
    },
    backend: { flows: s1.producer.produced - s0.producer.produced, pushed: s1.hub.sentItems - s0.hub.sentItems,
               rowsRead: s1.qb.rows_read - s0.qb.rows_read, source: s1.source },
    snapT, snapshot, events, filters,
  }
  fs.writeFileSync(path.join(OUT, 'qb-live-e2e.raw.json'), JSON.stringify(raw, null, 1))
  console.log(`采样完成 ${wall.toFixed(0)}s:事件 ${events.length} 条,快照 ${snapshot.length} 行,后端最优价变动 ${raw.backend.flows}`)
})().catch(e => { console.error(e); process.exit(2) })
