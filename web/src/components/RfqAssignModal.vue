<script setup lang="ts">
/** 开始询价：左侧填每只债，右侧勾当前号的好友和组织架构。确认后才发。 */
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { App } from 'ant-design-vue'
import { api, type RfqPolicy, type RfqPreview, type RfqSubmission, type CounterpartyCapacity } from '../api'
import type { BondRow, Strategy } from '../lib/types'
import { useBodyScrollLock } from '../lib/useModal'
import {
  imContacts, imCurrent, imBoundAccount, imIdentityText, imOrg, imOrgStatus, orgEmptyLabel, orgReadNotice, orgTree, refreshImContacts, visibleOrgBranch,
  type VisibleOrgNode,
} from '../lib/imLink'
import { renderQuote, yieldAfterBp, type RfqFields } from '../lib/rfqAssign'
import { isSubmitRejected } from '../lib/http'

const props = defineProps<{ open: boolean; bonds: BondRow[]; pickedCount?: number; floors?: Record<string, string>; savedFloors?: Record<string, {kind:'bp'|'yield';value:number}>; strategy?: Strategy }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'sent', codes: string[]): void; (e: 'pending', codes: string[]): void; (e: 'failed', codes: string[]): void }>()
const { message } = App.useApp()
useBodyScrollLock(() => props.open)

interface Draft {
  code: string
  uins: string[]
  fields: RfqFields
  text: string
  our_side: '' | 'BUY' | 'SELL'
  volume_wan: string
  opening_yield_pct: string
  limit_yield_pct: string
  step_bp: string
  pricing_basis: 'absolute' | 'bp'
  valuation_date: string
}

const drafts = ref<Draft[]>([])
const reading = ref(false)
const active = ref(0)
const query = ref('')
const openIds = ref<string[]>([])
const sending = ref(false)
const previewBusy = ref(false)
const preview = ref<RfqPreview | null>(null)
const submission = ref<RfqSubmission | null>(null)
const confirmationStarted = ref(false)
let receiptTimer:ReturnType<typeof setInterval>|null=null
let receiptBusy=false
function stopReceiptPolling(){if(receiptTimer)clearInterval(receiptTimer);receiptTimer=null}
watch(()=>[props.open,submission.value?.batch_id],()=>{
  stopReceiptPolling()
  if(props.open&&submission.value)receiptTimer=setInterval(()=>void pollSubmission(),1500)
})
onScopeDispose(stopReceiptPolling)
const editingLocked = computed(() => sending.value || confirmationStarted.value)
const formError = ref('')
const previewNotice = ref('')
let generation = 0
let previewAttempt: { fingerprint: string; id: string } | null = null
let confirmRequestId = ''
let directAttempt: { account: string; id: string; items: (RfqPolicy & {peer_ids:string[]})[] } | null = null
function accountKey() { const b = imBoundAccount.value; return JSON.stringify([b?.workbench_id || '', b?.account_uid || '', b?.version ?? 0, b?.context_id || '']) }
function invalidatePreview() {
  generation++
  if (preview.value) previewNotice.value = '参数、收件人或账号已变化，请重新生成预览并核对。'
  preview.value = null; submission.value = null; confirmationStarted.value = false; confirmRequestId = ''; previewAttempt = null; directAttempt = null
}
function releaseRejectedSubmission(error: unknown): boolean {
  if (!isSubmitRejected(error)) return false
  const hint = error instanceof Error ? error.message : typeof error.hint === 'string' ? error.hint : '本次提交已拒绝，未创建发送指令'
  invalidatePreview()
  previewNotice.value = ''
  formError.value = hint
  return true
}
watch(drafts, () => { invalidatePreview(); formError.value = '' }, {deep:true,flush:'sync'})
watch(()=>props.strategy?.strict,invalidatePreview,{flush:'sync'})
watch(accountKey, () => {
  invalidatePreview()
  drafts.value.forEach(d => { d.uins = [] })
  previewNotice.value = '当前账号已变化，请重新选择收件人并生成预览。'
}, {flush:'sync'})


function clean(value: string | undefined): string {
  const text = (value || '').trim()
  if (!text || text === '—' || text === '–' || text === '-' || text === '--') return ''
  return text
}

function brokerOf(raw: string | undefined): string {
  return clean(raw).replace(/^经纪商/, '').trim()
}

function seed(bond: BondRow): Draft {
  const ofr = clean(bond.oo)
  const bid = clean(bond.ob)
  const side = ofr ? 'ofr' : (bid ? 'bid' : '')
  const place = bond.tags?.includes('私募') ? '私募' : (bond.tags?.includes('公募') ? '公募' : '')
  const val = clean(bond.val).split('/')[0]?.trim() || ''
  const fields: RfqFields = {
      price: side === 'bid' ? bid : ofr,
      side,
      tenor: clean(bond.term),
      name: clean(bond.name),
      placement: place,
      bid,
      ofr,
      bid_vol: clean(bond.obv),
      ofr_vol: clean(bond.oov),
      coupon: clean(bond.coupon),
      issuer_rating: clean(bond.issuerRating),
      bond_rating: '',
      cnbd: val,
      balance: '',
      maturity: '',
      broker: brokerOf(side === 'bid' ? bond.obs : bond.oos),
      quoted_at: bond.quotedAt ? new Date(bond.quotedAt).toLocaleString() : '',
  }
  const code = clean(bond.code)
  const floor = props.savedFloors?.[code]
  return { code, uins: [], fields, text: renderQuote(code, fields), our_side:'', volume_wan:'', opening_yield_pct:'',
    limit_yield_pct: floor?.kind === 'yield' && Number.isFinite(floor.value) ? String(floor.value) : '',
    step_bp: props.strategy?.configured && Number.isFinite(props.strategy.step) ? String(props.strategy.step) : '',
    pricing_basis:floor?.kind === 'bp' ? 'bp' : 'absolute', valuation_date:clean(bond.valDate) }
}

