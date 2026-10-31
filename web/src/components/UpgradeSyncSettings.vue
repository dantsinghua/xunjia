<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, type UpgradeDiagnostics, type UpgradeJobState, type UpgradeSyncStatus } from '../api'
import { authUserId, HttpError } from '../lib/http'

const state = ref<UpgradeSyncStatus | null>(null)
const scheduleEnabled = ref(true), scheduleDirty = ref(false), savingSchedule = ref(false)
const reading = ref(false), submitting = ref(false), fresh = ref(false)
const error = ref(''), submitError = ref(''), reconnecting = ref(false)
const diagnostics = ref<UpgradeDiagnostics | null>(null), diagnosticsOpen = ref(false)
const diagnosticsLoading = ref(false), downloadingLog = ref(false)
const diagnosticsError = ref(''), logError = ref('')
// A lost POST response must not turn a page refresh into another queue download.
const pendingKey = 'xj_upgrade_pending:' + authUserId()
const uncertainBaseline = ref<string | null>(null)
let disposed = false, authExpired = false
let timer: ReturnType<typeof setInterval> | undefined
const requests = new Set<AbortController>()
const jobRequests = new Set<AbortController>()
let jobRevision = 0
const activeStates: UpgradeJobState[] = ['queued', 'downloading', 'verifying', 'installing']
const terminalStates: UpgradeJobState[] = ['succeeded', 'failed', 'empty']
const labels: Record<UpgradeJobState, string> = {
  queued: '等待执行', downloading: '正在同步升级包', verifying: '正在校验升级包',
  installing: '正在安装', succeeded: '升级成功', failed: '升级失败',
  unknown: '结果待核对', empty: '暂无升级包',
}
const phaseLabels: Record<string, string> = {
  ...labels, preflight: '正在检查安装条件', preparing: '正在准备新版本',
  prepared: '新版本准备完成', switching: '正在切换版本', restarting: '正在重启服务',
  checking: '正在检查服务状态', health: '正在检查服务健康状态', health_check: '正在检查服务健康状态',
  rollback: '正在恢复旧版', rolling_back: '正在恢复旧版', rollback_check: '正在检查旧版服务状态',
  recovery: '正在核对中断任务', reconciling: '正在核对中断任务',
}
function phaseLabel(phase: string) { return phaseLabels[phase] || '状态待核对' }
const active = computed(() => !!state.value?.job && activeStates.includes(state.value.job.state))
const unknown = computed(() => state.value?.job?.state === 'unknown' || uncertainBaseline.value !== null)
const detail = computed(() => state.value?.job?.detail)
const stage = computed(() => {
  if (detail.value?.rollback === 'running') return '升级失败，正在恢复旧版'
  if (detail.value?.rollback === 'restored') return '升级失败，已回退旧版'
  if (detail.value?.rollback === 'failed') return '升级失败，自动恢复失败'
  return state.value?.job ? labels[state.value.job.state] || '状态待确认' : '尚无升级任务'
})
const recoveryMessage = computed(() => {
  if (detail.value?.rollback === 'running') return '正在自动恢复升级前版本，请等待恢复结果。'
  if (detail.value?.rollback === 'restored' && detail.value.health === 'failed')
    return '已回退到升级前版本，但旧版服务健康检查未通过，请下载诊断日志并联系技术支持。'
  if (detail.value?.rollback === 'restored') return detail.value.health === 'ready'
    ? '已回退到升级前版本，旧版服务健康检查通过。可继续使用旧版，并下载诊断日志交给技术支持。'
    : '已回退到升级前版本，服务健康状态尚待确认。请查看诊断记录。'
  if (detail.value?.rollback === 'failed') return '自动恢复未通过检查，请下载诊断日志并联系技术支持。'
  if (detail.value?.rollback === 'unknown') return '旧版恢复结果尚未确认，请查看诊断记录，等待结果核对。'
  return ''
})
const canStart = computed(() => !!state.value?.enabled && state.value.can_upgrade && fresh.value &&
  !reading.value && !submitting.value && !unknown.value &&
  !['running', 'failed', 'unknown'].includes(detail.value?.rollback || '') &&
  detail.value?.health !== 'failed' &&
  (!state.value.job || terminalStates.includes(state.value.job.state)))
