<script setup lang="ts">
/** 管线埋点面板 —— 契约 §6。
 *
 *  为什么先做它:这次改造(SharedWorker + pull + 成批 + flowId upsert)每一项都声称
 *  能降负载,但「声称」不算数。DM 自己就埋了 worker→main 延迟 / 批量条数 / 分发耗时,
 *  超 10s 上报 RenderPerformanceError。没有这三个数,改完无法证明收益,也无法定位回归。
 *
 *  默认折叠成一个小角标,点开才展开;Alt+P 切换显隐,免得挡行情。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

export interface PipelineStat {
  latencyMs: number   // worker→main:BATCH.workerSendTime - requestTime
  batchSize: number   // 本次 pull 拿到的总条数
  dispatchMs: number  // upsert + 渲染耗时
  dropped: number     // worker 背压累计丢弃
  transport: string   // sharedworker | worker | inline
  rendered: number    // 虚拟窗口内实际渲染行数(应恒定)
  held: number        // 内存中持有的行数
  e2e?: { p50: number; p95: number } | null   // 数据到本机 → 应用到行(QB 源才有)
}

const props = defineProps<{ stat: PipelineStat }>()

const open = ref(false)
const showing = ref(true)

/* 阈值照契约 §6;超阈值变红,不弹窗打扰 */
const THRESH = { latencyMs: 2000, dispatchMs: 1000 }
const bad = computed(() => ({
  latencyMs: props.stat.latencyMs > THRESH.latencyMs,
  dispatchMs: props.stat.dispatchMs > THRESH.dispatchMs,
  dropped: props.stat.dropped > 0,
}))
const anyBad = computed(() => bad.value.latencyMs || bad.value.dispatchMs || bad.value.dropped)

function onKey(e: KeyboardEvent) {
  if (e.altKey && (e.key === 'p' || e.key === 'P')) { showing.value = !showing.value; e.preventDefault() }
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))

const TRANSPORT_CN: Record<string, string> = {
  sharedworker: 'SharedWorker(跨标签共享)',
  worker: 'Worker(每标签一条)',
  inline: '主线程直连(降级)',
}
</script>

<template>
  <div v-if="showing" class="ps" :class="{ open, bad: anyBad }">
    <button class="ps-tab" :title="'Alt+P 隐藏 · ' + (TRANSPORT_CN[stat.transport] || stat.transport)" @click="open = !open">
      <span class="dot" :class="{ warn: anyBad }" />
      <span class="mono">{{ stat.rendered }}行 / {{ stat.batchSize }}条</span>
    </button>

    <div v-if="open" class="ps-body">
      <div class="ps-hd">实时管线</div>
      <div class="kv"><span>传输层</span><b>{{ TRANSPORT_CN[stat.transport] || stat.transport }}</b></div>
      <div v-if="stat.e2e" class="kv" :class="{ hot: stat.e2e.p95 > 2000 }">
        <span>端到端延迟 P50/P95</span><b class="mono">{{ stat.e2e.p50 }}/{{ stat.e2e.p95 }}ms</b>
      </div>
      <div class="kv" :class="{ hot: bad.latencyMs }">
        <span>worker→main 延迟</span><b class="mono">{{ stat.latencyMs }}ms</b>
      </div>
      <div class="kv"><span>本批条数</span><b class="mono">{{ stat.batchSize }}</b></div>
      <div class="kv" :class="{ hot: bad.dispatchMs }">
        <span>分发+渲染</span><b class="mono">{{ stat.dispatchMs }}ms</b>
      </div>
      <div class="kv" :class="{ hot: bad.dropped }">
        <span>背压丢弃</span><b class="mono">{{ stat.dropped }}</b>
      </div>
      <div class="ps-sep" />
      <div class="kv"><span>渲染行数</span><b class="mono">{{ stat.rendered }}</b></div>
      <div class="kv"><span>内存持有</span><b class="mono">{{ stat.held }}</b></div>
      <div class="ps-note">渲染行数应恒定,不随内存持有量增长 —— 涨了说明虚拟滚动失效</div>
    </div>
  </div>
</template>

<style scoped>
.ps { position: fixed; right: 14px; bottom: 14px; z-index: 90; font-size: 12px; }
.ps-tab {
  cursor: pointer; display: flex; align-items: center; gap: 7px; padding: 6px 11px; border-radius: 999px;
  background: rgba(250,248,244,.88); backdrop-filter: blur(10px);
  border: 1px solid rgba(176, 28, 36,.18); color: #6E6258; font-family: inherit; font-size: 11.5px;
  box-shadow: 0 4px 16px rgba(43,35,32,.12);
}
.ps.bad .ps-tab { border-color: rgba(176, 28, 36,.5); color: #B01C24; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: #3DA65E; }
.dot.warn { background: #C2333B; box-shadow: 0 0 8px rgba(180,66,56,.7); }
.ps-body {
  position: absolute; right: 0; bottom: 34px; width: 268px; padding: 12px 14px; border-radius: 14px;
  background: rgba(250,248,244,.96); backdrop-filter: blur(16px);
  border: 1px solid rgba(176, 28, 36,.2); box-shadow: 0 14px 40px rgba(43,35,32,.18);
}
.ps-hd { font-size: 11.5px; font-weight: 700; letter-spacing: 1px; color: #8A7F76; margin-bottom: 8px; }
.kv { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 3px 0; color: #6E6258; }
.kv b { color: #2B2320; font-weight: 700; }
.kv.hot b { color: #C2333B; }
.ps-sep { height: 1px; background: rgba(176, 28, 36,.14); margin: 8px 0; }
.ps-note { margin-top: 8px; font-size: 10.5px; line-height: 1.5; color: #A89C90; }
</style>