watch(() => props.open, (on) => {
  invalidatePreview()
  formError.value = ''; previewNotice.value = ''
  if (!on) return
  active.value = 0
  query.value = ''
  openIds.value = []
  sending.value = false
  drafts.value = props.bonds.map(seed)
  if (bookEmpty.value) readBook()
})

const activeDraft = computed(() => drafts.value[active.value] || null)
const outgoingCount = computed(() => new Set(drafts.value.flatMap(d => d.uins)).size)
const capacity = ref<CounterpartyCapacity | null>(null)
let capacityRequest = 0
watch(() => [props.open, accountKey()], async () => {
  const request = ++capacityRequest
  const account = accountKey()
  capacity.value = null
  if (!props.open || !imBoundAccount.value?.account_uid) return
  try {
    const response = await api.negotiationCapacity()
    if (request !== capacityRequest || account !== accountKey() || !props.open) return
    const data = response.data
    if (response.ok && data?.unit === 'distinct_counterparties'
      && Number.isInteger(data.active) && data.active >= 0 && data.limit === 50) capacity.value = data
  } catch { /* A missing advisory never bypasses the server's atomic admission. */ }
}, { immediate: true })
onScopeDispose(() => { capacityRequest++ })
type DraftField = 'our_side' | 'uins' | 'volume_wan' | 'opening_yield_pct' | 'limit_yield_pct' | 'step_bp' | 'pricing_basis' | 'valuation'
interface DraftIssue { field: DraftField; reason: string }
function numeric(value: unknown): boolean {
  const text = String(value ?? '').trim()
  return /^-?\d{1,8}(?:\.\d{1,8})?$/.test(text) && Number.isFinite(Number(text))
}
function valuationReady(d: Draft): boolean {
  // Public display values are arithmetic references, never trusted authorization evidence.
  return numeric(d.fields.cnbd)
}
function fillSavedBpBoundary(d: Draft) {
  if (editingLocked.value || !d.our_side || d.pricing_basis !== 'bp') return
  const floor = props.savedFloors?.[d.code]
  if (floor?.kind !== 'bp' || !Number.isFinite(floor.value)) return
  if (d.limit_yield_pct.trim()) {
    previewNotice.value = '方向已变化，已填边界保持原值，请核对；BP口径仍由服务端按当前中债估值重算。'
    return
  }
  if (!valuationReady(d)) return
  try {
    d.limit_yield_pct = yieldAfterBp(d.fields.cnbd.trim(), String((d.our_side === 'BUY' ? 1 : -1) * floor.value))
    previewNotice.value = '已按明确方向返显保存BP的边界参考；未改变授权口径，服务端将按当前中债估值重算。'
  } catch { previewNotice.value = '保存BP无法按页面参考估值换算，请填写本批边界并交由服务端按当前中债估值重算。' }
}
function draftIssues(d: Draft): DraftIssue[] {
  const issues: DraftIssue[] = []
  if (d.our_side !== 'BUY' && d.our_side !== 'SELL') issues.push({field:'our_side',reason:'请选择本方买入或卖出'})
  if (!d.uins.length) issues.push({field:'uins',reason:'请选择收件人'})
  for (const [field, name] of [['volume_wan','询价量'],['opening_yield_pct','起始收益率'],['limit_yield_pct','收益率边界'],['step_bp','步幅']] as const) {
    if (!numeric(d[field])) issues.push({field,reason:`${name}须填写有限数字`})
    else if ((field === 'volume_wan' || field === 'step_bp') && Number(d[field]) <= 0) issues.push({field,reason:`${name}必须大于0`})
  }
  if (d.pricing_basis === 'bp') {
    const floor = props.savedFloors?.[d.code]
    if (floor?.kind !== 'bp' || !Number.isFinite(floor.value)) issues.push({field:'pricing_basis',reason:'BP口径需要该券已保存的有效BP参考；也可明确选择绝对收益率填写本批边界'})
    if (!valuationReady(d) && !d.limit_yield_pct.trim()) issues.push({field:'valuation',reason:'页面无可用估值，未能按BP返显；可填写本批边界，服务端将读取中债估值；日期缺失不影响估算'})
  }
  return issues
}
const blockedDrafts = computed(() => drafts.value.map((draft,index) => ({index,code:draft.code,label:draft.fields.name || draft.code,issues:draftIssues(draft)})).filter(row => row.issues.length))
const globalReasons = computed(() => {
  const reasons: string[] = []
  if (!imBoundAccount.value?.account_uid) reasons.push('本人尚未绑定企点账号，不能预览授权')
  if (!drafts.value.length) reasons.push('没有可询价的债券')
  if (outgoingCount.value > 50) reasons.push('同一企点账号最多同时与50个不同对手询价，请减少收件人；同一对手多券只计一人。')
  return reasons
})
function validationError(): string {
  if (outgoingCount.value > 50) return '同一企点账号最多同时与50个不同对手询价，请减少收件人；同一对手多券只计一人。'
  if (!drafts.value.length) return '没有可询价的债券'
  const first = blockedDrafts.value[0]
  return first ? `${first.label}：${first.issues[0]!.reason}` : ''
}
const canPreview = computed(() => !validationError() && !!imBoundAccount.value?.account_uid)
async function locateIssue(index: number, field: DraftField) {
  active.value = index
  await nextTick()
  const bond = document.querySelector<HTMLElement>(`[data-testid="rfq-modal"] .bond[data-index="${index}"]`)
  bond?.scrollIntoView({block:'nearest'})
  const names: Partial<Record<DraftField,string>> = {our_side:'rfq-side',volume_wan:'rfq-volume',opening_yield_pct:'rfq-opening',limit_yield_pct:'rfq-limit',step_bp:'rfq-step',pricing_basis:'rfq-pricing-basis',valuation:'rfq-limit'}
  const target = field === 'uins' ? document.querySelector<HTMLElement>('[data-testid="rfq-book"] input') : bond?.querySelector<HTMLElement>(`[data-testid="${names[field]}"]`)
  target?.focus()
}
watch(() => props.savedFloors, () => {
  if (!props.open || editingLocked.value) return
  for (const d of drafts.value) {
    const floor = props.savedFloors?.[d.code]
    if (!d.limit_yield_pct.trim() && d.pricing_basis === 'absolute' && floor?.kind === 'yield' && Number.isFinite(floor.value)) d.limit_yield_pct = String(floor.value)
  }
}, {deep:true})
function policyItems(): (RfqPolicy & {peer_ids:string[]})[] {
  return drafts.value.map(d => ({code:d.code.trim(),name:d.fields.name.trim(),peer_ids:[...d.uins],our_side:d.our_side as 'BUY'|'SELL',
    volume_wan:d.volume_wan.trim(),opening_yield_pct:d.opening_yield_pct.trim(),limit_yield_pct:d.limit_yield_pct.trim(),step_bp:d.step_bp.trim(),pricing_basis:d.pricing_basis,...(props.strategy?.configured ? {strict_match:props.strategy.strict} : {})}))
}
function prefillFromSaved(d: Draft) {
  if (editingLocked.value) return
  if (!d.our_side) { formError.value = '请先明确本方方向'; return }
  const floor = props.savedFloors?.[d.code]
  const hasValuation = valuationReady(d)
  const strategy = props.strategy?.configured ? props.strategy : null
  const sign = d.our_side === 'BUY' ? 1 : -1
  try {
    // The explicit click fills blanks only, and never changes the chosen pricing basis.
    if (!d.limit_yield_pct.trim() && floor && Number.isFinite(floor.value)) {
      if (floor.kind === 'yield') d.limit_yield_pct = String(floor.value)
      else if (hasValuation) d.limit_yield_pct = yieldAfterBp(d.fields.cnbd.trim(),String(sign*floor.value))
    }
    if (!d.opening_yield_pct.trim() && strategy && hasValuation && Number.isFinite(strategy.startDev)) {
      d.opening_yield_pct = yieldAfterBp(d.fields.cnbd.trim(),String(sign*strategy.startDev))
    }
    if (!d.step_bp.trim() && strategy && Number.isFinite(strategy.step)) d.step_bp = String(strategy.step)
    previewNotice.value = d.pricing_basis === 'bp'
      ? 'BP口径保持不变；仅补空值，参考估值与估值日须由服务端按可信数据重新核验。'
      : '绝对收益率口径保持不变；仅补空值，已编辑价格不覆盖，本批仍须本人核对确认。'
    if (!hasValuation && (floor?.kind === 'bp' || strategy)) previewNotice.value += ' 页面参考估值缺失，相关价格未预填；可填写本批边界交由服务端核验。'
    if (!strategy) previewNotice.value += ' 尚无已保存策略，起始收益率和步幅请本批明确填写。'
  } catch { formError.value = '参考估值或BP无法换算，未改变授权口径，请核对数据后填写本批价格' }
}
async function copySource(d: Draft) {
  try { await navigator.clipboard.writeText(d.text); message.success('行情原文已复制，请在人工发送前核对') }
  catch { message.error('复制失败，请手工复制行情原文') }
}
const needle = computed(() => query.value.trim())
const friends = computed(() => {
  const n = needle.value
  return imContacts.value.filter(p => p.uin && (!n || p.name.includes(n) || p.uin.includes(n)))
})
const bookEmpty = computed(() => imContacts.value.length === 0 && imOrg.value.departments.length === 0 && imOrg.value.members.length === 0)