function when(value?: number) { return value ? new Date(value * 1000).toLocaleString() : '尚无记录' }
function rememberPending(value: string | null) {
  uncertainBaseline.value = value
  try {
    if (value === null) sessionStorage.removeItem(pendingKey)
    else sessionStorage.setItem(pendingKey, value)
  } catch { /* The mounted component still guards uncertain submissions. */ }
}
function applyStatus(value: UpgradeSyncStatus) {
  if (state.value?.job?.id !== value.job?.id) resetDiagnostics()
  state.value = value
  if (!scheduleDirty.value) scheduleEnabled.value = value.schedule?.enabled ?? true
  fresh.value = true; error.value = ''; reconnecting.value = false
  if (uncertainBaseline.value !== null && value.job && value.job.id !== uncertainBaseline.value) rememberPending(null)
  if (diagnosticsOpen.value) void loadDiagnostics()
}
async function saveSchedule() {
  if (savingSchedule.value) return
  savingSchedule.value=true;submitError.value=''
  try {
    const response=await api.saveUpgradeSchedule(scheduleEnabled.value)
    if (!response.ok || !response.data) throw Error(response.hint || '自动升级配置保存失败')
    if (disposed) return
    if (state.value) state.value={...state.value,schedule:response.data}
    scheduleDirty.value=false
  } catch(e) { if (!disposed) submitError.value=e instanceof Error?e.message:'自动升级配置保存失败' }
  finally { savingSchedule.value=false }
}
async function bounded<T>(call: (signal: AbortSignal) => Promise<T>, forJob = false): Promise<T> {
  const controller = new AbortController()
  requests.add(controller)
  if (forJob) jobRequests.add(controller)
  const timeout = setTimeout(() => controller.abort(), 20000)
  try { return await call(controller.signal) }
  finally { clearTimeout(timeout); requests.delete(controller); jobRequests.delete(controller) }
}
function resetDiagnostics() {
  jobRevision++
  for (const request of jobRequests) request.abort()
  jobRequests.clear()
  diagnostics.value = null; diagnosticsOpen.value = false
  diagnosticsLoading.value = false; downloadingLog.value = false
  diagnosticsError.value = ''; logError.value = ''
}
function diagnosticFailure(e: unknown, fallback: string) {
  if (e instanceof HttpError && e.status === 401) {
    authExpired = true; fresh.value = false
    return '登录已过期，请重新登录后查看诊断。'
  }
  if (e instanceof HttpError && e.status === 404) return '当前服务暂未提供此任务的诊断，请刷新升级状态。'
  return fallback
}
async function loadDiagnostics() {
  const jobId = state.value?.job?.id, revision = jobRevision
  if (!jobId || disposed || authExpired || diagnosticsLoading.value) return
  diagnosticsLoading.value = true; diagnosticsError.value = ''
  try {
    const response = await bounded(signal => api.upgradeDiagnostics(jobId, signal), true)
    if (disposed || revision !== jobRevision) return
    if (!response.ok || !response.data || response.data.job?.id !== jobId || !Array.isArray(response.data.events))
      throw Error('INVALID_DIAGNOSTICS')
    diagnostics.value = response.data
  } catch (e) {
    if (!disposed && revision === jobRevision)
      diagnosticsError.value = diagnosticFailure(e, '诊断暂时无法读取，请稍后刷新；升级任务不会重复提交。')
  } finally { if (revision === jobRevision) diagnosticsLoading.value = false }
}
function toggleDiagnostics() {
  diagnosticsOpen.value = !diagnosticsOpen.value
  if (diagnosticsOpen.value) void loadDiagnostics()
}
async function downloadLog() {
  const jobId = state.value?.job?.id, revision = jobRevision
  if (!jobId || disposed || authExpired || downloadingLog.value) return
  downloadingLog.value = true; logError.value = ''
  try {
    const file = await bounded(signal => api.upgradeLog(jobId, signal), true)
    if (disposed || revision !== jobRevision) return
    const url = URL.createObjectURL(file.blob)
    const link = document.createElement('a')
    try {
      link.href = url; link.download = file.filename
      document.body.appendChild(link); link.click()
    } finally {
      link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000)
    }
  } catch (e) {
    if (!disposed && revision === jobRevision)
      logError.value = diagnosticFailure(e, '诊断日志暂时无法下载，请稍后重试；升级任务不会重复提交。')
  } finally { if (revision === jobRevision) downloadingLog.value = false }
}
async function refresh() {
  if (disposed || authExpired || reading.value || submitting.value) return
  reading.value = true
  try {
    const response = await bounded(signal => api.upgradeStatus(signal))
    if (disposed) return
    if (!response.ok || !response.data) throw Error(response.hint || '升级状态读取失败')
    applyStatus(response.data)
  } catch (e) {
    if (disposed) return
    fresh.value = false
    if (e instanceof HttpError && e.status === 401) {
      authExpired = true; reconnecting.value = false; error.value = '登录已过期，请重新登录后查看升级结果。'
    } else if (active.value || uncertainBaseline.value !== null) {
      reconnecting.value = true
      error.value = '暂时无法连接服务，正在等待服务恢复并读取升级结果。请勿重复提交。'
    } else {
      reconnecting.value = false
      error.value = e instanceof HttpError && e.status === 404 ? '服务端尚未提供页面升级功能。' : e instanceof Error ? e.message : '升级状态读取失败'
    }
  } finally { reading.value = false }
}
async function synchronize() {
  if (!canStart.value || disposed) return
  submitting.value = true; error.value = ''; submitError.value = ''; fresh.value = false
  rememberPending(state.value?.job?.id || '')
  try {
    const response = await bounded(signal => api.syncUpgrade(signal))
    if (disposed) return
    if (!response.ok || !response.data) throw Error(response.hint || '升级提交结果尚未确认')
    rememberPending(null)
    applyStatus(response.data)
  } catch (e) {
    if (disposed) return
    // A definite HTTP rejection is safe to display; transport loss is not a rejection.
    if (e instanceof HttpError && e.status >= 400 && e.status < 500 && e.status !== 408) {
      rememberPending(null)
      submitError.value = e.message
      if (e.status === 401) authExpired = true
    } else {
      reconnecting.value = true
      error.value = '提交结果尚未确认，正在查询后台任务。请勿重复提交。'
    }
  } finally {
    submitting.value = false
    if (!disposed) void refresh()
  }
}
onMounted(() => {
  try { uncertainBaseline.value = sessionStorage.getItem(pendingKey) } catch { /* Storage may be unavailable. */ }
  void refresh()
  timer = setInterval(() => { void refresh() }, 5000)
})
onBeforeUnmount(() => {
  disposed = true
  if (timer) clearInterval(timer)
  for (const request of requests) request.abort()
})
</script>

