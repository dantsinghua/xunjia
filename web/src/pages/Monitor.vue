<script setup lang="ts">
/** 屏2 · 机器人监控:开关+统计条 / 左对手×券卡片(同对手多券标「同一线程⚠」) /
 *  中聊天流(bot·对方·人工·系统气泡,券锚高亮,意图标签,点消息看链路追踪) /
 *  同页人工发送框 / 右侧在聊债券(三步进度+价格+操作) / 询价明细·导出弹窗。
 *  还原设计稿 isMonitor 屏。 */
import { computed, nextTick, onMounted, onScopeDispose, reactive, ref, watch } from 'vue'
import { App } from 'ant-design-vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import { STATUS_COLOR } from '../theme'
import type { BondRow, ChatMsg, Counterparty, DetailRow, NegotiationQuote, CpBond } from '../lib/types'
import BondModal from '../components/BondModal.vue'
import TraceModal from '../components/TraceModal.vue'
import ImLinkBar from '../components/ImLinkBar.vue'
import ReconcileModal from '../components/ReconcileModal.vue'
import { imCurrent, imBoundAccount, imFeedError, imIdentity } from '../lib/imLink'
import { useBodyScrollLock } from '../lib/useModal'
import { monitorTradeDay, isCurrentInquiry, canResumeInquiry } from '../lib/monitorBonds'

const { message } = App.useApp()
const route = useRoute()
const reconcileOpen=ref(false)

