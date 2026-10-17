<script setup lang="ts">
/** 屏3 · 智能问答:左快捷问题 + 历史会话,右聊天(markdown 答案 + 图表)。
 *  还原设计稿 isChat 屏。 */
import { nextTick, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { api } from '../api'
import type { ChartSpec } from '../lib/types'
import Chart from '../components/Chart.vue'

interface Msg { role: 'user' | 'ai'; text: string; html: string; chart?: ChartSpec }

const messages = ref<Msg[]>([])
const input = ref('')
const busy = ref(false)
const box = ref<HTMLElement | null>(null)

const CHIPS = ['查券 23国开10', '报价 230210.IB', '我的在谈概要']
const CONVS: string[] = []

function md(s: string): string { return DOMPurify.sanitize(marked.parse(s, { async: false }) as string, { USE_PROFILES: { html: true } }) }

async function send(text?: string) {
  const q = (text ?? input.value).trim()
  if (!q || busy.value) return
  input.value = ''
  messages.value.push({ role: 'user', text: q, html: '' })
  busy.value = true
  scrollDown()
  try {
    const r = await api.qa(q)
    if (!r.ok) throw new Error(r.hint || '问答服务读取失败')
    const d = r.data
    messages.value.push({ role: 'ai', text: d.answer, html: md(d.answer || '(空回答)'), chart: d.chart })
  } catch (e) {
    messages.value.push({ role:'ai', text:'请求失败', html:md(e instanceof Error ? e.message : '请求失败，请重试') })
  } finally {
    busy.value = false
    scrollDown()
  }
}
function newChat() { messages.value = []; input.value = '' }
function scrollDown() { nextTick(() => box.value?.scrollTo({ top: box.value.scrollHeight, behavior: 'smooth' })) }
</script>

<template>
  <main class="chat fade-up">
    <!-- 左:新会话 + 历史 -->
    <aside class="side glass">
      <div class="side-top">
        <button class="new-chat" @click="newChat">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14" /></svg>
          新会话
        </button>
        <p class="hint">尚未接入持久问答历史。当前问题仅保留在本页。</p>
      </div>
      <div class="conv-list">
        <button v-for="(cv, i) in CONVS" :key="i" class="conv" @click="send(cv)">{{ cv }}</button>
      </div>
    </aside>

    <!-- 右:聊天 -->
    <section class="main glass">
      <div ref="box" class="msgs">
        <!-- 空态 -->
        <div v-if="!messages.length" class="empty">
          <div class="orb">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4M8 4h8" /><rect x="4" y="8" width="16" height="12" rx="3" /><path d="M9 14h.01M15 14h.01" /></svg>
          </div>
          <h1 class="serif">您好,安琳</h1>
          <p class="lead">支持按代码或简称查券、查看已接入的 Bid / Ofr 对比图，以及本人的在谈概要。缺失数据会明确提示；目前不支持成交趋势或自由统计。</p>
          <p class="tip-sep">— 不知从何问起?试试下面的问题 —</p>
          <div class="chips">
            <button v-for="(c, i) in CHIPS" :key="i" class="chip" @click="send(c)">{{ c }}</button>
          </div>
        </div>

        <!-- 消息流 -->
        <div v-else class="thread">
          <template v-for="(m, i) in messages" :key="i">
            <div class="row" :class="m.role">
              <div v-if="m.role === 'user'" class="bubble user">{{ m.text }}</div>
              <div v-else class="bubble ai">
                <div class="md-body" v-html="m.html" />
                <Chart v-if="m.chart" :spec="m.chart" />
              </div>
            </div>
          </template>
          <div v-if="busy" class="row ai"><div class="bubble ai thinking">正在检索报价与对话记录…</div></div>
        </div>
      </div>

      <!-- 输入 -->
      <div class="input-bar">
        <div class="input-wrap">
          <input v-model="input" class="q-input" placeholder="输入：查券 简称 / 报价 代码 / 我的在谈概要" @keydown.enter="send()" />
          <button class="btn-primary send" @click="send()">发 送</button>
        </div>
        <p class="foot">回车即可查询 · 只读查询已接入行情与本人会话；不执行询价操作、不保存本页历史</p>
      </div>
    </section>
  </main>
</template>

<style scoped>
.chat { max-width: 1440px; margin: 0 auto; padding: 26px 32px 30px; display: grid; grid-template-columns: 290px 1fr; gap: 20px; height: calc(100vh - 66px); box-sizing: border-box; }
.side { border-radius: 18px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 8px 30px rgba(43,35,32,.06); }
.side-top { padding: 16px 16px 12px; }
.new-chat { cursor: pointer; width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px; border: 1px dashed rgba(176, 28, 36,.4); background: rgba(176, 28, 36,.05); color: #B01C24; padding: 11px; border-radius: 12px; font-family: inherit; font-size: 13.5px; font-weight: 600; letter-spacing: 1px; transition: all .2s; }
.new-chat:hover { background: rgba(176, 28, 36,.1); box-shadow: 0 0 14px rgba(176, 28, 36,.2); }
.hint { margin: 10px 2px 0; font-size: 11.5px; color: #A89C90; line-height: 1.5; }
.conv-list { flex: 1; overflow-y: auto; padding: 0 10px 14px; display: flex; flex-direction: column; gap: 3px; }
.conv { cursor: pointer; text-align: left; border: none; background: transparent; padding: 9px 12px; border-radius: 10px; font-family: inherit; font-size: 12.8px; color: #5C5148; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; transition: all .18s; }
.conv:hover { background: rgba(176, 28, 36,.07); color: #B01C24; }

.main { border-radius: 18px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 8px 30px rgba(43,35,32,.06); min-width: 0; }
.msgs { flex: 1; overflow-y: auto; padding: 28px 34px; display: flex; flex-direction: column; }

.empty { margin: auto; max-width: 760px; text-align: center; }
.orb { width: 64px; height: 64px; margin: 0 auto 18px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #C2333B, #8E1620); display: grid; place-items: center; box-shadow: 0 0 0 6px rgba(176, 28, 36,.08), 0 0 30px rgba(176, 28, 36,.4); animation: glowPulse 3s ease-in-out infinite; }
.empty h1 { margin: 0; font-weight: 900; font-size: 32px; letter-spacing: 1px; }
.lead { margin: 14px auto 0; font-size: 14px; line-height: 1.9; color: #6E6258; max-width: 640px; }
.lead b { color: #B01C24; }
.tip-sep { margin: 20px 0 0; font-size: 12px; color: #A89C90; letter-spacing: 1px; }
.chips { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; margin-top: 16px; }
.chip { cursor: pointer; padding: 10px 18px; border-radius: 999px; font-family: inherit; font-size: 13px; color: #5C5148; background: rgba(255,255,255,.7); border: 1px solid rgba(176, 28, 36,.2); backdrop-filter: blur(8px); transition: all .22s; }
.chip:hover { border-color: rgba(176, 28, 36,.6); color: #B01C24; box-shadow: 0 0 16px rgba(176, 28, 36,.25); transform: translateY(-2px); }

.thread { display: flex; flex-direction: column; gap: 16px; max-width: 860px; width: 100%; margin: 0 auto; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.ai { justify-content: flex-start; }
.bubble { max-width: 82%; padding: 13px 18px; border-radius: 16px; font-size: 14px; line-height: 1.8; backdrop-filter: blur(8px); }
.bubble.user { background: linear-gradient(135deg, #C2333B, #8E1620); color: #fff; box-shadow: 0 0 16px rgba(176, 28, 36,.3); }
.bubble.ai { background: rgba(255,255,255,.8); color: #2B2320; border: 1px solid rgba(176, 28, 36,.14); box-shadow: 0 4px 14px rgba(43,35,32,.06); }
.bubble.thinking { color: #8A7F76; }

.input-bar { padding: 16px 22px 20px; border-top: 1px solid rgba(176, 28, 36,.1); background: rgba(250,248,244,.55); backdrop-filter: blur(12px); }
.input-wrap { display: flex; gap: 12px; align-items: center; max-width: 960px; margin: 0 auto; }
.q-input { flex: 1; padding: 13px 18px; font-size: 14px; font-family: inherit; border-radius: 14px; border: 1px solid rgba(176, 28, 36,.2); background: rgba(255,255,255,.8); color: #2B2320; transition: all .2s; }
.q-input::placeholder { color: #B0A69B; }
.q-input:focus { outline: none; border-color: rgba(176, 28, 36,.6); box-shadow: 0 0 0 3px rgba(176, 28, 36,.12); }
.send { padding: 13px 30px; border-radius: 14px; font-size: 14px; font-weight: 600; letter-spacing: 3px; }
.foot { margin: 9px auto 0; max-width: 960px; font-size: 11.5px; color: #A89C90; }
</style>
