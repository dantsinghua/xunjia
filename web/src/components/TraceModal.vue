<script setup lang="ts">
/** 数据链路追踪弹窗:一条消息从收到→归属→规则判定→动作的全链路(留痕)。
 *  还原设计稿 hasTrace。 */
import { computed } from 'vue'
import { traceOf } from '../api'
import type { ChatMsg } from '../lib/types'
import { useBodyScrollLock } from '../lib/useModal'

const props = defineProps<{ msg: (ChatMsg & { idx: number; cp: string }) | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()
useBodyScrollLock(() => !!props.msg)

const trace = computed(() => (props.msg ? traceOf(props.msg) : null))
const isBot = computed(() => props.msg && (props.msg.role === 'bot' || props.msg.role === 'human'))
</script>

<template>
  <Teleport to="body">
  <div v-if="msg && trace" class="ov" @click="emit('close')">
    <div class="mdl pop-in trace-mdl" @click.stop>
      <div class="mdl-hd">
      <div class="head">
        <div class="head-l">
          <h2 class="serif">数据链路追踪 <span class="mono id">#msg:{{ trace.id }}</span></h2>
          <span class="pill dir" :class="{ bot: isBot }">{{ trace.dirLabel }}</span>
          <span class="pill ok">{{ trace.status }}</span>
        </div>
        <button class="x" @click="emit('close')"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg></button>
      </div>
      <div class="summary">{{ trace.summary }}</div>
      </div>

      <div class="mdl-bd body">
        <div class="steps-col">
          <div class="sec-title">链路节点(按时序)</div>
          <div class="timeline">
            <div class="axis" />
            <div v-for="(s, i) in trace.steps" :key="i" class="step">
              <span class="node"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg></span>
              <div class="step-card">
                <div class="step-hd"><b>{{ s.title }}</b><span :class="s.reached === false ? 'st-bad' : 'st-ok'">{{ s.reached === false ? '未到达' : '已到达' }}</span><span class="cost mono">耗时 <b>{{ s.cost }}</b></span></div>
                <div class="desc">{{ s.desc }}</div>
                <div class="chips"><span v-for="(c, k) in s.chips" :key="k" class="chip">{{ c.k }} <b class="mono">{{ c.v }}</b></span></div>
              </div>
            </div>
          </div>
        </div>
        <div class="meta-col">
          <div class="sec-title">本条概要</div>
          <div class="meta-list">
            <div v-for="(kv, i) in trace.meta" :key="i" class="meta-row"><span>{{ kv.k }}</span><b class="mono">{{ kv.v }}</b></div>
          </div>
          <div class="sec-title mt">原文片段</div>
          <div class="raw">{{ trace.text }}</div>
        </div>
      </div>

      <div class="mdl-ft"><button class="btn-ghost close-btn" @click="emit('close')">关 闭</button></div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩/骨架/滚动区用 styles.css 的 .ov / .mdl / .mdl-hd / .mdl-bd / .mdl-ft */
.trace-mdl { max-width: 1180px; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.head-l { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.head h2 { margin: 0; font-weight: 900; font-size: 21px; color: #2B2320; }
.id { font-weight: 700; color: #B01C24; }
.pill { padding: 3px 11px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.pill.dir { background: rgba(30,95,180,.1); color: #1E5FB4; border: 1px solid rgba(30,95,180,.3); }
.pill.dir.bot { background: rgba(176, 28, 36,.1); color: #B01C24; border-color: rgba(176, 28, 36,.3); }
.pill.ok { background: rgba(46,125,79,.1); color: #1F6B3E; }
.x { cursor: pointer; width: 32px; height: 32px; border-radius: 50%; border: 1px solid rgba(176, 28, 36,.25); background: rgba(255,255,255,.7); color: #B01C24; display: grid; place-items: center; }
.summary { margin-top: 14px; padding: 10px 16px; border-radius: 12px; background: rgba(176, 28, 36,.06); border: 1px solid rgba(176, 28, 36,.22); font-size: 13px; font-weight: 600; color: #B01C24; }
.body { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(280px, 1fr); gap: 26px; align-items: start; }
@media (max-width: 860px) { .body { grid-template-columns: minmax(0, 1fr); gap: 16px; } }
.sec-title { font-size: 13.5px; font-weight: 700; margin-bottom: 12px; }
.sec-title.mt { margin-top: 16px; }
.timeline { display: flex; flex-direction: column; gap: 14px; position: relative; padding-left: 34px; }
.timeline .axis { position: absolute; left: 11px; top: 14px; bottom: 14px; width: 2px; background: rgba(176, 28, 36,.14); }
.step { position: relative; }
.node { position: absolute; left: -34px; top: 14px; width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; background: rgba(46,125,79,.1); border: 2px solid #2E8B57; color: #1F6B3E; box-shadow: 0 0 10px rgba(46,125,79,.4); }
.step-card { border-radius: 14px; padding: 14px 18px; background: rgba(255,255,255,.75); border: 1px solid rgba(176, 28, 36,.12); box-shadow: 0 4px 14px rgba(43,35,32,.05); }
.step-hd { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.step-hd b { font-size: 14.5px; }
.st-bad { color: #B01C24; font-weight: 700; }
.st-ok { padding: 2px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600; background: rgba(46,125,79,.12); color: #1F6B3E; }
.cost { margin-left: auto; font-size: 12px; color: #8A7F76; }
.cost b { color: #1F6B3E; }
.desc { font-size: 13px; color: #5C5148; line-height: 1.7; margin-top: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.chip { padding: 3px 9px; border-radius: 7px; font-size: 11.5px; background: rgba(255,255,255,.9); border: 1px solid rgba(176, 28, 36,.14); color: #6E6258; white-space: nowrap; }
.chip b { color: #2B2320; }
.meta-list { display: flex; flex-direction: column; gap: 6px; }
.meta-row { display: flex; justify-content: space-between; gap: 12px; padding: 9px 14px; border-radius: 10px; background: rgba(255,255,255,.75); border: 1px solid rgba(176, 28, 36,.1); font-size: 12.5px; }
.meta-row span { color: #6E6258; white-space: nowrap; }
.meta-row b { text-align: right; font-weight: 600; color: #2B2320; }
.raw { padding: 12px 14px; border-radius: 10px; background: rgba(255,255,255,.75); border: 1px solid rgba(176, 28, 36,.1); font-size: 13px; line-height: 1.7; color: #2B2320; }
.foot { display: flex; justify-content: flex-end; margin-top: 16px; }
.close-btn { padding: 9px 22px; border-radius: 10px; font-size: 13px; letter-spacing: 2px; }
</style>
