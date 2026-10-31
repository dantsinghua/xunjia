<script setup lang="ts">
/** 屏3 · 智能问答:左快捷问题 + 历史会话,右聊天(markdown 答案 + 图表)。
 *  还原设计稿 isChat 屏。 */
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch, type ComputedRef } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { api, type QaCandidate, type QaWorkflowState, type QaSession, type QaSessionDetail, type QaStoredMessage, type QaRequestScope } from '../api'
import type { BondRow, ChartSpec, Strategy } from '../lib/types'
import Chart from '../components/Chart.vue'
import RfqAssignModal from '../components/RfqAssignModal.vue'
import { imBoundAccount } from '../lib/imLink'
import { newRequestId } from '../lib/requestId'

interface Msg { id?: string; role: 'user' | 'ai'; text: string; html: string; chart?: ChartSpec;
  state?: 'pending' | 'complete' | 'failed'; requestId?: string;
  candidates?: QaCandidate[]; selected?: string[]; inquiry?: boolean; notice?: string }

const messages = ref<Msg[]>([])
const userDisplayName = inject<ComputedRef<string>>('authenticatedDisplayName') ?? computed(() => '')
const userId = inject<ComputedRef<string>>('authenticatedUserId') ?? computed(() => '')
const sessions = ref<QaSession[]>([]), activeSession = ref<QaSession | null>(null)
const nextSessionCursor=ref<string|null>(null)
const sessionBusy = ref(false), listBusy = ref(false), historyReady = ref(false)
const historyError = ref(''), sendError = ref('')
const pendingRequest = ref<(QaRequestScope & {question: string}) | null>(null)
const hasPending = computed(() => messages.value.some(message => message.state === 'pending'))
const input = ref('')
const busy = ref(false)
const box = ref<HTMLElement | null>(null)
const workflow = ref<QaWorkflowState | null>(null)
const workflowError = ref(''), inquiryError = ref(''), inquiryNotice = ref('')
const rfqOpen = ref(false), rfqOpening = ref(false), rfqBonds = ref<BondRow[]>([])
const savedFloors = ref<Record<string, {kind: 'bp' | 'yield'; value: number}>>({})
const strategy = ref<Strategy>()
let generation = 0, selectionRevision = 0, listRevision = 0, disposed = false
const requests = new Set<AbortController>()

