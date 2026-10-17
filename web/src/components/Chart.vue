<script setup lang="ts">
/** 轻量 SVG 图表:柱状 / 折线 / 散点。供智能问答出图,无三方依赖。 */
import { computed } from 'vue'
import type { ChartSpec } from '../lib/types'

const props = defineProps<{ spec: ChartSpec }>()

const W = 560, H = 260, PAD_L = 46, PAD_R = 16, PAD_T = 28, PAD_B = 40
const plotW = W - PAD_L - PAD_R
const plotH = H - PAD_T - PAD_B
const COLORS = ['#B01C24', '#C68D58', '#1E5FB4', '#1F6B3E', '#8F6412']

interface Bounds { min: number; max: number }
function niceBounds(vals: number[], padRatio = 0.12): Bounds {
  let min = Math.min(...vals), max = Math.max(...vals)
  if (min === max) { min -= 1; max += 1 }
  const pad = (max - min) * padRatio
  return { min: min - pad, max: max + pad }
}

const yTicks = 4

/* ---- bar ---- */
const bar = computed(() => {
  const s = props.spec
  if (s.type !== 'bar' || !s.categories || !s.series) return null
  const all = s.series.flatMap(se => se.data)
  const lo = Math.min(...all), hi = Math.max(...all)
  // 数值都为正且彼此接近(如各渠道 YTM 都在 2.2 附近)时不强制 0 基线,否则差异不可见
  const zoom = lo > 0 && (hi - lo) < hi * 0.35
  const min = zoom ? lo - (hi - lo || hi * 0.1) * 0.6 : Math.min(0, lo)
  const max = hi
  const range = (max - min) || 1
  const n = s.categories.length, groups = s.series.length
  const groupW = plotW / n
  const barW = Math.min(46, groupW / (groups + 0.6))
  const yOf = (x: number) => PAD_T + plotH - (x - min) / range * plotH
  const baseline = (min <= 0 && max >= 0) ? 0 : min
  const yBase = yOf(baseline)
  const bars = s.categories.map((cat, i) => {
    const gx = PAD_L + i * groupW + (groupW - barW * groups) / 2
    return {
      cat, x: PAD_L + i * groupW + groupW / 2,
      rects: s.series!.map((se, j) => {
        const v = se.data[i]
        const yv = yOf(v)
        const y = Math.min(yv, yBase), h = Math.max(2, Math.abs(yBase - yv))
        return { x: gx + j * barW, y, w: barW - 3, h, color: se.color || COLORS[j % COLORS.length], v }
      }),
    }
  })
  const ticks = Array.from({ length: yTicks + 1 }, (_, k) => {
    const val = min + (max - min) * k / yTicks
    return { y: PAD_T + plotH - (val - min) / range * plotH, val: fmt(val) }
  })
  return { bars, ticks }
})

/* ---- line ---- */
const line = computed(() => {
  const s = props.spec
  if (s.type !== 'line' || !s.categories || !s.series) return null
  const all = s.series.flatMap(se => se.data)
  const { min, max } = niceBounds(all)
  const range = (max - min) || 1
  const n = s.categories.length
  const xOf = (i: number) => PAD_L + (n === 1 ? plotW / 2 : i * plotW / (n - 1))
  const yOf = (v: number) => PAD_T + plotH - (v - min) / range * plotH
  const lines = s.series.map((se, j) => ({
    name: se.name, color: se.color || COLORS[j % COLORS.length],
    points: se.data.map((v, i) => `${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`).join(' '),
    dots: se.data.map((v, i) => ({ x: xOf(i), y: yOf(v) })),
  }))
  const ticks = Array.from({ length: yTicks + 1 }, (_, k) => {
    const val = min + (max - min) * k / yTicks
    return { y: yOf(val), val: fmt(val) }
  })
  const xLabels = s.categories.map((c, i) => ({ x: xOf(i), label: c }))
    .filter((_, i) => n <= 8 || i % Math.ceil(n / 8) === 0)
  return { lines, ticks, xLabels }
})

/* ---- scatter ---- */
const scatter = computed(() => {
  const s = props.spec
  if (s.type !== 'scatter' || !s.points) return null
  const xs = s.points.map(p => p.x), ys = s.points.map(p => p.y)
  const bx = niceBounds(xs), by = niceBounds(ys)
  const xr = (bx.max - bx.min) || 1, yr = (by.max - by.min) || 1
  const xOf = (v: number) => PAD_L + (v - bx.min) / xr * plotW
  const yOf = (v: number) => PAD_T + plotH - (v - by.min) / yr * plotH
  const dots = s.points.map((p, i) => ({ x: xOf(p.x), y: yOf(p.y), label: p.label, color: COLORS[i % COLORS.length] }))
  const yticks = Array.from({ length: yTicks + 1 }, (_, k) => { const val = by.min + (by.max - by.min) * k / yTicks; return { y: yOf(val), val: fmt(val) } })
  const xticks = Array.from({ length: yTicks + 1 }, (_, k) => { const val = bx.min + (bx.max - bx.min) * k / yTicks; return { x: xOf(val), val: fmt(val) } })
  return { dots, yticks, xticks }
})

