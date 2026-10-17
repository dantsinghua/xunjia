<script setup lang="ts">
/** 个人策略模板：保存后仅供新批次预览参考，不构成发送授权。 */
import { computed, reactive, watch } from 'vue'
import { App } from 'ant-design-vue'
import { api } from '../api'
import { STRATEGY_DEFAULT, type Strategy } from '../lib/types'
import { useBodyScrollLock } from '../lib/useModal'

const props = defineProps<{ open: boolean; value: Strategy }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved', s: Strategy): void }>()
const { message } = App.useApp()
useBodyScrollLock(() => props.open)

const draft = reactive<Strategy>({ ...props.value })
watch(() => props.open, (o) => { if (o) Object.assign(draft, props.value) })

// 派生:轮次上限 =(起始−边界)/步幅 + 1
const maxRounds = computed(() => {
  const span = draft.startDev - draft.boundary
  if (draft.step <= 0 || span < 0) return '—'
  return Math.ceil(span / draft.step) + 1
})

interface Item { key: keyof Strategy; label: string; desc: string; min: number; max: number; step: number; unit: string }
const ITEMS: Item[] = [
  { key: 'startDev', label: '起始偏离(BP)', desc: '首轮报价相对中债估值的偏离,买入为最有利端(估值+N),越大越保守。', min: 1, max: 30, step: 1, unit: 'BP' },
  { key: 'boundary', label: '探价边界(BP)', desc: '可让步到的最差位置;越过边界即钳到边界并收口请示,末轮不越界。', min: 0, max: 25, step: 1, unit: 'BP' },
  { key: 'step', label: '步幅(BP)', desc: '每轮在上一轮基础上让步的幅度,决定还价节奏与轮次上限。', min: 0.5, max: 10, step: 0.5, unit: 'BP' },
]

function pct(it: Item) { const v = draft[it.key] as number; return Math.round((v - it.min) / (it.max - it.min) * 100) }
function disp(it: Item) { const v = draft[it.key] as number; return (Number.isInteger(v) ? v : v.toFixed(1)) + it.unit }
function inc(it: Item) { const v = draft[it.key] as number; (draft[it.key] as number) = Math.min(it.max, +(v + it.step).toFixed(2)) }
function dec(it: Item) { const v = draft[it.key] as number; (draft[it.key] as number) = Math.max(it.min, +(v - it.step).toFixed(2)) }

function reset() { Object.assign(draft, STRATEGY_DEFAULT, { templateId: props.value.templateId || 'personal' }) }
async function save() {
  const r = await api.saveStrategy({ ...draft, templateId: draft.templateId || 'personal' })
  if (!r.ok) { message.error(r.hint || '保存失败'); return }
  message.success('模板已保存，请在新批次预览中核对并确认')
  emit('saved', r.data)
  emit('close')
}
</script>

<template>
  <Teleport to="body">
  <div v-if="open" class="ov" @click="emit('close')">
    <div class="mdl pop-in strategy-mdl" @click.stop>
      <div class="mdl-hd">
        <div class="head">
          <h2 class="serif">询价策略 <span class="ver">v1.0</span></h2>
          <button class="x" @click="emit('close')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        </div>
        <p class="sub">模板仅供新批次预览参考；每批须明确本方方向、数量与价格边界，并再次确认。跨日需要重新确认。</p>
      </div>

      <div class="mdl-bd">
      <p data-testid="strategy-state" class="sub">{{ value.configured === false ? '未保存草稿：默认参数尚未保存，也未应用于任何询价。' : '已保存模板：本次修改须保存，再在新批次预览中核对。' }}</p>
      <div class="items">
        <div v-for="it in ITEMS" :key="it.key" class="item">
          <div class="item-top">
            <div><div class="it-label">{{ it.label }}</div><div class="it-desc">{{ it.desc }}</div></div>
            <div class="stepper">
              <button class="sb" @click="dec(it)">−</button>
              <span class="sval mono">{{ disp(it) }}</span>
              <button class="sb" @click="inc(it)">+</button>
            </div>
          </div>
          <div class="track"><div class="fill" :style="{ width: pct(it) + '%' }" /></div>
        </div>

        <!-- 严格匹配报量 -->
        <div class="item toggle-item">
          <div><div class="it-label">严格匹配报量</div><div class="it-desc">作为新批次的数量核对偏好；每批预览中核对，以该批确认的策略为准。</div></div>
          <div class="switch" :class="{ on: draft.strict }" @click="draft.strict = !draft.strict"><span class="knob" /></div>
        </div>

        <div class="derived">
          <span>派生 · 参考步数</span>
          <b class="mono">{{ maxRounds }} 步</b>
          <span class="derived-note">=ceil((起始 − 边界)/步幅)+1；仅为参数参考，不代表已授权或强制执行轮次</span>
        </div>

        <p class="sub">询价量由每批人工填写；本期不自动生成数量或跨日继承授权。</p>
      </div>

      </div>

      <div class="mdl-ft">
        <button class="btn-ghost reset" @click="reset">重置草稿</button>
        <button class="btn-primary save" @click="save">保存</button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩/骨架/滚动区用 styles.css 的 .ov / .mdl / .mdl-hd / .mdl-bd / .mdl-ft */