const botOn = ref(false)
const serverBotStatus = ref('未知')
const botStarting = ref(false)
const stats = ref<{ k: string; v: string; d: string }[]>([])
const cps = ref<Counterparty[]>([])
const bonds = ref<BondRow[]>([])
const selCpId = ref('gl')
const activeBond = ref('')
const messageBondFocus = ref('')
const tradingDay = ref(monitorTradeDay())
const openBonds = reactive<Record<string, boolean>>({})
const chatInput = ref('')
const sendBusy = ref(false)
const sendPhase = ref<'' | 'submitted' | 'unknown' | 'delivered' | 'failed'>('')
const sendFail = ref('')
const traceMsg = ref<(ChatMsg & { idx: number; cp: string }) | null>(null)
const detailOpen = ref(false)
const exportOpen = ref(false)
useBodyScrollLock(() => detailOpen.value || exportOpen.value)
const detailRows = ref<DetailRow[]>([])
const modalBond = ref<BondRow | null>(null)
function localDate(d:Date){return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`}
const expFrom = ref(localDate(new Date(Date.now()-6*86400000)))
const expTo = ref(localDate(new Date()))
const exportAllPeers=ref(false)
const expQuick = ref<number | string | null>(7)

onMounted(async () => {
  void connectUpdates()
  await reloadDesk()
  if (disposed) return
  const b = await api.bonds()
  if (b.ok) bonds.value = b.data
  if (disposed) return
  deskTimer = setInterval(() => void reloadDesk(), 3000)
  rawTimer = setInterval(() => void loadChat(), 1000)
})

const selCp = computed(() => cps.value.find(c => c.id === selCpId.value) || cps.value[0])
function visibleBonds(cp?: Counterparty): CpBond[] {
  return (cp?.bonds || []).filter(bond => isCurrentInquiry(bond, tradingDay.value))
}
const currentBonds = computed(() => visibleBonds(selCp.value))
const resumableBonds = computed(() => (selCp.value?.bonds || []).filter(bond => canResumeInquiry(bond, tradingDay.value)))
function syncActiveBond() {
  if (currentBonds.value.some(bond => bond.code === activeBond.value)) return
  const requested = route.query.bond as string | undefined
  activeBond.value = currentBonds.value.find(bond => bond.code === requested)?.code || currentBonds.value[0]?.code || ''
  Object.keys(openBonds).forEach(key => delete openBonds[key])
  if (activeBond.value) openBonds[activeBond.value] = true
}
watch(currentBonds, syncActiveBond, { flush: 'sync' })
const botStatus = computed(() => botStarting.value ? '启动中' : serverBotStatus.value)
const botDot = computed(() => botStarting.value ? '#E0A030' : botOn.value ? '#3DA65E' : '#B0A69B')


onScopeDispose(() => { if (deskTimer) clearInterval(deskTimer);if(rawTimer)clearInterval(rawTimer) })

async function toggleBot() {
  if (botStarting.value) return
  botStarting.value = true
  try {
    const r = await api.botToggle(!botOn.value)
    if (!r.ok) { message.error(r.hint || '机器人状态没有修改'); return }
    await reloadDesk()
  } finally { botStarting.value = false }
}

function st(status: string) { return STATUS_COLOR[status] || STATUS_COLOR['待询价'] }

/* ---- 左:对手卡 ---- */
function selectCp(cp: Counterparty) {
  selCpId.value = cp.id
  messageBondFocus.value = ''
  activeBond.value = visibleBonds(cp)[0]?.code ?? ''
  Object.keys(openBonds).forEach(k => delete openBonds[k])
  if (activeBond.value) openBonds[activeBond.value] = true
}

/* ---- 中:聊天流 ---- */
// 聊天原始数据缓存(异步载入)
const rawChatCache = reactive<Record<string, ChatMsg[]>>({})
const chatFlow = ref<HTMLElement | null>(null)
function shownText(text: string) {
  return String(text || '')
}
function quotePrice(q:NegotiationQuote):string {
  if(!q.price)return '—'
  return `${q.price.value}${q.price.kind==='YIELD'?'%':q.price.kind==='NET_PRICE'?' 净价':q.price.kind==='DIRTY_PRICE'?' 全价':' '+q.price.kind}`
}
function quoteValidity(q:NegotiationQuote,day?:string):string {
  if(q.validity==='WITHDRAWN')return '已撤回，不计覆盖'
  if(q.validity==='UNCONFIRMED'||!q.confirmed_day)return '待对手确认，不计覆盖'
  if(!day || q.confirmed_day!==day)return '跨日待对手确认，不计覆盖'
  return '当日有效'
}
function counterpartyQuote(b: CpBond): NegotiationQuote | undefined {
  const side = b.trade?.own_side === 'BUY' ? 'ofr' : b.trade?.own_side === 'SELL' ? 'bid' : undefined
  return side ? b.quotes?.[side] : undefined
}
function counterpartyPrice(b: CpBond): string {
  const quote = counterpartyQuote(b)
  return quote ? quotePrice(quote) : b.quotes ? '—' : b.trade?.peer_quote || '—'
}
function counterpartyVolume(b: CpBond): string {
  const quote = counterpartyQuote(b)
  return quote ? quote.volume_wan ?? '—' : b.quotes ? '—' : b.trade?.peer_volume_wan || '—'
}
// Controls are keyed by the immutable peer/bond member, never by the current tab.
const bondRequests = reactive(new Set<string>())
const pendingBondControls = reactive(new Map<string, boolean>())
let accountGeneration = 0
function bondControlBusy(b: CpBond): boolean {
  return !!b.sessionId && (bondRequests.has(b.sessionId) || pendingBondControls.has(b.sessionId))
}
async function toggleBondNegotiation(b: CpBond) {
  const memberId = b.sessionId
  if (!memberId || typeof b.memberSendAllowed !== 'boolean' || bondControlBusy(b)) return
  const allowed = !b.memberSendAllowed
  const account = accountScope(), generation = accountGeneration, cp = selCp.value?.id
  bondRequests.add(memberId)
  try {
    const response = await api.memberSending(memberId, allowed)
    if (disposed || account !== accountScope() || generation !== accountGeneration) return
    if (!response.ok) {
      message.error(response.hint || '本券谈价状态未更新')
      return
    }
    // Wait for the authoritative projection before flipping the button label.
    pendingBondControls.set(memberId, allowed)
    await reloadDesk()
    if (cp === selCp.value?.id) {
      message.success(allowed ? '已恢复本券，沿用原会话、报价和谈价进度；全局或券组暂停仍生效'
        : '已暂停与该对手的本券谈价；其他债券和对手不受影响')
    }
  } finally { bondRequests.delete(memberId) }
}


const chatMsgs = computed(() => {

  const cp = selCp.value
  if (!cp) return []
  const raw = rawChatCache[cp.id] || []
  const list = raw
  return list.map((m, idx) => {
    const contexts = Array.isArray((m as any).contexts) ? (m as any).contexts : []
    const hasBond = contexts.length > 0 || !!m.bond
    const focus = messageBondFocus.value || activeBond.value
    const hit = contexts.some((c: { code?: string }) => c.code === focus) || m.bond === focus
    const dim = !!focus && (!hasBond || !hit)
    const isBot = m.role === 'bot'
    const isHuman = m.role === 'human'
    return {
      ...m, idx, cp: cp.name,
      isSys: m.role === 'sys', isMsg: m.role !== 'sys',
      dim, isBot, isHuman,
      alignRight: isBot || isHuman || m.role === 'self',
      contexts, ctxActive: hit,
    }
  })
})
const heroNick = computed(() => imIdentity(imCurrent.value).nick)
const heroUid = computed(() => imIdentity(imCurrent.value).uid || '未接入')
const deskTitle = computed(() => {
  const link = imCurrent.value
  if (!link) return '尚未接入采集端'
  if (link.workbench_id) return '工作台 ' + link.workbench_id
  return link.name || '采集端'
})
const sendLabel = computed(() => unknownPending.value ? '待核对' : sendPhase.value === 'delivered' ? '已发送' : sendPhase.value === 'submitted' ? '已提交' : '发 送')

let deskTimer: ReturnType<typeof setInterval> | null = null
let rawTimer: ReturnType<typeof setInterval> | null = null
let deskBusy = false
let deskQueued=false
let deskGeneration = 0
let chatGeneration = 0
let disposed = false
let counterpartiesRevision=0
let summaryRevision=0
const chatRevisions:Record<string,number>={}
let lastEventRevision=0
let updatesController:AbortController|null=null
let updatesTimer:ReturnType<typeof setTimeout>|null=null
const updatesState=ref('连接中')
function stopUpdates(){
  updatesController?.abort();updatesController=null
  if(updatesTimer)clearTimeout(updatesTimer)
  updatesTimer=null
}
async function connectUpdates(){
  stopUpdates()
  if(disposed)return
  const controller=new AbortController();updatesController=controller
  const account=accountScope()
  try {
    await api.negotiationUpdates(lastEventRevision,controller.signal,revision=>{
      if(account!==accountScope()||disposed)return
      lastEventRevision=revision;void reloadDesk()
    },()=>{updatesState.value='实时连接';void reloadDesk()})
  } catch(error){
    if(!controller.signal.aborted)updatesState.value='连接中断，正在补拉'
  } finally {
    if(!disposed&&!controller.signal.aborted&&account===accountScope()){
      updatesState.value='连接中断，正在补拉'
      updatesTimer=setTimeout(()=>void connectUpdates(),1500)
    }
  }
}
const accountScope = () => `${imBoundAccount.value?.id || ''}:${imBoundAccount.value?.account_uid||''}:${imBoundAccount.value?.workbench_id||''}:${imBoundAccount.value?.version||0}:${imBoundAccount.value?.context_id||''}`
const chatPageCursor=reactive<Record<string,string>>({})
const chatNextCursor=reactive<Record<string,string>>({})
async function olderChat(){const cp=selCp.value?.id;if(cp&&chatNextCursor[cp]){chatPageCursor[cp]=chatNextCursor[cp];await loadChat()}}
async function latestChat(){const cp=selCp.value?.id;if(cp){chatPageCursor[cp]='';await loadChat()}}
let chatInFlight:{key:string;promise:Promise<void>}|null=null
function loadChat():Promise<void> {
  const cp=selCp.value?.id
  if(!cp)return Promise.resolve()
  const key=JSON.stringify([cp,chatPageCursor[cp]||'',accountScope()])
  if(chatInFlight?.key===key)return chatInFlight.promise
  const promise=fetchChat().finally(()=>{if(chatInFlight?.promise===promise)chatInFlight=null})
  chatInFlight={key,promise}
  return promise
}
async function fetchChat() {
  const cp = selCp.value?.id
  if (!cp) return
  const generation = ++chatGeneration
  const account = accountScope()
  const r=await api.monitorChat(cp,chatPageCursor[cp]||'')
  if (disposed || generation !== chatGeneration || account !== accountScope() || cp !== selCp.value?.id) return
  if (r.ok) {
    if(r.revision!=null && r.revision<(chatRevisions[cp]||0))return
    const previous=new Map((rawChatCache[cp]||[]).filter(m=>m.id).map(m=>[m.id,m]))
    rawChatCache[cp]=r.data.map(m=>{const old=m.id?previous.get(m.id):undefined;return old && (old.revision??0)>(m.revision??0)?old:m})
    if(r.revision!=null)chatRevisions[cp]=r.revision
    chatNextCursor[cp]=r.nextCursor||''; imFeedError.value = ''
  }
  else imFeedError.value = r.hint || '消息拉取失败，保留上次成功内容'
}
async function reloadDesk() {
  if (disposed)return
  tradingDay.value = monitorTradeDay()
  if (deskBusy){deskQueued=true;return}
  deskBusy = true
  const generation = ++deskGeneration
  const account = accountScope()
  const initialCp=selCp.value?.id
  // Raw messages never wait for model projections or collector receipts.
  const chatRequest=loadChat()
  const receiptsRequest=pollReceipts()
  try {
    const [s, c] = await Promise.all([api.summary(), api.counterparties()])
    if (disposed || generation !== deskGeneration || account !== accountScope()) return
    if (s.ok && (s.revision==null||s.revision>=summaryRevision)) { stats.value = s.data.stats; serverBotStatus.value = s.data.botStatus; botOn.value = s.data.botEnabled ?? s.data.botStatus === '正常';summaryRevision=s.revision??summaryRevision }
    if (c.ok && (c.revision==null||c.revision>=counterpartiesRevision)) {
      cps.value = c.data
      counterpartiesRevision=c.revision??counterpartiesRevision
      for (const [memberId, allowed] of pendingBondControls) {
        const current = c.data.flatMap(cp => cp.bonds).find(b => b.sessionId === memberId)
        if (current?.memberSendAllowed === allowed) pendingBondControls.delete(memberId)
      }
      if (!cps.value.some(x => x.id === selCpId.value)) {
        const requested=route.query.cp as string|undefined
        selCpId.value=cps.value.find(x=>x.id===requested)?.id||cps.value[0]?.id||''
      }
      syncActiveBond()
    }
    await Promise.all([chatRequest,receiptsRequest])
    if(selCp.value?.id!==initialCp)await loadChat()
    if (!s.ok || !c.ok) imFeedError.value = s.hint || c.hint || '刷新失败，保留上次成功内容'
  } catch (e) { imFeedError.value = e instanceof Error ? e.message : '刷新失败' }
  finally { deskBusy = false;if(deskQueued&&!disposed){deskQueued=false;void reloadDesk()} }
}
watch(() => selCp.value?.id, () => { sendPhase.value = ''; sendFail.value = ''; void loadChat() })
watch(accountScope, () => {
  accountGeneration++; bondRequests.clear(); pendingBondControls.clear()
  deskGeneration++; chatGeneration++
  counterpartiesRevision=0;summaryRevision=0
  lastEventRevision=0;stopUpdates()
  for(const key of Object.keys(chatRevisions))delete chatRevisions[key]
  Object.keys(chatPageCursor).forEach(k=>delete chatPageCursor[k]);Object.keys(chatNextCursor).forEach(k=>delete chatNextCursor[k])
  cps.value = []; selCpId.value = ''; activeBond.value = ''; messageBondFocus.value = ''
  for (const key of Object.keys(rawChatCache)) delete rawChatCache[key]
  pendingReceipts.clear()
  sendPhase.value = ''; sendFail.value = ''
  void reloadDesk()
  void connectUpdates()
})
onScopeDispose(() => { disposed = true; deskGeneration++; chatGeneration++;stopUpdates() })
watch(chatMsgs, async () => {
  await nextTick()
  const box = chatFlow.value
  if (box) box.scrollTop = box.scrollHeight
})

let draftRevision = 0
watch(chatInput, () => { draftRevision++ }, {flush:'sync'})
watch(chatInput, (val) => {
  if (!val) return
  if (sendPhase.value === 'delivered' || sendPhase.value === 'failed') {
    sendPhase.value = ''
    sendFail.value = ''
  }
})

let sendAttempt: { fingerprint:string; id:string } | null = null
interface SendOrigin { cp:string; bond:string; account:string; draft:string; revision:number; requestId:string; status?:string }
const pendingReceipts = reactive(new Map<string, SendOrigin>())
const unknownPending=computed(()=>selCp.value?.bonds.some(b=>b.commandState==='UNKNOWN')||[...pendingReceipts.values()].some(origin=>origin.account===accountScope()&&origin.cp===selCp.value?.id&&origin.status==='unknown'))
let receiptBusy = false
async function pollReceipts() {
  if (receiptBusy || disposed) return
  receiptBusy = true
  try {
    for (const [cmdId, origin] of pendingReceipts) {
      if (origin.account !== accountScope()) { pendingReceipts.delete(cmdId); continue }
      const current = () => !disposed && origin.account === accountScope() && origin.cp === selCp.value?.id && origin.requestId===sendAttempt?.id
      try {
        const rec = await api.imReceipt(cmdId)
        if (disposed || origin.account !== accountScope()) return
        if (!rec.ok || (rec.data.cmd_id && rec.data.cmd_id !== cmdId)) {
          if (current()) { sendPhase.value = 'submitted'; sendFail.value = rec.hint || '回执暂不可读，等待确认' }
          continue
        }
        origin.status=rec.data.status
        if (rec.data.status === 'delivered' || rec.data.status === 'sent') {
          pendingReceipts.delete(cmdId)
          if (current()) {
            sendAttempt = null; sendPhase.value = 'delivered'; sendFail.value = ''
            if (draftRevision === origin.revision && chatInput.value === origin.draft) chatInput.value = ''
            await loadChat()
          }
        } else if (['failed','cancelled','expired'].includes(rec.data.status)) {
          pendingReceipts.delete(cmdId)
          if (current()) { sendPhase.value = 'failed'; sendFail.value = rec.data.reason || '发送被拒绝、取消或过期';sendAttempt=null }
        } else if(rec.data.status==='unknown') {
          if(current()){sendPhase.value='unknown';sendFail.value='结果未知，请核对未决指令；不会自动重发'}
        } else if (current()) { sendPhase.value = 'submitted'; sendFail.value = '已提交，还没有采集端回执' }
      } catch (e) {
        if (current()) { sendPhase.value = 'submitted'; sendFail.value = e instanceof Error ? e.message : '回执读取失败，等待确认' }
      }
    }
  } finally { receiptBusy = false }
}
async function sendChat() {
  const t = chatInput.value.trim()
  if (!t || !selCp.value || sendBusy.value || unknownPending.value || imCurrent.value?.catalog_stale) return
  const origin = { cp:selCp.value.id, bond:activeBond.value, account:accountScope(), draft:chatInput.value, revision:draftRevision, requestId:'' }
  const stillCurrent = () => origin.cp === selCp.value?.id && origin.account === accountScope()
  const fingerprint = JSON.stringify([origin.cp, origin.bond, origin.account, t])
  if (sendAttempt?.fingerprint !== fingerprint) sendAttempt = { fingerprint, id:crypto.randomUUID() }
  const requestId = sendAttempt.id
  origin.requestId=requestId
  sendBusy.value = true
  sendFail.value = ''
  try {
    const r = await api.monitorSend(origin.cp, origin.bond, t, requestId)
    if (r.ok && r.data?.cmd_id) pendingReceipts.set(r.data.cmd_id, origin)
    if (!stillCurrent()) return
    const blocked = !!r.blocked || !!r.data?.blocked
    const localHit = /成交|按这个价做|我们要了|\bdone\b/i.test(t)
    if (blocked || (localHit && r.ok && !r.data?.cmd_id)) {
      message.warning('触发成交词 · 红线闸拦截,消息未发送(本系统仅询价，不支持成交)')
      sendPhase.value = ''
      return
    }
    if (!r.ok) {
      message.error(r.hint || '未发送')
      return
    }
    if (r.data?.cmd_id) {
      sendPhase.value = 'submitted'
      await pollReceipts()
      return
    }
    message.error('没有发出去')
    sendPhase.value = 'failed'
    sendFail.value = '没有发出去'
  } catch {
    if (!stillCurrent()) return
    sendPhase.value = 'failed'
    sendFail.value = '失败'
  } finally {
    sendBusy.value = false
  }
}

async function pauseSession(memberId?:string) {
  const mid=memberId||selCp.value?.bonds.find(b=>b.code===activeBond.value)?.sessionId
  if (!mid) {message.warning('请先选择需要接管的单券子会话');return}
  const r = await api.pauseSession(mid)
  if (!r.ok) { message.error(r.hint || '没有暂停'); return }
  message.success('已接管该券子会话；接收与分析继续')
  await reloadDesk()
}
async function resumeSession(memberId?:string) {
  const mid=memberId||selCp.value?.bonds.find(b=>b.code===activeBond.value)?.sessionId
  if (!mid) {message.warning('请先选择需要恢复的单券子会话');return}
  const r = await api.resumeSession(mid)
  if (!r.ok) { message.error(r.hint || '没有交回'); return }
  message.success('已恢复本券对手发送；旧候选不补发，由最新事实重新判断')
  await reloadDesk()
}
async function widenFloor(code: string) {
  message.info(`${code} 请先停止本轮并完成对账，再到意向询价页调整底价、重新预览确认。`)
}
async function markBond(code: string, status: string,groupId?:string) {
  const gid=groupId||selCp.value?.bonds.find(b=>b.code===code)?.groupId
  const r = gid?await api.groupSending(gid,false):await api.markInquiry(code, status)
  if (!r.ok) { message.error(r.hint || '没有改状态'); return }
  message.success(code + ' 已停止相关询价；未决指令继续对账')
  await reloadDesk()
}
async function archiveSession(id:string){
  try{const result=await api.archiveSession(id);if(!result.ok)throw Error(result.hint||'归档失败');await reloadDesk();message.success('该会话已归档，审计记录保留')}
  catch(error){message.error(error instanceof Error?error.message:'归档失败')}
}
function focusBond(bond: string) {
  messageBondFocus.value = bond
  // A historical message may be focused without reviving an expired bond card.
  if (!currentBonds.value.some(item => item.code === bond)) return
  activeBond.value = bond
  Object.keys(openBonds).forEach(k => delete openBonds[k])
  openBonds[bond] = true
}
function openTrace(m: ChatMsg & { idx: number; cp: string }) { traceMsg.value = m }

/* ---- 右:在聊债券 ---- */
const STEPS = ['待询价', '询价中', '转人工']
function toggleBond(name: string) {
  messageBondFocus.value = ''
  if (activeBond.value === name && openBonds[name]) { Object.keys(openBonds).forEach(k => delete openBonds[k]) }
  else { activeBond.value = name; Object.keys(openBonds).forEach(k => delete openBonds[k]); openBonds[name] = true }
}
function openBondModal(code: string) {
  const b = bonds.value.find(x => x.code === code)
  if (b) modalBond.value = b
}

/* ---- 明细 / 导出 ---- */
async function openDetail() {
  if (selCp.value) {
    const cp = selCp.value.id
    const r = await api.detail(selCp.value)
    if (!r.ok) { message.error(r.hint || '读取明细失败'); return }
    if (selCp.value?.id !== cp) return
    detailRows.value = r.data
  }
  detailOpen.value = true
}
const DETAIL_COLS = ['剩余期限', '我方交易方向', '中债估值', '对方最终报价', '对方报量(万)', '我方底价', '我方报量(万)', '距底价价差', '我方交易员', '投资经理']
const EXP_QUICK = [['近 7 天', 7], ['近 30 天', 30], ['本月', 'month'], ['全部', 'all']] as [string, number | string][]
function setExpRange(k: number | string) {
  const to = new Date(), from = new Date()
  if (k === 'month') from.setDate(1)
  else if (k === 'all') from.setFullYear(from.getFullYear() - 1)
  else from.setDate(from.getDate() - (k as number) + 1)
  const f = (d: Date) => { const p = (n: number) => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` }
  expQuick.value = k; expFrom.value = f(from); expTo.value = f(to)
}
async function doExport() {
  try {
    const file=await api.exportMonitor(expFrom.value,expTo.value,exportAllPeers.value ? undefined : selCp.value?.id)
    const url=URL.createObjectURL(file.blob);const link=document.createElement('a')
    link.href=url;link.download=file.filename;document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url)
    message.success('已导出本人记录')
  } catch(e) {message.error(e instanceof Error ? e.message : '导出失败')}
}