function fmt(v: number): string {
  if (Math.abs(v) >= 100) return v.toFixed(0)
  if (Math.abs(v) >= 10) return v.toFixed(1)
  return v.toFixed(v % 1 === 0 ? 0 : 2)
}
</script>

<template>
  <div class="chart">
    <div v-if="spec.title" class="chart-title">{{ spec.title }}</div>
    <svg :viewBox="`0 0 ${W} ${H}`" class="svg" preserveAspectRatio="xMidYMid meet">
      <!-- BAR -->
      <template v-if="bar">
        <g v-for="(t, i) in bar.ticks" :key="'bt' + i">
          <line :x1="PAD_L" :y1="t.y" :x2="W - PAD_R" :y2="t.y" stroke="rgba(176, 28, 36,.1)" />
          <text :x="PAD_L - 8" :y="t.y + 3" text-anchor="end" class="axis">{{ t.val }}</text>
        </g>
        <g v-for="(g, i) in bar.bars" :key="'bg' + i">
          <rect v-for="(r, j) in g.rects" :key="j" :x="r.x" :y="r.y" :width="r.w" :height="r.h" :fill="r.color" rx="3">
            <title>{{ g.cat }}: {{ r.v }}</title>
          </rect>
          <text :x="g.x" :y="H - PAD_B + 16" text-anchor="middle" class="axis cat">{{ g.cat }}</text>
        </g>
      </template>

      <!-- LINE -->
      <template v-else-if="line">
        <g v-for="(t, i) in line.ticks" :key="'lt' + i">
          <line :x1="PAD_L" :y1="t.y" :x2="W - PAD_R" :y2="t.y" stroke="rgba(176, 28, 36,.1)" />
          <text :x="PAD_L - 8" :y="t.y + 3" text-anchor="end" class="axis">{{ t.val }}</text>
        </g>
        <text v-for="(x, i) in line.xLabels" :key="'lx' + i" :x="x.x" :y="H - PAD_B + 16" text-anchor="middle" class="axis cat">{{ x.label }}</text>
        <g v-for="(l, i) in line.lines" :key="'ll' + i">
          <polyline :points="l.points" fill="none" :stroke="l.color" stroke-width="2.2" stroke-linejoin="round" />
          <circle v-for="(d, k) in l.dots" :key="k" :cx="d.x" :cy="d.y" r="2.6" :fill="l.color" />
        </g>
      </template>

      <!-- SCATTER -->
      <template v-else-if="scatter">
        <g v-for="(t, i) in scatter.yticks" :key="'st' + i">
          <line :x1="PAD_L" :y1="t.y" :x2="W - PAD_R" :y2="t.y" stroke="rgba(176, 28, 36,.1)" />
          <text :x="PAD_L - 8" :y="t.y + 3" text-anchor="end" class="axis">{{ t.val }}</text>
        </g>
        <text v-for="(t, i) in scatter.xticks" :key="'sx' + i" :x="t.x" :y="H - PAD_B + 16" text-anchor="middle" class="axis">{{ t.val }}</text>
        <g v-for="(d, i) in scatter.dots" :key="'sd' + i">
          <circle :cx="d.x" :cy="d.y" r="6" :fill="d.color" fill-opacity="0.8" :stroke="d.color" stroke-width="1.2" />
          <text v-if="d.label" :x="d.x" :y="d.y - 10" text-anchor="middle" class="pt-label">{{ d.label }}</text>
        </g>
      </template>
    </svg>

    <div v-if="spec.series && spec.series.length > 1" class="legend">
      <span v-for="(se, i) in spec.series" :key="i" class="lg">
        <span class="sw" :style="{ background: se.color || COLORS[i % COLORS.length] }" />{{ se.name }}
      </span>
    </div>
    <div v-if="spec.xLabel || spec.yLabel" class="axis-labels">
      <span v-if="spec.yLabel">纵轴 {{ spec.yLabel }}</span>
      <span v-if="spec.xLabel">横轴 {{ spec.xLabel }}</span>
    </div>
  </div>
</template>

<style scoped>
.chart { border-radius: 14px; padding: 14px 16px 10px; background: rgba(255,255,255,.75); border: 1px solid rgba(176, 28, 36,.14); box-shadow: 0 6px 20px rgba(43,35,32,.05); margin-top: 6px; }
.chart-title { font-size: 12.5px; font-weight: 700; letter-spacing: .5px; color: #B01C24; margin-bottom: 4px; }
.svg { width: 100%; height: auto; display: block; overflow: visible; }
.axis { font-family: 'JetBrains Mono', monospace; font-size: 10px; fill: #A89C90; }
.axis.cat { font-family: 'Noto Sans SC', sans-serif; fill: #6E6258; font-size: 10.5px; }
.pt-label { font-family: 'Noto Sans SC', sans-serif; font-size: 10px; fill: #5C5148; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 6px; font-size: 11.5px; color: #6E6258; }
.legend .sw { display: inline-block; width: 12px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }
.axis-labels { display: flex; gap: 16px; margin-top: 4px; font-size: 10.5px; color: #A89C90; }
</style>