interface FlatRow {
  kind: 'dept' | 'person'
  id: string
  depth: number
  name: string
  uin?: string
  opened?: boolean
  childCount?: number
  memberCount?: number
}

function walk(nodes: VisibleOrgNode[], depth: number): FlatRow[] {
  const rows: FlatRow[] = []
  for (const node of nodes) {
    rows.push({
      kind: 'dept',
      id: node.id,
      depth,
      name: node.name,
      opened: node.opened,
      childCount: node.childCount,
      memberCount: node.memberCount,
    })
    if (!node.opened) continue
    for (const member of node.members) {
      rows.push({
        kind: 'person',
        id: node.id + ':' + member.uin + ':' + member.order,
        depth: depth + 1,
        name: member.name,
        uin: member.uin,
      })
    }
    rows.push(...walk(node.children, depth + 1))
  }
  return rows
}

const orgRows = computed(() => walk(visibleOrgBranch(orgTree.value, openIds.value), 0))
const orgHits = computed(() => {
  const n = needle.value
  if (!n) return []
  const depts = new Map(imOrg.value.departments.map(d => [d.id, d.name]))
  const seen = new Set<string>()
  const found: { uin: string; name: string; dept: string }[] = []
  for (const member of imOrg.value.members) {
    if (!member.uin || seen.has(member.uin)) continue
    const account = member.account || ''
    if (!member.name.includes(n) && !member.uin.includes(n) && !account.includes(n)) continue
    seen.add(member.uin)
    found.push({ uin: member.uin, name: member.name || member.uin, dept: depts.get(member.department_id) || '' })
  }
  return found
})

