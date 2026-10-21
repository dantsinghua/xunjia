<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, type CollectorCatalog, type CollectorContacts, type CollectorSelection } from '../api'
import { applyCollectorSelection, buildOrgTree, imBoundAccount, imTarget, orgReadNotice, selectImTarget } from '../lib/imLink'
import OrgTree from './OrgTree.vue'

const props = withDefaults(defineProps<{ autoLoad?: boolean }>(), { autoLoad: true })
const emit = defineEmits<{ saved: []; availability: [boolean]; institution: [boolean] }>()
const initial = imBoundAccount.value
const saved = ref<CollectorSelection | null>(initial ? {
  workbench_id: initial.workbench_id, account_uid: initial.account_uid, version: initial.version || 0,
} : null)
const catalog = ref<CollectorCatalog | null>(null)
const workbenchId = ref(''), accountUid = ref(''), savedNickname = ref(initial?.account_nick || '')
const loading = ref(false), busy = ref(false), error = ref(''), notice = ref('')
const preview = ref<CollectorContacts | null>(null)
let catalogRequest = 0, actionRequest = 0, disposed = false, catalogInFlight = false
let timer: ReturnType<typeof setInterval> | undefined
const institutional = ref(false), now = ref(Date.now() / 1000), syncPending = ref(false)
let syncDeadline = 0, syncBaseline = { friends: 0, org: 0 }, syncReading = false
function expired(value?: number | null) { return institutional.value && (value == null || now.value - value >= 60 || value > now.value + 60) }
const workbench = computed(() => catalog.value?.workbenches.find(w => w.workbench_id === workbenchId.value))
const account = computed(() => workbench.value?.accounts.find(a => a.account_uid === accountUid.value))
const directorySaved = computed(() => {
  const wb = catalog.value?.workbenches.find(w => w.workbench_id === saved.value?.workbench_id)
  return { wb, account: wb?.accounts.find(a => a.account_uid === saved.value?.account_uid) }
})
const savedAvailable = computed(() => !!directorySaved.value.account)
const savedStale = computed(() => !!directorySaved.value.wb?.stale || !!directorySaved.value.account?.stale || directorySaved.value.account?.available === false || expired(directorySaved.value.account?.updated_at || directorySaved.value.wb?.updated_at))
const draftStale = computed(() => !!workbench.value?.stale || !!account.value?.stale || account.value?.available === false || expired(account.value?.updated_at || workbench.value?.updated_at))
watch([savedAvailable, savedStale, institutional], () => { emit('availability', savedAvailable.value && !savedStale.value); emit('institution', institutional.value) })
const orgTree = computed(() => buildOrgTree(preview.value?.org))
function samePair(a: { workbench_id: string; account_uid: string } | null | undefined, b: typeof a): boolean {
  return !!a && !!b && a.workbench_id === b.workbench_id && a.account_uid === b.account_uid
}
function when(value?: number | null) { return value ? new Date(value * 1000).toLocaleString() : '未提供更新时间' }
function changedWorkbench() { accountUid.value = ''; changedAccount() }
function changedAccount() { actionRequest++; syncPending.value = false; preview.value = null; notice.value = ''; error.value = '' }
async function load(refresh = false, silent = false) {
  if (busy.value || loading.value || catalogInFlight) return
  catalogInFlight = true
  const request = ++catalogRequest
  loading.value = !silent
  if (!silent) { error.value = ''; notice.value = '' }
  try {
    const response = await (refresh ? api.refreshImCatalog() : api.imCatalog())
    if (!response.ok || !response.data || !Array.isArray(response.data.workbenches)) throw Error(response.hint || '采集端目录读取失败')
    if (disposed || request !== catalogRequest) return
    catalog.value = response.data
    if (response.data.contract === 'its-relay-path-v1') institutional.value = true
    const previous = saved.value
    saved.value = response.data.selected
    if (saved.value) {
      const entry = response.data.workbenches.find(w => w.workbench_id === saved.value?.workbench_id)
      const item = entry?.accounts.find(a => a.account_uid === saved.value?.account_uid)
      if (item) savedNickname.value = item.account_nick || ''
      else if (!samePair(previous, saved.value)) savedNickname.value = ''
      if (!samePair(saved.value, imBoundAccount.value)) {
        applyCollectorSelection(saved.value, { contacts: [], source: '', org: { departments: [], members: [] } })
      }
      if (!silent || !samePair(previous, saved.value)) {
        workbenchId.value = entry ? saved.value.workbench_id : ''
        accountUid.value = item ? saved.value.account_uid : ''
      }
    } else {
      workbenchId.value = ''; accountUid.value = ''; savedNickname.value = ''
    }
    if (!silent || !samePair(previous, saved.value)) preview.value = null
    if (response.data.refresh_error) error.value = response.data.refresh_error
    if (refresh) notice.value = '采集端目录已重新读取，请核对工作台、企点号及更新时间。'
  } catch (e) {
    if (!disposed && request === catalogRequest) {
      error.value = e instanceof Error ? e.message : '采集端目录读取失败'
      catalog.value = null; workbenchId.value = ''; accountUid.value = ''; preview.value = null
    }
  } finally { if (request === catalogRequest) { loading.value = false; catalogInFlight = false } }
}
async function connect() {
  if (busy.value || loading.value || !workbench.value || !account.value || (institutional.value && draftStale.value)) return
  const pair = { workbench_id: workbenchId.value, account_uid: accountUid.value, version: saved.value?.version || 0 }
  const nickname = account.value.account_nick || ''
  const request = ++actionRequest
  busy.value = true; error.value = ''; notice.value = ''; preview.value = null
  try {
    const response = await api.selectImBinding(pair)
    if (!response.ok || !response.data) throw Error(response.hint || '连接所选企点号失败')
    if (disposed || request !== actionRequest) return
    if (!samePair(pair, response.data.binding) || !samePair(pair, response.data.contacts)) throw Error('返回的账号或通讯录不属于所选工作台与企点号，请刷新目录重试')
    saved.value = response.data.binding; savedNickname.value = nickname
    preview.value = response.data.contacts
    applyCollectorSelection(response.data.binding, response.data.contacts)
    notice.value = response.data.connected === false
      ? '已保存工作台和企点号，已读取缓存通讯录；目录过期，尚未确认实时连接。'
      : '已保存选择并读取对应通讯录，发送仍须单独授权。'
    emit('saved')
  } catch (e) { if (!disposed && request === actionRequest) error.value = e instanceof Error ? e.message : '连接所选企点号失败' }
  finally { if (request === actionRequest) busy.value = false }
}
async function readSaved() {
  if (busy.value || loading.value || !saved.value || (institutional.value && (!savedAvailable.value || savedStale.value))) return
  const pair = { ...saved.value }, request = ++actionRequest
  busy.value = true; error.value = ''; notice.value = ''; preview.value = null
  try {
    const response = await api.imContacts()
    if (!response.ok || !response.data) throw Error(response.hint || '已选账号通讯录读取失败')
    if (disposed || request !== actionRequest || !samePair(pair, saved.value)) return
    const contacts = response.data as unknown as CollectorContacts
    if (!samePair(pair, contacts)) throw Error('通讯录不属于当前已选工作台与企点号，已忽略该响应')
    preview.value = contacts
    notice.value = '已读取当前已选账号的通讯录，未更换绑定。'
  } catch (e) { if (!disposed && request === actionRequest) error.value = e instanceof Error ? e.message : '已选账号通讯录读取失败' }
  finally { if (request === actionRequest) busy.value = false }
}
async function refreshContacts() {
  if (!saved.value || savedStale.value || !savedAvailable.value || busy.value || syncPending.value) return
  const pair = { ...saved.value }, request = ++actionRequest
  busy.value = true; error.value = ''
  try {
    const r = await api.imContacts(true)
    if (disposed || request !== actionRequest || !samePair(pair, saved.value)) return
    if (!r.ok || !r.data) throw Error(r.hint || '通讯录同步请求失败')
    const book = r.data as unknown as CollectorContacts
    if (!samePair(pair, book)) throw Error('通讯录归属不一致')
    preview.value = book
    syncBaseline = { friends: book.friends_updated_at || 0, org: book.org_updated_at || 0 }
    syncDeadline = Date.now() + 65000; syncPending.value = true
    notice.value = '同步请求已提交，等待个人好友与机构组织架构的新版本；当前显示最近成功缓存。'
  } catch (e) { error.value = e instanceof Error ? e.message : '同步请求失败' }
  finally { busy.value = false }
}
async function pollSync() {
  if (syncReading || disposed || !syncPending.value || !saved.value) return
  if (Date.now() >= syncDeadline || savedStale.value) {
    syncPending.value = false; notice.value = savedStale.value ? '账号已离线，同步尚未完成。' : '尚未收到完整的新版本，请检查采集端状态；上次成功数据保留。'; return
  }
  const pair = { ...saved.value }, generation = actionRequest
  syncReading = true
  try {
    const r = await api.imContacts()
    if (disposed || generation !== actionRequest || !samePair(pair, saved.value) || !syncPending.value) return
    if (!r.ok) throw Error(r.hint || '读取同步结果失败')
    const book = r.data as unknown as CollectorContacts
    if (!samePair(pair, book)) throw Error('同步结果归属不一致')
    preview.value = book
    if ((book.friends_updated_at || 0) > syncBaseline.friends && (book.org_updated_at || 0) > syncBaseline.org && ['ready', 'empty'].includes(book.org_state || '')) {
      syncPending.value = false; notice.value = '已收到新的个人好友与机构组织架构版本。'
    }
  } catch (e) { error.value = e instanceof Error ? e.message : '读取同步结果失败' }
  finally { syncReading = false }
}
defineExpose({ readDirectory: load })
onMounted(() => {
  if (props.autoLoad) void load()
  let ticks = 0
  timer = setInterval(() => {
    now.value = Date.now() / 1000
    if (institutional.value && ++ticks % 5 === 0) void load(false, true)
    if (syncPending.value) void pollSync()
  }, 1000)
})
onBeforeUnmount(() => { disposed = true; catalogRequest++; actionRequest++; syncPending.value = false; if (timer) clearInterval(timer) })
</script>