const CHIPS = ['查券 23国开10', '报价 230210.IB', '我想询价 26广越14', '我的在谈概要']
function md(s: string): string { return DOMPurify.sanitize(marked.parse(s, { async: false }) as string, { USE_PROFILES: { html: true } }) }
function storedMessage(message: QaStoredMessage): Msg {
  const result = message.result
  const text = message.text || result?.answer || ''
  return { id: message.id, role: message.role, text, html: message.role === 'ai' ? md(text) : '',
    state: message.state, requestId: message.request_id, chart: result?.chart, candidates: result?.candidates,
    selected: [], inquiry: message.state === 'complete' && result?.requires_selection, notice: result?.notice }
}
async function bounded<T>(call: (signal: AbortSignal) => Promise<T>, timeout = 20000): Promise<T> {
  const controller = new AbortController()
  requests.add(controller)
  const timer = setTimeout(() => controller.abort(), timeout)
  try { return await call(controller.signal) }
  finally { clearTimeout(timer); requests.delete(controller) }
}
function cancelRequests() {
  generation++
  for (const controller of requests) controller.abort()
  requests.clear()
}
function clearInquiry() {
  inquiryError.value = ''; inquiryNotice.value = ''; rfqOpening.value = false
  rfqOpen.value = false; rfqBonds.value = []; savedFloors.value = {}; strategy.value = undefined
}
function applySession(detail: QaSessionDetail) {
  const restored = detail.messages.map(storedMessage)
  cancelRequests(); listRevision++; listBusy.value = false
  activeSession.value = detail.session
  messages.value = restored
  input.value = ''; busy.value = false; pendingRequest.value = null; sendError.value = ''
  const waiting = [...detail.messages].reverse().find(message => (message.state === 'pending'||message.state === 'failed') && message.request_id)
  const question = waiting && detail.messages.find(message => message.role === 'user' && message.request_id === waiting.request_id)
  if (waiting?.request_id && question) pendingRequest.value = {
    session_id: detail.session.id, request_id: waiting.request_id, question: question.text,
  }
  historyReady.value = true; clearInquiry(); scrollDown()
}
async function openSession(id: string): Promise<boolean> {
  if (disposed || sessionBusy.value) return false
  const request = generation, selection = ++selectionRevision
  sessionBusy.value = true; historyError.value = ''
  try {
    const response = await bounded(signal => api.qaSession(id, signal))
    if (disposed || request !== generation || selection !== selectionRevision) return false
    if (!response.ok || response.data?.session?.id !== id || !Array.isArray(response.data.messages))
      throw Error(response.hint || '会话历史读取失败，已保留当前会话')
    applySession(response.data)
    return true
  } catch (e) {
    if (!disposed && request === generation) historyError.value = e instanceof Error ? e.message : '读取会话失败，当前内容已保留'
    return false
  } finally { if (selection === selectionRevision) sessionBusy.value = false }
}
async function loadSessions(restoreLatest = false,more = false) {
  if (disposed || listBusy.value) return
  const request = generation, revision = ++listRevision
  listBusy.value = true; historyError.value = ''
  try {
    const response = await bounded(signal => api.qaSessions(signal,more?nextSessionCursor.value||undefined:undefined))
    if (disposed || request !== generation || revision !== listRevision) return
    if (!response.ok || !Array.isArray(response.data?.items)) throw Error(response.hint || '会话列表读取失败')
    const combined=more?[...sessions.value,...response.data.items]:response.data.items
    sessions.value = [...new Map(combined.map(item=>[item.id,item])).values()].sort((a, b) => b.updated_at - a.updated_at)
    nextSessionCursor.value=response.data.has_more?response.data.next_before||null:null
    if(response.data.notice)historyError.value=response.data.notice
    if (activeSession.value) activeSession.value = sessions.value.find(item => item.id === activeSession.value?.id) || activeSession.value
    if (restoreLatest && !activeSession.value && sessions.value.length) await openSession(sessions.value[0]!.id)
    else if (!activeSession.value) historyReady.value = true
  } catch (e) {
    if (!disposed && request === generation) historyError.value = e instanceof Error ? e.message : '会话列表读取失败'
  } finally { if (revision === listRevision) listBusy.value = false }
}
async function newChat(): Promise<boolean> {
  if (disposed || sessionBusy.value || listBusy.value) return false
  const request = generation, selection = ++selectionRevision
  sessionBusy.value = true; historyError.value = ''
  try {
    const response = await bounded(signal => api.createQaSession(signal))
    if (disposed || request !== generation || selection !== selectionRevision) return false
    if (!response.ok || !response.data?.id) throw Error(response.hint || '新会话创建失败')
    const session = response.data
    sessions.value = [session, ...sessions.value.filter(item => item.id !== session.id)]
    const history = await bounded(signal => api.qaSession(session.id, signal))
    if (disposed || request !== generation || selection !== selectionRevision) return false
    if (!history.ok || history.data?.session?.id !== session.id || !Array.isArray(history.data.messages))
      throw Error(history.hint || '新会话已创建，但历史读取失败；当前会话已保留')
    applySession(history.data)
    return true
  } catch (e) {
    if (!disposed && request === generation) historyError.value = e instanceof Error ? e.message : '新会话创建失败，当前内容已保留'
    return false
  } finally { if (selection === selectionRevision) sessionBusy.value = false }
}
async function submitQuestion(value: QaRequestScope & {question: string}) {
  if (busy.value || sessionBusy.value || disposed || activeSession.value?.id !== value.session_id) return
  const request = generation
  busy.value = true; sendError.value = ''
  try {
    const response = await bounded(signal => api.qa(value.question, signal, {
      session_id: value.session_id, request_id: value.request_id,
    }), 150000)
    if (disposed || request !== generation) return
    if (!response.ok) throw Error(response.hint || '问答请求未完成')
    const d = response.data
    if (d.session_id !== value.session_id) throw Error('返回的问答会话不一致，请重新读取当前会话')
    for (const message of messages.value) if (message.requestId === value.request_id) message.state = 'complete'
    messages.value.push({ role: 'ai', text: d.answer, html: md(d.answer), state: 'complete', chart: d.chart,
      candidates: d.candidates, selected: [], inquiry: d.requires_selection, notice: d.notice })
    pendingRequest.value = null
    void loadSessions()
  } catch (e) {
    if (!disposed && request === generation) sendError.value = (e instanceof Error ? e.message : '问答请求未完成') + '。请读取当前会话核对，或使用同一请求重试。'
  } finally { if (!disposed && request === generation) { busy.value = false; scrollDown() } }
}
async function send(text?: string) {
  const q = (text ?? input.value).trim()
  if (!q || busy.value || sessionBusy.value || listBusy.value || pendingRequest.value || hasPending.value || !historyReady.value) return
  if (!activeSession.value && !await newChat()) return
  if (!activeSession.value || disposed) return
  try {
    pendingRequest.value = {question: q, session_id: activeSession.value.id, request_id: newRequestId()}
  } catch (e) { sendError.value = e instanceof Error ? e.message : '请求编号生成失败'; return }
  input.value = ''
  messages.value.push({ role: 'user', text: q, html: '', state: 'pending', requestId: pendingRequest.value.request_id })
  scrollDown()
  await submitQuestion(pendingRequest.value)
}
function resetPage() {
  cancelRequests(); selectionRevision++; listRevision++
  sessions.value = []; activeSession.value = null; messages.value = []; input.value = ''
  nextSessionCursor.value=null
  busy.value = false; sessionBusy.value = false; listBusy.value = false; historyReady.value = false
  pendingRequest.value = null; sendError.value = ''; historyError.value = ''; clearInquiry()
}
async function retryQuestion() {
  if (pendingRequest.value) await submitQuestion(pendingRequest.value)
}
async function configureInquiry(message: Msg) {
  if (!message.inquiry || !message.selected?.length || rfqOpening.value || rfqOpen.value) return
  const selected = new Set(message.selected)
  const codes = (message.candidates || []).filter(candidate => selected.has(candidate.code)).map(candidate => candidate.code)
  if (!codes.length) return
  const request = generation
  rfqOpening.value = true; inquiryError.value = ''; inquiryNotice.value = ''
  try {
    const [bonds, floors, preferences] = await Promise.all([
      api.resolveBonds({windcodes: codes}), api.getFloors(), api.getStrategy(),
    ])
    if (disposed || request !== generation) return
    const resolved = new Set(bonds.data.map(row => row.code))
    if (!bonds.ok || bonds.data.length !== codes.length || resolved.size !== codes.length || codes.some(code => !resolved.has(code)))
      throw Error(bonds.hint || '所选债券资料已变化，请重新查券')
    const terminal = new Set(['待询价', '已放弃', '已撤价', '未成交', '已成交'])
    if (bonds.data.some(row => row.hasActiveIntent || !terminal.has(row.status)))
      throw Error('所选债券已有未结束询价，请处理当前会话，避免重复发起。')
    if (!floors.ok) throw Error(floors.hint || '请先绑定本人账号，再设置询价策略')
    const saved: typeof savedFloors.value = {}
    for (const item of floors.data.items) {
      const value = Number(item.value)
      if ((item.kind === 'yield' || item.kind === 'bp') && Number.isFinite(value)) saved[item.code] = {kind: item.kind, value}
    }
    savedFloors.value = saved; strategy.value = preferences.ok ? preferences.data : undefined
    rfqBonds.value = bonds.data; rfqOpen.value = true
  } catch (e) { if (!disposed && request === generation) inquiryError.value = e instanceof Error ? e.message : '询价准备信息读取失败' }
  finally { if (request === generation) rfqOpening.value = false }
}
function inquirySent() { rfqOpen.value = false; inquiryNotice.value = '询价结果已由服务端确认，可在机器人监控查看当前会话。' }
watch(() => JSON.stringify([userId.value, imBoundAccount.value?.account_uid, imBoundAccount.value?.workbench_id, imBoundAccount.value?.version, imBoundAccount.value?.context_id]), () => {
  resetPage()
  if (userId.value) void loadSessions(true)
})
onMounted(async () => {
  void loadSessions(true)
  const controller = new AbortController(); requests.add(controller)
  const timer = setTimeout(() => controller.abort(), 20000)
  try {
    const response = await api.qaWorkflow(controller.signal)
    if (!disposed && response.ok) workflow.value = response.data
  } catch { if (!disposed && !controller.signal.aborted) workflowError.value = '工作流配置状态暂不可读，实际查询结果会显示启用状态。' }
  finally { clearTimeout(timer); requests.delete(controller) }
})
onBeforeUnmount(() => { disposed = true; resetPage() })
function scrollDown() { nextTick(() => box.value?.scrollTo({ top: box.value.scrollHeight, behavior: 'smooth' })) }
</script>