function personName(uin: string): string {
  const friend = imContacts.value.find(p => p.uin === uin)
  if (friend?.name) return friend.name
  const member = imOrg.value.members.find(m => m.uin === uin)
  return member?.name || uin
}

function checked(uin: string): boolean {
  return !!activeDraft.value?.uins.includes(uin)
}

async function readBook() {
  if (reading.value) return
  reading.value = true
  try {
    const got = await refreshImContacts()
    if (got.org_state === 'error') message.warning(orgReadNotice(imOrg.value, got))
    else if (!got.org.departments.length && !got.org.members.length && !got.contacts.length) {
      message.warning(orgEmptyLabel(got.org, got) || '当前账号暂无可读联系人')
    }
  } catch (err) {
    message.error(err instanceof Error ? err.message : '读通讯录失败')
  } finally {
    reading.value = false
  }
}

function assignSamePerson() {
  if (editingLocked.value) return
  const chosen = activeDraft.value?.uins || []
  if (!chosen.length) {
    message.warning('先在右侧勾一个联系人')
    return
  }
  const copy = [...chosen]
  drafts.value.forEach(draft => { draft.uins = [...copy] })
}

function togglePerson(uin: string) {
  if (editingLocked.value) return
  const draft = drafts.value[active.value]
  if (!draft || !uin) return
  const at = draft.uins.indexOf(uin)
  if (at >= 0) draft.uins.splice(at, 1)
  else draft.uins.push(uin)
}

function toggleDept(id: string) {
  openIds.value = openIds.value.includes(id)
    ? openIds.value.filter(item => item !== id)
    : [...openIds.value, id]
}