<template>
  <section class="collector-settings" aria-labelledby="collector-title" data-testid="collector-account-settings">
    <h3 id="collector-title">选择工作台与企点账号</h3>
    <p>从采集端已上报目录选择工作台，再选择该工作台内的企点号，读取该账号对应的通讯录。</p>
    <div v-if="saved" class="summary" data-testid="collector-current">
      <span>当前已选工作台：<b>{{ saved.workbench_id }}</b></span>
      <span>企点账号：<b>{{ savedNickname ? savedNickname + ' · ' : '' }}{{ saved.account_uid }}</b></span>
      <span v-if="!savedAvailable" class="warning">已保存，目录暂不可用</span>
      <span v-else-if="savedStale" class="warning" data-testid="collector-offline">{{ institutional ? '已离线，请在采集端恢复登录' : '已保存，目录已过期' }}</span>
      <span v-else>目录有效 · {{ when(directorySaved.account?.updated_at || directorySaved.wb?.updated_at) }}</span>
    </div>
    <p v-else>尚未选择采集账号。选择目录中的工作台与企点号后连接。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="catalog?.note">{{ catalog.note }}</p>
    <p v-if="catalog && !catalog.workbenches.length" class="warning">采集端尚未上报可用工作台目录，请确认采集端连接并刷新目录。已保存选择不会因此被清除。</p>
    <form @submit.prevent="connect">
      <fieldset :disabled="loading || busy">
        <label>工作台<select v-model="workbenchId" name="collector-workbench" data-testid="collector-workbench" @change="changedWorkbench">
          <option value="" disabled>{{ loading ? '正在读取工作台目录…' : '请选择工作台' }}</option>
          <option v-for="wb in catalog?.workbenches || []" :key="wb.workbench_id" :value="wb.workbench_id">{{ wb.name ? wb.name + ' · ' : '' }}{{ wb.workbench_id }} · {{ wb.stale ? '目录过期' : '目录有效' }} · {{ when(wb.updated_at) }}</option>
        </select></label>
        <label>台内企点号<select v-model="accountUid" name="collector-account" data-testid="collector-account" :disabled="!workbench" @change="changedAccount">
          <option value="" disabled>{{ workbench ? '请选择该工作台内的企点号' : '请先选择工作台' }}</option>
          <option v-for="item in workbench?.accounts || []" :key="item.account_uid" :value="item.account_uid">{{ item.account_nick || '未提供昵称' }} · {{ item.account_uid }} · {{ workbench?.stale || item.stale ? '目录过期' : '目录有效' }} · {{ when(item.updated_at) }}</option>
        </select></label>
      </fieldset>
      <p v-if="workbench && !workbench.accounts.length" class="warning">此工作台尚未上报企点账号，请刷新采集端目录。</p>
      <p v-if="draftStale" class="warning">{{ institutional ? '账号已离线；需先在采集端恢复，不能保存为可用收发账号。' : '目录已过期，可保存选择并读取缓存；尚未确认采集端当前在线，不会因此启用发送。' }}</p>
      <div class="controls">
        <button class="btn-primary" type="submit" data-testid="collector-connect" :disabled="loading || busy || !account || (institutional && draftStale)">保存选择并读取对应通讯录</button>
        <button class="btn-ghost" type="button" data-testid="collector-refresh" :disabled="loading || busy" @click="load(true)">刷新采集端目录</button>
        <button v-if="saved" class="btn-ghost" type="button" data-testid="collector-read-saved" :disabled="loading || busy || (institutional && (!savedAvailable || savedStale))" @click="readSaved">读取当前已选通讯录</button>
        <button v-if="institutional && saved" class="btn-ghost" type="button" data-testid="collector-sync-contacts" :disabled="busy || syncPending || savedStale || !savedAvailable" @click="refreshContacts">{{ syncPending ? '正在等待最新通讯录…' : '同步最新通讯录' }}</button>
      </div>
    </form>
    <div v-if="preview" class="preview" data-testid="collector-contacts">
      <h4>对应通讯录 · {{ preview.workbench_id }} / {{ preview.account_uid }}</h4>
      <p :class="{warning:preview.stale || savedStale}">{{ preview.stale || savedStale ? '过期缓存，尚未确认实时连接' : '已读取' }} · {{ when(preview.updated_at) }}</p>
      <p data-testid="collector-org-status" :class="{warning:preview.org_state !== 'ready'}">{{ orgReadNotice(preview.org, preview) }}<span v-if="preview.org_error"> · {{ preview.org_error }}</span></p>
      <OrgTree v-if="orgTree.length" :nodes="orgTree" />
      <div v-if="preview.contacts?.length" class="people">
        <button v-for="person in preview.contacts" :key="person.uin" class="person" :class="{chosen:imTarget?.uin===person.uin}" type="button" :disabled="institutional && savedStale" @click="selectImTarget(person)">{{ person.name }} · {{ person.uin }}</button>
      </div>
      <p v-if="!orgTree.length && !preview.contacts?.length">该账号对应的通讯录暂未包含联系人或组织架构。</p>
    </div>
  </section>