<template>
  <main class="chat fade-up">
    <!-- 左:新会话 + 历史 -->
    <aside class="side glass">
      <div class="side-top">
        <button class="new-chat" type="button" :disabled="sessionBusy || listBusy" @click="newChat">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14" /></svg>
          新会话
        </button>
        <p class="hint">会话与问答结果保存在服务端，仅本人可读取。</p>
        <button class="history-refresh" type="button" :disabled="listBusy || sessionBusy" @click="loadSessions(!activeSession)">{{ listBusy ? '正在读取…' : '刷新会话列表' }}</button>
        <p v-if="historyError" class="inquiry-error" role="alert">{{ historyError }}</p>
        <p v-if="sessionBusy" class="hint" role="status">正在读取会话，成功后切换…</p>
      </div>
      <div class="conv-list">
        <button v-for="session in sessions" :key="session.id" class="conv" type="button"
          :class="{ active: activeSession?.id === session.id }" :aria-current="activeSession?.id === session.id ? 'page' : undefined"
          :disabled="sessionBusy" :title="session.title || '新会话'" @click="openSession(session.id)">{{ session.title || '新会话' }}</button>
        <p v-if="historyReady && !sessions.length" class="hint">暂无已保存的会话。</p>
        <button v-if="nextSessionCursor" type="button" class="history-refresh" :disabled="listBusy||sessionBusy" @click="loadSessions(false,true)">加载更早会话</button>
      </div>
    </aside>

    <!-- 右:聊天 -->
    <section class="main glass">
      <div class="conversation-heading">
        <b>{{ activeSession?.title || '智能问答' }}</b>
        <button v-if="activeSession" type="button" :disabled="sessionBusy" @click="openSession(activeSession.id)">读取当前会话</button>
      </div>
      <div ref="box" class="msgs">
        <!-- 空态 -->
        <div v-if="!messages.length" class="empty">
          <div class="orb">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4M8 4h8" /><rect x="4" y="8" width="16" height="12" rx="3" /><path d="M9 14h.01M15 14h.01" /></svg>
          </div>
          <h1 class="serif">您好<span v-if="userDisplayName">，{{ userDisplayName }}</span></h1>
          <p class="lead">可查找债券、读取当日已采集报价；启用独立工作流后可识别询价意图，在本页明确选券、设置策略、选择对手并确认。缺失行情不编造。</p>
          <p class="tip-sep">— 不知从何问起?试试下面的问题 —</p>
          <div class="chips">
            <button v-for="(c, i) in CHIPS" :key="i" class="chip" :disabled="busy || sessionBusy || listBusy || !historyReady" @click="send(c)">{{ c }}</button>
          </div>
        </div>

        <!-- 消息流 -->
        <div v-else class="thread">
          <template v-for="(m, i) in messages" :key="m.id || i">
            <div class="row" :class="m.role">
              <div v-if="m.role === 'user'" class="bubble user">{{ m.text }}</div>
              <div v-else class="bubble ai">
                <div class="md-body" v-html="m.html" />
                <p v-if="m.state === 'pending'" class="workflow-notice">此问题尚在处理，请读取当前会话核对结果。</p>
                <p v-else-if="m.state === 'failed'" class="inquiry-error">此条问答处理失败，已保留服务端记录。</p>
                <Chart v-if="m.chart" :spec="m.chart" />
                <p v-if="m.notice" class="workflow-notice">{{ m.notice }}</p>
                <div v-if="m.inquiry && m.candidates?.length" class="inquiry-candidates" data-testid="qa-inquiry-candidates">
                  <b>请明确选择本次询价债券</b>
                  <label v-for="candidate in m.candidates" :key="candidate.code">
                    <input v-model="m.selected" type="checkbox" :value="candidate.code" :disabled="rfqOpening || rfqOpen" />
                    <span>{{ candidate.code }} · {{ candidate.name }}<small v-if="!candidate.current_day">暂无可核验的当日报价</small></span>
                  </label>
                  <button class="btn-primary" type="button" data-testid="qa-configure-rfq" :disabled="!m.selected?.length || rfqOpening || rfqOpen" @click="configureInquiry(m)">{{ rfqOpening ? '正在核对当前状态…' : '设置策略并选择对手' }}</button>
                </div>
              </div>
            </div>
          </template>
          <div v-if="busy" class="row ai"><div class="bubble ai thinking">正在识别问题并读取可信行情，请稍候…</div></div>
        </div>
      </div>

      <!-- 输入 -->
      <div class="input-bar">
        <div class="input-wrap">
          <input v-model="input" class="q-input" maxlength="200" placeholder="查券、当日报价，或说明想询价的债券" @keydown.enter="send()" />
          <button class="btn-primary send" :disabled="busy || sessionBusy || listBusy || !historyReady || !!pendingRequest || hasPending || !input.trim()" @click="send()">发 送</button>
        </div>
        <p class="foot">{{ workflow?.hint || workflowError || '回车即可查询；模型只能读取行情，不能替你发送询价。' }}</p>
        <p class="foot">会话历史保存在服务端。询价须选券、设置策略、选择对手并确认；打开策略窗口不会发送。</p>
        <p v-if="sendError" role="alert" class="inquiry-error">{{ sendError }}</p>
        <p v-if="hasPending && !busy" class="foot" role="status">本会话仍有待确认的问答，请读取当前会话查看处理进度。</p>
        <button v-if="pendingRequest && !busy" class="retry-question" type="button" :disabled="sessionBusy" @click="retryQuestion">使用同一请求重试</button>
        <p v-if="inquiryError" role="alert" class="inquiry-error">{{ inquiryError }}</p>
        <p v-if="inquiryNotice" role="status" class="foot">{{ inquiryNotice }}</p>
      </div>
    </section>
  </main>
  <RfqAssignModal :open="rfqOpen" :bonds="rfqBonds" :picked-count="rfqBonds.length" :saved-floors="savedFloors" :strategy="strategy" @close="rfqOpen = false" @sent="inquirySent" @pending="inquiryNotice = '本批已提交，尚待回执；请勿重复发起。'" @failed="inquiryNotice = '本批发送结果请在策略窗口核对，不自动重发。'" />
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
.conv.active { background:rgba(176,28,36,.1);color:#B01C24;font-weight:600; }
.history-refresh,.conversation-heading button,.retry-question{font:inherit;font-size:12px;padding:6px 10px;border:1px solid #dacfc6;border-radius:7px;background:#fff;color:#5c5148;cursor:pointer}.history-refresh{margin-top:10px}.new-chat:disabled,.conv:disabled,.chip:disabled,.history-refresh:disabled{opacity:.5;cursor:not-allowed}.conversation-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 22px;border-bottom:1px solid rgba(176,28,36,.1);color:#5c5148}.conversation-heading b{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.conversation-heading button{flex-shrink:0}.retry-question{margin-top:8px}

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
.workflow-notice{font-size:11px;color:#8A7F76}.inquiry-candidates{display:grid;gap:10px;margin-top:14px;padding-top:12px;border-top:1px solid #e8e2dc;max-height:320px;overflow:auto}.inquiry-candidates label{display:flex;align-items:flex-start;gap:8px}.inquiry-candidates small{display:block;color:#8A7F76}.inquiry-candidates button{padding:8px 12px;border-radius:8px;cursor:pointer}.inquiry-candidates button:disabled,.send:disabled{opacity:.5;cursor:not-allowed}.inquiry-error{color:#a02e2e;font-size:12px}
</style>