async function requestPreview() {
  if (previewBusy.value || sending.value || preview.value || confirmationStarted.value) return
  formError.value = validationError()
  if (formError.value) return
  if (!imBoundAccount.value?.account_uid) { formError.value = '本人尚未绑定企点账号，不能预览授权'; return }
  const items = policyItems()
  const fingerprint = JSON.stringify({account:accountKey(),items})
  if (previewAttempt?.fingerprint !== fingerprint) previewAttempt = {fingerprint,id:crypto.randomUUID()}
  const requestId = previewAttempt.id
  const request = generation
  previewBusy.value = true
  preview.value = null; submission.value = null; previewNotice.value = ''
  try {
    const r = await api.imRfqPreview(items, requestId)
    if (request !== generation || !props.open) return
    if (!r.ok) { formError.value = r.hint || '预览失败，没有授权发送'; return }
    const data = r.data
    if (!data?.batch_id || !Number.isInteger(data.version) || !['DRAFT','PREVIEW'].includes(data.status.toUpperCase()) || !Array.isArray(data.commands_preview) || !data.commands_preview.length) {
      formError.value = '服务端未返回可确认的草稿预览'; return
    }
    if (data.commands_preview.some(c => !c.peer_id || !c.text || [...c.text].length > 1500)) {
      formError.value = '服务端逐收件人首句为空或超过1500字，不能确认'; return
    }
    preview.value = data
    confirmRequestId = crypto.randomUUID()
  } catch (e) {
    if (request === generation) formError.value = e instanceof Error ? e.message : '预览请求失败'
  } finally { previewBusy.value = false }
}
async function sendDirect() {
  if (sending.value || previewBusy.value || preview.value) return
  if (submission.value) { await pollSubmission(); return }
  if (!directAttempt) {
    formError.value = validationError()
    if (formError.value) return
  }
  if (!imBoundAccount.value?.account_uid) { formError.value = '请先绑定企点账号'; return }
  const account = accountKey(), request = generation
  if (!directAttempt) directAttempt = { account, id: crypto.randomUUID(), items: policyItems() }
  if (directAttempt.account !== account) { formError.value = '当前账号已变化，请重新核对收件人'; return }
  const attempt = directAttempt
  const codes = [...new Set(attempt.items.map(item => item.code))]
  confirmationStarted.value = true
  sending.value = true; formError.value = ''; previewNotice.value = ''
  try {
    const response = await api.imRfqSendDirect(attempt.items, attempt.id)
    if (request !== generation || !props.open || account !== accountKey()) return
    if (!response.ok) {
      if (!releaseRejectedSubmission(response)) formError.value = `${response.hint || '发送请求未被接受'}；提交结果未确认，请沿用本次请求重试`
      return
    }
    const data = response.data
    if (!data?.batch_id || !Array.isArray(data.commands) || !data.commands.length
        || data.commands.some(command => !command.command_id || !command.peer_id)) {
      formError.value = '尚未取得本次提交结果，请沿用本次请求重试核对'; return
    }
    submission.value = data
    if (data.commands.every(command => command.status === 'sent')) {
      message.success('服务端确认本批已发送'); emit('sent', codes)
    } else if (data.commands.every(command => ['failed','cancelled','expired'].includes(command.status))) emit('failed', codes)
    else emit('pending', codes)
  } catch (error) {
    if (request === generation && props.open && account === accountKey() && !releaseRejectedSubmission(error))
      formError.value = `${error instanceof Error ? error.message : '发送请求失败'}；提交结果未确认，请沿用本次请求重试，不会重复创建指令`
  } finally { sending.value = false }
}
async function confirm() {
  if (sending.value || !preview.value || !confirmRequestId) return
  const batch = preview.value
  const request = generation
  const requestId = confirmRequestId
  const codes = [...new Set(drafts.value.map(d => d.code))]
  confirmationStarted.value = true
  sending.value = true; formError.value = ''
  try {
    const r = await api.imRfqSend(batch.batch_id, batch.version, requestId)
    if (request !== generation || !props.open) return
    if (!r.ok) {
      if (!releaseRejectedSubmission(r)) formError.value = `${r.hint || '确认失败，未证明已发送'}；提交结果未确认，请沿用本次确认重试`
      return
    }
    if (r.data?.batch_id !== batch.batch_id || !Array.isArray(r.data.commands) || !r.data.commands.length) {
      formError.value = '未得到本批确认结果，请用同一请求重试核对'; return
    }
    submission.value = r.data
    if (r.data.commands.every(c => c.status === 'sent')) { message.success('服务端确认本批已发送'); emit('sent',codes) }
    else if (r.data.commands.every(c => c.status === 'failed' || c.status === 'cancelled')) emit('failed',codes)
    else emit('pending',codes)
  } catch (e) {
    if (request === generation && props.open && !releaseRejectedSubmission(e)) formError.value = `${e instanceof Error ? e.message : '确认请求失败'}；提交结果未确认，请沿用本次确认重试`
  } finally { sending.value = false }
}
async function pollSubmission(){
  const batch=submission.value
  if(!batch||receiptBusy)return
  receiptBusy=true
  const request=generation,account=accountKey()
  try {
    for(const command of batch.commands){
      if(['sent','failed','cancelled','expired'].includes(command.status))continue
      const receipt=await api.imReceipt(command.command_id)
      if(request!==generation||account!==accountKey()||submission.value?.batch_id!==batch.batch_id)return
      if(!receipt.ok||receipt.data.cmd_id!==command.command_id)continue
      const status=receipt.data.status==='delivered'?'sent':receipt.data.status
      if(['queued','pending','unknown','sent','failed','cancelled','expired'].includes(status)){
        command.status=status as typeof command.status
        command.reason=receipt.data.reason
      }
    }
    const codes=[...new Set(drafts.value.map(d=>d.code))]
    if(batch.commands.every(c=>c.status==='sent')){stopReceiptPolling();emit('sent',codes)}
    else if(batch.commands.every(c=>['sent','failed','cancelled','expired'].includes(c.status))){stopReceiptPolling();emit('failed',codes)}
  } catch(error){formError.value=error instanceof Error?error.message:'回执暂不可读，保留未决状态'}
  finally {receiptBusy=false}
}
function commandStatus(status: string): string {
  return ({queued:'已入队，尚未发送',pending:'已提交，等待回执',unknown:'结果未知，等待对账',sent:'服务端确认已发送',failed:'发送失败',cancelled:'已取消',expired:'已过期'} as Record<string,string>)[status] || '状态未知'
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="ov" @click="!sending && emit('close')">
      <div class="mdl pop-in rfq-mdl" data-testid="rfq-modal" @click.stop>
        <div class="mdl-hd">
          <div class="head">
            <h2 class="serif">开始询价</h2>
            <button class="x" type="button" :disabled="sending" @click="emit('close')">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>
            </button>
          </div>
          <p class="sub">明确本方方向、询价量、起始收益率、边界、步幅和收件人后，可直接发送，也可先预览再确认。直接发送即提交本批询价；行情原文仅供复制或人工发送。</p>
          <p class="sub" data-testid="rfq-counterparty-capacity">
            每个企点账号最多50个活跃对手；本批已选{{ outgoingCount }}人。
            <span v-if="capacity">当前活跃{{ capacity.active }}/{{ capacity.limit }}人。</span>
            同一对手多券只计一人，暂停仍占名额；名额在预览和确认时由服务端重新核对。
          </p>
          <p v-if="imBoundAccount && !imCurrent" class="sub">本人账号已绑定，可以生成零外发预览；实际投递仍须通过服务端通道健康检查。</p>
          <p v-if="(pickedCount || 0) > bonds.length" class="warn">已选数量与服务端解析结果不一致，请关闭弹窗并重新核对选择。</p>
        </div>

        <section v-if="!preview && !confirmationStarted && (globalReasons.length || blockedDrafts.length)" class="validation-summary" data-testid="rfq-validation-summary" role="status" aria-live="polite">
          <b>预览前待完善：{{ blockedDrafts.length }} 只券；每券收件人数在券卡中单独显示。</b>
          <p v-for="reason in globalReasons" :key="reason" class="warn">{{ reason }}</p>
          <div v-for="row in blockedDrafts" :key="row.code + ':' + row.index" data-testid="rfq-bond-validation" :data-code="row.code" :data-index="row.index" class="validation-row">
            <strong>{{ row.label }} · {{ row.code }}</strong>
            <button v-for="(issue,index) in row.issues" :key="issue.field + ':' + index" class="validation-issue" type="button" data-testid="rfq-validation-issue" :data-field="issue.field" @click="locateIssue(row.index,issue.field)">{{ issue.reason }}</button>
          </div>
        </section>

        <div class="mdl-bd rfq-bd">
          <div v-if="!drafts.length" class="empty">服务端解析的选择里没有可询价的债券。</div>
          <div v-else class="rfq-grid">
            <section class="pane">
              <button class="btn-ghost assign" type="button" data-testid="rfq-assign-all" :disabled="editingLocked" @click="assignSamePerson">一键分配同一个人</button>
              <div
                v-for="(draft, index) in drafts"
                :key="draft.code + ':' + index"
                class="bond"
                :data-index="index"
                :data-code="draft.code"
                :class="{ on: index === active }"
                @click="active = index"
              >
                <div class="bond-hd">
                  <b>{{ draft.fields.name || '未填简称' }}</b>
                  <em :class="{ missing: !(floors || {})[draft.code] }">{{ (floors || {})[draft.code] ? '已保存参考：' + (floors || {})[draft.code] : '未保存底价参考，可填写本批边界' }}</em>
                  <em>{{ draft.uins.length }} 人</em>
                  <span v-if="draftIssues(draft).length" class="bond-issue-count">待补 {{ draftIssues(draft).length }} 项</span>
                </div>
                <div v-if="index === active" class="bond-bd" @click.stop>
                  <label class="fld">本方方向（必选）
                    <select v-model="draft.our_side" class="ny-input" data-testid="rfq-side" :disabled="editingLocked" @change="fillSavedBpBoundary(draft)"><option value="">请选择本方方向</option><option value="BUY">买入 BUY</option><option value="SELL">卖出 SELL</option></select>
                  </label>
                  <label class="fld">券组目标总量（万，必填）<input v-model="draft.volume_wan" class="ny-input" inputmode="decimal" data-testid="rfq-volume" :disabled="editingLocked" /></label>
                  <label class="fld">授权价格口径
                    <select v-model="draft.pricing_basis" class="ny-input" data-testid="rfq-pricing-basis" :disabled="editingLocked">
                      <option value="absolute">绝对收益率（本人确认）</option>
                      <option value="bp">保存 BP（按中债估值估算）</option>
                    </select>
                  </label>
                  <label class="fld">起始收益率（%）<input v-model="draft.opening_yield_pct" class="ny-input" inputmode="decimal" data-testid="rfq-opening" :disabled="editingLocked" /></label>
                  <label class="fld">收益率边界（%）<input v-model="draft.limit_yield_pct" class="ny-input" inputmode="decimal" data-testid="rfq-limit" :disabled="editingLocked" /></label>
                  <label class="fld">每轮步幅（BP）<input v-model="draft.step_bp" class="ny-input" inputmode="decimal" data-testid="rfq-step" :disabled="editingLocked" /></label>
                  <button class="btn-ghost" type="button" data-testid="rfq-prefill" :disabled="editingLocked || !draft.our_side" @click="prefillFromSaved(draft)">用已保存底价/策略补充空值</button>
                  <p v-if="draft.pricing_basis === 'bp'" class="source-note" data-testid="rfq-valuation">预填参考估值：{{ draft.fields.cnbd || '未提供' }}% · 估值日：{{ draft.valuation_date || '未提供' }}；日期仅作提示，未提供也可按中债估值估算。</p>
                  <p class="source-note">起始收益率按本批输入发送，不再与谈价边界比较。BP 模式使用服务端中债估值估算；没有估值数值时会说明原因，估值日期不作为前置条件。</p>
                  <label class="fld source-note">行情原文（仅复制或人工发送，不进入自动首句）
                    <textarea v-model="draft.text" class="ny-input msg" rows="4" data-testid="rfq-text" :disabled="editingLocked" />
                  </label>
                  <button class="btn-ghost" type="button" @click="copySource(draft)">复制行情原文</button>

                </div>
              </div>
            </section>

            <section class="pane book" data-testid="rfq-book">
              <div class="book-hd">
                <b>当前号通讯录</b>
                <span class="mono muted">{{ imIdentityText(imCurrent) || imBoundAccount?.account_uid || '本人尚未绑定企点号' }}</span>
              </div>
              <input v-model="query" class="ny-input" placeholder="按姓名或号码筛选" />
              <p v-if="bookEmpty" class="muted">这个号的通讯录还没读到。</p>
              <button v-if="bookEmpty" class="btn-ghost read-book" type="button" data-testid="rfq-read-book" :disabled="reading" @click="readBook">{{ reading ? '正在读取…' : '读取通讯录' }}</button>
              <template v-else>
                <div class="book-k">私人好友 {{ friends.length }}</div>
                <button
                  v-for="person in friends"
                  :key="'f:' + person.uin"
                  type="button"
                  class="person"
                  :class="{ on: checked(person.uin) }"
                  :data-uin="person.uin"
                  :disabled="editingLocked"
                  @click="togglePerson(person.uin)"
                >{{ person.name || person.uin }} · <em class="mono">{{ person.uin }}</em></button>
                <div class="book-k">组织架构</div>
                <p v-if="imOrgStatus.org_state === 'error' && orgRows.length" class="warn">{{ orgReadNotice(imOrg, imOrgStatus) }}</p>
                <template v-if="needle">
                  <p v-if="!orgHits.length" class="muted">没有匹配的人</p>
                  <button
                    v-for="member in orgHits"
                    :key="'h:' + member.uin"
                    type="button"
                    class="person"
                    :class="{ on: checked(member.uin) }"
                    :data-uin="member.uin"
                    :disabled="editingLocked"
                    @click="togglePerson(member.uin)"
                  >{{ member.name }} · <em class="mono">{{ member.uin }}</em><span v-if="member.dept" class="dept">{{ member.dept }}</span></button>
                </template>
                <template v-else>
                  <p v-if="!orgRows.length" class="muted">{{ orgEmptyLabel(imOrg, imOrgStatus) }}</p>
                  <template v-for="row in orgRows" :key="row.kind + ':' + row.id">
                    <button
                      v-if="row.kind === 'dept'"
                      type="button"
                      class="dept-btn"
                      :style="{ marginLeft: (row.depth * 14) + 'px' }"
                      @click="toggleDept(row.id)"
                    >
                      <span>{{ row.opened ? '▾' : '▸' }}</span>
                      {{ row.name }}
                      <em v-if="row.memberCount">{{ row.memberCount }} 人</em>
                    </button>
                    <button
                      v-else
                      type="button"
                      class="person"
                      :class="{ on: checked(row.uin || '') }"
                      :data-uin="row.uin"
                      :disabled="editingLocked"
                      :style="{ marginLeft: (row.depth * 14) + 'px' }"
                      @click="togglePerson(row.uin || '')"
                    >{{ row.name }} · <em class="mono">{{ row.uin }}</em></button>
                  </template>
                </template>
              </template>
            </section>
          </div>
        </div>

        <div v-if="formError || previewNotice" class="review-note" role="status">
          <p v-if="formError" class="warn" role="alert">{{ formError }}</p><p v-if="previewNotice">{{ previewNotice }}</p>
        </div>
        <section v-if="preview" class="server-preview" data-testid="rfq-server-preview">
          <h3>第二步：核对服务端最终首句</h3>
          <p v-if="confirmationStarted">确认已提交，本批参数冻结；刷新结果沿用原请求。关闭弹窗不会取消已经授权的发送。</p>
          <p>以下正文按本方授权生成，未使用上方行情原文。确认将授权机器人按当前边界询价，不能成交。</p>
          <div v-for="item in preview.items" :key="item.peer_id + ':' + item.code" class="policy-summary">
            {{ item.name }} {{ item.code }} · {{ item.our_side === 'BUY' ? '本方买入' : '本方卖出' }} {{ item.volume_wan }} 万 · 起始 {{ item.opening_yield_pct }}% · 边界 {{ item.limit_yield_pct }}% · 步幅 {{ item.step_bp }} BP
            <span v-if="item.pricing_basis === 'bp' && item.valuation_snapshot" data-testid="rfq-valuation-snapshot"> · {{ item.valuation_snapshot.source === 'innodealing' ? 'Innodealing 中债估值' : '中债资料源估值' }} {{ item.valuation_snapshot.yield_pct }}%（估算参考；估值日：{{ item.valuation_snapshot.valuation_date || '未提供' }}）</span>
            <span v-else> · 绝对收益率由本人确认</span>
            <span v-for="warning in item.valuation_warnings || []" :key="warning" class="valuation-advisory" data-testid="rfq-valuation-advisory">提示：{{ warning }}</span>
          </div>
          <div v-for="command in preview.commands_preview" :key="command.peer_id" class="final-command">
            <b>收件人：{{ personName(command.peer_id) }} · {{ command.peer_id }}</b><pre>{{ command.text }}</pre>
          </div>
        </section>
        <section v-if="submission" class="server-preview" data-testid="rfq-submission" role="status">
          <h3>本批服务端状态</h3>
          <p v-for="command in submission.commands" :key="command.command_id">{{ personName(command.peer_id) }}：{{ commandStatus(command.status) }}</p>
          <p>排队、提交和结果未知均不代表已发送；可刷新本批确认结果，不会新建发送请求。</p>
        </section>

        <div class="mdl-ft between">
          <span class="muted">已选择 {{ outgoingCount }} 位收件人；同一人的首句由服务端合并</span>
          <span class="acts">
            <button class="btn-ghost cancel" type="button" :disabled="sending" @click="emit('close')">{{ confirmationStarted ? '关闭' : '取消' }}</button>
            <button class="btn-ghost send" type="button" data-testid="rfq-preview" :disabled="sending || previewBusy || confirmationStarted || !!preview || !canPreview" @click="requestPreview">{{ previewBusy ? '正在生成预览…' : '生成服务端预览' }}</button>
            <button v-if="!preview" class="btn-primary send" type="button" data-testid="rfq-direct-send"
              :disabled="sending || previewBusy || (!confirmationStarted && !canPreview)" @click="sendDirect">{{ sending ? '正在提交…' : submission ? '刷新本批发送结果' : confirmationStarted ? '重试核对本次发送' : '直接发送' }}</button>
            <button v-show="!!preview" class="btn-primary send" type="button" data-testid="rfq-confirm" :disabled="sending || !preview" @click="confirm">{{ sending ? '正在确认…' : submission ? '刷新本批确认结果' : '核对无误，确认授权发送' }}</button>
          </span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.rfq-mdl { width: min(1120px, 100%); max-width: 1120px; }
.head { display: flex; align-items: center; justify-content: space-between; }
.head h2 { margin: 0; font-weight: 900; font-size: 20px; color: #B01C24; }
.x { border: 0; background: transparent; cursor: pointer; color: #6E6258; }
.sub, .warn { margin: 8px 0 0; font-size: 12.5px; line-height: 1.55; color: #6E6258; }
.warn { color: #8E1620; }
.rfq-bd { overflow: auto; }
.validation-summary { flex:none;max-height:18vh;overflow:auto;padding:10px 24px;border-top:1px solid #ead9d3;border-bottom:1px solid #ead9d3;background:#fff8f2;font-size:12px;line-height:1.6; }
.validation-row { display:flex;flex-wrap:wrap;align-items:center;gap:5px 8px;margin-top:6px; }.validation-row strong { width:100%;overflow-wrap:anywhere; }.validation-issue { border:1px solid #e0c5bc;border-radius:6px;padding:2px 7px;background:#fff;color:#8e1620;font:inherit;text-align:left;cursor:pointer; }.validation-issue:focus { outline:2px solid #b01c24;outline-offset:1px; }.bond-issue-count { color:#8e1620;font-size:11px;white-space:nowrap; }
.review-note, .server-preview { padding: 12px 24px; font-size: 12px; }
.server-preview { max-height: 30vh; overflow: auto; border-top: 1px solid rgba(176,28,36,.16); }
.server-preview h3 { margin: 0 0 6px; }
.final-command pre { white-space: pre-wrap; overflow-wrap: anywhere; background: rgba(255,255,255,.8); padding: 10px; border-radius: 8px; }
.policy-summary { margin: 6px 0; }
.valuation-advisory { display:block;color:#7c654a;line-height:1.6; }
.source-note { grid-column: 1 / -1; font-size: 12px; color: #6E6258; margin: 2px 0; }
.msg { width: 100%; box-sizing: border-box; }
.rfq-bd::after { display: none; }
.rfq-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(260px, .85fr); gap: 14px; height: min(68vh, 720px); min-height: 0; }
.pane { min-height: 0; overflow: auto; padding-right: 4px; }
.empty { padding: 12px 0 20px; color: #6E6258; }
.bond { border: 1px solid rgba(176, 28, 36, .16); border-radius: 14px; padding: 12px; margin-bottom: 10px; background: rgba(255, 255, 255, .62); cursor: pointer; }
.bond.on { border-color: #B01C24; box-shadow: 0 0 0 1px #B01C24; }
.bond-hd { display: flex; align-items: baseline; gap: 8px; }
.bond-hd em { margin-left: auto; font-style: normal; color: #8A7F76; font-size: 12px; }
.bond-bd { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; margin-top: 10px; cursor: default; }
.fld { display: flex; flex-direction: column; gap: 3px; font-size: 11.5px; color: #6E6258; }
.picked { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 6px; }
.chip, .person, .dept-btn { font-family: inherit; cursor: pointer; }
.chip { border: 1px solid rgba(176, 28, 36, .2); background: rgba(176, 28, 36, .08); color: #8E1620; border-radius: 999px; padding: 2px 8px; font-size: 12px; }
.preview { grid-column: 1 / -1; margin: 0; padding: 8px 10px; border-radius: 8px; background: rgba(255, 255, 255, .9); border: 1px solid rgba(176, 28, 36, .12); white-space: pre-wrap; font-size: 12px; line-height: 1.45; }
.book-hd { display: flex; flex-direction: column; gap: 2px; margin-bottom: 8px; }
.book-k { margin: 10px 0 6px; font-size: 12px; font-weight: 700; color: #B01C24; }
.person, .dept-btn { display: flex; align-items: center; gap: 6px; width: calc(100% - 8px); text-align: left; margin: 0 0 4px; border-radius: 8px; border: 1px solid rgba(176, 28, 36, .12); background: rgba(255, 255, 255, .8); padding: 4px 8px; font-size: 12px; color: inherit; }
.person.on { color: #fff; background: #B01C24; border-color: transparent; }
.person em, .dept-btn em { font-style: normal; color: #8A7F76; }
.person.on em { color: rgba(255, 255, 255, .82); }
.dept-btn { font-weight: 700; color: #B01C24; background: rgba(176, 28, 36, .08); }
.dept { margin-left: auto; color: #8A7F76; }
.assign { margin: 0 0 10px; padding: 8px 14px; border-radius: 10px; font-size: 13px; font-weight: 600; }
.cancel { padding: 9px 18px; border-radius: 10px; font-size: 13px; }
.send { padding: 9px 24px; border-radius: 10px; font-size: 13px; font-weight: 600; letter-spacing: 2px; }
.send:disabled { opacity: .45; cursor: not-allowed; transform: none; box-shadow: none; }
.acts { display: flex; gap: 10px; align-items: center; }
.muted { color: #8A7F76; font-size: 12px; }
@media (max-width: 900px) {
  .rfq-grid { grid-template-columns: 1fr; height: auto; }
  .pane { max-height: 48vh; }
}
</style>