</template>

<style scoped>
.collector-settings{margin:20px 0 24px;padding:18px;border:1px solid #e8e2dc;border-radius:10px}
h3{margin:0 0 12px}h4{margin:0 0 10px}p{color:#70665e;line-height:1.7;overflow-wrap:anywhere}.summary{display:flex;flex-wrap:wrap;gap:12px;padding:12px;background:#faf8f5;border-radius:8px}.summary span{overflow-wrap:anywhere}
fieldset{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;border:0;padding:0;margin:18px 0}label{min-width:0;display:flex;flex-direction:column;gap:8px;font-weight:600}select{box-sizing:border-box;width:100%;min-width:0;min-height:42px;padding:10px 12px;border:1px solid #d9d0c8;border-radius:7px;background:#fff;color:#2B2320;font:inherit;font-weight:400}select:focus{outline:2px solid #b01c24;outline-offset:1px}
.controls,.people{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}button{min-height:36px;padding:7px 14px;border-radius:8px;font:inherit;line-height:1.5;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}.error{color:#a02e2e}.warning{color:#885600}.preview{padding:16px;border:1px solid #ded7cf;border-radius:8px}.preview h4{overflow-wrap:anywhere}.person{border:1px solid #dacfc6;background:#faf8f5;color:#5c5148}.person.chosen{background:#b01c24;color:#fff;border-color:#b01c24}
@media(max-width:720px){fieldset{grid-template-columns:minmax(0,1fr)}}
</style>