<template>
  <section class="upgrade-settings" aria-labelledby="upgrade-title" data-testid="upgrade-sync-settings" :aria-busy="submitting || active">
    <h3 id="upgrade-title">升级包同步</h3>
    <p>从升级专用通道同步增量升级包，准备期间旧版继续运行；校验通过后切换版本，服务会短暂停顿。升级失败时自动恢复旧版，本页会自动读取升级和恢复结果。</p>
    <div class="autoupgrade" data-testid="upgrade-auto-settings"><slot name="autoupgrade">
      <label><input v-model="scheduleEnabled" type="checkbox" :disabled="!state?.can_upgrade || savingSchedule" @change="scheduleDirty=true" />每天北京时间00:00自动检查并升级</label>
      <p>每天自动检查一次；与手动升级共用互斥保护，正在升级时不会重复执行。手动升级仍可随时发起。</p>
      <p v-if="state?.schedule?.last_day">最近自动检查日期：{{ state.schedule.last_day }}</p>
      <p v-if="state?.log_delivery">诊断日志回传：{{ ({pending:'等待回传',sending:'回传中',uploaded:'已回传文件中转服务',failed:'回传失败，将再次尝试',unknown:'结果待核对，已保留本地日志'} as Record<string,string>)[state.log_delivery.state] || '等待回传' }}</p>
    </slot></div>
    <p v-if="error" :role="reconnecting ? 'status' : 'alert'" :class="reconnecting ? 'warning' : 'error'">{{ error }}</p>
    <p v-if="submitError" role="alert" class="error">{{ submitError }}</p>
    <template v-if="state">
      <dl class="summary">
        <div><dt>当前版本</dt><dd class="build">{{ state.current_build || '暂未取得版本' }}</dd></div>
        <div><dt>最近任务</dt><dd>{{ stage }}</dd></div>
        <div v-if="detail?.phase"><dt>当前阶段</dt><dd>{{ phaseLabel(detail.phase) }}</dd></div>
        <div v-if="detail?.previous_build"><dt>升级前版本</dt><dd class="build">{{ detail.previous_build }}</dd></div>
        <div v-if="state.job?.target_build"><dt>目标版本</dt><dd class="build">{{ state.job.target_build }}</dd></div>
        <div v-if="state.job"><dt>更新时间</dt><dd>{{ when(state.job.updated_at) }}</dd></div>
      </dl>
      <p v-if="state.job?.message" role="status" :class="{ error: state.job.state === 'failed', warning: state.job.state === 'unknown' }">{{ state.job.message }}</p>
      <p v-if="recoveryMessage" role="status" :class="{ error: detail?.rollback === 'failed', warning: detail?.rollback !== 'restored' }" data-testid="upgrade-recovery">{{ recoveryMessage }}</p>
      <p v-if="detail?.code" class="diagnostic-code">错误码：{{ detail.code }}</p>
      <p v-if="state.hint">{{ state.hint }}</p>
      <p v-if="!state.enabled" class="warning">升级通道尚未启用，请联系运维完成配置。</p>
      <p v-else-if="!state.can_upgrade">当前暂不能执行升级，请查看后台返回的状态提示。</p>
      <p v-if="unknown" class="warning" role="status">升级结果尚未确认，已暂停再次同步。可查看诊断或下载日志交给技术支持，系统不会重复下载或安装。</p>
      <p v-if="state.job?.state === 'succeeded'">升级已完成，可刷新页面加载新版本。</p>
      <div v-if="state.job" class="diagnostics" data-testid="upgrade-diagnostics">
        <p v-if="logError" role="alert" class="error">{{ logError }}</p>
        <div v-if="diagnosticsOpen" id="upgrade-timeline" :aria-busy="diagnosticsLoading">
          <p>这里展示已脱敏的升级阶段和结果，可直接下载日志交给技术支持。</p>
          <p v-if="diagnosticsError" role="alert" class="error">{{ diagnosticsError }}</p>
          <p v-if="diagnosticsLoading" role="status">正在读取诊断…</p>
          <p v-if="diagnostics?.job.code" class="diagnostic-code">任务错误码：{{ diagnostics.job.code }}</p>
          <ol v-if="diagnostics?.events.length" class="timeline">
            <li v-for="item in diagnostics.events" :key="item.id">
              <div><time>{{ when(item.at) }}</time> · {{ phaseLabel(item.phase) }}</div>
              <p>{{ item.message }}</p>
              <span v-if="item.code" class="diagnostic-code">{{ item.code }}</span>
            </li>
          </ol>
          <p v-else-if="diagnostics && !diagnosticsLoading">此任务暂无阶段记录，可下载已有诊断摘要。</p>
        </div>
      </div>
      <div class="retention">
        <p>下载文件保留 {{ state.retention?.days ?? state.retention_days }} 天。业务记录的保存期限独立管理。</p>
        <p v-if="state.retention?.last_run">最近清理：{{ when(state.retention.last_run) }}<span v-if="state.retention.removed != null"> · 清理 {{ state.retention.removed }} 项</span><span v-if="state.retention.blocked"> · 保留 {{ state.retention.blocked }} 项受保护或待核对内容</span></p>
        <p v-if="state.retention?.hint">{{ state.retention.hint }}</p>
      </div>
    </template>
    <div class="controls settings-actions">
      <button class="btn-primary" type="button" data-testid="upgrade-sync" :disabled="!canStart" @click="synchronize">{{ submitting ? '正在提交…' : active ? '升级进行中…' : '同步并升级' }}</button>
      <button class="btn-ghost" type="button" :disabled="reading || submitting || authExpired" @click="refresh">{{ reading ? '正在读取…' : '刷新升级状态' }}</button>
      <template v-if="state?.job">
        <button class="btn-ghost" type="button" data-testid="upgrade-diagnostics-toggle" :aria-expanded="diagnosticsOpen" aria-controls="upgrade-timeline" :disabled="authExpired" @click="toggleDiagnostics">{{ diagnosticsOpen ? '收起诊断' : '查看诊断' }}</button>
        <button class="btn-ghost" type="button" data-testid="upgrade-log-download" :disabled="downloadingLog || authExpired" @click="downloadLog">{{ downloadingLog ? '正在下载…' : '下载诊断日志' }}</button>
        <button v-if="diagnosticsOpen" class="btn-ghost" type="button" :disabled="diagnosticsLoading || authExpired" @click="loadDiagnostics">刷新诊断</button>
      </template>
      <slot name="autoupgrade-actions"><button class="btn-ghost" type="button" data-testid="upgrade-auto-save" :disabled="!scheduleDirty || savingSchedule || !state?.can_upgrade" @click="saveSchedule">{{ savingSchedule ? '正在保存…' : '保存自动升级配置' }}</button></slot>
    </div>
  </section>