.strategy-mdl { max-width: 560px; }
.head { display: flex; align-items: center; justify-content: space-between; }
.head h2 { margin: 0; font-weight: 900; font-size: 20px; color: #B01C24; }
.ver { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #1F6B3E; background: rgba(46,125,79,.1); padding: 1px 7px; border-radius: 6px; vertical-align: middle; }
.x { cursor: pointer; width: 30px; height: 30px; border-radius: 50%; border: 1px solid rgba(176, 28, 36,.25); background: rgba(255,255,255,.7); color: #B01C24; display: grid; place-items: center; }
.x:hover { background: rgba(176, 28, 36,.1); }
.sub { margin: 6px 0 0; font-size: 12px; color: #A89C90; line-height: 1.6; }
.items { display: flex; flex-direction: column; gap: 14px; margin-top: 18px; }
.item { border-radius: 14px; padding: 14px 16px; background: rgba(255,255,255,.75); border: 1px solid rgba(176, 28, 36,.12); }
.item-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.it-label { font-size: 13.5px; font-weight: 700; }
.it-desc { font-size: 11.5px; color: #8A7F76; margin-top: 3px; line-height: 1.5; }
.stepper { display: flex; align-items: center; gap: 6px; flex: none; }
.sb { cursor: pointer; width: 26px; height: 26px; border-radius: 7px; border: 1px solid rgba(176, 28, 36,.25); background: rgba(255,255,255,.9); color: #B01C24; font-size: 16px; line-height: 1; display: grid; place-items: center; }
.sb:hover { background: rgba(176, 28, 36,.1); }
.sval { font-weight: 700; font-size: 15px; color: #B01C24; min-width: 74px; text-align: center; }
.track { margin-top: 10px; height: 5px; border-radius: 999px; background: rgba(176, 28, 36,.1); overflow: hidden; }
.fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #C2333B, #8E1620); }
.toggle-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.switch { cursor: pointer; flex: none; width: 40px; height: 22px; border-radius: 999px; position: relative; transition: all .3s; background: rgba(110,98,88,.3); }
.switch.on { background: #3DA65E; box-shadow: 0 0 12px rgba(61,166,94,.6); }
.knob { position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%; background: #fff; transition: left .3s; box-shadow: 0 1px 4px rgba(0,0,0,.3); }
.switch.on .knob { left: 20px; }
.derived { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 16px; border-radius: 12px; background: rgba(176, 28, 36,.06); border: 1px solid rgba(176, 28, 36,.2); font-size: 12px; color: #6E6258; }
.derived b { font-size: 15px; color: #B01C24; }
.derived-note { font-size: 11px; color: #A89C90; }
.sys-title { font-size: 11.5px; font-weight: 700; letter-spacing: 2px; color: #8A7F76; margin-top: 2px; }
.reset { padding: 9px 18px; border-radius: 10px; font-size: 13px; }
.save { padding: 9px 24px; border-radius: 10px; font-size: 13px; font-weight: 600; letter-spacing: 2px; }
</style>