function dirColor(d: string) { return d === '买入' ? '#B01C24' : '#1F6B3E' }
function dirBg(d: string) { return d === '买入' ? 'rgba(176, 28, 36,.1)' : 'rgba(46,125,79,.12)' }

function onJump(cpId: string, bondName: string) {
  modalBond.value = null
  const cp = cps.value.find(x => x.id === cpId)
  if (cp) { selectCp(cp); if (bondName) focusBond(bondName) }
}
</script>

<template>
  <main class="monitor fade-up">
    <ImLinkBar :show-address-book-details="false" />
    <ReconcileModal :open="reconcileOpen" @close="reconcileOpen=false" @resolved="reloadDesk" />
    <!-- 统计条 -->
    <section class="hero">
      <div class="hero-glow" />
      <div class="hero-l">
        <div class="hero-line">
          <span class="qd">企点</span><template v-if="heroNick"><b>{{ heroNick }}</b><span class="sep">·</span></template><b class="mono">{{ heroUid }}</b><span class="sep">·</span>
          <span class="bot-status"><span class="dot" :style="{ background: botDot, boxShadow: '0 0 10px ' + botDot }" :class="{ pulse: botOn && !botStarting }" />{{ botStatus }}</span>
        </div>
        <div class="hero-line">
          <span class="desk">{{ deskTitle }}</span>
          <div class="bot-sw" :class="{ on: botOn }" @click="toggleBot"><span class="knob" /></div>
        </div>
        <div class="hero-note">授权内询价 · 停止发送仍继续接收与分析 · 不支持成交 · {{ updatesState }}</div>
      </div>
      <div class="hero-stats">
        <div v-for="(s, i) in stats" :key="i" class="stat">
          <div class="stat-top"><span class="sk">{{ s.k }}</span><span class="sv mono">{{ s.v }}</span></div>
          <div class="sd">{{ s.d }}</div>
        </div>
      </div>
    </section>

    <!-- 三栏 -->
    <section class="cols">
      <!-- 左:对手 -->
      <aside class="panel cp-panel">
        <div class="panel-hd"><b>在谈对手</b><span class="cnt">{{ cps.length }} 个</span></div>
        <div class="panel-body">
          <p v-if="!cps.length" class="empty-real">这个企点号还没有上来的对话。</p>
          <div v-for="cp in cps" :key="cp.id" class="cp-card" :class="{ on: cp.id === selCpId }" @click="selectCp(cp)">
            <div class="cp-top">
              <span class="qd">企点</span><b>{{ cp.name }}</b>
              <span v-if="cp.username" class="mono cp-user">{{ cp.username }}</span>
              <span v-if="visibleBonds(cp).length > 1" class="thread-warn" title="同一对手多券共用一个线程/thread">同一线程 ⚠</span>
              <span v-if="visibleBonds(cp)[0]" class="dot" :style="{ background: st(visibleBonds(cp)[0].stage)[1], boxShadow: '0 0 8px ' + st(visibleBonds(cp)[0].stage)[1] }" />
            </div>
            <div class="cp-bonds">
              <span v-if="!visibleBonds(cp).length" class="history-only">今日暂无询价中债券，可查看历史对话</span>
              <div v-for="b in visibleBonds(cp)" :key="b.sessionId || b.code" class="cp-bond">
                <span class="cb-name mono">{{ b.name }}</span>
                <span class="stage" :style="{ background: st(b.stage)[0], color: st(b.stage)[1] }">{{ b.stage }}</span>
              </div>
            </div>
          </div>
        </div>
      </aside>

      <!-- 中:聊天 -->
      <section class="panel chat-panel">
        <div class="chat-hd">
          <span class="qd">企点</span>
          <div class="chat-hd-mid"><div class="ch-name">{{ selCp?.name }}</div><div class="ch-org">{{ selCp?.username || selCp?.org }} · 在聊 {{ currentBonds.length }} 只</div></div>
          <button class="hd-btn primary" @click="openDetail">询价明细</button>
          <button class="hd-btn" @click="exportOpen = true">导出记录</button>
          <button class="hd-btn" @click="reconcileOpen = true">核对未决指令</button>
        </div>
        <div class="chat-tip">原文收到后立即显示，识券与价量随后更新。切换右侧债券可聚焦对应消息；尚未识别或涉及其他券的原文仍保留。</div>
        <div ref="chatFlow" class="chat-flow">
          <div class="chat-pages"><button class="hd-btn" :disabled="!chatNextCursor[selCpId]" @click="olderChat">更早50条</button><button v-if="chatPageCursor[selCpId]" class="hd-btn" @click="latestChat">回到最新消息</button></div>
          <template v-for="m in chatMsgs" :key="m.id || m.idx">
            <div v-if="m.isSys" class="sys-pill" :style="{ opacity: m.dim ? 0.4 : 1 }">{{ shownText(m.text) }} · <span class="mono">{{ m.time }}</span></div>
            <div v-else class="msg" :class="{ right: m.alignRight }" :data-message-id="m.id" :data-revision="m.revision" :style="{ opacity: m.dim ? 0.4 : 1 }">
              <div class="bubble" :class="{ bot: m.isBot, human: m.isHuman, peer: !m.alignRight }" title="点击查看数据链路追踪" @click="openTrace(m)">{{ shownText(m.text) }}</div>
              <div class="msg-meta" :class="{ right: m.alignRight }">
                <span class="tag">{{ m.tag }}</span>
                <span class="mtime mono">{{ m.time }}</span>
                <span v-if="m.analysisState === 'QUEUED' || ['识别中','解析中'].includes(m.bond_state || '')" class="ctx">原文已接收 · 解析中</span>
                <span v-else-if="m.analysisState === 'ANALYZED'" class="ctx">已分析</span>
                <span v-if="m.kind === 'retract'" class="ctx">撤回消息 · {{ m.targetId }}</span>
                <span v-else-if="m.kind === 'edit'" class="ctx">修订消息 · {{ m.targetId }}</span>
                <template v-if="m.contexts && m.contexts.length">
                  <span v-for="c in m.contexts" :key="c.memberId || c.code" class="ctx" :class="{ active: c.code === (messageBondFocus || activeBond) }" @click.stop="focusBond(c.code)">{{ c.name || c.code }}</span>
                </template>
              </div>
            </div>
          </template>
        </div>
      </section>

      <!-- 右:在聊债券 -->
      <aside class="panel bond-panel">
        <div class="panel-hd"><b>当前在聊债券</b><span class="cnt">{{ currentBonds.length }} 只</span></div>
        <details v-if="resumableBonds.length" class="resume-menu" data-testid="paused-bond-actions">
          <summary>恢复本券</summary>
          <button v-for="b in resumableBonds" :key="b.sessionId" class="rba" type="button"
            :disabled="bondControlBusy(b)" @click="toggleBondNegotiation(b)">{{ b.name }} · 恢复本券</button>
        </details>
        <div class="panel-body">
          <p v-if="!currentBonds.length" class="empty-real" data-testid="current-bonds-empty">今日暂无询价中债券，历史消息及识券结果仍保留在对话中。</p>
          <div v-for="b in currentBonds" :key="b.sessionId || b.code" class="rb-card" :class="{ act: b.code === activeBond }">
            <div class="rb-hd" @click="toggleBond(b.code || '')">
              <span class="rb-name mono" @click.stop="openBondModal(b.code || '')">{{ b.name }}</span>
              <span class="stage" :style="{ background: st(b.stage)[0], color: st(b.stage)[1] }">{{ b.stage }}</span>
              <svg class="caret" :style="{ transform: `rotate(${openBonds[b.code || ''] ? 180 : 0}deg)` }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#8A7F76" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6" /></svg>
            </div>
            <div v-if="openBonds[b.code || '']" class="rb-body">
              <p v-if="b.analysisPending" class="analysis-note" data-testid="analysis-pending">新原文解析中；以下仍是最近已确认的结构化事实。</p>
              <p v-if="b.responseState === 'NO_RESPONSE' || b.responseState === 'UNRESPONSIVE'" class="analysis-note">对手一小时未响应；不代表最终实价或确认成交。</p>
              <p v-if="b.pendingCommandId" class="analysis-note" data-testid="command-state">{{ b.commandState === 'UNKNOWN' ? '结果未知，仅等待对账，不自动重发' : '发送处理中，等待原生回执' }} · <span class="mono">{{ b.pendingCommandId }}</span></p>
              <!-- 三步进度 -->
              <div class="steps">
                <div class="steps-line" />
                <div v-for="(sname, i) in STEPS" :key="i" class="step-node">
                  <span class="sdot" :style="{ background: i <= b.step ? ['#8F6412','#1E5FB4','#B01C24'][i] : 'rgba(176, 28, 36,.15)', boxShadow: i === b.step ? '0 0 10px ' + ['#8F6412','#1E5FB4','#B01C24'][i] : 'none' }" />
                  <span class="slabel" :style="{ color: i <= b.step ? ['#8F6412','#1E5FB4','#B01C24'][i] : '#A89C90', fontWeight: i === b.step ? 700 : 400 }">{{ sname }}</span>
                </div>
              </div>
              <div class="rb-grid" data-testid="trade-board">
                <div class="rb-metric"><div class="rm-k">本方方向</div><div class="rm-v" data-field="own_side">{{ b.trade?.own_side === 'BUY' ? '买入 BUY' : b.trade?.own_side === 'SELL' ? '卖出 SELL' : b.trade?.own_side || '—' }}</div></div>
                <div class="rb-metric amber"><div class="rm-k">本方收益率边界（%）</div><div class="rm-v mono" data-field="own_limit">{{ b.trade?.own_limit || '—' }}</div></div>
                <div class="rb-metric"><div class="rm-k">距目标价差（BP）</div><div class="rm-v mono" data-field="gap_bp">{{ b.trade?.gap_bp || '—' }}</div></div>
                <div class="rb-metric"><div class="rm-k">对手报价</div><div class="rm-v mono" data-field="peer_quote">{{ counterpartyPrice(b) }}</div></div>
                <div class="rb-metric"><div class="rm-k">对手报量（万）</div><div class="rm-v mono" data-field="peer_volume_wan">{{ counterpartyVolume(b) }}</div></div>
                <div class="rb-metric"><div class="rm-k">报价时间</div><div class="rm-v mono" data-field="quote_at">{{ b.trade?.quote_at || '—' }}</div></div>
                <div class="rb-metric"><div class="rm-k">有效性 / 新鲜度</div><div class="rm-v" data-field="freshness">{{ counterpartyQuote(b) ? quoteValidity(counterpartyQuote(b)!, b.tradeDay) : b.trade?.freshness || '—' }}</div></div>
                <div class="rb-metric"><div class="rm-k">当前阶段</div><div class="rm-v" data-field="phase">{{ b.trade?.phase || '—' }}</div></div>
              </div>
              <div class="room-bar">
                <div v-if="b.used != null" class="rb-track"><div class="rb-fill" :style="{ width: b.used + '%' }" /></div>
                <div class="rb-cap"><span>{{ b.used == null ? '让价空间使用情况未知' : `让价空间已用 ${b.used}%` }}</span><span>底价不入对话</span></div>
              </div>
              <div class="rb-actions">
                <button class="rba primary" :disabled="!b.sessionId" @click="pauseSession(b.sessionId)">人工介入</button>
                <button class="rba" :disabled="!b.sessionId" @click="resumeSession(b.sessionId)">交回机器</button>
                <button class="rba"  :disabled="!b.code" @click="widenFloor(b.code || '')">重新确认底价</button>
                <button class="rba"  :disabled="!b.code" title="停止该券组后续发送；此操作不会向对手发送撤价消息" @click="markBond(b.code || '', '已撤价',b.groupId)">主动撤价</button>
                <button class="rba" data-testid="bond-negotiation-toggle"
                  :disabled="!b.sessionId || b.memberSendAllowed == null || bondControlBusy(b) || (b.memberSendAllowed === false && b.groupState === 'HANDOFF')"
                  :title="b.memberSendAllowed === false ? '沿用本券原会话、报价及谈价进度；不重发旧指令' : '只暂停与当前对手的本券谈价，其他债券和对手不受影响'"
                  @click="toggleBondNegotiation(b)">{{ bondControlBusy(b) ? '正在更新…' : b.memberSendAllowed === false ? '恢复本券' : '放弃本轮' }}</button>
                <button v-if="b.sessionId" class="rba" :disabled="!['已暂停','已放弃'].includes(b.stage)" @click="archiveSession(b.sessionId)">归档此会话</button>
              </div>
              <p class="rb-confirm">放弃/恢复仅作用于当前对手的本券，保留原会话、报价和谈价进度；主动撤价停止该券组发送。接收与分析继续，其他债券不受影响。</p>
            </div>
          </div>
        </div>
      </aside>
    </section>

    <!-- 询价明细弹窗 -->
    <Teleport to="body">
    <div v-if="detailOpen" class="ov" @click="detailOpen = false">
      <div class="mdl pop-in detail-mdl" @click.stop>
        <div class="mdl-hd">
          <div class="dm-hd">
            <div><h2 class="serif">询价明细 · {{ selCp?.name }}</h2><span class="dm-sub">{{ selCp?.org }} · 共 {{ detailRows.length }} 条</span></div>
            <button class="x" @click="detailOpen = false"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg></button>
          </div>
          <p class="dm-tip">已到期债券的中债估值显示「已到期」;点击债券简称可查看报价详情。</p>
        </div>
        <div class="mdl-bd dm-bd">
        <div class="dm-table-wrap">
          <table class="dm-table">
            <thead><tr>
              <th class="dm-sl0">询价日期</th><th class="dm-sl1">债券简称</th>
              <th v-for="c in DETAIL_COLS" :key="c">{{ c }}</th>
              <th class="dm-sr">最终状态</th>
            </tr></thead>
            <tbody>
              <tr v-for="(d, i) in detailRows" :key="i">
                <td class="dm-sl0 mono muted">{{ d.date }}</td>
                <td class="dm-sl1"><span class="dm-name" @click="openBondModal(d.code)">{{ d.name }}</span></td>
                <td class="muted">{{ d.term }}</td>
                <td><span class="dir" :style="{ background: dirBg(d.dir), color: dirColor(d.dir) }">{{ d.dir }}</span></td>
                <td class="mono muted">{{ d.val }}</td>
                <td class="mono b green">{{ d.theirs }}</td>
                <td class="mono muted">{{ d.theirVol }}</td>
                <td class="mono b red">{{ d.ours }}</td>
                <td class="mono muted">{{ d.ourVol }}</td>
                <td class="mono b">{{ d.spread }}</td>
                <td class="muted">{{ d.trader }}</td>
                <td class="muted">{{ d.pm }}</td>
                <td class="dm-sr"><span class="status" :style="{ background: st(d.st)[0], color: st(d.st)[1] }">{{ d.st }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        </div>
      </div>
    </div>
    </Teleport>

    <!-- 导出弹窗 -->
    <Teleport to="body">
    <div v-if="exportOpen" class="ov" @click="exportOpen = false">
      <div class="mdl pop-in export-mdl" @click.stop>
        <div class="mdl-hd">
          <div class="dm-hd"><h2 class="serif">导出聊天记录</h2><button class="x" @click="exportOpen = false"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg></button></div>
          <p class="dm-sub2">对手 <b>{{ selCp?.name }}</b> · {{ selCp?.org }}</p>
        </div>
        <div class="mdl-bd">
        <div class="e-label">导出日期范围</div>
        <div class="e-dates">
          <input v-model="expFrom" type="date" class="e-date mono" @change="expQuick = null" />
          <span class="e-dash">–</span>
          <input v-model="expTo" type="date" class="e-date mono" @change="expQuick = null" />
        </div>
        <div class="e-quick">
          <span v-for="q in EXP_QUICK" :key="q[0]" class="eq" :class="{ on: expQuick === q[1] }" @click="setExpRange(q[1])">{{ q[0] }}</span>
        </div>
        <label><input v-model="exportAllPeers" type="checkbox">导出本人全部对手（未选则只导出当前对手）</label>
        <p class="e-note">导出本人所选日期范围内已入库的消息时间、方向和正文，以 Excel 形式下载；不包含策略、底价等内部配置。</p>
        </div>
        <div class="mdl-ft"><button class="btn-ghost" @click="exportOpen = false">取消</button><button class="btn-primary e-do" @click="doExport">导出</button></div>
      </div>
    </div>
    </Teleport>

    <TraceModal :msg="traceMsg" @close="traceMsg = null" />
    <BondModal :bond="modalBond" @close="modalBond = null" @jump="onJump" />
  </main>

  <!-- 人工发送浮条:照设计稿是 <main> 的兄弟节点,固定在页面底部居中。
       不能放回 .monitor 里——.monitor 带 fade-up 动画留下 transform,
       会给 position:fixed 造包含块,浮条就不再相对视口定位了。 -->
  <div class="manual-dock">
    <button type="button" class="manual-tag" @click="pauseSession()">人工介入</button>
    <input v-model="chatInput" class="send-input" placeholder="人工回复将接管该对手线程所有券；本系统不支持成交" :disabled="!!imCurrent?.catalog_stale" @keydown.enter="sendChat" />
    <button class="btn-primary send-btn" :disabled="sendBusy || unknownPending || !!imCurrent?.catalog_stale" @click="sendChat">{{ sendLabel }}</button>
    <span v-if="sendFail" class="send-fail">{{ sendFail }}</span>
  </div>
</template>

<style scoped>
.analysis-note { font-size:12px; color:#8F6412; line-height:1.6; }
.monitor { height: calc(100vh - 66px); overflow: hidden; padding: 16px 24px 16px; display: flex; flex-direction: column; gap: 16px; box-sizing: border-box; }

/* Hero */
.hero { flex: 0 0 auto; border-radius: 20px; position: relative; overflow: hidden; background: linear-gradient(135deg, #9E3A32 0%, #7C231F 60%, #611A17 100%); box-shadow: 0 20px 50px rgba(124,35,31,.3), 0 0 40px rgba(176, 28, 36,.25); color: #fff; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 24px; padding: 14px 24px; min-height: 120px; box-sizing: border-box; }
.hero-glow { position: absolute; top: -80px; right: -40px; width: 340px; height: 340px; border-radius: 50%; background: radial-gradient(circle, rgba(255,255,255,.14), transparent 65%); filter: blur(10px); }
.hero-l { position: relative; display: flex; flex-direction: column; justify-content: center; gap: 10px; padding-right: 8px; }
.hero-line { display: flex; align-items: center; gap: 10px; white-space: nowrap; }
.hero .qd { padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 800; letter-spacing: 1px; color: #8E1620; background: #FFF6F1; border: 1px solid #fff; }
.hero .mono { font-size: 15px; }
.sep { color: rgba(255,240,235,.6); }
.bot-status { display: inline-flex; align-items: center; gap: 7px; font-weight: 700; font-size: 14.5px; }
.bot-status .dot { width: 9px; height: 9px; border-radius: 50%; }
.bot-status .dot.pulse { animation: dotPulse 2s ease-out infinite; }
.desk { font-weight: 700; font-size: 16px; }
.bot-sw { cursor: pointer; width: 34px; height: 18px; border-radius: 999px; position: relative; transition: all .3s; background: rgba(255,255,255,.3); }
.bot-sw.on { background: #3DA65E; box-shadow: 0 0 14px rgba(61,166,94,.7); }
.bot-sw .knob { position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 50%; background: #fff; transition: left .3s; box-shadow: 0 1px 4px rgba(0,0,0,.3); }
.bot-sw.on .knob { left: 18px; }
.hero-note { font-size: 12px; color: rgba(255,240,235,.65); }
.hero-stats { position: relative; display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; align-content: center; min-width: 0; }
.stat { border-radius: 12px; padding: 10px 12px; min-width: 0; overflow: hidden; background: rgba(255,255,255,.1); border: 1px solid rgba(255,255,255,.2); backdrop-filter: blur(10px); transition: all .25s; }
.stat:hover { background: rgba(255,255,255,.16); box-shadow: 0 0 18px rgba(255,200,180,.25); }
.stat-top { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; white-space: nowrap; }
.sk { font-size: 11.5px; color: rgba(255,240,235,.75); letter-spacing: 1px; overflow: hidden; text-overflow: ellipsis; }
.sv { font-weight: 700; font-size: 22px; text-shadow: 0 0 16px rgba(255,200,180,.35); }
.sd { font-size: 11px; color: rgba(255,240,235,.6); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* 三栏 */
.cols { flex: 1; min-height: 0; display: grid; grid-template-columns: minmax(240px, .7fr) minmax(320px, 1.6fr) minmax(260px, .8fr); gap: 16px; }
.panel { border-radius: 18px; background: rgba(255,255,255,.55); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); border: 1px solid rgba(176, 28, 36,.12); box-shadow: 0 8px 30px rgba(43,35,32,.06); display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.panel-hd { padding: 16px 16px 10px; display: flex; justify-content: space-between; align-items: baseline; }
.panel-hd b { font-size: 15px; letter-spacing: 1px; }
.cnt { font-size: 12px; color: #A89C90; }
.panel-body { flex: 1; min-height: 0; overflow-x: hidden; overflow-y: auto; padding: 4px 12px 14px; display: flex; flex-direction: column; gap: 8px; }

.qd { padding: 2px 7px; border-radius: 6px; font-size: 10.5px; font-weight: 700; background: rgba(46,125,79,.1); color: #1F6B3E; }
.cp-card { cursor: pointer; flex-shrink: 0; border-radius: 13px; padding: 12px 13px; background: rgba(255,255,255,.75); border: 1px solid rgba(176, 28, 36,.12); transition: all .22s; }
.cp-card:hover { transform: translateY(-1px); }
.cp-card.on { background: rgba(176, 28, 36,.06); border-color: rgba(176, 28, 36,.55); box-shadow: 0 0 18px rgba(176, 28, 36,.25); }
.cp-top { display: flex; align-items: center; gap: 8px; }
.cp-top b { font-size: 14px; }
.thread-warn { font-size: 10px; font-weight: 700; color: #8F6412; background: rgba(249,239,217,.9); border: 1px solid rgba(180,140,50,.35); padding: 1px 6px; border-radius: 5px; }
.cp-top .dot { margin-left: auto; width: 9px; height: 9px; border-radius: 50%; }
.cp-bonds { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.cp-bond { display: flex; align-items: center; gap: 8px; flex-shrink: 0; min-height: 28px; font-size: 12.5px; color: #5C5148; }
.cb-name { font-size: 12.5px; line-height: 1.45; white-space: normal; word-break: break-all; }
.stage { padding: 1px 7px; border-radius: 5px; font-size: 10.5px; font-weight: 600; white-space: nowrap; }

/* 聊天 */
.chat-panel { min-width: 0; min-height: 0; overflow: hidden; background: rgba(255,255,255,.5); }
.chat-hd { flex: none; padding: 14px 18px; border-bottom: 1px solid rgba(176, 28, 36,.1); display: flex; align-items: center; gap: 12px; }
.chat-hd-mid { flex: 1; min-width: 0; }
.ch-name { font-weight: 700; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ch-org { font-size: 12px; color: #8A7F76; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hd-btn { cursor: pointer; padding: 7px 14px; border-radius: 9px; font-family: inherit; font-size: 12.5px; color: #5C5148; background: rgba(255,255,255,.7); border: 1px solid rgba(176, 28, 36,.16); transition: all .2s; white-space: nowrap; }
.hd-btn:hover { border-color: rgba(176, 28, 36,.4); }
.hd-btn.primary { font-weight: 600; color: #B01C24; background: rgba(176, 28, 36,.07); border-color: rgba(176, 28, 36,.3); }
.chat-tip { flex: none; padding: 8px 18px 0; font-size: 11.5px; color: #A89C90; }
.chat-flow { flex: 1; min-height: 0; overflow-y: auto; padding: 20px 18px 96px; display: flex; flex-direction: column; gap: 26px; }
.sys-pill { align-self: center; padding: 5px 14px; border-radius: 999px; font-size: 11.5px; color: #8A6B1F; background: rgba(249,239,217,.8); border: 1px solid rgba(180,140,50,.3); transition: opacity .25s; text-align: center; }
.msg { display: flex; flex-direction: column; align-items: flex-start; transition: opacity .25s; }
.msg.right { align-items: flex-end; }
.bubble { cursor: pointer; max-width: 78%; padding: 11px 16px; border-radius: 14px; font-size: 14px; line-height: 1.7; transition: all .2s; }
.bubble.peer { background: rgba(255,255,255,.85); color: #2B2320; border: 1px solid rgba(176, 28, 36,.14); box-shadow: 0 4px 14px rgba(43,35,32,.06); }
.bubble.bot { background: linear-gradient(135deg, #C2333B, #8E1620); color: #fff; box-shadow: 0 0 16px rgba(176, 28, 36,.3); }
.bubble.human { background: linear-gradient(135deg, #C08A2E, #8A6B1F); color: #fff; box-shadow: 0 0 16px rgba(180,140,50,.3); }
.bubble:hover { transform: translateY(-1px); box-shadow: 0 0 18px rgba(176, 28, 36,.35); }
.msg-meta { display: flex; align-items: center; gap: 6px; margin-top: 5px; font-size: 11.5px; flex-wrap: wrap; }
.msg-meta.right { justify-content: flex-end; }
.tag { padding: 1px 7px; border-radius: 5px; font-weight: 600; background: rgba(30,95,180,.1); color: #1E5FB4; }
.mtime { color: #A89C90; }
.ctx { cursor: pointer; white-space: nowrap; padding: 1px 8px; border-radius: 5px; font-size: 11px; font-family: var(--font-ui); background: rgba(255,255,255,.7); color: #8A7F76; border: 1px solid rgba(176, 28, 36,.14); transition: all .2s; }
.ctx:hover { border-color: rgba(176, 28, 36,.5); }
.ctx.active { background: rgba(176, 28, 36,.1); color: #B01C24; border-color: rgba(176, 28, 36,.4); }

.manual-dock {
  position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); z-index: 85;
  width: min(760px, calc(100vw - 48px)); padding: 12px 14px; border-radius: 18px;
  background: rgba(250,248,244,.82);
  backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
  border: 1px solid rgba(176, 28, 36,.18);
  box-shadow: 0 14px 40px rgba(43,35,32,.16), 0 0 24px rgba(176, 28, 36,.15);
  display: flex; gap: 10px; align-items: center;
}
.manual-tag { padding: 9px 14px; border-radius: 10px; font-size: 13px; font-weight: 600; color: #8A6B1F; background: rgba(249,239,217,.9); border: 1px solid rgba(180,140,50,.35); white-space: nowrap; }
.send-input { flex: 1; padding: 11px 15px; font-size: 13.5px; font-family: inherit; border-radius: 12px; border: 1px solid rgba(176, 28, 36,.2); background: rgba(255,255,255,.8); color: #2B2320; transition: all .2s; }
.send-input::placeholder { color: #B0A69B; }
.send-input:focus { outline: none; border-color: rgba(176, 28, 36,.6); box-shadow: 0 0 0 3px rgba(176, 28, 36,.12); }
.send-btn { padding: 11px 26px; border-radius: 12px; font-size: 14px; font-weight: 600; letter-spacing: 3px; white-space: nowrap; }
.send-btn:disabled { opacity: .55; cursor: not-allowed; }
.send-fail { color: #B01C24; font-size: 12.5px; font-weight: 700; white-space: nowrap; }

/* 右:在聊债券 */
.rb-card { flex-shrink: 0; border-radius: 13px; background: rgba(255,255,255,.75); border: 1px solid rgba(176, 28, 36,.12); overflow: hidden; transition: all .25s; }
.rb-card.act { border-color: rgba(176, 28, 36,.5); box-shadow: 0 0 16px rgba(176, 28, 36,.2); }
.rb-hd { cursor: pointer; display: flex; align-items: center; gap: 8px; padding: 12px 13px; }
.rb-name { font-weight: 700; font-size: 13.5px; line-height: 1.45; color: #B01C24; border-bottom: 1px dashed rgba(176, 28, 36,.4); white-space: normal; word-break: break-all; }
.caret { margin-left: auto; transition: transform .25s; }
.rb-body { padding: 0 13px 14px; display: flex; flex-direction: column; gap: 12px; }
.steps { display: flex; justify-content: space-between; position: relative; padding: 0 6px; }
.steps-line { position: absolute; left: 18px; right: 18px; top: 6px; height: 2px; background: rgba(176, 28, 36,.12); }
.step-node { display: flex; flex-direction: column; align-items: center; gap: 5px; position: relative; width: 52px; }
.sdot { width: 14px; height: 14px; border-radius: 50%; border: 2px solid #fff; }
.slabel { font-size: 10.5px; white-space: nowrap; }
.rb-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.rb-metric { border-radius: 10px; padding: 9px 11px; min-width: 0; background: rgba(255,255,255,.9); border: 1px solid rgba(176, 28, 36,.12); }
.rb-metric.amber { background: rgba(249,239,217,.7); border-color: rgba(180,140,50,.3); }
.rm-k { font-size: 10.5px; color: #A89C90; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rb-metric.amber .rm-k { color: #8A6B1F; }
.rm-v[data-field="quote_at"], .rm-v[data-field="freshness"], .rm-v[data-field="phase"] { font-size: 12px; white-space: normal; overflow-wrap: anywhere; }
.rm-v { font-weight: 700; font-size: 18px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rb-metric.amber .rm-v { color: #8A6B1F; }
.rb-track { height: 6px; border-radius: 999px; background: rgba(176, 28, 36,.1); overflow: hidden; }
.rb-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #2E8B57, #C68D58, #C2333B); }
.rb-cap { display: flex; justify-content: space-between; gap: 8px; font-size: 10.5px; color: #A89C90; margin-top: 4px; }
.rb-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.rba { cursor: pointer; padding: 8px; border-radius: 9px; font-family: inherit; font-size: 12px; color: #5C5148; background: rgba(255,255,255,.8); border: 1px solid rgba(176, 28, 36,.16); transition: all .2s; }
.rba.primary { font-weight: 600; color: #B01C24; border-color: rgba(176, 28, 36,.3); }
.rba:hover { background: rgba(176, 28, 36,.06); }
.rb-confirm { padding: 9px; border-radius: 9px; font-family: inherit; font-size: 12px; color: #B0A69B; background: rgba(255,255,255,.5); border: 1px solid rgba(176, 28, 36,.1); cursor: not-allowed; }

/* 弹窗共用 */
/* 遮罩/骨架/滚动区用 styles.css 的 .ov / .mdl / .mdl-hd / .mdl-bd / .mdl-ft */
.x { cursor: pointer; width: 32px; height: 32px; border-radius: 50%; border: 1px solid rgba(176, 28, 36,.25); background: rgba(255,255,255,.7); color: #B01C24; display: grid; place-items: center; }
.status { padding: 3px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600; }

/* 明细弹窗 */
.detail-mdl { max-width: 1600px; }
/* 明细表自己是滚动容器(表头/首末列 sticky),所以这层不再重复出纵向滚动条 */
.dm-bd { display: flex; flex-direction: column; overflow: hidden; }
.dm-bd::after { content: none; }
.dm-hd { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.dm-hd h2 { margin: 0; font-weight: 900; font-size: 22px; color: #B01C24; }
.dm-sub { font-size: 12.5px; color: #8A7F76; margin-left: 12px; }
.dm-tip { margin: 6px 0 0; font-size: 12px; color: #A89C90; }
.dm-table-wrap { margin-bottom: 20px; border-radius: 14px; border: 1px solid rgba(176, 28, 36,.12); background: rgba(255,255,255,.7); overflow: auto; flex: 1; min-height: 0; }
.dm-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12.5px; min-width: 1500px; }
.dm-table th { position: sticky; top: 0; z-index: 2; text-align: left; padding: 10px 12px; font-size: 11.5px; font-weight: 600; color: #B01C24; white-space: nowrap; background: #F6F1EC; border-bottom: 1px solid rgba(176, 28, 36,.16); }
.dm-table td { padding: 10px 12px; border-bottom: 1px solid rgba(176, 28, 36,.07); white-space: nowrap; }
.dm-sl0 { position: sticky; left: 0; z-index: 3; background: #FBF9F6; min-width: 112px; }
th.dm-sl0 { z-index: 4; }
.dm-sl1 { position: sticky; left: 112px; z-index: 3; background: #FBF9F6; border-right: 1px solid rgba(176, 28, 36,.14); box-shadow: 6px 0 12px -6px rgba(43,35,32,.1); }
th.dm-sl1 { z-index: 4; }
.dm-sr { position: sticky; right: 0; z-index: 3; background: #FBF9F6; border-left: 1px solid rgba(176, 28, 36,.14); box-shadow: -6px 0 12px -6px rgba(43,35,32,.1); }
th.dm-sr { z-index: 4; }
.dm-name { cursor: pointer; font-weight: 700; color: #B01C24; border-bottom: 1px dashed rgba(176, 28, 36,.4); }
.dir { padding: 2px 9px; border-radius: 6px; font-size: 11.5px; font-weight: 700; }
.b { font-weight: 700; }
.red { color: #B01C24; }
.green { color: #1F6B3E; }
.muted { color: #6E6258; }
.mono { font-family: var(--font-ui); }

/* 导出弹窗 */
.export-mdl { max-width: 460px; }
.dm-sub2 { margin: 6px 0 0; font-size: 12.5px; color: #8A7F76; }
.dm-sub2 b { color: #2B2320; }
.e-label { margin-top: 18px; font-size: 12px; font-weight: 600; letter-spacing: 1px; color: #8A7F76; }
.e-dates { display: grid; grid-template-columns: 1fr 14px 1fr; gap: 8px; align-items: center; margin-top: 8px; }
.e-date { min-width: 0; padding: 9px 12px; font-size: 13px; border-radius: 10px; border: 1px solid rgba(176, 28, 36,.2); background: rgba(255,255,255,.85); color: #2B2320; }
.e-dash { text-align: center; color: #B0A69B; }
.e-quick { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }
.eq { cursor: pointer; padding: 4px 11px; border-radius: 8px; font-size: 12px; background: rgba(255,255,255,.6); color: #5C5148; border: 1px solid rgba(176, 28, 36,.16); transition: all .18s; }
.eq.on { background: rgba(176, 28, 36,.1); color: #B01C24; border-color: rgba(176, 28, 36,.5); }
.e-note { margin: 14px 0 0; font-size: 11.5px; color: #A89C90; line-height: 1.6; }
.mdl-ft .btn-ghost { padding: 9px 20px; border-radius: 10px; font-size: 13px; }
.e-do { padding: 9px 24px; border-radius: 10px; font-size: 13px; font-weight: 600; letter-spacing: 2px; }
.empty-real { margin: 8px 4px; font-size: 12.5px; color: #8A7F76; }
.history-only { font-size: 12px; color: var(--muted); line-height: 1.6; }
.resume-menu { flex: none; margin: 0 16px 8px; font-size: 12px; color: var(--muted); }
.resume-menu summary { cursor: pointer; }
.resume-menu button { display: block; margin-top: 6px; width: 100%; text-align: left; }
</style>