</template>

<style scoped>
.upgrade-settings{margin:20px 0 24px;padding:18px;border:1px solid #e8e2dc;border-radius:10px}
h3{margin:0 0 12px}p{color:#70665e;line-height:1.7;overflow-wrap:anywhere}.summary{display:grid;gap:12px;padding:12px;background:#faf8f5;border-radius:8px}.summary div{display:grid;grid-template-columns:6em minmax(0,1fr);gap:10px}dt{color:#70665e}dd{margin:0;overflow-wrap:anywhere}.build{font-family:var(--font-code);font-size:13px}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0 0}button{min-height:36px;padding:7px 14px;border-radius:8px;font:inherit;line-height:1.5;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}.error{color:#a02e2e}.warning{color:#885600}.retention{font-size:13px}
.diagnostics{border-top:1px solid #e8e2dc;margin-top:16px}.timeline{padding-left:22px;max-height:360px;overflow:auto}.timeline li{padding:8px 0;overflow-wrap:anywhere}.timeline p{margin:4px 0}.timeline time{color:#70665e;font-size:13px}.diagnostic-code{font-family:var(--font-code);font-size:12px;overflow-wrap:anywhere}
@media(max-width:480px){.summary div{grid-template-columns:minmax(0,1fr);gap:4px}}
</style>
